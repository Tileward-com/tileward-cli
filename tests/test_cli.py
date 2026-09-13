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
