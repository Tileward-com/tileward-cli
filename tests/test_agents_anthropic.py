from __future__ import annotations

import json

import pytest

from tileward import errors
from tileward.cli.agents import anthropic


def parse_sse(pieces):
    """[b"event: x\\ndata: {...}\\n\\n", ...] -> [("x", {...}), ...]."""
    events = []
    for piece in pieces:
        text = piece.decode("utf-8")
        event_line, data_line = text.strip("\n").split("\n", 1)
        assert event_line.startswith("event: ")
        assert data_line.startswith("data: ")
        events.append((event_line[len("event: ") :], json.loads(data_line[len("data: ") :])))
    return events


# ---- request translation ------------------------------------------------------------------


def test_system_string_and_user_text():
    body = {"system": "Be terse.", "messages": [{"role": "user", "content": "hi"}]}
    out = anthropic.to_chat_request(body, model="x")
    assert out["messages"] == [
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "hi"},
    ]
    assert out["stream"] is False


def test_mid_conversation_system_message_is_folded_into_the_leading_one():
    """Real bug, found running a live agentic session through the proxy, not a synthetic case:
    Claude Code's `mid-conversation-system` beta can put a `role: "system"` message anywhere in
    `messages`, and Tileward's backend 400s on a system message that isn't the first entry."""
    body = {
        "system": "Be terse.",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "system", "content": "Also: always answer in French."},
            {"role": "user", "content": "how are you?"},
        ],
    }
    out = anthropic.to_chat_request(body, model="x")
    assert out["messages"][0] == {
        "role": "system",
        "content": "Be terse.\n\nAlso: always answer in French.",
    }
    assert [m["role"] for m in out["messages"]] == ["system", "user", "assistant", "user"]
    assert sum(1 for m in out["messages"] if m["role"] == "system") == 1


def test_system_as_block_list_is_flattened_to_text():
    body = {
        "system": [{"type": "text", "text": "Be terse."}],
        "messages": [{"role": "user", "content": "hi"}],
    }
    out = anthropic.to_chat_request(body, model="x")
    assert out["messages"][0] == {"role": "system", "content": "Be terse."}


def test_tool_use_and_matching_tool_result_round_trip():
    body = {
        "messages": [
            {"role": "user", "content": "what's the weather in SF?"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_weather",
                        "input": {"city": "SF"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "68F"}],
            },
        ]
    }
    out = anthropic.to_chat_request(body, model="x")
    assert out["messages"][1]["tool_calls"] == [
        {
            "id": "toolu_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": json.dumps({"city": "SF"})},
        }
    ]
    assert out["messages"][2] == {"role": "tool", "tool_call_id": "toolu_1", "content": "68F"}


def test_multiple_tool_results_in_one_turn_split_into_separate_messages_in_order():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": "1"},
                    {"type": "tool_result", "tool_use_id": "b", "content": "2"},
                    {"type": "text", "text": "thanks"},
                ],
            }
        ]
    }
    out = anthropic.to_chat_request(body, model="x")
    assert out["messages"] == [
        {"role": "tool", "tool_call_id": "a", "content": "1"},
        {"role": "tool", "tool_call_id": "b", "content": "2"},
        {"role": "user", "content": "thanks"},
    ]


def test_tool_result_error_flag_is_prefixed():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": "boom", "is_error": True}
                ],
            }
        ]
    }
    out = anthropic.to_chat_request(body, model="x")
    assert out["messages"][0]["content"] == "Error: boom"


def test_tools_and_tool_choice_shape():
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "name": "get_weather",
                "description": "weather lookup",
                "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
            }
        ],
        "tool_choice": {"type": "tool", "name": "get_weather"},
    }
    out = anthropic.to_chat_request(body, model="x")
    assert out["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "weather lookup",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    assert out["tool_choice"] == {"type": "function", "function": {"name": "get_weather"}}


@pytest.mark.parametrize(
    "anthropic_choice,chat_choice",
    [
        ({"type": "auto"}, "auto"),
        ({"type": "any"}, "required"),
        ({"type": "none"}, "none"),
    ],
)
def test_tool_choice_auto_any_none(anthropic_choice, chat_choice):
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "t", "input_schema": {}}],
        "tool_choice": anthropic_choice,
    }
    out = anthropic.to_chat_request(body, model="x")
    assert out["tool_choice"] == chat_choice


def test_max_tokens_temperature_top_p_stop_sequences_pass_through():
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 512,
        "temperature": 0.2,
        "top_p": 0.9,
        "stop_sequences": ["</done>"],
        "stream": True,
    }
    out = anthropic.to_chat_request(body, model="x")
    assert out["max_tokens"] == 512
    assert out["temperature"] == 0.2
    assert out["top_p"] == 0.9
    assert out["stop"] == ["</done>"]
    assert out["stream"] is True


# ---- response translation (non-streaming) --------------------------------------------------


def test_from_chat_response_text():
    completion = {
        "id": "chatcmpl-1",
        "model": "tileward-35b-a3b",
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": "Hello!"}}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    }
    out = anthropic.from_chat_response(completion)
    assert out["type"] == "message"
    assert out["role"] == "assistant"
    assert out["content"] == [{"type": "text", "text": "Hello!"}]
    assert out["stop_reason"] == "end_turn"
    assert out["usage"] == {"input_tokens": 10, "output_tokens": 3}


def test_from_chat_response_tool_calls_map_to_tool_use_blocks():
    completion = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
                        }
                    ],
                },
            }
        ],
        "usage": {},
    }
    out = anthropic.from_chat_response(completion)
    assert out["content"] == [
        {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "SF"}}
    ]
    assert out["stop_reason"] == "tool_use"


