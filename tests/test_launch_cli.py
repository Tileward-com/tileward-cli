"""`twcli launch` end to end through the click command, with `subprocess.Popen` faked out so
nothing needs `claude`/`codex`/`opencode` installed."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
import respx
from click.testing import CliRunner

from tileward import errors
from tileward.cli.main import cli


class FakePopen:
    """Records the one call `runner.py` makes. Also snapshots a codex profile file's text (if
    `--profile <name>` is in `args`) before `launch_codex` deletes it in `finally`."""

    last_call = None

    def __init__(self, args, env=None, **kwargs):
        args = list(args)
        FakePopen.last_call = {"args": args, "env": dict(env or {})}
        codex_home = (env or {}).get("CODEX_HOME")
        if codex_home and "--profile" in args:
            name = args[args.index("--profile") + 1]
            profile_path = Path(codex_home) / f"{name}.config.toml"
            if profile_path.exists():
                FakePopen.last_call["codex_profile_path"] = profile_path
                FakePopen.last_call["codex_profile_text"] = profile_path.read_text()

    def wait(self):
        return 0


def run(args, env=None, which=None, monkeypatch=None, input=None):
    base = {
        "TILEWARD_BASE_URL": "https://api.test",
        "TILEWARD_CONSOLE_URL": "https://console.test",
        "TILEWARD_CONTEXT_URL": "https://context.test",
        "TILEWARD_API_KEY": "tw_live_testkey",
        # Most of these tests aren't about which model gets picked -- give them an unambiguous
        # default so `serve_models()`'s two rows don't trip the interactive picker. Tests that
        # exercise the picker itself override this back to "" (unset).
        "TILEWARD_MODEL": "tileward-35b-a3b",
        # CliRunner merges env with os.environ, so parent-process Claude Code vars leak in.
        # Delete them explicitly so tests asserting their absence don't flake.
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": None,
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": None,
    }
    base.update(env or {})
    monkeypatch.setattr("tileward.cli.agents.runner.subprocess.Popen", FakePopen)
    monkeypatch.setattr(
        "tileward.cli.agents.runner.shutil.which", lambda name: which or f"/usr/bin/{name}"
    )
    return CliRunner().invoke(cli, args, env=base, input=input, catch_exceptions=False)


def serve_models():
    return respx.get("https://api.test/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "tileward-35b-a3b"}, {"id": "gpt-oss-20b"}]},
        )
    )


@respx.mock
def test_launch_claude_resolves_configured_default_and_wires_the_proxy(
    isolated_config, monkeypatch
):
    serve_models()
    result = run(["launch", "claude"], monkeypatch=monkeypatch)
    assert result.exit_code == 0
    env = FakePopen.last_call["env"]
    assert env["ANTHROPIC_MODEL"] == "tileward-35b-a3b"
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "tileward-35b-a3b"
    assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    assert env["ANTHROPIC_AUTH_TOKEN"]
    assert "ANTHROPIC_API_KEY" not in env
    # compaction hooks ride along as `--settings <json>`; tested on their own below
    assert FakePopen.last_call["args"][0] == "/usr/bin/claude"
    assert FakePopen.last_call["args"][1] == "--settings"
    assert len(FakePopen.last_call["args"]) == 3
    # An isolated config dir, never the user's real ~/.claude -- see runner.py's comment on why:
    # a real `claude login` session otherwise wins over ANTHROPIC_AUTH_TOKEN regardless (verified
    # live, not from docs alone).
    claude_config_dir = Path(env["CLAUDE_CONFIG_DIR"])
    assert claude_config_dir.name == "claude-launch"
    assert claude_config_dir.is_dir()
    assert str(isolated_config) in str(claude_config_dir)


@respx.mock
def test_launch_claude_auto_selects_the_only_served_model(isolated_config, monkeypatch):
    respx.get("https://api.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "solo-model"}]})
    )
    result = run(["launch", "claude"], env={"TILEWARD_MODEL": ""}, monkeypatch=monkeypatch)
    assert result.exit_code == 0
    assert FakePopen.last_call["env"]["ANTHROPIC_MODEL"] == "solo-model"


@respx.mock
def test_launch_claude_with_multiple_models_and_no_default_fails_clearly_when_not_interactive(
    isolated_config, monkeypatch
):
    """No tty to prompt on (a script, CI, a pipe) -- guessing which model to use is exactly the
    trap this whole feature exists to avoid, so this must fail loudly instead of picking one."""
    serve_models()
    with pytest.raises(errors.ConfigError, match="Multiple models"):
        run(["launch", "claude"], env={"TILEWARD_MODEL": ""}, monkeypatch=monkeypatch)


@respx.mock
def test_launch_claude_prompts_to_choose_when_multiple_models_and_no_default(
    isolated_config, monkeypatch
):
    serve_models()
    monkeypatch.setattr("tileward.cli.agents.runner._interactive", lambda ctx: True)
    result = run(
        ["launch", "claude"],
        env={"TILEWARD_MODEL": ""},
        monkeypatch=monkeypatch,
        input="2\n",
    )
    assert result.exit_code == 0
    assert "Available models" in result.output
    assert "tileward-35b-a3b" in result.output
    assert "gpt-oss-20b" in result.output
    assert "Pick a model" in result.output
    # the 2nd row, as typed at the prompt -- not the 1st, which the old code silently always used
    assert FakePopen.last_call["env"]["ANTHROPIC_MODEL"] == "gpt-oss-20b"
    assert "twcli config set model gpt-oss-20b" in result.output


@respx.mock
def test_launch_claude_model_override_is_validated_against_the_catalogue(
    isolated_config, monkeypatch
):
    serve_models()
    with pytest.raises(errors.NotFoundError):
        run(["launch", "claude", "--model", "not-served"], monkeypatch=monkeypatch)


@respx.mock
def test_launch_claude_passes_extra_args_through_to_the_child(isolated_config, monkeypatch):
    serve_models()
    result = run(["launch", "claude", "--", "-p", "hello"], monkeypatch=monkeypatch)
    assert result.exit_code == 0
    args = FakePopen.last_call["args"]
    assert args[0] == "/usr/bin/claude"
    assert args[-2:] == ["-p", "hello"]


def serve_model_with_context(context_len):
    return respx.get("https://api.test/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "tileward-35b-a3b", "tileward": {"context_len": context_len}}]},
        )
    )


@respx.mock
def test_launch_claude_declares_the_served_context_less_the_output_reserve(
    isolated_config, monkeypatch
):
    """Declaring the full 262,144 is the bug this avoids: Claude Code compacts ~24.8k below the
    window it is given, which at 262,144 still lets a prompt past the 230,144 the server accepts
    alongside a 32,000-token output reserve."""
    serve_model_with_context(262144)
    result = run(["launch", "claude"], monkeypatch=monkeypatch)
    assert result.exit_code == 0
    assert FakePopen.last_call["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "230144"
    assert "230,144" in result.output


@respx.mock
def test_launch_claude_window_follows_a_user_set_output_reserve(isolated_config, monkeypatch):
    serve_model_with_context(262144)
    result = run(
        ["launch", "claude"], env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "8192"}, monkeypatch=monkeypatch
    )
    assert result.exit_code == 0
    assert FakePopen.last_call["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == str(262144 - 8192)


@respx.mock
def test_launch_claude_leaves_a_user_set_window_alone(isolated_config, monkeypatch):
    serve_model_with_context(262144)
    result = run(
        ["launch", "claude"],
        env={"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "100000"},
        monkeypatch=monkeypatch,
    )
    assert result.exit_code == 0
    assert FakePopen.last_call["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "100000"


@respx.mock
def test_launch_claude_declares_no_window_a_model_cannot_hold(isolated_config, monkeypatch):
    serve_model_with_context(8192)
    result = run(["launch", "claude"], monkeypatch=monkeypatch)
    assert result.exit_code == 0
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in FakePopen.last_call["env"]
    assert "output reserve" in result.output


@respx.mock
def test_launch_claude_declares_no_window_when_the_catalogue_reports_none(
    isolated_config, monkeypatch
):
    serve_models()
    result = run(["launch", "claude"], monkeypatch=monkeypatch)
    assert result.exit_code == 0
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in FakePopen.last_call["env"]


@respx.mock
def test_launch_claude_registers_the_compaction_hooks_as_settings(isolated_config, monkeypatch):
    serve_models()
    result = run(["launch", "claude"], monkeypatch=monkeypatch)
    assert result.exit_code == 0
    args = FakePopen.last_call["args"]
    settings = json.loads(args[args.index("--settings") + 1])
    hooks = settings["hooks"]
    assert set(hooks) == {"PreCompact", "PostCompact", "SessionStart"}
    assert hooks["SessionStart"][0]["matcher"] == "compact"
    # no matcher: fire on manual /compact as well as auto-compaction
    assert "matcher" not in hooks["PreCompact"][0]
    assert "matcher" not in hooks["PostCompact"][0]
    command = hooks["PostCompact"][0]["hooks"][0]["command"]
    assert "claude_hook.py" in command
    assert str(isolated_config) in command


@respx.mock
def test_launch_claude_no_compaction_hooks_passes_no_settings(isolated_config, monkeypatch):
    serve_models()
    result = run(["launch", "claude", "--no-compaction-hooks"], monkeypatch=monkeypatch)
    assert result.exit_code == 0
    assert "--settings" not in FakePopen.last_call["args"]


@pytest.mark.parametrize(
    "own", [["--settings", "mine.json"], ["--settings=mine.json"]], ids=["spaced", "equals"]
)
@respx.mock
def test_launch_claude_yields_to_the_users_own_settings(isolated_config, monkeypatch, own):
    """Claude Code keeps only the last --settings (measured), so adding ours would silently drop
    one of the two. The user's wins, and the run says the hooks are off."""
    serve_models()
    result = run(["launch", "claude", "--", *own], monkeypatch=monkeypatch)
    assert result.exit_code == 0
    assert FakePopen.last_call["args"] == ["/usr/bin/claude", *own]
    assert "compaction hooks are off" in result.output


