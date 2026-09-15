from __future__ import annotations
import hashlib, os, secrets, sqlite3, stat
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union
from effect_receipt_authority import require_live_lease, require_live_lease_current
from effect_receipt_common import (EffectError, canonical_payload, public_row, token_hash, validate_attempt, validate_id, validate_receipt_ref, validate_sha, validate_token_path)


def _owned(info):
    if hasattr(os,'geteuid') and info.st_uid!=os.geteuid(): raise EffectError('DB_PATH_OWNER_MISMATCH')


def _safe_file_info(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1: raise EffectError('DB_PATH_UNSAFE')
    _owned(info)


def _prepare_db_path(db_path:Union[os.PathLike,str])->Tuple[Path,Tuple[int,int]]:
    path=Path(db_path)
    if str(path)==':memory:': raise EffectError('PERSISTENT_DB_REQUIRED')
    parent=path.parent if str(path.parent) else Path('.')
    try: parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    except OSError as exc: raise EffectError('DB_PARENT_PREPARE_FAILED') from exc
    dflags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)
    try: parent_fd=os.open(parent,dflags)
    except OSError as exc: raise EffectError('DB_PARENT_UNSAFE') from exc
    fd=None
    try:
        parent_info=os.fstat(parent_fd)
        if not stat.S_ISDIR(parent_info.st_mode): raise EffectError('DB_PARENT_UNSAFE')
        _owned(parent_info)
        if parent_info.st_mode&(stat.S_IWGRP|stat.S_IWOTH): raise EffectError('DB_PARENT_UNSAFE')
        name=path.name
        if not name or name in ('.','..'): raise EffectError('DB_PATH_UNSAFE')
        try: visible=os.stat(name,dir_fd=parent_fd,follow_symlinks=False)
        except FileNotFoundError:
            flags=os.O_RDWR|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0)
            try: fd=os.open(name,flags,0o600,dir_fd=parent_fd)
            except OSError as exc: raise EffectError('DB_CREATE_FAILED') from exc
        except OSError as exc: raise EffectError('DB_PATH_STAT_FAILED') from exc
        else:
            _safe_file_info(visible)
            flags=os.O_RDWR|getattr(os,'O_NOFOLLOW',0)
            try: fd=os.open(name,flags,dir_fd=parent_fd)
            except OSError as exc: raise EffectError('DB_OPEN_FAILED') from exc
            opened=os.fstat(fd)
            _safe_file_info(opened)
            if (opened.st_dev,opened.st_ino)!=(visible.st_dev,visible.st_ino): raise EffectError('DB_PATH_REBOUND')
        info=os.fstat(fd); _safe_file_info(info)
        try: os.fchmod(fd,0o600)
        except OSError as exc: raise EffectError('DB_PERMISSION_HARDENING_FAILED') from exc
        try: os.fsync(fd)
        except OSError as exc: raise EffectError('DB_FSYNC_FAILED') from exc
        after=os.fstat(fd); _safe_file_info(after)
        visible=os.stat(name,dir_fd=parent_fd,follow_symlinks=False)
        if (visible.st_dev,visible.st_ino)!=(after.st_dev,after.st_ino): raise EffectError('DB_PATH_REBOUND')
        try: canonical=parent.resolve(strict=True)/name
        except OSError as exc: raise EffectError('DB_PARENT_UNSAFE') from exc
        return canonical,(after.st_dev,after.st_ino)
    finally:
        if fd is not None:
            try: os.close(fd)
            except OSError: pass
        try: os.close(parent_fd)
        except OSError: pass


