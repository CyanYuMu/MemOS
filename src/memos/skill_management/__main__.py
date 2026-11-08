"""Explicit private service entry point; no credentials in command arguments."""

from __future__ import annotations

import argparse
import json
import logging

from pathlib import Path

from memos.skill_management.config import build_service, read_config, service_lock


class _Parser(argparse.ArgumentParser):
    def error(self, _):
        raise ValueError("invalid_arguments")


def main(argv=None):
    try:
        parser = _Parser(description="P3 Skill 管理服务（私有配置，单宿主运行）")
        parser.add_argument("command", choices=["serve", "check"])
        parser.add_argument("--config", required=True, type=Path)
        args = parser.parse_args(argv)
        config = read_config(args.config)
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True
        )
        with service_lock(config):
            service = build_service(config)
            try:
                if args.command == "check":
                    print(json.dumps({"status": "ready", "projects": len(service.projects)}))
                    return 0
                from memos.skill_management.api import server_for

                with server_for(
                    service,
                    config["serviceToken"],
                    (config["listen"]["host"], config["listen"]["port"]),
                ) as server:
                    logging.info(
                        "Skill management ready on loopback port %s", config["listen"]["port"]
                    )
                    server.serve_forever()
            finally:
                service.close()
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Provider/Neo4j/Memory exceptions may carry deployment credentials.
        print(json.dumps({"error": "management_startup_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
