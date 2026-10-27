"""User-manageable preferences for the Telegram job-search bridge.

Persisted in state/preferences.json with defaults for missing keys. The
bot's LLM assistant can change these values through conversational
actions; this file is the ONLY preference state the assistant may write.

Schema:
    fit_threshold: int 40-95, or None (None -> config FIT_THRESHOLD)
    lanes: non-empty subset of ["A", "B"]
    extra_queries: list of str, each 2-40 chars, at most 6 entries
    sources: non-empty subset of the SOURCES registry in sources.py
    roles: free-text job titles she chose (2-40 chars, max 6). Empty keeps
        the legacy A/B lane queries; non-empty REPLACES them (opt-in).
    locations: free-text places (2-40 chars, 1-4). Default ["Madrid"].
    conditions: free-text wishes (3-80 chars, max 6: "remoto", "media
        jornada"...). They help the evaluator explain gaps only.

All free text is USER DATA, never instructions: it is stripped, bounded,
deduplicated case-insensitively and rejected if it holds control
characters or the <</>> action markers.
"""

# ruff: noqa: I001  (flat single-block imports: sibling modules + stdlib)
import config  # type: ignore[reportMissingImports]  # flat sibling module
import json
import os
import sources  # type: ignore[reportMissingImports]  # flat sibling module
import sys
from pathlib import Path

PREFS_FILE = "preferences.json"
VALID_LANES = ["A", "B"]
VALID_SOURCES = list(sources.SOURCES)
MIN_THRESHOLD = 40
MAX_THRESHOLD = 95
MAX_EXTRA_QUERIES = 6
MAX_ROLES = 6
MAX_LOCATIONS = 4
MAX_CONDITIONS = 6
DEFAULT_LOCATIONS = ["Madrid"]

_DEFAULTS = {
    "fit_threshold": None,
    "lanes": ["A", "B"],
    "extra_queries": [],
    "sources": ["turijobs"],
    "roles": [],
    "locations": ["Madrid"],
    "conditions": [],
}


def _prefs_path() -> Path:
    return config.state_path(PREFS_FILE)


def get_preferences() -> dict:
    """Return current preferences merged over the defaults."""
    try:
        data = json.loads(_prefs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    merged = {key: list(value) if isinstance(value, list) else value
              for key, value in _DEFAULTS.items()}
    merged.update(data)
    return merged


def effective_fit_threshold() -> int:
    """User threshold when validly set, otherwise the config default."""
    value = get_preferences().get("fit_threshold")
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and MIN_THRESHOLD <= value <= MAX_THRESHOLD
    ):
        return value
    return config.fit_threshold()


def set_preferences(patch: dict) -> dict:
    """Validate a patch against the schema and save atomically.

    Raises ValueError (Spanish message, safe to show the user) when the
    patch is malformed or any resulting value is out of range.
    """
    if not isinstance(patch, dict):
        raise ValueError("el cambio debe ser un objeto")  # noqa: TRY004  (API contract: ValueError for user-facing messages)
    unknown = set(patch) - set(_DEFAULTS)
    if unknown:
        raise ValueError(f"claves desconocidas: {', '.join(sorted(unknown))}")
    current = get_preferences()
    current.update(patch)
    _validate(current)
    _save(current)
    return current


def _validate(prefs: dict) -> None:
    threshold = prefs.get("fit_threshold")
    if threshold is not None:
        if isinstance(threshold, bool) or not isinstance(threshold, int):
            raise ValueError(
                f"el umbral debe ser un número entero entre {MIN_THRESHOLD} "
                f"y {MAX_THRESHOLD}"
            )
        if not (MIN_THRESHOLD <= threshold <= MAX_THRESHOLD):
            raise ValueError(
                f"el umbral debe estar entre {MIN_THRESHOLD} y {MAX_THRESHOLD}"
            )

    lanes = prefs.get("lanes")
    if (
        not isinstance(lanes, list)
        or not lanes
        or any(lane not in VALID_LANES for lane in lanes)
    ):
        raise ValueError(
            "los carriles deben ser una lista no vacía con valores de "
            f"{VALID_LANES}"
        )
    # Dedupe while preserving the user's order.
    prefs["lanes"] = list(dict.fromkeys(lanes))

    queries = prefs.get("extra_queries")
    if not isinstance(queries, list):
        raise ValueError(  # noqa: TRY004  (API contract: ValueError for user-facing messages)
            "las búsquedas extra deben ser una lista de textos"
        )
    for query in queries:
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 40:
            raise ValueError(
                "cada búsqueda extra debe ser un texto de 2 a 40 caracteres"
            )
    if len(queries) > MAX_EXTRA_QUERIES:
        raise ValueError(
            f"como máximo {MAX_EXTRA_QUERIES} búsquedas extra"
        )
    prefs["extra_queries"] = [query.strip() for query in queries]

    prefs["roles"] = _clean_texts(prefs.get("roles"), "roles", 2, 40, MAX_ROLES)
    prefs["locations"] = _clean_texts(
        prefs.get("locations"), "ubicaciones", 2, 40, MAX_LOCATIONS
    )
    if not prefs["locations"]:
        raise ValueError("debe quedar al menos una ubicación de búsqueda")
    prefs["conditions"] = _clean_texts(
        prefs.get("conditions"), "condiciones", 3, 80, MAX_CONDITIONS
    )

    enabled = prefs.get("sources")
    if not isinstance(enabled, list) or not enabled:
        raise ValueError(
            "debe quedar activo al menos un portal de búsqueda"
        )
    portals: list[str] = []
    for portal in enabled:
        if not isinstance(portal, str) or portal.strip().lower() not in VALID_SOURCES:
            names = ", ".join(
                sources.display_name(name) for name in VALID_SOURCES
            )
            raise ValueError(
                f"portal desconocido: {portal!r}; portales disponibles: {names}"
            )
        portals.append(portal.strip().lower())
    # Dedupe while preserving the user's order.
    prefs["sources"] = list(dict.fromkeys(portals))


