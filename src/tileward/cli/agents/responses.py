"""OpenAI Responses API <-> Tileward's OpenAI-compatible chat completions.

Codex dropped chat-completions support in 0.122 and speaks only Responses now; Tileward has no
`/v1/responses` route at all, so `twcli launch codex` runs the same kind of local translation as
`launch claude` (see `anthropic.py`), against the subset an agentic coding CLI actually exercises:
text output, function calls, and their results. Stateless by design -- `store` and
`previous_response_id` are accepted and ignored, since Codex resends full `input` every turn.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional, Tuple

import voluptuous as vol

from ._util import new_id, text_from_blocks

# The shape `to_chat_request` and `count_tokens` index into, checked before either runs. See the
# same-named schema in `anthropic.py` for what is and is not constrained.
REQUEST_SCHEMA = vol.Schema(
    {
        vol.Optional("instructions"): vol.Any(str, None, msg="expected a string"),
        vol.Optional("input"): vol.Any(str, list, None, msg="expected a string or a list"),
        vol.Optional("tools"): vol.Any(list, None, msg="expected a list"),
        vol.Optional("stream"): vol.Any(bool, None, msg="expected true or false"),
    },
    extra=vol.ALLOW_EXTRA,
)


def to_chat_request(body: Dict[str, Any], *, model: str) -> Dict[str, Any]:
    """Same fix as anthropic.py's `to_chat_request`: every system-role item -- `instructions`
    and any `role: "system"` message item -- is folded into one leading message, since Tileward
    400s on a system message that isn't first."""
    system_parts: List[str] = []
    instructions = body.get("instructions")
    if instructions:
        system_parts.append(str(instructions))

    messages: List[Dict[str, Any]] = []
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    else:
        for item in raw_input or []:
            if not isinstance(item, dict):
                continue
            if item.get("type", "message") == "message" and item.get("role") == "system":
                content = item.get("content")
                text = content if isinstance(content, str) else _flatten_parts(
                    content if isinstance(content, list) else []
                )
                if text:
                    system_parts.append(text)
                continue
            messages.extend(_input_item_messages(item))

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
                    "parameters": t.get("parameters") or {"type": "object", "properties": {}},
                },
            }
            for t in tools
            # non-function tool types (web_search, code_interpreter, ...) have no equivalent
            if isinstance(t, dict) and t.get("type", "function") == "function" and t.get("name")
        ]
        tool_choice = body.get("tool_choice")
        if isinstance(tool_choice, str) and tool_choice in ("auto", "none", "required"):
            out["tool_choice"] = tool_choice
        elif isinstance(tool_choice, dict) and tool_choice.get("name"):
            out["tool_choice"] = {"type": "function", "function": {"name": tool_choice["name"]}}

    if body.get("max_output_tokens") is not None:
        out["max_tokens"] = body["max_output_tokens"]
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        out["reasoning_effort"] = reasoning["effort"]
    out["stream"] = bool(body.get("stream"))
    return out


def _input_item_messages(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    kind = item.get("type", "message")
    if kind == "function_call":
        return [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": item.get("call_id") or item.get("id") or new_id("call"),
                        "type": "function",
                        "function": {
                            "name": item.get("name") or "",
                            "arguments": item.get("arguments") or "{}",
                        },
                    }
                ],
            }
        ]
    if kind == "function_call_output":
        output = item.get("output")
        return [
            {
                "role": "tool",
                "tool_call_id": item.get("call_id") or item.get("id"),
                "content": output if isinstance(output, str) else text_from_blocks(output),
            }
        ]
    if kind == "message":
        role = item.get("role") or "user"
        content = item.get("content")
        if isinstance(content, str):
            return [{"role": role, "content": content}]
        parts = content if isinstance(content, list) else []
        return [{"role": role, "content": _flatten_parts(parts)}]
    return []  # reasoning items and similar: no chat-completions equivalent, dropped


