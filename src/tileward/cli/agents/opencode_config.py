"""Merge a Tileward provider block into opencode's own config.

opencode already speaks Tileward's real `/v1/chat/completions` (`openai_base_url()`), so unlike
`launch claude` / `launch codex` there is no local proxy here — just pointing opencode's own
`@ai-sdk/openai-compatible` provider at Tileward.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

PROVIDER_ID = "tileward"


def config_path() -> Path:
    explicit = os.environ.get("OPENCODE_CONFIG", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    base = Path.home() / ".config" / "opencode"
    jsonc = base / "opencode.jsonc"
    return jsonc if jsonc.exists() else base / "opencode.json"


def provider_block(base_url: str, model_ids: List[str]) -> Dict[str, Any]:
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Tileward",
        "options": {
            "baseURL": base_url,
            # A reference, not the literal key: opencode resolves `{env:NAME}` itself at call
            # time, so the real `tw_live_...` key is never written to this file.
            "apiKey": "{env:TILEWARD_API_KEY}",
        },
        "models": {model_id: {"name": model_id} for model_id in model_ids},
    }


def merge(base_url: str, model_ids: List[str]) -> Tuple[Path, bool]:
    """Merge `provider.tileward` into the user's opencode config. Returns `(path, applied)`;
    `applied` is False when the existing file isn't plain JSON (opencode also allows JSONC),
    in which case nothing is written rather than risk corrupting it."""
    path = config_path()
    doc: Dict[str, Any] = {"$schema": "https://opencode.ai/config.json"}
    if path.exists():
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return path, False
        if not isinstance(parsed, dict):
            return path, False
        doc = parsed

    providers = doc.setdefault("provider", {})
    if not isinstance(providers, dict):
        return path, False
    providers[PROVIDER_ID] = provider_block(base_url, model_ids)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path, True
