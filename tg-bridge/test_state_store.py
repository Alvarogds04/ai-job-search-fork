"""Tests: transactional per-record state updates (T2 integrity).

Synthetic temp state only; network fails closed; no LLM/Telegram.

Run: python3 test_state_store.py
"""

import json
import multiprocessing
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import config  # type: ignore[reportMissingImports]  # flat sibling module

_TMP = tempfile.mkdtemp(prefix="tg-store-test-")
os.environ["TG_BRIDGE_STATE_DIR"] = _TMP
config.ENV_FILE = Path(_TMP) / "no-such-dir" / ".env"
config.PROFILE_PATH = Path(_TMP) / "no-profile.md"
if config.state_path("p.json").parent != Path(_TMP) or config.ENV_FILE.exists():
    sys.exit("state isolation failed: refusing to run")


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


for _t in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
    mock.patch(_t, _no_network).start()

import state_store as st  # type: ignore[reportMissingImports]  # noqa: E402

# fork: the default forkserver would need a unix socket the network guard blocks.
_CTX = multiprocessing.get_context("fork")
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"ok   - {name}")
    else:
        print(f"FAIL - {name}" + (f" :: {detail}" if detail else ""))
        FAILURES.append(name)


def raw(name: str) -> list:
    p = config.state_path(name)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def reset(offers=None) -> None:
    for f in Path(_TMP).iterdir():
        if f.is_file():
            f.unlink()
    if offers is not None:
        st.replace_offers(offers)


def offer(i, **kw):
    return dict({"id": i, "title": i, "url": f"https://e.test/{i}"}, **kw)


def _worker(n: int) -> None:  # runs in a child process
    for k in range(10):
        st.add_offer(offer(f"p{n}-{k}"))
        st.mark_seen(f"https://e.test/p{n}-{k}")


def test_interleaved_decisions() -> None:
    reset([offer("E1", eval_status="error", fit=None), offer("E2"), offer("E3")])
    snapshot = st.snapshot_offers()  # daily's early (soon stale) view
    st.discard_offer("E1")           # bot discards while daily evaluates
    st.apply_offer("E2", {"id": "E2", "title": "E2", "url": "u"})
    ok1 = st.update_record("E1", lambda r: r.update(fit=90))
    ok2 = st.update_record("E2", lambda r: r.update(fit=90))
    ok3 = st.update_record("E3", lambda r: r.update(fit=90))
    ids = [o["id"] for o in raw("offers.json")]
    check("stale snapshot is never written back", snapshot[0]["id"] == "E1" and "E1" not in ids)
    check("discarded record not recreated by update", ok1 is False and "E1" not in ids)
    check("applied record not updated", ok2 is False)
    check("untouched record updated", ok3 is True
          and next(o for o in raw("offers.json") if o["id"] == "E3")["fit"] == 90)
    check("discard leaves URL tombstone", "https://e.test/E1" in raw("seen_jobs.json"))
    check("add_offer refuses decided id",
          st.add_offer(offer("E2")) is False and st.get_offer("E2") is None)


def test_invalid_records_no_mutation() -> None:
    reset([offer("A")])
    before = raw("offers.json")
    bad: tuple = (
        None, "x", {}, {"id": "", "url": "u", "title": "t"},
        {"id": "x", "title": "t"}, {"id": 5, "url": "u", "title": "t"},
        {"id": "x", "url": "u", "title": 7}, offer("A"))
    results = [st.add_offer(x) for x in bad]
    check("invalid/duplicate offers rejected", not any(results), repr(results))
    check("no mutation on rejection", raw("offers.json") == before)
    check("mutate returning False writes nothing",
          st.update_record("A", lambda r: (r.update(fit=1), False)[1]) is False
          and raw("offers.json") == before)
    check("missing record update is a no-op", st.update_record("zz", lambda r: None) is False)


def test_corrupt_state_not_overwritten() -> None:
    reset()
    config.state_path("offers.json").write_text("{not json", encoding="utf-8")
    try:
        st.add_offer(offer("N"))
        raised = False
    except st.StateCorrupt:
        raised = True
    check("corrupt file raises instead of being overwritten",
          raised and config.state_path("offers.json").read_text() == "{not json")
    config.state_path("offers.json").write_text('{"a": 1}', encoding="utf-8")
    try:
        st.snapshot_offers()
        raised = False
    except st.StateCorrupt:
        raised = True
    check("wrong-typed file raises", raised)


def test_interprocess_lock() -> None:
    reset([])
    procs = [_CTX.Process(target=_worker, args=(n,)) for n in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    ids = [o["id"] for o in raw("offers.json")]
    check("concurrent processes lose no record",
          len(ids) == 40 and len(set(ids)) == 40, str(len(ids)))
    check("concurrent seen writes lose no URL", len(raw("seen_jobs.json")) == 40)
    check("all workers exited cleanly", all(p.exitcode == 0 for p in procs))
    check("no stray tmp files",
          not [f for f in Path(_TMP).iterdir() if f.name.endswith(".tmp")])


def test_lock_not_held_by_helpers_afterwards() -> None:
    reset([offer("A")])
    st.update_record("A", lambda r: r.update(x=1))
    p = _CTX.Process(target=_worker, args=(9,))
    p.start()
    p.join(30)
    check("lock released after each helper", p.exitcode == 0)


def main() -> int:
    for fn in (test_interleaved_decisions, test_invalid_records_no_mutation,
               test_corrupt_state_not_overwritten, test_interprocess_lock,
               test_lock_not_held_by_helpers_afterwards):
        fn()
    print(f"\n{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
