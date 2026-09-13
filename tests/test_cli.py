from __future__ import annotations

import json

import httpx
import respx
from click.testing import CliRunner

from tileward.cli.main import cli


def run(args, env=None):
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
    return runner.invoke(cli, args, env=base, catch_exceptions=False)


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


@respx.mock
def test_chat_prints_the_answer(isolated_config):
    respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": "Hello there."}}]},
        )
    )
    result = run(["chat", "hi", "--no-stream", "-m", "x"])
    assert result.exit_code == 0
    assert "Hello there." in result.output


@respx.mock
def test_a_refused_chat_exits_4(isolated_config):
    respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"finish_reason": "content_filter", "message": {"content": "No."}}]},
        )
    )
    result = run(["chat", "hi", "--no-stream", "-m", "x"])
    assert result.exit_code == 4


@respx.mock
def test_guard_exit_code_flag_reports_a_refusal(isolated_config):
    respx.post("https://api.test/v1/guard").mock(
        return_value=httpx.Response(
            200, json={"tokens": 14, "result": {"allowed": False, "reason": "off_scope"}}
        )
    )
    assert run(["guard", "check", "x", "--allow", "support", "--exit-code"]).exit_code == 4


@respx.mock
def test_guard_without_exit_code_flag_still_succeeds(isolated_config):
    respx.post("https://api.test/v1/guard").mock(
        return_value=httpx.Response(200, json={"tokens": 14, "result": {"allowed": False}})
    )
    assert run(["guard", "check", "x", "--allow", "support"]).exit_code == 0


@respx.mock
def test_guard_sends_the_allow_list_it_was_given(isolated_config):
    route = respx.post("https://api.test/v1/guard").mock(
        return_value=httpx.Response(200, json={"tokens": 1, "result": {"allowed": True}})
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
