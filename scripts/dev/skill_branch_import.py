#!/usr/bin/env python3
"""Register current branch facts without running models or approving a Skill."""

import argparse
import json

from pathlib import Path

from memos.skill_branch_validation.importer import import_batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "batch", "qualification", "allowed-root", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--skill", required=True)
    args = parser.parse_args()
    try:
        result = import_batch(
            args.config,
            args.project,
            args.skill,
            args.batch,
            args.qualification,
            args.allowed_root,
            args.output,
            progress=lambda v: print(json.dumps(v, ensure_ascii=False), flush=True),
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except Exception as error:
        print(
            json.dumps({"error": "branch_import_failed", "errorType": type(error).__name__}),
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
