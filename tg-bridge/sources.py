"""Job-source registry: one fetch function per connected job portal.

The daily search iterates over the user-enabled subset of SOURCES. Every
fetch function takes one query and returns parsed result cards, and must
NEVER raise: on any failure (blocked, rate-limited, redesigned) it logs
to stderr and returns [] so the daily job keeps running.

Posting text is UNTRUSTED input: we only parse and display it, never
follow instructions found inside it.

Turijobs URL pattern (verified 2026-09-28):
    https://www.turijobs.com/es-es/ofertas-trabajo/<slug-with-hyphens>
Detail links look like:
    https://www.turijobs.com/es-es/oferta-trabajo/<city>/<slug>/<numeric-id>

LinkedIn guest endpoint (verified 2026-10-03, works without auth):
    GET https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
        ?keywords=<urlencoded>&location=<urlencoded>&start=0
    with a browser User-Agent. Returns an HTML fragment of
    <div class="base-card ..."> cards. Low-volume personal use only:
    max 1 request per query per run, and the caller sleeps 2s between
    requests. It can rate-limit (429) or block: on ANY failure return an empty SourceResult
(a list subclass carrying status blocked/failed; see below).

Indeed (python-jobspy) and InfoJobs (public server-rendered search) are
PERSONAL-USE, LOW-VOLUME connectors. Their Terms of Service restrict
automated access: at most one page per query, at most
MAX_REQUESTS_PER_RUN requests per portal per run, no commercial use, no
bulk collection. Both are off by default and toggled from the chat.
"""

import re
import sys
import time
import unicodedata
from html import unescape

import requests

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0"
HEADERS = {"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"}

_TURIJOBS_BASE = "https://www.turijobs.com/es-es"
_LINKEDIN_ENDPOINT = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)

