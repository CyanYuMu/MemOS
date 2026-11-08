#!/usr/bin/env python3
"""Run P4.1 qualification visibly without replacing the running P4 services."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys

from pathlib import Path


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_new(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    Path(path).chmod(0o600)


def verify_previous_evidence(root, previous):
    """Keep pre-run P4 reports unchanged and recheck their original receipt protocol."""
    from memos.skill_evaluation.verifier import check_receipt

    p4_root = previous.parents[1]
    hashes = read(root / "previous-record-hashes.json")
    for relative, expected in hashes.items():
        path = (p4_root / relative).resolve()
        if not path.is_relative_to(p4_root) or sha(path) != expected:
            raise ValueError("previous_evidence_changed")
    reports = [*previous.glob("runs/*/attempt-1/report.json")]
    reports += list((p4_root / "calibration/attempt-001").glob("*/report.json"))
    if len(reports) != 24:
        raise ValueError("previous_evidence_changed")
    for path in reports:
        report = read(path)
        checked = check_receipt(
            Path(report["receipt"]), path.parent / "candidate", path.parent / "validation"
        )
        if not checked.matches:
            raise ValueError("previous_evidence_changed")
    print("[p41] 原 21 次实验与 3 次校准的哈希、旧协议凭条复核通过。", flush=True)


def ensure_qualification(directory):
    from memos.skill_verification.selftest import require_qualification

    directory = Path(directory)
    if not directory.exists():
        print("[p41] 开始七任务隔离自检；这是任务资产检查，不是模型实验。", flush=True)
        result = subprocess.run(
            [
                sys.executable,
                "-u",
                "-m",
                "memos.skill_verification",
                "audit-tasks",
                "--output",
                str(directory),
            ],
            cwd=Path(__file__).resolve().parents[2],
            check=False,
        )
        if result.returncode:
            raise ValueError("qualification_failed")
    qualification = require_qualification(directory)
    print("[p41] 七任务隔离资格复核通过。", flush=True)
    return qualification


def ensure_native_probe(service, qualification_directory, output):
    from memos.skill_evaluation.tasks import case_digest, get_case
    from memos.skill_experiments.__main__ import runtime
    from memos.skill_experiments.manifest import sha as file_sha
    from memos.skill_verification.runner import adapter_digest, run_qualified_case
    from memos.skill_verification.selftest import require_qualification
    from memos.skill_verification.verifier import check_receipt

    output = Path(output)
    qualification = require_qualification(qualification_directory)
    config = runtime(service)
    case = get_case("holdout-fixture")
    identity = hashlib.sha256(
        json.dumps(
            {
                "purpose": "verifier_compatibility_validation",
                "qualification": qualification,
                "adapter": adapter_digest(),
                "case": case_digest(case),
                "model": config.model,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if output.exists() and not (output / "report.json").is_file():
        raise ValueError("native_probe_needs_review")
    if not output.exists():
        print("[p41] 开始一次 Claude H4 无经验兼容验证；不是新的独立效果样本。", flush=True)
        run_qualified_case(
            case.case_id,
            output,
            config,
            qualification_directory=qualification_directory,
            group="none",
            experiment_id=identity,
            expected_case_digest=case_digest(case),
            provider_environment=service["runtime"]["providerEnvironment"],
        )
    report = read(output / "report.json")
    if (
        report.get("status") != "passed"
        or report.get("experiment_id") != identity
        or report.get("verification_qualification") != qualification
        or report.get("adapter_sha256") != adapter_digest()
        or report.get("cli_sha256") != file_sha(config.cli_path.resolve())
        or report.get("task_accepted") is not True
        or report.get("capture", {}).get("status") != "complete"
    ):
        raise ValueError("native_probe_needs_review")
    receipt = Path(report["receipt"])
    if not receipt.resolve().is_relative_to((output / "validation").resolve()):
        raise ValueError("native_probe_needs_review")
    checked = check_receipt(receipt, output / "candidate", output / "validation")
    if not checked.matches or checked.verification_status != "passed":
        raise ValueError("native_probe_needs_review")
    print("[p41] Claude H4 兼容验证与最终凭条复核通过；未回写旧实验。", flush=True)
    return report


def main():
    from memos.skill_experiments.__main__ import runtime
    from memos.skill_experiments.manifest import load_experiment
    from memos.skill_management.config import read_config
    from memos.skill_verification.verifier import check_receipt, verify_candidate

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    if args.config.stat().st_mode & 0o077:
        raise ValueError("private_config_required")
    config = read(args.config)
    if set(config) != {"serviceConfig", "artifactsRoot", "previousExperiment"}:
        raise ValueError("invalid_config")
    service = read_config(Path(config["serviceConfig"]))
    previous = Path(config["previousExperiment"]).resolve()
    old_manifest = load_experiment(previous, runtime(service))
    root = Path(config["artifactsRoot"]).resolve()
    if root.is_relative_to(previous) or previous.is_relative_to(root):
        raise ValueError("invalid_config")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    verify_previous_evidence(root, previous)
    qualification = ensure_qualification(root / "qualification")
    observations = []
    # Recheck old final patches in a separate protocol; old receipts remain unknown.
    for group in ("none", "raw_memory", "skill"):
        old = previous / "runs" / ("holdout-fixture--" + group) / "attempt-1"
        original = read(old / "report.json")
        original_hashes = {"report": sha(old / "report.json"), "receipt": sha(original["receipt"])}
        target = root / "patch-rechecks" / group
        if target.exists():
            receipts = list(target.glob("runs/*/*/receipt.json"))
            if len(receipts) != 1:
                raise ValueError("recheck_needs_review")
            receipt = receipts[0]
        else:
            receipt = verify_candidate("holdout-fixture", old / "candidate", target).receipt_path
        checked = check_receipt(receipt, old / "candidate", target)
        if not checked.matches:
            raise ValueError("recheck_needs_review")
        if original_hashes != {
            "report": sha(old / "report.json"),
            "receipt": sha(original["receipt"]),
        }:
            raise ValueError("previous_evidence_changed")
        observations.append(
            {
                "group": group,
                "originalHashes": original_hashes,
                "receipt": str(receipt),
                "receiptSha256": sha(receipt),
                "status": checked.verification_status,
            }
        )
        print(
            "[p41] 旧 H4 最终补丁独立复验 " + group + ": " + checked.verification_status, flush=True
        )
    summary = {
        "purpose": "posthoc_verifier_compatibility_not_new_independent_sample",
        "previousExperimentId": old_manifest["experimentId"],
        "qualification": qualification,
        "originalRecordsReplaced": False,
        "newModelCalls": 0,
        "observations": observations,
    }
    summary_path = root / "patch-rechecks.json"
    if summary_path.exists():
        if read(summary_path) != summary:
            raise ValueError("recheck_needs_review")
    else:
        save_new(summary_path, summary)
    ensure_native_probe(service, root / "qualification", root / "native-h4-none")
    verify_previous_evidence(root, previous)
    print("[p41] 验收能力验证结束；P4 服务保持运行，人工审阅与发布尚未代签。", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        category = (
            str(error)
            if type(error) is ValueError
            and str(error)
            in {
                "private_config_required",
                "invalid_config",
                "qualification_failed",
                "native_probe_needs_review",
                "recheck_needs_review",
                "previous_evidence_changed",
            }
            else "verification_preparation_failed"
        )
        print("[p41] " + category + "；记录保留，未自动重跑或批准。", flush=True)
        raise SystemExit(1) from None
