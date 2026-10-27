"""One-shot daily search job.

Searches Turijobs for new postings in both lanes, evaluates fit against
the candidate profile via the LLM, stores results, and pushes qualifying
offers to the registered Telegram chat. Designed to run from a systemd
timer (or manually: python3 search_daily.py).
"""

import re
import sys
import time

import bot  # type: ignore[reportMissingImports]  # flat sibling module (scripts run from tg-bridge/)
import config  # type: ignore[reportMissingImports]  # flat sibling module
import fit_eval  # type: ignore[reportMissingImports]  # flat sibling module
import preferences  # type: ignore[reportMissingImports]  # flat sibling module
import scraper  # type: ignore[reportMissingImports]  # flat sibling module
import state_store  # type: ignore[reportMissingImports]  # flat sibling module
from candidate_profile import (
    compact_digest,  # type: ignore[reportMissingImports]  # flat sibling module
)

# Contact email extraction: only explicit email patterns in the posting
# text are used; the posting is untrusted data, never commands.
MAX_RETRIES_PER_RUN = 5  # re-evaluations of stored failures per run
MAX_DETAIL_ATTEMPTS = 3  # detail-page fetch tries per URL before giving up
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _recover_lane(rec: dict, result: dict) -> str:
    """Search lane for a record whose stored lane is unknown ('?').

    Only when justified: the lane of the plan query that found it (saved
    `query`), else the evaluator's valid A/B. Never invented.
    """
    query = str(rec.get("query", "")).lower()
    matches = {
        lane
        for lane, queries in scraper.LANE_QUERIES.items()
        if query in (q.lower() for q in queries)
    }
    if len(matches) == 1:
        return matches.pop()
    if result.get("eval_lane") in ("A", "B"):
        return result["eval_lane"]
    return rec.get("lane") or "?"


