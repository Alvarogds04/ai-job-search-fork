"""Regression tests: /ofertas only ever shows offers she has never seen.

Every offer shown to her (card push, daily digest, /ofertas listing) is
marked `seen`; the listing never repeats one. When nothing new is left it
says so instead of recycling the backlog.

Run: python3 test_offers.py   (no network, no LLM)
"""

import sys

import bot  # type: ignore[reportMissingImports]  # flat sibling module

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"ok   - {name}")
    else:
        print(f"FAIL - {name}" + (f" :: {detail}" if detail else ""))
        FAILURES.append(name)


def test_listing_only_unseen() -> None:
    offers = [
        {"id": "a", "title": "Vista ayer", "fit": 95, "lane": "A", "seen": True},
        {"id": "b", "title": "Nueva para ti", "fit": 70, "lane": "B"},
    ]
    text, listed = bot._ofertas_listing(offers)
    check("only unseen offers listed",
          "Nueva para ti" in text and "Vista ayer" not in text, repr(text))
    check("listing returns exactly the shown offers",
          [o["id"] for o in listed] == ["b"], repr(listed))
    check("listing keeps the card format", "⭐" in text, repr(text))


def test_never_repeats_when_all_seen() -> None:
    offers = [{"id": "a", "title": "Ya la vio", "fit": 50, "seen": True}]
    text, listed = bot._ofertas_listing(offers)
    check("seen offer is not listed again", "Ya la vio" not in text, repr(text))
    check("nothing is returned to mark", listed == [], repr(listed))
    check("she is told honestly there is nothing new",
          "nuevas" in text and "9" in text, repr(text))


def test_empty_backlog() -> None:
    text, listed = bot._ofertas_listing([])
    check("empty backlog is handled", listed == [] and bool(text), repr(text))


def test_listed_offers_are_the_ones_to_mark() -> None:
    """The handler must be able to persist `seen` on exactly what was sent."""
    offers = [
        {"id": "a", "title": "Uno", "fit": 80},
        {"id": "b", "title": "Dos", "fit": 60},
        {"id": "c", "title": "Tres", "fit": 40, "seen": True},
    ]
    _, listed = bot._ofertas_listing(offers)
    for offer in listed:
        offer["seen"] = True
    check("marking listed offers persists on the shared dicts",
          [o.get("seen") for o in offers] == [True, True, True],
          repr([o.get("seen") for o in offers]))


def test_welcome_documents_commands() -> None:
    check("WELCOME documents /ofertas", "/ofertas" in bot.WELCOME, bot.WELCOME)
    check("WELCOME describes them as new offers",
          "nuevas" in bot.WELCOME, bot.WELCOME)


def main() -> int:
    test_listing_only_unseen()
    test_never_repeats_when_all_seen()
    test_empty_backlog()
    test_listed_offers_are_the_ones_to_mark()
    test_welcome_documents_commands()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) FAILED")
        return 1
    print("\nAll offers-listing checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
