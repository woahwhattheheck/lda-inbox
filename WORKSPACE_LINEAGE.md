# Operator workspace lineage

Use `inbox_workspace_lineage.py` before reopening, copying, or choosing between
two complete LDA operator databases. It answers one narrow operational question:
is the candidate an exact copy, a safe forward continuation, or a rollback or
rewrite of retained work?

The command is read-only. It does not merge databases, repair task state, grant a
lease, replay a request, or replace the maintained task protocol and storage
integrity audit. Python 3.10 or newer and the standard-library SQLite module are
sufficient.

```sh
python inbox_workspace_lineage.py \
  --baseline /private/older/operator.sqlite \
  --candidate /private/candidate/operator.sqlite
```

Both files are opened with SQLite `mode=ro`, `query_only`,
`trusted_schema=OFF`, foreign-key checking, a single read snapshot, and bounded
row/time work. The comparator verifies the four maintained core tables, ordinary
schema objects, database integrity, desk scope, contiguous edition revisions,
and retained replay metadata before comparing anything.

## Classifications

- `equal`: every retained workspace edition and request from the baseline is
  byte-for-byte represented by the same digest/chronology metadata in the
  candidate, with no new retained rows.
- `forward`: the baseline is an exact prefix and the candidate only adds
  workspaces, editions, or requests. This is the normal continuation result.
- `diverged`: the candidate removes a workspace/request, shortens an edition
  history, or rewrites retained edition/request metadata. Do not treat it as a
  safe continuation without resolving the original records.
- `unrelated`: the desk scopes differ. The files may both be valid, but one is
  not a continuation of the other.

Exit status is zero only for `equal` and `forward`. Diverged, unrelated, invalid,
damaged, over-limit, or timed-out inputs return status 2.

The deterministic JSON report contains only counts, classifications, revisions,
and scope-bound SHA-256 subjects. It never emits workspace IDs, labels, task
documents, request IDs/fingerprints/results, workers, leases, commands, or
customer content. `report_sha256` lets another operator compare two reports made
over the same inputs.

Use `inbox_storage_integrity.py --db ...` when you need the deeper per-desk
protocol/storage audit. Use `inbox_workspace_archive.py backup` to take a
transactionally consistent complete snapshot before moving a running desk.
