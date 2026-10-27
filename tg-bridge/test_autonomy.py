"""Regression tests: the bot answers autonomously and never hands off.

The bot must never tell the candidate to talk to a real person (an operator name, "your
son", ...). Whatever she asks, the bot either applies the change itself
or explains it in its own words and records the request internally.

Run: python3 test_autonomy.py   (no network, no LLM, hermetic state dir)
"""



import os
import shutil
import sys
import tempfile
import unicodedata

# Hermetic state dir BEFORE importing the bridge modules: config.load_env()
# only setdefaults, so this override wins for every state_path() call.
_TMP_STATE = tempfile.mkdtemp(prefix="tg-autonomy-test-")
os.environ["TG_BRIDGE_STATE_DIR"] = _TMP_STATE
# Synthetic operator and candidate names, supplied through configuration.
os.environ["OPERATOR_NAMES"] = "Ramiro, Ana Operadora"
os.environ["CANDIDATE_NAME"] = "Marta"

import bot  # type: ignore[reportMissingImports]  # flat sibling module

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"ok   - {name}")
    else:
        print(f"FAIL - {name}" + (f" :: {detail}" if detail else ""))
        FAILURES.append(name)


def deaccent(text: str) -> str:
    lowered = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in lowered if not unicodedata.combining(ch)).lower()


def names_a_person(text: str) -> bool:
    """True when the text points the candidate at a real person."""
    flat = deaccent(text)
    return "ramiro" in flat or "tu hijo" in flat


def test_static_texts() -> None:
    prompt = bot._build_system_prompt()
    check("WELCOME never names a person", not names_a_person(bot.WELCOME),
          bot.WELCOME)
    check("system prompt never names a person", not names_a_person(prompt))
    check("system prompt states the absolute autonomy rule",
          "REGLA ABSOLUTA" in prompt)
    check("system prompt forbids handing off to anyone",
          "deriv" in deaccent(prompt))


def test_split_peticion_portal() -> None:
    """Existing portal queue keeps working exactly as before."""
    text, portal, request = bot._split_peticion(
        'Ahora mismo no busco ahí.\n<<PETICION>>{"portal":"jooble"}'
    )
    check("portal request parsed", portal == "jooble", repr(portal))
    check("generic text empty for portal request", request is None)
    check("peticion line stripped from reply", "PETICION" not in text, repr(text))

    text, portal, request = bot._split_peticion(
        'Conectado.\n<<PETICION>>{"portal":"linkedin"}'
    )
    check("connected portal is not recorded", portal is None, repr(portal))


def test_split_peticion_generic() -> None:
    """Anything she asks for that the bot cannot do is still recorded."""
    text, portal, request = bot._split_peticion(
        'No puedo cambiar la hora.\n<<PETICION>>{"texto":"cambiar la hora de búsqueda"}'
    )
    check("generic request parsed", request == "cambiar la hora de búsqueda",
          repr(request))
    check("generic request has no portal", portal is None, repr(portal))
    check("generic request line stripped from reply", "PETICION" not in text,
          repr(text))


def test_strip_handoff() -> None:
    raw = (
        "No puedo navegar por otras páginas de empleo. "
        "Para eso, lo gestiona Ramiro. "
        "Puedo ajustar el umbral de encaje o las búsquedas extra."
    )
    cleaned = bot._strip_handoff_sentences(raw)
    check("handoff sentence removed", not names_a_person(cleaned), repr(cleaned))
    check("first sentence kept", "No puedo navegar" in cleaned, repr(cleaned))
    check("third sentence kept", "umbral de encaje" in cleaned, repr(cleaned))

    cleaned = bot._strip_handoff_sentences(
        "Eso no lo hago yo.\nHabla con tu hijo para eso.\nPuedo ayudarte con ofertas."
    )
    check("handoff line removed", not names_a_person(cleaned), repr(cleaned))
    check("other lines kept", "Puedo ayudarte" in cleaned, repr(cleaned))

    cleaned = bot._strip_handoff_sentences("Consulta tu oferta pendiente con /ofertas.")
    check("clean reply untouched", cleaned.startswith("Consulta tu oferta"), repr(cleaned))


def test_configured_names() -> None:
    """Operator/candidate names come from configuration, not from code."""
    check("configured operator name is a person", bot._mentions_a_person("Habla con RAMIRO."))
    check("second configured name (accents/case) is a person",
          bot._mentions_a_person("Pregunta a ana operadora"))
    check("generic pattern still matches", bot._mentions_a_person("Habla con tu hijo."))
    check("unconfigured name is not stripped", not bot._mentions_a_person("Habla con Otro."))
    prompt = bot._build_system_prompt()
    check("candidate name from config used in prompt", "asistente de búsqueda de empleo de Marta" in prompt)
    old = os.environ.pop("OPERATOR_NAMES")
    try:
        check("no operator names configured: name not stripped",
              not bot._mentions_a_person("Habla con Ramiro."))
        check("no operator names configured: generic pattern kept",
              bot._mentions_a_person("Consulta con tu hijo."))
    finally:
        os.environ["OPERATOR_NAMES"] = old
    oldc = os.environ.pop("CANDIDATE_NAME")
    try:
        check("neutral default candidate name",
              "asistente de búsqueda de empleo de la candidata" in bot._build_system_prompt())
    finally:
        os.environ["CANDIDATE_NAME"] = oldc


def test_parse_reply_pipeline() -> None:
    """Parse order: action/peticion first, then strip the handoff."""
    parsed = bot._parse_reply(
        'Te lo subo a 85. Habla con Ramiro si quieres más.\n'
        '<<ACCION>>{"accion":"umbral","valor":85}'
    )
    check("reply free of person names", not names_a_person(parsed.text),
          repr(parsed.text))
    check("action survives the strip",
          parsed.action == {"accion": "umbral", "valor": 85}, repr(parsed.action))
    check("action line removed from text", "ACCION" not in parsed.text,
          repr(parsed.text))

    parsed = bot._parse_reply("Habla con Ramiro.")
    check("handoff-only reply still answers something",
          bool(parsed.text.strip()) and not names_a_person(parsed.text),
          repr(parsed.text))


def test_record_request() -> None:
    """Generic requests are persisted once, deduped, and never raise."""
    path = os.path.join(_TMP_STATE, "requests.json")
    bot._record_request("Cambiar la hora de búsqueda")
    bot._record_request("cambiar la hora de BUSQUEDA")  # dedup: case/accents
    bot._record_request("Añadir portal Jooble")

    data = bot._read_json(bot.config.state_path("requests.json"), [])
    check("requests persisted", len(data) == 2, repr(data))
    check("request text preserved",
          bool(data) and data[0]["text"] == "Cambiar la hora de búsqueda",
          repr(data[:1]))
    check("requests carry a timestamp",
          bool(data) and bool(data[0].get("requested_at")), repr(data[:1]))
    check("no stale duplicate file", os.path.isfile(path) or not data)


def main() -> int:
    try:
        test_static_texts()
        test_split_peticion_portal()
        test_split_peticion_generic()
        test_strip_handoff()
        test_configured_names()
        test_parse_reply_pipeline()
        test_record_request()
    finally:
        shutil.rmtree(_TMP_STATE, ignore_errors=True)
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) FAILED")
        return 1
    print("\nAll autonomy checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
