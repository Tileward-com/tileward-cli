# The command line

`twcli` is the same client the library exposes, with a terminal on the front. Every command speaks
JSON on request, and when it does, stdout carries nothing else.

```bash
twcli --help
twcli context recall --help
```

## Every command

<!-- command-index:start -->

| Command | What it does |
| --- | --- |
| `twcli auth login` | sign in from this terminal, approving in a browser |
| `twcli auth logout` | forget the stored session on this machine |
| `twcli auth status` | what this profile holds, and whether it still works |
| `twcli models list` | every model the API is serving |
| `twcli models show` | everything the API reports about one model |
| `twcli chat` | send a prompt and print the answer |
| `twcli guard check` | decide whether text is in policy |
| `twcli context recall` | retrieve only the history relevant to a query |
| `twcli context remember` | store a turn so later recalls can find it |
| `twcli context show` | a query-less primer of the live conversation state |
| `twcli context pin` | pin a fact that every recall includes |
| `twcli context unpin` | remove a pin by its id |
| `twcli context forget` | retire a topic from recall — not a delete |
| `twcli context topics` | label a conversation so `--scope topic:<name>` finds it |
| `twcli context stats` | size and shape of this conversation's store |
| `twcli context threads` | every conversation on the account (needs a session) |
| `twcli context reset` | empty ONE conversation's store |
| `twcli context purge` | delete every conversation's stored data on this key |
| `twcli docs ls` | list documents on the account |
| `twcli docs add` | ingest files, or text piped in |
| `twcli docs rm` | remove documents by source id |
| `twcli docs search` | find documents, or `--content` to search the text |
| `twcli keys list` | keys on this account |
| `twcli keys create` | mint a key — the secret is shown once |
| `twcli keys rotate` | new secret, same id, same policy, same history |
| `twcli keys revoke` | revoke a key, immediately and permanently |
| `twcli keys policy` | what governs this key |
| `twcli account show` | balance, plan, and entitlements |
| `twcli account usage` | day-by-day token usage |
| `twcli account audit` | request history, metadata only |
| `twcli account savings` | what Context has saved |
| `twcli config show` | what this profile resolves to |
| `twcli config set` | set a value on this profile |
| `twcli config unset` | remove a value, falling back to the default |
| `twcli config set-key` | store an API key, prompted rather than typed |
| `twcli config profiles` | every profile this machine knows about |
| `twcli config path` | where the config and credentials files live |

<!-- command-index:end -->

`auth`, `keys`, `account` and `context threads` need a console session. Everything else needs an
API key. See [Keys and sessions](credentials.md).

## Global flags

| Flag | Environment | What it does |
| --- | --- | --- |
| `-p, --profile` | `TILEWARD_PROFILE` | which stored profile to read and write |
| `--api-key` | `TILEWARD_API_KEY` | override the stored API key |
| `--base-url` | `TILEWARD_BASE_URL` | override the API host |
| `--context-url` | `TILEWARD_CONTEXT_URL` | override the Context host |
| `-c, --conversation` | `TILEWARD_CONVERSATION` | scope Context calls to a conversation |
| `-m, --model` | `TILEWARD_MODEL` | model id to use |
| `--json` | | emit JSON on stdout and nothing else |
| `-q, --quiet` | | suppress progress notes on stderr |
| `--no-color` | `NO_COLOR` | disable colour |
| `--timeout` | | per-request timeout in seconds |
| `-V, --version` | | print the version |

`--json`, `--quiet`, `-c` and `-m` work in either position — `twcli --json models list` and
`twcli models list --json` mean the same thing. The rest belong before the subcommand.

`NO_COLOR` is honoured whatever its value, as is `TILEWARD_NO_COLOR`.

## JSON, stdout, and stderr

With `--json`, the JSON is the entire contents of stdout. Progress notes, warnings and errors go to
stderr, so a pipe never sees them.

```bash
twcli models list --json | jq -r '.[].id'
twcli account usage --json > usage.json
```

Without `--json` you get a table or the raw text, and model output is written without markup
interpretation — square brackets in an answer stay square brackets.

## Reading from stdin

`chat`, `guard check`, `context recall`, `context remember` and `docs add` all read stdin when
their argument is missing or is `-`:

```bash
cat notes.md | twcli chat --system "You summarise."
cat prompts.txt | twcli guard check --allow customer_support
cat report.txt | twcli docs add --title "Q3 report"
```

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | fine |
| `1` | error |
| `2` | bad usage |
| `3` | not signed in, or not configured |
| `4` | refused by governance |
| `5` | balance exhausted |
| `130` | interrupted |

Distinguishing those matters in CI: "the balance ran out" and "the network was down" call for
different action, and neither should need English parsing to detect.

`twcli guard check --exit-code` is what turns a refusal into exit `4`; without it the command
reports the decision and exits `0`, because a successful check is a success whatever it decided.

A closed pipe is not a failure — `twcli models list | head` exits `0` rather than printing a
traceback.

## Recipes

```bash
# a governance gate in a shell script
twcli guard check -f prompts.txt --allow customer_support --exit-code || echo "off policy"

# recall a slice of a thread and pipe it into something else
twcli -c project-x context recall "what did we decide about pricing?" --text-only

# two accounts on one machine
twcli -p staging account show

# the id of the first served model
twcli models list --json | jq -r '.[0].id'

# check a key still works before a long job
twcli auth status --json | jq -e '.session_valid'
```

## Profiles

```bash
twcli config profiles
twcli -p staging config set base_url https://api.staging.example
twcli -p staging config set-key
twcli -p staging chat "..."
```

A profile is a named set of settings and credentials on one machine — one account per profile, no
environment juggling. [Configuration](configuration.md) covers what a profile holds and how it is
resolved.
