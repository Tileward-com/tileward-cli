"""`POST /v1/chat/completions` — OpenAI-shaped, with streaming.

A governed refusal is not an HTTP error: it arrives as a completion with id
`chatcmpl-governed` and `finish_reason: content_filter`. The guard's read of the prompt is
billed and reported as `prompt_tokens`; `completion_tokens` is 0. `create` passes it through;
`say` raises.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from typing import Any, Dict, List, Literal, Optional, Union, overload

from .._http import AsyncSSEStream, SSEStream
from ..errors import GuardRefusal

PATH = "/v1/chat/completions"
GOVERNED_ID = "chatcmpl-governed"
SOURCES_HEADER = "x-tileward-sources"

Message = Dict[str, Any]
MessageInput = Union[str, Iterable[Message]]


def normalize_messages(messages: MessageInput, system: Optional[str] = None) -> List[Message]:
    """Accept a bare string or a message list; always return a list.

    The string form exists because `tw.chat.say("hello")` is the first thing anyone types, and
    making that work should not cost them a lesson in the message schema.
    """
    if isinstance(messages, str):
        out: List[Message] = [{"role": "user", "content": messages}]
    else:
        out = [dict(m) for m in messages]
    if system:
        # Only if the caller has not already supplied one — silently shadowing their system prompt
        # would change the model's behaviour with no sign of it in the request they wrote.
        if not any(m.get("role") == "system" for m in out):
            out.insert(0, {"role": "system", "content": system})
    return out


def build_body(
    messages: MessageInput,
    *,
    model: str,
    system: Optional[str] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    stream: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": model,
        "messages": normalize_messages(messages, system),
    }
    if max_tokens is not None:
        body["max_tokens"] = int(max_tokens)
    if temperature is not None:
        body["temperature"] = float(temperature)
    if top_p is not None:
        body["top_p"] = float(top_p)
    if stream:
        body["stream"] = True
    if extra:
        body.update(extra)
    return body


def text_of(completion: Dict[str, Any]) -> str:
    """The assistant's message content, or "" if there is none."""
    for choice in completion.get("choices") or []:
        if isinstance(choice, dict):
            content = (choice.get("message") or {}).get("content")
            if isinstance(content, str):
                return content
    return ""


def delta_of(chunk: Dict[str, Any]) -> str:
    for choice in chunk.get("choices") or []:
        if isinstance(choice, dict):
            content = (choice.get("delta") or {}).get("content")
            if isinstance(content, str):
                return content
    return ""


def refusal_of(completion: Dict[str, Any]) -> Optional[str]:
    """The refusal text if the call finished with `content_filter`, else None.

    That covers a model that declined as well as the guard; `refused_by_gate` tells them apart.
    """
    for choice in completion.get("choices") or []:
        if isinstance(choice, dict) and choice.get("finish_reason") == "content_filter":
            return text_of(completion) or "This request was refused."
    return None


def refused_by_gate(payload: Dict[str, Any]) -> bool:
    """True when Tileward's guard refused the call before the model ran.

    Takes a completion or any chunk of a streamed one; the gateway gives each of them this id.
    `finish_reason` alone cannot tell: a model that declines also finishes with `content_filter`.
    """
    return payload.get("id") == GOVERNED_ID


def sources_of(headers: Mapping[str, str]) -> Optional[List[Dict[str, Any]]]:
    """`X-Tileward-Sources`: a `{doc, folder}` for each passage the model was given.

    `[]` when the header is absent. None when it is there but unreadable: the gateway caps its
    length, so a long list can arrive cut off, and None keeps that from passing for "none".
    """
    raw = next((value for name, value in headers.items() if name.lower() == SOURCES_HEADER), None)
    if raw is None:
        return []
    try:
        rows = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return None
    return rows


class ChatStream(SSEStream):
    """A streamed completion: iterate it for chunks.

    No chunk carries the `tileward` object a completion has, so what the answer was grounded on
    arrives as a response header. `sources` and `headers` are ready before the first chunk;
    reading either sends the request if iterating has not.
    """

    @property
    def sources(self) -> Optional[List[Dict[str, Any]]]:
        """See `sources_of`."""
        return sources_of(self.headers)


class AsyncChatStream(AsyncSSEStream):
    """The async twin of `ChatStream`. `create` returns it open, so `sources` is ready."""

    @property
    def sources(self) -> Optional[List[Dict[str, Any]]]:
        """See `sources_of`."""
        return sources_of(self.headers)