_DETAIL_RE = re.compile(
    r'href="(https://www\.turijobs\.com/es-es/oferta-trabajo/[^"]+)"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# LinkedIn fragment: one full-link anchor per result card; the title
# (h3), company (h4) and location (span) sit AFTER the anchor inside the
# same card, so we parse each anchor-to-next-anchor window.
_LI_ANCHOR_RE = re.compile(
    r'<a[^>]*class="[^"]*base-card__full-link[^"]*"[^>]*href="([^"]+)"',
    re.DOTALL,
)
_LI_TITLE_RE = re.compile(r'class="base-search-card__title"[^>]*>(.*?)</h3>', re.DOTALL)
_LI_COMPANY_RE = re.compile(
    r'class="base-search-card__subtitle"[^>]*>(.*?)</h4>', re.DOTALL
)
_LI_LOCATION_RE = re.compile(
    r'class="job-search-card__location"[^>]*>(.*?)</span>', re.DOTALL
)
_LI_JOB_ID_RE = re.compile(r"/jobs/view/[^?#]*?-(\d+)")

DEFAULT_LOCATION = "Madrid"

# Outcome of one fetch. SourceResult is a list subclass, so every caller
# that only iterates/len()s the cards keeps working unchanged.
STATUS_OK = "ok"            # 200 and at least one card parsed
STATUS_EMPTY = "empty"      # 200 and no cards: a genuine empty search
STATUS_BLOCKED = "blocked"  # 403/429/auth wall: the portal refused us
STATUS_FAILED = "failed"    # timeout, connection error, other HTTP error


class SourceResult(list):
    """Result cards plus an explicit, secret-free outcome.

    `detail` is a short code ('http_403', 'timeout', ...): never the raw
    exception text, URL or headers, which may carry request details.
    """

    def __init__(self, items=(), status: str | None = None, detail: str = ""):
        super().__init__(items)
        self.status = status or (STATUS_OK if self else STATUS_EMPTY)
        self.detail = detail


def _failure(status: str, detail: str) -> SourceResult:
    return SourceResult([], status, detail)


def _request_failure(exc: Exception) -> SourceResult:
    kind = (
        "timeout"
        if isinstance(exc, requests.Timeout)
        else "connection_error"
        if isinstance(exc, requests.ConnectionError)
        else "request_error"
    )
    return _failure(STATUS_FAILED, kind)


def _http_failure(code: int) -> SourceResult:
    status = STATUS_BLOCKED if code in (401, 403, 429) else STATUS_FAILED
    return _failure(status, f"http_{code}")


def result_status(result) -> tuple[str, str]:
    """(status, detail) of any fetch result, plain lists included."""
    status = getattr(result, "status", None)
    if status in (STATUS_OK, STATUS_EMPTY, STATUS_BLOCKED, STATUS_FAILED):
        return status, str(getattr(result, "detail", ""))
    return (STATUS_OK if result else STATUS_EMPTY), ""


def new_report() -> dict:
    return {}


def record(report: dict, source: str, status: str) -> None:
    stats = report.setdefault(
        source,
        {"queries": 0, STATUS_OK: 0, STATUS_EMPTY: 0, STATUS_BLOCKED: 0, STATUS_FAILED: 0},
    )
    stats["queries"] += 1
    stats[status] += 1


def source_outcome(stats: dict) -> str:
    """'ok' (all queries answered), 'partial' or 'failed' (none answered)."""
    answered = stats.get(STATUS_OK, 0) + stats.get(STATUS_EMPTY, 0)
    if stats.get("queries", 0) == 0 or answered == 0:
        return "failed"
    return "ok" if answered == stats["queries"] else "partial"


def slugify(query: str) -> str:
    """'agente de viajes' -> 'agente-de-viajes' (Turijobs URL slug)."""
    plain = unicodedata.normalize("NFKD", query.lower())
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")


def clean(text: str) -> str:
    """Strip tags and collapse whitespace from an HTML fragment."""
    return _WS_RE.sub(" ", unescape(_TAG_RE.sub(" ", text))).strip()


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def display_name(source: str) -> str:
    """Spanish-facing portal name ('turijobs' -> 'Turijobs')."""
    return {
        "turijobs": "Turijobs",
        "linkedin": "LinkedIn",
        "indeed": "Indeed",
        "infojobs": "InfoJobs",
    }.get(source, source)


def search_turijobs(query: str, location: str | None = None) -> SourceResult:
    """Return parsed result cards for one query (empty SourceResult on failure).

    Turijobs slug search has no evidenced location parameter, so
    `location` is accepted for a uniform signature but unused: results
    are filtered by location afterwards (scraper._is_relevant).
    """
    slug = slugify(query)
    if not slug:
        return _failure(STATUS_FAILED, "invalid_query")
    url = f"{_TURIJOBS_BASE}/ofertas-trabajo/{slug}"
    session = make_session()
    try:
        response = session.get(url, timeout=30)
    except requests.RequestException as exc:
        print(f"[sources] turijobs request failed ({type(exc).__name__})", file=sys.stderr)
        return _request_failure(exc)
    if response.status_code != 200:
        print(
            f"[sources] turijobs HTTP {response.status_code} for {url} "
            "(blocked or moved?)",
            file=sys.stderr,
        )
        return _http_failure(response.status_code)

    html_text = response.text
    results: list[dict] = []
    seen_urls: set[str] = set()
    for match in _DETAIL_RE.finditer(html_text):
        detail_url, inner = match.group(1), match.group(2)
        title = clean(inner)
        if not title or detail_url in seen_urls:
            continue
        seen_urls.add(detail_url)
        # URL shape: .../oferta-trabajo/<city>/<slug>/<id>
        parts = detail_url.rstrip("/").split("/")
        city = parts[-3].replace("-", " ") if len(parts) >= 3 else ""
        job_id = parts[-1] if parts else detail_url
        results.append(
            {
                "id": f"tj-{job_id}",
                "title": title,
                "company": "",  # parsed on the detail page
                "location": city.title(),
                "url": detail_url,
                "snippet": "",
                "source": "turijobs",
                "query": query,
            }
        )
    return SourceResult(results)


def search_linkedin(query: str, location: str | None = None) -> SourceResult:
    """Return parsed guest-endpoint cards for one query ([] on ANY failure).

    The LinkedIn guest endpoint can rate-limit (429), block or redirect
    to an auth wall at any moment; none of those may crash the daily
    job. One request per query, caller sleeps between requests.
    """
    try:
        response = make_session().get(
            _LINKEDIN_ENDPOINT,
            params={
                "keywords": query,
                "location": location or DEFAULT_LOCATION,
                "start": 0,
            },
            timeout=30,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        print(f"[sources] linkedin request failed ({type(exc).__name__})", file=sys.stderr)
        return _request_failure(exc)
    if response.status_code != 200:
        print(
            f"[sources] linkedin HTTP {response.status_code} for {query!r} "
            "(rate-limited, blocked or redirected?)",
            file=sys.stderr,
        )
        return _http_failure(response.status_code)
    if "jobs-guest" not in response.url:
        # Redirected to a login/auth wall: no usable cards in the body.
        print("[sources] linkedin redirected to an auth wall", file=sys.stderr)
        return _failure(STATUS_BLOCKED, "auth_wall")

    html_text = response.text
    anchors = list(_LI_ANCHOR_RE.finditer(html_text))
    results: list[dict] = []
    seen_ids: set[str] = set()
    for index, match in enumerate(anchors):
        raw_url = match.group(1)
        job_id_match = _LI_JOB_ID_RE.search(raw_url)
        if not job_id_match:
            continue
        job_id = job_id_match.group(1)
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)
        # Card window: from this anchor up to the next anchor (or EOF);
        # title, company and location appear after the anchor in a card.
        window = html_text[match.end(): anchors[index + 1].start() if index + 1 < len(anchors) else len(html_text)]
        title_match = _LI_TITLE_RE.search(window)
        title = clean(title_match.group(1)) if title_match else ""
        company_match = _LI_COMPANY_RE.search(window)
        company = clean(company_match.group(1)) if company_match else ""
        location_match = _LI_LOCATION_RE.search(window)
        location = clean(location_match.group(1)) if location_match else ""
        results.append(
            {
                "id": f"li-{job_id}",
                "title": title,
                "company": company,
                "location": location,
                # Canonical URL: no tracking params, no country subdomain.
                "url": f"https://www.linkedin.com/jobs/view/{job_id}",
                "snippet": "",
                "source": "linkedin",
                "query": query,
            }
        )
    return SourceResult(results)



# ---------------------------------------------------------------- shared
# Per-run request ceilings for the ToS-sensitive portals (the scraper
# applies min(this, its own global bound)); keeps daily volume low.
MAX_REQUESTS_PER_RUN = {"indeed": 8, "infojobs": 8}

# Posting text captured at search time, keyed by offer URL, so the
# evaluator needs no extra detail fetch (scraper.fetch_posting reads it).
_POSTING_CACHE: dict[str, dict] = {}


def cached_posting(url: str) -> dict | None:
    return _POSTING_CACHE.get(url)


def _is_remote_place(location) -> bool:
    folded = unicodedata.normalize("NFKD", str(location or "").lower())
    return any(m in folded for m in ("teletrabajo", "remoto", "remote"))


def _text(value) -> str:
    """Cell -> str; NaN/None/non-text (pandas missing values) -> ''."""
    return value.strip() if isinstance(value, str) else ""


# ---------------------------------------------------------------- Indeed
_INDEED_JK_RE = re.compile(r"[?&]jk=([0-9A-Za-z]+)")
_INDEED_RESULTS = 10
_BLOCK_CODE_RE = re.compile(r"\b(403|429)\b")


def _indeed_error_result(errors) -> SourceResult:
    """Map jobspy's error list to blocked/failed with a short code only."""
    blob = " ".join(str(e) for e in errors).lower()
    code = _BLOCK_CODE_RE.search(blob)
    if code:
        return _failure(STATUS_BLOCKED, f"http_{code.group(1)}")
    if "forbidden" in blob or "captcha" in blob or "blocked" in blob:
        return _failure(STATUS_BLOCKED, "blocked")
    return _failure(STATUS_FAILED, "scrape_error")


def search_indeed(query: str, location: str | None = None) -> SourceResult:
    """One Indeed (Spain) query through python-jobspy; never raises.

    jobspy is imported lazily so the bot keeps running under a python
    without it (-> failed 'jobspy_missing'). Description text comes with
    the result, so no detail fetch is needed.
    """
    term = " ".join(str(query).split())
    if not term:
        return _failure(STATUS_FAILED, "invalid_query")
    if " " in term and not (term.startswith('"') and term.endswith('"')):
        term = f'"{term}"'
    place = location if location and not _is_remote_place(location) else DEFAULT_LOCATION
    try:
        from jobspy import scrape_jobs  # type: ignore[reportMissingImports]
    except ImportError:
        print("[sources] indeed unavailable: python-jobspy not installed", file=sys.stderr)
        return _failure(STATUS_FAILED, "jobspy_missing")
    try:
        frame = scrape_jobs(
            site_name=["indeed"],
            search_term=term,
            location=place,
            country_indeed="Spain",
            results_wanted=_INDEED_RESULTS,
            hours_old=72,
            verbose=0,
        )
        records = list(frame.to_dict("records")) if not getattr(frame, "empty", False) else []
        errors = (getattr(frame, "attrs", None) or {}).get("errors")
    except Exception as exc:  # noqa: BLE001  (third-party library: never crash the run)
        print(f"[sources] indeed failed ({type(exc).__name__})", file=sys.stderr)
        return _failure(STATUS_FAILED, "jobspy_error")

    results: list[dict] = []
    seen: set[str] = set()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        url = _text(rec.get("job_url"))
        raw_id = _text(rec.get("id"))
        jk_match = _INDEED_JK_RE.search(url)
        job_id = (raw_id[3:] if raw_id.startswith("in-") else raw_id) or (
            jk_match.group(1) if jk_match else ""
        )
        if not job_id or job_id in seen:
            continue
        if not url:
            url = f"https://es.indeed.com/viewjob?jk={job_id}"
        seen.add(job_id)
        description = _text(rec.get("description"))
        item = {
            "id": f"in-{job_id}",
            "title": _text(rec.get("title")),
            "company": _text(rec.get("company")),
            "location": _text(rec.get("location")),
            "url": url,
            "snippet": "",
            "source": "indeed",
            "query": query,
            "description": description,
        }
        results.append(item)
        if description:
            _POSTING_CACHE[url] = {"text": description, "company": item["company"]}
    if not results and errors:
        return _indeed_error_result(errors if isinstance(errors, (list, tuple)) else [errors])
    return SourceResult(results)


# ---------------------------------------------------------------- InfoJobs
# Port of the HTML parsing and request headers of the reviewed InfoJobs
# connector helpers.ts (MIT licence), repo
# https://github.com/erikpr1994/ai-job-search , commit 0d019f2.
# Personal use only: InfoJobs' ToS restricts automated access.
INFOJOBS_SEARCH_URL = "https://www.infojobs.net/jobsearch/search-results/list.xhtml"
INFOJOBS_DETAIL_BASE = "https://www.infojobs.net/empleo/empleo"
INFOJOBS_TIMEOUT = 15
INFOJOBS_RETRY_SECONDS = 2
INFOJOBS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}
_CAPTCHA_MARKERS = (
    "captcha", "geetest", "datadome", "px-captcha", "unusual traffic",
    "acceso denegado", "access denied", "verifica que eres humano",
)
_IJ_LINK_RE = re.compile(
    r'class="ij-OfferCardContent-description-link[^"]*"[^>]*href="([^"]+)"', re.I
)
_IJ_TITLE_RE = re.compile(
    r'class="ij-OfferCardContent-description-title-link"[^>]*>(.*?)</span>', re.I | re.S
)
_IJ_ARIA_RE = re.compile(
    r'class="ij-OfferCardContent-description-link[^"]*"[^>]*aria-label="([^"]*)"', re.I
)
_IJ_SUB_RE = re.compile(
    r'class="ij-OfferCardContent-description-subtitle"[^>]*>(.*?)</h3>', re.I | re.S
)
_IJ_LOC_RE = re.compile(
    r'class="ij-OfferCardContent-description-list-item-truncate"[^>]*>(.*?)</span>',
    re.I | re.S,
)
_IJ_ITEM_RE = re.compile(
    r'class="ij-OfferCardContent-description-list-item"[^>]*>(.*?)</li>', re.I | re.S
)
_IJ_DESC_RE = re.compile(r"Descripci[oó]n</h3>(.*?)</article>", re.I | re.S)
_IJ_DETAIL_TITLE_RE = re.compile(
    r'class="[^"]*ij-Heading-title1[^"]*"[^>]*>(.*?)</h1>', re.I | re.S
)


