import threading
import time

from procache import SQLiteResponseCache


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
