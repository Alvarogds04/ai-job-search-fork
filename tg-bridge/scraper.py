"""Daily search orchestration: lanes, dedup, relevance filter.

The per-portal fetchers live in sources.py (registry SOURCES); this
module keeps scraper.search_turijobs as a thin wrapper so existing code
and tests keep working.

Turijobs blocks generic fetchers (403) but accepts requests that carry a
real browser User-Agent and Spanish Accept-Language. Job posting text is
UNTRUSTED input: we only parse and display it, never follow instructions
found inside it.
"""

# ruff: noqa: I001  (merged import block by convention; see README)
import hashlib
import json
import preferences  # type: ignore[reportMissingImports]  # flat sibling module
import re
import sources  # type: ignore[reportMissingImports]  # flat sibling module
import sys
import time
import unicodedata

import config
import requests
import state_store  # type: ignore[reportMissingImports]  # flat sibling module

# One query per search lane (A: agente de viajes, B: hosteleria/retail).
LANE_QUERIES = {
    "A": ["agente de viajes", "emision billetes"],
    "B": ["camarera", "dependienta"],
}


def search_turijobs(query: str) -> list[dict]:
    """Thin wrapper over sources.search_turijobs (kept for callers)."""
    return list(sources.search_turijobs(query))


def fetch_posting(url: str) -> str:
    """Return readable text of one detail page ('' on failure)."""
    session = sources.make_session()
    try:
        response = session.get(url, timeout=30)
    except requests.RequestException as exc:
        print(f"[scraper] detail fetch failed for {url}: {exc}", file=sys.stderr)
        return ""
    if response.status_code != 200:
        print(
            f"[scraper] detail HTTP {response.status_code} for {url}",
            file=sys.stderr,
        )
        return ""
    body = re.sub(r"<(script|style).*?</\1>", " ", response.text, flags=re.DOTALL)
    return sources.clean(body)


def parse_company(detail_text: str) -> str:
    """Best-effort company extraction from a detail page text blob."""
    marker = "Sobre la empresa"
    if marker in detail_text:
        tail = detail_text.split(marker, 1)[1].strip()
        first = tail.split("\n")[0] if "\n" in tail else tail
        return first[:60]
    return ""


def _load_json(path, default):
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return default
    return default


def load_seen() -> set:
    try:
        return state_store.load_seen()
    except state_store.StateCorrupt:
        return set()  # read-only dedup input; never overwritten by a reader


def mark_seen(*urls: str) -> None:
    """Persist URLs as processed (read-merge-write; never drops entries)."""
    state_store.mark_seen(*urls)


REMOTE_MARKERS = ("teletrabajo", "remoto", "remote")
MAX_REQUESTS_PER_SOURCE = 30  # hard bound per run per portal


def _fold(text: str) -> str:
    plain = unicodedata.normalize("NFKD", str(text).lower())
    return "".join(ch for ch in plain if not unicodedata.combining(ch))


def _is_remote_word(text: str) -> bool:
    return any(marker in _fold(text) for marker in REMOTE_MARKERS)


def _is_relevant(item: dict, locations: list | None = None) -> bool:
    """Location/relevance pre-filter (cheap, before any LLM call).

    Turijobs slug searches return national noise. Keep only offers in one
    of her chosen locations (default Madrid) or remote ones.
    """
    places = [_fold(p) for p in (locations or preferences.DEFAULT_LOCATIONS)]
    places = [p for p in places if p and not _is_remote_word(p)]
    url = _fold(item.get("url", ""))
    title = _fold(item.get("title", ""))
    location = _fold(item.get("location", ""))
    if any(p in url or p in location or p in title for p in places):
        return True
    return any(marker in text for marker in REMOTE_MARKERS for text in (url, title))


