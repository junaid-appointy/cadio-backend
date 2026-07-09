"""Abuse protection for the beta: per-user rate limits, a concurrent-build gate,
a daily run quota, and a request-body size cap.

Deliberately hand-rolled, not slowapi: the server is single-process (workers=1,
because of the shared SQLite connection + in-process worker pool), and we limit
per authenticated USER, not per IP. That's ~60 lines of token buckets vs a
dependency. All state is in-process and dies with the process — fine at this
scale; when the app goes multi-process this moves to Redis (same seams).
"""

from __future__ import annotations

import os
import threading
import time

from fastapi import Depends, HTTPException, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import get_current_user

DAILY_RUN_QUOTA = int(os.environ.get("FORMA_DAILY_RUN_QUOTA", "300"))
MAX_BODY_BYTES = 50 * 1024 * 1024  # nginx enforces this too; belt-and-suspenders


class _TokenBucket:
    """Classic token bucket: `capacity` tokens, refilling at `capacity/period`
    per second, one taken per request. Bursty-friendly, cheap, thread-safe."""

    __slots__ = ("capacity", "refill_per_s", "tokens", "updated", "lock")

    def __init__(self, capacity: int, period_s: float):
        self.capacity = float(capacity)
        self.refill_per_s = capacity / period_s
        self.tokens = float(capacity)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def take(self) -> bool:
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill_per_s)
            self.updated = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False


_buckets: dict[tuple[str, str], _TokenBucket] = {}
_buckets_lock = threading.Lock()


def _bucket(key: tuple[str, str], capacity: int, period_s: float) -> _TokenBucket:
    with _buckets_lock:
        b = _buckets.get(key)
        if b is None:
            b = _TokenBucket(capacity, period_s)
            _buckets[key] = b
        return b


def new_bucket(capacity: int, per_seconds: float) -> _TokenBucket:
    """A standalone bucket (e.g. per websocket connection). Caller holds it."""
    return _TokenBucket(capacity, per_seconds)


def rate_limit(name: str, capacity: int, per_seconds: float):
    """FastAPI dependency factory: allow `capacity` requests per `per_seconds`
    for each user on the endpoint `name`. 429 when the bucket is empty."""
    def dep(user: dict = Depends(get_current_user)) -> dict:
        if not _bucket((user["id"], name), capacity, per_seconds).take():
            raise HTTPException(status_code=429, detail="rate limit — slow down a moment")
        return user
    return dep


# ---- concurrent-build gate (one live agent turn / execute per user) --------
# The global worker pool is only 2 workers; without this one user's rapid-fire
# builds could monopolize both and starve everyone else.
_active_builds: dict[str, int] = {}
_active_lock = threading.Lock()
MAX_CONCURRENT_BUILDS = 1


def acquire_build_slot(uid: str) -> bool:
    with _active_lock:
        if _active_builds.get(uid, 0) >= MAX_CONCURRENT_BUILDS:
            return False
        _active_builds[uid] = _active_builds.get(uid, 0) + 1
        return True


def release_build_slot(uid: str) -> None:
    with _active_lock:
        n = _active_builds.get(uid, 0) - 1
        if n > 0:
            _active_builds[uid] = n
        else:
            _active_builds.pop(uid, None)


def check_daily_quota(store, uid: str) -> bool:
    """True if the user is still under the daily run quota."""
    return store.count_runs_today(uid) < DAILY_RUN_QUOTA


class BodyLimitMiddleware:
    """Reject oversized request bodies early (uvicorn has no max-body flag).
    Streams are cut off past the limit so a huge upload can't exhaust memory."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # fast path: honor a declared Content-Length
        for k, v in scope.get("headers", []):
            if k == b"content-length":
                try:
                    if int(v) > self.max_bytes:
                        return await self._reject(send)
                except ValueError:
                    pass
                break

        seen = 0

        async def guarded_receive() -> Message:
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b"") or b"")
                if seen > self.max_bytes:
                    raise _BodyTooLarge()
            return msg

        try:
            await self.app(scope, guarded_receive, send)
        except _BodyTooLarge:
            await self._reject(send)

    async def _reject(self, send: Send) -> None:
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"error":"request body too large"}'})


class _BodyTooLarge(Exception):
    pass
