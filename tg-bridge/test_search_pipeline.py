"""Tests: scoring/search failure integrity (T2, S1/S4).

An LLM failure or malformed answer must never become a genuine score 0;
failed detail fetches are retried next run; applied offers stay excluded;
the search lane is never overwritten by the evaluator. All synthetic.

Run: python3 test_search_pipeline.py   (no network, no LLM, hermetic state)
"""

import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from unittest import mock

import config  # type: ignore[reportMissingImports]  # flat sibling module

_TMP = tempfile.mkdtemp(prefix="tg-pipeline-test-")
os.environ["TG_BRIDGE_STATE_DIR"] = _TMP
config.ENV_FILE = Path(_TMP) / "no-such-dir" / ".env"
config.PROFILE_PATH = Path(_TMP) / "no-profile.md"
config.POSTINGS_DIR = Path(_TMP) / "postings"


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


# Fail closed for ALL network use for the whole run.
for _target in ("socket.socket.connect", "socket.create_connection",
                "socket.getaddrinfo"):
    mock.patch(_target, _no_network).start()
assert socket  # imported so the patch targets exist
if config.state_path("p.json").parent != Path(_TMP) or config.ENV_FILE.exists():
    sys.exit("state isolation failed: refusing to run")

import bot  # type: ignore[reportMissingImports]  # noqa: E402
import fit_eval  # type: ignore[reportMissingImports]  # noqa: E402
import search_daily  # type: ignore[reportMissingImports]  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"ok   - {name}")
    else:
        print(f"FAIL - {name}" + (f" :: {detail}" if detail else ""))
        FAILURES.append(name)


def reset() -> None:
    for f in Path(_TMP).iterdir():
        if f.is_file():
            f.unlink()


def write(name, data) -> None:
    config.state_path(name).write_text(json.dumps(data), encoding="utf-8")


def read(name: str, default: list | None = None) -> list:
    p = config.state_path(name)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return default if default is not None else []


# ------------------------------------------------------------ evaluator ----

def run_eval(raw=None, exc=None):
    def chat(messages, model=None):
        if exc:
            raise exc
        return raw
    fit_eval.llm.chat = chat
    return fit_eval.evaluate("Prácticas comercial EJEMPLOTUR", "perfil")


def test_evaluator() -> None:
    ok = run_eval('{"lane":"A","fit":0,"summary":"nada","gaps":["x"]}')
    check("genuine zero is a valid score",
          ok["eval_status"] == "ok" and ok["fit"] == 0 and fit_eval.fit_value(ok) == 0, repr(ok))
    bad = run_eval('```json\n{"lane":"B","fit":"80","summary":"s"}\n```')
    check("string fit is invalid, not a rating",
          bad["eval_status"] == "error" and bad["fit"] is None, repr(bad))
    err = run_eval(exc=fit_eval.llm.LLMError("down"))
    check("LLM failure is explicit error, not 0",
          err["eval_status"] == "error" and err["fit"] is None, repr(err))
    for label, raw in [
        ("no json", "lo siento"), ("broken json", '{"fit": 5,'),
        ("bool fit", '{"fit": true}'), ("missing fit", '{"lane":"A"}'),
        ("out of range", '{"fit": 150}'), ("negative", '{"fit": -3}'),
        ("null fit", '{"fit": null}'), ("nan", '{"fit": NaN}'),
    ]:
        r = run_eval(raw)
        check(f"malformed ({label}) -> error", r["eval_status"] == "error"
              and fit_eval.fit_value(r) is None, repr(r))
    good = run_eval('{"lane":"b","fit":82,"summary":"ok","gaps":"x"}')
    check("normal score works, lane normalised, gaps tolerant",
          good["fit"] == 82 and good["eval_lane"] == "B" and good["gaps"] == [], repr(good))
    check("legacy stored offers: valid fit kept, legacy failure shape unevaluated",
          fit_eval.fit_value({"fit": 77}) == 77
          and fit_eval.fit_value({"fit": 0}) == 0
          and fit_eval.fit_value(
              {"fit": 0, "summary": "error de evaluación", "lane": "?"}) is None
          and fit_eval.fit_value({"fit": True}) is None
          and fit_eval.fit_value({}) is None)


