"""Regression tests: the profile belongs to the candidate and lives in chat.

She dictates her profile in the conversation (<<PERFIL>> line), reads it
with /perfil, and `compact_digest()` uses her copy instead of waiting for
anyone else to prepare the framework file.

Run: python3 test_profile.py   (no network, no LLM, hermetic state dir)
"""


import os
import shutil
import sys
import tempfile
from pathlib import Path

# Hermetic state dir BEFORE importing the bridge modules.
_TMP_STATE = tempfile.mkdtemp(prefix="tg-profile-test-")
os.environ["TG_BRIDGE_STATE_DIR"] = _TMP_STATE
os.environ["OPERATOR_NAMES"] = "Ramiro"
os.environ["CANDIDATE_NAME"] = "Marta"

import bot  # type: ignore[reportMissingImports]  # flat sibling module
import candidate_profile as profile  # type: ignore[reportMissingImports]

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"ok   - {name}")
    else:
        print(f"FAIL - {name}" + (f" :: {detail}" if detail else ""))
        FAILURES.append(name)


def test_digest_prefers_her_profile() -> None:
    Path(_TMP_STATE, "profile.md").write_text(
        "# Mi perfil\n\nCamarera 6 años en hostelería de Madrid.",
        encoding="utf-8",
    )
    digest = profile.compact_digest()
    check("her profile is used", "Camarera 6 años" in digest, repr(digest))
    check("missing-file fallback is gone", "no disponible" not in digest,
          repr(digest))
    Path(_TMP_STATE, "profile.md").unlink()


def test_append_profile() -> None:
    ok = profile.append_profile("Hablo inglés y francés")
    check("note stored", ok is True, repr(profile.her_profile_text()))
    check("note readable back", "inglés y francés" in profile.her_profile_text())

    dup = profile.append_profile("  hablo INGLÉS y francés ")
    check("duplicate note not stored again", dup is False,
          repr(profile.her_profile_text()))
    body = profile.her_profile_text()
    check("only one copy kept", body.lower().count("francés") == 1, repr(body))

    check("empty note rejected", profile.append_profile("   ") is False)
    check("oversized note rejected",
          profile.append_profile("x" * 600) is False)

    # Growth is capped: the oldest notes are dropped, never the file itself.
    for index in range(60):
        profile.append_profile(f"nota {index} " + "y" * 250)
    final = profile.her_profile_text()
    check("profile size is capped", len(final) <= profile.MAX_STATE_CHARS + 200,
          f"len={len(final)}")


def test_parse_perfil_line() -> None:
    parsed = bot._parse_reply(
        'Ahora mismo tu perfil no lo tengo. <<PERFIL>>{"texto":'
        '"camarera 6 años en Madrid e inglés medio"}'
    )
    check("perfil payload parsed",
          parsed.perfil == "camarera 6 años en Madrid e inglés medio",
          repr(parsed.perfil))
    check("perfil line stripped from reply", "PERFIL" not in parsed.text,
          repr(parsed.text))
    check("reply text kept", "tu perfil no lo tengo" in parsed.text,
          repr(parsed.text))


def test_prompt_and_welcome_document_profile() -> None:
    check("WELCOME documents /perfil", "/perfil" in bot.WELCOME, bot.WELCOME)
    prompt = bot._build_system_prompt()
    check("prompt teaches the <<PERFIL>> line", "<<PERFIL>>" in prompt)
    check("prompt tells the bot to ask HER only",
          "SOLO ella" in prompt, prompt)
    check("prompt never names a person",
          "ramiro" not in bot._deaccent(prompt).lower()
          and "tu hijo" not in bot._deaccent(prompt).lower())


def main() -> int:
    try:
        test_digest_prefers_her_profile()
        test_append_profile()
        test_parse_perfil_line()
        test_prompt_and_welcome_document_profile()
    finally:
        shutil.rmtree(_TMP_STATE, ignore_errors=True)
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) FAILED")
        return 1
    print("\nAll profile checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
