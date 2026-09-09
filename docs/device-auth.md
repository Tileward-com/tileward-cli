# How `twcli auth login` works

Short version: the CLI shows you a code, you approve it in a browser, and the CLI ends up holding
a console session. No password is ever typed into a terminal.

## Why a login at all, when you have an API key

Two different credentials, and they are not interchangeable.

| | What it reaches | How you get it |
| --- | --- | --- |
| **API key** (`tw_live_…`) | the model, the guard, Context, Documents | minted from a session |
| **Console session** | the account: keys, billing, policies, audit | `twcli auth login` |

Key management authenticates on a signed session, not on a bearer key. That is deliberate: a key
that could mint keys would survive its own revocation — revoke it, and whoever had it just mints
another. So the first thing a fresh install does is get a session, and the second is mint a key:

```bash
twcli auth login
twcli keys create --label laptop --save
```

## Why a device flow and not something simpler

Three simpler options, and what is wrong with each:

- **Prompt for the password.** That puts the account's primary credential through a process that
  also prints to a terminal, gets recorded in shell history, and ends up in CI logs.
- **Paste a session cookie from browser devtools.** This asks people to do the exact thing every
  phishing page asks them to do, which is a bad habit to teach a customer.
- **Open a local callback server.** Needs a listening port, which is awkward in a container,
  over SSH, and on a locked-down laptop.

A device flow ([RFC 8628](https://www.rfc-editor.org/rfc/rfc8628)) moves the credential handling
into the browser, where it belongs. The terminal never sees a password, and what it ends up
holding is a session that can be cut off at any time by signing out.

## The exchange

```
   twcli                                     api.tileward.com            you, in a browser
     |                                              |                            |
     |  POST /auth/device/code                      |                            |
     |--------------------------------------------->|                            |
     |  { device_code, user_code, verification_uri, |                            |
     |    verification_uri_complete, expires_in,    |                            |
     |    interval }                                |                            |
     |<---------------------------------------------|                            |
     |                                              |                            |
     |  shows you  WXYZ-1234  and a link  ------------------------------------->  |
     |                                              |                            |
     |  POST /auth/device/token   (every `interval` seconds)                      |
     |--------------------------------------------->|      GET  /api/auth/device |
     |  400 { "error": "authorization_pending" }    |<---------------------------|
     |<---------------------------------------------|      POST .../approve      |
     |                                              |<---------------------------|
     |  200 { access_token, token_type, expires_in, email }                       |
     |<---------------------------------------------|                            |
```

The polling errors are the RFC's slugs, returned as HTTP 400, so a client can tell "not yet" from
"never" without reading prose:

| slug | meaning | what the client does |
| --- | --- | --- |
| `authorization_pending` | nobody has approved yet | keep polling |
| `slow_down` | you polled faster than `interval` | add 5s to the interval and continue |
| `access_denied` | somebody refused it | stop |
| `expired_token` | the code ran out, **or was never real** | stop; start again |

## What is stored, and where

`twcli auth login` writes the session token, its expiry, and the account email to
`~/.config/tileward/credentials.json`, mode `0600` in a `0700` directory. Not a password, and not
anything that can mint a new session on its own. `twcli auth logout` deletes the local copy.

Deleting the local copy is not the same as ending the session. To cut off a session you no longer
control — a laptop you no longer have, a code you should not have approved — sign out everywhere
from the console. That invalidates every session on the account, this one included.

## Notes for anyone porting this

- **An unknown device code answers `expired_token`, identically to a real one that ran out.** Told
  apart, the pair answers "is this device code real", which is a probe an unauthenticated caller
  should not be able to run. Do not add a friendlier error.
- **Honour `slow_down` by adding to the interval, not multiplying it.** It is a politeness signal,
  not a rate-limit punishment.
- **The user code is normalised before it is hashed** — uppercase, non-alphanumerics dropped,
  regrouped in fours. Both ends must normalise identically or a correct code will not be found.
  Be forgiving about dashes, spaces and case: the guess budget is five, and none of it should be
  spent on punctuation.
- **The session comes back in the response body, not as a `Set-Cookie`.** The caller is a terminal.
  It replays the value as a cookie header on the account endpoints:
  `Cookie: tw_session=<access_token>`.
- **A `client_name` is shown to the person approving.** Send something they will recognise — the
  hostname, not just the tool's name. A screen that says only "approve this code" is a rubber
  stamp, and the person on the other end of a social-engineering call needs something to be
  suspicious with.
