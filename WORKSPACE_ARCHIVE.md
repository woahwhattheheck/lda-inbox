# Complete operator workspace archives

Use `inbox_workspace_archive.py` to move or back up an entire local operator desk.
The archive retains every workspace, original inbox, saved edition, request
fingerprint and result, plus the desk scope. It complements the operator's
individual inbox JSON exports. It does not replace the operator or change task
states.

Python 3.10 or newer with the standard-library SQLite module is sufficient.
The archive command needs no phone, browser, network, credentials or additional
Python packages.

## Save the complete desk

```sh
python inbox_workspace_archive.py backup \
  --db /private/operator.sqlite \
  --archive /private/Inbox_Workspace.zip
```

The operator may remain open. The command uses SQLite's online backup API, not a
copy of the live database file, so committed data in a write-ahead log is included
in one consistent snapshot. Later edits stay in the running desk and belong to
its next backup. The original database is opened read-only.

The ZIP has just two members: `operator.sqlite` and `manifest.json`. The manifest
records the database size and digest, desk scope, workspace/edition/request
counts and table inventory. Commands, originals and saved results remain inside
the complete database, so keep this archive with your private working files.

## Inspect and restore

```sh
python inbox_workspace_archive.py inspect --archive /private/Inbox_Workspace.zip

python inbox_workspace_archive.py restore \
  --archive /private/Inbox_Workspace.zip \
  --db /private/restored/operator.sqlite

python inbox_operator.py --db /private/restored/operator.sqlite
```

Open the URL printed by the maintained operator. All retained workspaces and
editions are available with their original identifiers. Original request
fingerprints/results are retained for the operator's existing replay behavior.
The launcher creates its normal new connection token; browser-local unfinished
forms are not part of the database archive.

Restore requires a new database path and also refuses existing SQLite sidecars.
It never merges into, rolls back or replaces a running desk. The archive is read
completely, its exact database bytes and retained summary are checked, and the
validated file is published only at the selected new path. Backup similarly
requires a new archive filename. A concurrent file creation wins rather than
being overwritten.

## Size and operational limits

The default database limit is 256 MiB and the default per-operation receiving or
SQLite-work limit is 30 seconds. Raise either explicitly when needed:

```sh
python inbox_workspace_archive.py backup --db /private/operator.sqlite \
  --archive /private/Inbox_Workspace_Large.zip \
  --max-bytes 1073741824 --timeout 120
```

Use the same larger byte limit when inspecting or restoring that archive. The
command stages private files beside the output and needs space for both the
snapshot and compressed archive. The destination filesystem must support hard
links for create-only publication. This works without moving staging files
across filesystems.

Version 1 recognizes the maintained `meta`, `workspaces`, `editions` and `requests`
tables. Additional ordinary tables and indexes are preserved, not discarded.
Views, triggers, virtual tables or changed core columns require an updated
archive reader; the source database stays intact. An integrity check is a
corruption check, not a replacement for the operator's task protocol. Normal
current-time lease checks still apply after reopening.

Implementation reference: Python's
[SQLite backup API](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup).
