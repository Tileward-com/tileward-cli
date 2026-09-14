"""Local protocol translation for `twcli launch` — not part of the public library surface.

Claude Code and Codex do not speak Tileward's OpenAI-compatible `/v1/chat/completions`, so
`launch claude` and `launch codex` run a small local HTTP proxy (`proxy.py`) that translates each
tool's own protocol into a chat-completions call against Tileward and back. `launch opencode`
needs none of this: opencode already speaks chat completions natively.
"""
