"""Tests: honest source status (T3, S2).

200-empty, 403 and timeout fixtures through the real fetch functions
(mocked HTTP session), a mixed-success report and the bot digest.

Run: python3 test_source_status.py   (no network, hermetic state)
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import config  # type: ignore[reportMissingImports]  # flat sibling module

_TMP = tempfile.mkdtemp(prefix="tg-sourcestatus-test-")
os.environ["TG_BRIDGE_STATE_DIR"] = _TMP
config.ENV_FILE = Path(_TMP) / "no-such-dir" / ".env"
config.PROFILE_PATH = Path(_TMP) / "no-profile.md"
config.POSTINGS_DIR = Path(_TMP) / "postings"


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


for _target in ("socket.socket.connect", "socket.create_connection",
                "socket.getaddrinfo"):
    mock.patch(_target, _no_network).start()
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


class Resp:
    def __init__(self, code=200, text="", url="https://www.linkedin.com/jobs-guest/x"):
        self.status_code, self.text, self.url = code, text, url


def fetch_with(fn, outcome, *args):
    """Run a real fetch function with a mocked session returning `outcome`."""
    def get(*a, **k):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    session = mock.Mock()
    session.get = get
    with mock.patch.object(sources, "make_session", lambda: session):
        return fn(*args)


SECRET = "Bearer sk-SECRET-TOKEN https://u:pw@host/?key=abc"
CARD = ('<a class="base-card__full-link" href="https://es.linkedin.com/jobs/view/rec-123?x=1">'
        '<h3 class="base-search-card__title">Recepcionista</h3>'
        '<h4 class="base-search-card__subtitle">ACME</h4>'
        '<span class="job-search-card__location">Sevilla</span>')


def test_fixtures() -> None:
    for name, fn in (("linkedin", sources.search_linkedin),
                     ("turijobs", sources.search_turijobs)):
        r = fetch_with(fn, Resp(200, "<html></html>"), "recepcionista")
        check(f"{name} 200-empty is EMPTY, not failed",
              r == [] and r.status == sources.STATUS_EMPTY, r.status)
        r = fetch_with(fn, Resp(403), "recepcionista")
        check(f"{name} 403 is BLOCKED", r == [] and r.status == sources.STATUS_BLOCKED
              and r.detail == "http_403", f"{r.status} {r.detail}")
        r = fetch_with(fn, Resp(500), "recepcionista")
        check(f"{name} 500 is FAILED", r.status == sources.STATUS_FAILED)
        r = fetch_with(fn, requests.Timeout(SECRET), "recepcionista")
        check(f"{name} timeout is FAILED/timeout",
              r.status == sources.STATUS_FAILED and r.detail == "timeout", r.detail)
        check(f"{name} detail never carries exception text",
              "SECRET" not in r.detail and "sk-" not in r.detail)
    r = fetch_with(sources.search_linkedin, Resp(200, CARD, "https://www.linkedin.com/authwall"), "q")
    check("linkedin auth wall is BLOCKED", r.status == sources.STATUS_BLOCKED)
    r = fetch_with(sources.search_linkedin, Resp(200, CARD), "recepcionista", "Sevilla")
    check("linkedin parses cards, status ok, list API intact",
          r.status == sources.STATUS_OK and len(r) == 1 and r[0]["title"] == "Recepcionista"
          and isinstance(r, list))
    check("plain list results read as ok/empty",
          sources.result_status([1]) == ("ok", "") and sources.result_status([]) == ("empty", ""))
    with mock.patch.object(sources, "make_session",
                           lambda: mock.Mock(get=lambda *a, **k: Resp(403))):
        check("scraper.search_turijobs wrapper still returns a plain list",
              type(scraper.search_turijobs("x")) is list)


def test_stderr_has_no_secrets(capsys_text: list) -> None:
    check("stderr/exception secrets not printed", not any("SECRET" in t for t in capsys_text),
          str(capsys_text))


def test_report_and_digest() -> None:
    for f in Path(_TMP).iterdir():
        if f.is_file():
            f.unlink()
    preferences.set_preferences({"roles": ["recepcionista", "cajera"],
                                 "sources": ["linkedin", "turijobs"]})
    good = {"id": "li-1", "title": "Recepcionista", "location": "Madrid",
            "url": "https://x.test/1", "source": "linkedin"}

    def li(query, location=None):
        return sources.SourceResult([dict(good)] if query == "recepcionista" else [])

    def tj(query):
        return sources.SourceResult([], sources.STATUS_BLOCKED, "http_403")

    with mock.patch.dict(sources.SOURCES, {"linkedin": li, "turijobs": tj}), \
            mock.patch.object(scraper.time, "sleep", lambda s: None):
        items, report = scraper.search_with_report()
        legacy = scraper.search_all_lanes()
    check("mixed: good source still delivers items", len(items) == 1 and len(legacy) == 1)
    check("mixed: counters per status",
          report["linkedin"]["ok"] == 1 and report["linkedin"]["empty"] == 1
          and report["turijobs"]["blocked"] == 2, str(report))
    check("outcomes: ok / failed",
          sources.source_outcome(report["linkedin"]) == "ok"
          and sources.source_outcome(report["turijobs"]) == "failed")

    text = bot._digest_text(0, 0, [], source_report=report)
    check("digest with one failed source names it", "Turijobs" in text and "Fallaron" in text, text)
    check("digest hides no secrets/URLs", "http" not in text and "403" not in text, text)

    all_failed = {"linkedin": {"queries": 2, "ok": 0, "empty": 0, "blocked": 0, "failed": 2},
                  "turijobs": report["turijobs"]}
    text = bot._digest_text(0, 0, [], source_report=all_failed)
    check("zero-success never says 'Todo al día'",
          "Todo al día" not in text and "no he podido consultar ningún portal" in text, text)
    empty = {"linkedin": {"queries": 2, "ok": 0, "empty": 2, "blocked": 0, "failed": 0}}
    text = bot._digest_text(0, 0, [], source_report=empty)
    check("genuine empty search keeps 'Todo al día' and honors chosen sources",
          "Todo al día" in text and "LinkedIn" in text and "Turijobs" not in text, text)
    check("legacy call without report unchanged",
          "Todo al día" in bot._digest_text(0, 0, []) and "Madrid" in bot._digest_text(0, 0, []))
    partial = {"linkedin": {"queries": 4, "ok": 3, "empty": 0, "blocked": 0, "failed": 1}}
    text = bot._digest_text(2, 0, [good], source_report=partial)
    check("partial failure listed even with offers", "1 de 4 consultas" in text, text)
    preferences.set_preferences({"locations": ["Sevilla"]})
    check("digest copy not hardcoded to Madrid when location differs",
          "de Madrid" not in bot._digest_text(2, 0, [good]))


def test_push_digest_typed_path() -> None:
    import inspect
    params = inspect.signature(bot.push_digest).parameters
    check("push_digest has no untyped **kwargs",
          all(p.kind is not inspect.Parameter.VAR_KEYWORD for p in params.values()),
          str(list(params)))
    check("push_digest declares explicit detail/report parameters",
          {"detail_pending", "detail_gave_up", "source_report"} <= set(params))
    sent: list[str] = []
    report = {"turijobs": {"queries": 2, "ok": 0, "empty": 0, "blocked": 2, "failed": 0}}
    with mock.patch.object(bot, "send_message",
                           lambda tok, cid, text, keyboard=None: sent.append(text)):
        bot.push_digest("T", 1, 0, 0, [], detail_pending=1, source_report=report)
        bot.push_digest("T", 1, 0, 0, [], detail_gave_up=2)
        bot.push_digest("T", 1, 0, 0, [])
    check("push_digest forwards source_report and counters to the sent text",
          "no he podido consultar ningún portal" in sent[0] and "Todo al día" not in sent[0]
          and "1 ofertas no se pudieron abrir" in sent[0], sent[0])
    check("old counter-only API still works",
          "2 ofertas no pude abrirlas" in sent[1] and "Todo al día" in sent[2], str(sent[1:]))


def main() -> int:
    import io
    from contextlib import redirect_stderr
    buf = io.StringIO()
    with redirect_stderr(buf):
        test_fixtures()
    test_stderr_has_no_secrets([buf.getvalue()])
    test_report_and_digest()
    test_push_digest_typed_path()
    print(f"\n{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
