"""Freeze or run the none-vs-skill comparison on the fixed P5 suites.

Usage:
  python -m memos.skill_boundary freeze --config SERVICE --candidate DIR \
      --qualification DIR --output NEW_DIR [--repetitions 1]
  python -m memos.skill_boundary run --config SERVICE --candidate DIR \
      --qualification DIR --output FROZEN_DIR
"""

import argparse
import json
import re

from pathlib import Path

from memos.skill_boundary import experiment


_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{2,63}")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Arguments can carry private paths; never echo them back.
        raise ValueError("boundary_invalid_arguments")


def main(argv=None):
    def emit(value):
        print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)

    try:
        parser = _Parser(description=__doc__)
        parser.add_argument("command", choices=("freeze", "run"))
        for name in ("config", "candidate", "qualification", "output"):
            parser.add_argument("--" + name, type=Path, required=True)
        parser.add_argument("--repetitions", type=int, default=1)
        args = parser.parse_args(argv)
        inputs = (args.config, args.candidate, args.qualification, args.output)
        if args.command == "freeze":
            result = experiment.freeze(*inputs, repetitions=args.repetitions)
        else:
            summary = experiment.run(*inputs, progress=emit)
            result = {
                "experimentId": summary["experimentId"],
                "complete": summary["complete"],
                **{k: summary[k] for k in ("groups", "pairs")},
                "publicationEvidence": False,
            }
        emit(result)
        return 0 if result.get("complete", True) else 1
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) else ""
        emit(
            {
                "status": "error",
                "errorCode": code if _SAFE_CODE.fullmatch(code) else "boundary_failed",
                "automaticRetry": False,
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
