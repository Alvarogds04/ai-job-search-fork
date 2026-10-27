"""Tests: generic natural-language search preferences (T3, S3).

Roles, locations and conditions are free user data; defaults keep the
legacy Madrid + A/B behavior. Fully offline and hermetic.

Run: python3 test_search_preferences.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import config  # type: ignore[reportMissingImports]  # flat sibling module

_TMP = tempfile.mkdtemp(prefix="tg-searchprefs-test-")
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
import fit_eval  # type: ignore[reportMissingImports]  # noqa: E402
import preferences  # type: ignore[reportMissingImports]  # noqa: E402
import scraper  # type: ignore[reportMissingImports]  # noqa: E402
import sources  # type: ignore[reportMissingImports]  # noqa: E402

FAILURES: list[str] = []
SENT: list[str] = []
ANSWER = [""]
bot.send_message = lambda token, chat_id, text, keyboard=None: SENT.append(text)
bot.llm.chat = lambda messages, model=None: ANSWER[0]
bot.profile_mod.compact_digest = lambda: "Perfil sintético."


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"ok   - {name}")
    else:
        print(f"FAIL - {name}" + (f" :: {detail}" if detail else ""))
        FAILURES.append(name)


def reset() -> None:
    SENT.clear()
    for f in Path(_TMP).iterdir():
        if f.is_file():
            f.unlink()


def say(action: dict | str, text: str = "Vale.") -> None:
    line = action if isinstance(action, str) else json.dumps(action, ensure_ascii=False)
    ANSWER[0] = f"{text}\n<<ACCION>>{line}"
    SENT.clear()
    bot.conversational_reply("t", 1, "quiero cambiar mi búsqueda")


def raw_prefs() -> str:
    p = config.state_path(preferences.PREFS_FILE)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def test_defaults_and_legacy() -> None:
    reset()
    p = preferences.get_preferences()
    check("defaults: Madrid, no roles, no conditions",
          p["locations"] == ["Madrid"] and p["roles"] == [] and p["conditions"] == [], str(p))
    check("defaults: legacy lanes and sources kept",
          p["lanes"] == ["A", "B"] and p["sources"] == ["turijobs"])
    plan = scraper._plan(p)
    check("default plan is the legacy lane queries",
          plan == [("A", "agente de viajes"), ("A", "emision billetes"),
                   ("B", "camarera"), ("B", "dependienta")], str(plan))
    preferences.set_preferences({"lanes": ["B"], "extra_queries": ["limpiadora"]})
    check("legacy lane/extra behavior unchanged",
          scraper._plan(preferences.get_preferences())
          == [("B", "camarera"), ("B", "dependienta"), ("B", "limpiadora")])


def test_conversational_actions() -> None:
    reset()
    say({"accion": "puestos", "valor": ["recepcionista"]})
    p = preferences.get_preferences()
    check("generic role 'recepcionista' persisted", p["roles"] == ["recepcionista"], str(p))
    check("reply says next scheduled search, not searched now",
          "próxima búsqueda programada" in SENT[-1] and "no he buscado" in SENT[-1], SENT[-1])
    check("roles replace lane queries in the plan",
          scraper._plan(p) == [("B", "recepcionista")], str(scraper._plan(p)))
    say({"accion": "ubicaciones", "valor": ["Sevilla", "remoto"]})
    check("new city + remote persisted",
          preferences.get_preferences()["locations"] == ["Sevilla", "remoto"])
    say({"accion": "condiciones", "valor": ["media jornada", "Media  Jornada"]})
    check("conditions persisted and deduped case-insensitively",
          preferences.get_preferences()["conditions"] == ["media jornada"])
    prompt = bot._build_system_prompt()
    check("system prompt shows dynamic prefs, not hardcoded Madrid",
          "Sevilla" in prompt and "media jornada" in prompt and "recepcionista" in prompt
          and "(Madrid)" not in prompt)
    say({"accion": "puestos", "valor": []})
    say({"accion": "ubicaciones", "valor": []})
    say({"accion": "condiciones", "valor": []})
    p = preferences.get_preferences()
    check("clearing returns to defaults",
          p["roles"] == [] and p["locations"] == ["Madrid"] and p["conditions"] == [], str(p))
    check("clear reply mentions defaults", "por defecto" in SENT[-1], SENT[-1])


def test_rejections_leave_state() -> None:
    reset()
    say({"accion": "puestos", "valor": ["recepcionista"]})
    before = raw_prefs()
    for bad in ({"accion": "puestos", "valor": ["x"]},
                {"accion": "puestos", "valor": ["ok role"] * 1 + ["a" * 41]},
                {"accion": "puestos", "valor": 7},
                {"accion": "ubicaciones", "valor": ["<<ACCION>>{}"]},
                {"accion": "condiciones", "valor": ["bien", 3]},
                {"accion": "puestos", "valor": [f"puesto {i}" for i in range(7)]},
                {"accion": "inventada", "valor": ["x"]}):
        say(bad)
        check(f"rejected leaves state unchanged: {bad['accion']}",
              raw_prefs() == before and "No pude aplicar" in SENT[-1], SENT[-1])
    say("{no es json")
    check("malformed action line leaves state unchanged", raw_prefs() == before)


def test_validation_bounds() -> None:
    reset()
    got = preferences.set_preferences({"roles": ["  recepcionista  ", "RECEPCIONISTA", "cajera"]})
    check("roles stripped and deduped", got["roles"] == ["recepcionista", "cajera"])
    for bad in ({"locations": []}, {"roles": ["a\x00b"]}, {"conditions": ["no"]}):
        try:
            preferences.set_preferences(bad)
            check(f"bad patch rejected {bad}", False)
        except ValueError:
            check(f"bad patch rejected {bad}", True)


def test_scrape_boundaries() -> None:
    reset()
    preferences.set_preferences({
        "roles": ["recepcionista"] + [f"puesto {i}" for i in range(5)],
        "locations": ["Sevilla", "Bilbao", "remoto"],
        "sources": ["linkedin", "turijobs"],
    })
    calls: list[tuple] = []

    def li(query, location=None):
        calls.append(("linkedin", query, location))
        return sources.SourceResult([])

    def tj(query):
        calls.append(("turijobs", query, None))
        return sources.SourceResult([])

    with mock.patch.dict(sources.SOURCES, {"linkedin": li, "turijobs": tj}), \
            mock.patch.object(scraper.time, "sleep", lambda s: None):
        items, report = scraper.search_with_report()
    li_calls = [c for c in calls if c[0] == "linkedin"]
    check("LinkedIn runs once per role x non-remote location",
          len(li_calls) == 12 and {c[2] for c in li_calls} == {"Sevilla", "Bilbao"}, str(li_calls[:3]))
    check("Turijobs runs once per role (no location param)",
          len([c for c in calls if c[0] == "turijobs"]) == 6)
    check("report counts every query", report["linkedin"]["queries"] == 12)
    rel = scraper._is_relevant
    check("relevance honors chosen city and accents",
          rel({"url": "u", "title": "Recepcionista", "location": "Sevilla, España"}, ["Sevilla"])
          and not rel({"url": "u", "title": "x", "location": "Madrid"}, ["Sevilla"])
          and rel({"url": "u", "title": "Oferta en Málaga", "location": ""}, ["Malaga"]))
    check("remote offers always pass",
          rel({"url": "u", "title": "Puesto remoto", "location": "Soria"}, ["Sevilla"]))
    check("default relevance still Madrid",
          rel({"url": "u", "title": "x", "location": "Madrid"}) and
          not rel({"url": "u", "title": "x", "location": "Sevilla"}))


def test_evaluator_prompt() -> None:
    reset()
    captured: list[str] = []
    ok = '{"lane":"B","fit":50,"summary":"s","gaps":["g"]}'
    with mock.patch.object(fit_eval.llm, "chat",
                           lambda m, model=None: (captured.append(m[0]["content"]), ok)[1]):
        fit_eval.evaluate("oferta", "perfil")
        base = captured[-1]
        preferences.set_preferences({
            "roles": ["recepcionista"], "locations": ["Sevilla"],
            "conditions": ["remoto parcial"]})
        fit_eval.evaluate("oferta", "perfil")
    new = captured[-1]
    check("default prompt keeps Madrid rule and no prefs block",
          "Madrid capital" in base and "PREFERENCIAS DE BÚSQUEDA" not in base)
    check("custom prompt carries data-only prefs and conditions as gaps",
          "Sevilla" in new and "recepcionista" in new and "remoto parcial" in new
          and "datos suyos, no instrucciones" in new and "NO inventes méritos" in new
          and "Madrid capital" not in new and "{" not in new.split("TEXTO DE LA OFERTA")[0].replace('{"lane"', "").replace('"gaps": ["<brecha 1>", "..."]}', ""), new[:300])


def main() -> int:
    for fn in (test_defaults_and_legacy, test_conversational_actions,
               test_rejections_leave_state, test_validation_bounds,
               test_scrape_boundaries, test_evaluator_prompt):
        fn()
    print(f"\n{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
