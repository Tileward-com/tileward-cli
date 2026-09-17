"""The hook command `twcli launch claude` registers for Claude Code's compaction events.

Claude Code runs this file as a plain script, so it uses the standard library only: it starts fast
and cannot fail on the package's own imports. For each event it appends one line of metadata to the
JSONL path given on its command line -- never the summary or the conversation itself, which stay
in Claude Code's own transcript at `transcript_path`.

It never prints and always exits 0. Both matter: `PreCompact` stdout is appended to the
summarizer's instructions, `SessionStart` stdout is put in front of the model, and exit code 2 from
`PreCompact` blocks compaction -- after which Claude Code refuses the oversized request and the turn
ends. A bookkeeping hook must not be able to do any of that.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

_COPIED = ("hook_event_name", "trigger", "source", "session_id", "transcript_path", "cwd")


def record(payload: Dict[str, Any]) -> Dict[str, Any]:
    """What gets logged for one event: identifiers and sizes, no content."""
    out: Dict[str, Any] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    for key in _COPIED:
        value = payload.get(key)
        if isinstance(value, str):
            out[key] = value
    summary = payload.get("compact_summary")
    if isinstance(summary, str):
        out["summary_chars"] = len(summary)
    instructions = payload.get("custom_instructions")
    if isinstance(instructions, str):
        out["custom_instructions_chars"] = len(instructions)
    return out


def main(argv: List[str]) -> int:
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict) and len(argv) > 1:
            path = Path(argv[1])
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record(payload)) + "\n")
    except Exception:
        pass  # see the module docstring: a failed log line must never reach the session
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
