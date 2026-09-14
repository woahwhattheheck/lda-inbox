import hashlib,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from effect_receipt_common import EffectError
import effect_receipt_authority
from effect_receipt_io import normalized_token_path,read_token_file,write_token_file
from effect_receipt_ledger import EffectLedger
from task_protocol import claim_task,document_sha256

class T(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.db=self.root/'e.sqlite';self.inbox=self.root/'inbox.json';self.payload={'lead':'x','amount_cents':1};self._inbox_patch=mock.patch.object(effect_receipt_authority,'CANONICAL_INBOX_PATH',self.inbox);self._inbox_patch.start();self.write_inbox(1,'w','l');self.ledger=EffectLedger(self.db)
 def tearDown(self):self._inbox_patch.stop();self.tmp.cleanup()
 def pending_doc(self):
  return {"v":1,"tasks":[{"id":"task-1","created":"2026-09-14T08:59:00Z","kind":"run_task","command":"do","timeout_s":3600,"done":False}]}
 def write_inbox(self,attempt,worker,lease,state='leased'):
  first,_=claim_task(self.pending_doc(),task_id='task-1',worker_id='w',lease_id='l',now='2026-09-14T09:00:00Z',lease_seconds=600,expected_document_sha256=document_sha256(self.pending_doc()))
  if attempt==1:
   doc=first
   if worker!='w' or lease!='l':
    raise ValueError('attempt-1 fixture uses w/l')
  elif attempt==2:
   doc,_=claim_task(first,task_id='task-1',worker_id=worker,lease_id=lease,now='2026-09-14T09:10:00Z',lease_seconds=600,expected_document_sha256=document_sha256(first))
  else: raise ValueError('unsupported attempt')
  self.inbox.write_text(json.dumps(doc),encoding='utf-8');self.doc=doc;self.docsha=document_sha256(doc);return doc
 def prep(self,path=None,attempt=1,worker='w',lease='l'):
  path=path or self.root/f't{attempt}.json'
  public,tok=self.ledger.prepare(task_id='task-1',effect_id='effect-1',operation='send_email',payload=self.payload,worker_id=worker,lease_id=lease,attempt=attempt,token_path=normalized_token_path(path),expected_document_sha256=self.docsha,now=('2026-09-14T09:05:00Z' if attempt==1 else '2026-09-14T09:11:00Z'))
  return path,public,tok
 def publish(self,path,pub,tok):
  write_token_file(path,task_id=pub['task_id'],effect_id=pub['effect_id'],effect_generation=pub['effect_generation'],token=tok,worker_id=pub['lease']['worker_id'],lease_id=pub['lease']['lease_id'],attempt=pub['lease']['attempt'],task_sha256=pub['task_sha256'])
  return self.ledger.finalize_prepare(task_id=pub['task_id'],effect_id=pub['effect_id'],token_record=read_token_file(path))
 def test_prepare_publish_dispatch_success(self):
  p,pub,tok=self.prep();self.assertEqual(pub['state'],'TOKEN_PENDING');pub=self.publish(p,pub,tok);self.assertEqual(pub['state'],'PREPARED');rec=read_token_file(p);d=self.ledger.mark_dispatched(task_id='task-1',effect_id='effect-1',token_record=rec,expected_document_sha256=self.docsha,now='2026-09-14T09:06:00Z');self.assertEqual(d['state'],'DISPATCHED')
 def test_stale_attempt_cannot_dispatch_and_new_generation_handoffs(self):
  p1,pub1,t1=self.prep();self.publish(p1,pub1,t1);old=read_token_file(p1)
  self.write_inbox(2,'w2','l2')
  with self.assertRaisesRegex(EffectError,'LEASE_AUTHORITY_MISMATCH'):self.ledger.mark_dispatched(task_id='task-1',effect_id='effect-1',token_record=old,expected_document_sha256=self.docsha,now='2026-09-14T09:12:00Z')
  self.assertEqual(self.ledger.inspect(task_id='task-1',effect_id='effect-1')['state'],'PREPARED')
  p2,pub2,t2=self.prep(self.root/'t2.json',2,'w2','l2');self.assertEqual(pub2['effect_generation'],2);self.publish(p2,pub2,t2)
  with self.assertRaisesRegex(EffectError,'STALE_OR_INVALID'):self.ledger.mark_dispatched(task_id='task-1',effect_id='effect-1',token_record=old,expected_document_sha256=self.docsha,now='2026-09-14T09:13:00Z')
  new=read_token_file(p2);self.assertEqual(self.ledger.mark_dispatched(task_id='task-1',effect_id='effect-1',token_record=new,expected_document_sha256=self.docsha,now='2026-09-14T09:13:00Z')['state'],'DISPATCHED')
 def test_raw_identity_reuse_higher_attempt_invalidates_old_token(self):
  p1,pub1,t1=self.prep();self.publish(p1,pub1,t1);old=read_token_file(p1);self.write_inbox(2,'w','l');p2,pub2,t2=self.prep(self.root/'t2.json',2,'w','l');self.publish(p2,pub2,t2)
  with self.assertRaisesRegex(EffectError,'STALE_OR_INVALID'):self.ledger.mark_dispatched(task_id='task-1',effect_id='effect-1',token_record=old,expected_document_sha256=self.docsha,now='2026-09-14T09:20:00Z')
 def test_expired_lease_rejected_at_dispatch(self):
  p,pub,t=self.prep();self.publish(p,pub,t);rec=read_token_file(p)
  with self.assertRaisesRegex(EffectError,'LEASE_EXPIRED'):self.ledger.mark_dispatched(task_id='task-1',effect_id='effect-1',token_record=rec,expected_document_sha256=self.docsha,now='2026-09-14T09:10:00Z')
 def test_document_cas_mismatch_rejected(self):
  with self.assertRaisesRegex(EffectError,'LEASE_DOCUMENT_CAS_MISMATCH'):self.ledger.prepare(task_id='task-1',effect_id='effect-1',operation='send_email',payload=self.payload,worker_id='w',lease_id='l',attempt=1,token_path=normalized_token_path(self.root/'x'),expected_document_sha256='0'*64,now='2026-09-14T09:05:00Z')
 def test_token_writer_handles_short_writes_and_roundtrips_binding(self):
  p,pub,tok=self.prep();real_write=os.write
  def short(fd,data):return real_write(fd,data[:max(1,min(3,len(data)))])
  with mock.patch('effect_receipt_io.os.write',side_effect=short):self.publish(p,pub,tok)
  rec=read_token_file(p);self.assertEqual(rec['attempt'],1);self.assertEqual(rec['task_sha256'],pub['task_sha256'])
 def test_preexisting_target_never_overwritten_and_pending_can_recover_after_removal(self):
  p=self.root/'token.json';p.write_text('foreign',encoding='utf-8');pub,tok=None,None
  path,pub,tok=self.prep(p)
  with self.assertRaisesRegex(EffectError,'TOKEN_PATH_OCCUPIED'):self.publish(path,pub,tok)
  self.assertEqual(p.read_text(),'foreign');self.assertEqual(self.ledger.inspect(task_id='task-1',effect_id='effect-1')['state'],'TOKEN_PENDING');p.unlink()
  pub2,tok2=self.ledger.rotate_pending_token(task_id='task-1',effect_id='effect-1',token_path=normalized_token_path(p),expected_document_sha256=self.docsha,now='2026-09-14T09:07:00Z');self.assertEqual(self.publish(p,pub2,tok2)['state'],'PREPARED')
 def test_write_failure_does_not_publish_partial_target(self):
  p,pub,tok=self.prep()
  with mock.patch('effect_receipt_io.os.write',side_effect=OSError('disk')):
   with self.assertRaisesRegex(EffectError,'TOKEN_WRITE_FAILED'):self.publish(p,pub,tok)
  self.assertFalse(p.exists());self.assertEqual(self.ledger.inspect(task_id='task-1',effect_id='effect-1')['state'],'TOKEN_PENDING')
 def test_finalize_is_exact_token_and_generation_bound(self):
  p,pub,tok=self.prep();write_token_file(p,task_id=pub['task_id'],effect_id=pub['effect_id'],effect_generation=pub['effect_generation'],token=tok,worker_id=pub['lease']['worker_id'],lease_id=pub['lease']['lease_id'],attempt=pub['lease']['attempt'],task_sha256=pub['task_sha256']);rec=read_token_file(p);bad=dict(rec,effect_generation=2)
  with self.assertRaisesRegex(EffectError,'TOKEN_BINDING_MISMATCH'):self.ledger.finalize_prepare(task_id='task-1',effect_id='effect-1',token_record=bad)
  self.assertEqual(self.ledger.finalize_prepare(task_id='task-1',effect_id='effect-1',token_record=rec)['state'],'PREPARED')
 def test_repeated_dispatch_reconciliation(self):
  p,pub,t=self.prep();self.publish(p,pub,t);rec=read_token_file(p);self.ledger.mark_dispatched(task_id='task-1',effect_id='effect-1',token_record=rec,expected_document_sha256=self.docsha,now='2026-09-14T09:06:00Z')
  with self.assertRaisesRegex(EffectError,'RECONCILIATION_REQUIRED'):self.ledger.mark_dispatched(task_id='task-1',effect_id='effect-1',token_record=rec,expected_document_sha256=self.docsha,now='2026-09-14T09:07:00Z')
 def test_changed_payload_same_effect_conflicts(self):
  self.prep()
  with self.assertRaisesRegex(EffectError,'EFFECT_IDENTITY_CONFLICT'):
   self.ledger.prepare(task_id='task-1',effect_id='effect-1',operation='send_email',payload={'lead':'changed'},worker_id='w',lease_id='l',attempt=1,token_path=normalized_token_path(self.root/'t1.json'),expected_document_sha256=self.docsha,now='2026-09-14T09:05:30Z')
 def test_exact_pending_prepare_replay_does_not_reissue_secret(self):
  p,first,token=self.prep();p2,second,replay=self.prep(p)
  self.assertEqual(p,p2);self.assertEqual(first,second);self.assertTrue(token);self.assertIsNone(replay)
 def test_nonfinite_float_and_bool_attempt_rejected(self):
  with self.assertRaisesRegex(EffectError,'NONFINITE_NUMBER'):
   self.ledger.prepare(task_id='task-1',effect_id='e-nan',operation='send_email',payload={'x':float('nan')},worker_id='w',lease_id='l',attempt=1,token_path=normalized_token_path(self.root/'nan.json'),expected_document_sha256=self.docsha,now='2026-09-14T09:05:00Z')
  with self.assertRaisesRegex(EffectError,'FLOAT_NOT_ALLOWED'):
   self.ledger.prepare(task_id='task-1',effect_id='e-float',operation='send_email',payload={'x':1.5},worker_id='w',lease_id='l',attempt=1,token_path=normalized_token_path(self.root/'float.json'),expected_document_sha256=self.docsha,now='2026-09-14T09:05:00Z')
  with self.assertRaisesRegex(EffectError,'INVALID_ATTEMPT'):
   self.ledger.prepare(task_id='task-1',effect_id='e-bool',operation='send_email',payload={'x':1},worker_id='w',lease_id='l',attempt=True,token_path=normalized_token_path(self.root/'bool.json'),expected_document_sha256=self.docsha,now='2026-09-14T09:05:00Z')
 def test_token_plaintext_absent_from_database_and_public_state(self):
  _p,pub,token=self.prep()
  self.assertNotIn(token.encode(),self.db.read_bytes());self.assertNotIn(token,json.dumps(pub));self.assertNotIn('token',json.dumps(self.ledger.inspect(task_id='task-1',effect_id='effect-1')))
 def test_private_token_file_mode_and_binding(self):
  p,pub,tok=self.prep();self.publish(p,pub,tok);self.assertEqual(p.stat().st_mode&0o777,0o600);rec=read_token_file(p);self.assertEqual((rec['task_id'],rec['effect_id'],rec['attempt']),('task-1','effect-1',1))
 def test_outcome_before_dispatch_rejected(self):
  p,pub,tok=self.prep();self.publish(p,pub,tok);rec=read_token_file(p)
  with self.assertRaisesRegex(EffectError,'OUTCOME_BEFORE_DISPATCH'):
   self.ledger.record_outcome(task_id='task-1',effect_id='effect-1',token_record=rec,kind='SUCCEEDED',receipt_ref='provider:x',receipt_sha256='0'*64)
 def test_success_and_exact_terminal_replay(self):
  p,pub,tok=self.prep();self.publish(p,pub,tok);rec=read_token_file(p);self.ledger.mark_dispatched(task_id='task-1',effect_id='effect-1',token_record=rec,expected_document_sha256=self.docsha,now='2026-09-14T09:06:00Z')
  kw=dict(task_id='task-1',effect_id='effect-1',token_record=rec,kind='SUCCEEDED',receipt_ref='provider:msg-1',receipt_sha256=hashlib.sha256(b'msg-1').hexdigest())
  first=self.ledger.record_outcome(**kw);self.assertEqual(first['state'],'SUCCEEDED');self.assertEqual(first,self.ledger.record_outcome(**kw));self.assertFalse(first['retry_authorized'])
  with self.assertRaisesRegex(EffectError,'CONFLICTING_TERMINAL_OUTCOME'):
   self.ledger.record_outcome(task_id='task-1',effect_id='effect-1',token_record=rec,kind='FAILED_FINAL',receipt_ref='provider:late',receipt_sha256='1'*64)
 def test_invalid_token_cannot_dispatch_or_finish(self):
  p,pub,tok=self.prep();self.publish(p,pub,tok);rec=read_token_file(p);bad=dict(rec,token=rec['token']+'x')
  with self.assertRaisesRegex(EffectError,'STALE_OR_INVALID'):
   self.ledger.mark_dispatched(task_id='task-1',effect_id='effect-1',token_record=bad,expected_document_sha256=self.docsha,now='2026-09-14T09:06:00Z')
  self.ledger.mark_dispatched(task_id='task-1',effect_id='effect-1',token_record=rec,expected_document_sha256=self.docsha,now='2026-09-14T09:06:00Z')
  with self.assertRaisesRegex(EffectError,'STALE_OR_INVALID'):
   self.ledger.record_outcome(task_id='task-1',effect_id='effect-1',token_record=bad,kind='SUCCEEDED',receipt_ref='provider:x',receipt_sha256='0'*64)
 def test_later_generation_after_dispatch_enters_reconciliation(self):
  p,pub,tok=self.prep();self.publish(p,pub,tok);rec=read_token_file(p);self.ledger.mark_dispatched(task_id='task-1',effect_id='effect-1',token_record=rec,expected_document_sha256=self.docsha,now='2026-09-14T09:06:00Z');self.write_inbox(2,'w2','l2')
  with self.assertRaisesRegex(EffectError,'RECONCILIATION_REQUIRED'):
   self.ledger.prepare(task_id='task-1',effect_id='effect-1',operation='send_email',payload=self.payload,worker_id='w2',lease_id='l2',attempt=2,token_path=normalized_token_path(self.root/'t2.json'),expected_document_sha256=self.docsha,now='2026-09-14T09:11:00Z')
  self.assertEqual(self.ledger.inspect(task_id='task-1',effect_id='effect-1')['state'],'RECONCILIATION_REQUIRED')
 def test_restart_persists_reconciliation_hold(self):
  p,pub,tok=self.prep();self.publish(p,pub,tok);rec=read_token_file(p);self.ledger.mark_dispatched(task_id='task-1',effect_id='effect-1',token_record=rec,expected_document_sha256=self.docsha,now='2026-09-14T09:06:00Z')
  with self.assertRaises(EffectError):self.ledger.mark_dispatched(task_id='task-1',effect_id='effect-1',token_record=rec,expected_document_sha256=self.docsha,now='2026-09-14T09:07:00Z')
  self.assertEqual(EffectLedger(self.db).inspect(task_id='task-1',effect_id='effect-1')['state'],'RECONCILIATION_REQUIRED')
 def test_concurrent_exact_prepare_has_one_secret_winner(self):
  import concurrent.futures
  def worker():
   try:
    return EffectLedger(self.db).prepare(task_id='task-1',effect_id='effect-concurrent',operation='send_email',payload=self.payload,worker_id='w',lease_id='l',attempt=1,token_path=normalized_token_path(self.root/'concurrent.json'),expected_document_sha256=self.docsha,now='2026-09-14T09:05:00Z')[1]
   except Exception as exc:return exc
  with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _x:worker(),range(2)))
  self.assertEqual(sum(isinstance(x,str) for x in results),1,results);self.assertEqual(sum(x is None for x in results),1,results)
 def test_cli_collision_is_controlled_and_retry_finishes(self):
  payload=self.root/'payload.json';payload.write_text(json.dumps(self.payload));target=self.root/'token.json';target.write_text('foreign');canonical=ROOT/'inbox.json';old_bytes=canonical.read_bytes() if canonical.exists() else None;canonical.write_text(json.dumps(self.doc),encoding='utf-8')
  try:
   cmd=[sys.executable,str(ROOT/'effect_receipts.py'),'--db',str(self.db)];base=['prepare','--task-id','task-1','--effect-id','effect-1','--operation','send_email','--payload-file',str(payload),'--worker-id','w','--lease-id','l','--attempt','1','--token-out',str(target),'--expected-document-sha256',self.docsha,'--now','2026-09-14T09:05:00Z']
   first=subprocess.run(cmd+base,text=True,capture_output=True);self.assertEqual(first.returncode,2,first.stderr);self.assertIn('TOKEN_PATH_OCCUPIED',first.stderr);target.unlink();second=subprocess.run(cmd+base,text=True,capture_output=True);self.assertEqual(second.returncode,0,second.stderr);self.assertEqual(json.loads(second.stdout)['state'],'PREPARED')
  finally:
   if old_bytes is None: canonical.unlink(missing_ok=True)
   else: canonical.write_bytes(old_bytes)

if __name__=='__main__':unittest.main()
