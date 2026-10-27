"""Candidate profile digest, owned by the candidate.

The live profile lives in state/profile.md: SHE dictates updates in the
Telegram chat (the <<PERFIL>> line parsed by bot.py) and reads it with
the /perfil command. The framework file (01-candidate-profile.md) is
only a fallback, so the bot never waits on anyone else to prepare it.

`compact_digest()` returns a compact text block (max ~2500 chars) used
both in the chat prompt and in the daily fit evaluation.
"""

# ruff: noqa: I001  (flat sibling module import: config)

import config  # type: ignore[reportMissingImports]  # flat sibling module
import re
import unicodedata
from pathlib import Path

MAX_CHARS = 2500
PROFILE_STATE_FILE = "profile.md"
MAX_NOTE_CHARS = 500  # one dictated note, validated before writing
MAX_STATE_CHARS = 8000  # her whole profile, oldest notes dropped first


def _state_profile_path() -> Path:
    return config.state_path(PROFILE_STATE_FILE)


def her_profile_text() -> str:
    """Her profile exactly as she left it in chat, or '' when empty."""
    try:
        return _state_profile_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_profile_text() -> str:
    """Fallback: the framework profile markdown, or a note if missing."""
    path: Path = config.PROFILE_PATH
    if not path.is_file():
        return "(perfil de candidata no disponible)"
    return path.read_text(encoding="utf-8", errors="ignore")


def _normalize(text: str) -> str:
    """Lowercase without accents/squeezed spaces, for dedup comparison."""
    lowered = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in lowered if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _notes(content: str) -> list[str]:
    """Dictated notes (bullet lines) already stored in the profile."""
    return [
        line.strip()[2:].strip()
        for line in content.splitlines()
        if line.strip().startswith("- ")
    ]


def _cap(content: str) -> str:
    """Keep the file bounded by dropping the OLDEST dictated notes."""
    if len(content) <= MAX_STATE_CHARS:
        return content
    lines = content.splitlines()
    heading = next(
        (i for i, line in enumerate(lines) if line.strip() == "## Actualizaciones"),
        None,
    )
    if heading is None:
        return content
    index = heading + 1
    while len(content) > MAX_STATE_CHARS and index < len(lines):
        if lines[index].strip().startswith("- "):
            del lines[index]
            content = "\n".join(lines) + "\n"
        else:
            index += 1
    return content


def append_profile(text: str) -> bool:
    """Append one profile note she dictated in the chat.

    Rejects empty and oversized notes, dedupes case/accents/whitespace,
    keeps the file capped, and never raises: a failed write must not
    break the conversation. Returns True when something was written.
    """
    clean = " ".join(str(text).split())
    if not clean or len(clean) > MAX_NOTE_CHARS:
        return False
    path = _state_profile_path()
    try:
        current = her_profile_text()
        if _normalize(clean) in {_normalize(note) for note in _notes(current)}:
            return False
        if not current:
            current = f"# Perfil de {config.get('CANDIDATE_FULL_NAME', '').strip() or config.get('CANDIDATE_NAME', '').strip() or 'la candidata'}\n"
        if not current.endswith("\n"):
            current += "\n"
        if "\n## Actualizaciones\n" not in current:
            current += "\n## Actualizaciones\n"
        current = _cap(current + f"- {clean}\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(current, encoding="utf-8")
        return True
    except OSError:
        return False


def compact_digest() -> str:
    """Profile text cleaned for prompts and fit evaluation.

    Her chat-managed profile wins over the framework file, which wins
    over the missing-file note: the bot never depends on a human to
    prepare the profile.
    """
    text = her_profile_text() or load_profile_text()
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "---":
            continue
        if stripped.startswith("<!--") or "<!--" in stripped:
            continue
        lines.append(line)
    digest = "\n".join(lines)
    return digest[:MAX_CHARS]