# ----------------------------------------------------------- rendering -----

def ejemplotur_err(**extra):
    o = {"id": "k1", "title": "Prácticas <b>Comercial</b> (Agencia de Viajes)",
         "company": "EJEMPLOTUR & Co", "location": "Madrid", "lane": "A",
         "url": "https://example.test/ejemplotur?a=1&b=2", "eval_status": "error",
         "fit": None, "summary": "sin evaluar"}
    o.update(extra)
    return o


def test_rendering() -> None:
    o = ejemplotur_err()
    card = bot.offer_card(o)
    check("card says sin evaluar, no 0/100",
          "sin evaluar" in card and "0/100" not in card and "None" not in card, card)
    check("card escapes offer text", "<b>Comercial</b>" not in card
          and "&lt;b&gt;" in card and "&amp;" in card, card)
    text = bot._digest_text(1, 0, [o])
    check("digest shows sin evaluar, never 0", "sin evaluar" in text
          and "⭐ 0" not in text and "&lt;b&gt;" in text, text)
    listing, _ = bot._ofertas_listing([o])
    check("/ofertas shows sin evaluar",
          "sin evaluar" in listing and "⭐ 0" not in listing, listing)
    pages = bot._ofertas_todas([o, {"id": "g", "title": "Genuina", "fit": 0}])
    check("/ofertas todas: unevaluated != genuine 0",
          "sin evaluar · " in pages[0] and "🆕 0 · Genuina" in pages[0], pages[0])
    ranked = bot._digest_listed([o, {"id": "n", "title": "N", "fit": 5},
                                 {"id": "z", "title": "Z", "fit": 0}])
    check("unevaluated ranks after real scores incl. 0",
          [x["id"] for x in ranked] == ["n", "z", "k1"], repr(ranked))
    legacy = bot.offer_card({"id": "l", "title": "Vieja", "url": "https://e.test/l",
                             "fit": 88, "lane": "A", "summary": "bien", "gaps": ["g"]})
    check("legacy offer card keeps format",
          "⭐ Fit: <b>88/100</b> (carril A)" in legacy, legacy)
    ctx = bot.conversation.offers_context([o], "ejemplotur")
    check("conversation context says sin evaluar", "[sin evaluar]" in ctx, ctx)


# ------------------------------------------------------------ pipeline -----

SENT: list = []


def item(n, lane="A", url=None):
    return {"id": f"tj-{n}", "title": f"Oferta {n}", "location": "Madrid",
            "url": url or f"https://example.test/o/{n}", "lane": lane, "query": "q"}


class Env:
    """Patch network/LLM/Telegram around search_daily.main()."""

    def __init__(self, found, detail, results):
        self.found, self.detail, self.results = found, detail, results

    def __enter__(self):
        sc = search_daily.scraper
        self.old = (sc.fetch_posting, fit_eval.evaluate, bot.push_offer,
                    bot.push_digest, search_daily.time.sleep,
                    dict(sc.sources.SOURCES), search_daily.compact_digest,
                    bot.registered_chat_id, sc.LANE_QUERIES)
        sc.sources.SOURCES.clear()
        sc.sources.SOURCES["turijobs"] = (
            lambda query: [dict(i) for i in self.found if i["query"] == query])
        sc.LANE_QUERIES = {"A": ["q"]}
        sc.fetch_posting = lambda url: self.detail.get(url, "")
        fit_eval.evaluate = lambda t, d: self.results.pop(0)
        bot.push_offer = lambda tok, cid, o: SENT.append(("card", o["id"]))
        bot.push_digest = lambda tok, cid, n, p, offs, **kw: SENT.append(
            ("digest", n, p, bot._digest_text(n, p, offs, **kw)))
        bot.registered_chat_id = lambda: 1
        search_daily.time.sleep = lambda s: None  # also patches scraper's time
        search_daily.compact_digest = lambda: "perfil"
        os.environ["TG_BOT_TOKEN"] = "T"
        return self

    def __exit__(self, *a):
        sc = search_daily.scraper
        (sc.fetch_posting, fit_eval.evaluate, bot.push_offer, bot.push_digest,
         search_daily.time.sleep, srcs, search_daily.compact_digest,
         bot.registered_chat_id, sc.LANE_QUERIES) = self.old
        sc.sources.SOURCES.clear()
        sc.sources.SOURCES.update(srcs)
        return False


