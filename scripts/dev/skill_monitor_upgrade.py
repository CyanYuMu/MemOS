"""Extend a paused release's monitoring under the exclusive service ownership lock."""

import argparse
import json

from pathlib import Path

from memos.skill_management.config import read_config, service_lock
from memos.skill_management.maintenance import normal_monitor_subject
from memos.skill_management.repository import ManagementRepository


def main():
    parser = argparse.ArgumentParser(description="追加正常执行监测；暂停及恢复需单独审阅")
    parser.add_argument("--config", type=Path, required=True)
    for field in ("project", "skill", "revision", "binding", "operation", "reason"):
        parser.add_argument("--" + field, required=True)
    parser.add_argument("--sequence", type=int, required=True)
    args = parser.parse_args()
    config = read_config(args.config)
    setting = next(p for p in config["projects"] if p["projectId"] == args.project)
    with service_lock(config):
        repo = ManagementRepository(project_id=setting["graphProject"], **config["graph"])
        try:
            result = repo.extend_lifecycle(
                args.skill,
                args.revision,
                additions=[normal_monitor_subject(config["runtime"]["cliPath"])],
                expected_binding=args.binding,
                expected_sequence=args.sequence,
                actor="local-maintenance",
                operation_id=args.operation,
                reason=args.reason,
            )
            print(json.dumps(result, ensure_ascii=False))
        finally:
            repo.close()


if __name__ == "__main__":
    main()
