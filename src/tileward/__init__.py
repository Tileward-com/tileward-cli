"""Tileward: run large models on hardware you own, governed.

The library and the `twcli` command are the same package. Two lines to a first call:

```python
from tileward import Tileward

tw = Tileward(api_key="tw_live_...")     # or set TILEWARD_API_KEY
print(tw.chat.say("Say hello in one sentence."))
```

Four capabilities hang off the client:

  `tw.models` / `tw.chat`   Tileward Models — an OpenAI-compatible chat surface.
  `tw.guard`                Tileward Governance — allow/deny before the model writes a token.
  `tw.context`              Tileward Context — recall the slice of history a question needs.
  `tw.documents`            Tileward Documents — answers from your own files.

plus `tw.keys` and `tw.account`, which manage the account itself and need a console session from
`twcli auth login` rather than an API key.
"""

from ._version import __version__
from .client import AsyncTileward, Tileward, openai_base_url
from .config import Config
from .errors import (
    APIError,
    AuthenticationError,
    ConfigError,
    ConnectionError_,
    GuardRefusal,
    InsufficientBalanceError,
    NotFoundError,
    RateLimitError,
    ServerError,
    TilewardError,
)

__all__ = [
    "Tileward",
    "AsyncTileward",
    "Config",
    "openai_base_url",
    "TilewardError",
    "ConfigError",
    "APIError",
    "AuthenticationError",
    "InsufficientBalanceError",
    "NotFoundError",
    "RateLimitError",
    "ServerError",
    "ConnectionError_",
    "GuardRefusal",
    "__version__",
]