OK90 = {"eval_status": "ok", "eval_lane": "B", "fit": 90, "summary": "bien", "gaps": []}
ERR = {"eval_status": "error", "eval_error": "llm_error", "eval_lane": "?",
       "fit": None, "summary": "sin evaluar", "gaps": []}
ZERO = {"eval_status": "ok", "eval_lane": "A", "fit": 0, "summary": "no", "gaps": []}


def cards():
    return [s for s in SENT if s[0] == "card"]


def test_pipeline() -> None:
    reset(); SENT.clear()
    write("preferences.json", {"lanes": ["A", "B"], "sources": ["turijobs"]})
    a, k, z = item(1), item(2), item(3)
    details = {a["url"]: "texto a", k["url"]: "texto ejemplotur", z["url"]: "texto z"}
    with Env([a, k, z], details, [dict(OK90), dict(ERR), dict(ZERO)]):
        search_daily.main()
    by = {o["id"]: o for o in read("offers.json")}
    check("evaluator lane does not overwrite search lane",
          by["tj-1"]["lane"] == "A" and by["tj-1"]["eval_lane"] == "B", repr(by["tj-1"]))
    check("LLM failure stored as error, fit null (EJEMPLOTUR case)",
          by["tj-2"]["eval_status"] == "error" and by["tj-2"]["fit"] is None)
    check("genuine zero stored as ok 0",
          by["tj-3"]["eval_status"] == "ok" and by["tj-3"]["fit"] == 0)
    check("only the genuine pass is pushed as card",
          cards() == [("card", "tj-1")], repr(SENT))
    check("all three URLs marked seen after store",
          set(read("seen_jobs.json")) == {a["url"], k["url"], z["url"]})

    SENT.clear()
    with Env([a, k, z], details, [dict(OK90)]):
        search_daily.main()
    offers = read("offers.json")
    check("no duplicate offers on repeat", len(offers) == 3)
    by = {o["id"]: o for o in offers}
    check("retry succeeded: error cleared, lane kept",
          by["tj-2"]["eval_status"] == "ok" and by["tj-2"]["fit"] == 90
          and by["tj-2"]["lane"] == "A" and "eval_error" not in by["tj-2"],
          repr(by["tj-2"]))
    check("retry-succeeded offer pushed once; others not repeated",
          cards() == [("card", "tj-2")], repr(SENT))
    SENT.clear()
    with Env([a, k, z], details, []):
        search_daily.main()
    check("third run: nothing pushed again", not cards(), repr(SENT))


def test_retry_bounds_and_decisions() -> None:
    reset(); SENT.clear()
    write("preferences.json", {"lanes": ["A", "B"], "sources": ["turijobs"]})
    stuck = [dict(ejemplotur_err(id=f"e{i}", url=f"https://e.test/{i}"), text="t", attempts=1)
             for i in range(8)]
    stuck.append(dict(ejemplotur_err(id="done", url="https://e.test/done"),
                      text="t", attempts=1))
    stuck.append(dict(ejemplotur_err(id="maxed", url="https://e.test/m"),
                      text="t", attempts=3))
    write("offers.json", stuck)
    write("applications.json", [{"id": "done", "title": "x", "url": "u"}])
    calls: list = []
    with Env([], {}, []):
        fit_eval.evaluate = lambda t, d: (calls.append(1), dict(ERR))[1]
        search_daily.main()
    check("retries bounded per run",
          len(calls) == search_daily.MAX_RETRIES_PER_RUN, str(len(calls)))
    offers = read("offers.json")
    check("applied offer removed from pending, never re-evaluated",
          "done" not in [o["id"] for o in offers])
    maxed = next(o for o in offers if o["id"] == "maxed")
    check("max-attempt offer not retried", maxed["attempts"] == 3)
    check("retry failure keeps offer as error (no zero)",
          all(o["fit"] is None for o in offers if o["id"].startswith("e")))
    check("attempts count increments",
          max(o["attempts"] for o in offers if o["id"].startswith("e")) == 2)


