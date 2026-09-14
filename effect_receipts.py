#!/usr/bin/env python3
"""Fail-closed external-effect receipt ledger for lda-inbox.

This CLI records coordination truth only. It never performs an external effect.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from effect_receipt_common import EffectError
from effect_receipt_ledger import EffectLedger
from effect_receipt_io import load_payload_file, read_token_file, write_token_file

__all__=["EffectError","EffectLedger","read_token_file","write_token_file"]


def _print(obj): print(json.dumps(obj,sort_keys=True,separators=(",",":")))

def main(argv: Optional[list[str]]=None)->int:
    parser=argparse.ArgumentParser(description="lda-inbox external-effect receipt ledger"); parser.add_argument("--db",required=True)
    sub=parser.add_subparsers(dest="command",required=True)
    p=sub.add_parser("prepare")
    for flag in ("task-id","effect-id","operation","payload-file","worker-id","lease-id","token-out"): p.add_argument("--"+flag,required=True)
    p.add_argument("--attempt",required=True,type=int)
    for name in ("dispatch","succeed","fail-final"):
        q=sub.add_parser(name); q.add_argument("--token-file",required=True)
        if name!="dispatch": q.add_argument("--receipt-ref",required=True); q.add_argument("--receipt-sha256",required=True)
    i=sub.add_parser("inspect"); i.add_argument("--task-id",required=True); i.add_argument("--effect-id",required=True)
    args=parser.parse_args(argv)
    try:
        ledger=EffectLedger(args.db)
        if args.command=="prepare":
            public,token=ledger.prepare(task_id=args.task_id,effect_id=args.effect_id,operation=args.operation,payload=load_payload_file(Path(args.payload_file)),worker_id=args.worker_id,lease_id=args.lease_id,attempt=args.attempt)
            if token is None: raise EffectError("TOKEN_ALREADY_ISSUED_REUSE_PRIVATE_TOKEN_FILE")
            write_token_file(Path(args.token_out),task_id=args.task_id,effect_id=args.effect_id,token=token); _print(public); return 0
        if args.command in ("dispatch","succeed","fail-final"):
            task_id,effect_id,token=read_token_file(Path(args.token_file))
            if args.command=="dispatch": _print(ledger.mark_dispatched(task_id=task_id,effect_id=effect_id,token=token))
            else: _print(ledger.record_outcome(task_id=task_id,effect_id=effect_id,token=token,kind="SUCCEEDED" if args.command=="succeed" else "FAILED_FINAL",receipt_ref=args.receipt_ref,receipt_sha256=args.receipt_sha256))
            return 0
        _print(ledger.inspect(task_id=args.task_id,effect_id=args.effect_id)); return 0
    except EffectError as exc:
        print(json.dumps({"ok":False,"error":str(exc)},sort_keys=True,separators=(",",":")),file=sys.stderr); return 2

if __name__=="__main__": raise SystemExit(main())
