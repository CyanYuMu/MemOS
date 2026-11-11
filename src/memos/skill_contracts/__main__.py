"""Inspect semantic mappings or audit one retained transfer batch offline."""

import argparse
import json

from pathlib import Path

from memos.skill_contracts.audit import audit_batch
from memos.skill_contracts.mapping import catalog, contract


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("invalid_arguments")


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        commands = parser.add_subparsers(dest="command", required=True)
        show = commands.add_parser("catalog")
        show.add_argument("--case")
        audit = commands.add_parser("audit-transfer")
        audit.add_argument("--batch", type=Path, required=True)
        audit.add_argument("--output", type=Path, required=True)
        args = parser.parse_args(argv)
        if args.command == "catalog":
            result = (
                contract(args.case)
                if args.case
                else {**catalog(), "tasks": {k: contract(k) for k in catalog()["tasks"]}}
            )
        else:
            result = audit_batch(args.batch, args.output)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"status": "error", "errorCode": "contract_audit_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