def _ij_url(href: str) -> str:
    url = unescape(href).split("?")[0].strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://www.infojobs.net" + url
    return url


def _ij_get(session, url: str, params: dict | None):
    """GET with at most ONE retry on 429/5xx. Returns Response or SourceResult."""
    response = None
    for attempt in range(2):
        try:
            response = session.get(
                url, params=params, timeout=INFOJOBS_TIMEOUT, allow_redirects=True
            )
        except requests.RequestException as exc:
            print(f"[sources] infojobs request failed ({type(exc).__name__})", file=sys.stderr)
            return _request_failure(exc)
        code = response.status_code
        if (code == 429 or code >= 500) and attempt == 0:
            time.sleep(INFOJOBS_RETRY_SECONDS)
            continue
        break
    if response.status_code != 200:
        print(f"[sources] infojobs HTTP {response.status_code}", file=sys.stderr)
        return _http_failure(response.status_code)
    return response


def _parse_infojobs(html_text: str, query: str) -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()
    for chunk in html_text.split('id="job-title-')[1:]:
        id_match = re.match(r'([0-9a-f]+)"', chunk, re.I)
        if not id_match:
            continue
        job_id = id_match.group(1).lower()
        if job_id in seen:
            continue
        link = _IJ_LINK_RE.search(chunk)
        url = _ij_url(link.group(1)) if link else f"{INFOJOBS_DETAIL_BASE}/of-i{job_id}"
        title_match = _IJ_TITLE_RE.search(chunk)
        title = clean(title_match.group(1)) if title_match else ""
        if not title:
            aria = _IJ_ARIA_RE.search(chunk)
            title = unescape(aria.group(1)).strip() if aria else ""
        if not title:
            continue
        seen.add(job_id)
        sub = _IJ_SUB_RE.search(chunk)
        company = clean(sub.group(1)) if sub else ""
        loc = _IJ_LOC_RE.search(chunk)
        location = clean(loc.group(1)) if loc else ""
        modality = ""
        for li in _IJ_ITEM_RE.finditer(chunk):
            text = clean(li.group(1))
            if text and not re.search(r"truncate|FormatterSincedate", li.group(1), re.I) \
                    and text != location:
                modality = text
                break
        _POSTING_CACHE.setdefault(url, {"text": "", "company": company})
        results.append({
            "id": f"ij-{job_id}",
            "title": title,
            "company": company,
            "location": location,
            "url": url,
            "snippet": modality,
            "source": "infojobs",
            "query": query,
        })
    return results


