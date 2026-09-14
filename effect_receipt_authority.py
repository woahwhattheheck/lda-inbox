from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from effect_receipt_common import EffectError, validate_attempt, validate_id, validate_sha

CANONICAL_INBOX_PATH = Path(__file__).resolve().with_name('inbox.json')
from task_protocol import TaskProtocolError, document_sha256, parse_protocol_path, task_spec_sha256

def _time(value: Any, field: str) -> datetime:
    if not isinstance(value,str) or not value or value != value.strip(): raise EffectError(f'{field.upper()}_INVALID')
    normalized=value[:-1]+'+00:00' if value.endswith('Z') else value
    try: parsed=datetime.fromisoformat(normalized)
    except ValueError as exc: raise EffectError(f'{field.upper()}_INVALID') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None: raise EffectError(f'{field.upper()}_INVALID')
    return parsed.astimezone(timezone.utc)

def require_live_lease(*, expected_document_sha256: str, task_id: str, worker_id: str, lease_id: str, attempt: int, now: str, expected_task_sha256: Optional[str]=None) -> Dict[str,Any]:
    task_id=validate_id('task_id',task_id); worker_id=validate_id('worker_id',worker_id); lease_id=validate_id('lease_id',lease_id); attempt=validate_attempt(attempt)
    expected_document_sha256=validate_sha(expected_document_sha256,'expected_document_sha256')
    if expected_task_sha256 is not None: expected_task_sha256=validate_sha(expected_task_sha256,'task_sha256')
    try: document=parse_protocol_path(CANONICAL_INBOX_PATH)
    except (TaskProtocolError,OSError) as exc: raise EffectError('LEASE_EVIDENCE_INVALID') from exc
    actual_document=document_sha256(document)
    if actual_document != expected_document_sha256: raise EffectError('LEASE_DOCUMENT_CAS_MISMATCH')
    matches=[task for task in document.get('tasks',[]) if isinstance(task,dict) and task.get('id')==task_id]
    if len(matches)!=1: raise EffectError('LEASE_TASK_NOT_FOUND')
    task=matches[0]
    current_task_sha=task_spec_sha256(task)
    if expected_task_sha256 is not None and current_task_sha != expected_task_sha256: raise EffectError('LEASE_TASK_BINDING_MISMATCH')
    execution=task.get('execution')
    if not isinstance(execution,dict) or execution.get('state')!='leased' or task.get('done') is not False: raise EffectError('LEASE_NOT_CURRENT')
    if execution.get('worker_id')!=worker_id or execution.get('lease_id')!=lease_id or execution.get('attempt')!=attempt: raise EffectError('LEASE_AUTHORITY_MISMATCH')
    if execution.get('task_sha256')!=current_task_sha: raise EffectError('LEASE_TASK_BINDING_MISMATCH')
    instant=_time(now,'trusted_time'); claimed=_time(execution.get('claimed_at'),'claimed_at'); expires=_time(execution.get('lease_expires_at'),'lease_expires_at')
    if instant < claimed: raise EffectError('LEASE_TIME_PRECEDES_CLAIM')
    if instant >= expires: raise EffectError('LEASE_EXPIRED')
    return {'document_sha256':actual_document,'task_sha256':current_task_sha,'worker_id':worker_id,'lease_id':lease_id,'attempt':attempt,'lease_expires_at':execution['lease_expires_at']}