def main() -> int:
    config.load_env()
    digest = compact_digest()
    prefs = preferences.get_preferences()
    threshold = preferences.effective_fit_threshold()
    lanes = prefs.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        lanes = ["A", "B"]  # defensive: fall back to every lane
    extra_queries = {
        q.lower() for q in prefs.get("extra_queries", []) if isinstance(q, str)
    } | {
        r.lower() for r in prefs.get("roles", []) if isinstance(r, str)
    }  # queries/roles she explicitly requested always push, whatever the lane
    new_items, source_report = scraper.search_with_report()
    print(f"[daily] {len(new_items)} new postings found")

    pushed = 0
    new_offers: list = []  # offers found TODAY; the digest lists only these

    chat_id = bot.registered_chat_id() or config.get_int("CHAT_ID", 0) or None
    token = config.get("TG_BOT_TOKEN")

    def deliver(item: dict, retried: bool = False) -> None:
        """Push a card once for an evaluated offer that passes her filters."""
        nonlocal pushed
        fit = fit_eval.fit_value(item)
        if (
            token
            and chat_id
            and fit is not None
            and fit >= threshold
            and (
                item.get("lane", "B") in lanes
                or str(item.get("query", "")).lower() in extra_queries
            )
            and not item.get("card_sent")
            and (retried or not item.get("seen"))
        ):
            try:
                bot.push_offer(token, int(chat_id), item)
            except Exception as exc:  # noqa: BLE001  (delivery must never kill the daily run)
                print(f"[daily] telegram push failed: {exc}", file=sys.stderr)
                return
            pushed += 1
            # Per-record update: a decision taken meanwhile is never undone.
            state_store.update_record(
                item["id"], lambda rec: rec.update(seen=True, card_sent=True)
            )
            item["seen"] = item["card_sent"] = True

    # Bounded retry of stored offers without a genuine rating (explicit
    # errors AND the legacy fit-0 "error de evaluación" shape). The LLM
    # runs with NO lock held; the result is merged into the CURRENT record
    # only, so a discard/apply made meanwhile always wins.
    retried = 0
    for stored in bot.load_offers():
        if retried >= MAX_RETRIES_PER_RUN:
            break
        if not (
            isinstance(stored, dict)
            and isinstance(stored.get("id"), str)
            and fit_eval.will_retry(stored)
        ):
            continue
        retried += 1
        result = fit_eval.evaluate(stored["text"], digest)
        merged = {"ok": False}

        def merge(rec: dict, result: dict = result, merged: dict = merged) -> bool:
            if not fit_eval.will_retry(rec):
                return False  # changed meanwhile: leave it alone
            rec["attempts"] = fit_eval.attempts(rec) + 1
            known = rec.get("lane") in ("A", "B")
            if not known:
                rec["lane"] = _recover_lane(rec, result)
            rec.update(result)  # fit/status/summary/gaps/eval_lane only
            if result["eval_status"] == "ok":
                rec.pop("eval_error", None)
            merged["ok"] = result["eval_status"] == "ok"
            return True

        if state_store.update_record(stored["id"], merge) and merged["ok"]:
            current = state_store.get_offer(stored["id"])
            if current:
                deliver(current, retried=True)
        time.sleep(1)

    detail_pending = detail_gave_up = 0
    existing_ids = {o.get("id") for o in bot.load_offers() if isinstance(o, dict)}
    decided = state_store.decided_ids()
    for item in new_items:
        if item.get("id") in existing_ids or item.get("id") in decided:
            scraper.mark_seen(item["url"])  # already stored/decided: dedup
            continue
        counters = state_store.update_map("detail_attempts.json", lambda d: None)
        if counters.get(item["url"], 0) >= MAX_DETAIL_ATTEMPTS:
            continue  # exhausted earlier: not fetched nor re-reported
        text = scraper.fetch_posting(item["url"])
        if not text:
            # NOT marked seen: the next run finds it again and retries,
            # up to MAX_DETAIL_ATTEMPTS, then it is reported once and dropped.
            def bump(d: dict, url: str = item["url"]) -> None:
                d[url] = int(d.get(url, 0)) + 1

            tries = state_store.update_map("detail_attempts.json", bump)[item["url"]]
            if tries >= MAX_DETAIL_ATTEMPTS:
                detail_gave_up += 1
                print(f"[daily] gave up (no detail text): {item['url']}")
            else:
                detail_pending += 1
                print(f"[daily] skipped (no detail text), will retry: {item['url']}")
            continue
        item["company"] = scraper.parse_company(text)
        item["text"] = text
        contact = EMAIL_RE.search(text)
        item["contact_email"] = contact.group(0) if contact else None

        item.update(fit_eval.evaluate(text, digest))  # never touches item["lane"]
        item["attempts"] = 1
        # Merge ONE record into the current file: no stale-snapshot overwrite.
        if state_store.add_offer(item):
            existing_ids.add(item["id"])
            new_offers.append(item)
            scraper.mark_seen(item["url"])  # stored (even unevaluated): processed
            state_store.update_map(
                "detail_attempts.json", lambda d, u=item["url"]: d.pop(u, None)
            )
            deliver(item)
        time.sleep(1)

    # Keep only pending (unrejected) offers, cap the backlog (current file).
    state_store.trim_offers(60)

    # Daily heartbeat: always report, even on a zero-hit day.
    if token and chat_id:
        try:
            bot.push_digest(token, int(chat_id), len(new_items), pushed, new_offers,
                detail_pending=detail_pending,
                detail_gave_up=detail_gave_up,
                source_report=source_report,
            )
            # Whatever the digest listed has been shown to her: mark it so
            # /ofertas never repeats it either. Failed send -> nothing marked.
            for offer in bot._digest_listed(new_offers):
                offer["seen"] = True
                state_store.update_record(
                    offer["id"], lambda rec: rec.update(seen=True)
                )
        except Exception as exc:  # noqa: BLE001  (heartbeat must never kill the run)
            print(f"[daily] digest push failed: {exc}", file=sys.stderr)

    print(f"[daily] done: {len(new_items)} new, {pushed} pushed to Telegram")
    return 0


if __name__ == "__main__":
    sys.exit(main())