class EffectLedger:
    def __init__(self,db_path:Union[os.PathLike,str]):
        path,identity=_prepare_db_path(db_path); self.db_path=str(path); self._db_identity=identity; self._db_uri=path.as_uri()+'?mode=rw'; self._init_db(); self._assert_db_identity()
    def _assert_db_identity(self):
        try: info=os.lstat(self.db_path)
        except OSError as exc: raise EffectError('DB_PATH_REBOUND') from exc
        try: _safe_file_info(info)
        except EffectError as exc: raise EffectError('DB_PATH_REBOUND') from exc
        if (info.st_dev,info.st_ino)!=self._db_identity: raise EffectError('DB_PATH_REBOUND')
    def _connect(self):
        self._assert_db_identity()
        con=sqlite3.connect(self._db_uri,timeout=10.0,isolation_level=None,uri=True)
        try: self._assert_db_identity()
        except Exception:
            con.close(); raise
        con.row_factory=sqlite3.Row; con.execute('PRAGMA foreign_keys=ON'); con.execute('PRAGMA busy_timeout=10000'); return con
    def _init_db(self):
        con=self._connect()
        try:
            con.execute('PRAGMA journal_mode=WAL')
            con.execute("""CREATE TABLE IF NOT EXISTS effects (
            task_id TEXT NOT NULL,effect_id TEXT NOT NULL,operation TEXT NOT NULL,payload_sha256 TEXT NOT NULL,task_sha256 TEXT NOT NULL,
            worker_id TEXT NOT NULL,lease_id TEXT NOT NULL,attempt INTEGER NOT NULL,effect_generation INTEGER NOT NULL,token_hash TEXT NOT NULL,
            token_path TEXT NOT NULL,state TEXT NOT NULL,dispatch_count INTEGER NOT NULL DEFAULT 0,outcome_kind TEXT,outcome_receipt_ref TEXT,outcome_receipt_sha256 TEXT,
            PRIMARY KEY(task_id,effect_id),CHECK(effect_generation>=1),CHECK(dispatch_count>=0 AND dispatch_count<=1),
            CHECK(state IN ('TOKEN_PENDING','PREPARED','DISPATCHED','SUCCEEDED','FAILED_FINAL','RECONCILIATION_REQUIRED')))""")
        finally: con.close()
        self._assert_db_identity()
    def _authority(self,*,expected_document_sha256,task_id,worker_id,lease_id,attempt,now,expected_task_sha256=None):
        return require_live_lease(expected_document_sha256=expected_document_sha256,task_id=task_id,worker_id=worker_id,lease_id=lease_id,attempt=attempt,now=now,expected_task_sha256=expected_task_sha256)
    def _authority_current(self,*,expected_document_sha256,task_id,worker_id,lease_id,attempt,expected_task_sha256=None):
        return require_live_lease_current(expected_document_sha256=expected_document_sha256,task_id=task_id,worker_id=worker_id,lease_id=lease_id,attempt=attempt,expected_task_sha256=expected_task_sha256)
    def prepare(self,*,task_id:str,effect_id:str,operation:str,payload:Any,worker_id:str,lease_id:str,attempt:int,token_path:str,expected_document_sha256:str,now:str)->Tuple[Dict[str,Any],Optional[str]]:
        task_id,effect_id=validate_id('task_id',task_id),validate_id('effect_id',effect_id); operation=validate_id('operation',operation); worker_id,lease_id=validate_id('worker_id',worker_id),validate_id('lease_id',lease_id); attempt=validate_attempt(attempt); token_path=validate_token_path(token_path)
        authority=self._authority(expected_document_sha256=expected_document_sha256,task_id=task_id,worker_id=worker_id,lease_id=lease_id,attempt=attempt,now=now)
        task_sha256=authority['task_sha256']; payload_sha256=hashlib.sha256(canonical_payload(payload)).hexdigest()
        con=self._connect()
        try:
            con.execute('BEGIN IMMEDIATE'); row=con.execute('SELECT * FROM effects WHERE task_id=? AND effect_id=?',(task_id,effect_id)).fetchone()
            if row is None:
                token=secrets.token_urlsafe(32)
                con.execute("""INSERT INTO effects(task_id,effect_id,operation,payload_sha256,task_sha256,worker_id,lease_id,attempt,effect_generation,token_hash,token_path,state,dispatch_count)
                VALUES(?,?,?,?,?,?,?,?,1,?,?,'TOKEN_PENDING',0)""",(task_id,effect_id,operation,payload_sha256,task_sha256,worker_id,lease_id,attempt,token_hash(token),token_path))
                con.commit(); return public_row(self._row(con,task_id,effect_id)),token
            same_semantics=(row['operation']==operation and row['payload_sha256']==payload_sha256 and row['task_sha256']==task_sha256)
            exact_lease=(row['worker_id']==worker_id and row['lease_id']==lease_id and row['attempt']==attempt)
            if row['state'] in ('TOKEN_PENDING','PREPARED') and same_semantics and exact_lease:
                if row['token_path']!=token_path: raise EffectError('TOKEN_PATH_MISMATCH')
                con.commit(); return public_row(row),None
            if row['state'] in ('TOKEN_PENDING','PREPARED') and same_semantics and attempt>row['attempt']:
                token=secrets.token_urlsafe(32); generation=row['effect_generation']+1
                con.execute("""UPDATE effects SET worker_id=?,lease_id=?,attempt=?,effect_generation=?,token_hash=?,token_path=?,state='TOKEN_PENDING',dispatch_count=0,outcome_kind=NULL,outcome_receipt_ref=NULL,outcome_receipt_sha256=NULL
                WHERE task_id=? AND effect_id=?""",(worker_id,lease_id,attempt,generation,token_hash(token),token_path,task_id,effect_id))
                con.commit(); return public_row(self._row(con,task_id,effect_id)),token
            if row['state']=='DISPATCHED':
                con.execute("UPDATE effects SET state='RECONCILIATION_REQUIRED' WHERE task_id=? AND effect_id=? AND state='DISPATCHED'",(task_id,effect_id)); con.commit(); raise EffectError('RECONCILIATION_REQUIRED_AFTER_DISPATCH')
            if row['state']=='RECONCILIATION_REQUIRED': con.commit(); raise EffectError('RECONCILIATION_REQUIRED_AFTER_DISPATCH')
            if row['state'] in ('SUCCEEDED','FAILED_FINAL'): con.commit(); raise EffectError(f"EFFECT_ALREADY_TERMINAL:{row['state']}")
            raise EffectError('EFFECT_IDENTITY_CONFLICT')
        except Exception:
            if con.in_transaction: con.rollback()
            raise
        finally: con.close()
    def rotate_pending_token(self,*,task_id:str,effect_id:str,token_path:str,expected_document_sha256:str,now:str)->Tuple[Dict[str,Any],str]:
        task_id,effect_id=validate_id('task_id',task_id),validate_id('effect_id',effect_id); token_path=validate_token_path(token_path)
        con=self._connect()
        try:
            con.execute('BEGIN IMMEDIATE'); row=self._row(con,task_id,effect_id)
            if row['state']!='TOKEN_PENDING': raise EffectError('TOKEN_NOT_PENDING')
            if row['token_path']!=token_path: raise EffectError('TOKEN_PATH_MISMATCH')
            self._authority(expected_document_sha256=expected_document_sha256,task_id=task_id,worker_id=row['worker_id'],lease_id=row['lease_id'],attempt=row['attempt'],now=now,expected_task_sha256=row['task_sha256'])
            token=secrets.token_urlsafe(32); con.execute('UPDATE effects SET token_hash=? WHERE task_id=? AND effect_id=? AND state=\'TOKEN_PENDING\'',(token_hash(token),task_id,effect_id)); con.commit(); return public_row(self._row(con,task_id,effect_id)),token
        except Exception:
            if con.in_transaction: con.rollback()
            raise
        finally: con.close()
    def finalize_prepare(self,*,task_id:str,effect_id:str,token_record:Dict[str,Any])->Dict[str,Any]:
        task_id,effect_id=validate_id('task_id',task_id),validate_id('effect_id',effect_id); supplied=token_hash(token_record.get('token'))
        con=self._connect()
        try:
            con.execute('BEGIN IMMEDIATE'); row=self._row(con,task_id,effect_id)
            if not secrets.compare_digest(row['token_hash'],supplied): raise EffectError('STALE_OR_INVALID_EFFECT_TOKEN')
            expected={'task_id':row['task_id'],'effect_id':row['effect_id'],'effect_generation':row['effect_generation'],'worker_id':row['worker_id'],'lease_id':row['lease_id'],'attempt':row['attempt'],'task_sha256':row['task_sha256']}
            if any(token_record.get(k)!=v for k,v in expected.items()): raise EffectError('TOKEN_BINDING_MISMATCH')
            if row['state']=='PREPARED': con.commit(); return public_row(row)
            if row['state']!='TOKEN_PENDING': raise EffectError('TOKEN_NOT_PENDING')
            con.execute("UPDATE effects SET state='PREPARED' WHERE task_id=? AND effect_id=? AND state='TOKEN_PENDING'",(task_id,effect_id)); con.commit(); return public_row(self._row(con,task_id,effect_id))
        except Exception:
            if con.in_transaction: con.rollback()
            raise
        finally: con.close()
    def mark_dispatched(self,*,task_id:str,effect_id:str,token_record:Dict[str,Any],expected_document_sha256:str,now:Optional[str]=None)->Dict[str,Any]:
        # `now` is retained as a no-op compatibility keyword for older callers.
        # Dispatch authority never consumes it; current UTC comes from the verifier.
        task_id,effect_id=validate_id('task_id',task_id),validate_id('effect_id',effect_id); supplied=token_hash(token_record.get('token'))
        con=self._connect()
        try:
            con.execute('BEGIN IMMEDIATE'); row=self._row(con,task_id,effect_id)
            if not secrets.compare_digest(row['token_hash'],supplied): raise EffectError('STALE_OR_INVALID_EFFECT_TOKEN')
            if token_record.get('effect_generation')!=row['effect_generation'] or token_record.get('worker_id')!=row['worker_id'] or token_record.get('lease_id')!=row['lease_id'] or token_record.get('attempt')!=row['attempt'] or token_record.get('task_sha256')!=row['task_sha256']: raise EffectError('TOKEN_BINDING_MISMATCH')
            if row['state']=='TOKEN_PENDING': raise EffectError('TOKEN_PUBLICATION_INCOMPLETE')
            if row['state']=='PREPARED':
                self._authority_current(expected_document_sha256=expected_document_sha256,task_id=task_id,worker_id=row['worker_id'],lease_id=row['lease_id'],attempt=row['attempt'],expected_task_sha256=row['task_sha256'])
                con.execute("UPDATE effects SET state='DISPATCHED',dispatch_count=1 WHERE task_id=? AND effect_id=? AND state='PREPARED'",(task_id,effect_id)); con.commit(); return public_row(self._row(con,task_id,effect_id))
            if row['state']=='DISPATCHED':
                con.execute("UPDATE effects SET state='RECONCILIATION_REQUIRED' WHERE task_id=? AND effect_id=? AND state='DISPATCHED'",(task_id,effect_id)); con.commit(); raise EffectError('RECONCILIATION_REQUIRED_AFTER_DISPATCH')
            if row['state']=='RECONCILIATION_REQUIRED': raise EffectError('RECONCILIATION_REQUIRED_AFTER_DISPATCH')
            raise EffectError(f"EFFECT_ALREADY_TERMINAL:{row['state']}")
        except Exception:
            if con.in_transaction: con.rollback()
            raise
        finally: con.close()
    def record_outcome(self,*,task_id:str,effect_id:str,token_record:Dict[str,Any],kind:str,receipt_ref:str,receipt_sha256:str)->Dict[str,Any]:
        if kind not in ('SUCCEEDED','FAILED_FINAL'): raise EffectError('INVALID_OUTCOME_KIND')
        receipt_ref,receipt_sha256=validate_receipt_ref(receipt_ref),validate_sha(receipt_sha256,'receipt_sha256'); task_id,effect_id=validate_id('task_id',task_id),validate_id('effect_id',effect_id); supplied=token_hash(token_record.get('token'))
        con=self._connect()
        try:
            con.execute('BEGIN IMMEDIATE'); row=self._row(con,task_id,effect_id)
            if not secrets.compare_digest(row['token_hash'],supplied): raise EffectError('STALE_OR_INVALID_EFFECT_TOKEN')
            if token_record.get('effect_generation')!=row['effect_generation']: raise EffectError('TOKEN_BINDING_MISMATCH')
            if row['state'] in ('SUCCEEDED','FAILED_FINAL'):
                if row['state']==kind and row['outcome_receipt_ref']==receipt_ref and row['outcome_receipt_sha256']==receipt_sha256: con.commit(); return public_row(row)
                raise EffectError('CONFLICTING_TERMINAL_OUTCOME')
            if row['state'] in ('TOKEN_PENDING','PREPARED'): raise EffectError('OUTCOME_BEFORE_DISPATCH')
            if row['state'] not in ('DISPATCHED','RECONCILIATION_REQUIRED'): raise EffectError('INVALID_EFFECT_STATE')
            con.execute('UPDATE effects SET state=?,outcome_kind=?,outcome_receipt_ref=?,outcome_receipt_sha256=? WHERE task_id=? AND effect_id=?',(kind,kind,receipt_ref,receipt_sha256,task_id,effect_id)); con.commit(); return public_row(self._row(con,task_id,effect_id))
        except Exception:
            if con.in_transaction: con.rollback()
            raise
        finally: con.close()
    def inspect(self,*,task_id:str,effect_id:str)->Dict[str,Any]:
        con=self._connect()
        try:
            return public_row(self._row(con,validate_id('task_id',task_id),validate_id('effect_id',effect_id)))
        finally:
            con.close()
    @staticmethod
    def _row(con,task_id,effect_id):
        row=con.execute('SELECT * FROM effects WHERE task_id=? AND effect_id=?',(task_id,effect_id)).fetchone()
        if row is None: raise EffectError('EFFECT_NOT_FOUND')
        return row
