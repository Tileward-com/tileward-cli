from __future__ import annotations

import json

import httpx
import pytest
import respx

from tileward import errors
from tileward.resources import chat as chat_res
from tileward.resources import guard as guard_res
from tileward.resources.documents import MAX_FILE_BYTES, read_file


def tool_result(value):
    return {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"text": json.dumps(value)}]}}


# ---- chat -------------------------------------------------------------------------------
def test_a_bare_string_becomes_a_user_message():
    assert chat_res.normalize_messages("hi") == [{"role": "user", "content": "hi"}]


def test_system_is_prepended():
    out = chat_res.normalize_messages("hi", "be terse")
    assert out[0] == {"role": "system", "content": "be terse"}


def test_an_existing_system_message_is_not_shadowed():
    """Silently overriding the caller's system prompt would change behaviour invisibly."""
    given = [{"role": "system", "content": "mine"}, {"role": "user", "content": "hi"}]
    out = chat_res.normalize_messages(given, "theirs")
    assert [m["content"] for m in out if m["role"] == "system"] == ["mine"]


def test_unset_options_are_absent_from_the_body():
    body = chat_res.build_body("hi", model="m")
    assert set(body) == {"model", "messages"}


def test_refusal_is_detected_by_finish_reason():
    completion = {
        "choices": [{"finish_reason": "content_filter", "message": {"content": "Refused."}}]
    }
    assert chat_res.refusal_of(completion) == "Refused."


def test_a_normal_answer_is_not_a_refusal():
    completion = {"choices": [{"finish_reason": "stop", "message": {"content": "hello"}}]}
    assert chat_res.refusal_of(completion) is None
    assert chat_res.text_of(completion) == "hello"


@respx.mock
def test_say_raises_on_a_refusal_rather_than_returning_empty(client):
    respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "content_filter", "message": {"content": "Off policy."}}
                ],
                "usage": {"total_tokens": 0},
            },
        )
    )
    with pytest.raises(errors.GuardRefusal) as exc:
        client.chat.say("something", model="m")
    assert exc.value.completion["usage"]["total_tokens"] == 0


@respx.mock
def test_create_passes_a_refusal_through_untouched(client):
    """Anything metering responses has to be able to see the refusal."""
    respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"finish_reason": "content_filter", "message": {"content": "no"}}]},
        )
    )
    out = client.chat.completions.create("x", model="m")
    assert out["choices"][0]["finish_reason"] == "content_filter"


# ---- models -----------------------------------------------------------------------------
@respx.mock
def test_unknown_model_lists_what_is_actually_served(client):
    respx.get("https://api.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "served-a"}, {"id": "served-b"}]})
    )
    with pytest.raises(errors.NotFoundError) as exc:
        client.models.retrieve("tileward-3b")
    assert "served-a" in str(exc.value)


@respx.mock
def test_default_model_comes_from_the_catalogue_not_a_literal(client):
    respx.get("https://api.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "whatever-is-served"}]})
    )
    assert client.models.default() == "whatever-is-served"


# ---- guard ------------------------------------------------------------------------------
def test_omitting_both_lists_keeps_the_keys_bound_policy():
    """Sending empty lists would override the binding with 'govern nothing'."""
    body = guard_res.build_body("text")
    assert body == {"input": "text"}


def test_a_list_input_is_sent_as_a_batch():
    body = guard_res.build_body(["a", "b"], allow=["support"])
    assert body["input"] == ["a", "b"]
    assert body["allow"] == ["support"]


def test_allowed_requires_every_decision_to_pass():
    assert guard_res.allowed({"result": [{"allowed": True}, {"allowed": True}]})
    assert not guard_res.allowed({"result": [{"allowed": True}, {"allowed": False}]})


def test_allowed_is_false_when_there_are_no_decisions():
    """An empty result must never read as 'permitted'."""
    assert not guard_res.allowed({"result": []})
    assert not guard_res.allowed({})


def test_a_single_decision_is_normalised_to_a_list():
    assert len(guard_res.decisions({"result": {"allowed": False}})) == 1


# ---- context ----------------------------------------------------------------------------
@respx.mock
def test_client_conversation_is_used_when_the_call_names_none(client):
    route = respx.post("https://context.test").mock(
        return_value=httpx.Response(200, json=tool_result({"text": "x"}))
    )
    client.with_conversation("thread-9").context.recall("q")
    assert route.calls[0].request.headers["x-tileward-conversation"] == "thread-9"


@respx.mock
def test_a_per_call_conversation_beats_the_clients(client):
    route = respx.post("https://context.test").mock(
        return_value=httpx.Response(200, json=tool_result({"text": "x"}))
    )
    client.with_conversation("thread-9").context.recall("q", conversation="thread-override")
    assert route.calls[0].request.headers["x-tileward-conversation"] == "thread-override"


