"""Configuration loader for the Telegram job-search bridge.

Loads simple KEY=VALUE pairs from state/.env into os.environ (without
overriding variables that are already set in the real environment) and
exposes typed accessors with sensible defaults.

Secrets never leave this module: never log or print values.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
ENV_FILE = STATE_DIR / ".env"

DEFAULTS = {
    "NAN_BASE_URL": "https://api.nan.builders/v1",
    "NAN_MODEL": "qwen3.8-flash",
    "NAN_EVAL_MODEL": "deepseek-v4-flash",
    "FIT_THRESHOLD": "75",
}

# Framework root that holds the candidate profile (sibling of tg-bridge).
FRAMEWORK_ROOT = BASE_DIR.parent / "ai-job-search"
PROFILE_PATH = (
    FRAMEWORK_ROOT
    / ".claude"
    / "skills"
    / "job-application-assistant"
    / "01-candidate-profile.md"
)
POSTINGS_DIR = FRAMEWORK_ROOT / "documents" / "postings"


def _parse_env_file(path: Path) -> dict:
    """Minimal .env parser: KEY=VALUE lines, '#' comments, optional quotes."""
    values = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def load_env() -> None:
    """Populate os.environ from state/.env.

    The file WINS over pre-existing environment variables: the shell on
    this machine exports a stale NAN_API_KEY (an old revoked key), and
    the bridge must always use the key curated in its own state file.
    """
    os.environ.setdefault("TG_BRIDGE_STATE_DIR", str(STATE_DIR))
    for key, value in _parse_env_file(ENV_FILE).items():
        os.environ[key] = value
    for key, value in DEFAULTS.items():
        os.environ.setdefault(key, value)


def get(key: str, default: str = "") -> str:
    load_env()
    return os.environ.get(key, default)


def get_int(key: str, default: int) -> int:
    raw = get(key, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def fit_threshold() -> int:
    return get_int("FIT_THRESHOLD", 75)


def state_path(name: str) -> Path:
    load_env()
    return Path(os.environ.get("TG_BRIDGE_STATE_DIR", str(STATE_DIR))) / name
