# Errors

```python
from tileward import errors

try:
    tw.chat.say("...")
except errors.InsufficientBalanceError:   # 402 — top up; calls resume immediately
    ...
except errors.GuardRefusal as exc:        # governance blocked it
    ...
except errors.APIError as exc:            # exc.status, exc.code, exc.request_id
    ...
```

## A refusal is not an error

**A governed refusal arrives as an ordinary completion**, with `finish_reason: "content_filter"`
and zero tokens billed. You are not charged for a refusal.

`chat.completions.create` passes it through, because that is what the API returned and a caller
metering responses needs to see it. `chat.say` raises `GuardRefusal`, because it promised text
back and returning an empty string would hide the reason.

```python
completion = tw.chat.completions.create("...")

from tileward.resources.chat import refusal_of
if refusal_of(completion):
    ...        # refused
```

`GuardRefusal.completion` carries the whole completion, so a handler can read the usage block and
the refusal text.

## The exception tree

```
TilewardError
├── ConfigError            not configured well enough to make the call
├── ConnectionError_       no answer at all: DNS, TLS, connect or read timeout
├── GuardRefusal           refused by governance
└── APIError               the API answered, and the answer was an error
    ├── AuthenticationError      401
    ├── InsufficientBalanceError 402
    ├── PermissionError_         403
    ├── NotFoundError            404
    ├── RateLimitError           429
    └── ServerError              5xx
```

Every `APIError` carries `.status`, `.code`, `.type`, `.body` and `.request_id`. Quote the request
id when reporting a problem — it identifies the exact call in the audit trail.

`ConnectionError_` and `PermissionError_` carry trailing underscores so they do not shadow the
builtins of the same name. `PermissionError_` is reachable as `errors.PermissionError_`; it is not
re-exported at the top level for that reason.

## What each one usually means

| Exception | Usually |
| --- | --- |
| `ConfigError` | no key, or a call that needs a session and has only a key |
| `AuthenticationError` | the key or session is missing, malformed, revoked, or expired |
| `InsufficientBalanceError` | the prepaid balance is exhausted; top up and continue |
| `PermissionError_` | authenticated, but not entitled on this plan |
| `NotFoundError` | a model id that is not served — check `tw.models.list()` |
| `RateLimitError` | back off; the client already retries these |
| `ServerError` | includes a 502 from an upstream model, which is transient |
| `ConnectionError_` | the request never got an answer |

`NotFoundError` from `models.retrieve` names what *is* served in its message, which is the answer
to the question you were about to ask.

A `404` from `api.tileward.com` on a non-`/v1` path is not a missing endpoint — that host serves
`/v1` only. See [Hosts](hosts.md).

## Retries

Connection failures, 429 and 5xx are retried twice by default, with exponential backoff and full
jitter, floored by any `Retry-After` the server sent. Full jitter rather than a fixed doubling: a
fleet that all retries at exactly 1s, 2s, 4s re-creates the burst that caused the 429.

A stream is not retried once bytes have arrived.

```python
Tileward(max_retries=0)          # off
```

## From the command line

Errors become a one-line message on stderr and an exit code:

| Exception | Exit code |
| --- | --- |
| `GuardRefusal` | `4` |
| `InsufficientBalanceError` | `5` |
| `AuthenticationError`, `ConfigError` | `3` |
| any other `TilewardError` | `1` |
| bad usage | `2` |
| Ctrl-C | `130` |

The full table, and what to do with it in CI, is in [Exit codes](cli.md#exit-codes).