@respx.mock
def test_only_tileward_prefixed_tool_names_go_on_the_wire(client):
    """`twinkle` is an internal engine name and must not appear in anything customer-facing."""
    route = respx.post("https://context.test").mock(
        return_value=httpx.Response(200, json=tool_result({}))
    )
    client.context.recall("q")
    client.documents.list()
    for call in route.calls:
        name = json.loads(call.request.content)["params"]["name"]
        assert name.startswith("tileward_"), name


@respx.mock
def test_documents_never_send_a_conversation(client):
    """Documents are account-scoped; offering the scope would imply isolation that is not there."""
    route = respx.post("https://context.test").mock(
        return_value=httpx.Response(200, json=tool_result({"documents": []}))
    )
    client.with_conversation("thread-9").documents.list()
    assert "x-tileward-conversation" not in route.calls[0].request.headers


@respx.mock
def test_recall_text_returns_the_block(client):
    respx.post("https://context.test").mock(
        return_value=httpx.Response(200, json=tool_result({"text": "the slice", "items": 3}))
    )
    assert client.context.recall_text("q") == "the slice"


# ---- documents --------------------------------------------------------------------------
def test_read_file_rejects_a_missing_path(tmp_path):
    with pytest.raises(errors.TilewardError):
        read_file(tmp_path / "nope.txt")


def test_read_file_rejects_something_too_large(tmp_path, monkeypatch):
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 32)
    monkeypatch.setattr("tileward.resources.documents.MAX_FILE_BYTES", 8)
    with pytest.raises(errors.TilewardError) as exc:
        read_file(big)
    assert "limit" in str(exc.value)


def test_read_file_base64s_the_bytes(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("hello")
    out = read_file(path)
    assert out["filename"] == "a.txt"
    assert out["content_b64"] == "aGVsbG8="


def test_the_size_cap_is_a_real_number():
    assert MAX_FILE_BYTES > 0


# ---- keys -------------------------------------------------------------------------------
@respx.mock
def test_keys_list_hides_revoked_by_default(client):
    respx.get("https://console.test/api/account").mock(
        return_value=httpx.Response(
            200,
            json={"keys": [{"id": 1, "revoked": False}, {"id": 2, "revoked": True}]},
        )
    )
    assert [k["id"] for k in client.keys.list()] == [1]
    assert [k["id"] for k in client.keys.list(include_revoked=True)] == [1, 2]


@respx.mock
def test_key_management_uses_the_session_not_the_key(client):
    route = respx.post("https://console.test/api/account/keys").mock(
        return_value=httpx.Response(200, json={"ok": True, "id": 7, "key": "tw_live_new"})
    )
    client.keys.create("laptop")
    request = route.calls[0].request
    assert request.headers["cookie"] == "tw_session=session.token"
    assert "authorization" not in request.headers


# ---- the served catalogue's actual shape ---------------------------------------------------
# `/v1/models` nests everything interesting under a `tileward` object. Reading a top-level
# `context_len` gets None and renders as a blank column, which reads as "served without a context
# limit" rather than "this client looked in the wrong place". Found by running against the live
# endpoint, so these pin the real shape rather than an assumed one.

LIVE_ROW = {
    "id": "tileward-35b-a3b",
    "object": "model",
    "owned_by": "tileward",
    "description": "Tileward 35B-A3B",
    "tileward": {
        "precision": "W4A16 (Tileward)",
        "effective_mb": 24455,
        "compression_ratio": 2.8,
        "context_len": 65536,
        "governance_cells": 215,
        "tier": "small",
        "price_per_mtoken_usd": 1.0,
        "license": None,
        "input_modalities": ["text"],
    },
}


def test_the_nested_block_is_merged_up_for_display():
    from tileward.resources.models import summarize

    flat = summarize(LIVE_ROW)
    assert flat["context_len"] == 65536
    assert flat["price_per_mtoken_usd"] == 1.0
    assert flat["id"] == "tileward-35b-a3b"


def test_identity_fields_are_not_shadowed_by_the_nested_block():
    """`id` and `owned_by` are the model's identity; the catalogue growing a colliding inner key
    must not be able to rename a model in the output."""
    from tileward.resources.models import summarize

    row = dict(LIVE_ROW, tileward=dict(LIVE_ROW["tileward"], id="something-else"))
    assert summarize(row)["id"] == "tileward-35b-a3b"


def test_a_row_with_no_nested_block_still_summarises():
    """Not every served id carries one — a legacy alias has no spec of its own."""
    from tileward.resources.models import summarize

    assert summarize({"id": "tileward-31b", "owned_by": "tileward"})["id"] == "tileward-31b"


@respx.mock
def test_list_returns_the_apis_own_rows_unflattened(client):
    """--json must show what the endpoint returned, not a shape invented for a terminal."""
    respx.get("https://api.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [LIVE_ROW]})
    )
    assert client.models.list()[0]["tileward"]["context_len"] == 65536
