"""Reuse the bounded Unix transport; execute only this catalog's fixed checks."""

import json
import tempfile

from pathlib import Path

from memos.skill_adapter.broker import VisibleCheckBroker as Transport
from memos.skill_adapter.broker import _failure
from memos.skill_evaluation.tasks import candidate_policy_violations
from memos.skill_transfer.tasks import get_case
from memos.skill_transfer.verification import run_checks, write


class VisibleCheckBroker(Transport):
    def __init__(self, case_id, workspace, evidence, *, secret_values=(), cancellation_event=None):
        # The inherited constructor only initializes bounded transport/state.
        # Replace its legacy task immediately; no legacy task is ever executed.
        super().__init__("dev-strict-signature", workspace, evidence, secret_values=secret_values)
        self.case = get_case(case_id)
        self.cancellation_event = cancellation_event

    def _run(self):
        if len(self.records) >= self.max_calls:
            return _failure("test_budget_exhausted")
        try:
            snapshot = self.store.capture(self.workspace)
            if candidate_policy_violations(self.case, self.workspace):
                return _failure("protected_task_changed")
            index = len(self.records) + 1
            with tempfile.TemporaryDirectory(prefix="memos-transfer-visible-") as temp:
                candidate = self.store.restore(snapshot.snapshot_id, Path(temp) / "candidate")
                checked = run_checks(
                    self.case,
                    candidate,
                    self.evidence / f"phase-{index}",
                    hidden=False,
                    secret_values=self.secret_values,
                    cancellation_event=self.cancellation_event,
                )
            result = {
                "exit_code": 0
                if checked["status"] == "passed"
                else 1
                if checked["status"] == "failed"
                else None,
                "stdout": json.dumps({"checks": checked["checks"]}),
                "stderr": "",
                "truncated": False,
                "timed_out": bool(checked["execution"].get("timed_out")),
                "execution_error": "guarded_checks_failed"
                if checked["status"] == "error"
                else None,
                "evidence_ref": {"check_index": index, "candidate_snapshot": snapshot.snapshot_id},
            }
            record = {
                "check_index": index,
                "candidate_snapshot": snapshot.snapshot_id,
                "result": result,
            }
            write(self.evidence / f"check-{index}.json", record)
            self.records.append(record)
            return result
        except (ValueError, OSError):
            return _failure("test_input_or_isolation_invalid")
