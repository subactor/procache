from procache import CachedPyGithubRequester, SQLiteResponseCache


class Delegate:
    def __init__(self):
        self.calls = 0

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
