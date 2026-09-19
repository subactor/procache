from __future__ import annotations

import json
from typing import Any

from .cache import SQLiteResponseCache, is_rate_limit_error


class CachedPyGithubRequester:
    """Read-through adapter for PyGithub's JSON requester.

    Only GET responses with a known authentication context are cached.
    Response headers and request representation are preserved. All writes go
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
        identity = self._identity() if verb.upper() == "GET" else None
        if identity is None:
            return self._delegate.requestJsonAndCheck(
                verb, url, parameters, headers, input, follow_302_redirect
            )

        key = self._cache.key(
            "pygithub-v2", identity, verb.upper(), url, parameters, headers,
            input, follow_302_redirect,
        )

        def load() -> bytes:
            response_headers, payload = self._delegate.requestJsonAndCheck(
                verb, url, parameters, headers, input, follow_302_redirect
            )
            return json.dumps(
                [dict(response_headers), payload], separators=(",", ":"), default=str
            ).encode("utf-8")

        value, _ = self._cache.get_or_set(
            key,
            load,
            ttl=self._ttl,
            cooldown_key=self._cache.key("pygithub-provider-v2", identity),
            cooldown_seconds=60,
            is_rate_limit=is_rate_limit_error,
        )
        response_headers, payload = json.loads(value)
        return response_headers, payload

    def _identity(self) -> str | None:
        """Hash PyGithub's public auth context; unknown delegates bypass sharing."""
        try:
            auth = self._delegate.auth
            base_url = self._delegate.base_url
            token = auth.token if auth is not None else None
            token_type = auth.token_type if auth is not None else "anonymous"
        except AttributeError:
            return None
        if not isinstance(base_url, str) or not base_url:
            return None
        if auth is not None and (not isinstance(token, str) or not token):
            return None
        return self._cache.key("pygithub-auth-v1", base_url, token_type, token)
