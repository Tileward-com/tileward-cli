from __future__ import annotations

import json

import httpx
import respx
from click.testing import CliRunner

from tileward.cli.main import cli


def run(args, env=None, stdin=None):
    runner = CliRunner()
    base = {
        "TILEWARD_BASE_URL": "https://api.test",
        # Distinct from the api host, because they are distinct in production: the session
        # surface is console-only and the api host 404s it.
        "TILEWARD_CONSOLE_URL": "https://console.test",
        "TILEWARD_CONTEXT_URL": "https://context.test",
        "TILEWARD_API_KEY": "tw_live_testkey",
    }
    base.update(env or {})
    return runner.invoke(cli, args, env=base, input=stdin, catch_exceptions=False)


def tool_result(value):
    return {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"text": json.dumps(value)}]}}


def test_help_works_with_no_credentials_at_all(isolated_config, monkeypatch):
    monkeypatch.delenv("TILEWARD_API_KEY", raising=False)
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "models" in result.output


def test_config_show_needs_no_network(isolated_config):
    result = run(["config", "show", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["profile"] == "default"


def test_config_show_redacts_the_key_by_default(isolated_config):
    result = run(["config", "show", "--json"])
    assert "tw_live_testkey" not in result.output
    assert json.loads(result.output)["api_key"].startswith("tw_live_…")


@respx.mock
def test_models_list_json_is_the_only_thing_on_stdout(isolated_config):
    respx.get("https://api.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})
    )
    result = run(["models", "list", "--json"])
    assert result.exit_code == 0
    assert [row["id"] for row in json.loads(result.output)] == ["a", "b"]


@respx.mock
def test_models_list_renders_a_table_without_json(isolated_config):
    respx.get("https://api.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "tileward-35b-a3b"}]})
    )
    result = run(["models", "list"])
    assert "tileward-35b-a3b" in result.output


CHAT = "https://api.test/v1/chat/completions"


def serve_models(context_len=8192):
    """The catalogue, stating a window for model `x` — what chat sizes its requests against."""
    return respx.get("https://api.test/v1/models").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "x", "tileward": {"context_len": context_len}}]}
        )
    )


def answer(text, finish="stop"):
    return httpx.Response(
        200, json={"choices": [{"finish_reason": finish, "message": {"content": text}}]}
    )


def streamed(text, finish="stop", usage=None):
    closing = {"choices": [{"delta": {}, "finish_reason": finish}]}
    if usage:
        closing["usage"] = usage
    chunks = [{"choices": [{"delta": {"content": text}}]}, closing]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def too_long():
    """The refusal the API sends when a request asks for more than the window leaves.

    Its "at least N" is the room the ask left plus one, never the prompt's length, so nothing in it
    can size a retry.
    """
    message = (
        "This model's maximum context length is 8192 tokens. However, you requested 7926 output "
        "tokens and your prompt contains at least 267 input tokens, for a total of at least 8193 "
        "tokens. (parameter=input_tokens, value=267)"
    )
    return httpx.Response(
        400, json={"error": {"message": message, "code": "context_length_exceeded"}}
    )


def sent(route, index=-1):
    return json.loads(route.calls[index].request.content)


@respx.mock
def test_chat_prints_the_answer(isolated_config):
    serve_models()
    respx.post(CHAT).mock(return_value=answer("Hello there."))
    result = run(["chat", "hi", "--no-stream", "-m", "x"])
    assert result.exit_code == 0
    assert "Hello there." in result.output


@respx.mock
def test_a_refused_chat_exits_4(isolated_config):
    serve_models()
    respx.post(CHAT).mock(return_value=answer("No.", finish="content_filter"))
    result = run(["chat", "hi", "--no-stream", "-m", "x"])
    assert result.exit_code == 4


@respx.mock
def test_chat_asks_for_everything_the_window_leaves(isolated_config):
    # Unset, max_tokens falls to the gateway's short default and a long answer stops mid-sentence.
    serve_models(context_len=8192)
    route = respx.post(CHAT).mock(return_value=answer("ok"))
    run(["chat", "hi", "--no-stream", "-m", "x"])
    assert 8192 - 300 < sent(route)["max_tokens"] < 8192


@respx.mock
def test_a_refused_ask_is_halved_and_sent_again(isolated_config):
    serve_models(context_len=8192)
    route = respx.post(CHAT).mock(side_effect=[too_long(), answer("Fits now.")])
    result = run(["chat", "hi", "--no-stream", "-m", "x"])
    assert result.exit_code == 0
    assert "Fits now." in result.output
    assert sent(route, 1)["max_tokens"] == sent(route, 0)["max_tokens"] // 2


