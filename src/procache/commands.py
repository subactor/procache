from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .cache import SQLiteResponseCache


@dataclass(frozen=True)
class CachedCommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str = ""
    cache_hit: bool = False


class CachedReadCommand:
    """Run allowlisted read commands through a shared SQLite response cache."""

    def __init__(self, cache: SQLiteResponseCache, *, ttl: float = 15.0) -> None:
        self.cache = cache
        self.ttl = ttl

    @staticmethod
    def is_read(args: Sequence[str]) -> bool:
        argv = tuple(args)
        if not argv or Path(argv[0]).name != "gh":
            return False
        if len(argv) >= 3 and argv[1] == "search":
            return argv[2] in {"prs", "issues", "repos", "commits", "code"}
        if len(argv) >= 3 and argv[1] in {"pr", "issue", "repo"}:
            return argv[2] in {"list", "view", "status", "checks"}
        if len(argv) >= 2 and argv[1] == "api":
            method = "GET"
            if "--method" in argv:
                method = argv[argv.index("--method") + 1].upper()
            # GET endpoints may use -f for URL query parameters. For GraphQL,
            # cache only a query document and never a mutation document.
            if "graphql" not in argv[2:]:
                return method == "GET"
            query = next(
                (
                    item.split("=", 1)[1]
                    for item in argv[2:]
                    if item.startswith("query=")
                ),
                None,
            )
            if query is None:
                try:
                    query = argv[argv.index("query") + 1]
                except (ValueError, IndexError):
                    query = None
            if query is None:
                return False
            return method in {"GET", "POST"} and not query.lstrip().startswith("mutation")
        return False

    def run(self, args: Sequence[str], *, timeout: int | None = None, env: dict[str, str] | None = None) -> CachedCommandResult:
        argv = tuple(str(item) for item in args)
        if not self.is_read(argv):
            return self._execute(argv, timeout=timeout, env=env)
        effective_env = os.environ if env is None else env
        scope = tuple(
            (name, effective_env.get(name, ""))
            for name in ("GH_HOST", "GH_REPO", "GH_CONFIG_DIR")
        )
        key = self.cache.key("command", argv, "scope", scope)
        def load() -> bytes:
            result = self._execute(argv, timeout=timeout, env=env)
            return json.dumps(
                {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr},
                separators=(",", ":"),
            ).encode()

        value, hit = self.cache.get_or_set(
            key,
            load,
            ttl=self.ttl,
            should_cache=lambda payload: json.loads(payload)["returncode"] == 0,
        )
        result = json.loads(value)
        return CachedCommandResult(
            argv,
            int(result["returncode"]),
            result["stdout"],
            result["stderr"],
            cache_hit=hit,
        )

    @staticmethod
    def _execute(args: tuple[str, ...], *, timeout: int | None, env: dict[str, str] | None) -> CachedCommandResult:
        proc = subprocess.run(args, check=False, timeout=timeout, env=env, capture_output=True, text=True)
        return CachedCommandResult(args, proc.returncode, proc.stdout, proc.stderr)
