"""Create new source evidence and an unpublished Skill without private fixtures."""

import json

from pathlib import Path

from memos.skill_contracts.__main__ import Parser
from memos.skill_demo import workflow


def main(argv=None):
    def emit(value):
        print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)

    stage = "arguments"
    try:
        parser = Parser(description=__doc__)
        parser.add_argument(
            "command",
            choices=("doctor", "selfcheck", "init", "collect", "ingest", "draft", "graph"),
        )
        parser.add_argument("--config", type=Path)
        parser.add_argument("--output", type=Path)
        args = parser.parse_args(argv)
        stage = args.command
        config = workflow.load_config(args.config) if args.config else None
        if stage == "doctor":
            result = workflow.doctor(config)
        else:
            workflow.require(args.output is not None)
            if stage == "selfcheck":
                result = workflow.selfcheck(args.output, emit)
            else:
                workflow.require(config is not None)
                if stage == "init":
                    result = workflow.init_demo(config, args.output)
                elif stage in {"collect", "ingest"}:
                    result = getattr(workflow, stage)(config, args.output, emit)
                else:
                    result = getattr(workflow, stage)(config, args.output)
        emit(result)
        return 0 if result["status"] in {"ready", "initialized", "completed", "passed"} else 1
    except Exception:
        # No filesystem paths, endpoints, SDK bodies or credentials in diagnostics.
        emit({"status": "error", "errorCode": "demo_" + stage + "_failed", "automaticRetry": False})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
