# External-effect receipt ledger

`effect_receipts.py` is a fail-closed duplicate-action barrier downstream of the
repository-backed task lease protocol. It does **not** perform the external
action. It records one logical effect, publishes a private generation-bound
token durably, and refuses the `PREPARED -> DISPATCHED` transition unless the
same lease generation is still live in the repository's canonical `inbox.json`.

## Required ordering

1. The task publisher claims a lease through `task_protocol.py`, publishes the
   candidate with repository CAS, and re-reads the published `inbox.json`.
2. Compute the semantic document SHA-256 from that exact re-read.
3. `prepare` re-reads the canonical sibling `inbox.json` itself. The requested
   `(task_id, worker_id, lease_id, attempt)` must be the exact live lease and its
   task digest must match. An alternate caller-selected inbox path is not
   accepted.
4. `prepare` first records `TOKEN_PENDING`. The private token is then fully
   written, fsynced, byte-read back, and published create-exclusively at the
   requested token path. Only after that complete artifact is visible does the
   ledger advance to `PREPARED`.
5. Immediately before the real irreversible action, call `dispatch` with the
   token and a fresh semantic digest from the current published `inbox.json`.
   `dispatch` independently re-reads that canonical file while holding the
   ledger transition lock. Only the exact still-live lease generation can move
   `PREPARED -> DISPATCHED`.
6. The surrounding caller may invoke the external action **only after** a
   successful `DISPATCHED` result. Record a bound provider/operator receipt with
   `succeed` or `fail-final` afterward.

The `--now` value and the repository publication/re-read step remain trusted
coordinator inputs, exactly as documented by `TASK_PROTOCOL.md`. The effect
ledger does not replace the repository CAS publisher.

## Token publication and crash recovery

`TOKEN_PENDING` is deliberately non-authoritative. It closes the crash window
where a database reservation existed but the sole usable secret had not been
published.

The token writer creates a private staging inode in the target directory, loops
until every byte is written, fsyncs it, verifies exact byte readback and mode,
then hard-links it to the final name with create-exclusive semantics and fsyncs
the directory. A pre-existing final path is never overwritten. Cleanup only
unlinks a staging name when it still resolves to the exact inode created by the
writer.

Recovery rules:

- `TOKEN_PENDING` + no final token file: the exact live lease may rotate the
  unpublished secret and retry publication.
- `TOKEN_PENDING` + complete matching token file: retry finalizes the existing
  artifact to `PREPARED`; it does not mint a second capability.
- `PREPARED` + missing token file: fail closed. The ledger will not regenerate a
  capability that may already have escaped.
- A short write, write/fsync failure, target collision, or interruption before
  finalization cannot yield a partial action-permitting token.

## Lease-generation handoff

A private effect token is bound to the task digest, worker, raw lease id,
`attempt`, and `effect_generation`. `attempt` is the task protocol's ABA fence.

If an older generation reached only `TOKEN_PENDING` or `PREPARED` and a newer
published lease generation takes the same logical effect with identical
operation/payload/task semantics, the ledger rotates to a new effect generation
and invalidates the older token. A stale attempt cannot dispatch, including
same-worker/same-raw-lease reuse under a higher attempt.

Once an effect reached `DISPATCHED`, no later lease is allowed to infer a safe
retry. A repeat dispatch or a competing generation advances/holds
`RECONCILIATION_REQUIRED` until an exact bound external receipt resolves the
outcome.

Use a generation-specific private token pathname for a successor lease, or
remove a known stale unpublished artifact after operator validation. Final-path
publication is always create-exclusive.

## CLI sketch

```sh
SHA="$(python task_protocol.py digest inbox.json)"

python effect_receipts.py --db .local/effects.sqlite prepare \
  --task-id task-42 --effect-id email-lead-acme --operation send_email \
  --payload-file payload.json --worker-id worker-a --lease-id lease-a \
  --attempt 3 --token-out .local/effect-42-attempt-3.token.json \
  --expected-document-sha256 "$SHA" --now 2026-09-14T09:01:00Z

# Re-read the exact current published inbox immediately before dispatch and
# recompute SHA. A stale digest or stale lease generation fails closed.
SHA="$(python task_protocol.py digest inbox.json)"
python effect_receipts.py --db .local/effects.sqlite dispatch \
  --token-file .local/effect-42-attempt-3.token.json \
  --expected-document-sha256 "$SHA" --now 2026-09-14T09:01:10Z
```

## State machine

- `TOKEN_PENDING`: identity is reserved, but no action capability is yet durable.
- `PREPARED`: exact private token publication is complete; no external action has
  happened.
- `DISPATCHED`: live-lease gate passed and the durable pre-action marker exists;
  external outcome is not yet recorded.
- `RECONCILIATION_REQUIRED`: an earlier dispatch may have taken effect; retry is
  forbidden.
- `SUCCEEDED` / `FAILED_FINAL`: an exact bound receipt was recorded.

## Authority ceiling

This program does not contact a device, provider, customer, mailbox, payment
processor, repository host, or other external system. It does not authenticate a
provider receipt, prove payment/delivery, grant a task lease, recognize revenue,
or authorize retry after an unknown external outcome. `SUCCEEDED` means only
that the caller supplied an exact receipt reference/digest under the retained
current effect token.
