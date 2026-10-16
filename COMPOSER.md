# Inbox authoring desk

`inbox_compose.py` adds a local, browser-based authoring surface to the retained
operator desk. It does not publish an inbox, claim work, contact a provider or
execute a task.

## Launch

```sh
python inbox_compose.py --db /private/path/operator.sqlite --port 8765
```

Open the exact loopback URL printed by the launcher. The authoring page and the
existing operator desk share the selected SQLite database. `--demo` retains the
maintained three-task example once before opening the composer.

## Complete workflow

1. Name the future operator workspace and the inbox document.
2. Add pending `run_task` records. Edit IDs, commands, timestamps and task
   timeouts; reorder or remove tasks as needed.
3. Leave and reopen the page. The unfinished draft is retained in browser-local
   storage scoped to the selected operator database.
4. Resolve the native validation message. The server applies the maintained
   protocol and returns canonical JSON, byte count and semantic document digest.
5. Download the canonical JSON, or choose **Save and open workspace**. Saving
   uses the existing retained `/api/import` request and opens the new workspace
   in the ordinary operator desk.
6. If the connection is lost after dispatch, reopen the same launcher and use
   **Recover exact save**. The retained request ID and body are replayed through
   the desk's existing idempotent request store.

A pending authored task has exactly these fields:

```json
{
  "id": "review-layout",
  "created": "2026-10-16T14:00:00Z",
  "kind": "run_task",
  "command": "Review the complete local demonstration layout",
  "timeout_s": 3600,
  "done": false
}
```

The authoring boundary rejects completion fields, execution state, unknown task
fields, duplicate IDs, unsupported kinds, malformed timestamps, non-positive or
oversized timeouts, empty commands, too many tasks and documents beyond the
maintained inbox limit.

## Authority boundary

The new workspace is a local retained candidate. Shared task authority still
requires the existing GitHub publisher's exact ref/blob compare-and-swap and a
reread of the published lease before device execution. The composer adds no
provider credentials, device route, automatic publish or automatic execution.
