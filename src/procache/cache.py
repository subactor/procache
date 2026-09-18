from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class CacheEntry:
    key: str
    value: bytes
    expires_at: float
    etag: str | None = None


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
    ) -> tuple[bytes, bool]:
        """Return ``(value, hit)`` while coalescing same-key local callers."""
        cached = self.get(key)
        if cached is not None:
            return cached.value, True
        with self._lock_for(f"{self.namespace}:{key}"):
            cached = self.get(key)
            if cached is not None:
                return cached.value, True
            value = loader()
            if should_cache is None or should_cache(value):
                self.put(key, value, ttl=ttl, etag=etag)
            return value, False

    def prune(self, *, now: float | None = None) -> int:
        moment = time.time() if now is None else now
        with self._connect() as db:
            result = db.execute(
                "DELETE FROM responses WHERE namespace=? AND expires_at<=?",
                (self.namespace, moment),
            )
        return int(result.rowcount)
