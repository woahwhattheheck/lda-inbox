#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
from typing import Optional
from effect_receipt_common import EffectError
from effect_receipt_ledger import EffectLedger
from effect_receipt_io import load_payload_file,normalized_token_path,read_token_file,token_path_missing,write_token_file
__all__=['EffectError','EffectLedger','read_token_file','write_token_file']
def _print(obj):print(json.dumps(obj,sort_keys=True,separators=(',',':')))
def _prepare_auth_args(p):
    p.add_argument('--expected-document-sha256',required=True);p.add_argument('--now',required=True)
def _dispatch_auth_args(p):
    p.add_argument('--expected-document-sha256',required=True)
def _publish(ledger,public,token,path):
    write_token_file(path,task_id=public['task_id'],effect_id=public['effect_id'],effect_generation=public['effect_generation'],token=token,worker_id=public['lease']['worker_id'],lease_id=public['lease']['lease_id'],attempt=public['lease']['attempt'],task_sha256=public['task_sha256'])
    record=read_token_file(path);return ledger.finalize_prepare(task_id=public['task_id'],effect_id=public['effect_id'],token_record=record)
def main(argv:Optional[list[str]]=None)->int:
    parser=argparse.ArgumentParser(description='lda-inbox external-effect receipt ledger');parser.add_argument('--db',required=True);sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('prepare')
    for flag in ('task-id','effect-id','operation','payload-file','worker-id','lease-id','token-out'):p.add_argument('--'+flag,required=True)
    p.add_argument('--attempt',required=True,type=int);_prepare_auth_args(p)
    d=sub.add_parser('dispatch');d.add_argument('--token-file',required=True);_dispatch_auth_args(d)
    for name in ('succeed','fail-final'):
        q=sub.add_parser(name);q.add_argument('--token-file',required=True);q.add_argument('--receipt-ref',required=True);q.add_argument('--receipt-sha256',required=True)
    i=sub.add_parser('inspect');i.add_argument('--task-id',required=True);i.add_argument('--effect-id',required=True)
    args=parser.parse_args(argv)
    try:
        ledger=EffectLedger(args.db)
        if args.command=='prepare':
            path=Path(args.token_out);token_path=normalized_token_path(path)
            public,token=ledger.prepare(task_id=args.task_id,effect_id=args.effect_id,operation=args.operation,payload=load_payload_file(Path(args.payload_file)),worker_id=args.worker_id,lease_id=args.lease_id,attempt=args.attempt,token_path=token_path,expected_document_sha256=args.expected_document_sha256,now=args.now)
            if token is not None: public=_publish(ledger,public,token,path)
            elif public['state']=='TOKEN_PENDING':
                if token_path_missing(path):
                    public,token=ledger.rotate_pending_token(task_id=args.task_id,effect_id=args.effect_id,token_path=token_path,expected_document_sha256=args.expected_document_sha256,now=args.now);public=_publish(ledger,public,token,path)
                else:
                    public=ledger.finalize_prepare(task_id=args.task_id,effect_id=args.effect_id,token_record=read_token_file(path))
            elif public['state']=='PREPARED':
                if token_path_missing(path):raise EffectError('TOKEN_FILE_MISSING_FOR_PREPARED')
                public=ledger.finalize_prepare(task_id=args.task_id,effect_id=args.effect_id,token_record=read_token_file(path))
            _print(public);return 0
        if args.command=='dispatch':
            record=read_token_file(Path(args.token_file));_print(ledger.mark_dispatched(task_id=record['task_id'],effect_id=record['effect_id'],token_record=record,expected_document_sha256=args.expected_document_sha256));return 0
        if args.command in ('succeed','fail-final'):
            record=read_token_file(Path(args.token_file));_print(ledger.record_outcome(task_id=record['task_id'],effect_id=record['effect_id'],token_record=record,kind='SUCCEEDED' if args.command=='succeed' else 'FAILED_FINAL',receipt_ref=args.receipt_ref,receipt_sha256=args.receipt_sha256));return 0
        _print(ledger.inspect(task_id=args.task_id,effect_id=args.effect_id));return 0
    except (EffectError,OSError) as exc:
        print(json.dumps({'ok':False,'error':str(exc)},sort_keys=True,separators=(',',':')),file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())
