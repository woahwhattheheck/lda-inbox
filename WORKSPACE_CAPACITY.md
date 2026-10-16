# Operator workspace capacity audit

`inbox_workspace_capacity.py` gives an operator a bounded, read-only view of
storage pressure before another import or complete-workspace archive. It reports
database allocation, free pages, core row counts and retained payload totals.
It emits no workspace, task, request, worker or lease identifiers and no labels,
commands, results or retained JSON.

```sh
python inbox_workspace_capacity.py --db /private/operator.sqlite
```

The default logical database ceiling is 256 MiB, matching the complete archive
tool. The command exits 0 when no pressure signal is present, 2 when a threshold
or storage-maintenance warning is present, and 1 when inspection fails closed.
Use explicit limits for a larger operating envelope:

```sh
python inbox_workspace_capacity.py --db /private/operator.sqlite \
  --max-bytes 1073741824 --warn-at 0.85 --timeout 30
```

The report distinguishes the main database file, non-empty SQLite WAL/SHM
sidecars, logical page allocation, used pages and free pages. A large WAL is a
checkpoint-maintenance signal; substantial free pages indicate reusable space,
not missing retained history. Neither warning authorizes deleting editions,
requests or source records. Back up with `inbox_workspace_archive.py` before any
ordinary database maintenance.

Inspection uses a single read-only SQLite transaction, `quick_check`, exact core
column checks, a 250,000-row ceiling per core table and a 10-second default time
bound. The deterministic report includes a digest of the report and a digest of
the desk scope only; those hashes provide internal association, not source
authentication. The command grants no task, lease, publication or provider
authority and never changes the selected database.
