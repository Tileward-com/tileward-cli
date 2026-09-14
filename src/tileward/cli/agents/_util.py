"""Small helpers shared by the `anthropic` and `responses` adapters.

Kept deliberately tiny: the two protocols differ enough in shape (content blocks vs. input
items) that a bigger shared abstraction would cost more than it saves. Only the bits that are
genuinely identical — and genuinely easy to get subtly wrong twice — live here.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Union


def safe_json_loads(text: Optional[str]) -> Dict[str, Any]:
    """Parse tool-call arguments, tolerating a malformed upstream result.

    `gpt-oss-20b` returns unparseable JSON on nested tool arguments in roughly 10% of calls
    (measured in fwaa's docs/Tool_Calling_Reliability_2026-08-23.md) — usually cut off by exactly
    one closing brace. A proxy that raises on that turns a model-quality issue into a hard crash;
    an empty object at least lets the client's turn continue.
    """
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def text_from_blocks(content: Union[str, List[Dict[str, Any]], None]) -> str:
    """Flatten a content value (a plain string, or a list of blocks) to text.

    Both source protocols allow a message or a tool result to carry a list of typed blocks
    (text / image / …) instead of a bare string. Chat completions' `tool` and `user` message
    content is a string, so this is the one join point both adapters need. Non-text blocks are
    marked rather than silently dropped, since a translated transcript that quietly loses an
    image is a harder bug to notice than a visible placeholder.
    """
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
