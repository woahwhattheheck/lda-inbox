#!/usr/bin/env python3
"""Workload overview and portable observations for the maintained inbox desk."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

SCHEMA = "lda.inbox.workload.v1"
MAX_PAGE = 10
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ASSETS = {
    "/workload": ("workload.html", "text/html; charset=utf-8"),
    "/workload.js": ("workload.js", "text/javascript; charset=utf-8"),
    "/workload.css": ("workload.css", "text/css; charset=utf-8"),
    "/workload-navigation.js": ("workload-navigation.js", "text/javascript; charset=utf-8"),
}


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def decode(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate JSON field in retained workspace")
            value[key] = item
        return value

    def reject(_):
        raise ValueError("Non-finite JSON in retained workspace")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=reject)


def page(db, after="", limit=5):
    """Read one page inside the caller's SQLite read transaction.

    Observations retain whole current inbox documents. They are not a new queue
    or a source of execution authority. No request, result or edition is written.
    """
    if after and (not isinstance(after, str) or not ID.fullmatch(after)):
        raise ValueError("Invalid workspace continuation")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError("Choose between one and ten workspaces per page")
    scope = db.execute("SELECT value FROM meta WHERE key='scope'").fetchone()
    if scope is None:
        raise ValueError("This database has no inbox operator scope")
    total = db.execute("SELECT count(*) FROM workspaces").fetchone()[0]
    records = db.execute(
        "SELECT w.id,w.label,w.created,e.revision,e.digest,e.document,e.action,e.created "
        "FROM workspaces w JOIN editions e ON e.workspace=w.id "
        "AND e.revision=(SELECT max(v.revision) FROM editions v WHERE v.workspace=w.id) "
        "WHERE w.id>? ORDER BY w.id LIMIT ?", (after, limit + 1)).fetchall()
    as_of, workspaces = stamp(), []
    for row in records[:limit]:
        key, label, created, revision, digest, text, action, updated = row
        if not isinstance(key, str) or not ID.fullmatch(key):
            raise ValueError("Retained workspace has an invalid identity")
        # The desk already validates each edition. Check the stored content
        # binding before projecting it, without inventing another task protocol.
        document = decode(text)
        actual = hashlib.sha256(canonical(document).encode("utf-8")).hexdigest()
        if actual != digest:
            raise ValueError("Retained edition differs from its saved digest")
        if not isinstance(document, dict) or not isinstance(document.get("tasks"), list):
            raise ValueError("Retained edition is not an inbox document")
        workspaces.append({"id": key, "label": label, "created": created,
                           "revision": revision, "digest": digest, "document": document,
                           "action": action, "updated": updated, "as_of": as_of})
    return {"schema": SCHEMA, "scope": scope[0], "as_of": as_of,
            "total_workspaces": total, "workspaces": workspaces,
            "next": records[limit - 1][0] if len(records) > limit else None}


def export_observation(database, destination):
    """Export all current workspaces from one read-only SQLite snapshot."""
    if not database.is_file():
        raise ValueError("Select an existing operator database")
    db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        db.execute("BEGIN")
        cursor, count, observed_at = "", 0, None
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                    dir=destination.parent, prefix=".workload-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                output.write('{"schema":"lda.inbox.workload-snapshot.v1","created":')
                json.dump(stamp(), output)
                output.write(',"pages":[')
                first = True
                while True:
                    result = page(db, cursor, MAX_PAGE)
                    observed_at = observed_at or result["as_of"]
                    result["as_of"] = observed_at
                    for workspace in result["workspaces"]:
                        workspace["as_of"] = observed_at
                    if not first:
                        output.write(",")
                    json.dump(result, output, ensure_ascii=False, allow_nan=False)
                    first = False
                    count += len(result["workspaces"])
                    cursor = result["next"]
                    if cursor is None:
                        break
                output.write("]}\n")
                output.flush()
                os.fsync(output.fileno())
            # Publish only complete bytes, and never replace a chosen existing file.
            os.link(temporary, destination)
            return count
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    finally:
        db.close()


def server_type():
    """Extend the maintained host rather than duplicate its authenticated API."""
    from inbox_operator import Handler, OperatorServer

    class WorkloadHandler(Handler):
        def do_GET(self):
            target = urlsplit(self.path)
            if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
                return super().do_GET()
            if target.path in ASSETS:
                name, mime = ASSETS[target.path]
                return self.respond(200, (self.server.workload_assets / name).read_bytes(), mime)
            if target.path == "/":
                html = (self.server.assets / "index.html").read_text(encoding="utf-8")
                # Load before operator.js so the original fragment survives its
                # normal history cleanup. Existing assets and editors stay intact.
                html = html.replace("</head>",
                    '<script src="/workload-navigation.js"></script></head>', 1)
                return self.respond(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if target.path != "/api/workload":
                return super().do_GET()
            if not self.authorized():
                return self.respond(403, {"error": "Open the current launcher URL to reconnect"})
            try:
                query = parse_qs(target.query, keep_blank_values=True)
                if set(query) - {"after", "limit"} or any(len(v) != 1 for v in query.values()):
                    raise ValueError("Invalid workload query")
                after = query.get("after", [""])[0]
                limit = int(query.get("limit", ["5"])[0])
                with self.server.desk.connection() as db:
                    result = page(db, after, limit)
                return self.respond(200, result)
            except (ValueError, TypeError, RecursionError) as exc:
                return self.respond(400, {"error": str(exc)})
            except sqlite3.Error:
                return self.respond(503, {"error": "Workspace read interrupted. Refresh this page."})

    class WorkloadServer(OperatorServer):
        def __init__(self, port, desk):
            super().__init__(port, desk)
            self.RequestHandlerClass = WorkloadHandler
            self.workload_assets = Path(__file__).with_name("operator_ui")
    return WorkloadServer


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="existing private operator database")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--export", type=Path, metavar="NEW_JSON", help="save a complete read-only observation and exit")
    args = parser.parse_args(argv)
    try:
        if args.export:
            count = export_observation(args.db, args.export)
            print(f"Saved {count} current workspaces to {args.export}")
            return 0
        from inbox_operator import Desk
        server = server_type()(args.port, Desk(args.db))
        print(f"Workload: {server.origin}/workload#token={server.token}", flush=True)
        print(f"Operator: {server.origin}/#token={server.token}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except (OSError, ValueError, sqlite3.Error, ImportError) as exc:
        print(f"Workload: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
