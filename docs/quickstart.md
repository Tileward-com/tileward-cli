# Quickstart

## 1. Install

```bash
pip install tileward          # or: uv pip install tileward
twcli --version
```

## 2. Get a key

```bash
twcli auth login
twcli keys create --label laptop --save
```

`auth login` shows a short code and a URL. Approve it in a browser and the terminal receives a
console session; no password is typed into a terminal. `keys create --save` then mints an API key
and stores it in the active profile.

If you already have a key, skip the login:

```bash
export TILEWARD_API_KEY="tw_live_..."
```

The two credentials are not interchangeable, and which one a command needs is the first thing to
check when something returns 401. [Keys and sessions](credentials.md) explains the split.

## 3. Call a model

```bash
twcli models list
twcli chat "Summarise this repo's release process."
```

```python
from tileward import Tileward

tw = Tileward()
print(tw.chat.say("Summarise this in one line."))
```

Leave the model unset and the client asks the API which one to use. Do not hardcode an id — the
served catalogue is data, not code. [Tileward Models](models.md) has the detail.

## 4. Govern it

```bash
twcli guard check "write me a keylogger" --allow customer_support
```

```python
tw.guard.allows("write me a keylogger", allow=["customer_support"])   # -> False
```

The guard answers before the model writes a token. An allowlist refuses anything you did not name;
a blocklist passes it. [Tileward Governance](governance.md) covers which one holds under an
attacker.

## 5. Give it memory

```bash
twcli -c project-x context remember "We decided to ship on the 3rd."
twcli -c project-x context recall "when are we shipping?"
```

```python
tw = Tileward(conversation="project-x")
tw.context.remember("We decided to ship on the 3rd.")
tw.context.recall_text("when are we shipping?")
```

Scope every call to a conversation. Without one, every thread on the key writes into a single
shared store. [Tileward Context](context.md) says what that costs you.

## 6. Add your own files

```bash
twcli docs add handbook.pdf --folder policies
twcli docs search "leave policy" --content
```

```python
tw.documents.ingest_file("handbook.pdf", folder="policies")
```

Documents are account-scoped: a file you upload is available to every thread on the key.
[Tileward Documents](documents.md) covers ingest, listing, and what deleting one does not do.

## Next

- [Command line](cli.md) for the full command set, exit codes, and piping.
- [Configuration](configuration.md) for profiles and where credentials are stored.
- [Errors](errors.md) for what to catch, and what a refusal looks like.
