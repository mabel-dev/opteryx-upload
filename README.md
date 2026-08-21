# opteryx-upload

Client for the [Opteryx Upload Service](https://github.com/mabel-dev/upload.opteryx):
a Python SDK, a command line, and a full-screen terminal app.

All three do the same four things in the same order, because a difference
between them is a place where the thing you tested by hand is not the thing CI
does. **Negotiate** — send a few megabytes of sample and agree what the data
will become. **Look** at the plan. **Accept** it. Then **upload and commit**.

Nothing is sent until that is settled, so an upload that was going to be refused
is refused for the price of a sample rather than after four gigabytes. And an
inferred type is never accepted without somebody saying so: a CSV cannot say
that a column of dotted quads is an address, and once it is catalogued as
VARCHAR no amount of reading the data back will tell you it was wrong.

## Install

```bash
pip install opteryx-upload
```

The CLI and the TUI come with it — argparse and curses, both standard library.
Nothing here pulls a terminal framework into your dependency tree.

## The command line

```bash
export OPTERYX_CLIENT_ID="<access token username>"
export OPTERYX_CLIENT_SECRET="<access token>"

opteryx-upload push findings.csv --to acme.security.findings
```

You are not asked where the schema comes from, because the destination answers
it. A dataset that already declares its columns supplies them, and the only
question left is whether these rows are added or replace what is there. A
dataset that does not exist yet has its types read from your data and shows them
to you first:

```
  findings.csv  686.5 KB

  acme.security.findings  new, types read from your data

  column     sample                type
  cve_id     CVE-2026-00001        VARCHAR
  published  2026-08-02T04:22:07Z  VARCHAR
  source_ip  10.1.7.13             VARCHAR
  hosts      1                     INT64

  these types were read from your data
  accept [enter]   change column=TYPE   drop -column   stop q
  > published=TIMESTAMP source_ip=IPV4
```

The sampled value next to each column is the point of that table: `published`
sitting next to `2026-08-02T04:22:07Z` reads as wrong at a glance in a way that
`published: VARCHAR` does not.

Appending to a dataset that already exists asks nothing at all — it uses the
types the catalog holds, converts your columns to match, and tells you which
conversions it will make.

### In a pipeline

There is no terminal to show the table to, so inference has to be authorised in
advance. `--yes` accepts what was read from the data; `--declare` says the types
outright. A `push` with neither is refused rather than guessed at.

```bash
opteryx-upload push data/*.parquet --to acme.security.findings \
    --type published=TIMESTAMP --type source_ip=IPV4 \
    --message "nightly load" --yes
```

`plan` runs the same negotiation, prints the table and abandons the contract, so
it uploads nothing and leaves nothing behind:

```bash
opteryx-upload plan data/*.parquet --to acme.security.findings --json
```

### Exit codes

Part of the interface: a pipeline that has to grep stderr will eventually retry
the wrong thing.

| code | meaning | retrying |
|---|---|---|
| 0 | committed | — |
| 2 | bad arguments, missing file, no credentials | no |
| 3 | refused: a value that will not cast, files that disagree, a column the dataset does not declare | no |
| 4 | the target moved after the contract was agreed | yes |
| 5 | not signed in, or not permitted to write here | no |
| 6 | the service could not be reached, or failed | yes |
| 130 | interrupted | — |

3 and 6 are deliberately different numbers. Retrying a refusal never helps and
retrying a broken service often does.

### Commands

| | |
|---|---|
| `push FILE... --to W.C.D` | negotiate, upload, commit |
| `plan FILE... --to W.C.D` | negotiate and print; upload nothing |
| `show CONTRACT_ID` | print a contract by id |
| `abandon CONTRACT_ID` | give up on one |
| `tui` | the full-screen version; also what bare `opteryx-upload` opens |

### Options

| | |
|---|---|
| `--to WORKSPACE.COLLECTION.DATASET` | where the rows go (required) |
| `--append` / `--overwrite` | for a dataset that exists; asked if you are at a terminal |
| `--type COLUMN=TYPE` | correct one type without a prompt; repeatable |
| `--ignore COLUMN` | read this column and do not write it; repeatable |
| `--infer` / `--use-dataset` / `--declare COLUMN:TYPE` | override the destination's answer |
| `-y`, `--yes` | accept inferred types unasked; required off a terminal |
| `-m`, `--message` | snapshot message |
| `--json` | the contract as the service sent it |
| `--no-color` | never colour the output |

### Credentials

Set `OPTERYX_CLIENT_ID` to your access token username and
`OPTERYX_CLIENT_SECRET` to the access token. It is exchanged for a short-lived
assertion and re-exchanged as that ages, which is what lets an upload measured
in gigabytes finish.

`OPTERYX_TOKEN` takes a bearer JWT instead, for a caller that already holds a
valid one. An access token in the environment wins over it, and `--token` wins
over both.

The service comes from `OPTERYX_UPLOAD_URL` and the authenticate service from
`OPTERYX_AUTH_URL`. Each has a flag if you would rather pass it.

## The full-screen version

```bash
opteryx-upload                 # or: opteryx-upload tui findings.csv --to acme.security.findings
```

Run it with no arguments at a terminal and this is what you get — typing the
name and nothing else means you want to upload something, not to read a list of
subcommands. Off a terminal, no arguments prints the usage instead.

Same contract, same calls. What it adds is that the plan stays put: at a
scrolling prompt the table goes past once and correcting a type means retyping
the whole command, and here the cursor moves down it and `e` changes the type of
the row under the cursor.

```
 opteryx upload                                        https://upload.opteryx.app

 FILES
   part-0000.parquet  412.9 MB
   part-0001.parquet  398.1 MB

 ACCOUNT
   acme-etl

 TO
   acme.security.findings

 PLAN   a new dataset; these types were read from your data
   column     sample                type
   cve_id     CVE-2026-00001        VARCHAR
   published  2026-08-02T04:22:07Z  TIMESTAMP[us]   was VARCHAR, converted
 › source_ip  10.1.7.13             IPV4            was VARCHAR, converted
   hosts      1                     INT64
   score      0.5                   FLOAT64         read and not written

 these types were read from your data - nothing is written until you accept
 ↑↓ column  e retype  x ignore  ⏎ accept  u upload  h keys  q quit
```

`h` lists every key. `c` signs in, `a` browses for files, `t` sets the
destination, `n` negotiates, `x` drops a column, `u` uploads and commits.

Requests run on a worker thread and the screen keeps redrawing while they do, so
a multi-gigabyte write shows a byte counter rather than a frozen terminal.
Quitting with a contract still open abandons it — nothing written was ever
readable, so there is nothing to undo.

### Signing in

Starting with no credentials opens the screen rather than refusing. `c` asks for
your access token username, then the access token, which is masked as you type.
It exchanges them straight away, so a mistyped token is a line on the status bar
rather than a 401 half way through negotiating. It never asks for a bearer JWT.
Neither is written anywhere; set them in the environment to skip the prompt.

### Choosing files

`a` opens a browser:

```
 ADD FILES  ~/exports/2026-08
 ›  ..
    part-0000.parquet   412.9 MB
  ✓ part-0001.parquet   398.1 MB
  ✓ part-0002.parquet   401.7 MB
    SHA256SUMS               64 B

 2 to add
 ↑↓ move  ⏎ open  ← up  space tag  a all here  g type a path  . hidden  esc back
```

The cursor always starts on the top row, so a sequence of keys means the same
thing in every directory. Space tags, `a` tags every readable file here, and
tagging survives walking into another directory — one upload can gather from
several. Files the service cannot read are listed and dimmed rather than hidden;
an empty directory is the one answer that sends you looking in the wrong place.
Anything already on the list shows as tagged and is not offered twice.

`g` types a path, a folder or a glob instead — still the fastest way in when the
path is already on your clipboard, and the only way to say `**/*.parquet`.

Colours are the Alucard palette, defined once in `opteryx_upload/cli/render.py`
and read by the printed output too. Truecolor when the terminal advertises it,
the nearest xterm-256 index when it does not. `NO_COLOR` turns it all off.

The TUI needs `curses`, which is in the standard library everywhere except
Windows; there, `pip install windows-curses`, or use `push`.

## The Python SDK

```python
from opteryx_upload import ContractClient, PATAuthenticator, Schema, Target

client = ContractClient(
    token=PATAuthenticator(client_id="<username>", client_secret="<access token>"),
)

contract = client.negotiate(
    Target("acme", "security", "findings"),
    ["findings.parquet", "more_findings.parquet"],
    Schema.auto(),
)

for entry in contract.plan:
    print(entry)          # source_ip: VARCHAR -> IPV4 (cast)

if contract.blocking:
    raise SystemExit(contract.issues)

if contract.state == "proposed":
    contract.accept()

contract.write_all(["findings.parquet", "more_findings.parquet"])
result = contract.commit(message="nightly load")
print(result.table, result.commit_id, result.rows_written)
```

Negotiating uploads nothing. Each file is sampled locally — a prefix for text,
the *footer* for parquet, which is where its schema lives — so it costs
megabytes whatever the files weigh. Every file is sampled, not just the first:
one contract covers all of them, so two that disagree are caught here rather
than at commit.

### Where the schema comes from

There is no default. Omitting it is a `TypeError` at the call site, not a quiet
inference.

```python
Schema.auto()                  # work it out from the destination
Schema.inferred()              # read the types from the data, and show me first
Schema.of_dataset("append")    # use the types the dataset already declares
Schema.declared({"source_ip": "IPV4", "published": "TIMESTAMP[us]"})
```

`auto` is not a fourth source of types; it asks the service to look up something
it already knows, and the contract that comes back names the mode it resolved
to. A dataset that declares its columns has nothing to infer, and one that does
not exist has nothing to read.

`of_dataset("overwrite")` replaces the rows the dataset resolves to and leaves
its definition exactly as the catalog holds it — a dataset defined as IPV4 is
still IPV4 afterwards.

### Reading the plan before accepting it

A contract from `Schema.inferred()` arrives `proposed` and refuses writes until
it is accepted, so a script that never looks at what was inferred fails loudly
instead of cataloguing a guess.

```python
contract.values          # {"source_ip": "10.4.19.7"} - one real value per column
contract.plan            # PlanEntry(column, from_, to, action)
contract.issues          # Issue(code, column, detail, severity)
contract.blocking        # True when something must be resolved first

contract.retype(source_ip="IPV4", published="TIMESTAMP[us]")
contract.ignore("score")     # read it, do not write it
contract.accept()
```

`PlanEntry.action` is `keep`, `retag`, `widen`, `cast`, `unsupported`,
`undeclared` or `ignored`. `entry.changes_values` is the distinction worth
reading for: a column relabelled IPV4 and a column having every value multiplied
by a thousand are both one line of a table, and only one of them is worth
stopping for.

An amended inference is a declaration — you looked at it and said what you
wanted — so the contract returns to `proposed` and has to be accepted again.
`accept()` echoes back the fingerprint you were shown, so a proposal that moved
between being read and being accepted is refused rather than confirmed blind.

### Everything at once

```python
client.load(
    ["findings.parquet"],
    Target("acme", "security", "findings"),
    Schema.declared({"cve_id": "VARCHAR", "source_ip": "IPV4"}),
    message="nightly load",
)
```

`schema` is required here too. A load that chose its own types because nobody
said otherwise is the thing this design exists to prevent, and making the
convenience wrapper the exception would defeat it.

### Errors

One exception per error code, each carrying its fields, so a caller branches on
a field rather than matching English in a message.

```python
from opteryx_upload import ContractStale, ValueNotCastable

try:
    contract.write("findings.parquet")
except ValueNotCastable as error:
    print(error.column, error.row, error.value, error.declared)
except ContractStale as error:
    print(error.diff, error.written_rows)   # re-negotiate; retrying works
```

`ValueNotCastable` is raised on the write that carries the bad value, naming the
row — not at commit after everything has been sent. `ContractStale` means the
target's definition moved after the contract was agreed; nothing was published,
so the cost is work rather than a dataset somebody has read.

`InternalError` carries `reference`, the id the service logged its traceback
against. Others: `SchemaSourceRequired`, `ColumnUndeclared`, `ColumnMissing`,
`SourcesDisagree`, `ContractNotAccepted`, `ProposalChanged`, `ContractExpired`,
`AlreadyCommitted`, `DatasetExists`, `FormatUnreadable`, `NotAuthorized`,
`ContractNotFound`.

### Reattaching, and progress

```python
contract = client.contract("ct_20260819180247_b47d7241786f")
contract.write("big.parquet", progress=lambda sent, total: print(sent, total))
contract.commit(message="retry", idempotency_key="nightly-2026-08-19")
```

Commit is idempotent on `idempotency_key`: a retry after a lost response returns
the original snapshot instead of writing a second one.

## Parts held in memory

For a producer that never writes a part to disk - a log gateway whose only disk
is its WAL, say. `negotiate` takes `samples` instead of `files`, and `Contract`
gains `write_bytes`:

```python
import json, zstandard
from opteryx_upload import ContractClient, Schema, Target

part = zstandard.ZstdCompressor().compress(
    b"\n".join(json.dumps(record).encode() for record in records)
)
name = "gateway-0000.ndjson.zst"

client = ContractClient(token=auth)
contract = client.negotiate(
    Target("acme", "security", "findings"),
    schema=Schema.declared({"cve_id": "VARCHAR", "source_ip": "IPV4"}),
    samples=[(name, part[:4 * 1024 * 1024])],
)
contract.write_bytes(part, name, content_type="application/x-ndjson")
contract.commit(message="nightly", idempotency_key=batch_id)
```

The name carries the format exactly as a path does, codec suffix included:
`.ndjson.zst` is NDJSON that happens to be zstd, and the service decodes it.
gzip, zstd, brotli and DEFLATE all work; the SDK declares `Content-Encoding`
from the suffix, which matters because gzip and zstd can be identified from
their leading bytes and brotli and raw DEFLATE cannot.

`content_type` is sent as given, so `application/x-ndjson` is a second,
independent way for the service to get the format right if the name is ever
wrong.

Exactly one of `files` or `samples`. `write()` still streams from disk and is
not `write_bytes(open(path, "rb").read())` - streaming a four gigabyte parquet
file is the right thing and stays.

## Authenticating with an access token

`PATAuthenticator` exchanges an access token (a username and the token itself)
for a short-lived assertion, caches it, and re-authenticates before it expires.
Pass it straight through as `token=` — it is callable, and it is re-resolved per
request.

```python
from opteryx_upload import ContractClient, PATAuthenticator

auth = PATAuthenticator(client_id="<username>", client_secret="<access token>")
client = ContractClient(token=auth)
```

This uses `POST {auth_url}/token` with `grant_type=client_credentials` (default
`auth_url` is `https://authenticate.opteryx.app`), the same flow as the
`opteryx-sqlalchemy` driver. If the API ever rejects a token as expired, call
`auth.invalidate()` and retry to force a fresh exchange.

A plain string works too, but a bearer JWT lives about five minutes and an
upload can take longer than that.

## Authenticating from GitHub Actions, without a secret

`GitHubOIDCAuthenticator` is `PATAuthenticator` with nothing stored. GitHub
mints a short-lived, signed token describing the running workflow, the
authenticate service verifies it and matches it to a repository registered
against a client, and hands back the same assertion the access-token flow
would have. No `UPLOAD_CLIENT` / `UPLOAD_TOKEN` repository secrets, nothing to
rotate.

```python
from opteryx_upload import ContractClient, GitHubOIDCAuthenticator

client = ContractClient(token=GitHubOIDCAuthenticator())
```

The job has to grant itself the right to ask GitHub for a token:

```yaml
jobs:
  scan:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write    # without this there is no token to present
    steps:
      - uses: actions/checkout@v4
      - run: python scripts/upload_scan_results.py
```

`id-token: write` is what sets `ACTIONS_ID_TOKEN_REQUEST_URL` and
`ACTIONS_ID_TOKEN_REQUEST_TOKEN` in the job environment; the authenticator
reads those. Adding a `permissions:` block narrows the job to exactly what it
lists, so keep `contents: read` if the job checks out code.

The repository must be registered against a client first - which client, and
what it may do, is decided there, not here. See
`authenticate.opteryx/scripts/register_federated_credential.py`.

`audience=` defaults to `https://authenticate.opteryx.app` and must match what
the authenticate service accepts; it is what stops a token GitHub minted for
some other relying party being replayed. Everything else matches
`PATAuthenticator` - callable, cached, same `invalidate()` - so `token=` takes
either.

For a script that runs both in Actions and on a laptop, ask before committing:

```python
from opteryx_upload import GitHubOIDCAuthenticator, PATAuthenticator

if GitHubOIDCAuthenticator.is_available():
    auth = GitHubOIDCAuthenticator()
else:
    auth = PATAuthenticator(client_id=..., client_secret=...)
```

## Authenticating from GCP, without a key

`GoogleWorkloadAuthenticator` is the same trade on GCP. Anything running as a
service account — Cloud Run, a GCE VM, a Cloud Function, a GKE pod with
Workload Identity — can ask the metadata server for a signed token saying
which service account it is. No key file, nothing to rotate.

```python
from opteryx_upload import ContractClient, GoogleWorkloadAuthenticator

client = ContractClient(token=GoogleWorkloadAuthenticator())
```

The service account must be registered against a client first, and it is
matched on its immutable numeric id, not its email — delete a service account
and recreate it with the same name and the email comes back, the id does not.
See `authenticate.opteryx/scripts/register_federated_credential.py`.

`is_available()` here makes a short request to the metadata server rather than
reading an environment variable: GCP sets nothing that reliably means "you are
on GCP with a service account". Off GCP it returns False quickly, and calling
the authenticator anyway raises with a message saying so.

## The session flow

`UploadClient` and the `/v1/upload` endpoints still work, so nothing that uses
them breaks. They are no longer documented and no longer where new work should
start: they infer types from the data and report what they found at inspect,
which is after the upload rather than before it. See the git history of this
file for the version that described them.

## Notes

- Files are typed from their extension: `.parquet`/`.pq`, `.csv`,
  `.ndjson`/`.jsonl`. Anything else is refused by name rather than guessed at.
- `token` may be a string or a zero-arg callable, resolved per request, so a
  short-lived assertion can be refreshed transparently between calls.

## Development

```bash
pip install -e ".[dev]"
pytest tests/
```
