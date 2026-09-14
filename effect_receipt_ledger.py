from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from effect_receipt_common import (EffectError, canonical_payload, public_row, token_hash,
    validate_attempt, validate_id, validate_receipt_ref, validate_sha)


class EffectLedger:
    def __init__(self, db_path: os.PathLike[str] | str):
        path = Path(db_path)
        if str(path) == ":memory:":
            raise EffectError("PERSISTENT_DB_REQUIRED")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def _init_db(self) -> None:
        con = self._connect()
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("""CREATE TABLE IF NOT EXISTS effects (
                task_id TEXT NOT NULL, effect_id TEXT NOT NULL, operation TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, worker_id TEXT NOT NULL, lease_id TEXT NOT NULL,
                attempt INTEGER NOT NULL, effect_generation INTEGER NOT NULL, token_hash TEXT NOT NULL,
                state TEXT NOT NULL, dispatch_count INTEGER NOT NULL DEFAULT 0, outcome_kind TEXT,
                outcome_receipt_ref TEXT, outcome_receipt_sha256 TEXT, PRIMARY KEY (task_id, effect_id),
                CHECK (effect_generation = 1), CHECK (dispatch_count >= 0 AND dispatch_count <= 1),
                CHECK (state IN ('PREPARED','DISPATCHED','SUCCEEDED','FAILED_FINAL','RECONCILIATION_REQUIRED')))""")
        finally:
            con.close()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError as exc:
            raise EffectError("DB_PERMISSION_HARDENING_FAILED") from exc

    def prepare(self, *, task_id: str, effect_id: str, operation: str, payload: Any,
                worker_id: str, lease_id: str, attempt: int) -> Tuple[Dict[str, Any], Optional[str]]:
        task_id, effect_id = validate_id("task_id", task_id), validate_id("effect_id", effect_id)
        operation, worker_id, lease_id = validate_id("operation", operation), validate_id("worker_id", worker_id), validate_id("lease_id", lease_id)
        attempt = validate_attempt(attempt)
        payload_sha256 = hashlib.sha256(canonical_payload(payload)).hexdigest()
        token = secrets.token_urlsafe(32); hashed = token_hash(token)
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM effects WHERE task_id=? AND effect_id=?", (task_id, effect_id)).fetchone()
            if row is None:
                con.execute("""INSERT INTO effects (task_id,effect_id,operation,payload_sha256,worker_id,lease_id,attempt,effect_generation,token_hash,state,dispatch_count) VALUES (?,?,?,?,?,?,?,1,?,'PREPARED',0)""",
                            (task_id,effect_id,operation,payload_sha256,worker_id,lease_id,attempt,hashed))
                con.commit(); return public_row(self._row(con, task_id, effect_id)), token
            exact = (row["operation"] == operation and row["payload_sha256"] == payload_sha256 and
                     row["worker_id"] == worker_id and row["lease_id"] == lease_id and row["attempt"] == attempt)
            if row["state"] == "PREPARED" and exact:
                con.commit(); return public_row(row), None
            if row["state"] == "DISPATCHED":
                con.execute("UPDATE effects SET state='RECONCILIATION_REQUIRED' WHERE task_id=? AND effect_id=? AND state='DISPATCHED'", (task_id,effect_id))
                con.commit(); raise EffectError("RECONCILIATION_REQUIRED_AFTER_DISPATCH")
            if row["state"] == "RECONCILIATION_REQUIRED":
                con.commit(); raise EffectError("RECONCILIATION_REQUIRED_AFTER_DISPATCH")
            if row["state"] in ("SUCCEEDED","FAILED_FINAL"):
                con.commit(); raise EffectError(f"EFFECT_ALREADY_TERMINAL:{row['state']}")
            con.commit(); raise EffectError("EFFECT_IDENTITY_CONFLICT")
        except Exception:
            if con.in_transaction: con.rollback()
            raise
        finally:
            con.close()

    def mark_dispatched(self, *, task_id: str, effect_id: str, token: str) -> Dict[str, Any]:
        task_id, effect_id, supplied = validate_id("task_id",task_id), validate_id("effect_id",effect_id), token_hash(token)
        con=self._connect()
        try:
            con.execute("BEGIN IMMEDIATE"); row=self._row(con,task_id,effect_id)
            if not secrets.compare_digest(row["token_hash"], supplied): raise EffectError("STALE_OR_INVALID_EFFECT_TOKEN")
            if row["state"] == "PREPARED":
                con.execute("UPDATE effects SET state='DISPATCHED', dispatch_count=1 WHERE task_id=? AND effect_id=? AND state='PREPARED'", (task_id,effect_id)); con.commit(); return public_row(self._row(con,task_id,effect_id))
            if row["state"] == "DISPATCHED":
                con.execute("UPDATE effects SET state='RECONCILIATION_REQUIRED' WHERE task_id=? AND effect_id=? AND state='DISPATCHED'", (task_id,effect_id)); con.commit(); raise EffectError("RECONCILIATION_REQUIRED_AFTER_DISPATCH")
            if row["state"] == "RECONCILIATION_REQUIRED": raise EffectError("RECONCILIATION_REQUIRED_AFTER_DISPATCH")
            raise EffectError(f"EFFECT_ALREADY_TERMINAL:{row['state']}")
        except Exception:
            if con.in_transaction: con.rollback()
            raise
        finally: con.close()

    def record_outcome(self, *, task_id: str, effect_id: str, token: str, kind: str, receipt_ref: str, receipt_sha256: str) -> Dict[str, Any]:
        if kind not in ("SUCCEEDED","FAILED_FINAL"): raise EffectError("INVALID_OUTCOME_KIND")
        receipt_ref, receipt_sha256 = validate_receipt_ref(receipt_ref), validate_sha(receipt_sha256,"receipt_sha256")
        task_id, effect_id, supplied = validate_id("task_id",task_id), validate_id("effect_id",effect_id), token_hash(token)
        con=self._connect()
        try:
            con.execute("BEGIN IMMEDIATE"); row=self._row(con,task_id,effect_id)
            if not secrets.compare_digest(row["token_hash"], supplied): raise EffectError("STALE_OR_INVALID_EFFECT_TOKEN")
            if row["state"] in ("SUCCEEDED","FAILED_FINAL"):
                if row["state"] == kind and row["outcome_receipt_ref"] == receipt_ref and row["outcome_receipt_sha256"] == receipt_sha256:
                    con.commit(); return public_row(row)
                raise EffectError("CONFLICTING_TERMINAL_OUTCOME")
            if row["state"] == "PREPARED": raise EffectError("OUTCOME_BEFORE_DISPATCH")
            if row["state"] not in ("DISPATCHED","RECONCILIATION_REQUIRED"): raise EffectError("INVALID_EFFECT_STATE")
            con.execute("UPDATE effects SET state=?, outcome_kind=?, outcome_receipt_ref=?, outcome_receipt_sha256=? WHERE task_id=? AND effect_id=?", (kind,kind,receipt_ref,receipt_sha256,task_id,effect_id)); con.commit(); return public_row(self._row(con,task_id,effect_id))
        except Exception:
            if con.in_transaction: con.rollback()
            raise
        finally: con.close()

    def inspect(self, *, task_id: str, effect_id: str) -> Dict[str, Any]:
        with self._connect() as con: return public_row(self._row(con, validate_id("task_id",task_id), validate_id("effect_id",effect_id)))

    @staticmethod
    def _row(con: sqlite3.Connection, task_id: str, effect_id: str) -> sqlite3.Row:
        row=con.execute("SELECT * FROM effects WHERE task_id=? AND effect_id=?",(task_id,effect_id)).fetchone()
        if row is None: raise EffectError("EFFECT_NOT_FOUND")
        return row
