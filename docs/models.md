# Tileward Models

An OpenAI-compatible chat surface on `api.tileward.com/v1`. Authenticates with an API key.

## Do not hardcode a model id

The served catalogue is a database table, not something in this package. Ids appear, get repriced
and get withdrawn without a release, so an id copied into a script is one that will eventually
404. Ask instead:

```bash
twcli models list
```

```console
id                precision           context  USD / Mtoken  compression
tileward-35b-a3b  W4A16 (Tileward)    262,144  1             2.8
gpt-oss-20b       MXFP4 (as shipped)  8,192    0.2           1
gpt-oss-120b      MXFP4 (as shipped)  131,072  0.4           —
```

```python
tw.models.list()               # every served row, as the API returned it
tw.models.ids()                # just the ids
tw.models.retrieve("gpt-oss-20b")
tw.models.default()            # the id the client would use if you named none
```

Leave `model` unset on a call and the client resolves it: the configured default first, then the
first served id. It deliberately does not fall back to a literal.

`twcli models list --json` emits the API's own rows unflattened, so a script sees what the endpoint
returns rather than a shape the CLI invented for a terminal. The table view flattens the nested
`tileward` block up one level for reading.

The `context` column is the served window for that model. Read it from here rather than from any
document — it is the live value.

## Chat

```python
tw.chat.say("Summarise this in one line.")            # -> str
tw.chat.completions.create(                           # -> the raw OpenAI-shaped dict
    [{"role": "user", "content": "Hello"}],
    max_tokens=200,
)

for piece in tw.chat.stream("Count to five."):
    print(piece, end="", flush=True)
```

`create` returns exactly what the API returned, `usage` included. `say` gives you the text, and
raises `GuardRefusal` when governance blocked the call rather than returning an empty string. See
[Errors](errors.md).

A bare string is accepted anywhere a message list is:

```python
tw.chat.say("hello")
tw.chat.say([{"role": "user", "content": "hello"}])
```

`system=` inserts a system message only if you have not already supplied one, so it cannot silently
shadow yours.

### Parameters

`model`, `system`, `max_tokens`, `temperature`, `top_p`, `stream`, `timeout`. Anything else you
pass is forwarded to the API untouched, which is how you reach a parameter this client does not
know about yet.

```python
tw.chat.completions.create("Hello", seed=7, stop=["\n\n"])
```

## From the command line

```bash
twcli chat "Say hello in one sentence."
twcli chat -i                                  # a REPL that keeps the transcript
cat notes.md | twcli chat --system "You summarise."
twcli chat "..." --usage                       # print token usage after the answer
twcli chat "..." -m gpt-oss-20b --max-tokens 200
```

Streaming is on when stdout is a terminal and off when it is a pipe: a consumer reading the output
wants the whole answer, not tokens interleaved with progress. `--stream` / `--no-stream` overrides
that.

`--remember` writes each turn into Tileward Context and recalls before answering:

```bash
twcli chat -i -c project-x --remember
```

Pair it with `-c`. Without one the transcript joins the key's shared default store — the REPL warns
once when that is about to happen. See [Tileward Context](context.md).

In the REPL, `/reset` clears the transcript and `/exit` (or Ctrl-D) leaves.

## Model detail

```bash
twcli models show tileward-35b-a3b
```

Everything the API reports about one model: precision, served context window, rate, compression
ratio, and whatever fields the catalogue grows later. How each published figure was measured is on
[tileward.com/models](https://tileward.com/models/).
