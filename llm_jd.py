"""Optional LLM enrichment of sparse job descriptions.

Extracts stack, YOE, salary hints, remote eligibility when regex parsing misses.
Skips gracefully when no API key is configured.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_CACHE_NAME = ".jd_cache.json"
_CACHE_BY_PATH: dict[Path, dict] = {}
_LLM_CALLS_THIS_RUN = 0
_LLM_CONSECUTIVE_FAILURES = 0
_LLM_BLOCKED_REASON = ""


def _cache_path(base: Path | None = None) -> Path:
    root = base or Path(__file__).parent
    return root / _CACHE_NAME


def _load_cache(base: Path | None = None) -> dict:
    path = _cache_path(base)
    if path in _CACHE_BY_PATH:
        return _CACHE_BY_PATH[path]
    if not path.exists():
        cache: dict = {}
        _CACHE_BY_PATH[path] = cache
        return cache
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    _CACHE_BY_PATH[path] = cache
    return cache


def _save_cache(cache: dict, base: Path | None = None) -> None:
    path = _cache_path(base)
    _CACHE_BY_PATH[path] = cache
    path.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def _llm_config() -> tuple[str, str, str] | None:
    """Return (provider, model, api_key) or None when unavailable."""
    if os.environ.get("LLM_JD_DISABLE", "").strip().lower() in ("1", "true", "yes"):
        return None
    provider = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if not provider:
        if os.environ.get("OPENAI_API_KEY", "").strip():
            provider = "openai"
        elif os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("GOOGLE_API_KEY", "").strip():
            provider = "gemini"
        elif os.environ.get("ANTHROPIC_API_KEY", "").strip():
            provider = "anthropic"
        else:
            return None
    if provider == "openai":
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini").strip()
    elif provider == "gemini":
        key = (
            os.environ.get("GEMINI_API_KEY", "").strip()
            or os.environ.get("GOOGLE_API_KEY", "").strip()
        )
        model = os.environ.get("LLM_MODEL", "gemini-2.5-flash-lite").strip()
    elif provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        model = os.environ.get("LLM_MODEL", "claude-3-5-haiku-latest").strip()
    else:
        return None
    if not key:
        return None
    return provider, model, key


def _max_calls_per_run() -> int:
    raw = os.environ.get("LLM_MAX_CALLS_PER_RUN", "8").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 8


def _failure_limit() -> int:
    raw = os.environ.get("LLM_FAILURE_LIMIT", "2").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 2


def _trip_llm_circuit(reason: str) -> None:
    """Disable optional network enrichment for the remainder of this process."""
    global _LLM_BLOCKED_REASON
    if _LLM_BLOCKED_REASON:
        return
    _LLM_BLOCKED_REASON = reason
    print(f"  [llm_jd] {reason}; disabling LLM enrichment for the rest of this run.")


def _reserve_llm_call() -> bool:
    global _LLM_CALLS_THIS_RUN
    limit = _max_calls_per_run()
    if _LLM_CALLS_THIS_RUN >= limit:
        _trip_llm_circuit(f"per-run call budget ({limit}) reached")
        return False
    _LLM_CALLS_THIS_RUN += 1
    return True


def _record_llm_success() -> None:
    global _LLM_CONSECUTIVE_FAILURES
    _LLM_CONSECUTIVE_FAILURES = 0


def _record_llm_failure(error: Exception) -> None:
    global _LLM_CONSECUTIVE_FAILURES
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status == 429:
        _trip_llm_circuit("provider rate limit reached")
        return
    if status in {400, 401, 403, 404}:
        _trip_llm_circuit(f"provider rejected requests with HTTP {status}")
        return
    _LLM_CONSECUTIVE_FAILURES += 1
    if _LLM_CONSECUTIVE_FAILURES >= _failure_limit():
        _trip_llm_circuit(
            f"provider failed {_LLM_CONSECUTIVE_FAILURES} consecutive calls"
        )


def _needs_enrichment(description: str, exp_years: str, salary: str) -> bool:
    if not (description or "").strip():
        return False
    if len(description.strip()) < 80:
        return False
    return not (exp_years or "").strip() or not (salary or "").strip()


def _parse_llm_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def _call_openai(model: str, api_key: str, prompt: str) -> str:
    from llm_http import post_json

    data = post_json(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        body={
            "model": model,
            "messages": [
                {"role": "system", "content": "Extract job facts as JSON only. No markdown."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": 400,
        },
    )
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")


def _call_anthropic(model: str, api_key: str, prompt: str) -> str:
    from llm_http import post_json

    data = post_json(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        body={
            "model": model,
            "max_tokens": 400,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    blocks = data.get("content") or []
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def _call_gemini(model: str, api_key: str, prompt: str) -> str:
    from llm_http import post_json

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    data = post_json(
        url,
        body={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 400},
        },
    )
    parts = (
        (data.get("candidates") or [{}])[0]
        .get("content", {})
        .get("parts") or []
    )
    return "".join(p.get("text", "") for p in parts)


def _build_prompt(title: str, location: str, description: str) -> str:
    desc = description[:4000]
    return (
        "From this job posting, extract JSON with keys:\n"
        '  "exp_years": minimum years required (number or null),\n'
        '  "salary_hint": short salary string if mentioned (or ""),\n'
        '  "remote": "remote" | "onsite" | "hybrid" | "unknown",\n'
        '  "stack": comma-separated tech stack (max 8 items),\n'
        '  "fit_summary": one sentence on fresher/entry-level fit\n\n'
        f"Title: {title}\nLocation: {location}\n\nDescription:\n{desc}"
    )


def llm_enabled() -> bool:
    if _LLM_BLOCKED_REASON:
        return False
    if os.environ.get("LLM_JD_DISABLE", "").strip().lower() in ("1", "true", "yes"):
        return False
    return _llm_config() is not None


def _exp_match_from_years(years: float | None) -> str:
    if years is None:
        return "unknown"
    if years <= 1.0:
        return "good"
    if years <= 2.0:
        return "warn"
    return "bad"


def enrich_job(
    *,
    company: str,
    title: str,
    description: str,
    url: str = "",
    location: str = "",
) -> dict:
    """LLM-parse a JD when description is present. Always returns at least {keep: True}."""
    del company  # reserved for future company-aware prompts
    base = Path(__file__).parent
    empty: dict = {"keep": True}
    if not (description or "").strip():
        return empty
    if len(description.strip()) < 80:
        return empty

    cache = _load_cache(base)
    if url and url in cache:
        return cache[url]
    if not llm_enabled():
        return empty

    from llm_http import redact_secrets

    cfg = _llm_config()
    if not cfg:
        return empty
    if not _reserve_llm_call():
        return empty

    provider, model, api_key = cfg
    prompt = (
        _build_prompt(title, location or "India", description)
        + '\nAlso include "keep": true if suitable for 0-1 yr fresher, else false.'
    )
    try:
        if provider == "openai":
            raw = _call_openai(model, api_key, prompt)
        elif provider == "gemini":
            raw = _call_gemini(model, api_key, prompt)
        else:
            raw = _call_anthropic(model, api_key, prompt)
        parsed = _parse_llm_json(raw)
    except Exception as e:
        _record_llm_failure(e)
        label = url[:60] if url else (title or "")[:40]
        msg = redact_secrets(str(getattr(e, "response", e) or e))
        print(f"  [llm_jd] skip {label}… ({type(e).__name__}: {msg[:120]})")
        return empty

    _record_llm_success()
    out: dict = {"keep": parsed.get("keep", True) is not False}
    if parsed.get("exp_years") is not None:
        try:
            y = float(parsed["exp_years"])
            out["exp_years"] = y
            out["exp_match"] = _exp_match_from_years(y)
        except (TypeError, ValueError):
            pass

    notes_parts = []
    stack = (parsed.get("stack") or "").strip()
    if stack:
        notes_parts.append(f"stack: {stack[:120]}")
        sl = stack.lower()
        if any(k in sl for k in ("machine learning", "llm", " ai")):
            out["resume_variant"] = "ai_platform"
        elif any(k in sl for k in ("backend", "golang", "go ", "api")):
            out["resume_variant"] = "backend"
    remote = (parsed.get("remote") or "").strip()
    if remote and remote != "unknown":
        notes_parts.append(f"work: {remote}")
    summary = (parsed.get("fit_summary") or "").strip()
    if summary:
        notes_parts.append(summary[:160])
    if notes_parts:
        out["note"] = " | ".join(notes_parts)[:200]
    if url:
        cache[url] = out
        _save_cache(cache, base)
    return out


def enrich_from_description(
    url: str,
    title: str,
    location: str,
    description: str,
    *,
    exp_years: str = "",
    salary: str = "",
    base: Path | None = None,
) -> dict:
    """Return enrichment dict: exp_years, salary, notes fragments. Empty if skipped."""
    if not _needs_enrichment(description, exp_years, salary):
        return {}

    cache = _load_cache(base)
    if url and url in cache:
        return cache[url]

    cfg = _llm_config()
    if not cfg:
        return {}
    if _LLM_BLOCKED_REASON or not _reserve_llm_call():
        return {}

    from llm_http import redact_secrets
    provider, model, api_key = cfg
    prompt = _build_prompt(title, location, description)
    try:
        if provider == "openai":
            raw = _call_openai(model, api_key, prompt)
        elif provider == "gemini":
            raw = _call_gemini(model, api_key, prompt)
        else:
            raw = _call_anthropic(model, api_key, prompt)
        parsed = _parse_llm_json(raw)
    except Exception as e:
        _record_llm_failure(e)
        msg = redact_secrets(str(getattr(e, "response", e) or e))
        print(f"  [llm_jd] skip {url[:60]}… ({type(e).__name__}: {msg[:120]})")
        return {}

    _record_llm_success()
    out: dict = {}
    if not exp_years and parsed.get("exp_years") is not None:
        try:
            y = float(parsed["exp_years"])
            out["exp_years"] = str(int(y)) if y == int(y) else str(y)
        except (TypeError, ValueError):
            pass

    if not salary and (parsed.get("salary_hint") or "").strip():
        out["salary"] = str(parsed["salary_hint"]).strip()[:80]

    notes_parts = []
    stack = (parsed.get("stack") or "").strip()
    if stack:
        notes_parts.append(f"stack: {stack[:120]}")
    remote = (parsed.get("remote") or "").strip()
    if remote and remote != "unknown":
        notes_parts.append(f"work: {remote}")
    summary = (parsed.get("fit_summary") or "").strip()
    if summary:
        notes_parts.append(summary[:200])
    if notes_parts:
        out["notes"] = " | ".join(notes_parts)

    if url:
        cache[url] = out
        _save_cache(cache, base)

    return out
