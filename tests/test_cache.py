import threading
import time

import pytest

from procache import ProviderCooldownError, SQLiteResponseCache


def test_cache_persists_and_expires(tmp_path):
    cache = SQLiteResponseCache(tmp_path / "cache.sqlite3", namespace="github")
    key = cache.key("GET", "/repos/acme/demo")
    assert cache.get(key) is None
    cache.put(key, b"payload", ttl=60)
    assert cache.get(key).value == b"payload"
    assert cache.prune(now=time.time() + 61) == 1
    assert cache.get(key) is None


def test_same_key_load_is_coalesced(tmp_path):
    cache = SQLiteResponseCache(tmp_path / "cache.sqlite3")
    calls = 0
    lock = threading.Lock()

    def loader():
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.02)
        return b"ok"

    results = []
    threads = [threading.Thread(target=lambda: results.append(cache.get_or_set("k", loader, ttl=60))) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls == 1
    assert all(value == b"ok" for value, _ in results)


def test_rate_limit_cooldown_is_shared_and_blocks_loader(tmp_path):
    cache = SQLiteResponseCache(tmp_path / "cache.sqlite3", namespace="github")
    calls = 0

    def loader():
        nonlocal calls
        calls += 1
        error = RuntimeError("HTTP 429 rate limit exceeded")
        error.status_code = 429
        raise error

    with pytest.raises(RuntimeError):
        cache.get_or_set(
            "read",
            loader,
            ttl=60,
            cooldown_key="provider",
            cooldown_seconds=60,
            is_rate_limit=lambda exc: getattr(exc, "status_code", None) == 429,
        )
    with pytest.raises(ProviderCooldownError):
        cache.get_or_set(
            "other-read",
            loader,
            ttl=60,
            cooldown_key="provider",
            is_rate_limit=lambda exc: getattr(exc, "status_code", None) == 429,
        )
    assert calls == 1
