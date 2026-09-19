from procache import CachedPyGithubRequester, SQLiteResponseCache
from types import SimpleNamespace

import pytest


class Delegate:
    def __init__(self, token="synthetic-a", base_url="https://api.github.test"):
        self.calls = 0
        self.auth = SimpleNamespace(token=token, token_type="token")
        self.base_url = base_url

    def requestJsonAndCheck(self, *args):
        self.calls += 1
        return {"X-RateLimit-Remaining": "10"}, {"value": self.calls}


def test_pygithub_get_is_cached_but_mutation_is_delegated(tmp_path):
    delegate = Delegate()
    requester = CachedPyGithubRequester(
        delegate,
        SQLiteResponseCache(tmp_path / "cache.sqlite3", namespace="github"),
    )
    assert requester.requestJsonAndCheck("GET", "https://api.github.test/repos/a/b")[1] == {"value": 1}
    assert requester.requestJsonAndCheck("GET", "https://api.github.test/repos/a/b")[1] == {"value": 1}
    requester.requestJsonAndCheck("POST", "https://api.github.test/repos/a/b/issues", input={"x": 1})
    assert delegate.calls == 2



def test_accounts_and_token_rotation_do_not_share_payloads(tmp_path):
    cache = SQLiteResponseCache(tmp_path / "cache.sqlite3", namespace="same-repo")
    first, second = Delegate("synthetic-a"), Delegate("synthetic-b")
    a, b = CachedPyGithubRequester(first, cache), CachedPyGithubRequester(second, cache)
    a.requestJsonAndCheck("GET", "/user")
    b.requestJsonAndCheck("GET", "/user")
    assert (first.calls, second.calls) == (1, 1)
    a.requestJsonAndCheck("GET", "/user")
    assert first.calls == 1
    first.auth.token = "synthetic-rotated"
    a.requestJsonAndCheck("GET", "/user")
    assert first.calls == 2
    assert b"synthetic-" not in (tmp_path / "cache.sqlite3").read_bytes()


@pytest.mark.parametrize("change", [
    {"headers": {"Accept": "application/vnd.github.raw"}},
    {"headers": {"Authorization": "synthetic-override"}},
    {"follow_302_redirect": True},
])
def test_request_representation_partitions_cache(tmp_path, change):
    delegate = Delegate()
    requester = CachedPyGithubRequester(delegate, SQLiteResponseCache(tmp_path / "cache.db"))
    original = requester.requestJsonAndCheck("GET", "/resource")
    assert original[0] == {"X-RateLimit-Remaining": "10"}
    assert requester.requestJsonAndCheck("GET", "/resource") == original
    assert delegate.calls == 1
    requester.requestJsonAndCheck("GET", "/resource", **change)
    assert delegate.calls == 2


def test_unknown_auth_delegate_bypasses_cache(tmp_path):
    delegate = Delegate()
    del delegate.auth
    requester = CachedPyGithubRequester(delegate, SQLiteResponseCache(tmp_path / "cache.db"))
    requester.requestJsonAndCheck("GET", "/resource")
    requester.requestJsonAndCheck("GET", "/resource")
    assert delegate.calls == 2


def test_host_isolation_with_relative_urls(tmp_path):
    cache = SQLiteResponseCache(tmp_path / "cache.db")
    first, second = Delegate(), Delegate(base_url="https://enterprise.github.test/api/v3")
    CachedPyGithubRequester(first, cache).requestJsonAndCheck("GET", "/user")
    CachedPyGithubRequester(second, cache).requestJsonAndCheck("GET", "/user")
    assert (first.calls, second.calls) == (1, 1)


def test_known_anonymous_context_can_share_reads(tmp_path):
    delegate = Delegate()
    delegate.auth = None
    requester = CachedPyGithubRequester(delegate, SQLiteResponseCache(tmp_path / "cache.db"))
    requester.requestJsonAndCheck("GET", "/public")
    requester.requestJsonAndCheck("GET", "/public")
    assert delegate.calls == 1