def test_launch_claude_missing_binary_is_a_clear_error(isolated_config, monkeypatch):
    monkeypatch.setenv("TILEWARD_API_KEY", "tw_live_testkey")
    monkeypatch.setenv("TILEWARD_BASE_URL", "https://api.test")
    monkeypatch.setattr("tileward.cli.agents.runner.shutil.which", lambda name: None)
    with respx.mock:
        respx.get("https://api.test/v1/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "x"}]})
        )
        result = CliRunner().invoke(cli, ["launch", "claude"])
    assert result.exit_code != 0
    assert "claude" in result.output
    assert "not found on PATH" in result.output


@respx.mock
def test_launch_codex_writes_provider_and_profile_and_passes_the_flag(
    isolated_config, monkeypatch, tmp_path
):
    serve_models()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    result = run(["launch", "codex", "--model", "gpt-oss-20b"], monkeypatch=monkeypatch)
    assert result.exit_code == 0
    args = FakePopen.last_call["args"]
    assert args[0] == "/usr/bin/codex"
    assert args[1] == "--profile"
    profile_name = args[2]
    assert profile_name.startswith("tileward-")  # per-process, not a fixed name -- see below

    # A dedicated profile file, per `codex --profile`'s real behavior (confirmed against a live
    # 0.154.0 install: it layers $CODEX_HOME/<name>.config.toml, not a [profiles.x] table inside
    # config.toml) -- so the user's own config.toml is never touched at all.
    config = FakePopen.last_call["codex_profile_text"]
    assert f"[model_providers.{profile_name}]" in config
    assert f'model_provider = "{profile_name}"' in config
    assert 'model = "gpt-oss-20b"' in config
    assert "wire_api = \"responses\"" in config
    assert not (tmp_path / "codex-home" / "config.toml").exists()

    env = FakePopen.last_call["env"]
    assert env["TILEWARD_PROXY_TOKEN"]