@respx.mock
def test_a_streamed_chat_retries_before_printing_anything(isolated_config):
    serve_models(context_len=8192)
    route = respx.post(CHAT).mock(side_effect=[too_long(), streamed("Fits now.")])
    result = run(["chat", "hi", "--stream", "-m", "x"])
    assert result.exit_code == 0
    assert result.output.count("Fits now.") == 1
    assert sent(route, 1)["max_tokens"] == sent(route, 0)["max_tokens"] // 2


@respx.mock
def test_chat_lets_the_refusal_stand_after_four_halvings(isolated_config, monkeypatch):
    monkeypatch.setenv("TILEWARD_BASE_URL", "https://api.test")
    monkeypatch.setenv("TILEWARD_API_KEY", "tw_live_testkey")
    serve_models(context_len=8192)
    route = respx.post(CHAT).mock(return_value=too_long())
    from tileward.cli.main import main

    assert main(["chat", "hi", "--no-stream", "-m", "x"]) == 1
    asks = [json.loads(call.request.content)["max_tokens"] for call in route.calls]
    assert asks == [asks[0] >> n for n in range(5)]


@respx.mock
def test_chat_sends_max_tokens_as_given_and_skips_the_catalogue(isolated_config):
    catalogue = serve_models()
    route = respx.post(CHAT).mock(return_value=answer("ok"))
    run(["chat", "hi", "--no-stream", "-m", "x", "--max-tokens", "200"])
    assert sent(route)["max_tokens"] == 200
    assert not catalogue.called


@respx.mock
def test_chat_leaves_max_tokens_unset_when_the_catalogue_states_no_window(isolated_config):
    respx.get("https://api.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "x"}]})
    )
    route = respx.post(CHAT).mock(return_value=answer("ok"))
    run(["chat", "hi", "--no-stream", "-m", "x"])
    assert "max_tokens" not in sent(route)


@respx.mock
def test_chat_with_no_prompt_on_a_terminal_starts_a_conversation(isolated_config, monkeypatch):
    from tileward.cli.commands import chat as chat_command

    monkeypatch.setattr(chat_command, "_interactive_terminal", lambda: True)
    serve_models(context_len=262144)
    result = run(["chat", "-m", "x"], env={"COLUMNS": "200"}, stdin="/exit\n")
    assert result.exit_code == 0
    assert "262,144-token window" in result.output


@respx.mock
def test_the_conversation_carries_the_transcript_forward(isolated_config):
    serve_models()
    route = respx.post(CHAT).mock(side_effect=[streamed("Paris."), streamed("About two million.")])
    result = run(["chat", "-i", "-m", "x"], stdin="Capital of France?\nIts population?\n/exit\n")
    assert result.exit_code == 0
    assert "About two million." in result.output
    contents = [m["content"] for m in sent(route)["messages"]]
    assert contents == ["Capital of France?", "Paris.", "Its population?"]


@respx.mock
def test_the_conversation_sizes_the_next_ask_from_reported_usage(isolated_config):
    serve_models(context_len=8192)
    used = {"prompt_tokens": 3000, "completion_tokens": 1000}
    route = respx.post(CHAT).mock(side_effect=[streamed("A.", usage=used), streamed("B.")])
    run(["chat", "-i", "-m", "x"], stdin="one\ntwo\n/exit\n")
    assert 8192 - 4000 - 300 < sent(route)["max_tokens"] < 8192 - 4000


@respx.mock
def test_the_conversation_survives_an_error_and_drops_that_turn(isolated_config):
    serve_models()
    bad = httpx.Response(400, json={"error": {"message": "malformed", "code": "invalid_request"}})
    route = respx.post(CHAT).mock(side_effect=[bad, streamed("Recovered.")])
    result = run(["chat", "-i", "-m", "x"], stdin="first\nsecond\n/exit\n")
    assert result.exit_code == 0
    assert "malformed" in result.output
    assert "Recovered." in result.output
    assert [m["content"] for m in sent(route)["messages"]] == ["second"]


@respx.mock
def test_the_conversation_does_not_keep_a_refused_turn(isolated_config):
    serve_models()
    refused = streamed("Not on this key.", finish="content_filter")
    route = respx.post(CHAT).mock(side_effect=[refused, streamed("Sure.")])
    result = run(["chat", "-i", "-m", "x"], stdin="off topic\non topic\n/exit\n")
    assert "Refused by governance" in result.output
    assert [m["content"] for m in sent(route)["messages"]] == ["on topic"]


