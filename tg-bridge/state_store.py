"""Interprocess-safe, per-record updates of the bridge state files.

The bot (long-running) and search_daily (timer) both write offers.json,
applications.json and seen_jobs.json. Whole-snapshot saves let a stale
daily snapshot resurrect an offer the bot just discarded. Every mutation
here runs in a short critical section: take the lock file, RE-READ the
file, change ONE record, write atomically (tmp + os.replace), release.
Never call the network or the LLM while holding the lock.

Tombstones: a discarded offer is absent from offers.json and its URL is
in seen_jobs.json; an applied one is in applications.json. Per-record
updates never recreate a record that is absent or decided.

A state file that exists but is unreadable/not the expected type raises
StateCorrupt and is NOT overwritten.
"""

import contextlib
import copy
import fcntl
import json
import os
import threading
import time
from pathlib import Path

import config  # type: ignore[reportMissingImports]  # flat sibling module

LOCK_NAME = "state.lock"
LOCK_TIMEOUT = 15.0
_local = threading.local()


class StateCorrupt(RuntimeError):
    """A state file exists but cannot be trusted; refusing to overwrite."""


class StateLockTimeout(RuntimeError):
    """Another process holds the state lock for too long."""


@contextlib.contextmanager
def locked():
    """Exclusive interprocess lock (re-entrant within this thread)."""
    depth = getattr(_local, "depth", 0)
    if depth:
        _local.depth = depth + 1
        try:
            yield
        finally:
            _local.depth -= 1
        return
    path = config.state_path(LOCK_NAME)
    try:
        handle = open(path, "a+")  # noqa: SIM115  (closed in finally)
    except OSError as exc:
        raise StateLockTimeout(f"cannot open lock {path}: {exc}") from exc
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise StateLockTimeout(str(path)) from exc
                time.sleep(0.05)
        _local.depth = 1
        try:
            yield
        finally:
            _local.depth = 0
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def read_json(name: str, default, kind: type = list):
    """Strict read: missing -> default; unreadable or wrong type -> raise."""
    path = config.state_path(name)
    if not path.is_file():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise StateCorrupt(f"{name}: {exc}") from exc
    if not isinstance(data, kind):
        raise StateCorrupt(f"{name}: expected {kind.__name__}")
    return data


def write_json(name: str, data) -> None:
    """Atomic replace so a crash never leaves a half-written file."""
    path = config.state_path(name)
    tmp = Path(f"{path}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def snapshot_offers() -> list:
    """Read-only copy of the offers (lock held only for the read)."""
    with locked():
        return copy.deepcopy(read_json("offers.json", []))


def replace_offers(offers: list) -> None:
    """Whole-list write (seeding/tests); prefer the per-record helpers."""
    with locked():
        write_json("offers.json", offers)


def decided_ids() -> set:
    with locked():
        apps = read_json("applications.json", [])
    return {a.get("id") for a in apps if isinstance(a, dict)}


def valid_offer(offer) -> bool:
    return (
        isinstance(offer, dict)
        and isinstance(offer.get("id"), str)
        and bool(offer["id"])
        and isinstance(offer.get("url"), str)
        and bool(offer["url"])
        and isinstance(offer.get("title"), str)
    )


def add_offer(offer: object) -> bool:
    """Append a new offer unless invalid, duplicate or already decided."""
    if not isinstance(offer, dict) or not valid_offer(offer):
        return False
    with locked():
        offers = read_json("offers.json", [])
        if offer["id"] in decided_ids():
            return False
        if any(isinstance(o, dict) and o.get("id") == offer["id"] for o in offers):
            return False
        offers.append(copy.deepcopy(offer))
        write_json("offers.json", offers)
    return True


def update_record(offer_id: str, mutate) -> bool:
    """Apply mutate(record) to ONE stored offer, atomically.

    Returns False (nothing written) when the offer is absent (discarded),
    applied, or when mutate returns False. mutate works on a copy that is
    stored only on success, so a failing mutate leaves no partial change.
    """
    with locked():
        offers = read_json("offers.json", [])
        index = next(
            (
                i
                for i, o in enumerate(offers)
                if isinstance(o, dict) and o.get("id") == offer_id
            ),
            None,
        )
        if index is None:
            return False
        current = offers[index]
        if offer_id in decided_ids():
            return False
        candidate = copy.deepcopy(current)
        if mutate(candidate) is False:
            return False
        offers[index] = candidate
        write_json("offers.json", offers)
    return True


def get_offer(offer_id: str) -> dict | None:
    """Copy of a stored offer that is still pending (not discarded/applied)."""
    with locked():
        if offer_id in decided_ids():
            return None
        for o in read_json("offers.json", []):
            if isinstance(o, dict) and o.get("id") == offer_id:
                return copy.deepcopy(o)
    return None


def discard_offer(offer_id: str) -> None:
    """Remove one offer and tombstone its URL in seen_jobs."""
    with locked():
        offers = read_json("offers.json", [])
        gone = [o for o in offers if isinstance(o, dict) and o.get("id") == offer_id]
        write_json(
            "offers.json",
            [o for o in offers if not (isinstance(o, dict) and o.get("id") == offer_id)],
        )
        mark_seen(*(o.get("url") for o in gone))


def apply_offer(offer_id: str, record: dict) -> None:
    """Record an application once (idempotent)."""
    with locked():
        apps = read_json("applications.json", [])
        if offer_id not in [a.get("id") for a in apps if isinstance(a, dict)]:
            apps.append(record)
            write_json("applications.json", apps)


def mark_seen(*urls) -> None:
    urls = [u for u in urls if isinstance(u, str) and u]
    if not urls:
        return
    with locked():
        seen = set(read_json("seen_jobs.json", []))
        if not seen.issuperset(urls):
            write_json("seen_jobs.json", sorted(seen | set(urls)))


def load_seen() -> set:
    with locked():
        return set(read_json("seen_jobs.json", []))


def trim_offers(keep: int) -> None:
    """Drop decided offers and cap the backlog, on the CURRENT file."""
    with locked():
        decided = decided_ids()
        offers = [
            o
            for o in read_json("offers.json", [])
            if isinstance(o, dict) and o.get("id") not in decided
        ]
        write_json("offers.json", offers[-keep:])


def update_map(name: str, mutate) -> dict:  # type: ignore[type-arg]
    """Read-modify-write a JSON object file (e.g. detail retry counters)."""
    with locked():
        data: dict = read_json(name, {}, dict)  # type: ignore[assignment]
        mutate(data)
        write_json(name, data)
        return copy.deepcopy(data)