def search_infojobs(query: str, location: str | None = None) -> SourceResult:
    """One InfoJobs search page for one query (never raises, max 1 page).

    The location is appended to the keyword (no evidenced location
    parameter); the scraper's location filter drops other provinces.
    """
    keyword = " ".join(str(query).split())
    if not keyword:
        return _failure(STATUS_FAILED, "invalid_query")
    if location and not _is_remote_place(location):
        keyword = f"{keyword} {location}"
    response = _ij_get(
        make_infojobs_session(),
        INFOJOBS_SEARCH_URL,
        {"keyword": keyword, "sinceDate": "_7_DAYS"},
    )
    if isinstance(response, SourceResult):
        return response
    html_text = response.text
    items = _parse_infojobs(html_text, query)
    if not items:
        folded = html_text.lower()
        if any(marker in folded for marker in _CAPTCHA_MARKERS):
            print("[sources] infojobs anti-bot page", file=sys.stderr)
            return _failure(STATUS_BLOCKED, "captcha")
        return SourceResult([])
    return SourceResult(items)


def make_infojobs_session() -> requests.Session:
    session = make_session()
    session.headers.update(INFOJOBS_HEADERS)
    return session


def fetch_infojobs_detail(url: str) -> str:
    """Description text of one InfoJobs offer page ('' on any failure)."""
    response = _ij_get(make_infojobs_session(), url, None)
    if isinstance(response, SourceResult):
        return ""
    html_text = response.text
    desc = _IJ_DESC_RE.search(html_text)
    if not desc:
        return ""
    with_breaks = re.sub(r"<\s*br\s*/?>", "\n", desc.group(1), flags=re.I)
    with_breaks = re.sub(r"</(p|li|ul|ol|div|h\d)>", "\n", with_breaks, flags=re.I)
    lines = [_WS_RE.sub(" ", unescape(_TAG_RE.sub(" ", l))).strip() for l in with_breaks.split("\n")]
    body = "\n".join(l for l in lines if l)
    title = _IJ_DETAIL_TITLE_RE.search(html_text)
    head = clean(title.group(1)) if title else ""
    return f"{head}\n{body}".strip() if body else ""

