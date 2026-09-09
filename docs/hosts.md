# Hosts

There are three, and they do not overlap.

| Host | Serves | Authenticates with |
| --- | --- | --- |
| `api.tileward.com` | `/v1` — chat completions, models, the guard | API key |
| `app.tileward.com` | `/auth/*` and `/api/*` — the account surface | console session |
| `context.tileward.com` | Tileward Context and Documents, over MCP | API key |

`api.tileward.com` serves `/v1` **only**. Anything else answers `404 wrong_host` — it is not a
routing accident you can work around, and a `404` from it does not mean the endpoint is gone.

## You do not route this by hand

The client picks the host from what a call needs: a call that carries an API key goes to the API
host, a call that carries a session goes to the console host, and Context has its own connection.
One client owns all three.

```python
tw = Tileward()
tw.chat.say("...")        # api.tileward.com/v1/chat/completions
tw.account.get()          # app.tileward.com/api/account
tw.context.recall("...")  # context.tileward.com
```

The one thing to get right is not collapsing them. A configuration that points `TILEWARD_BASE_URL`
at the console host, or the reverse, produces 404s that read like a missing feature.

## Overriding them

| Setting | Environment | CLI flag | `twcli config set` |
| --- | --- | --- | --- |
| API host | `TILEWARD_BASE_URL` | `--base-url` | `base_url` |
| Console host | `TILEWARD_CONSOLE_URL` | — | `console_url` |
| Context host | `TILEWARD_CONTEXT_URL` | `--context-url` | `context_url` |

```python
tw = Tileward(
    base_url="https://api.internal.example",
    console_url="https://app.internal.example",
    context_url="https://context.internal.example",
)
```

Set all three when you point at a self-hosted deployment. Setting one and leaving the others at
their defaults sends part of your traffic to the hosted service.

There is no global `--console-url` flag; use the environment variable or store it on the profile.

## The Context address

`https://context.tileward.com` is the canonical one. It is a whole address with nothing to
append — the MCP endpoint is the host root, not a path under it.

`https://api.tileward.com/mcp` is the legacy address and still serves, so an older configured value
keeps working. New configuration should use the canonical host.
