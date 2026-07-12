"""Thread-safe budgets, query rotation, and response caching for paid search APIs."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Callable


class SearchBudgetExhausted(RuntimeError):
    """Raised when a provider's run/month budget or circuit breaker is closed."""


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _next_month(now: dt.datetime) -> dt.datetime:
    if now.month == 12:
        return now.replace(
            year=now.year + 1,
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    return now.replace(
        month=now.month + 1,
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(value, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)


class SearchBudget:
    """A conservative local quota broker for one paid provider.

    Reservations are persisted before network I/O, so crashes can under-use a
    quota but can never silently over-use it. ``force`` flags intentionally do
    not exist here: callers may bypass source freshness, never a hard API budget.
    """

    def __init__(
        self,
        provider: str,
        *,
        state_path: Path,
        cache_path: Path,
        monthly_budget: int,
        max_calls_per_run: int,
        cache_ttl_hours: float,
        now: Callable[[], dt.datetime] = _utcnow,
    ) -> None:
        self.provider = provider
        self.state_path = state_path
        self.cache_path = cache_path
        self.monthly_budget = max(0, int(monthly_budget))
        self.max_calls_per_run = max(0, int(max_calls_per_run))
        self.cache_ttl_hours = max(0.0, float(cache_ttl_hours))
        self._now = now
        self._lock = threading.Lock()
        self._run_calls = 0
        self._cache_hits = 0
        self._blocked_calls = 0
        self._state = _read_json(state_path)
        self._cache = _read_json(cache_path)

    @property
    def _month(self) -> str:
        return self._now().strftime("%Y-%m")

    def _normalize_month_locked(self) -> dict:
        month = self._month
        if self._state.get("month") != month:
            self._state = {
                "month": month,
                "used": 0,
                "routes": {},
                "rotations": self._state.get("rotations", {}),
                "blocked_until": "",
                "last_error": "",
            }
            _write_json(self.state_path, self._state)
        self._state.setdefault("used", 0)
        self._state.setdefault("routes", {})
        self._state.setdefault("rotations", {})
        return self._state

    def _blocked_reason_locked(self) -> str:
        state = self._normalize_month_locked()
        blocked_until = str(state.get("blocked_until") or "")
        if blocked_until:
            try:
                until = dt.datetime.fromisoformat(blocked_until)
                if until.tzinfo is None:
                    until = until.replace(tzinfo=dt.timezone.utc)
                if self._now() < until:
                    return (
                        f"{self.provider} circuit open until "
                        f"{until.isoformat(timespec='minutes')}"
                    )
            except ValueError:
                pass
        if self.max_calls_per_run == 0:
            return f"{self.provider} disabled (max_calls_per_run=0)"
        if self._run_calls >= self.max_calls_per_run:
            return (
                f"{self.provider} per-run budget exhausted "
                f"({self.max_calls_per_run})"
            )
        used = int(state.get("used") or 0)
        if self.monthly_budget == 0 or used >= self.monthly_budget:
            return (
                f"{self.provider} monthly budget exhausted "
                f"({used}/{self.monthly_budget})"
            )
        return ""

    def reserve(self, route: str) -> None:
        """Reserve and persist one real network search."""
        with self._lock:
            reason = self._blocked_reason_locked()
            if reason:
                self._blocked_calls += 1
                raise SearchBudgetExhausted(reason)
            state = self._state
            state["used"] = int(state.get("used") or 0) + 1
            routes = state.setdefault("routes", {})
            routes[route] = int(routes.get(route) or 0) + 1
            state["last_used_at"] = self._now().isoformat()
            self._run_calls += 1
            _write_json(self.state_path, state)

    def open_circuit(self, message: str, *, until_next_month: bool) -> None:
        """Prevent sibling tasks from repeating a quota/auth failure."""
        with self._lock:
            state = self._normalize_month_locked()
            now = self._now()
            until = (
                _next_month(now)
                if until_next_month
                else now + dt.timedelta(hours=6)
            )
            state["blocked_until"] = until.isoformat()
            state["last_error"] = str(message)[:300]
            _write_json(self.state_path, state)

    @staticmethod
    def cache_key(engine: str, params: dict) -> str:
        safe = {
            str(key): value
            for key, value in params.items()
            if str(key).lower() not in {"api_key", "key", "token"}
        }
        encoded = json.dumps(
            {"engine": engine, "params": safe},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def cached(
        self,
        engine: str,
        params: dict,
        *,
        ttl_hours: float | None = None,
    ) -> Any | None:
        key = self.cache_key(engine, params)
        ttl = self.cache_ttl_hours if ttl_hours is None else max(0.0, ttl_hours)
        with self._lock:
            entry = self._cache.get(key)
            if not isinstance(entry, dict):
                return None
            try:
                saved = dt.datetime.fromisoformat(str(entry["saved_at"]))
                if saved.tzinfo is None:
                    saved = saved.replace(tzinfo=dt.timezone.utc)
            except (KeyError, ValueError, TypeError):
                return None
            age_h = (self._now() - saved).total_seconds() / 3600
            if age_h > ttl:
                self._cache.pop(key, None)
                _write_json(self.cache_path, self._cache)
                return None
            self._cache_hits += 1
            return entry.get("value")

    def store(self, engine: str, params: dict, value: Any) -> None:
        key = self.cache_key(engine, params)
        with self._lock:
            self._cache[key] = {
                "saved_at": self._now().isoformat(),
                "value": value,
            }
            _write_json(self.cache_path, self._cache)

    def rotate(
        self,
        route: str,
        values: list[str],
        batch_size: int,
    ) -> list[str]:
        """Return a persistent round-robin slice of values for this route."""
        if not values:
            return []
        size = min(len(values), max(1, int(batch_size)))
        with self._lock:
            state = self._normalize_month_locked()
            rotations = state.setdefault("rotations", {})
            start = int(rotations.get(route) or 0) % len(values)
            selected = [values[(start + offset) % len(values)] for offset in range(size)]
            rotations[route] = (start + size) % len(values)
            _write_json(self.state_path, state)
            return selected

    def snapshot(self) -> dict[str, int | str]:
        with self._lock:
            state = self._normalize_month_locked()
            return {
                "provider": self.provider,
                "month": str(state.get("month") or ""),
                "month_used": int(state.get("used") or 0),
                "monthly_budget": self.monthly_budget,
                "run_calls": self._run_calls,
                "run_budget": self.max_calls_per_run,
                "cache_hits": self._cache_hits,
                "blocked_calls": self._blocked_calls,
            }
