# Tileward Documents

Answers from your own files. Documents share Context's connection —
`context.tileward.com`, authenticated with an API key.

## Account-scoped, not conversation-scoped

A file you upload is available to every thread on the key. That is why none of these calls take a
`conversation`, and why `twcli docs` ignores `-c` rather than pretending to honour it.

## Ingest

```python
tw.documents.ingest_file("handbook.pdf", folder="policies")
tw.documents.ingest_text("Q3 report", report_text, folder="reports", tags=["finance"])
```

```bash
twcli docs add handbook.pdf notes.md --folder policies
cat report.txt | twcli docs add --title "Q3 report"
```

`twcli docs add` takes several files and does them one at a time: a failure on one does not abandon
the rest, and the command exits `1` if any failed. Each file's result is reported as it goes.

Files are sent base64 inside a JSON-RPC body, so this client caps a single file at **32 MB** — a
mistaken `twcli docs add *` against a directory of videos fails immediately with a readable message
instead of after a long upload. For something larger, extract the text and use `ingest_text`.

## List and search

```python
tw.documents.list(folder="policies")
tw.documents.search("handbook")
```

```bash
twcli docs ls --folder policies
twcli docs ls --tag finance --limit 20
twcli docs search "leave policy"
twcli docs search "leave policy" --content
```

**`search` filters the listing, not the text.** By default it matches titles, folders and tags —
it finds a filename. `--content` recalls against the ingested chunks instead, which is what answers
a question:

```python
tw.context.recall("what does the handbook say about leave?", scope="all")
```

That is the same call `--content` makes. Documents are read through Context's recall, so
everything on [Tileward Context](context.md) about budgets and scopes applies.

Rows carry `source_id`, `title`, `type`, `locator`, `folder`, `tags`, `chunks`, `ts` and
`disabled`.

## Delete, and what delete leaves

```python
tw.documents.delete("src_...")
```

```bash
twcli docs rm src_abc123
```

Deleting a document drops its chunks. **Material already folded into a conversation's summary can
still surface in a recall**, because the summary is not the document. If something has to be
genuinely gone, `twcli context purge` — `context.purge_account()` — is the operation that means
it.

That distinction matters most in exactly the case where you care: a file uploaded by mistake is not
fully removed by `docs rm` alone if a conversation has already recalled against it.

## Privacy

Files stay private to the account: never pooled with another tenant's, never used for training.
The full statement, including deployment modes where content never reaches Tileward at all, is at
[tileward.com/llms.txt](https://tileward.com/llms.txt).
