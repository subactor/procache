from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .cache import ProviderCooldownError, SQLiteResponseCache, is_rate_limit_error


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
        self.cooldown_seconds = 60.0

    @staticmethod
    def is_read(args: Sequence[str]) -> bool:
        argv = tuple(args)
        if not argv or Path(argv[0]).name != "gh":
            return False
        if "--web" in argv or "--watch" in argv:
            return False
        if len(argv) >= 3 and argv[1] == "search":
            return argv[2] in {"prs", "issues", "repos", "commits", "code"}
        if len(argv) >= 3 and argv[1] in {"pr", "issue", "repo"}:
            return argv[2] in {"list", "view", "status", "checks"}
        if len(argv) >= 2 and argv[1] == "api":
            return _api_is_read(argv[2:])
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
        try:
            identity = _credential_scope(effective_env)
        except OSError:
            # If identity cannot be established, execute once without sharing.
            return self._execute(argv, timeout=timeout, env=env)
        key = self.cache.key("command-v2", argv, "scope", scope, os.getcwd(), identity)
        cooldown_key = self.cache.key("provider-v2", effective_env.get("GH_HOST", "github.com"), identity)
        def load() -> bytes:
            result = self._execute(argv, timeout=timeout, env=env)
            if result.returncode and is_rate_limit_error(RuntimeError(result.stderr)):
                self.cache.cooldown(cooldown_key, ttl=self.cooldown_seconds)
            return json.dumps(
                {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr},
                separators=(",", ":"),
            ).encode()

        try:
            value, hit = self.cache.get_or_set(
                key,
                load,
                ttl=self.ttl,
                should_cache=lambda payload: json.loads(payload)["returncode"] == 0,
                cooldown_key=cooldown_key,
                cooldown_seconds=self.cooldown_seconds,
                is_rate_limit=is_rate_limit_error,
            )
        except ProviderCooldownError as exc:
            return CachedCommandResult(argv, 75, "", str(exc))
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


def _credential_scope(env: dict[str, str]) -> str:
    """Hash identity inputs; credentials never become persisted cache metadata."""
    tokens = tuple(env.get(name, "") for name in (
        "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
    ))
    config = Path(env.get("GH_CONFIG_DIR") or
                  str(Path(env.get("XDG_CONFIG_HOME") or
                           str(Path(env.get("HOME") or str(Path.home())) / ".config")) / "gh"))
    try:
        auth_config = (config / "hosts.yml").read_bytes()
    except FileNotFoundError:
        auth_config = b""
    return SQLiteResponseCache.key("auth-v1", tokens, auth_config.hex())


def _api_is_read(arguments: Sequence[str]) -> bool:
    """Recognize gh's effective method; ambiguous request bodies bypass caching."""
    values = {"-X", "--method", "-f", "--raw-field", "-F", "--field",
              "-H", "--header", "--hostname", "-q", "--jq", "-t", "--template", "-p", "--preview"}
    switches = {"--paginate", "--slurp", "-i", "--include", "--silent", "--verbose"}
    endpoint = None
    method = None
    fields = []
    index = 0
    while index < len(arguments):
        token = arguments[index]
        option, separator, value = token.partition("=")
        if not separator and len(token) > 2 and token[:2] in {"-X", "-f", "-F", "-H", "-q", "-t", "-p"}:
            option, value, separator = token[:2], token[2:], "="
        if option in values:
            if not separator:
                index += 1
                if index >= len(arguments):
                    return False
                value = arguments[index]
            if option in {"-X", "--method"}:
                if method is not None:
                    return False
                method = value.upper()
            elif option in {"-f", "--raw-field", "-F", "--field"}:
                if "=" not in value or value.split("=", 1)[1].startswith("@"):
                    return False
                fields.append(value)
        elif token in switches:
            pass
        elif token.startswith("-") or endpoint is not None:
            return False
        else:
            endpoint = token
        index += 1
    if not endpoint:
        return False
    effective_method = method or ("POST" if fields else "GET")
    if endpoint != "graphql":
        return effective_method == "GET"
    queries = [value.split("=", 1)[1] for value in fields if value.startswith("query=")]
    if effective_method not in {"GET", "POST"} or len(queries) != 1:
        return False
    # Strip strings before comments so a '#' in a string cannot hide a mutation.
    document = re.sub(r'"(?:\\.|[^"\\])*"', '""', queries[0])
    document = re.sub(r"#[^\r\n]*", "", document).lstrip("\ufeff \t\r\n,")
    return bool(re.match(r"(?:query\b|\{)", document)) and not re.search(r"\b(?:mutation|subscription)\b", document)
