"""Tiny async TTL cache with per-key locks so concurrent callers share one fetch."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

_store: dict[str, tuple[float, Any]] = {}
_locks: dict[str, asyncio.Lock] = {}
MAX_KEYS = 2000


async def cached(key: str, ttl: float, fn: Callable[[], Awaitable[Any]]) -> Any:
    hit = _store.get(key)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _store.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        value = await fn()
        if len(_store) >= MAX_KEYS:
            # drop the oldest-expiring quarter
            for k, _ in sorted(_store.items(), key=lambda kv: kv[1][0])[: MAX_KEYS // 4]:
                _store.pop(k, None)
                _locks.pop(k, None)
        _store[key] = (time.monotonic() + ttl, value)
        return value


def clear() -> None:
    _store.clear()
    _locks.clear()
