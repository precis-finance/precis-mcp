# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""In-process concurrency caps for the read/engine path.

The ``/mcp`` transport is read-only and a single Excel workbook refresh fans out
to one tool call per cell, so a burst can hit ClickHouse all at once. Two
semaphores bound it without any external state:

* a **per-user** semaphore — the working control: no single principal can hold
  more than ``PRECIS_MAX_CONCURRENT_READS_PER_USER`` in-flight queries. This is
  what contains a refresh flush (and a non-conforming client) and keeps one user
  from starving the rest.
* a **global** semaphore — a loose box guard: total in-flight is capped at
  ``PRECIS_MAX_CONCURRENT_READS_GLOBAL`` so a pathological convergence can't
  exceed the ClickHouse pool ceiling (see ``precis_mcp/db.py``).

Both are in-process, so the effective cap is *per uvicorn worker* — at the
target seat count that over-count is bounded and acceptable; this is a backstop,
not a billing boundary. The semaphore, not pool exhaustion, is the binding limit
on a burst as long as the ClickHouse pool maxsize is >= the global cap.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager


class TooBusy(Exception):
    """Raised when a read slot can't be acquired within the bounded wait."""


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


class ReadConcurrencyLimiter:
    """Per-user + global in-flight caps for read-class engine calls."""

    def __init__(self) -> None:
        self._per_user: dict[str, asyncio.Semaphore] = {}
        self._global: asyncio.Semaphore | None = None
        self._lock = asyncio.Lock()

    async def _slots_for(self, user_id: str) -> tuple[asyncio.Semaphore, asyncio.Semaphore]:
        # Lazily size the semaphores from the env on first use; a deployment that
        # retunes the caps restarts the process. Keyed dict grows with distinct
        # users only (bounded by the seat count), so no eviction is needed.
        async with self._lock:
            user_sem = self._per_user.get(user_id)
            if user_sem is None:
                user_sem = asyncio.Semaphore(
                    _int_env("PRECIS_MAX_CONCURRENT_READS_PER_USER", 6)
                )
                self._per_user[user_id] = user_sem
            if self._global is None:
                self._global = asyncio.Semaphore(
                    _int_env("PRECIS_MAX_CONCURRENT_READS_GLOBAL", 32)
                )
            return user_sem, self._global

    @asynccontextmanager
    async def acquire(self, user_id: str):
        """Hold a read slot for the body, or raise ``TooBusy`` if none frees up.

        Acquires the per-user slot first, then the global slot — a single,
        consistent order, so the two semaphores can't deadlock. A bounded wait
        turns a stuck slot into a clean retryable error rather than a hang.
        """
        user_sem, global_sem = await self._slots_for(user_id)
        timeout = _float_env("PRECIS_READ_SLOT_WAIT_SECONDS", 3.0)
        got_user = False
        got_global = False
        try:
            try:
                await asyncio.wait_for(user_sem.acquire(), timeout)
                got_user = True
                await asyncio.wait_for(global_sem.acquire(), timeout)
                got_global = True
            except asyncio.TimeoutError as exc:
                raise TooBusy(
                    "Précis is handling too many queries right now; retry shortly."
                ) from exc
            yield
        finally:
            if got_global:
                global_sem.release()
            if got_user:
                user_sem.release()


# Process-wide limiter for the read/engine path.
read_limiter = ReadConcurrencyLimiter()