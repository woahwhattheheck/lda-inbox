# EffectLedger SQLite auxiliary-file custody

EffectLedger keeps SQLite in **WAL mode** and retains the descriptor-bound main-file generation from the #16 hardening. This document defines the additional auxiliary-file custody provided by issue #18 without overstating what Python's stdlib SQLite binding can mechanically prove.

## Mechanically enforced

Before every SQLite connection is opened, the sibling names `<db>-wal`, `<db>-shm`, and `<db>-journal` are inspected with `lstat`. An existing auxiliary path is rejected with `DB_SIDECAR_UNSAFE` unless it is all of the following:

- a regular file rather than a symlink/device/socket/directory;
- owned by the current effective UID when that concept is available;
- exactly one hard link (`st_nlink == 1`), so it cannot already alias a foreign victim;
- not group- or world-writable.

The same predicate is re-evaluated after the SQLite connection is opened and at Python-visible `execute`, `executemany`, `executescript`, `commit`, and `rollback` boundaries. This prevents a pre-existing hardlink/symlink victim from being handed to SQLite and fails closed if an unsafe alias is installed between Python-visible operations.

The main database path retains its independent descriptor/generation binding and owner/link checks. WAL/crash-integrity behavior is not weakened: `PRAGMA journal_mode=WAL` remains in force. This repair does **not** switch to `MEMORY`, `OFF`, or another durability downgrade.

## Explicit threat-boundary exclusion

Python's stdlib `sqlite3` API does not expose SQLite's VFS `xOpen` hook or the file descriptors SQLite internally obtains for `-wal`, `-shm`, and rollback-journal files. Consequently this implementation does **not** claim full auxiliary-file custody against a hostile same-UID process that can rename or replace an auxiliary pathname in the sub-operation interval **after a successful guard check but before SQLite's internal VFS open**.

Closing that remaining namespace-ABA interval requires a custom SQLite VFS (or an OS-level isolation mechanism that removes the attacker's directory-mutation authority). Detecting an unsafe name only after SQLite has already opened or written it would not prove victim preservation, so this package deliberately does not label such detection as a complete defense.

The accepted threat boundary is therefore:

1. pre-existing unsafe auxiliary aliases fail closed before SQLite opens the database;
2. unsafe aliases installed between Python-visible SQLite operations fail closed before the next guarded operation;
3. arbitrary same-UID sub-operation namespace mutation during SQLite's internal auxiliary-file open is outside the mechanically enforced boundary and must be addressed by a custom VFS / stronger OS isolation before any broader custody claim is made.

## Regression expectations

Hostile tests must preserve victim bytes for pre-existing hardlink and symlink aliases for all three SQLite auxiliary suffixes, must reject permissive regular sidecars, must preserve WAL mode on the clean path, and must verify that an unsafe alias installed while a connection is live blocks the next Python-visible SQLite operation. Tests run under ordinary Python and `python -O` so safety does not depend on assertions.
