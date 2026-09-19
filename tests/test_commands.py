from procache import CachedReadCommand, SQLiteResponseCache
import subprocess

import pytest


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


@pytest.mark.parametrize('arguments', [
    ['repos/acme/demo/issues', '-X', 'POST'],
    ['repos/acme/demo/issues', '-XPOST'],
    ['repos/acme/demo/issues', '--method=PATCH'],
    ['repos/acme/demo/issues', '-f', 'title=new'],
    ['repos/acme/demo/issues', '--raw-field=title=new'],
    ['repos/acme/demo/issues', '-F', 'title=new'],
    ['repos/acme/demo/issues', '--input', 'body.json'],
    ['graphql', '-f', 'query=# comment\nmutation { changeThing }'],
    ['graphql', '-f', 'query=query { viewer { login } } mutation { changeThing }'],
    ['graphql', '-f', 'query=subscription { changed }'],
    ['graphql', '-F', 'query=@query.graphql'],
    ['graphql', '--input', '-'],
    ['repos/acme/demo', '--method'],
    ['repos/acme/demo', '--unknown-flag'],
])
def test_mutations_and_ambiguous_requests_execute_each_time(tmp_path, monkeypatch, arguments):
    calls = []
    def execute(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '{}', '')
    monkeypatch.setattr('procache.commands.subprocess.run', execute)
    runner = CachedReadCommand(SQLiteResponseCache(tmp_path / 'cache.sqlite3'))
    for _ in range(2):
        assert not runner.run(['gh', 'api', *arguments]).cache_hit
    assert len(calls) == 2


@pytest.mark.parametrize('arguments', [
    ['repos/acme/demo/issues', '--method', 'GET', '-f', 'state=open'],
    ['repos/acme/demo/issues', '-XGET', '--field=page=1'],
    ['graphql', '-f', 'query=# comment\nquery { viewer { login } }'],
    ['graphql', '-f', 'query={ viewer { login } }'],
])
def test_explicit_reads_still_hit_cache(tmp_path, monkeypatch, arguments):
    calls = []
    def execute(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '{}', '')
    monkeypatch.setattr('procache.commands.subprocess.run', execute)
    runner = CachedReadCommand(SQLiteResponseCache(tmp_path / 'cache.sqlite3'))
    assert not runner.run(['gh', 'api', *arguments]).cache_hit
    assert runner.run(['gh', 'api', *arguments]).cache_hit
    assert len(calls) == 1


def test_credentials_config_and_repository_context_partition_cache(tmp_path, monkeypatch):
    cache = SQLiteResponseCache(tmp_path / 'cache.sqlite3')
    runner = CachedReadCommand(cache)
    calls = []
    def execute(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, str(len(calls)), '')
    monkeypatch.setattr('procache.commands.subprocess.run', execute)
    env = {'GH_CONFIG_DIR': str(tmp_path / 'gh'), 'GH_TOKEN': 'synthetic-secret-a'}
    args = ['gh', 'api', 'user']
    first = runner.run(args, env=env)
    assert runner.run(args, env=env).stdout == first.stdout
    env['GH_TOKEN'] = 'synthetic-secret-b'
    assert runner.run(args, env=env).stdout != first.stdout
    config = tmp_path / 'gh'
    config.mkdir()
    (config / 'hosts.yml').write_text('new-account')
    assert not runner.run(args, env=env).cache_hit
    other = tmp_path / 'repository'
    other.mkdir()
    monkeypatch.chdir(other)
    assert not runner.run(args, env=env).cache_hit
    assert len(calls) == 4
    assert b'synthetic-secret' not in (tmp_path / 'cache.sqlite3').read_bytes()


def test_textual_403_stops_further_commands_for_same_identity(tmp_path, monkeypatch):
    calls = []
    def execute(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, '', 'HTTP 403: API rate limit exceeded')
    monkeypatch.setattr('procache.commands.subprocess.run', execute)
    cache = SQLiteResponseCache(tmp_path / 'cache.sqlite3')
    first = CachedReadCommand(cache)
    second = CachedReadCommand(cache)
    env = {'GH_CONFIG_DIR': str(tmp_path / 'gh'), 'GH_TOKEN': 'synthetic-a'}
    assert first.run(['gh', 'api', 'user'], env=env).returncode == 1
    assert second.run(['gh', 'api', 'repos/acme/demo'], env=env).returncode == 75
    assert len(calls) == 1


def test_unrelated_issue_number_is_not_a_rate_limit():
    from procache import is_rate_limit_error
    assert not is_rate_limit_error(RuntimeError('Issue 429 is missing'))
    assert not is_rate_limit_error(RuntimeError('HTTP 403: permission denied'))
