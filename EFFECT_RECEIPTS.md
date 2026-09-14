# External-effect receipt ledger

`effect_receipts.py` closes one narrow gap left deliberately open by the LDA task lease/CAS protocol: a worker can lose process state after an irreversible device/provider action but before it publishes task completion. A later lease generation must not interpret that uncertainty as permission to repeat the action.

## Rule

The caller chooses a stable logical `task_id + effect_id` before an irreversible action. `prepare` binds that identity to the operation, canonical JSON payload SHA-256, and exact lease tuple `(worker_id, lease_id, attempt)`. The returned private token is written create-exclusively to a mode-0600 file and only its SHA-256 is stored in SQLite.

Immediately before the real external mutation, call `dispatch`. It commits `DISPATCHED` in a `BEGIN IMMEDIATE` transaction. The external action happens **after** that durable marker and outside this program.

If the first dispatch call returns but the caller loses its external outcome, do not call dispatch again. A repeated dispatch call, or a later lease generation attempting the same effect after `DISPATCHED`, moves the row to `RECONCILIATION_REQUIRED` and refuses retry. A late authoritative external receipt may still resolve that row to `SUCCEEDED` or `FAILED_FINAL` with its receipt reference and SHA-256.

`SUCCEEDED` means only “the caller supplied a receipt under the exact retained effect token.” It does not authenticate the provider, prove payment/delivery, authorize work, or recognize revenue.

## Example

```sh
python effect_receipts.py --db .local/effects.sqlite prepare \
  --task-id task-42 --effect-id email-lead-acme --operation send_email \
  --payload-file payload.json --worker-id ZKR-F9T6 --lease-id lease-42 \
  --attempt 3 --token-out .local/effect.token.json

# Durable marker first. Only after this succeeds may the surrounding worker
# invoke the real external action.
python effect_receipts.py --db .local/effects.sqlite dispatch \
  --token-file .local/effect.token.json

python effect_receipts.py --db .local/effects.sqlite succeed \
  --token-file .local/effect.token.json \
  --receipt-ref provider:message-123 \
  --receipt-sha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
```

## State machine

- `PREPARED`: exact intent is durably reserved; external action must not have happened yet.
- `DISPATCHED`: durable pre-action marker exists; outcome is not yet recorded.
- `RECONCILIATION_REQUIRED`: retry is forbidden because an earlier dispatch may have taken effect.
- `SUCCEEDED`: bound external success receipt recorded.
- `FAILED_FINAL`: bound external final-failure receipt recorded.

There is intentionally no automatic reset from an unknown dispatched outcome. An operator/provider-specific reconciliation workflow can decide whether to mint a **new logical effect id** only after it has authoritative evidence that doing so is safe.

## Authority ceiling

The ledger performs no device, network, provider, customer, payment, repository, or account mutation beyond its own local SQLite/token files. It does not grant task lease authority and cannot make an untrusted provider receipt authoritative. It is a fail-closed duplicate-action barrier, not an exactly-once external transaction protocol.