def _plan(prefs: dict) -> list[tuple[str, str]]:
    """Ordered, deduplicated (lane, query) plan.

    With explicit roles the lane queries are replaced by them (opt-in);
    otherwise the legacy lane queries run. Extra queries always run.
    """
    lanes = prefs.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        lanes = list(LANE_QUERIES)  # defensive: fall back to all lanes
    roles = [r for r in prefs.get("roles") or [] if isinstance(r, str)]
    if roles:
        plan = [("B", role) for role in roles]
    else:
        plan = [
            (lane, query)
            for lane in LANE_QUERIES
            if lane in lanes
            for query in LANE_QUERIES[lane]
        ]
    plan += [("B", q) for q in prefs.get("extra_queries") or [] if isinstance(q, str)]
    seen: set[str] = set()
    unique = []
    for lane, query in plan:
        key = " ".join(query.split()).casefold()
        if key and key not in seen:
            seen.add(key)
            unique.append((lane, query))
    return unique


def search_all_lanes() -> list[dict]:
    """Compatible wrapper: just the new items (see search_with_report)."""
    return search_with_report()[0]


def search_with_report() -> tuple[list[dict], dict]:
    """Search every enabled source; return (new_items, source_report).

    Plan: preferences.roles (else the lane queries) plus extra_queries,
    deduped. Every portal in preferences.sources runs the same plan;
    portals with a location parameter run it once per chosen location.
    Results are merged and deduped across sources by URL against
    seen_jobs.json and pass the location filter. Only URLs filtered out
    as irrelevant are marked seen here; relevant new items are returned
    UNMARKED so a failed detail fetch is retried next run. Items carry
    their query, lane and source; fit_eval refines the A/B guess later.

    source_report maps source -> {queries, ok, empty, blocked, failed}
    so callers can tell a genuine empty search from a failed one. It
    never holds exception text, URLs or credentials.
    """
    seen = load_seen()
    discovered_filtered: set = set()
    new_items: list[dict] = []
    claimed: set = set()  # relevant URLs already returned in this run
    filtered_out = 0
    report = sources.new_report()
    prefs = preferences.get_preferences()
    enabled_sources = prefs.get("sources")
    if (
        not isinstance(enabled_sources, list)
        or not enabled_sources
        or any(not sources.valid_source(name) for name in enabled_sources)
    ):
        enabled_sources = ["turijobs"]  # defensive: fall back to Turijobs
    locations = prefs.get("locations")
    if not isinstance(locations, list) or not locations:
        locations = list(preferences.DEFAULT_LOCATIONS)
    fetch_places = [p for p in locations if isinstance(p, str) and not _is_remote_word(p)]
    plan = _plan(prefs)
    for source_name in enabled_sources:
        fetch = sources.SOURCES[source_name]
        aware = source_name in sources.LOCATION_AWARE
        places = fetch_places if aware and fetch_places else [None]
        requests_made = 0
        for lane, query in plan:
            for place in places:
                if requests_made >= MAX_REQUESTS_PER_SOURCE:
                    break
                requests_made += 1
                result = fetch(query, place) if place else fetch(query)
                status, _detail = sources.result_status(result)
                sources.record(report, source_name, status)
                for item in result:
                    if item["url"] in seen or item["url"] in claimed:
                        continue
                    item["lane"] = lane
                    item["query"] = query
                    item["source"] = source_name
                    if _is_relevant(item, locations):
                        claimed.add(item["url"])
                        new_items.append(item)
                    else:
                        seen.add(item["url"])
                        discovered_filtered.add(item["url"])
                        filtered_out += 1
                time.sleep(2)  # polite: 1 request per query/location/source
    mark_seen(*discovered_filtered)
    if filtered_out:
        print(
            f"[scraper] {filtered_out} offers outside her locations filtered out",
            file=sys.stderr,
        )
    return new_items, report


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def main() -> int:
    if "--dry-run" in sys.argv:
        query = "agente de viajes"
        print(f"[dry-run] searching Turijobs: {query!r}")
        items = search_turijobs(query)
        print(f"[dry-run] parsed {len(items)} results")
        for item in items[:10]:
            print(f"  - {item['title']} | {item['location']} | {item['url']}")
        return 0 if items else 1
    print("usage: python3 scraper.py --dry-run")
    return 2


if __name__ == "__main__":
    sys.exit(main())
