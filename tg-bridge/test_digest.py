"""Regression tests: the daily digest never repeats yesterday's offers.

The digest used to list the top-3 of the whole pending backlog (60
offers), so the candidate received the same titles every morning. It must only
mention offers found TODAY, and never re-list an offer already delivered
as a card.

Run: python3 test_digest.py   (no network, no LLM)
"""

import inspect
import re
import sys
from pathlib import Path

import bot  # type: ignore[reportMissingImports]  # flat sibling module

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"ok   - {name}")
    else:
        print(f"FAIL - {name}" + (f" :: {detail}" if detail else ""))
        FAILURES.append(name)


def test_wiring() -> None:
    """search_daily must feed the digest today's offers, not the backlog."""
    source = (Path(__file__).resolve().parent / "search_daily.py").read_text(
        encoding="utf-8"
    )
    call_lines = [
        line
        for line in source.splitlines()
        if "push_digest(" in line and "def " not in line
    ]
    check("digest call exists", bool(call_lines))
    if not call_lines:
        return
    call = call_lines[0]
    check("digest receives today's offers only", "new_offers" in call, call)
    check("digest no longer receives the pending backlog",
          not re.search(r",\s*offers\)", call), call)


def test_builder_is_backlog_free() -> None:
    builder = inspect.getsource(bot._digest_text)
    check("digest builder never reads offers.json",
          "load_offers" not in builder, builder)


def test_pushed_offer_not_relisted() -> None:
    # `seen` is the single flag for "already shown to her": search_daily
    # sets it when a card is delivered and when the digest lists it.
    text = bot._digest_text(
        2, 1,
        [{"id": "x", "title": "Agente de viajes", "fit": 93,
          "seen": True}],
    )
    check("offer delivered as a card is not listed again",
          "Agente de viajes" not in text, repr(text))
    check("the push is still reported", "acabo de enviar" in text, repr(text))


def test_fresh_offers_listed_once() -> None:
    text = bot._digest_text(
        4, 0,
        [{"id": "a", "title": "Camarero/a de sala", "fit": 60},
         {"id": "b", "title": "Dependienta/a de Tienda", "fit": 55}],
    )
    check("today's offers are listed", "Camarero/a de sala" in text, repr(text))
    check("no-push day is reported",
          "Ninguna superó el filtro" in text, repr(text))


def test_zero_new_day() -> None:
    text = bot._digest_text(0, 0, [])
    check("zero-new heartbeat keeps working",
          "no hay ofertas nuevas" in text, repr(text))
    check("zero-new heartbeat lists no titles", "⭐" not in text, repr(text))


def main() -> int:
    test_wiring()
    test_builder_is_backlog_free()
    test_pushed_offer_not_relisted()
    test_fresh_offers_listed_once()
    test_zero_new_day()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) FAILED")
        return 1
    print("\nAll digest checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
