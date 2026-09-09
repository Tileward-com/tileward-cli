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
    respx.post("https://api.test/api/account/keys").mock(
        return_value=httpx.Response(200, json={"ok": True, "id": 3, "key": "tw_live_brandnew"})
    )
    result = run(["keys", "create", "--label", "ci"], env={"TILEWARD_SESSION": "sess"})
    assert result.exit_code == 0
    assert "tw_live_brandnew" in result.output
    assert "only time it is shown" in result.output


@respx.mock
def test_keys_create_save_writes_the_key_to_the_profile(isolated_config):
    respx.post("https://api.test/api/account/keys").mock(
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
