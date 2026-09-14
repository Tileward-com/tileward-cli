from __future__ import annotations

import json

import pytest

from tileward import errors
from tileward.cli.agents import responses


def parse_sse(pieces):
    events = []
    for piece in pieces:
        text = piece.decode("utf-8")
        event_line, data_line = text.strip("\n").split("\n", 1)
        assert event_line.startswith("event: ")
        assert data_line.startswith("data: ")
        events.append((event_line[len("event: ") :], json.loads(data_line[len("data: ") :])))
    return events


# ---- request translation ------------------------------------------------------------------


def test_input_string_shorthand():
    out = responses.to_chat_request({"input": "hi"}, model="x")
    assert out["messages"] == [{"role": "user", "content": "hi"}]


def test_instructions_becomes_system_message():
    out = responses.to_chat_request({"instructions": "Be terse.", "input": "hi"}, model="x")
    assert out["messages"][0] == {"role": "system", "content": "Be terse."}


def test_mid_input_system_item_is_folded_into_the_leading_message():
    """Same fix as anthropic.py's identical test: Tileward's backend 400s on a system message
    that isn't the first entry, so a `role: "system"` item found later in `input` is merged in."""
    body = {
        "instructions": "Be terse.",
        "input": [
            {"type": "message", "role": "user", "content": "hi"},
            {"type": "message", "role": "system", "content": "Also: answer in French."},
            {"type": "message", "role": "user", "content": "how are you?"},
        ],
    }
    out = responses.to_chat_request(body, model="x")
    assert out["messages"][0] == {
        "role": "system",
        "content": "Be terse.\n\nAlso: answer in French.",
    }
    assert [m["role"] for m in out["messages"]] == ["system", "user", "user"]


def test_message_item_with_content_parts_is_flattened():
    body = {
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi there"}],
            }
        ]
    }
    out = responses.to_chat_request(body, model="x")
    assert out["messages"] == [{"role": "user", "content": "hi there"}]


def test_function_call_and_output_round_trip():
    body = {
        "input": [
            {"type": "message", "role": "user", "content": "weather in SF?"},
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "get_weather",
                "arguments": '{"city": "SF"}',
            },
            {"type": "function_call_output", "call_id": "call_1", "output": "68F"},
        ]
    }
    out = responses.to_chat_request(body, model="x")
    assert out["messages"][1]["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
        }
    ]
    assert out["messages"][2] == {"role": "tool", "tool_call_id": "call_1", "content": "68F"}


def test_tools_are_flat_and_get_wrapped():
    tool = {
        "type": "function",
        "name": "get_weather",
        "description": "d",
        "parameters": {"type": "object"},
    }
    out = responses.to_chat_request({"input": "hi", "tools": [tool]}, model="x")
    assert out["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "d",
                "parameters": {"type": "object"},
            },
        }
    ]


def test_non_function_tools_are_dropped():
    body = {"input": "hi", "tools": [{"type": "web_search"}]}
    out = responses.to_chat_request(body, model="x")
    assert "tools" not in out or out["tools"] == []


@pytest.mark.parametrize("choice", ["auto", "none", "required"])
def test_tool_choice_string_passthrough(choice):
    body = {"input": "hi", "tools": [{"type": "function", "name": "t"}], "tool_choice": choice}
    out = responses.to_chat_request(body, model="x")
    assert out["tool_choice"] == choice


def test_tool_choice_named_is_wrapped():
    body = {
        "input": "hi",
        "tools": [{"type": "function", "name": "t"}],
        "tool_choice": {"type": "function", "name": "t"},
    }
    out = responses.to_chat_request(body, model="x")
    assert out["tool_choice"] == {"type": "function", "function": {"name": "t"}}


def test_max_output_tokens_and_reasoning_effort():
    body = {"input": "hi", "max_output_tokens": 256, "reasoning": {"effort": "low"}, "stream": True}
    out = responses.to_chat_request(body, model="x")
    assert out["max_tokens"] == 256
    assert out["reasoning_effort"] == "low"
    assert out["stream"] is True


