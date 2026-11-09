#!/usr/bin/env python3
"""Run the frozen two-unit combined batch; no review or publication is automatic."""

import argparse
import json

from pathlib import Path

from memos.skill_branch_extensions.batch import run_batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--qualification", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    def emit(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)

    try:
        result = run_batch(
            args.config, args.candidate, args.qualification, args.output, progress=emit
        )
        emit(result)
        return 0 if result["success"] else 2
    except KeyboardInterrupt:
        emit({"error": "batch_interrupted", "artifactsRetained": True})
        return 130
    except Exception as error:
        emit({"error": "batch_preflight_failed", "errorType": type(error).__name__})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
