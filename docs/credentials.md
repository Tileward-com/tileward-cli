# Keys and sessions

Two credentials, and they are not interchangeable.

| | What it reaches | How you get it | Where the client sends it |
| --- | --- | --- | --- |
| **API key** (`tw_live_…`) | the model, the guard, Context, Documents | minted from a session | `Authorization: Bearer …` |
| **Console session** | the account: keys, billing, policies, audit | `twcli auth login` | `Cookie: tw_session=…` |

Key management authenticates on a signed session, not on a bearer key. A key that could mint keys
would survive its own revocation — revoke it, and whoever had it just mints another. So a fresh
install does two things in order:

```bash
twcli auth login                          # get a session
twcli keys create --label laptop --save   # mint a key
```

## Which one a call needs

Everything under `twcli keys`, `twcli account`, and `twcli context threads` needs a session.
Everything else needs a key. In the library the split is the same: `tw.keys` and `tw.account` read
`session_token`; `tw.chat`, `tw.models`, `tw.guard`, `tw.context` and `tw.documents` read
`api_key`.

Getting this wrong is the most common 401. `twcli auth status` shows which of the two this profile
is holding, and checks the session against the server rather than reporting the file's contents as
truth.

```console
$ twcli auth status
Profile        default
API host       https://api.tileward.com
Context host   https://context.tileward.com
API key        tw_live_…f4a1
Session        valid
Account        you@example.com
```

## Signing in

```bash
twcli auth login
twcli auth login --no-browser        # print the URL instead of opening one
twcli auth login --client-name "ci runner"
```

The flow is RFC 8628 device authorization: the CLI shows a code, you approve it in a browser, and
the terminal ends up holding a session. The approval screen shows a client name — by default
`twcli on <hostname>`, so the person approving can recognise the machine.
[The device flow](device-auth.md) documents the exchange and the polling states.

Driving it from a script:

```bash
twcli auth login --json
```

The code and URL are emitted **before** the wait, not after, so a script has them while they are
still useful.

## Signing out

```bash
twcli auth logout          # forget the session on this machine
twcli auth logout --all    # also forget the stored API key
```

This deletes the local copy. It does not end the session server-side. To cut off a session you no
longer control — a laptop you no longer have, a code you should not have approved — sign out
everywhere from [the console](https://app.tileward.com/account). That invalidates every session on
the account, this one included.

## Managing keys

```bash
twcli keys list
twcli keys create --label ci --tile customer_support
twcli keys rotate 7 --save
twcli keys revoke 7
twcli keys policy 7
```

The secret is shown once. Nothing can retrieve it afterwards, including `twcli`.

**Rotate rather than revoke-and-recreate.** Rotating keeps the key's id, and with it the policy
bound to the key and its whole usage and audit history. Creating a new key instead silently drops
the configuration and splits the trail.

`--tile` locks a key to named topics, enforced at the gate. See
[Tileward Governance](governance.md).

## Where they are stored

Settings go in `config.json`; credentials go in `credentials.json`, written `0600` in a `0700`
directory. They are separate files so showing your config never has to redact, and pasting it into
an issue has not pasted a key.

```bash
twcli config path
twcli config set-key          # prompts, so the secret misses shell history and `ps`
```

[Configuration](configuration.md) covers the resolution order and profiles.
