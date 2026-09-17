"""Claude Code settings `twcli launch claude` derives for one run: the context window to compact
against, and hooks that record each compaction.

**The window.** Claude Code has no way to know a Tileward model's context size. For a model id it
doesn't recognize it assumes 200k, and it compacts a roughly fixed ~24.8k tokens below whatever
window it has (measured against a stub server on Claude Code 2.1.273;
`CLAUDE_CODE_MAX_CONTEXT_TOKENS` moves the trigger, not the gap). Each request also reserves Claude
Code's output budget -- 32,000 tokens unless `CLAUDE_CODE_MAX_OUTPUT_TOKENS` says otherwise -- out
of the same served window. So declaring the full served size is not safe: at 262,144, compaction
fires near 237.3k, while the server rejects any prompt over 230,144 once 32,000 output tokens are
requested. Declaring the served size minus the output reserve (230,144) compacts near 205.3k and
keeps that ~24.8k gap as margin.

**The hooks.** Claude Code brackets a compaction with three hook events (measured on 2.1.274):
`PreCompact` runs first, `PostCompact` receives the summary, and `SessionStart` fires with source
`compact` on the first turn after. The handler registered for all three (`claude_hook.py`) only
appends each event's metadata to a local JSONL and prints nothing, so a session behaves exactly as
it would without it -- what it adds is a record of when compaction happened and how big the summary
was.

They are passed as `--settings <json>`. Claude Code keeps only the last `--settings` it is given --
a second one replaces the first rather than merging with it (measured) -- so when the user passes
their own, the runner leaves the hooks out and says so rather than silently dropping either.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ...config import config_dir
from ...resources.models import summarize

WINDOW_ENV = "CLAUDE_CODE_MAX_CONTEXT_TOKENS"
OUTPUT_ENV = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"
# Claude Code's own default output reserve; the 400 that exposed all this read "you requested
# 32000 output tokens".
DEFAULT_OUTPUT_RESERVE = 32_000

HANDLER = Path(__file__).resolve().with_name("claude_hook.py")


def served_context(row: Mapping[str, Any]) -> Optional[int]:
    """A catalogue row's served context length, or None when it doesn't report a usable one."""
    value = summarize(dict(row)).get("context_len")
    if value is None:
        return None
    try:
        tokens = int(value)
    except (TypeError, ValueError):
        return None
    return tokens if tokens > 0 else None


def output_reserve(environ: Mapping[str, str]) -> int:
    raw = (environ.get(OUTPUT_ENV) or "").strip()
    try:
        tokens = int(raw)
    except ValueError:
        return DEFAULT_OUTPUT_RESERVE
    return tokens if tokens > 0 else DEFAULT_OUTPUT_RESERVE


def compaction_window(context: Optional[int], environ: Mapping[str, str]) -> Optional[int]:
    """The window to declare to Claude Code, or None when there is nothing safe to declare."""
    if context is None:
        return None
    window = context - output_reserve(environ)
    return window if window > 0 else None


def passes_own_settings(args: Sequence[str]) -> bool:
    return any(arg == "--settings" or arg.startswith("--settings=") for arg in args)


def compaction_log_path() -> Path:
    return config_dir() / "claude-launch" / "compactions.jsonl"


def _command_line(parts: List[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return " ".join(shlex.quote(part) for part in parts)


def hook_settings(log_path: Path, python: str = sys.executable) -> str:
    """The `--settings` JSON registering the handler on every compaction event."""
    command = {"type": "command", "command": _command_line([python, str(HANDLER), str(log_path)])}
    settings: Dict[str, Any] = {
        "hooks": {
            "PreCompact": [{"hooks": [command]}],
            "PostCompact": [{"hooks": [command]}],
            "SessionStart": [{"matcher": "compact", "hooks": [command]}],
        }
    }
    return json.dumps(settings)
