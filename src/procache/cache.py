from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class CacheEntry:
    key: str
    value: bytes
    expires_at: float
    etag: str | None = None


class ProviderCooldownError(RuntimeError):
    """Raised while a provider rate-limit cooldown is active."""

    def __init__(self, provider: str, remaining: float) -> None:
        self.provider = provider
        self.remaining = max(0.0, remaining)
        super().__init__(f"provider {provider!r} is cooling down for {self.remaining:.1f}s")


def is_rate_limit_error(error: Exception) -> bool:
    """Recognize common 429/secondary-limit provider errors without dependencies."""
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(error, "status", None)
    if status is None:
        status = getattr(error, "code", None)
    if status == 429 or "429" in str(error):
        return True
    return status == 403 and any(
        marker in str(error).lower() for marker in ("rate limit", "rate-limit", "retry-after", "secondary limit")
    )


class SQLiteResponseCache:
    """A bounded persistent cache for provider reads.

    The cache is deliberately blind to provider payloads. Callers choose the
    stable key and TTL, while mutations never enter this class. SQLite WAL and
    a per-key process lock make concurrent agent processes converge on one
    value instead of issuing a request storm.
    """

    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: str | Path, *, namespace: str = "default") -> None:
        self.path = Path(path)
        self.namespace = namespace
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS responses (
                    namespace TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    value BLOB NOT NULL,
                    expires_at REAL NOT NULL,
                    etag TEXT,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (namespace, cache_key)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS responses_expiry ON responses(expires_at)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS flights (
                    namespace TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (namespace, cache_key)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS cooldowns (
                    namespace TEXT NOT NULL,
                    cooldown_key TEXT NOT NULL,
                    until_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (namespace, cooldown_key)
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level="IMMEDIATE")
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @classmethod
    def _lock_for(cls, key: str) -> threading.Lock:
        with cls._locks_guard:
            return cls._locks.setdefault(key, threading.Lock())

    @staticmethod
    def key(*parts: object) -> str:
        encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode()).hexdigest()

    def get(self, key: str, *, now: float | None = None) -> CacheEntry | None:
        moment = time.time() if now is None else now
        with self._connect() as db:
            row = db.execute(
                "SELECT cache_key, value, expires_at, etag FROM responses "
                "WHERE namespace=? AND cache_key=? AND expires_at>?",
                (self.namespace, key, moment),
            ).fetchone()
        if row is None:
            return None
        return CacheEntry(key=str(row[0]), value=bytes(row[1]), expires_at=float(row[2]), etag=row[3])

    def put(self, key: str, value: bytes, *, ttl: float, etag: str | None = None) -> CacheEntry:
        if ttl <= 0:
            raise ValueError("cache TTL must be positive")
        expires_at = time.time() + ttl
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO responses(namespace,cache_key,value,expires_at,etag,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (self.namespace, key, sqlite3.Binary(value), expires_at, etag, time.time()),
            )
        return CacheEntry(key=key, value=value, expires_at=expires_at, etag=etag)

    def get_or_set(
        self,
        key: str,
        loader: Callable[[], bytes],
        *,
        ttl: float,
        etag: str | None = None,
        should_cache: Callable[[bytes], bool] | None = None,
        cooldown_key: str | None = None,
        cooldown_seconds: float = 60.0,
        is_rate_limit: Callable[[Exception], bool] | None = None,
    ) -> tuple[bytes, bool]:
        """Return ``(value, hit)`` while coalescing callers across processes."""
        if cooldown_key is not None:
            self.raise_if_cooling_down(cooldown_key)
        cached = self.get(key)
        if cached is not None:
            return cached.value, True
        with self._lock_for(f"{self.namespace}:{key}"):
            owner = uuid.uuid4().hex
            while True:
                if cooldown_key is not None:
                    self.raise_if_cooling_down(cooldown_key)
                cached = self.get(key)
                if cached is not None:
                    return cached.value, True
                if self._claim_flight(key, owner):
                    break
                time.sleep(0.05)
            try:
                # A previous flight may have completed between the cache check
                # and the claim if it was released just before our transaction.
                cached = self.get(key)
                if cached is not None:
                    return cached.value, True
                try:
                    value = loader()
                except Exception as exc:
                    if cooldown_key is not None and is_rate_limit is not None and is_rate_limit(exc):
                        self.cooldown(cooldown_key, ttl=cooldown_seconds)
                    raise
                if should_cache is None or should_cache(value):
                    self.put(key, value, ttl=ttl, etag=etag)
                return value, False
            finally:
                self._release_flight(key, owner)

    def _claim_flight(self, key: str, owner: str, *, ttl: float = 120.0) -> bool:
        now = time.time()
        with self._connect() as db:
            db.execute(
                "DELETE FROM flights WHERE namespace=? AND cache_key=? AND expires_at<=?",
                (self.namespace, key, now),
            )
            result = db.execute(
                "INSERT OR IGNORE INTO flights(namespace,cache_key,owner,expires_at) VALUES(?,?,?,?)",
                (self.namespace, key, owner, now + ttl),
            )
        return result.rowcount == 1

    def _release_flight(self, key: str, owner: str) -> None:
        with self._connect() as db:
            db.execute(
                "DELETE FROM flights WHERE namespace=? AND cache_key=? AND owner=?",
                (self.namespace, key, owner),
            )

    def cooldown(self, key: str, *, ttl: float) -> float:
        if ttl <= 0:
            raise ValueError("cooldown TTL must be positive")
        until = time.time() + ttl
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO cooldowns(namespace,cooldown_key,until_at,updated_at) "
                "VALUES(?,?,?,?)",
                (self.namespace, key, until, time.time()),
            )
        return until

    def cooldown_remaining(self, key: str, *, now: float | None = None) -> float:
        moment = time.time() if now is None else now
        with self._connect() as db:
            row = db.execute(
                "SELECT until_at FROM cooldowns WHERE namespace=? AND cooldown_key=?",
                (self.namespace, key),
            ).fetchone()
        if row is None:
            return 0.0
        remaining = float(row[0]) - moment
        if remaining <= 0:
            with self._connect() as db:
                db.execute(
                    "DELETE FROM cooldowns WHERE namespace=? AND cooldown_key=?",
                    (self.namespace, key),
                )
            return 0.0
        return remaining

    def raise_if_cooling_down(self, key: str) -> None:
        remaining = self.cooldown_remaining(key)
        if remaining > 0:
            raise ProviderCooldownError(self.namespace, remaining)

    def prune(self, *, now: float | None = None) -> int:
        moment = time.time() if now is None else now
        with self._connect() as db:
            result = db.execute(
                "DELETE FROM responses WHERE namespace=? AND expires_at<=?",
                (self.namespace, moment),
            )
        return int(result.rowcount)

    def clear(self) -> None:
        """Drop cached reads and cooldowns for this provider namespace."""
        with self._connect() as db:
            db.execute("DELETE FROM responses WHERE namespace=?", (self.namespace,))
            db.execute("DELETE FROM cooldowns WHERE namespace=?", (self.namespace,))
            db.execute("DELETE FROM flights WHERE namespace=?", (self.namespace,))