def test_failed_detail_retry_and_exclusions() -> None:
    reset(); SENT.clear()
    write("preferences.json", {"lanes": ["A"], "sources": ["turijobs"]})
    a, b, c = item(1), item(2), item(3)
    write("seen_jobs.json", [c["url"]])  # discarded earlier: stays excluded
    write("applications.json", [{"id": "tj-2", "title": "x", "url": b["url"]}])
    details: dict = {}
    with Env([a, b, c], details, []):
        search_daily.main()
    check("failed detail: not stored", read("offers.json") == [])
    seen = set(read("seen_jobs.json"))
    check("failed detail URL not marked seen, existing seen kept",
          a["url"] not in seen and c["url"] in seen, repr(seen))
    details[a["url"]] = "ahora si"
    with Env([a, b, c], details, [dict(OK90)]):
        search_daily.main()
    ids = [o["id"] for o in read("offers.json")]
    check("next run retries failed detail; discarded/applied not stored",
          ids == ["tj-1"], repr(ids))
    check("success marks seen", a["url"] in set(read("seen_jobs.json")))
    check("applied URL dedup-marked, not evaluated again",
          b["url"] in set(read("seen_jobs.json")))


def test_existing_offers_survive() -> None:
    reset(); SENT.clear()
    write("preferences.json", {"lanes": ["A"], "sources": ["turijobs"]})
    legacy = [{"id": "old", "title": "Vieja", "fit": 77, "lane": "A",
               "url": "https://e.test/old", "seen": True}]
    write("offers.json", legacy)
    with Env([item(9)], {"https://example.test/o/9": "t"}, [dict(ERR)]):
        search_daily.main()
    offers = read("offers.json")
    check("existing offers kept untouched, new appended",
          offers[0] == legacy[0] and offers[1]["id"] == "tj-9", repr(offers))


def keys(offer_list):
    return {o["id"]: o for o in offer_list}


def test_legacy_error_retry_and_lane() -> None:
    reset(); SENT.clear()
    write("preferences.json", {"lanes": ["A"], "sources": ["turijobs"]})
    base = {"title": "Vieja", "text": "texto", "seen": True,
            "summary": "error de evaluación", "gaps": []}
    stored = [
        dict(base, id="l1", url="https://e.test/1", fit=0, lane="?", query="q"),
        dict(base, id="l2", url="https://e.test/2", fit=0, lane="?", query="zzz"),
        dict(base, id="l3", url="https://e.test/3", fit=0, lane="?", query="zzz"),
        dict(base, id="l4", url="https://e.test/4", fit=0, lane="A", query="zzz"),
        dict(base, id="notext", url="https://e.test/5", fit=0, lane="?", text=""),
    ]
    write("offers.json", stored)
    seq = [
        dict(OK90, eval_lane="B"),   # l1: lane recovered from query q -> A
        dict(OK90, eval_lane="B"),   # l2: unknown query -> eval_lane fallback
        dict(OK90, eval_lane="?"),   # l3: nothing known -> stays "?"
        dict(OK90, eval_lane="B"),   # l4: known A preserved
    ]
    with Env([], {}, seq):
        search_daily.main()
    by = keys(read("offers.json"))
    check("legacy error retried with saved text, never real 0",
          by["l1"]["fit"] == 90 and by["l1"]["eval_status"] == "ok", repr(by["l1"]))
    check("lane recovered from saved search query", by["l1"]["lane"] == "A")
    check("lane falls back to valid eval_lane when query unknown",
          by["l2"]["lane"] == "B", repr(by["l2"]))
    check("lane not invented when unknown", by["l3"]["lane"] == "?")
    check("known search lane preserved", by["l4"]["lane"] == "A"
          and by["l4"]["eval_lane"] == "B")
    check("retried legacy offer pushed (lane A enabled)", ("card", "l1") in SENT
          and ("card", "l4") in SENT and ("card", "l3") not in SENT, repr(SENT))
    check("legacy offer without text is not retried",
          by["notext"]["fit"] == 0 and by["notext"].get("attempts") is None)


