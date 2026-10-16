# Operator storage integrity audit

`inbox_storage_integrity.py` checks that an existing local operator-desk database still contains a complete, internally linked retained history. It is complementary to the cross-workspace semantic audit: it checks the SQLite storage contract, not whether two workspaces describe the same task.

## Run

```bash
python inbox_storage_integrity.py --db /private/path/operator.sqlite > storage-integrity.json
```

Exit `0` means the report is clean. Exit `2` means the report contains findings or the database could not be inspected safely. The command opens the selected file with SQLite `mode=ro`, enables `query_only`, disables trusted schema behavior, and holds one read transaction for the complete audit. It does not create a database, checkpoint a WAL, acquire a lease, choose a winning task copy, or change any operator state.

## Checks

- the four maintained core tables have their exact expected columns;
- SQLite quick-check and foreign-key checks pass;
- the operator scope and stored digests use canonical formats;
- every original and retained edition parses through the maintained task protocol;
- every workspace starts at revision 1 with `import`, revisions are contiguous, later imports are rejected, and retained times do not move backward;
- the original document equals revision 1;
- every retained request result points to an actual workspace edition and digest;
- changed transition receipts link the prior and resulting edition, action, and timestamp;
- replayed transition receipts preserve one digest rather than inventing an edition.

The default one-million-row bound can be lowered with `--max-rows`. Per-document and retained-result byte limits prevent unbounded parsing.

## Privacy and authority boundary

The deterministic JSON report contains counts, schema/report hashes, a one-way hash of the workspace scope, finding kinds, revisions, and opaque 20-hex subjects. It never emits workspace IDs or labels, task IDs or commands, request IDs, worker or lease IDs, completion results, or document contents. An opaque subject only correlates repeated findings within the same report.

Findings are evidence for the existing operator. They do not authorize repair, deletion, lease release, task execution, or publication of the private database. Preserve the database and use its maintained operator/protocol path for any separately authorized correction.
