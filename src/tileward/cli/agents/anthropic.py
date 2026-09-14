"""Anthropic Messages API <-> Tileward's OpenAI-compatible chat completions.

Claude Code speaks the Messages format only, and Tileward's `/v1/messages` is a BYOK passthrough
to the customer's own Anthropic key, not a path to Tileward's served models -- so `twcli launch
claude` runs this translation locally. Streaming event shapes are checked against Anthropic's docs.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ._util import new_id, safe_json_loads, text_from_blocks

_STOP_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}

_ERROR_TYPE = {
    401: "authentication_error",
    402: "invalid_request_error",  # Anthropic has no balance-exhausted type; closest honest fit
    403: "permission_error",
    404: "not_found_error",
    429: "rate_limit_error",
}


def to_chat_request(body: Dict[str, Any], *, model: str) -> Dict[str, Any]:
    """A Messages request body -> a chat-completions request body (no `model` key; the caller
    supplies that itself).

    Claude Code's `mid-conversation-system` beta can put a `role: "system"` message anywhere in
    the transcript, but Tileward 400s on a system message that isn't first -- every one found is
    folded into a single leading message instead.
    """
    system_parts: List[str] = []
    system = body.get("system")
    if system:
        system_parts.append(text_from_blocks(system))

    messages: List[Dict[str, Any]] = []
    for msg in body.get("messages") or []:
        role = msg.get("role")
        content = msg.get("content")
        text = content if isinstance(content, str) else text_from_blocks(content)
        if role == "system":
            if text:
                system_parts.append(text)
            continue
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        blocks = content if isinstance(content, list) else []
        if role == "assistant":
            messages.append(_assistant_message(blocks))
        else:
            messages.extend(_user_turn_messages(blocks))

    if system_parts:
        messages.insert(0, {"role": "system", "content": "\n\n".join(system_parts)})

    out: Dict[str, Any] = {"messages": messages}

    tools = body.get("tools")
    if tools:
        out["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.get("name"),
                    "description": t.get("description") or "",
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
            for t in tools
            if isinstance(t, dict) and t.get("name")
        ]
        tool_choice = _tool_choice(body.get("tool_choice"))
        if tool_choice is not None:
            out["tool_choice"] = tool_choice

    if body.get("max_tokens") is not None:
        out["max_tokens"] = body["max_tokens"]
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    out["stream"] = bool(body.get("stream"))
    return out


def _assistant_message(blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text_parts.append(str(block.get("text") or ""))
        elif kind == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id") or new_id("call"),
                    "type": "function",
                    "function": {
                        "name": block.get("name") or "",
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                }
            )
        # `thinking` / `redacted_thinking` / cache_control: no equivalent downstream, dropped.
    entry: Dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) or None}
    if tool_calls:
        entry["tool_calls"] = tool_calls
    return entry


def _user_turn_messages(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One Anthropic user turn -> one or more chat messages: each `tool_result` block becomes
    its own `role: "tool"` message, since chat completions allows only one per message."""
    out: List[Dict[str, Any]] = []
    text_parts: List[str] = []

    def flush() -> None:
        if text_parts:
            out.append({"role": "user", "content": "\n".join(text_parts)})
            text_parts.clear()

    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "tool_result":
            flush()
            result_text = text_from_blocks(block.get("content"))
            if block.get("is_error"):
                result_text = f"Error: {result_text}"
            out.append(
                {"role": "tool", "tool_call_id": block.get("tool_use_id"), "content": result_text}
            )
        elif kind == "text":
            text_parts.append(str(block.get("text") or ""))
        elif kind in ("image", "input_image"):
            text_parts.append("[omitted: image]")
    flush()
    return out


