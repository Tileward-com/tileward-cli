"""The compaction hook handler, run the way Claude Code runs it: as a separate process.

Its two hard rules come from measured Claude Code behaviour, not preference: `PreCompact` stdout is
appended to the summarizer's instructions and `SessionStart` stdout reaches the model, and exit code
2 from `PreCompact` blocks compaction, which ends the turn. So every case checks stdout is empty and
the exit code is 0.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tileward.cli.agents import claude_config

SUMMARY = "<summary>the agent edited catalog.py and ran the tests</summary>"


def run_handler(stdin: str, *args: str):
    # -I: isolated mode, no PYTHONPATH and no user site. The handler must run on the standard
    # library alone, since Claude Code starts it outside the package.
    return subprocess.run(
        [sys.executable, "-I", str(claude_config.HANDLER), *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
    )


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_post_compact_logs_sizes_and_never_the_summary(tmp_path):
    log = tmp_path / "compactions.jsonl"
    payload = {
        "hook_event_name": "PostCompact",
        "trigger": "auto",
        "session_id": "s1",
        "transcript_path": "/t.jsonl",
        "cwd": "/repo",
        "compact_summary": SUMMARY,
    }
    proc = run_handler(json.dumps(payload), str(log))
    assert (proc.returncode, proc.stdout) == (0, "")
    [row] = lines(log)
    assert row["hook_event_name"] == "PostCompact"
    assert row["trigger"] == "auto"
    assert row["transcript_path"] == "/t.jsonl"
    assert row["summary_chars"] == len(SUMMARY)
    assert "catalog.py" not in log.read_text()


@pytest.mark.parametrize(
    "payload",
    [
        {"hook_event_name": "PreCompact", "trigger": "manual", "custom_instructions": "keep paths"},
        {"hook_event_name": "SessionStart", "source": "compact", "session_id": "s1"},
    ],
    ids=["pre-compact", "session-start"],
)
def test_events_whose_stdout_reaches_the_model_print_nothing(tmp_path, payload):
    log = tmp_path / "compactions.jsonl"
    proc = run_handler(json.dumps(payload), str(log))
    assert (proc.returncode, proc.stdout) == (0, "")
    [row] = lines(log)
    assert row["hook_event_name"] == payload["hook_event_name"]
    assert "keep paths" not in log.read_text()


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]"], ids=["empty", "garbage", "list"])
def test_unreadable_input_is_silent_and_exits_zero(tmp_path, stdin):
    log = tmp_path / "compactions.jsonl"
    proc = run_handler(stdin, str(log))
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    assert not log.exists()


def test_an_unwritable_log_is_silent_and_exits_zero(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    proc = run_handler(json.dumps({"hook_event_name": "PreCompact"}), str(blocker / "log.jsonl"))
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_the_registered_command_runs_through_a_shell(tmp_path):
    """The exact string placed in `--settings`, run as Claude Code runs a hook command: through a
    shell. A config dir with a space in it is what unquoted paths break on."""
    log = tmp_path / "config dir" / "claude-launch" / "compactions.jsonl"
    settings = json.loads(claude_config.hook_settings(log))
    command = settings["hooks"]["PreCompact"][0]["hooks"][0]["command"]
    proc = subprocess.run(
        command,
        shell=True,
        input=json.dumps({"hook_event_name": "PreCompact", "trigger": "auto"}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (proc.returncode, proc.stdout) == (0, "")
    [row] = lines(log)
    assert row["trigger"] == "auto"


@pytest.mark.parametrize(
    "row, context",
    [
        ({"id": "m", "tileward": {"context_len": 262144}}, 262144),
        ({"id": "m", "tileward": {"context_len": "262144"}}, 262144),
        ({"id": "m", "tileward": {}}, None),
        ({"id": "m"}, None),
        ({"id": "m", "tileward": {"context_len": 0}}, None),
        ({"id": "m", "tileward": {"context_len": "lots"}}, None),
    ],
)
def test_served_context_reads_the_nested_catalogue_field(row, context):
    assert claude_config.served_context(row) == context


@pytest.mark.parametrize(
    "context, environ, window",
    [
        (262144, {}, 230144),
        (262144, {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "8192"}, 253952),
        (262144, {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "junk"}, 230144),
        (262144, {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "0"}, 230144),
        (32000, {}, None),
        (8192, {}, None),
        (None, {}, None),
    ],
)
def test_compaction_window_is_served_context_less_the_output_reserve(context, environ, window):
    assert claude_config.compaction_window(context, environ) == window
