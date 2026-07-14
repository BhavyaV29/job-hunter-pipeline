"""HTTP helpers for LLM API calls — requests + certifi SSL + rate limiting."""
from __future__ import annotations

import os
import threading
import time
from typing import Any

import requests

_last_call_mono = 0.0
_throttle_lock = threading.Lock()


class LLMRateLimitError(requests.HTTPError):
    """Raised after the configured 429 attempts are exhausted."""


def _min_interval_sec() -> float:
    """Seconds between LLM API calls (default ~9 RPM for Gemini free tier)."""
    raw = os.environ.get("LLM_MIN_INTERVAL_SEC", "6.5").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 6.5


def _max_retries() -> int:
    raw = os.environ.get("LLM_MAX_RETRIES", "2").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 2


def _max_retry_delay_sec() -> float:
    raw = os.environ.get("LLM_MAX_RETRY_DELAY_SEC", "30").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 30.0


def _retry_delay(response: requests.Response, attempt: int) -> float:
    """Return a bounded Retry-After or exponential delay."""
    retry_after = response.headers.get("Retry-After")
    try:
        requested = float(retry_after) if retry_after else 5.0 * (2 ** attempt)
    except (TypeError, ValueError):
        requested = 5.0 * (2 ** attempt)
    return min(_max_retry_delay_sec(), max(0.0, requested))


def _throttle() -> None:
    global _last_call_mono
    gap = _min_interval_sec()
    if gap <= 0:
        return
    with _throttle_lock:
        elapsed = time.monotonic() - _last_call_mono
        if elapsed < gap:
            time.sleep(gap - elapsed)
        _last_call_mono = time.monotonic()


def redact_secrets(text: str) -> str:
    """Strip API keys from URLs before logging."""
    import re
    return re.sub(r"key=[^&\s\"']+", "key=***", text or "")


def post_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 20,
) -> dict:
    """POST JSON with throttle + 429 backoff. Raises requests.HTTPError on failure."""
    attempts = _max_retries()
    for attempt in range(attempts):
        _throttle()
        r = requests.post(
            url,
            headers=headers or {"Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
        if r.status_code == 429:
            error = LLMRateLimitError(
                f"429 Too Many Requests (attempt {attempt + 1}/{attempts})",
                response=r,
            )
            if attempt == attempts - 1:
                # Never sleep after the final failed attempt.
                raise error
            time.sleep(_retry_delay(r, attempt))
            continue
        r.raise_for_status()
        return r.json()
    raise requests.HTTPError("LLM request failed after retries")