def _tool_choice(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return None
    kind = choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    return None


def from_chat_response(
    completion: Dict[str, Any], *, model: Optional[str] = None
) -> Dict[str, Any]:
    """A non-streaming chat completion -> a non-streaming Messages response."""
    choices = completion.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason") or "stop"

    content: List[Dict[str, Any]] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        content.append(
            {
                "type": "tool_use",
                "id": call.get("id") or new_id("toolu"),
                "name": fn.get("name") or "",
                "input": safe_json_loads(fn.get("arguments")),
            }
        )

    usage = completion.get("usage") or {}
    return {
        "id": completion.get("id") or new_id("msg"),
        "type": "message",
        "role": "assistant",
        "model": completion.get("model") or model or "",
        "content": content,
        "stop_reason": _STOP_REASON.get(finish_reason, "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or 0,
        },
    }


def _sse(event: str, data: Dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _block_stop(index: int) -> bytes:
    return _sse("content_block_stop", {"type": "content_block_stop", "index": index})


def stream_events(chunks: Iterator[Dict[str, Any]], *, model: str) -> Iterator[bytes]:
    """A stream of chat-completions chunks -> the Messages SSE event sequence:
    `message_start -> (content_block_start -> content_block_delta* -> content_block_stop)* ->
    message_delta -> message_stop`."""
    message_id = new_id("msg")
    started = False
    open_index: Optional[int] = None
    open_kind: Optional[str] = None
    next_index = 0
    call_index_to_block: Dict[int, int] = {}
    input_tokens = 0
    output_tokens = 0
    finish_reason = "stop"

    for chunk in chunks:
        usage = chunk.get("usage")
        if usage:
            input_tokens = usage.get("prompt_tokens") or input_tokens
            output_tokens = usage.get("completion_tokens") or output_tokens
        choices = chunk.get("choices") or []
        if not choices:
            continue  # the trailing usage-only chunk `stream_options` adds
        choice = choices[0]

        if not started:
            started = True
            yield _sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": message_id,
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": chunk.get("model") or model,
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": input_tokens, "output_tokens": 0},
                    },
                },
            )

        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]

        delta = choice.get("delta") or {}
        text = delta.get("content")
        if text:
            if open_kind != "text":
                if open_index is not None:
                    yield _block_stop(open_index)
                open_index, open_kind = next_index, "text"
                next_index += 1
                yield _sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": open_index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            yield _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": open_index,
                    "delta": {"type": "text_delta", "text": text},
                },
            )

        for call in delta.get("tool_calls") or []:
            call_idx = call.get("index", 0)
            block_idx = call_index_to_block.get(call_idx)
            if block_idx is None:
                if open_index is not None:
                    yield _block_stop(open_index)
                block_idx = next_index
                next_index += 1
                open_index, open_kind = block_idx, "tool_use"
                call_index_to_block[call_idx] = block_idx
                fn = call.get("function") or {}
                yield _sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": block_idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": call.get("id") or new_id("toolu"),
                            "name": fn.get("name") or "",
                            "input": {},
                        },
                    },
                )
            args = (call.get("function") or {}).get("arguments")
            if args:
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": block_idx,
                        "delta": {"type": "input_json_delta", "partial_json": args},
                    },
                )

    if open_index is not None:
        yield _block_stop(open_index)
    if not started:
        # An empty stream (no choices at all) still owes the client a well-formed message.
        yield _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0},
                },
            },
        )
    yield _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": _STOP_REASON.get(finish_reason, "end_turn"),
                "stop_sequence": None,
            },
            "usage": {"output_tokens": output_tokens},
        },
    )
    yield _sse("message_stop", {"type": "message_stop"})


def count_tokens(body: Dict[str, Any]) -> Dict[str, Any]:
    """`/v1/messages/count_tokens` -- an approximation for Claude Code's context gauge, not a
    bill; Tileward's own server-side metering stays authoritative."""
    parts: List[str] = []
    system = body.get("system")
    if system:
        parts.append(text_from_blocks(system))
    for msg in body.get("messages") or []:
        content = msg.get("content")
        parts.append(content if isinstance(content, str) else text_from_blocks(content))
    for tool in body.get("tools") or []:
        if isinstance(tool, dict):
            parts.append(json.dumps(tool))
    total_chars = sum(len(p) for p in parts)
    return {"input_tokens": max(1, total_chars // 4)}


def error_body(exc: Exception) -> Tuple[int, Dict[str, Any]]:
    status = getattr(exc, "status", None) or 500
    message = getattr(exc, "message", None) or str(exc)
    etype = _ERROR_TYPE.get(status, "api_error" if status >= 500 else "invalid_request_error")
    return status, {"type": "error", "error": {"type": etype, "message": message}}


def stream_error_event(exc: Exception) -> bytes:
    """A failure after headers are already sent -- an `event: error`, Anthropic's own documented
    shape for a mid-stream failure, instead of the connection just dying with no signal."""
    _, body = error_body(exc)
    return _sse("error", body)
