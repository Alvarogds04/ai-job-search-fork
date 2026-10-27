"""Minimal OpenAI-compatible chat completions client for NAN API.

POST {NAN_BASE_URL}/chat/completions with a Bearer token from the
NAN_API_KEY environment variable. Uses only `requests`, 60s timeout and
2 retries with exponential backoff.
"""

# ruff: noqa: I001  (merged import block by convention; see README)
import time

import requests
import config

TIMEOUT_SECONDS = 60
MAX_RETRIES = 2  # extra attempts after the initial request
RETRY_BACKOFF_SECONDS = 2


class LLMError(RuntimeError):
    """Raised when the LLM backend cannot be reached or refuses the call."""


def _endpoint() -> str:
    base = config.get("NAN_BASE_URL").rstrip("/")
    return f"{base}/chat/completions"


def _is_local() -> bool:
    """True when the backend is a local server (e.g. Abito on 127.0.0.1)."""
    base = config.get("NAN_BASE_URL")
    return "127.0.0.1" in base or "localhost" in base


def chat(messages: list, model: str | None = None) -> str:
    """Send a chat completion request and return the assistant text.

    messages: list of {"role": ..., "content": ...} dicts.
    model: optional model name; falls back to NAN_MODEL.
    Raises LLMError with a clear message when NAN_API_KEY is missing or
    the API keeps failing after retries.
    """
    api_key = config.get("NAN_API_KEY")
    if not api_key:
        raise LLMError(
            "NAN_API_KEY no está configurada: define NAN_API_KEY en "
            "tg-bridge/state/.env antes de usar el asistente LLM."
        )
    model = model or config.get("NAN_MODEL")
    payload = {"model": model, "messages": messages}
    if _is_local():
        # Local llama.cpp servers (Abito) burn the whole budget on
        # reasoning and return empty content unless thinking is off.
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            time.sleep(RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))
        try:
            response = requests.post(
                _endpoint(),
                json=payload,
                headers=headers,
                timeout=TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:  # network error
            last_error = exc
            continue
        if response.status_code == 200:
            try:
                data = response.json()
                return data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, ValueError) as exc:
                raise LLMError(f"Unexpected NAN API response shape: {exc}") from exc
        # 4xx (except 429) will not improve by retrying.
        if 400 <= response.status_code < 500 and response.status_code != 429:
            raise LLMError(
                f"NAN API rejected the request (HTTP {response.status_code})."
            )
        last_error = LLMError(f"NAN API HTTP {response.status_code}")

    raise LLMError(f"NAN API failed after retries: {last_error}")