def test_every_old_score_representation() -> None:
    reset(); SENT.clear()
    write("preferences.json", {"lanes": ["A"], "sources": ["turijobs"]})
    mk = lambda i, **kw: dict(  # noqa: E731
        {"id": i, "title": i, "url": f"https://e.test/{i}", "text": "t",
         "lane": "A", "query": "q"}, **kw)
    invalid = [mk("s", fit="80"), mk("b", fit=True), mk("m"), mk("hi", fit=150),
               mk("neg", fit=-1), mk("nan", fit=float("nan")), mk("nul", fit=None),
               mk("err", fit=None, eval_status="error")]
    valid = [mk("z", fit=0, summary="no encaja"), mk("g", fit=77),
             mk("okz", fit=0, eval_status="ok")]
    write("offers.json", invalid + valid)
    calls: list = []
    with Env([], {}, []):
        fit_eval.evaluate = lambda t, d: (calls.append(1), dict(ERR))[1]
        search_daily.main()
    check("every invalid representation retried, valid scores and real 0 are not",
          len(calls) == search_daily.MAX_RETRIES_PER_RUN, str(len(calls)))
    check("helper classifies all old shapes",
          all(fit_eval.needs_evaluation(o) for o in invalid)
          and not any(fit_eval.needs_evaluation(o) for o in valid))
    by = keys(read("offers.json"))
    check("valid scores untouched", by["z"]["fit"] == 0 and by["g"]["fit"] == 77
          and by["okz"]["fit"] == 0 and "attempts" not in by["g"])


def test_discard_race_during_retry() -> None:
    reset(); SENT.clear()
    write("preferences.json", {"lanes": ["A"], "sources": ["turijobs"]})
    e = lambda i, **kw: dict(  # noqa: E731
        {"id": i, "title": i, "url": f"https://e.test/{i}", "text": "t", "lane": "A",
         "query": "q", "fit": None, "eval_status": "error", "attempts": 1}, **kw)
    ok = {"id": "keep", "title": "keep", "url": "https://e.test/keep", "fit": 80,
          "lane": "A"}
    write("offers.json", [e("E1"), e("E2"), ok])
    sent_msgs: list = []
    old_send = bot.send_message
    bot.send_message = lambda tok, cid, text, kb=None: sent_msgs.append(text)
    discarded: list = []

    def evaluate(text, digest):
        # While the daily job is "evaluating" (no lock held), she discards
        # E1 and applies E2 from the bot.
        if not discarded:
            discarded.append(1)
            bot.handle_descartar("T", 1, "E1")
            bot.handle_aplicar("T", 1, "E2")
        return dict(OK90)

    try:
        with Env([], {}, []):
            fit_eval.evaluate = evaluate
            search_daily.main()
    finally:
        bot.send_message = old_send
    ids = [o["id"] for o in read("offers.json")]
    check("discarded offer is not resurrected by the daily save",
          "E1" not in ids, repr(ids))
    check("applied offer is not resurrected", "E2" not in ids)
    check("untouched offer kept", "keep" in ids)
    check("discard tombstones the URL", "https://e.test/E1" in set(read("seen_jobs.json")))
    check("application kept", [a["id"] for a in read("applications.json")] == ["E2"])
    check("no card pushed for decided offers",
          ("card", "E1") not in SENT and ("card", "E2") not in SENT, repr(SENT))


def test_stale_snapshot_with_new_item() -> None:
    reset(); SENT.clear()
    write("preferences.json", {"lanes": ["A"], "sources": ["turijobs"]})
    old = {"id": "old", "title": "old", "url": "https://e.test/old", "fit": 70, "lane": "A"}
    write("offers.json", [old])
    n = item(1)
    bot_msgs: list = []
    bot_send = bot.send_message
    bot.send_message = lambda *a, **k: bot_msgs.append(1)

    def evaluate(text, digest):
        bot.handle_descartar("T", 1, "old")  # discard while a new item evaluates
        return dict(OK90)

    try:
        with Env([n], {n["url"]: "texto"}, []):
            fit_eval.evaluate = evaluate
            search_daily.main()
    finally:
        bot.send_message = bot_send
    ids = [o["id"] for o in read("offers.json")]
    check("discard during new-item evaluation survives", ids == ["tj-1"], repr(ids))