@respx.mock
def test_ctrl_c_stops_the_answer_but_not_the_conversation(isolated_config, monkeypatch):
    from tileward.resources.chat import Completions

    def interrupted(self, messages, **kwargs):
        yield {"choices": [{"delta": {"content": "Once upon"}}]}
        raise KeyboardInterrupt

    monkeypatch.setattr(Completions, "create", interrupted)
    serve_models()
    result = run(["chat", "-i", "-m", "x"], stdin="a story\n/exit\n")
    assert result.exit_code == 0
    assert "Once upon" in result.output
    assert "Stopped." in result.output


@respx.mock
def test_remember_in_a_conversation_recalls_before_answering(isolated_config):
    serve_models()
    respx.post("https://context.test").mock(
        return_value=httpx.Response(200, json=tool_result({"text": "prefers metric units"}))
    )
    route = respx.post(CHAT).mock(return_value=streamed("About 330 m."))
    run(["-c", "t1", "chat", "-i", "-m", "x", "--remember"], stdin="How tall is it?\n/exit\n")
    system = sent(route)["messages"][0]
    assert system["role"] == "system"
    assert "prefers metric units" in system["content"]


@respx.mock
def test_each_conversation_is_a_thread_of_its_own_on_the_gateway(isolated_config):
    # Two sessions that open with the same line must still keep separate histories.
    serve_models()
    route = respx.post(CHAT).mock(side_effect=[streamed("a"), streamed("b"), streamed("c")])
    run(["chat", "-i", "-m", "x"], stdin="hi\nagain\n/exit\n")
    run(["chat", "-i", "-m", "x"], stdin="hi\n/exit\n")
    threads = [call.request.headers["x-tileward-conversation"] for call in route.calls]
    assert threads[0] == threads[1]
    assert threads[2] != threads[0]


@respx.mock
def test_guard_exit_code_flag_reports_a_refusal(isolated_config):
    respx.post("https://api.test/v1/guard").mock(
        return_value=httpx.Response(200, json={"cost_micros": 3.69, "result": {"allowed": False}})
    )
    assert run(["guard", "check", "x", "--allow", "support", "--exit-code"]).exit_code == 4


@respx.mock
def test_guard_without_exit_code_flag_still_succeeds(isolated_config):
    respx.post("https://api.test/v1/guard").mock(
        return_value=httpx.Response(200, json={"cost_micros": 3.69, "result": {"allowed": False}})
    )
    assert run(["guard", "check", "x", "--allow", "support"]).exit_code == 0


@respx.mock
def test_guard_sends_the_allow_list_it_was_given(isolated_config):
    route = respx.post("https://api.test/v1/guard").mock(
        return_value=httpx.Response(200, json={"cost_micros": 0.26, "result": {"allowed": True}})
    )
    run(["guard", "check", "x", "--allow", "a, b"])
    assert json.loads(route.calls[0].request.content)["allow"] == ["a", "b"]


@respx.mock
def test_context_recall_scopes_to_the_conversation_flag(isolated_config):
    route = respx.post("https://context.test").mock(
        return_value=httpx.Response(200, json=tool_result({"text": "the slice"}))
    )
    result = run(["-c", "thread-7", "context", "recall", "q"])
    assert "the slice" in result.output
    assert route.calls[0].request.headers["x-tileward-conversation"] == "thread-7"


@respx.mock
def test_remember_without_a_conversation_warns_about_the_shared_store(isolated_config):
    respx.post("https://context.test").mock(
        return_value=httpx.Response(200, json=tool_result({"stored": True}))
    )
    result = run(["context", "remember", "a fact"])
    assert "shared default store" in result.output


@respx.mock
def test_docs_ls_lists_documents(isolated_config):
    respx.post("https://context.test").mock(
        return_value=httpx.Response(
            200,
            json=tool_result({"documents": [{"source_id": "src_1", "title": "Handbook"}]}),
        )
    )
    result = run(["docs", "ls"])
    assert "Handbook" in result.output


@respx.mock
def test_docs_search_filters_the_list_unless_asked_to_read_the_text(isolated_config):
    route = respx.post("https://context.test").mock(
        side_effect=lambda request: httpx.Response(
            200, json=tool_result({"documents": [], "text": ""})
        )
    )
    run(["docs", "search", "leave"])
    run(["docs", "search", "leave", "--content"])
    names = [json.loads(call.request.content)["params"]["name"] for call in route.calls]
    assert names == ["tileward_list_documents", "tileward_recall"]


