"""`POST /v1/chat/completions` — OpenAI-shaped, with streaming.

A governed refusal is not an HTTP error: it arrives as a completion with
`finish_reason: content_filter` and zero tokens billed. `create` passes it through;
`say` raises.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Iterator
from typing import Any, Dict, List, Literal, Optional, Union, overload

from ..errors import GuardRefusal

PATH = "/v1/chat/completions"

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
    """The refusal text if governance blocked this call, else None."""
    for choice in completion.get("choices") or []:
        if isinstance(choice, dict) and choice.get("finish_reason") == "content_filter":
            return text_of(completion) or "This request was refused by governance."
    return None


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
    ) -> Iterator[Dict[str, Any]]: ...

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
        **extra: Any,
    ) -> Union[Dict[str, Any], Iterator[Dict[str, Any]]]:
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
            return self._client._transport.stream_sse("POST", PATH, json=body, timeout=timeout)
        return self._client._transport.request("POST", PATH, json=body, timeout=timeout)


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
    ) -> AsyncIterator[Dict[str, Any]]: ...

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
        **extra: Any,
    ) -> Union[Dict[str, Any], AsyncIterator[Dict[str, Any]]]:
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
            # Not awaited: the async generator is the return value, and awaiting it here would
            # buffer the whole answer before the caller saw a token.
            return self._client._transport.stream_sse("POST", PATH, json=body, timeout=timeout)
        return await self._client._transport.request("POST", PATH, json=body, timeout=timeout)


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
