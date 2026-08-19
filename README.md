# opteryx-upload

Python client SDK for the [Opteryx Upload Service](https://github.com/mabel-dev/upload.opteryx).

## Install

```bash
pip install opteryx-upload
```

Parts are compressed before upload. gzip comes from the standard library, so that
works out of the box; installing the `zstd` extra adds a denser and faster codec,
which the SDK then selects automatically:

```bash
pip install "opteryx-upload[zstd]"
```

## The command line

Installing the package puts `opteryx-upload` on your PATH.

```bash
export OPTERYX_CLIENT_ID="<client id>"
export OPTERYX_CLIENT_SECRET="<pat secret>"
opteryx-upload push findings.csv --to acme.security.findings
```

You are not asked where the schema comes from, because the destination answers
it. A dataset that already declares its columns supplies them, and the only
question left is whether these rows are added or replace what is there. A
dataset that does not exist yet has its types read from your data and shows them
to you before anything is written:

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

Nothing is uploaded until that is settled. `published` and `source_ip` are the
reason: a CSV cannot say that a column of dotted quads is an address, and once
it is catalogued as VARCHAR no amount of reading the data back will tell you it
was wrong.

### In a pipeline

There is no terminal to show the table to, so inference has to be authorised in
advance - `--yes` accepts what was read from the data, and `--declare` says the
types outright. A `push` with neither is refused rather than guessed at.

```bash
opteryx-upload push data/*.parquet --to acme.security.findings \
    --type published=TIMESTAMP --type source_ip=IPV4 \
    --message "nightly load" --yes
```

`plan` does the same negotiation, prints the table and abandons the contract, so
it uploads nothing and leaves nothing behind:

```bash
opteryx-upload plan data/*.parquet --to acme.security.findings --json
```

Exit codes are part of the interface, because a pipeline that has to grep stderr
will eventually retry the wrong thing:

| code | meaning | retrying |
|---|---|---|
| 0 | committed | - |
| 2 | bad arguments, missing file, no credentials | no |
| 3 | the service refused it: a value that will not cast, files that disagree | no |
| 4 | the target moved after the contract was agreed | yes |
| 5 | not signed in, or not permitted to write here | no |
| 6 | the service could not be reached | yes |
| 130 | interrupted | - |

### Options

| | |
|---|---|
| `--to WORKSPACE.COLLECTION.DATASET` | where the rows go (required) |
| `--append` / `--overwrite` | for a dataset that exists; asked if you are at a terminal |
| `--type COLUMN=TYPE` | correct one type without a prompt; repeatable |
| `--ignore COLUMN` | read this column and do not write it; repeatable |
| `--infer` / `--use-dataset` / `--declare COLUMN:TYPE` | override the destination's answer |
| `-y`, `--yes` | accept inferred types unasked; required off a terminal |
| `--json` | the contract as the service sent it |

### Credentials

Set `OPTERYX_CLIENT_ID` to your access token username and `OPTERYX_CLIENT_SECRET`
to the access token. `OPTERYX_TOKEN` takes a bearer JWT instead; an access token
in the environment wins over one, and `--token` wins over both.

The service comes from `OPTERYX_UPLOAD_URL` and the authenticate service from
`OPTERYX_AUTH_URL`. Each has a flag if you would rather pass it.

## The full-screen version

Run it with no arguments at a terminal and this is what you get - typing the
name and nothing else means you want to upload something, not to read a list of
subcommands. Off a terminal, no arguments prints the usage instead.

```bash
opteryx-upload                                             # a is add, t is target
opteryx-upload tui findings.csv --to acme.security.findings  # or start it filled in
```

Same contract, same calls - what it adds is that the table stays put. At a
scrolling prompt the plan goes past once and correcting a type means retyping
the whole command; here the cursor moves down it and `e` changes the type of the
row under the cursor.

```
 opteryx upload                                              http://upload.opteryx.app

 FILES
   findings.csv       686.5 KB
   findings_more.csv  457.7 KB

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
 ↑↓ column  e retype  x ignore  ⏎ accept  u upload  r re-plan  q quit
```

`h` lists every key. `c` signs in, `a` adds files, `t` sets the destination,
`n` negotiates, `x` drops a column, `u` uploads and commits.

`a` takes a path, a folder, or a glob - `data/*.parquet`, or the folder itself.
A glob or a folder only picks up files the service can read, so `data/*` does
not drag a README and a checksums file into the upload, and adding the same
pattern twice does not double the list.

Starting it with no credentials opens the screen rather than refusing: press `c`
and it asks for your access token username, then the access token, which is
masked as you type. It exchanges them straight away, so a mistyped token is a
line on the status bar rather than a 401 half way through negotiating. It never
asks for a bearer JWT. Neither is written anywhere - they are held for the
session; set them in the environment to skip the prompt. Requests run on a worker thread and the screen keeps
redrawing while they do, so a multi-gigabyte write shows a byte counter rather
than a frozen terminal. Quitting with a contract still open abandons it - nothing
written was ever readable, so there is nothing to undo.

It needs `curses`, which is in the standard library everywhere except Windows;
there, `pip install windows-curses`, or use `push`.

## Usage

```python
from opteryx_upload import UploadClient, Target, ConflictResolution

client = UploadClient(token="<jwt>")  # or token=lambda: fetch_fresh_token()

session = client.create_session()
session.upload_file("findings.parquet")
session.upload_file("more_findings.csv")  # compressed, and auto-split if still too big

result = session.inspect()
if result.has_issues:
    raise SystemExit(result.issues)

commit = session.commit(
    Target(workspace="acme", collection="security", dataset="findings"),
    snapshot_message="Initial load",
    conflict_resolution=ConflictResolution.APPEND,
)
print(commit.table, commit.commit_id, commit.rows_written)
```

Or in one call:

```python
client.upload_and_commit(
    ["findings.parquet"],
    Target("acme", "security", "findings"),
    snapshot_message="Initial load",
)
```

## Authenticating with an access token

If you have an access token (a username + the token itself) instead of a
ready-made JWT, use `PATAuthenticator` to exchange it for a short-lived
assertion. It caches the
token and transparently re-authenticates before it expires, so you can pass it
straight through as `token=`:

```python
from opteryx_upload import UploadClient, PATAuthenticator

auth = PATAuthenticator(client_id="<username>", client_secret="<access token>")
client = UploadClient(token=auth)
```

This exchanges the access token via `POST {auth_url}/token` with `grant_type=client_credentials`
(default `auth_url` is `https://authenticate.opteryx.app`), the same flow used by
the `opteryx-sqlalchemy` driver. If the API ever rejects a token as expired/invalid,
call `auth.invalidate()` and retry to force a fresh exchange.

## Examples

Each `UploadSession` maps directly onto the service's REST flow: create a session,
stage one or more parts, inspect them, then commit. See the
[service README](https://github.com/mabel-dev/upload.opteryx#flow) for the underlying
HTTP API these calls wrap.

### End-to-end: upload and commit a dataset

```python
from opteryx_upload import UploadClient, Target, ConflictResolution

client = UploadClient(token="<jwt>")

session = client.create_session()
print(session.info.session_id, session.info.expires_at)  # sessions expire after 6 hours

session.upload_file("findings.parquet")
session.upload_file("more_findings.parquet")

result = session.inspect()
print(result.rows_estimate, result.schema)
if result.has_issues:
    for issue in result.issues:
        print(f"part {issue.part}: {issue.issue}")
    raise SystemExit("fix the reported issues before committing")

commit = session.commit(
    Target(workspace="acme", collection="security", dataset="findings"),
    snapshot_message="Initial load of findings",
    conflict_resolution=ConflictResolution.FAIL,  # default: error if the dataset already exists
)
print(f"committed {commit.rows_written} rows across {commit.files_created} files as {commit.commit_id}")
```

### Choosing a conflict resolution strategy

- `ConflictResolution.FAIL` (default) — reject the commit if the dataset already exists.
- `ConflictResolution.APPEND` — add the new rows to the existing dataset (schemas must match).
- `ConflictResolution.OVERWRITE` — replace the existing dataset's contents entirely.

```python
session.commit(
    Target("acme", "security", "findings"),
    conflict_resolution=ConflictResolution.OVERWRITE,
)
```

### Uploading many files, then deciding what to commit

Parts can be staged incrementally (e.g. from multiple upload jobs) before a single
commit, and a bad part can be removed before it's committed:

```python
session = client.create_session()
part_numbers = []
for path in ("2026-01.parquet", "2026-02.parquet", "2026-03.parquet"):
    part_numbers += session.upload_file(path)

result = session.inspect()
if result.has_issues:
    bad_part = result.issues[0].part
    session.delete_part(bad_part)
    result = session.inspect()

session.commit(Target("acme", "security", "findings"))
```

### Handling errors

```python
from opteryx_upload import (
    UploadClient,
    ConflictError,
    SessionExpiredError,
    UnprocessableEntityError,
)

client = UploadClient(token="<jwt>")
session = client.create_session()

try:
    session.upload_file("findings.csv")
    session.commit(Target("acme", "security", "findings"))
except UnprocessableEntityError as exc:
    print(f"file rejected: {exc}")
except ConflictError as exc:
    print(f"commit conflict, consider ConflictResolution.APPEND/OVERWRITE: {exc}")
except SessionExpiredError:
    session = client.create_session()  # start over with a fresh session
```

### One-shot upload

For simple jobs where you just want to push files straight into a table:

```python
client.upload_and_commit(
    ["findings.parquet"],
    Target("acme", "security", "findings"),
    snapshot_message="Initial load",
)
```

### Authenticating with an access token end-to-end

```python
from opteryx_upload import UploadClient, PATAuthenticator, Target

client = UploadClient(
    token=PATAuthenticator(client_id="acme-etl", client_secret="opt_XXXXXXXX_01"),
)
client.upload_and_commit(["findings.parquet"], Target("acme", "security", "findings"))
```

## Notes

- Files are auto-typed from their extension (`.parquet`, `.csv`, `.ndjson`/`.jsonl`).
- CSV and NDJSON files larger than the part size limit are automatically split into
  multiple parts (CSV chunks repeat the header row). Parquet is a binary format and
  cannot be split this way — write multiple smaller parquet files and upload each as
  a separate part if a single export is too large.
- CSV and NDJSON parts are compressed before upload and sent with `Content-Encoding`.
  `compression="auto"` (the default) uses zstd when `zstandard` is installed and gzip
  otherwise; pass `"gzip"`, `"zstd"` or `None` to choose explicitly. Parquet is never
  compressed — it already is, internally.

  This matters more than bandwidth: the server's 30MB part limit applies to the
  *compressed* bytes, so a compressed part carries far more rows and a large file
  needs far fewer parts. A 55MB NDJSON export goes from 2 parts to 1 at ~7x. Parts
  are also bounded by `max_source_bytes` (default 190MB), because the server decodes
  at most 200MB per part.
- Errors map to typed exceptions (`AuthenticationError`, `SessionExpiredError`,
  `ConflictError`, `UnprocessableEntityError`, etc.) so callers can catch specific
  failure modes instead of parsing HTTP status codes.
- `token` may be a plain string or a zero-arg callable, so short-lived JWTs can be
  refreshed transparently between requests.

## Development

```bash
pip install -e ".[dev]"
pytest tests/
```
