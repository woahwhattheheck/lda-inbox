# Inbox workload overview

The workload view joins the existing operator's current workspace editions. It
shows available and released tasks, expired leases, active owners, leases that
expire within 15 minutes, task age, repeated task IDs, and source workspace
activity. Search and filter by command, task, state, worker or workspace; open a
task in the original desk, with its current edition refreshed before acting.

## Open the current desk with workload navigation

From this repository, use the same private database and port as the operator:

```sh
python inbox_workload.py --db /private/operator.sqlite --port 8765
```

Stop the earlier local server first if it owns that port. The printed addresses
open the workload view and the maintained operator. This launcher reuses the
original `Desk`, authenticated `Handler` and `OperatorServer`. Its only added
API is a read-only workload page; the original import, transition, recovery,
history and export routes remain unchanged. It serves the maintained operator
HTML with one additive navigation script, so composing and archive additions
remain available without a second copy of those editors.

Five workspaces load at a time. **Load next 5** extends the observation; each
page has its own read time and is internally consistent. **Refresh live
workspaces** starts from the current source again. The coverage line states
exactly how many workspace records are loaded. New workspaces or editions can
arrive between pages, so use refresh for a current operational picture. This is
an operator-requested view, not a polling service.

States are fixed at each workspace's read time, including in saved files. They
do not silently advance with the receiving computer's clock. Task age is time
since task creation, not evidence of a worker's last activity. Workspace update
time describes its latest saved edition. Released tasks count as unassigned;
their former worker is still visible. An expired lease is a scheduling signal,
not a statement that a worker is dead.

## Work from the view

Click a summary count or a worker to filter. Use **Oldest created first** or
**Nearest lease expiry** to organize the current task records. Repeated IDs are
marked with their number of source workspaces: an imported copy remains a
separate local edition, not additional fleet capacity. Long commands and whole
task records are available under **Complete command and source**.

**Open original task** carries the exact workspace and task IDs to the maintained
operator, which reads its latest edition. The original optimistic-concurrency,
lease-generation and pending-request recovery rules still apply. A task removed
from that current workspace is reported rather than silently acted upon.
**Download inbox** preserves the complete selected source edition as ordinary
inbox JSON. It does not publish or execute it.

## Portable observations and handoff

**Save complete observation** saves all loaded current inboxes with their scope,
workspace identities, revisions, digests and read times. The file contains no
launcher token. **Export filtered tasks** saves the selected task identities
and their complete original source inboxes. Open either file using **Open saved
observation**. A filtered selection reopens as a selection; **Reset filters**
reveals all its retained source tasks. Saved observations expose downloads and
inspection, while live task-action links require a fresh connection.

For a complete export of every workspace from one SQLite read transaction:

```sh
python inbox_workload.py --db /private/operator.sqlite --export /private/Inbox_Workload.json
```

The command opens the database read-only and requires a new output filename.
It checks each current document against its retained content digest. It does not
read historical requests, change workspaces, contact devices, or create an
execution claim. This current-state observation is different from
`inbox_workspace_archive.py`, which retains the complete database and history.
Keep observations private when their inboxes contain private tasks or results.

The viewer also opens locally as `operator_ui/workload.html` with its sibling
JavaScript and stylesheet. Use **Open saved observation** for the exported JSON.
The file viewer requires no server or remote asset. Live refresh is available
only from the authenticated local launcher. Browser imports are limited to
64 MiB; the command-line exporter can retain larger complete observations.

## Source layout

`inbox_workload.py` supplies the read-only projection, complete export and native
host extension. `operator_ui/workload.{html,js,css}` supplies the responsive view;
`workload-navigation.js` adds original-desk routing. The task protocol, existing
operator sources and archive remain unchanged. No hosted workflow is required.
