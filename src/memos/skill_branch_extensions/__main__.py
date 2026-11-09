"""Export the initial task or run host asset checks without invoking a model."""

import argparse
import json

from pathlib import Path

from memos.skill_adapter.isolation import IsolationUnavailableError
from memos.skill_branch_extensions.selftest import audit_suite
from memos.skill_branch_extensions.tasks import get_suite, materialize, suite_manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--suite", default="combined-c1-v1")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--suite", required=True)
    prepare.add_argument("--workspace", type=Path, required=True)
    audit = commands.add_parser("audit", help="Run fixed no-fork checks (macOS backend required)")
    audit.add_argument("--suite", required=True)
    audit.add_argument("--output", type=Path, required=True)
    audit.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            result = suite_manifest(get_suite(args.suite))
        elif args.command == "prepare":
            result = {
                "workspace": str(materialize(args.suite, args.workspace)),
                "variant": "initial",
            }
        else:
            result = audit_suite(args.suite, args.output, args.timeout)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("passed", True) else 1
    except (OSError, ValueError, TypeError, IsolationUnavailableError) as error:
        print(
            json.dumps(
                {"error": "extension_asset_command_failed", "errorType": type(error).__name__}
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