def test_docs_search_help_says_it_matches_titles_and_tags_not_the_text(isolated_config):
    listing = " ".join(run(["docs", "--help"]).output.split())
    detail = " ".join(run(["docs", "search", "--help"]).output.split())
    assert "Filter the document list" in listing
    assert "does not read what the documents say" in detail


def test_keys_without_a_session_says_to_log_in(isolated_config):
    result = run(["keys", "list"])
    assert result.exit_code != 0
    assert "auth login" in result.output


@respx.mock
def test_keys_create_prints_the_secret_once(isolated_config):
    respx.post("https://console.test/api/account/keys").mock(
        return_value=httpx.Response(200, json={"ok": True, "id": 3, "key": "tw_live_brandnew"})
    )
    result = run(["keys", "create", "--label", "ci"], env={"TILEWARD_SESSION": "sess"})
    assert result.exit_code == 0
    assert "tw_live_brandnew" in result.output
    assert "only time it is shown" in result.output


@respx.mock
def test_keys_create_save_writes_the_key_to_the_profile(isolated_config):
    respx.post("https://console.test/api/account/keys").mock(
        return_value=httpx.Response(200, json={"ok": True, "id": 3, "key": "tw_live_saved"})
    )
    run(["keys", "create", "--label", "ci", "--save"], env={"TILEWARD_SESSION": "sess"})
    stored = json.loads((isolated_config / "credentials.json").read_text())
    assert stored["profiles"]["default"]["api_key"] == "tw_live_saved"


@respx.mock
def test_a_402_exits_5_so_ci_can_tell_it_from_a_network_failure(isolated_config, monkeypatch):
    monkeypatch.setenv("TILEWARD_BASE_URL", "https://api.test")
    monkeypatch.setenv("TILEWARD_API_KEY", "tw_live_testkey")
    serve_models()
    respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(402, json={"error": {"message": "no funds"}})
    )
    from tileward.cli.main import main

    assert main(["chat", "hi", "--no-stream", "-m", "x"]) == 5


@respx.mock
def test_an_auth_failure_exits_3(isolated_config, monkeypatch):
    monkeypatch.setenv("TILEWARD_BASE_URL", "https://api.test")
    monkeypatch.setenv("TILEWARD_API_KEY", "tw_live_bad")
    respx.get("https://api.test/v1/models").mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    from tileward.cli.main import main

    assert main(["models", "list"]) == 3


def test_bad_usage_exits_2(isolated_config):
    from tileward.cli.main import main

    assert main(["models", "nosuchcommand"]) == 2


# The payloads below carry the field names the console endpoints actually return. These tables were
# first written against guessed names (`prefix`, `day`, `decision`, `updated`), which rendered as
# columns of dashes — and epoch floats as `1,789,002,488.0501` — while every test here passed.


def local_minute(ts):
    from datetime import datetime

    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def session_run(args):
    # Wide enough that rich never wraps a timestamp across two lines.
    return run(args, env={"TILEWARD_SESSION": "sess", "COLUMNS": "200"})


@respx.mock
def test_keys_list_shows_the_prefix_and_dates_not_epoch_floats(isolated_config):
    created, used = 1789002488.0500658, 1789004659.1518748
    respx.get("https://console.test/api/account").mock(
        return_value=httpx.Response(
            200,
            json={
                "keys": [
                    {"id": 7, "label": "laptop", "key_prefix": "tw_live_ab12cd", "cells": [],
                     "allow": [], "created": created, "last_used": used, "revoked": False,
                     "deleted_ts": None},
                    {"id": 8, "label": "ci", "key_prefix": "tw_live_ef34ab", "cells": ["billing"],
                     "allow": [], "created": created, "last_used": None, "revoked": False,
                     "deleted_ts": None},
                ]
            },
        )
    )
    result = session_run(["keys", "list"])
    assert result.exit_code == 0
    assert "tw_live_ab12cd" in result.output
    assert local_minute(created) in result.output
    assert local_minute(used) in result.output
    assert "1,789," not in result.output