# Registry of connected portals. Enabled portals live in
# preferences.json ("sources"); anything not listed here cannot be
# enabled and is recorded as a <<PETICION>> request instead.
SOURCES = {
    "turijobs": search_turijobs,
    "linkedin": search_linkedin,
    "indeed": search_indeed,
    "infojobs": search_infojobs,
}
# Portals whose search takes a location (LinkedIn param, Indeed param,
# InfoJobs keyword suffix).
LOCATION_AWARE = {"linkedin", "indeed", "infojobs"}


def valid_source(name) -> bool:
    return isinstance(name, str) and name in SOURCES


def main() -> int:
    if "--selftest" not in sys.argv:
        print("usage: python3 sources.py --selftest")
        return 2
    query = "agente de viajes"
    failures: list[str] = []
    for name, fetch in SOURCES.items():
        items = fetch(query)
        print(f"[selftest] {name}: {len(items)} results for {query!r}")
        for item in items[:3]:
            print(f"  - {item['title']} | {item['source']} | {item['url']}")
        if not items:
            failures.append(name)
    if failures:
        print(
            f"[selftest] FAIL: no results from: {', '.join(failures)} "
            "(rate-limited, blocked or parser broken)",
            file=sys.stderr,
        )
        return 1
    print("[selftest] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
