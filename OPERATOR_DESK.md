# Inbox operator desk

The local desk makes the maintained inbox protocol usable in a browser. Open a
JSON inbox, find a task, inspect its worker and lease generation, stage a claim,
renewal, handoff or completion, then download the complete current edition.
Every changed edition and the exact imported source stay in the selected SQLite
workspace. The desk uses the existing `task_protocol.py` and `validate_inbox.py`;
it does not maintain a second ownership state machine.

## Start

Python 3.10 or newer is required. There are no third-party dependencies.
From this repository, run:

```sh
python inbox_operator.py --db /path/to/private/operator.sqlite
```

On Windows, use a private writable path, for example
`python inbox_operator.py --db "%LOCALAPPDATA%\LDA\operator.sqlite"`.
Open the full local address printed by the launcher. The fragment connects this
browser tab to the running desk. The default port is 8765. Keep the same port
and database when restarting; use `--port` to choose another fixed port.

For an isolated authored walkthrough, select a new database:

```sh
python inbox_operator.py --db ./operator-demo.sqlite --demo
```

The three demonstration tasks are local examples. The same demo is imported
only once, so restarting never resets its completed work. For ordinary work,
start without `--demo` and use **Open inbox JSON** to select your own file. The
application does not automatically read the repository's live `inbox.json`.

## Operate

1. **Open and review.** Import a complete inbox JSON file. Its name becomes the
   workspace label unless you supply another. Each import is a separate local
   workspace; importing an older document never rolls back another workspace.
   Select a task or search its ID, command and current worker. Filter available,
   active, future, expired and completed tasks.
2. **Stage the transition.** Enter your own worker and lease IDs. The desk never
   substitutes another worker's identity. Select claim, renew, release or
   complete. Renewals and completions bind the displayed lease generation;
   every action also binds the exact visible document digest. The local host's
   clock supplies the transition instant. Stale editions and wrong identities
   produce a readable error without replacing the current edition.
3. **Continue and hand off.** Release an owned live task with a note, then claim
   it with the receiving worker and a new lease. The maintained protocol
   advances its generation. Complete with the exact current owner, a completion
   ID and the actual result. **Download current JSON** retains the entire inbox,
   including all specifications, metadata and completion data.

The displayed states are a local scheduling view, not device actions. Shared
execution still uses the existing `inbox_github.py` publication flow: reread the
current repository, stage against its exact source, publish with the original
repository compare-and-swap, and reread the resulting authority. Do not replace
a shared inbox wholesale with an old local staging export. The desk deliberately
keeps provider credentials, task execution and external effects out of its HTTP
interface.

## Recovery and history

The browser tab retains the complete request before sending it. SQLite commits
the new edition and that request's exact outcome in one transaction. A dropped
response leaves **Recover exact request** available, including after reloading
the tab. Replaying the same request returns its original outcome; reusing its
ID for different content is rejected. Recovery then loads the latest workspace,
so an old receipt cannot replace newer work on screen.

Restart with the same database and port and open the newly printed URL in the
same browser tab to reconnect. Browser session storage is scoped to that tab
and origin. Closing the tab, clearing its storage, or changing ports can remove
its pending-request copy; the committed editions and request records remain in
the database. Use **Refresh** and **Retained editions** to inspect saved work
before preparing another operation. Reads and JSON downloads remain possible
when browser request storage is unavailable; new transitions are not dispatched
until their request can be retained.

History is shown in pages of 30 editions with **Show older editions**. Each
edition can be downloaded independently. **Download imported original** returns
the exact JSON text selected during browser import. Neither action changes the
current workspace. Stop the server before copying the SQLite database for a
complete backup, including original texts, editions and retained requests.

## Local interface

The server binds only to `127.0.0.1`. Its fixed routes serve the three local UI
assets plus authenticated JSON import, transition, state, history and export.
The UI has no remote assets. Task text is rendered as text rather than HTML.
Requests are bounded, browser origins and the launcher token are checked, and
SQLite serializes writers. Run this as a local operator tool, not an
internet-facing or multi-user service. The original inbox's 1 MiB and 256-task
limits still apply; exports preserve the original JSON contract.

## Focused operation course

For a changed desk, operate a fresh `--demo` database through claim, renewal,
release, new-worker reclaim and completion in the actual browser. Download the
JSON and open it in a second workspace. Check stale-edition rejection, recover
one exact request after an interrupted response, and inspect desktop plus
390-pixel layouts. Keep original protocol suites and hosted CI outside this
specific UI course unless their source behavior is changed.