@respx.mock
def test_account_usage_reads_by_day_and_prints_dollars_not_micros(isolated_config):
    respx.get("https://console.test/api/account").mock(
        return_value=httpx.Response(
            200, json={"by_day": [{"d": "2026-09-12", "tok": 1260642, "cost": 1191792.0}]}
        )
    )
    result = session_run(["account", "usage"])
    assert result.exit_code == 0
    assert "2026-09-12" in result.output
    assert "1,260,642" in result.output
    assert "1.1918" in result.output
    assert "—" not in result.output


@respx.mock
def test_account_audit_shows_the_outcome_and_total_tokens(isolated_config):
    ts = 1789245490.0385704
    respx.get("https://console.test/api/account/audit").mock(
        return_value=httpx.Response(
            200,
            json={
                "summary": [],
                "total": 1,
                "shown_max": 50,
                "rows": [
                    {"ts": ts, "model": "tileward-35b-a3b", "outcome": "allowed", "key_id": 7,
                     "total_tokens": 3444, "request_id": "req_1"},
                ],
            },
        )
    )
    result = session_run(["account", "audit"])
    assert result.exit_code == 0
    assert "allowed" in result.output
    assert "3,444" in result.output
    assert local_minute(ts) in result.output


@respx.mock
def test_account_audit_filters_by_key_and_outcome(isolated_config):
    route = respx.get("https://console.test/api/account/audit").mock(
        return_value=httpx.Response(200, json={"summary": [], "total": 0, "rows": []})
    )
    result = session_run(["account", "audit", "--key-id", "7", "--outcome", "rejected"])
    assert result.exit_code == 0
    assert dict(route.calls[0].request.url.params) == {"key_id": "7", "outcome": "rejected"}


@respx.mock
def test_context_threads_shows_the_conversation_tokens_and_last_activity(isolated_config):
    last = 1789248288.580418
    respx.get("https://console.test/api/context/threads").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "threads": [
                    {"conv": "thread-99", "title": "Pricing review", "turns": 10,
                     "used_tokens": 3444, "saved_tokens": 238, "first_ts": last - 7000,
                     "last_ts": last},
                ],
            },
        )
    )
    result = session_run(["context", "threads"])
    assert result.exit_code == 0
    assert "thread-99" in result.output
    assert "3,444" in result.output
    assert local_minute(last) in result.output


def savings_payload(**changes):
    payload = {
        "available": True, "window": "30d", "window_hours": 720, "stale": False,
        "snapshot_age_hours": 0.2, "snapshot_writer_stalled": False,
        "saved": 38174185, "spent": 7175956, "reduction_pct": 84.2,
        "weekly": 6875782460, "lifetime": 76367648, "lifetime_reduction_pct": 86.3,
        "by_conversation": [
            {"conversation": "b228754b-44d6-49b4-8033-fa4447566847", "title": "Release checklist",
             "client": "claude-code", "saved": 9075288, "spent": 539071, "reduction_pct": 94.4},
        ],
        "series": [{"label": "09-12", "saved": 2040773359, "spent": 629860}],
        "granularity": "day",
    }
    payload.update(changes)
    return payload


def serve_savings(payload, daily=None):
    respx.get("https://console.test/api/account/twinkle-savings").mock(
        return_value=httpx.Response(200, json=payload)
    )
    return respx.get("https://console.test/api/account/context/heatmap").mock(
        return_value=httpx.Response(200, json=daily or {"available": False})
    )


@respx.mock
def test_account_savings_prints_tables_not_the_raw_payload(isolated_config):
    import time

    now = time.time()
    serve_savings(savings_payload(), daily={"available": True, "series": [
        {"label": "2026-07-01", "ts": now - 60 * 86400, "tokens": 111},
        {"label": "2026-09-12", "ts": now - 86400, "tokens": 4046561},
    ]})
    result = session_run(["account", "savings", "--detail"])
    assert result.exit_code == 0
    assert "84.2%" in result.output
    assert "Release checklist" in result.output
    assert "4,046,561" in result.output
    assert "2026-07-01" not in result.output
    assert "{'" not in result.output
    # The day table comes from the daily endpoint; the payload's own `weekly` and `series` are
    # not printed.
    assert "6,875,782,460" not in result.output
    assert "2,040,773,359" not in result.output


@respx.mock
def test_account_savings_says_when_it_is_unavailable(isolated_config):
    daily = serve_savings({"available": False})
    result = session_run(["account", "savings"])
    assert result.exit_code == 0
    assert "not available" in result.output
    assert not daily.called


@respx.mock
def test_account_savings_json_is_the_payload_as_sent(isolated_config):
    daily = serve_savings(savings_payload())
    result = session_run(["account", "savings", "--json"])
    assert json.loads(result.output)["series"][0]["saved"] == 2040773359
    assert not daily.called


