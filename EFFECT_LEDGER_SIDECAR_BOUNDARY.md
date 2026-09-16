# Effect-ledger SQLite auxiliary-file custody boundary

This note defines the exact storage-custody claim for the external-effect receipt ledger.
It supplements `EFFECT_RECEIPTS.md`; it does not grant dispatch, provider, customer,
payment, account, or revenue authority.

## Main database generation

The main `ledger.sqlite` generation is descriptor-bound. The requested database path
must resolve through a private trusted final parent; the file must be a same-owner
regular single-link generation. Every SQLite open is routed through a retained verified
`/proc/self/fd/<n>` or `/dev/fd/<n>` anchor and the visible main pathname is revalidated.
The retained descriptor remains alive for the SQLite connection lifetime. This is the
strong main-file ABA boundary inherited from the descriptor donor.

## WAL, SHM, and rollback-journal names

Stock Python `sqlite3` does not expose the file descriptors or VFS callbacks SQLite uses
for pathname-derived auxiliary files. SQLite may create or open:

- `ledger.sqlite-wal`
- `ledger.sqlite-shm`
- `ledger.sqlite-journal`

Before every SQLite connection is opened, the ledger checks each existing auxiliary
pathname with `lstat`. An existing sidecar is accepted only when it is a same-owner,
regular, single-link file with no group/other permission bits. Symlinks, hard-linked
foreign generations, non-regular objects, foreign-owner files, and broadly accessible
sidecars fail closed with `DB_AUXILIARY_PATH_UNSAFE` **before `sqlite3.connect` is
called**. Rejected sidecars are not followed, chmodded, unlinked, replaced, or cleaned
up. The same static safety check is repeated at the controlled connection boundary
before the connection is returned to ledger code.

These guards protect foreign victims already present at a sidecar name and substitutions
that become visible before the next controlled connection boundary. Hostile tests cover
all three names for symlink and hard-link aliases and verify victim bytes, mode, inode,
link count, and alias presence remain unchanged. A between-connections substitution is
also rejected before the next SQLite open.

## Deliberate threat-boundary limit

This stdlib-only implementation **does not claim** that it can prevent a malicious
concurrent process running as the ledger owner from replacing an auxiliary pathname
after the last safety check while SQLite itself is opening or writing that sidecar.
Python's stock `sqlite3` API does not expose those hidden sidecar descriptors so this
module cannot bind their inode identities for the entire SQLite I/O lifetime in the same
way it binds the main database descriptor.

Admitting that stronger same-UID concurrent-namespace threat requires a separately
reviewed custody-capable SQLite VFS, a broker that owns the directory namespace, or OS
process/filesystem isolation strong enough to prevent such mutation. Until one of those
boundaries exists, any claim of perpetual WAL/SHM/journal inode binding would be false.

This limit is narrower than accepting arbitrary pre-existing sidecars: unsafe aliases
are rejected, and safe sidecars are rechecked at every connection boundary. It also does
not authenticate the semantic contents of a private same-owner single-link sidecar; that
remains SQLite's recovery responsibility.

## Crash integrity is preserved

The ledger continues to request `PRAGMA journal_mode=WAL`. There is no fallback to
`MEMORY`, `OFF`, or another mode that would trade custody for weaker crash behavior.
Normal SQLite WAL recovery therefore remains available for sidecars that satisfy the
accepted static namespace invariants.

## Operational response

`DB_AUXILIARY_PATH_UNSAFE` is a reconciliation condition. An operator must identify the
contested sidecar generation before changing the namespace. Runtime code intentionally
does not unlink or overwrite a rejected auxiliary path because doing so could destroy a
foreign successor or victim.
