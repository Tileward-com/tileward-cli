"""Small helpers shared by the `anthropic` and `responses` adapters."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Union


def safe_json_loads(text: Optional[str]) -> Dict[str, Any]:
    """Parse tool-call arguments, tolerating a malformed upstream result -- `gpt-oss-20b`
    returns unparseable JSON on nested arguments in roughly 10% of calls."""
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def text_from_blocks(content: Union[str, List[Dict[str, Any]], None]) -> str:
    """Flatten a content value (a plain string, or a list of typed blocks) to text. Non-text
    blocks are marked rather than silently dropped."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    pieces: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            pieces.append(str(block.get("text") or ""))
        elif kind in ("image", "input_image"):
            pieces.append("[omitted: image]")
        else:
            pieces.append(f"[omitted: {kind}]")
    return "\n".join(p for p in pieces if p)


def new_id(prefix: str) -> str:
    import secrets

    return f"{prefix}_{secrets.token_hex(12)}"