def test_store_and_previous_response_id_are_silently_ignored():
    body = {"input": "hi", "store": True, "previous_response_id": "resp_123"}
    out = responses.to_chat_request(body, model="x")
    assert "store" not in out
    assert "previous_response_id" not in out


# ---- response translation (non-streaming) --------------------------------------------------


def test_from_chat_response_text():
    completion = {
        "choices": [{"finish_reason": "stop", "message": {"content": "Hello!"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    }
    out = responses.from_chat_response(completion)
    assert out["status"] == "completed"
    assert out["output"] == [
        {
            "type": "message",
            "id": out["output"][0]["id"],
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Hello!", "annotations": []}],
        }
    ]
    assert out["usage"] == {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}


def test_from_chat_response_function_call():
    completion = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "function": {"name": "get_weather", "arguments": "{}"}}
                    ],
                },
            }
        ]
    }
    out = responses.from_chat_response(completion)
    assert out["output"] == [
        {
            "type": "function_call",
            "id": out["output"][0]["id"],
            "call_id": "call_1",
            "name": "get_weather",
            "arguments": "{}",
            "status": "completed",
        }
    ]


def test_from_chat_response_length_marks_incomplete():
    completion = {"choices": [{"finish_reason": "length", "message": {"content": "cut off"}}]}
    out = responses.from_chat_response(completion)
    assert out["status"] == "incomplete"
    assert out["incomplete_details"] == {"reason": "max_output_tokens"}


# ---- streaming ------------------------------------------------------------------------------


def _tool_call_chunk(index, *, id=None, name=None, arguments=None, finish_reason=None):
    """See the identical helper in test_agents_anthropic.py: avoids hand-counting braces five
    levels deep (choices -> delta -> tool_calls -> function -> arguments)."""
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


def test_stream_events_text_and_function_call():
    chunks = [
        {"choices": [{"delta": {"content": "Okay"}}]},
        _tool_call_chunk(0, id="call_1", name="get_weather", arguments=""),
        _tool_call_chunk(0, arguments='{"city": "SF"}'),
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 4}},
    ]
    events = parse_sse(list(responses.stream_events(iter(chunks), model="x")))
    kinds = [e for e, _ in events]
    assert kinds[0] == "response.created"
    assert kinds[-1] == "response.completed"
    assert "response.output_item.added" in kinds
    assert "response.output_text.delta" in kinds
    assert "response.function_call_arguments.delta" in kinds
    assert "response.output_item.done" in kinds

    final = next(d for k, d in events if k == "response.completed")
    final_response = final["response"]
    assert final_response["status"] == "completed"
    kinds_in_output = [item["type"] for item in final_response["output"]]
    assert kinds_in_output == ["message", "function_call"]
    fn_item = next(i for i in final_response["output"] if i["type"] == "function_call")
    assert json.loads(fn_item["arguments"]) == {"city": "SF"}
    assert final_response["usage"] == {"input_tokens": 7, "output_tokens": 4, "total_tokens": 11}

    # every event after response.created carries a strictly increasing sequence_number
    seqs = [d["sequence_number"] for _, d in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_stream_events_length_finish_reason_marks_response_incomplete():
    chunks = [
        {"choices": [{"delta": {"content": "cut"}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
    ]
    events = parse_sse(list(responses.stream_events(iter(chunks), model="x")))
    kinds = [e for e, _ in events]
    assert kinds[-1] == "response.incomplete"


# ---- errors -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc,status",
    [
        (errors.AuthenticationError("no key", status=401), 401),
        (errors.ServerError("bad gateway", status=502), 502),
    ],
)
def test_error_body_shape(exc, status):
    got_status, body = responses.error_body(exc)
    assert got_status == status
    assert body["error"]["message"] == exc.message


def test_stream_error_event_is_a_response_failed_event():
    piece = responses.stream_error_event(errors.ServerError("upstream down", status=502))
    event_line, data_line = piece.decode("utf-8").strip("\n").split("\n", 1)
    assert event_line == "event: response.failed"
    data = json.loads(data_line[len("data: ") :])
    assert data["type"] == "response.failed"
    assert data["response"]["status"] == "failed"
    assert data["response"]["error"]["message"] == "upstream down"
