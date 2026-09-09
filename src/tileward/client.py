"""`Tileward` and `AsyncTileward`.

Models, governance, keys and billing live on the API host; Context and Documents live
on the Context host and speak MCP. One client owns both connections.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, Optional

import httpx

from . import errors
from ._http import DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT, AsyncTransport, Transport
from ._mcp import AsyncContextTransport, ContextTransport
from ._version import __version__
from .config import (
    DEFAULT_BASE_URL,
    DEFAULT_CONSOLE_URL,
    DEFAULT_CONTEXT_URL,
    Config,
)
from .resources.account import Account, AsyncAccount
from .resources.chat import AsyncChat, Chat
from .resources.context import AsyncContext, Context
from .resources.documents import AsyncDocuments, Documents
from .resources.guard import AsyncGuard, Guard
from .resources.keys import AsyncKeys, Keys
from .resources.models import AsyncModels, Models


class _ClientBase:
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        session_token: Optional[str] = None,
        base_url: Optional[str] = None,
        context_url: Optional[str] = None,
        console_url: Optional[str] = None,
        model: Optional[str] = None,
        conversation: Optional[str] = None,
        profile: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_headers: Optional[Mapping[str, str]] = None,
        user_agent_suffix: Optional[str] = None,
        load_config: bool = True,
    ) -> None:
        # An explicit argument always wins. The stored profile is consulted only for what the
        # caller left out, and only when they have not opted out of file config entirely —
        # `load_config=False` is what a server process wants, so a developer's ~/.config cannot
        # change how production behaves.
        config = Config(profile) if load_config else None

        self.config = config
        self.api_key = api_key or (config.api_key if config else None)
        self.session_token = session_token or (config.session_token if config else None)
        self.base_url = (base_url or (config.base_url if config else DEFAULT_BASE_URL)).rstrip("/")
        self.context_url = (
            context_url or (config.context_url if config else DEFAULT_CONTEXT_URL)
        ).rstrip("/")
        self.console_url = (
            console_url or (config.console_url if config else DEFAULT_CONSOLE_URL)
        ).rstrip("/")
        self.default_model = model or (config.model if config else None)
        self.conversation = conversation or (config.conversation if config else None)
        self.timeout = timeout
        self.max_retries = max_retries
        self.default_headers = dict(default_headers or {})
        self.user_agent_suffix = user_agent_suffix

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    @property
    def has_session(self) -> bool:
        return bool(self.session_token)

    def _transport_kwargs(self) -> Dict[str, Any]:
        return {
            "base_url": self.base_url,
            "console_url": self.console_url,
            "api_key": self.api_key,
            "session_token": self.session_token,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "default_headers": self.default_headers,
            "user_agent_suffix": self.user_agent_suffix,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<{type(self).__name__} base_url={self.base_url!r} "
            f"api_key={'set' if self.has_api_key else 'unset'} "
            f"session={'set' if self.has_session else 'unset'}>"
        )


class Tileward(_ClientBase):
    """Synchronous client.

    ```python
    from tileward import Tileward

    tw = Tileward()                       # reads TILEWARD_API_KEY, then ~/.config/tileward
    print(tw.chat.say("Say hello."))
    print(tw.context.recall_text("what did we decide about pricing?"))
    ```
    """

    def __init__(self, *args: Any, http_client: Optional[httpx.Client] = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._transport = Transport(client=http_client, **self._transport_kwargs())
        self._context_transport = ContextTransport(
            url=self.context_url,
            api_key=self.api_key,
            timeout=self.timeout,
            user_agent=self._transport.user_agent,
            client=http_client,
        )
        self.models = Models(self)
        self.chat = Chat(self)
        self.guard = Guard(self)
        self.context = Context(self)
        self.documents = Documents(self)
        self.keys = Keys(self)
        self.account = Account(self)

    def with_conversation(self, conversation: str) -> Tileward:
        """A view of this client scoped to one conversation, sharing its connections.

        Cheaper and safer than constructing a second client per thread: same pooled sockets, and
        no chance of the copy silently picking up different credentials from the environment.
        """
        clone = object.__new__(Tileward)
        clone.__dict__.update(self.__dict__)
        clone.conversation = conversation
        clone.models = Models(clone)
        clone.chat = Chat(clone)
        clone.guard = Guard(clone)
        clone.context = Context(clone)
        clone.documents = Documents(clone)
        clone.keys = Keys(clone)
        clone.account = Account(clone)
        return clone

    def close(self) -> None:
        self._transport.close()
        self._context_transport.close()

    def __enter__(self) -> Tileward:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class AsyncTileward(_ClientBase):
    """Asynchronous client. Same surface, every method awaitable."""

    def __init__(self, *args: Any, http_client: Optional[httpx.AsyncClient] = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._transport = AsyncTransport(client=http_client, **self._transport_kwargs())
        self._context_transport = AsyncContextTransport(
            url=self.context_url,
            api_key=self.api_key,
            timeout=self.timeout,
            user_agent=self._transport.user_agent,
            client=http_client,
        )
        self.models = AsyncModels(self)
        self.chat = AsyncChat(self)
        self.guard = AsyncGuard(self)
        self.context = AsyncContext(self)
        self.documents = AsyncDocuments(self)
        self.keys = AsyncKeys(self)
        self.account = AsyncAccount(self)

    def with_conversation(self, conversation: str) -> AsyncTileward:
        clone = object.__new__(AsyncTileward)
        clone.__dict__.update(self.__dict__)
        clone.conversation = conversation
        clone.models = AsyncModels(clone)
        clone.chat = AsyncChat(clone)
        clone.guard = AsyncGuard(clone)
        clone.context = AsyncContext(clone)
        clone.documents = AsyncDocuments(clone)
        clone.keys = AsyncKeys(clone)
        clone.account = AsyncAccount(clone)
        return clone

    async def aclose(self) -> None:
        await self._transport.aclose()
        await self._context_transport.aclose()

    async def __aenter__(self) -> AsyncTileward:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


def openai_base_url(base_url: str = DEFAULT_BASE_URL) -> str:
    """The value to hand an OpenAI-compatible SDK.

    Every framework that speaks OpenAI works against Tileward by changing two things — this URL
    and the key — so this exists to stop people guessing whether `/v1` belongs on the end.
    """
    return f"{base_url.rstrip('/')}/v1"


__all__ = ["Tileward", "AsyncTileward", "openai_base_url", "errors", "__version__"]
