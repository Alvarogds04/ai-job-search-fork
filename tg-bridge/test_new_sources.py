"""Tests: Indeed (python-jobspy) and InfoJobs connectors.

Offline only: fake jobspy module injected via sys.modules, synthetic
InfoJobs HTML (fictional companies), mocked HTTP session.

Run: python3 test_new_sources.py   (no network, hermetic state)
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


# Fail-closed socket guard BEFORE any project import.
for _target in ("socket.socket.connect", "socket.create_connection",
                "socket.getaddrinfo"):
    mock.patch(_target, _no_network).start()

import config  # type: ignore[reportMissingImports]  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="tg-newsources-test-")
os.environ["TG_BRIDGE_STATE_DIR"] = _TMP
config.ENV_FILE = Path(_TMP) / "no-such-dir" / ".env"
config.PROFILE_PATH = Path(_TMP) / "no-profile.md"
config.POSTINGS_DIR = Path(_TMP) / "postings"
if config.state_path("p.json").parent != Path(_TMP) or config.ENV_FILE.exists():
    sys.exit("state isolation failed: refusing to run")

import bot  # type: ignore[reportMissingImports]  # noqa: E402
import preferences  # type: ignore[reportMissingImports]  # noqa: E402
import requests  # noqa: E402
import scraper  # type: ignore[reportMissingImports]  # noqa: E402
import sources  # type: ignore[reportMissingImports]  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"ok   - {name}")
    else:
        print(f"FAIL - {name}" + (f" :: {detail}" if detail else ""))
        FAILURES.append(name)


SECRET = "Bearer sk-SECRET-TOKEN https://u:pw@host/?key=abc"


# ---------------------------------------------------------------- Indeed
class FakeDF:
    """DataFrame-like stub: no pandas needed."""

    def __init__(self, records, errors=None):
        self._records = records
        self.attrs = {"errors": errors} if errors is not None else {}
        self.empty = not records

    def to_dict(self, orient="dict"):
        assert orient == "records"
        return [dict(r) for r in self._records]


def fake_jobspy(df=None, raises=None):
    calls: list[dict] = []
    module = mock.Mock()

    def scrape_jobs(**kwargs):
        calls.append(kwargs)
        if raises:
            raise raises
        return df if df is not None else FakeDF([])

    module.scrape_jobs = scrape_jobs
    return module, calls


NAN = float("nan")
REC = {"id": "in-abc123def", "title": "Agente de viajes", "company": "Viajes Ficticios SL",
       "location": "Madrid, ES", "job_url": "https://es.indeed.com/viewjob?jk=abc123def",
       "description": "Venta de paquetes turisticos.\nIncluye jornada completa.",
       "is_remote": False}


def run_indeed(df=None, raises=None, query="agente de viajes", location="Madrid"):
    module, calls = fake_jobspy(df, raises)
    with mock.patch.dict(sys.modules, {"jobspy": module}):
        return sources.search_indeed(query, location), calls


def test_indeed() -> None:
    r, calls = run_indeed(FakeDF([REC]))
    check("indeed ok with one item", r.status == sources.STATUS_OK and len(r) == 1, r.status)
    item = r[0]
    check("indeed item has the shared shape",
          {"id", "title", "company", "location", "url", "snippet", "source", "query"} <= set(item))
    check("indeed id prefixed in- exactly once", item["id"] == "in-abc123def", item["id"])
    check("indeed uses job_url and source", item["url"] == REC["job_url"] and item["source"] == "indeed")
    check("indeed passes description through", item["description"] == REC["description"])
    kw = calls[0]
    check("indeed call args", kw["site_name"] == ["indeed"] and kw["country_indeed"] == "Spain"
          and kw["hours_old"] == 72 and kw["verbose"] == 0 and kw["location"] == "Madrid"
          and kw["results_wanted"] <= 10, str(kw))
    check("multiword query is quoted", kw["search_term"] == '"agente de viajes"', kw["search_term"])
    _, c1 = run_indeed(FakeDF([]), query="camarera")
    check("single word query not quoted", c1[0]["search_term"] == "camarera")
    _, c2 = run_indeed(FakeDF([]), query='"ya entre comillas"')
    check("already quoted query not double quoted", c2[0]["search_term"] == '"ya entre comillas"')
    _, c3 = run_indeed(FakeDF([]), location=None)
    check("no location falls back to the default like LinkedIn",
          c3[0]["location"] == sources.DEFAULT_LOCATION, c3[0]["location"])
    _, c4 = run_indeed(FakeDF([]), location="Teletrabajo")
    check("remote location word falls back to default", c4[0]["location"] == sources.DEFAULT_LOCATION)

    nan_rec = {"id": NAN, "title": "Camarera", "company": NAN, "location": NAN,
               "job_url": "https://es.indeed.com/viewjob?jk=zz9", "description": NAN}
    r = run_indeed(FakeDF([nan_rec]))[0]
    check("NaN cells become empty text, jk from url",
          len(r) == 1 and r[0]["company"] == "" and r[0]["id"] == "in-zz9"
          and r[0]["description"] == "", str(r))
    dup = run_indeed(FakeDF([REC, dict(REC)]))[0]
    check("duplicate ids collapsed", len(dup) == 1)
    r = run_indeed(FakeDF([]))[0]
    check("indeed empty is EMPTY", r.status == sources.STATUS_EMPTY)

    # Failure mapping: never exception text.
    r = run_indeed(FakeDF([], errors=["HTTP 403 forbidden " + SECRET]))[0]
    check("attrs errors 403 -> blocked", r.status == sources.STATUS_BLOCKED and r.detail == "http_403", r.detail)
    r = run_indeed(FakeDF([], errors=["429 Too Many Requests"]))[0]
    check("attrs errors 429 -> blocked", r.status == sources.STATUS_BLOCKED and r.detail == "http_429")
    r = run_indeed(FakeDF([], errors=["weird " + SECRET]))[0]
    check("attrs errors other -> failed, no text",
          r.status == sources.STATUS_FAILED and "SECRET" not in r.detail and "http" not in r.detail, r.detail)
    r = run_indeed(FakeDF([REC], errors=["partial"]))[0]
    check("errors with results still ok", r.status == sources.STATUS_OK and len(r) == 1)
    r = run_indeed(raises=RuntimeError(SECRET))[0]
    check("scrape_jobs exception -> failed, no text",
          r.status == sources.STATUS_FAILED and "SECRET" not in r.detail and r.detail == "jobspy_error", r.detail)
    with mock.patch.dict(sys.modules, {"jobspy": None}):
        r = sources.search_indeed("camarera", "Madrid")
    check("ImportError -> failed jobspy_missing",
          r.status == sources.STATUS_FAILED and r.detail == "jobspy_missing", f"{r.status} {r.detail}")
    check("empty query is rejected", run_indeed(FakeDF([]), query="  ")[0].status == sources.STATUS_FAILED)


# -------------------------------------------------------------- InfoJobs
def card(hexid, title, company, location, modality):
    return (
        f'<div><a id="job-title-{hexid}" class="ij-OfferCardContent-description-link x" '
        f'href="//www.infojobs.net/madrid/{hexid}/of-i{hexid}?applicationOrigin=search">'
        f'<span class="ij-OfferCardContent-description-title-link">{title}</span></a>'
        f'<h3 class="ij-OfferCardContent-description-subtitle"><a href="/c/x">{company}</a></h3>'
        f'<ul><li class="ij-OfferCardContent-description-list-item">'
        f'<span class="ij-OfferCardContent-description-list-item-truncate">{location}</span></li>'
        f'<li class="ij-OfferCardContent-description-list-item">{modality}</li>'
        f'<li class="ij-OfferCardContent-description-list-item"><span class="ij-FormatterSincedate">Hace 2 d</span></li>'
        f'</ul></div>'
    )


HTML = ("<html><div class='ad'>banner</div>"
        + card("a1b2c3d4e5f6", "Agente de viajes &amp; reservas", "Agencia Imaginaria SA", "Sevilla", "Presencial")
        + card("0f9e8d7c6b5a", "Vendedor/a de billetes", "Turismo Inventado SL", "Madrid", "Solo teletrabajo")
        + "</html>")
DETAIL = ('<html><h1 class="ij-Heading-title1 big">Agente de viajes</h1>'
          '<h3>Descripción</h3><p>Gestion de reservas.</p><ul><li>Ingles B2</li></ul></article></html>')
CAPTCHA = "<html><title>Verificación</title><div id='px-captcha'>Please complete the captcha</div></html>"


class Resp:
    def __init__(self, code=200, text="", url="https://www.infojobs.net/x"):
        self.status_code, self.text, self.url = code, text, url


def ij(outcome, *args, **kwargs):
    """Run the real fetcher with a scripted session; returns (result, gets)."""
    outcomes = list(outcome) if isinstance(outcome, list) else [outcome]
    gets: list[dict] = []

    def get(url, **k):
        gets.append({"url": url, **k})
        o = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(o, Exception):
            raise o
        return o

    session = mock.Mock()
    session.get = get
    fn = kwargs.pop("fn", sources.search_infojobs)
    with mock.patch.object(sources, "make_session", lambda: session), \
            mock.patch.object(sources.time, "sleep", lambda s: None):
        return fn(*args, **kwargs), gets


def test_infojobs() -> None:
    r, gets = ij(Resp(200, HTML), "agente de viajes", "Sevilla")
    check("infojobs ok two cards", r.status == sources.STATUS_OK and len(r) == 2, f"{r.status} {len(r)}")
    a, b = r[0], r[1]
    check("infojobs ids ij-<hex>", a["id"] == "ij-a1b2c3d4e5f6" and b["id"] == "ij-0f9e8d7c6b5a", a["id"])
    check("infojobs fields parsed",
          a["title"] == "Agente de viajes & reservas" and a["company"] == "Agencia Imaginaria SA"
          and a["location"] == "Sevilla" and a["source"] == "infojobs" and a["query"] == "agente de viajes")
    check("infojobs url normalised (https, no query)",
          a["url"] == "https://www.infojobs.net/madrid/a1b2c3d4e5f6/of-ia1b2c3d4e5f6", a["url"])
    check("infojobs modality kept in snippet", "Presencial" in a["snippet"] and "teletrabajo" in b["snippet"].lower())
    g = gets[0]
    check("infojobs request params/headers",
          g["url"] == sources.INFOJOBS_SEARCH_URL and g["params"]["sinceDate"] == "_7_DAYS"
          and g["params"]["keyword"] == "agente de viajes Sevilla" and g["timeout"] == 15
          and len(gets) == 1, str(g))
    check("infojobs uses the connector browser UA",
          "Chrome/120" in sources.INFOJOBS_HEADERS["User-Agent"])
    r, gets = ij(Resp(200, HTML), "camarera", None)
    check("no location -> keyword is the query only", gets[0]["params"]["keyword"] == "camarera")
    r, gets = ij(Resp(200, HTML), "camarera", "Remoto")
    check("remote location not added to keyword", gets[0]["params"]["keyword"] == "camarera")

    r, _ = ij(Resp(200, "<html>nothing</html>"), "q")
    check("infojobs 200 without cards is EMPTY", r.status == sources.STATUS_EMPTY)
    r, _ = ij(Resp(200, CAPTCHA), "q")
    check("captcha page is BLOCKED/captcha",
          r.status == sources.STATUS_BLOCKED and r.detail == "captcha", f"{r.status} {r.detail}")
    r, _ = ij(Resp(403), "q")
    check("infojobs 403 blocked", r.status == sources.STATUS_BLOCKED and r.detail == "http_403")
    r, gets = ij([Resp(429), Resp(200, HTML)], "q")
    check("429 then ok: exactly one retry", r.status == sources.STATUS_OK and len(gets) == 2)
    r, gets = ij(Resp(429), "q")
    check("persistent 429 -> blocked after ONE retry only",
          r.status == sources.STATUS_BLOCKED and r.detail == "http_429" and len(gets) == 2, f"{r.detail} {len(gets)}")
    r, gets = ij(Resp(503), "q")
    check("persistent 5xx -> failed after one retry",
          r.status == sources.STATUS_FAILED and r.detail == "http_503" and len(gets) == 2)
    r, gets = ij(requests.Timeout(SECRET), "q")
    check("timeout -> failed/timeout, no text, no retry",
          r.status == sources.STATUS_FAILED and r.detail == "timeout" and "SECRET" not in r.detail
          and len(gets) == 1)
    r, _ = ij(requests.ConnectionError(SECRET), "q")
    check("connection error -> failed/connection_error", r.detail == "connection_error")

    # Detail path
    text, _ = ij(Resp(200, DETAIL), "https://www.infojobs.net/x/of-iabc", fn=sources.fetch_infojobs_detail)
    check("detail extracts description text",
          "Gestion de reservas" in text and "Ingles B2" in text and "<" not in text, repr(text))
    text, _ = ij(Resp(200, "<html>no title</html>"), "https://www.infojobs.net/x/of-iabc",
                 fn=sources.fetch_infojobs_detail)
    check("detail without description returns empty (T2 retry applies)", text == "")
    text, _ = ij(Resp(500), "https://www.infojobs.net/x/of-iabc", fn=sources.fetch_infojobs_detail)
    check("detail HTTP failure returns empty", text == "")


# ------------------------------------------------ registry / pipeline / bot
def test_registry_and_pipeline() -> None:
    check("both portals registered", {"indeed", "infojobs"} <= set(sources.SOURCES))
    check("defaults unchanged (not enabled by default)",
          preferences.DEFAULT_LOCATIONS == ["Madrid"] and preferences._DEFAULTS["sources"] == ["turijobs"])
    check("display names", sources.display_name("indeed") == "Indeed"
          and sources.display_name("infojobs") == "InfoJobs")
    check("per-run caps are low", sources.MAX_REQUESTS_PER_RUN["indeed"] <= 8
          and sources.MAX_REQUESTS_PER_RUN["infojobs"] <= 8)

    # per-run cap: 6 roles x 2 locations = 12 requests, cap 8
    preferences.set_preferences({"roles": [f"puesto {i}" for i in range(6)],
                                 "locations": ["Madrid", "Sevilla"],
                                 "sources": ["indeed", "infojobs"]})
    count = {"indeed": 0, "infojobs": 0}

    def make(name):
        def fetch(query, location=None):
            count[name] += 1
            return sources.SourceResult([])
        return fetch

    with mock.patch.dict(sources.SOURCES, {"indeed": make("indeed"), "infojobs": make("infojobs")}), \
            mock.patch.object(scraper.time, "sleep", lambda s: None):
        _, report = scraper.search_with_report()
    check("per-run request cap enforced", count == {"indeed": 8, "infojobs": 8}, str(count))
    check("report counts capped queries", report["indeed"]["queries"] == 8)
    check("both portals receive a location", {"indeed", "infojobs"} <= sources.LOCATION_AWARE)

    # detail text: indeed uses stored description, no HTTP
    sources.search_indeed  # noqa: B018
    run_indeed(FakeDF([REC]))
    with mock.patch.object(sources, "make_session", side_effect=AssertionError("no http")):
        text = scraper.fetch_posting(REC["job_url"])
    check("indeed detail comes from stored description, no HTTP",
          "paquetes turisticos" in text and scraper.parse_company(text) == "Viajes Ficticios SL", repr(text))

    # infojobs: detail via the offer page
    url = "https://www.infojobs.net/madrid/a1b2/of-ia1b2"
    ij(Resp(200, HTML), "q")
    sess = mock.Mock(get=lambda *a, **k: Resp(200, DETAIL))
    with mock.patch.object(sources, "make_session", lambda: sess):
        text = scraper.fetch_posting(url)
    check("infojobs detail gives evaluator text", "Gestion de reservas" in text, repr(text))
    sess = mock.Mock(get=lambda *a, **k: Resp(500))
    with mock.patch.object(sources, "make_session", lambda: sess), \
            mock.patch.object(sources.time, "sleep", lambda s: None):
        check("infojobs detail failure -> '' (retry logic applies)", scraper.fetch_posting(url) == "")

    # location filter still applies to InfoJobs results from other provinces
    far = {"id": "ij-1", "title": "Agente", "location": "Sevilla", "url": "https://www.infojobs.net/s/of-i1"}
    near = {"id": "ij-2", "title": "Agente", "location": "Madrid", "url": "https://www.infojobs.net/m/of-i2"}
    check("scraper filter drops other provinces",
          not scraper._is_relevant(far, ["Madrid"]) and scraper._is_relevant(near, ["Madrid"]))


def test_toggle_and_digest() -> None:
    for f in Path(_TMP).iterdir():
        if f.is_file():
            f.unlink()
    ok, msg = bot._apply_action({"accion": "agregar_fuente", "valor": "Indeed"})
    check("agregar_fuente indeed", ok and "Indeed" in msg, msg)
    ok, msg = bot._apply_action({"accion": "agregar_fuente", "valor": "infojobs"})
    check("agregar_fuente infojobs", ok and "InfoJobs" in msg, msg)
    check("both enabled in state",
          preferences.get_preferences()["sources"] == ["turijobs", "indeed", "infojobs"])
    ok, msg = bot._apply_action({"accion": "quitar_fuente", "valor": "indeed"})
    check("quitar_fuente indeed", ok and preferences.get_preferences()["sources"] == ["turijobs", "infojobs"], msg)
    ok, msg = bot._apply_action({"accion": "agregar_fuente", "valor": "jooble"})
    check("unknown portal still rejected", not ok)

    prompt = bot._build_system_prompt("hola", [])
    check("prompt lists the connected portals dynamically",
          "Indeed" in prompt and "InfoJobs" in prompt, prompt[:0])

    report = {"turijobs": {"queries": 2, "ok": 2, "empty": 0, "blocked": 0, "failed": 0},
              "infojobs": {"queries": 2, "ok": 0, "empty": 0, "blocked": 2, "failed": 0},
              "indeed": {"queries": 2, "ok": 0, "empty": 0, "blocked": 0, "failed": 2}}
    text = bot._digest_text(0, 0, [], source_report=report)
    check("digest names failed new sources", "InfoJobs" in text and "Indeed" in text, text)
    check("digest hides codes/URLs", "http" not in text and "jobspy" not in text, text)


def main() -> int:
    test_indeed()
    test_infojobs()
    test_registry_and_pipeline()
    test_toggle_and_digest()
    print(f"\n{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
