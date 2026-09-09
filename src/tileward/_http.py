"""HTTP transport: auth, retries, error mapping, SSE.

Retries cover connection failures, 429 and 5xx only. Streaming responses are not
retried once bytes have arrived.
"""

from __future__ import annotations

import json as _json
import random
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any, Dict, Optional, Union

import httpx

from . import errors
from ._version import __version__

DEFAULT_TIMEOUT = 60.0
# Generation is slow by nature: a long completion legitimately takes minutes, and a read timeout
# that fires mid-answer bills for tokens the caller never sees.
STREAM_TIMEOUT = 600.0
DEFAULT_MAX_RETRIES = 2
RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})

USER_AGENT = f"tileward-python/{__version__}"

AuthMode = str  # "key" | "session" | "none"


def _user_agent(suffix: Optional[str] = None) -> str:
    return f"{USER_AGENT} ({suffix})" if suffix else USER_AGENT


def _decode(response: httpx.Response) -> Any:
    ctype = response.headers.get("content-type", "")
    if "json" in ctype:
        try:
            return response.json()
        except ValueError:
            pass
    text = response.text
    try:
        return _json.loads(text)
    except ValueError:
        return text


def _request_id(response: httpx.Response) -> Optional[str]:
    for header in ("x-request-id", "x-tileward-request-id", "cf-ray"):
        value = response.headers.get(header)
        if value:
            return value
    return None


def _retry_after(response: httpx.Response) -> Optional[float]:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _backoff(attempt: int, retry_after: Optional[float]) -> float:
    """Exponential with full jitter, floored by any Retry-After the server sent.

    Full jitter rather than a fixed doubling: a fleet of clients that all retry at exactly 1s, 2s,
    4s re-creates the burst that caused the 429 in the first place.
    """
    if retry_after is not None:
        return min(retry_after, 60.0)
    return min(0.5 * (2**attempt), 8.0) * (0.5 + random.random() / 2)


def _iter_sse(lines: Iterator[str]) -> Iterator[Dict[str, Any]]:
    """Yield decoded `data:` payloads from an SSE stream, stopping at `[DONE]`.

    The sentinel is not JSON, so a caller that json.loads every data line crashes on the last one.
    """
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if payload == "[DONE]":
            return
        try:
            yield _json.loads(payload)
        except ValueError:
            continue


class _Base:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: Optional[str] = None,
        session_token: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        user_agent_suffix: Optional[str] = None,
        default_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.session_token = session_token
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.default_headers = dict(default_headers or {})
        self.user_agent = _user_agent(user_agent_suffix)

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _headers(self, auth: AuthMode, extra: Optional[Mapping[str, str]]) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        headers.update(self.default_headers)
        if auth == "key":
            if not self.api_key:
                raise errors.ConfigError(
                    "No API key. Set TILEWARD_API_KEY, pass api_key=..., or run "
                    "`twcli auth login` and mint one with `twcli keys create`."
                )
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif auth == "session":
            if not self.session_token:
                raise errors.ConfigError(
                    "This needs a signed-in console session, not an API key. "
                    "Run `twcli auth login`."
                )
            # The account surface authenticates on the session COOKIE, not a bearer header —
            # `_session_sub` in the gateway reads `request.cookies`. Sending it as a bearer token
            # authenticates nothing and returns a confusing 401.
            headers["Cookie"] = f"tw_session={self.session_token}"
        if extra:
            headers.update({k: v for k, v in extra.items() if v is not None})
        return headers

    def _raise(self, response: httpx.Response) -> None:
        raise errors.from_response(response.status_code, _decode(response), _request_id(response))


class Transport(_Base):
    """Synchronous transport."""

    def __init__(self, *args: Any, client: Optional[httpx.Client] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout, follow_redirects=True)
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        auth: AuthMode = "key",
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
        raw: bool = False,
    ) -> Any:
        url = self._url(path)
        hdrs = self._headers(auth, headers)
        budget = self.max_retries if retries is None else max(0, retries)
        attempt = 0
        while True:
            try:
                response = self.client.request(
                    method,
                    url,
                    json=json,
                    params=_clean_params(params),
                    headers=hdrs,
                    timeout=timeout or self.timeout,
                )
            except httpx.TimeoutException as exc:
                if attempt >= budget:
                    raise errors.ConnectionError_(f"{method} {url} timed out: {exc}") from exc
            except httpx.HTTPError as exc:
                if attempt >= budget:
                    raise errors.ConnectionError_(f"{method} {url} failed: {exc}") from exc
            else:
                if response.status_code in RETRY_STATUS and attempt < budget:
                    time.sleep(_backoff(attempt, _retry_after(response)))
                    attempt += 1
                    continue
                if response.status_code >= 400:
                    self._raise(response)
                return response if raw else _decode(response)
            time.sleep(_backoff(attempt, None))
            attempt += 1

    def stream_sse(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        auth: AuthMode = "key",
        timeout: Optional[float] = None,
    ) -> Iterator[Dict[str, Any]]:
        url = self._url(path)
        hdrs = self._headers(auth, headers)
        hdrs["Accept"] = "text/event-stream"
        try:
            with self.client.stream(
                method, url, json=json, headers=hdrs, timeout=timeout or STREAM_TIMEOUT
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    self._raise(response)
                yield from _iter_sse(response.iter_lines())
        except httpx.HTTPError as exc:
            raise errors.ConnectionError_(f"{method} {url} stream failed: {exc}") from exc


class AsyncTransport(_Base):
    """Asynchronous transport. Same policy, same errors."""

    def __init__(
        self, *args: Any, client: Optional[httpx.AsyncClient] = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        auth: AuthMode = "key",
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
        raw: bool = False,
    ) -> Any:
        import asyncio

        url = self._url(path)
        hdrs = self._headers(auth, headers)
        budget = self.max_retries if retries is None else max(0, retries)
        attempt = 0
        while True:
            try:
                response = await self.client.request(
                    method,
                    url,
                    json=json,
                    params=_clean_params(params),
                    headers=hdrs,
                    timeout=timeout or self.timeout,
                )
            except httpx.TimeoutException as exc:
                if attempt >= budget:
                    raise errors.ConnectionError_(f"{method} {url} timed out: {exc}") from exc
            except httpx.HTTPError as exc:
                if attempt >= budget:
                    raise errors.ConnectionError_(f"{method} {url} failed: {exc}") from exc
            else:
                if response.status_code in RETRY_STATUS and attempt < budget:
                    await asyncio.sleep(_backoff(attempt, _retry_after(response)))
                    attempt += 1
                    continue
                if response.status_code >= 400:
                    self._raise(response)
                return response if raw else _decode(response)
            await asyncio.sleep(_backoff(attempt, None))
            attempt += 1

    async def stream_sse(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        auth: AuthMode = "key",
        timeout: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        url = self._url(path)
        hdrs = self._headers(auth, headers)
        hdrs["Accept"] = "text/event-stream"
        try:
            async with self.client.stream(
                method, url, json=json, headers=hdrs, timeout=timeout or STREAM_TIMEOUT
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    self._raise(response)
                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:") :].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        yield _json.loads(payload)
                    except ValueError:
                        continue
        except httpx.HTTPError as exc:
            raise errors.ConnectionError_(f"{method} {url} stream failed: {exc}") from exc


def _clean_params(params: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Drop None values so an unset optional does not become the literal string 'None'."""
    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None}


Streamable = Union[Iterator[Dict[str, Any]], AsyncIterator[Dict[str, Any]]]
