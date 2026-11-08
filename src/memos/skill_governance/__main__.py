"""Explicit host registry import; intentionally no publication or arbitrary execution."""

import argparse
import json

from pathlib import Path

from memos.skill_governance.registry import import_evidence


def emit(value):
    print("[governance] " + json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    command = subcommands.add_parser("import-evidence")
    command.add_argument("--registry", required=True, type=Path)
    args = parser.parse_args()
    try:
        emit(import_evidence(args.registry, progress=emit))
        return 0
    except KeyboardInterrupt:
        emit({"error": "import_interrupted", "humanJudgmentsCreated": 0})
        return 130
    except Exception:
        emit({"error": "evidence_import_failed", "humanJudgmentsCreated": 0})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
