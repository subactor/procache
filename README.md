# subactor-procache

`subactor-procache` is a small provider-neutral cache for read-side API work.
It uses SQLite in WAL mode, coalesces concurrent loads for the same key, and
keeps mutations outside the cache path. The same database can be shared by
several agent processes.

The `CachedReadCommand` adapter currently covers safe `gh` reads, including
read-only GraphQL queries. Failed commands are returned to the caller but are
never cached. Cache keys include the command and the non-secret GitHub scope
(`GH_HOST`, `GH_REPO`, and `GH_CONFIG_DIR`); tokens are never persisted.

```python
from procache import CachedReadCommand, SQLiteResponseCache

runner = CachedReadCommand(
    SQLiteResponseCache("/var/cache/subactor/github.sqlite3", namespace="github"),
    ttl=15,
)
result = runner.run(["gh", "pr", "view", "136", "--json", "state"])
```

Mutating commands must continue to use the existing command runner directly.
