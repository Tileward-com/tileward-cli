# Tileward Context

Keep the conversation, send the model only the part that answers the question. Context speaks MCP
on `context.tileward.com` and authenticates with an API key.

## Scope every call to a conversation

Without one, every thread on the key writes into a single shared store, and a recall in one thread
hands back another thread's material as if it were its own. There is no safety net underneath
this: a caller that names no conversation joins `default` along with everyone else who named none.

```python
tw = Tileward(conversation="thread-42")

# or, sharing one client's connections across many threads:
thread = tw.with_conversation("thread-42")
```

```bash
twcli -c thread-42 context recall "what did we decide about pricing?"
```

`with_conversation` is cheaper and safer than building a second client per thread: same pooled
sockets, and no chance of the copy picking up different credentials from the environment.

The write commands warn once when they are about to write into the shared default.

## Remember and recall

```python
tw.context.remember("We decided to ship on the 3rd.", role="user")
tw.context.remember_many([{"role": "user", "text": "..."}, ...])
tw.context.recall("when are we shipping?")      # the full bundle
tw.context.recall_text("when are we shipping?") # just the block to paste into a prompt
```

```bash
twcli -c thread-42 context remember "We decided to ship on the 3rd."
twcli -c thread-42 context recall "when are we shipping?"
twcli -c thread-42 context recall "..." --text-only    # for piping into a prompt
```

`recall` sizes the answer automatically. The knobs, all optional:

| Argument | CLI | What it does |
| --- | --- | --- |
| `budget_tokens` | `--budget` | a fixed token ceiling instead of the adaptive one |
| `context_window` | `--window` | your model's window, for a better automatic cap |
| `max_items` | `--max-items` | cap how many items come back |
| `scope` | `--scope` | `all` for every conversation on the key, or `topic:<name>` |
| `tags` | `--tag` | narrow to tagged turns |
| `ingest` | `--no-ingest` | whether the query itself is stored |

Only what you actually set is sent, so a request describes your intent rather than this client's
defaults.

## Pins

```python
tw.context.pin("The customer is ACME.")
tw.context.unpin(3)
```

```bash
twcli -c thread-42 context pin "The customer is ACME."
twcli -c thread-42 context unpin 3
```

A pin is included in every recall on that conversation whatever the query. Use it for the standing
facts a good answer always needs, not for anything a query would find on its own.

## Topics

Label a conversation so recall can search across a set of them:

```bash
twcli -c thread-42 context topics pricing launch
twcli -c thread-99 context recall "pricing decisions" --scope topic:pricing
```

```python
tw.context.set_topics(["pricing", "launch"])          # add
tw.context.set_topics(["pricing"], replace=True)      # replace
```

## Forget is not delete

```python
tw.context.forget("the old pricing model")
```

`forget` retires a topic from recall. **The stored turns remain.** It stops material coming back;
it does not remove it. The name is the trap — if the data itself has to go, the operation is
`purge_account()`.

| Call | CLI | What it does |
| --- | --- | --- |
| `forget(query)` | `context forget` | stops material being recalled; turns stay |
| `reset()` | `context reset` | empties ONE conversation's store |
| `clear_account()` | — | empties every conversation on the key |
| `purge_account()` | `context purge` | deletes the stored data itself |

`reset`, `clear_account` and `purge_account` have no undo. The CLI confirms before each; `--yes`
skips the prompt.

## Inspecting a store

```bash
twcli -c thread-42 context show      # a query-less primer of the live state
twcli -c thread-42 context stats     # size and shape of this conversation's store
twcli context threads                # every conversation on the account
```

`context threads` reads the account surface, so it needs a signed-in session rather than an API
key. See [Keys and sessions](credentials.md).

```python
tw.context.primer()
tw.context.stats()
```

## Wiring it into a prompt

The pattern is: recall, prepend, call.

```python
tw = Tileward(conversation="thread-42")

question = "when are we shipping?"
recalled = tw.context.recall_text(question)
answer = tw.chat.say(
    question,
    system=f"Relevant context from earlier:\n{recalled}",
)
tw.context.remember(question, role="user")
tw.context.remember(answer, role="assistant")
```

Prepend it as context rather than merging it into the user's words, so the model can tell what the
person asked from what the store supplied. `twcli chat --remember` does exactly this.

What Context saves on real threads, and how it was measured, is on
[tileward.com/context](https://tileward.com/context/). Your own account's figures are in
`twcli account savings`.
