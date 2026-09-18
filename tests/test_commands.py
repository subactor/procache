from procache import CachedReadCommand, SQLiteResponseCache


def test_only_read_gh_commands_are_cacheable(tmp_path):
    runner = CachedReadCommand(SQLiteResponseCache(tmp_path / "cache.sqlite3"))
    assert runner.is_read(["gh", "pr", "view", "1"])
    assert runner.is_read(["gh", "api", "repos/acme/demo"])
    assert runner.is_read(["gh", "search", "prs", "--json", "number"])
    assert runner.is_read(["gh", "api", "graphql", "-f", "query=query { viewer { login } }"])
    assert runner.is_read(["gh", "api", "graphql", "-f", "query=query { viewer { login } }", "--method", "POST"])
    assert not runner.is_read(["gh", "pr", "merge", "1"])
    assert not runner.is_read(["gh", "api", "graphql", "-f", "query=mutation { deleteThing }"])


def test_command_cache_preserves_result_and_does_not_cache_failures(tmp_path, monkeypatch):
    runner = CachedReadCommand(SQLiteResponseCache(tmp_path / "cache.sqlite3"))
    calls = 0

    def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        class Result:
            returncode = 1
            stdout = "partial"
            stderr = "provider unavailable"
        return Result()

    monkeypatch.setattr("procache.commands.subprocess.run", fake_run)
    first = runner.run(["gh", "pr", "view", "1"])
    second = runner.run(["gh", "pr", "view", "1"])
    assert (first.returncode, first.stdout, first.stderr) == (1, "partial", "provider unavailable")
    assert second.cache_hit is False
    assert calls == 2
