import copy,json,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from effect_receipt_audit import audit_document
from effect_receipt_common import SCHEMA,EffectError
from effect_receipt_ledger import EffectLedger,read_effects_snapshot
import effect_receipt_ledger
from unittest import mock
from task_protocol import task_spec_sha256


def task(task_id='task-1',done=False):
    value={'id':task_id,'created':'2026-10-15T12:00:00Z','kind':'run_task','command':'do it','timeout_s':60,'done':done}
    if done:value.update(completed_at='2026-10-15T12:00:10Z',result='done')
    return value


def row(t,state='PREPARED',effect_id='effect-1'):
    terminal=state in ('SUCCEEDED','FAILED_FINAL')
    dispatched=state in ('DISPATCHED','RECONCILIATION_REQUIRED') or terminal
    outcome={'kind':state,'receipt_ref':'synthetic:r1','receipt_sha256':'2'*64} if terminal else None
    return {'schema':SCHEMA,'task_id':t['id'],'effect_id':effect_id,'operation':'send_email','payload_sha256':'1'*64,'task_sha256':task_spec_sha256(t),'lease':{'worker_id':'w','lease_id':'l','attempt':1},'effect_generation':1,'state':state,'dispatch_count':1 if dispatched else 0,'outcome':outcome,'retry_authorized':False,'authority':{}}


class AuditTests(unittest.TestCase):
    def report(self,tasks,rows):return audit_document({'v':1,'tasks':tasks},rows)
    def test_unresolved_never_authorizes_completion(self):
        for state in ('TOKEN_PENDING','PREPARED','DISPATCHED','RECONCILIATION_REQUIRED'):
            with self.subTest(state=state):
                report=self.report([task()],[row(task(),state)])
                self.assertFalse(report['authorizes_completion'])
                self.assertEqual(report['findings'][0]['code'],'UNRESOLVED_EFFECT')
    def test_completed_unresolved_is_contradiction(self):
        t=task(done=True);report=self.report([t],[row(t,'DISPATCHED')])
        self.assertEqual(report['findings'][0]['code'],'COMPLETED_WITH_UNRESOLVED_EFFECT')
    def test_orphan_and_binding_mismatch(self):
        current=task();changed=copy.deepcopy(current);changed['command']='changed'
        mismatch=row(current);mismatch['task_sha256']=task_spec_sha256(changed)
        orphan=row(task('missing'),effect_id='e2')
        report=self.report([current],[orphan,mismatch])
        self.assertEqual([x['code'] for x in report['findings']],['ORPHAN_EFFECT','TASK_BINDING_MISMATCH'])
    def test_terminal_is_not_provider_or_payment_proof(self):
        t=task();terminal=row(t,'SUCCEEDED')
        report=self.report([t],[terminal])
        self.assertEqual(report['findings'][0]['code'],'TERMINAL_EFFECT')
        self.assertFalse(report['limitations']['external_effect_execution_proven'])
        self.assertFalse(report['limitations']['provider_authenticity_proven'])
        self.assertFalse(report['limitations']['payment_or_delivery_proven'])
    def test_insertion_order_is_byte_identical_and_private_fields_absent(self):
        t=task();a=row(t,effect_id='a');b=row(t,effect_id='b')
        first=json.dumps(self.report([t],[b,a]),sort_keys=True,separators=(',',':'))
        second=json.dumps(self.report([t],[a,b]),sort_keys=True,separators=(',',':'))
        self.assertEqual(first,second)
        for forbidden in ('token_hash','token_path','/tmp/private'):self.assertNotIn(forbidden,first)
    def test_duplicate_snapshot_identity_fails_closed(self):
        t=task();item=row(t)
        with self.assertRaisesRegex(EffectError,'DUPLICATE_EFFECT'):self.report([t],[item,item])
    def test_ledger_snapshot_is_ordered_public_and_row_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=EffectLedger(Path(directory)/'effects.sqlite');con=ledger._connect()
            try:
                for effect_id in ('z','a'):
                    con.execute("INSERT INTO effects VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",('task-1',effect_id,'op','1'*64,'2'*64,'w','l',1,1,'secret-hash','/tmp/private','PREPARED',0,None,None,None))
            finally:con.close()
            tracked=[ledger.db_path+suffix for suffix in ('','-wal','-shm','-journal')]
            before={path:Path(path).read_bytes() for path in tracked if Path(path).exists()}
            snapshot=read_effects_snapshot(ledger.db_path)
            after={path:Path(path).read_bytes() for path in tracked if Path(path).exists()}
            self.assertEqual([x['effect_id'] for x in snapshot],['a','z']);self.assertEqual(before,after)
            self.assertNotIn('secret-hash',json.dumps(snapshot));self.assertNotIn('/tmp/private',json.dumps(snapshot))
    def test_inconsistent_terminal_and_unresolved_rows_fail_closed(self):
        t=task();terminal=row(t,'SUCCEEDED');terminal['outcome']=None;unresolved=row(t,'DISPATCHED','e2');unresolved['dispatch_count']=0
        with self.assertRaisesRegex(EffectError,'INVALID_TERMINAL'):self.report([t],[terminal])
        with self.assertRaisesRegex(EffectError,'INVALID_UNRESOLVED'):self.report([t],[unresolved])
    def test_writer_race_fails_instead_of_returning_stale_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=EffectLedger(Path(directory)/'effects.sqlite');con=ledger._connect()
            values=('task-1','a','op','1'*64,'2'*64,'w','l',1,1,'hash','/private','PREPARED',0,None,None,None)
            con.execute("INSERT INTO effects VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",values);con.close()
            original=effect_receipt_ledger._descriptor_db_uri;writer=[]
            def race(fd,identity,**kwargs):
                other=sqlite3.connect(ledger.db_path,isolation_level=None);writer.append(other)
                other.execute("INSERT INTO effects VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",tuple('b' if i==1 else value for i,value in enumerate(values)))
                return original(fd,identity,**kwargs)
            try:
                with mock.patch.object(effect_receipt_ledger,'_descriptor_db_uri',side_effect=race):
                    with self.assertRaisesRegex(EffectError,'AUDIT_DB_HAS_SIDECAR|AUDIT_DB_CHANGED_DURING_READ'):
                        read_effects_snapshot(ledger.db_path)
            finally:
                for item in writer:item.close()
    def test_cli_rejects_malformed_inbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);bad=root/'bad.json';bad.write_text('{bad',encoding='utf-8')
            result=subprocess.run([sys.executable,str(ROOT/'effect_receipts.py'),'--db',str(root/'effects.sqlite'),'audit','--inbox',str(bad)],text=True,capture_output=True)
            self.assertEqual(result.returncode,2);self.assertIn('ok',result.stderr);self.assertFalse((root/'effects.sqlite').exists())
    def test_cli_corrupt_db_is_controlled_and_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);inbox=root/'inbox.json';inbox.write_text(json.dumps({'v':1,'tasks':[task()]}),encoding='utf-8')
            db=root/'effects.sqlite';db.write_bytes(b'not sqlite');db.chmod(0o600);before=db.read_bytes()
            result=subprocess.run([sys.executable,str(ROOT/'effect_receipts.py'),'--db',str(db),'audit','--inbox',str(inbox)],text=True,capture_output=True)
            self.assertEqual(result.returncode,2);self.assertIn('AUDIT_DB_',result.stderr);self.assertNotIn('Traceback',result.stderr);self.assertEqual(db.read_bytes(),before)


if __name__=='__main__':unittest.main()
