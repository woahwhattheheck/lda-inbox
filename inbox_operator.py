#!/usr/bin/env python3
"""Local operator desk for retained inbox editions using the maintained protocol."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
from urllib.parse import parse_qs, urlsplit
import uuid

from task_protocol import (
    TaskProtocolError, claim_task, complete_task, document_sha256,
    parse_protocol_text, release_task, renew_task,
)
from validate_inbox import MAX_DOCUMENT_BYTES

MAX_REQUEST_BYTES = 8 * MAX_DOCUMENT_BYTES
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TRANSITIONS = {"claim": claim_task, "renew": renew_task,
               "release": release_task, "complete": complete_task}
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/operator.js": ("operator.js", "text/javascript; charset=utf-8"),
          "/operator.css": ("operator.css", "text/css; charset=utf-8")}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def object_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


def decode(text):
    def reject(_):
        raise ValueError("Non-finite JSON value")
    return json.loads(text, object_pairs_hook=object_pairs, parse_constant=reject)


def identifier(value, name):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a canonical identifier of at most 128 characters")
    return value


class Conflict(ValueError):
    """A retained request or visible edition has changed."""


class Desk:
    """Append-only local workspace. One transaction stores both edition and request."""
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        with self.connection(write=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS workspaces(
                    id TEXT PRIMARY KEY, label TEXT NOT NULL, created TEXT NOT NULL,
                    original TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS editions(
                    workspace TEXT NOT NULL REFERENCES workspaces(id), revision INTEGER NOT NULL,
                    digest TEXT NOT NULL, document TEXT NOT NULL, action TEXT NOT NULL,
                    created TEXT NOT NULL, PRIMARY KEY(workspace,revision));
                CREATE TABLE IF NOT EXISTS requests(
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO meta VALUES('scope',?)", (uuid.uuid4().hex,))
            self.scope = db.execute("SELECT value FROM meta WHERE key='scope'").fetchone()[0]

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def latest(db, workspace):
        identifier(workspace, "workspace")
        row = db.execute("SELECT * FROM editions WHERE workspace=? ORDER BY revision DESC LIMIT 1",
                         (workspace,)).fetchone()
        if row is None:
            raise ValueError("Workspace not found")
        return row

    @staticmethod
    def replay(db, request):
        key = identifier(request.get("request_id"), "request_id")
        fingerprint = hashlib.sha256(encode(request).encode("utf-8")).hexdigest()
        found = db.execute("SELECT * FROM requests WHERE id=?", (key,)).fetchone()
        if found:
            if found["fingerprint"] != fingerprint:
                raise Conflict("This request ID already belongs to a different operation")
            return key, fingerprint, decode(found["result"])
        return key, fingerprint, None

    def import_document(self, request):
        if set(request) != {"request_id", "label", "source_text"}:
            raise ValueError("Import requires request_id, label and source_text")
        label = request["label"]
        if not isinstance(label, str) or not label.strip() or len(label) > 160:
            raise ValueError("Use a workspace name between 1 and 160 characters")
        source = request["source_text"]
        if not isinstance(source, str):
            raise ValueError("Select an inbox JSON file")
        document = parse_protocol_text(source)
        with self.connection(write=True) as db:
            key, fingerprint, result = self.replay(db, request)
            if result is not None:
                return result
            workspace, at = uuid.uuid4().hex, utc_now()
            digest = document_sha256(document)
            db.execute("INSERT INTO workspaces VALUES(?,?,?,?)", (workspace, label.strip(), at, source))
            db.execute("INSERT INTO editions VALUES(?,?,?,?,?,?)",
                       (workspace, 1, digest, encode(document), "import", at))
            result = {"workspace": workspace, "revision": 1, "digest": digest, "action": "import"}
            db.execute("INSERT INTO requests VALUES(?,?,?)", (key, fingerprint, encode(result)))
            return result

    def transition(self, request):
        action = request.get("action")
        if not isinstance(action, str) or action not in TRANSITIONS:
            raise ValueError("Choose claim, renew, release or complete")
        required = {"request_id", "workspace", "action", "task_id", "worker_id", "lease_id",
                    "expected_document_sha256"}
        required |= {"lease_seconds"} if action in {"claim", "renew"} else set()
        required |= {"expected_attempt"} if action != "claim" else set()
        required |= {"reason"} if action == "release" else set()
        required |= {"completion_id", "result"} if action == "complete" else set()
        if set(request) != required:
            raise ValueError("Operation fields do not match the selected action")
        with self.connection(write=True) as db:
            key, fingerprint, result = self.replay(db, request)
            if result is not None:
                return result
            old = self.latest(db, request["workspace"])
            if old["digest"] != request["expected_document_sha256"]:
                raise Conflict("The inbox changed. Refresh and review the current edition before staging again")
            args = {k: v for k, v in request.items() if k not in {"request_id", "workspace", "action"}}
            args["now"] = utc_now()
            document, receipt = TRANSITIONS[action](parse_protocol_text(old["document"]), **args)
            text = encode(document)
            parse_protocol_text(text)  # Keep every exported edition within the original contract.
            revision = old["revision"] + int(receipt["changed"])
            if receipt["changed"]:
                db.execute("INSERT INTO editions VALUES(?,?,?,?,?,?)",
                           (request["workspace"], revision, receipt["after_sha256"],
                            text, action, receipt["at"]))
            result = {"workspace": request["workspace"], "revision": revision,
                      "digest": receipt["after_sha256"], "receipt": receipt, "action": action}
            db.execute("INSERT INTO requests VALUES(?,?,?)", (key, fingerprint, encode(result)))
            return result

    def state(self, workspace=None):
        with self.connection() as db:
            workspaces = [dict(row) for row in db.execute(
                "SELECT id,label,created FROM workspaces ORDER BY created DESC,id")]
            if workspace is None and workspaces:
                workspace = workspaces[0]["id"]
            result = {"scope": self.scope, "as_of": utc_now(), "workspaces": workspaces,
                      "workspace": workspace, "document": None}
            if workspace:
                row = self.latest(db, workspace)
                result.update(revision=row["revision"], digest=row["digest"],
                              document=parse_protocol_text(row["document"]))
            return result

    def history(self, workspace, before):
        if type(before) is not int or before < 1:
            raise ValueError("History boundary must be positive")
        with self.connection() as db:
            self.latest(db, workspace)
            rows = [dict(row) for row in db.execute(
                "SELECT revision,digest,action,created FROM editions "
                "WHERE workspace=? AND revision<? ORDER BY revision DESC LIMIT 31", (workspace, before))]
            return {"editions": rows[:30], "before": rows[29]["revision"] if len(rows) > 30 else None}

    def export(self, workspace, revision=None, original=False):
        with self.connection() as db:
            latest = self.latest(db, workspace)
            if original:
                return db.execute("SELECT original FROM workspaces WHERE id=?", (workspace,)).fetchone()[0]
            if revision is None:
                return latest["document"] + "\n"
            row = db.execute("SELECT document FROM editions WHERE workspace=? AND revision=?",
                             (workspace, revision)).fetchone()
            if row is None:
                raise ValueError("Edition not found")
            return row[0] + "\n"


class OperatorServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, port, desk):
        super().__init__(("127.0.0.1", port), Handler)
        self.desk = desk
        self.token = secrets.token_urlsafe(32)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.assets = Path(__file__).with_name("operator_ui")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def respond(self, status, payload, content_type="application/json; charset=utf-8"):
        raw = payload if isinstance(payload, bytes) else encode(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; "
                         "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            return False
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            return False
        supplied = self.headers.get("X-Operator-Token", "")
        return secrets.compare_digest(supplied, self.server.token)

    def do_GET(self):
        target = urlsplit(self.path)
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            return self.respond(403, {"error": "Open the local address printed by the launcher"})
        if target.path in STATIC:
            filename, mime = STATIC[target.path]
            return self.respond(200, (self.server.assets / filename).read_bytes(), mime)
        if not self.authorized():
            return self.respond(403, {"error": "Open the current launcher URL to reconnect"})
        try:
            query = parse_qs(target.query)
            value = lambda key, default=None: query.get(key, [default])[0]
            if target.path == "/api/state":
                result = self.server.desk.state(value("workspace"))
            elif target.path == "/api/history":
                result = self.server.desk.history(value("workspace"), int(value("before", "2147483647")))
            elif target.path == "/api/export":
                rev = value("revision")
                text = self.server.desk.export(value("workspace"), int(rev) if rev else None,
                                               value("original") == "1")
                return self.respond(200, text.encode("utf-8"))
            else:
                return self.respond(404, {"error": "Route not found"})
            self.respond(200, result)
        except (ValueError, TypeError) as exc:
            self.respond(400, {"error": str(exc)})
        except sqlite3.Error:
            self.respond(503, {"error": "Workspace is unavailable. Preserve the database and retry"})

    def do_POST(self):
        if not self.authorized():
            return self.respond(403, {"error": "Open the current launcher URL to reconnect"})
        try:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("Expected application/json")
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("A fixed request length is required")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise ValueError("Request is empty or exceeds the import limit")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Request ended before all bytes arrived")
            request = decode(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("Expected a request object")
            if self.path == "/api/import":
                result = self.server.desk.import_document(request)
            elif self.path == "/api/transition":
                result = self.server.desk.transition(request)
            else:
                return self.respond(404, {"error": "Route not found"})
            self.respond(200, result)
        except Conflict as exc:
            self.respond(409, {"error": str(exc)})
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            self.respond(400, {"error": str(exc)})
        except sqlite3.Error:
            self.respond(503, {"error": "Workspace write was interrupted. Recover the same request"})


def demo_document():
    created = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    return {"v": 1, "label": "Authored local operator demonstration", "tasks": [
        {"id": "demo-layout-review", "created": created, "kind": "run_task",
         "command": "Review the local demonstration layout", "timeout_s": 3600, "done": False},
        {"id": "demo-export-handoff", "created": created, "kind": "run_task",
         "command": "Prepare the local demonstration handoff", "timeout_s": 3600, "done": False},
        {"id": "demo-reopen-edition", "created": created, "kind": "run_task",
         "command": "Reopen the saved demonstration edition", "timeout_s": 3600, "done": False}]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="private local workspace database")
    parser.add_argument("--port", type=int, default=8765, help="loopback port; retain it across restarts for browser recovery")
    parser.add_argument("--demo", action="store_true", help="add the authored demonstration once")
    args = parser.parse_args(argv)
    try:
        desk = Desk(args.db)
        if args.demo:
            # A stable ID prevents creating a new demonstration on every restart.
            with desk.connection() as db:
                exists = db.execute("SELECT 1 FROM requests WHERE id='demo-v1'").fetchone()
            if not exists:
                desk.import_document({"request_id": "demo-v1", "label": "Local demonstration",
                                      "source_text": encode(demo_document())})
        server = OperatorServer(args.port, desk)
        print(f"{server.origin}/#token={server.token}", flush=True)
        print("Local staging desk. All editions are retained in the selected database.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Unable to open operator desk: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