@respx.mock
def test_launch_codex_profile_name_is_per_process_not_fixed(isolated_config, monkeypatch, tmp_path):
    """A fixed name would let two concurrent sessions race on the same file; pid-keying avoids
    it (same process here, so the same name both calls)."""
    serve_models()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    result = run(["launch", "codex"], monkeypatch=monkeypatch)
    assert result.exit_code == 0
    first_name = FakePopen.last_call["args"][2]

    result = run(["launch", "codex"], monkeypatch=monkeypatch)
    assert result.exit_code == 0
    second_name = FakePopen.last_call["args"][2]

    assert first_name == second_name == f"tileward-{os.getpid()}"


@respx.mock
def test_launch_codex_deletes_its_profile_file_once_the_child_exits(
    isolated_config, monkeypatch, tmp_path
):
    serve_models()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    run(["launch", "codex"], monkeypatch=monkeypatch)
    assert list((tmp_path / "codex-home").glob("tileward-*.config.toml")) == []


@respx.mock
def test_launch_opencode_writes_provider_config_with_the_real_key_only_in_env(
    isolated_config, monkeypatch, tmp_path
):
    serve_models()
    config_path = tmp_path / "opencode.json"
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    result = run(["launch", "opencode"], monkeypatch=monkeypatch)
    assert result.exit_code == 0

    doc = json.loads(config_path.read_text())
    provider = doc["provider"]["tileward"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "https://api.test/v1"
    assert provider["options"]["apiKey"] == "{env:TILEWARD_API_KEY}"
    assert set(provider["models"]) == {"tileward-35b-a3b", "gpt-oss-20b"}
    assert "tw_live_testkey" not in config_path.read_text()  # the real key never touches disk

    assert FakePopen.last_call["env"]["TILEWARD_API_KEY"] == "tw_live_testkey"


@respx.mock
def test_launch_opencode_leaves_unparsable_existing_config_untouched(
    isolated_config, monkeypatch, tmp_path
):
    serve_models()
    config_path = tmp_path / "opencode.json"
    config_path.write_text("{ // a comment opencode allows and json.loads does not\n}")
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    result = run(["launch", "opencode"], monkeypatch=monkeypatch)
    assert result.exit_code == 0
    assert "// a comment" in config_path.read_text()  # untouched
    assert "isn't plain JSON" in result.output


class FakeDesktopPopen:
    """Records calls to `open -a ai.opencode.desktop` for desktop launch tests."""

    last_call = None

    def __init__(self, cmd, env=None, **kwargs):
        FakeDesktopPopen.last_call = {"cmd": list(cmd), "env": dict(env or {}), "kwargs": kwargs}

    def wait(self):
        return 0


def run_desktop(args, env=None, monkeypatch=None):
    base = {
        "TILEWARD_BASE_URL": "https://api.test",
        "TILEWARD_CONSOLE_URL": "https://console.test",
        "TILEWARD_CONTEXT_URL": "https://context.test",
        "TILEWARD_API_KEY": "tw_live_testkey",
        "TILEWARD_MODEL": "tileward-35b-a3b",
    }
    base.update(env or {})
    monkeypatch.setattr("tileward.cli.agents.runner.subprocess.Popen", FakeDesktopPopen)
    monkeypatch.setattr(
        "tileward.cli.agents.runner.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    return CliRunner().invoke(cli, args, env=base, catch_exceptions=False)


@respx.mock
def test_launch_opencode_desktop_merges_config_and_launches_desktop(
    isolated_config, monkeypatch, tmp_path
):
    serve_models()
    config_path = tmp_path / "opencode.json"
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.setattr("tileward.cli.agents.runner.sys.platform", "darwin")
    result = run_desktop(["launch", "opencode", "--desktop"], monkeypatch=monkeypatch)
    assert result.exit_code == 0

    doc = json.loads(config_path.read_text())
    provider = doc["provider"]["tileward"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "https://api.test/v1"
    assert provider["options"]["apiKey"] == "{env:TILEWARD_API_KEY}"

    assert FakeDesktopPopen.last_call["cmd"] == ["open", "-a", "ai.opencode.desktop"]
    assert FakeDesktopPopen.last_call["env"]["TILEWARD_API_KEY"] == "tw_live_testkey"


@respx.mock
def test_launch_opencode_desktop_non_macos_raises_error(
    isolated_config, monkeypatch, tmp_path
):
    serve_models()
    config_path = tmp_path / "opencode.json"
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.setattr("tileward.cli.agents.runner.sys.platform", "linux")
    result = run_desktop(["launch", "opencode", "--desktop"], monkeypatch=monkeypatch)
    assert result.exit_code != 0
    assert "macOS-only" in result.output