class Completions:
    def __init__(self, client: Any) -> None:
        self._client = client

    # WHY OVERLOADS. `stream` changes what comes back — a completion dict, or an iterator of
    # chunks — and a union return type pushes that decision onto every caller as a narrowing
    # problem they did not ask for. With these, `create(..., stream=True)` is an iterator to a
    # type checker and to an editor's autocomplete, and `create(...)` is a completion.
    @overload
    def create(
        self,
        messages: MessageInput,
        *,
        stream: Literal[False] = False,
        **kwargs: Any,
    ) -> Dict[str, Any]: ...

    @overload
    def create(
        self,
        messages: MessageInput,
        *,
        stream: Literal[True],
        **kwargs: Any,
    ) -> ChatStream: ...

    def create(
        self,
        messages: MessageInput,
        *,
        model: Optional[str] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        stream: bool = False,
        timeout: Optional[float] = None,
        headers: Optional[Mapping[str, str]] = None,
        **extra: Any,
    ) -> Union[Dict[str, Any], ChatStream]:
        resolved = model or self._client.models.default()
        if not resolved:
            from ..errors import ConfigError

            raise ConfigError(
                "No model given and the served catalogue is empty. "
                "Pass model=..., or check `twcli models list`."
            )
        body = build_body(
            messages,
            model=resolved,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stream=stream,
            extra=extra or None,
        )
        if stream:
            return ChatStream(
                self._client._transport, "POST", PATH, json=body, headers=headers, timeout=timeout
            )
        return self._client._transport.request(
            "POST", PATH, json=body, headers=headers, timeout=timeout
        )


class Chat:
    def __init__(self, client: Any) -> None:
        self._client = client
        self.completions = Completions(client)

    def say(self, prompt: MessageInput, **kwargs: Any) -> str:
        """One prompt in, the answer's text out. Raises `GuardRefusal` if governance blocked it."""
        kwargs.pop("stream", None)
        completion = self.completions.create(prompt, stream=False, **kwargs)
        refusal = refusal_of(completion)
        if refusal:
            raise GuardRefusal(refusal, completion=completion)
        return text_of(completion)

    def stream(self, prompt: MessageInput, **kwargs: Any) -> Iterator[str]:
        """Yield text deltas as they arrive."""
        kwargs.pop("stream", None)
        for chunk in self.completions.create(prompt, stream=True, **kwargs):
            piece = delta_of(chunk)
            if piece:
                yield piece


class AsyncCompletions:
    def __init__(self, client: Any) -> None:
        self._client = client

    @overload
    async def create(
        self,
        messages: MessageInput,
        *,
        stream: Literal[False] = False,
        **kwargs: Any,
    ) -> Dict[str, Any]: ...

    @overload
    async def create(
        self,
        messages: MessageInput,
        *,
        stream: Literal[True],
        **kwargs: Any,
    ) -> AsyncChatStream: ...

    async def create(
        self,
        messages: MessageInput,
        *,
        model: Optional[str] = None,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        stream: bool = False,
        timeout: Optional[float] = None,
        headers: Optional[Mapping[str, str]] = None,
        **extra: Any,
    ) -> Union[Dict[str, Any], AsyncChatStream]:
        resolved = model or await self._client.models.default()
        if not resolved:
            from ..errors import ConfigError

            raise ConfigError(
                "No model given and the served catalogue is empty. "
                "Pass model=..., or check `twcli models list`."
            )
        body = build_body(
            messages,
            model=resolved,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stream=stream,
            extra=extra or None,
        )
        if stream:
            # Opened here, so `sources` is ready when the await returns. Only the headers are
            # awaited; the answer still arrives as the caller iterates.
            chunks = AsyncChatStream(
                self._client._transport, "POST", PATH, json=body, headers=headers, timeout=timeout
            )
            await chunks.start()
            return chunks
        return await self._client._transport.request(
            "POST", PATH, json=body, headers=headers, timeout=timeout
        )


class AsyncChat:
    def __init__(self, client: Any) -> None:
        self._client = client
        self.completions = AsyncCompletions(client)

    async def say(self, prompt: MessageInput, **kwargs: Any) -> str:
        kwargs.pop("stream", None)
        completion = await self.completions.create(prompt, stream=False, **kwargs)
        refusal = refusal_of(completion)
        if refusal:
            raise GuardRefusal(refusal, completion=completion)
        return text_of(completion)

    async def stream(self, prompt: MessageInput, **kwargs: Any) -> AsyncIterator[str]:
        kwargs.pop("stream", None)
        stream = await self.completions.create(prompt, stream=True, **kwargs)
        async for chunk in stream:
            piece = delta_of(chunk)
            if piece:
                yield piece
