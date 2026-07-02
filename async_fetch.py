"""Parallel fetch orchestration with asyncio + httpx."""
from __future__ import annotations

import asyncio
from typing import Any, Callable

import httpx

HEADERS = {"User-Agent": "job-sourcing-pipeline/1.0 (personal job search)"}
TIMEOUT = 20.0
_RETRY_STATUS = frozenset({429, 502, 503, 504})
_RETRY_ATTEMPTS = 4
_RETRY_BACKOFF = 2.0


async def async_get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
) -> Any:
    """GET JSON with exponential backoff on transient errors."""
    hdrs = headers or HEADERS
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            r = await client.get(url, params=params, headers=hdrs)
            if r.status_code in _RETRY_STATUS and attempt < _RETRY_ATTEMPTS - 1:
                await asyncio.sleep(_RETRY_BACKOFF * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            last_exc = e
            if attempt < _RETRY_ATTEMPTS - 1:
                await asyncio.sleep(_RETRY_BACKOFF * (attempt + 1))
            else:
                raise
    if last_exc:
        raise last_exc
    raise RuntimeError("async_get_json failed")


async def run_parallel(
    tasks: list[tuple[str, Callable[..., Any], tuple, dict]],
) -> list[tuple[str, Any]]:
    """Run named fetch tasks concurrently. Sync callables run in a thread pool."""

    async def _one(name: str, fn: Callable[..., Any], args: tuple, kwargs: dict):
        try:
            if asyncio.iscoroutinefunction(fn):
                return name, await fn(*args, **kwargs)
            return name, await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as e:
            return name, e

    results = await asyncio.gather(
        *[_one(name, fn, args, kwargs) for name, fn, args, kwargs in tasks]
    )
    return list(results)


async def fetch_greenhouse_async(client: httpx.AsyncClient, token: str):
    from fetch_jobs import clean

    data = await async_get_json(
        client, f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
    )
    return [
        {
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "updated": j.get("updated_at", ""),
            "description": clean(j.get("content", "")),
        }
        for j in data.get("jobs", [])
    ]


async def fetch_lever_async(client: httpx.AsyncClient, token: str):
    from fetch_jobs import clean

    data = await async_get_json(client, f"https://api.lever.co/v0/postings/{token}?mode=json")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        out.append({
            "title": j.get("text", ""),
            "location": cats.get("location", ""),
            "url": j.get("hostedUrl", ""),
            "updated": str(j.get("createdAt", "")),
            "description": clean(j.get("descriptionPlain") or j.get("description", "") or ""),
        })
    return out


async def fetch_ashby_async(client: httpx.AsyncClient, token: str):
    from fetch_jobs import clean

    data = await async_get_json(
        client,
        f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true",
    )
    return [
        {
            "title": j.get("title", ""),
            "location": j.get("location", ""),
            "url": j.get("jobUrl", ""),
            "updated": j.get("publishedAt", "") or "",
            "description": clean(j.get("descriptionPlain") or j.get("descriptionHtml", "") or ""),
        }
        for j in data.get("jobs", [])
    ]


async def fetch_workable_async(client: httpx.AsyncClient, token: str):
    data = await async_get_json(
        client, f"https://apply.workable.com/api/v1/widget/accounts/{token}",
    )
    out = []
    for j in data.get("jobs", []):
        loc = ", ".join(filter(None, [j.get("city", ""), j.get("country", "")]))
        out.append({
            "title": j.get("title", ""),
            "location": loc,
            "url": j.get("url", "") or j.get("application_url", ""),
            "updated": j.get("published_on", "") or "",
        })
    return out


async def fetch_recruitee_async(client: httpx.AsyncClient, token: str):
    data = await async_get_json(client, f"https://{token}.recruitee.com/api/offers")
    return [
        {
            "title": j.get("title", ""),
            "location": j.get("location", "") or j.get("city", ""),
            "url": j.get("careers_url", "") or j.get("url", ""),
            "updated": j.get("published_at", "") or "",
        }
        for j in data.get("offers", [])
    ]


async def fetch_remoteok_async(client: httpx.AsyncClient):
    data = await async_get_json(client, "https://remoteok.com/api")
    out = []
    for j in data:
        if not isinstance(j, dict) or "legal" in j:
            continue
        out.append({
            "company": j.get("company", ""),
            "title": j.get("position", "") or j.get("title", ""),
            "location": j.get("location", "") or "Remote",
            "url": j.get("url", ""),
            "updated": str(j.get("date", "")),
            "remote": True,
        })
    return out


async def fetch_arbeitnow_async(client: httpx.AsyncClient):
    data = await async_get_json(client, "https://www.arbeitnow.com/api/job-board-api")
    out = []
    for j in data.get("data", []):
        loc = j.get("location", "")
        if not loc and j.get("remote"):
            loc = "Remote"
        out.append({
            "company": j.get("company_name", ""),
            "title": j.get("title", ""),
            "location": loc,
            "url": j.get("url", ""),
            "updated": str(j.get("created_at", "")),
            "remote": bool(j.get("remote")),
        })
    return out


ASYNC_ATS_FETCHERS = {
    "greenhouse": fetch_greenhouse_async,
    "lever": fetch_lever_async,
    "ashby": fetch_ashby_async,
    "workable": fetch_workable_async,
    "recruitee": fetch_recruitee_async,
}

ASYNC_SIMPLE_AGGREGATORS = {
    "remoteok": lambda client, _cfg: fetch_remoteok_async(client),
    "arbeitnow": lambda client, _cfg: fetch_arbeitnow_async(client),
}


def run_parallel_fetch(
    tasks: list[tuple[str, Callable[..., Any], tuple, dict]],
    *,
    max_workers: int = 8,
) -> list[tuple[str, Any]]:
    """Sync entry: run fetch tasks concurrently via asyncio + thread pool."""
    del max_workers  # asyncio.gather runs all tasks; cap via caller batching if needed
    return asyncio.run(run_parallel(tasks))
