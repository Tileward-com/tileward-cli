# Configuration

Highest wins: an explicit argument, then the environment, then the profile, then the built-in
default.

## Environment

| Variable | What it sets |
| --- | --- |
| `TILEWARD_API_KEY` | the key used for models, guard, Context and Documents |
| `TILEWARD_SESSION` | a console session, for the account surface |
| `TILEWARD_BASE_URL` | the API host — serves `/v1` only |
| `TILEWARD_CONSOLE_URL` | the console host — the account surface and `auth login` |
| `TILEWARD_CONTEXT_URL` | the Context host |
| `TILEWARD_CONVERSATION` | default conversation scope |
| `TILEWARD_MODEL` | default model id |
| `TILEWARD_PROFILE` | which stored profile to read |
| `TILEWARD_CONFIG_DIR` | where the files live |
| `TILEWARD_NO_COLOR` | disable colour (so does `NO_COLOR`) |

## Files

```bash
twcli config path
twcli config show
```

`config.json` holds settings. `credentials.json` holds secrets, written `0600` in a `0700`
directory. They are separate files so showing your config never has to redact, and pasting it into
an issue has not pasted a key.

The directory is `$TILEWARD_CONFIG_DIR` if set, then `$XDG_CONFIG_HOME/tileward`, then
`~/.config/tileward`.

Writes are atomic: the temp file is created with its final permissions before any bytes go in, and
replaced into place — so a crash mid-write cannot leave a half-file that reads as "logged out", and
a secret is never briefly world-readable.

`twcli config show` prints a fingerprint of the key (`tw_live_…f4a1`), never the key. `--reveal`
prints it in full and warns that it is now in your scrollback.

## Profiles

A profile is a named set of settings and credentials. `default` unless you say otherwise.

```bash
twcli config profiles
twcli -p staging config set base_url https://api.staging.example
twcli -p staging config set-key
twcli -p staging account show
```

Settable names: `base_url`, `context_url`, `console_url`, `model`, `conversation`. `twcli config
unset <name>` returns one to its built-in default.

```python
tw = Tileward(profile="staging")
```

## In a server process

```python
tw = Tileward(load_config=False, api_key=os.environ["TILEWARD_API_KEY"])
```

`load_config=False` ignores the files entirely, so a developer's `~/.config` cannot change how
production behaves. Pass what you need explicitly.

## Client arguments

Every setting has an explicit argument, and an explicit argument always wins.

```python
Tileward(
    api_key=...,            # or TILEWARD_API_KEY
    session_token=...,      # or TILEWARD_SESSION
    base_url=...,           # api host
    console_url=...,        # account surface
    context_url=...,        # Context
    model=...,              # default model id
    conversation=...,       # default Context scope
    profile=...,            # which stored profile to read
    timeout=60.0,
    max_retries=2,
    default_headers={...},
    user_agent_suffix="my-service",
    load_config=True,
    http_client=...,        # bring your own httpx.Client
)
```

`http_client` lets you supply an `httpx.Client` (or `httpx.AsyncClient`) you already own — your own
pool limits, proxy, or transport. The client will not close a connection it did not open.

## Timeouts and retries

`timeout` defaults to 60s per request. Streaming uses a longer ceiling, because a long completion
legitimately takes minutes and a read timeout firing mid-answer bills for tokens you never see.

`max_retries` defaults to 2 and covers connection failures, 429, and 5xx. Backoff is exponential
with full jitter, floored by any `Retry-After` the server sent. A stream is not retried once bytes
have arrived — retrying would replay part of an answer.

Per-call overrides:

```python
tw.chat.completions.create("...", timeout=120)
```

## Connections

```python
with Tileward() as tw:
    ...

tw = Tileward()
try:
    ...
finally:
    tw.close()
```

`with_conversation()` returns a view that shares the parent's connections, so scoping per thread
does not multiply sockets. See [Tileward Context](context.md).
