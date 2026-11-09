"""Run one published Skill through the normal admission path under the service lock.

The configuration and retained job record belong in a private artifacts directory.
Stop the management server first; this command never bypasses its publication gate.
"""

import argparse
import json
import os
import time

from pathlib import Path

from memos.skill_management.config import build_service, read_config, service_lock


parser = argparse.ArgumentParser()
parser.add_argument("--config", type=Path, required=True)
parser.add_argument("--project", required=True)
parser.add_argument("--skill", required=True)
parser.add_argument("--revision", required=True)
parser.add_argument("--case", required=True)
parser.add_argument("--operation", required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
if args.output.exists():
    raise SystemExit("fresh_output_required")
os.umask(0o022)
config = read_config(args.config)
try:
    with service_lock(config):
        service = build_service(config)
        try:
            job = service.start(
                args.project,
                args.skill,
                args.revision,
                args.case,
                args.operation,
                "用户授权、助手受托执行P5",
                mode="normal",
            )
            print(json.dumps({"stage": "started", "jobId": job["jobId"]}), flush=True)
            repo = service.projects[args.project].repository
            deadline = time.monotonic() + 360
            previous = None
            while True:
                job = repo.get_job(job["jobId"])
                public = {k: job.get(k) for k in ("jobId", "status", "validationId", "errorCode")}
                if public != previous:
                    print(json.dumps(public, ensure_ascii=False), flush=True)
                    previous = public
                if job["status"] not in {"queued", "running"}:
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("host_wait_exceeded")
                time.sleep(1)
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(json.dumps(job, ensure_ascii=False, indent=2) + "\n")
        finally:
            service.close()
except Exception as exc:
    print(
        json.dumps({"errorType": type(exc).__name__, "code": getattr(exc, "code", None)}),
        flush=True,
    )
    raise SystemExit(1) from None
