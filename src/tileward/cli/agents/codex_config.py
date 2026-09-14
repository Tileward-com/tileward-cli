"""Write a dedicated Codex profile file that points at this run's local proxy.

`codex --profile <name>` layers a separate `$CODEX_HOME/<name>.config.toml` on top of the base
config, so this never touches the user's real `config.toml`. The profile name is keyed to this
process's pid rather than fixed: two concurrent `launch codex` sessions sharing one file would
race, with whichever writes last silently cross-wiring the other's traffic through its proxy.
`runner.py` deletes the file once the child exits.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Tuple

_HEADER = "# managed by `twcli launch codex` -- deleted when this session exits\n"


def codex_home() -> Path:
    explicit = os.environ.get("CODEX_HOME", "").strip()
    return Path(explicit).expanduser() if explicit else Path.home() / ".codex"


def profile_config_path(profile_name: str) -> Path:
    return codex_home() / f"{profile_name}.config.toml"


def merge(*, base_url: str, token_env: str, model_id: str) -> Tuple[Path, str]:
    """Write this launch's profile file. `env_key` only names an environment variable
    (`token_env`); the real Tileward key never appears here."""
    profile_name = f"tileward-{os.getpid()}"
    path = profile_config_path(profile_name)
    text = (
        f"{_HEADER}"
        f'model_provider = "{profile_name}"\n'
        f'model = "{model_id}"\n'
        f"\n"
        f"[model_providers.{profile_name}]\n"
        f'name = "Tileward (local proxy)"\n'
        f'base_url = "{base_url}"\n'
        f'env_key = "{token_env}"\n'
        f'wire_api = "responses"\n'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path, profile_name
