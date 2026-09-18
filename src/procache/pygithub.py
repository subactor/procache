from __future__ import annotations

import json
from typing import Any

from .cache import SQLiteResponseCache, is_rate_limit_error


class CachedPyGithubRequester:
    """Read-through adapter for PyGithub's JSON requester.

    Only GET responses are cached. All writes and all provider exceptions go
    directly through the delegated requester; rate-limit exceptions open the
    shared provider cooldown in ``SQLiteResponseCache``.
    """

    def __init__(self, delegate: Any, cache: SQLiteResponseCache, *, ttl: float = 15.0) -> None:
        self._delegate = delegate
        self._cache = cache
        self._ttl = ttl

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def requestJsonAndCheck(
        self,
        verb: str,
        url: str,
        parameters: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        input: Any | None = None,
        follow_302_redirect: bool = False,
    ) -> tuple[dict[str, Any], Any]:
        if verb.upper() != "GET":
            return self._delegate.requestJsonAndCheck(
                verb, url, parameters, headers, input, follow_302_redirect
            )

        key = self._cache.key("pygithub", verb.upper(), url, parameters, input)

        def load() -> bytes:
            response_headers, payload = self._delegate.requestJsonAndCheck(
                verb, url, parameters, headers, input, follow_302_redirect
            )
            return json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")

        value, _ = self._cache.get_or_set(
            key,
            load,
            ttl=self._ttl,
            cooldown_key="provider",
            cooldown_seconds=60,
            is_rate_limit=is_rate_limit_error,
        )
        return {}, json.loads(value)
