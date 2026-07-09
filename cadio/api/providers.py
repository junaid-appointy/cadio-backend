"""Provider helpers: list available models with a user-supplied API key.

The key is used for the outbound request only — never stored, never logged.
Listing models doubles as the key-validity test (it's free on every provider).
"""

from __future__ import annotations

import httpx

TIMEOUT = 15.0

# obvious non-chat models to keep out of the picker
_OPENAI_EXCLUDE = (
    "embedding", "whisper", "tts", "dall-e", "moderation", "audio",
    "realtime", "transcribe", "image", "davinci", "babbage", "codex",
)


class ProviderError(Exception):
    """User-facing problem: bad key, unknown provider, provider outage."""


def _get(url: str, **kwargs) -> dict:
    try:
        r = httpx.get(url, timeout=TIMEOUT, **kwargs)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in (401, 403):
            raise ProviderError("the API key was rejected by the provider") from exc
        detail = exc.response.text[:300]
        raise ProviderError(f"provider returned HTTP {status}: {detail}") from exc
    except httpx.HTTPError as exc:
        raise ProviderError(f"could not reach the provider: {exc}") from exc


def list_models(provider: str, api_key: str) -> list[str]:
    if not api_key:
        raise ProviderError("no API key given")

    if provider == "anthropic":
        data = _get(
            "https://api.anthropic.com/v1/models?limit=100",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
        models = [m["id"] for m in data.get("data", [])]

    elif provider == "openai":
        data = _get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        models = [
            m["id"] for m in data.get("data", [])
            if not any(x in m["id"] for x in _OPENAI_EXCLUDE)
        ]

    elif provider == "gemini":
        data = _get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": api_key, "pageSize": 1000},
        )
        models = [
            m["name"].removeprefix("models/")
            for m in data.get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]

    elif provider == "xai":
        data = _get(
            "https://api.x.ai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        models = [m["id"] for m in data.get("data", [])]

    else:
        raise ProviderError(f"unknown provider {provider!r}")

    return sorted(set(models))