def _clean_texts(values, label: str, low: int, high: int, limit: int) -> list[str]:
    """Validate a list of free-text entries: stripped, deduped, bounded."""
    if not isinstance(values, list):
        raise ValueError(f"{label} debe ser una lista de textos")  # noqa: TRY004
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"cada elemento de {label} debe ser un texto")  # noqa: TRY004
        text = " ".join(value.split())
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value.replace("\n", " ").replace("\t", " ")):
            raise ValueError(f"{label} contiene caracteres no permitidos")
        if "<<" in text or ">>" in text:
            raise ValueError(f"{label} contiene marcas no permitidas")
        if not low <= len(text) <= high:
            raise ValueError(
                f"cada elemento de {label} debe tener de {low} a {high} caracteres"
            )
        if text.casefold() not in seen:
            seen.add(text.casefold())
            cleaned.append(text)
    if len(cleaned) > limit:
        raise ValueError(f"como máximo {limit} elementos en {label}")
    return cleaned


def _save(prefs: dict) -> None:
    """Atomic write: dump to a tmp file in the same directory, rename."""
    path = _prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(prefs, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


# ------------------------------------------------------------ selftest ----

def _selftest() -> int:
    """Apply every action type against a temp state dir. No network."""
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="tg-prefs-selftest-")
    os.environ["TG_BRIDGE_STATE_DIR"] = tmp
    try:
        path = config.state_path(PREFS_FILE)
        if not str(path).startswith(tmp):
            print(f"[selftest] FAIL: state_path ignored env override: {path}")
            return 1
        print(f"[selftest] state dir override works: {path}")

        prefs = get_preferences()
        if prefs != _DEFAULTS:
            print(f"[selftest] FAIL: defaults wrong when file missing: {prefs}")
            return 1
        print("[selftest] defaults OK when preferences.json is missing")

        # Each conversational action type, exactly as bot.py maps them.
        set_preferences({"fit_threshold": 85})            # umbral
        set_preferences({"lanes": ["B"]})                 # carriles
        set_preferences({"extra_queries": ["limpiadora"]})  # agregar_consulta
        current = get_preferences()["extra_queries"]
        current.remove("limpiadora")                      # quitar_consulta
        set_preferences({"extra_queries": current})
        set_preferences({"sources": ["linkedin"]})        # agregar_fuente
        set_preferences({"roles": ["recepcionista"], "locations": ["Sevilla"],
                         "conditions": ["remoto"]})       # roles/ubicaciones/condiciones
        set_preferences({"roles": [], "locations": ["Madrid"],
                         "conditions": []})               # clear -> defaults

        # Negative cases that materially protect the contract.
        for bad in (
            {"fit_threshold": 120},
            {"fit_threshold": 39},
            {"fit_threshold": "85"},
            {"lanes": []},
            {"lanes": ["A", "C"]},
            {"extra_queries": ["x"]},
            {"extra_queries": ["ok"] * 7},
            {"sources": []},                  # quitar_fuente leaving none
            {"sources": ["infojobs"]},        # portal not in the registry
            {"sources": "turijobs"},          # not a list
            {"sources": ["turijobs", 42]},    # non-string entry
            {"roles": ["x"]},
            {"roles": "recepcionista"},
            {"locations": []},
            {"conditions": ["<<ACCION>>{}"]},
        ):
            try:
                set_preferences(bad)
                print(f"[selftest] FAIL: {bad} was accepted")
                return 1
            except ValueError:
                pass
        print("[selftest] invalid patches rejected OK")

        final = get_preferences()
        expected = {
            "fit_threshold": 85,
            "lanes": ["B"],
            "extra_queries": [],
            "sources": ["linkedin"],
            "roles": [],
            "locations": ["Madrid"],
            "conditions": [],
        }
        if final != expected:
            print(f"[selftest] FAIL: final prefs {final} != {expected}")
            return 1
        print(f"[selftest] resulting preferences: {final}")
        print("[selftest] PASS")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("usage: python3 preferences.py --selftest")
    sys.exit(2)