def _flatten_parts(parts: List[Any]) -> str:
    pieces: List[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("input_text", "output_text", "text"):
            pieces.append(str(part.get("text") or ""))
        elif kind in ("input_image", "image_url"):
            pieces.append("[omitted: image]")
        else:
            pieces.append(f"[omitted: {kind}]")
    return "\n".join(p for p in pieces if p)


def from_chat_response(
    completion: Dict[str, Any], *, model: Optional[str] = None
) -> Dict[str, Any]:
    choices = completion.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason") or "stop"

    output: List[Dict[str, Any]] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        output.append(
            {
                "type": "message",
                "id": new_id("msg"),
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        output.append(
            {
                "type": "function_call",
                "id": new_id("fc"),
                "call_id": call.get("id") or new_id("call"),
                "name": fn.get("name") or "",
                "arguments": fn.get("arguments") or "{}",
                "status": "completed",
            }
        )

    usage = _usage(completion.get("usage") or {})
    status = "incomplete" if finish_reason == "length" else "completed"
    out = {
        "id": completion.get("id") or new_id("resp"),
        "object": "response",
        "model": completion.get("model") or model or "",
        "status": status,
        "output": output,
        "usage": usage,
    }
    if status == "incomplete":
        out["incomplete_details"] = {"reason": "max_output_tokens"}
    return out


def _usage(raw: Dict[str, Any]) -> Dict[str, Any]:
    input_tokens = raw.get("prompt_tokens") or 0
    output_tokens = raw.get("completion_tokens") or 0
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": raw.get("total_tokens") or (input_tokens + output_tokens),
    }


def stream_events(chunks: Iterator[Dict[str, Any]], *, model: str) -> Iterator[bytes]:
    """A stream of chat-completions chunks -> the Responses SSE event sequence."""
    response_id = new_id("resp")
    seq = 0

    def emit(event_type: str, **fields: Any) -> bytes:
        nonlocal seq
        payload = {"type": event_type, "sequence_number": seq, **fields}
        seq += 1
        return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n".encode()

    started = False
    open_index: Optional[int] = None
    open_kind: Optional[str] = None  # "message" | "function_call"
    next_index = 0
    output_items: List[Dict[str, Any]] = []
    # openai tool_call stream index -> (output_index, item_id, call_id, name, arguments so far)
    calls: Dict[int, Dict[str, Any]] = {}
    text_item_id: Optional[str] = None
    text_so_far = ""
    input_tokens = 0
    output_tokens = 0
    finish_reason = "stop"

    def close_open_item() -> Iterator[bytes]:
        nonlocal open_index, open_kind
        if open_kind == "message":
            yield emit("response.output_text.done", item_id=text_item_id, output_index=open_index,
                        content_index=0, text=text_so_far)
            output_items.append(
                {
                    "type": "message",
                    "id": text_item_id,
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text_so_far, "annotations": []}],
                }
            )
            yield emit("response.output_item.done", output_index=open_index, item=output_items[-1])
        elif open_kind == "function_call":
            call = next(c for c in calls.values() if c["output_index"] == open_index)
            yield emit(
                "response.function_call_arguments.done",
                item_id=call["item_id"],
                output_index=open_index,
                arguments=call["arguments"],
            )
            item = {
                "type": "function_call",
                "id": call["item_id"],
                "call_id": call["call_id"],
                "name": call["name"],
                "arguments": call["arguments"],
                "status": "completed",
            }
            output_items.append(item)
            yield emit("response.output_item.done", output_index=open_index, item=item)
        open_index, open_kind = None, None

    for chunk in chunks:
        usage = chunk.get("usage")
        if usage:
            input_tokens = usage.get("prompt_tokens") or input_tokens
            output_tokens = usage.get("completion_tokens") or output_tokens
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]

        if not started:
            started = True
            initial = _response_obj(
                response_id, model, "in_progress", [], input_tokens, output_tokens
            )
            yield emit("response.created", response=initial)

        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]

        delta = choice.get("delta") or {}
        text = delta.get("content")
        if text:
            if open_kind != "message":
                for piece in close_open_item():
                    yield piece
                text_item_id = new_id("msg")
                text_so_far = ""
                open_index, open_kind = next_index, "message"
                next_index += 1
                yield emit(
                    "response.output_item.added",
                    output_index=open_index,
                    item={
                        "type": "message",
                        "id": text_item_id,
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    },
                )
            text_so_far += text
            yield emit(
                "response.output_text.delta",
                item_id=text_item_id,
                output_index=open_index,
                content_index=0,
                delta=text,
            )

        for call in delta.get("tool_calls") or []:
            call_idx = call.get("index", 0)
            state = calls.get(call_idx)
            if state is None:
                for piece in close_open_item():
                    yield piece
                idx = next_index
                next_index += 1
                fn = call.get("function") or {}
                state = {
                    "output_index": idx,
                    "item_id": new_id("fc"),
                    "call_id": call.get("id") or new_id("call"),
                    "name": fn.get("name") or "",
                    "arguments": "",
                }
                calls[call_idx] = state
                open_index, open_kind = idx, "function_call"
                yield emit(
                    "response.output_item.added",
                    output_index=idx,
                    item={
                        "type": "function_call",
                        "id": state["item_id"],
                        "call_id": state["call_id"],
                        "name": state["name"],
                        "arguments": "",
                        "status": "in_progress",
                    },
                )
            args = (call.get("function") or {}).get("arguments")
            if args:
                state["arguments"] += args
                yield emit(
                    "response.function_call_arguments.delta",
                    item_id=state["item_id"],
                    output_index=state["output_index"],
                    delta=args,
                )

    for piece in close_open_item():
        yield piece

    final_status = "incomplete" if finish_reason == "length" else "completed"
    response = _response_obj(
        response_id, model, final_status, output_items, input_tokens, output_tokens
    )
    if final_status == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    yield emit(f"response.{final_status}", response=response)


def _response_obj(
    response_id: str,
    model: str,
    status: str,
    output: List[Dict[str, Any]],
    input_tokens: int,
    output_tokens: int,
) -> Dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "model": model,
        "status": status,
        "output": output,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def count_tokens(body: Dict[str, Any]) -> Dict[str, Any]:
    """No official count_tokens leg on the Responses API; kept for interface parity with
    `anthropic.py`."""
    parts: List[str] = []
    instructions = body.get("instructions")
    if instructions:
        parts.append(str(instructions))
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        parts.append(raw_input)
    else:
        for item in raw_input or []:
            if isinstance(item, dict):
                parts.append(json.dumps(item))
    total_chars = sum(len(p) for p in parts)
    return {"input_tokens": max(1, total_chars // 4)}


def error_body(exc: Exception) -> Tuple[int, Dict[str, Any]]:
    status = getattr(exc, "status", None) or 500
    message = getattr(exc, "message", None) or str(exc)
    code = getattr(exc, "code", None)
    etype = "invalid_request_error" if status < 500 else "server_error"
    return status, {"error": {"message": message, "type": etype, "code": code}}


def stream_error_event(exc: Exception) -> bytes:
    """A failure after headers are already sent -- a `response.failed` event instead of the
    connection just dying with no signal."""
    _, body = error_body(exc)
    response = _response_obj(new_id("resp"), "", "failed", [], 0, 0)
    response["error"] = body["error"]
    payload = {"type": "response.failed", "sequence_number": 0, "response": response}
    return f"event: response.failed\ndata: {json.dumps(payload)}\n\n".encode()