@respx.mock
def test_account_savings_warns_when_the_snapshot_is_stale(isolated_config):
    serve_savings(savings_payload(stale=True, snapshot_age_hours=30.0))
    result = session_run(["account", "savings"])
    assert "30.0 hours old" in result.output


@respx.mock
def test_account_savings_shows_only_the_summary_by_default(isolated_config):
    daily = serve_savings(savings_payload())
    result = session_run(["account", "savings"])
    assert result.exit_code == 0
    assert "84.2%" in result.output
    assert "by conversation" not in result.output
    assert not daily.called


@respx.mock
def test_account_savings_conversation_flag_adds_only_that_table(isolated_config):
    daily = serve_savings(savings_payload())
    result = session_run(["account", "savings", "--conversation"])
    assert "Release checklist" in result.output
    assert "by day" not in result.output
    assert not daily.called


@respx.mock
def test_account_savings_asks_for_a_window_by_the_name_the_api_uses(isolated_config):
    route = respx.get("https://console.test/api/account/twinkle-savings").mock(
        return_value=httpx.Response(200, json=savings_payload(window="24h", window_hours=24))
    )
    result = session_run(["account", "savings", "1d"])
    assert result.exit_code == 0
    assert route.calls[0].request.url.params["window"] == "24h"


@respx.mock
def test_account_savings_refuses_a_window_the_api_replaced(isolated_config):
    # Asked for five weeks, the API answers with its 30-day default.
    serve_savings(savings_payload())
    result = session_run(["account", "savings", "5w"])
    assert result.exit_code == 2
    assert "did not measure a 5w window; it answered for 30d" in result.output


@respx.mock
def test_account_savings_rejects_a_window_it_cannot_read(isolated_config):
    result = session_run(["account", "savings", "fortnight"])
    assert result.exit_code == 2
    assert "number and a unit" in result.output


def signed_in_without_a_key():
    return {"TILEWARD_API_KEY": None, "TILEWARD_SESSION": "sess"}


def serve_key(key="tw_live_minted"):
    return respx.post("https://console.test/api/account/keys").mock(
        return_value=httpx.Response(200, json={"ok": True, "id": 91, "key": key})
    )


@respx.mock
def test_a_signed_in_profile_gets_an_api_key_the_first_time_one_is_needed(isolated_config):
    minted = serve_key()
    serve_models()
    route = respx.post(CHAT).mock(return_value=answer("Hello."))
    result = run(["chat", "hi", "--no-stream", "-m", "x"], env=signed_in_without_a_key())
    assert result.exit_code == 0
    assert "Hello." in result.output
    assert minted.call_count == 1
    assert json.loads(minted.calls[0].request.content)["label"].startswith("twcli")
    assert route.calls[0].request.headers["authorization"] == "Bearer tw_live_minted"
    stored = json.loads((isolated_config / "credentials.json").read_text())
    assert stored["profiles"]["default"]["api_key"] == "tw_live_minted"


@respx.mock
def test_context_and_chat_share_the_one_key_created(isolated_config):
    minted = serve_key()
    serve_models()
    respx.post("https://context.test").mock(
        return_value=httpx.Response(200, json=tool_result({"text": "earlier"}))
    )
    respx.post(CHAT).mock(return_value=answer("Hello."))
    result = run(
        ["-c", "t1", "chat", "hi", "--no-stream", "-m", "x", "--remember"],
        env=signed_in_without_a_key(),
    )
    assert result.exit_code == 0
    assert minted.call_count == 1


@respx.mock
def test_without_a_session_a_missing_api_key_is_still_an_error(isolated_config, monkeypatch):
    monkeypatch.setenv("TILEWARD_BASE_URL", "https://api.test")
    from tileward.cli.main import main

    assert main(["models", "list"]) == 3


@respx.mock
def test_a_key_that_cannot_be_created_says_how_to_store_one(isolated_config, monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("TILEWARD_BASE_URL", "https://api.test")
    monkeypatch.setenv("TILEWARD_CONSOLE_URL", "https://console.test")
    monkeypatch.setenv("TILEWARD_SESSION", "sess")
    respx.post("https://console.test/api/account/keys").mock(
        return_value=httpx.Response(403, json={"error": {"message": "key limit reached"}})
    )
    from tileward.cli.main import main

    assert main(["models", "list"]) == 3
    assert "twcli config set-key" in capsys.readouterr().err
