"""HTTP helpers for LLM API calls — requests + certifi SSL + rate limiting."""
from __future__ import annotations

import os
import threading
import time
from typing import Any

import requests

_last_call_mono = 0.0
_throttle_lock = threading.Lock()


def _min_interval_sec() -> float:
    """Seconds between LLM API calls (default ~9 RPM for Gemini free tier)."""
    raw = os.environ.get("LLM_MIN_INTERVAL_SEC", "6.5").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 6.5


def _max_retries() -> int:
    raw = os.environ.get("LLM_MAX_RETRIES", "5").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


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
    timeout: int = 45,
) -> dict:
    """POST JSON with throttle + 429 backoff. Raises requests.HTTPError on failure."""
    last_exc: Exception | None = None
    for attempt in range(_max_retries()):
        _throttle()
        try:
            r = requests.post(
                url,
                headers=headers or {"Content-Type": "application/json"},
                json=body,
                timeout=timeout,
            )
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else min(90.0, 5.0 * (2 ** attempt))
                time.sleep(wait)
                last_exc = requests.HTTPError(
                    f"429 Too Many Requests (retry {attempt + 1}/{_max_retries()})",
                    response=r,
                )
                continue
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                last_exc = e
                continue
            raise
    if last_exc:
        raise last_exc
    raise requests.HTTPError("LLM request failed after retries")