def test_from_chat_response_malformed_tool_arguments_degrade_to_empty_object():
    completion = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "function": {"name": "f", "arguments": '{"city": "SF"'}}
                    ],
                },
            }
        ],
        "usage": {},
    }
    out = anthropic.from_chat_response(completion)
    assert out["content"][0]["input"] == {}


def test_content_filter_maps_to_refusal_stop_reason():
    completion = {"choices": [{"finish_reason": "content_filter", "message": {"content": "no."}}]}
    out = anthropic.from_chat_response(completion)
    assert out["stop_reason"] == "refusal"


# ---- streaming ------------------------------------------------------------------------------


def test_stream_events_text_only_reconstructs_full_text_and_ordering():
    chunks = [
        {"model": "x", "choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
    ]
    events = parse_sse(list(anthropic.stream_events(iter(chunks), model="x")))
    kinds = [e for e, _ in events]
    assert kinds == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    text = "".join(d["delta"]["text"] for k, d in events if k == "content_block_delta")
    assert text == "Hello"
    message_delta = next(d for k, d in events if k == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "end_turn"
    assert message_delta["usage"]["output_tokens"] == 2


def test_stream_events_reports_the_prompt_size_on_the_final_delta():
    """Real bug, found when a `twcli launch claude` session died on a 400 at 262,145 tokens.

    Claude Code drives its context gauge -- and therefore auto-compact -- off the usage it is
    handed back. vLLM only reports `prompt_tokens` in the trailing usage-only chunk, which
    arrives long after `message_start` has already been emitted with a placeholder 0, so the
    count has to ride `message_delta`. While it did not, the gauge read empty on every turn,
    compaction never fired, and sessions grew until the server refused the request.
    """
    chunks = [
        {"model": "x", "choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 230145, "completion_tokens": 2}},
    ]
    events = parse_sse(list(anthropic.stream_events(iter(chunks), model="x")))
    message_delta = next(d for k, d in events if k == "message_delta")
    assert message_delta["usage"]["input_tokens"] == 230145
    assert message_delta["usage"]["output_tokens"] == 2


def _tool_call_chunk(index, *, id=None, name=None, arguments=None, finish_reason=None):
    """One chat-completions streaming chunk carrying a single `tool_calls` delta entry --
    avoids hand-counting braces five levels deep in inline literals."""
    function = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    call = {"index": index}
    if id is not None:
        call["id"] = id
    if function:
        call["function"] = function
    choice = {"delta": {"tool_calls": [call]}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {"choices": [choice]}


def test_stream_events_tool_use_matches_the_documented_shape():
    """Mirrors the get_weather example from Anthropic's own streaming docs: a text block, then
    a tool_use block streamed as name-first-then-argument-fragments."""
    chunks = [
        {"model": "x", "choices": [{"delta": {"content": "Okay, checking"}}]},
        _tool_call_chunk(0, id="call_1", name="get_weather", arguments=""),
        _tool_call_chunk(0, arguments='{"city":'),
        _tool_call_chunk(0, arguments=' "SF"}'),
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events = parse_sse(list(anthropic.stream_events(iter(chunks), model="x")))
    starts = [d for k, d in events if k == "content_block_start"]
    assert starts[0]["content_block"] == {"type": "text", "text": ""}
    assert starts[1]["content_block"]["type"] == "tool_use"
    assert starts[1]["content_block"]["id"] == "call_1"
    assert starts[1]["content_block"]["name"] == "get_weather"
    assert starts[1]["index"] == 1  # the tool_use block is index 1, after the text block at 0

    partials = "".join(
        d["delta"]["partial_json"]
        for k, d in events
        if k == "content_block_delta" and d["delta"]["type"] == "input_json_delta"
    )
    assert json.loads(partials) == {"city": "SF"}

    message_delta = next(d for k, d in events if k == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "tool_use"

    # both blocks were closed exactly once, in order
    stops = [d["index"] for k, d in events if k == "content_block_stop"]
    assert stops == [0, 1]


def test_stream_events_empty_stream_still_emits_a_well_formed_message():
    events = parse_sse(list(anthropic.stream_events(iter([]), model="x")))
    kinds = [e for e, _ in events]
    assert kinds == ["message_start", "message_delta", "message_stop"]


# ---- count_tokens and errors ------------------------------------------------------------------


def test_count_tokens_scales_with_length():
    short = anthropic.count_tokens({"messages": [{"role": "user", "content": "hi"}]})
    long = anthropic.count_tokens({"messages": [{"role": "user", "content": "hi " * 1000}]})
    assert short["input_tokens"] >= 1
    assert long["input_tokens"] > short["input_tokens"]


@pytest.mark.parametrize(
    "exc,status,etype",
    [
        (errors.AuthenticationError("no key", status=401), 401, "authentication_error"),
        (errors.InsufficientBalanceError("empty", status=402), 402, "invalid_request_error"),
        (errors.RateLimitError("slow down", status=429), 429, "rate_limit_error"),
        (errors.ServerError("bad gateway", status=502), 502, "api_error"),
    ],
)
def test_error_body_maps_status_to_anthropic_error_type(exc, status, etype):
    got_status, body = anthropic.error_body(exc)
    assert got_status == status
    assert body["type"] == "error"
    assert body["error"]["type"] == etype
    assert body["error"]["message"] == str(exc.message)


def test_stream_error_event_matches_anthropics_documented_shape():
    piece = anthropic.stream_error_event(errors.ServerError("upstream down", status=502))
    event_line, data_line = piece.decode("utf-8").strip("\n").split("\n", 1)
    assert event_line == "event: error"
    data = json.loads(data_line[len("data: ") :])
    assert data == {"type": "error", "error": {"type": "api_error", "message": "upstream down"}}