def test_detail_failure_bounded() -> None:
    reset(); SENT.clear()
    write("preferences.json", {"lanes": ["A"], "sources": ["turijobs"]})
    a = item(1)
    fetched: list = []
    details: dict = {}

    def run(results=()):
        SENT.clear()
        with Env([a], details, list(results)):
            real = search_daily.scraper.fetch_posting
            search_daily.scraper.fetch_posting = lambda u: (fetched.append(u), real(u))[1]
            search_daily.main()
        return [s for s in SENT if s[0] == "digest"][-1][3]

    t1 = run()
    check("run 1 detail failure: retry promised", "reintentar" in t1 and a["url"] not in set(
        read("seen_jobs.json", [])), t1)
    run()
    t3 = run()
    check("after max attempts the digest says it gave up (no retry promise)",
          "no pude abrir" in t3 and "reintentaré" not in t3, t3)
    check("never marked seen without a stored offer",
          a["url"] not in set(read("seen_jobs.json", [])))
    before = len(fetched)
    t4 = run()
    check("exhausted detail is no longer fetched nor re-reported",
          len(fetched) == before and "no pude abrir" not in t4, t4)
    check("attempts persisted", read("detail_attempts.json" , [])
          or config.state_path("detail_attempts.json").exists())
    # A success clears the counter and stores the offer.
    reset()
    write("preferences.json", {"lanes": ["A"], "sources": ["turijobs"]})
    details2: dict = {}
    with Env([a], details2, []):
        search_daily.main()
    details2[a["url"]] = "ya funciona"
    with Env([a], details2, [dict(OK90)]):
        search_daily.main()
    check("success after a failure stores the offer and marks seen",
          [o["id"] for o in read("offers.json")] == ["tj-1"]
          and a["url"] in set(read("seen_jobs.json")))


def test_honest_texts() -> None:
    pending = ejemplotur_err(text="t", attempts=1)
    spent = ejemplotur_err(text="t", attempts=3)
    notext = ejemplotur_err()
    check("pending card promises a retry", "reintentar" in bot.offer_card(pending))
    check("exhausted card does not promise a retry",
          "reintentar" not in bot.offer_card(spent), bot.offer_card(spent))
    check("no-text card does not promise a retry",
          "reintentar" not in bot.offer_card(notext))
    d = bot._digest_text(2, 0, [spent, notext])
    check("digest of exhausted offers does not say next search",
          "próxima búsqueda" not in d and "sin evaluar" in d, d)
    d = bot._digest_text(1, 0, [pending])
    check("digest of pending offers promises a retry", "reintentaré" in d, d)


def test_digest_gets_source_report() -> None:
    """T3: a run where every portal fails must not claim 'Todo al día'."""
    reset(); SENT.clear()
    write("preferences.json", {"lanes": ["A"], "sources": ["turijobs"]})
    with Env([], {}, []):
        search_daily.scraper.sources.SOURCES["turijobs"] = (
            lambda query: search_daily.scraper.sources.SourceResult(
                [], "blocked", "http_403"))
        search_daily.main()
    digests = [s for s in SENT if s[0] == "digest"]
    check("daily digest with all sources blocked is honest",
          len(digests) == 1 and "Todo al día" not in digests[0][3]
          and "no he podido consultar" in digests[0][3], str(digests))
    reset(); SENT.clear()
    write("preferences.json", {"lanes": ["A"], "sources": ["turijobs"]})
    with Env([], {}, []):
        search_daily.main()
    digests = [s for s in SENT if s[0] == "digest"]
    check("genuine empty search still 'Todo al día'",
          len(digests) == 1 and "Todo al día" in digests[0][3], str(digests))


def main() -> int:
    for fn in (test_digest_gets_source_report, test_evaluator, test_rendering, test_pipeline,
               test_retry_bounds_and_decisions,
               test_failed_detail_retry_and_exclusions, test_existing_offers_survive,
               test_legacy_error_retry_and_lane, test_every_old_score_representation,
               test_discard_race_during_retry, test_stale_snapshot_with_new_item,
               test_detail_failure_bounded, test_honest_texts):
        fn()
    print(f"\n{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
