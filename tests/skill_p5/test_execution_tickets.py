"""Host-issued identities exist before native capture and bind immutable artifacts."""

import json

import pytest

from p5_run_fixture import make_run


class Guard:
    def __init__(self):
        self.ticket = None
        self.finished = []

    def begin(self, metadata):
        assert self.ticket is None
        self.ticket = {
            **metadata,
            "ticketId": "1" * 64,
            "runId": "2" * 64,
            "attemptId": "3" * 32,
            "startedSequence": 5,
            "observationIds": ["4" * 64],
        }
        return self.ticket

    def finish(self, ticket, artifacts):
        assert ticket == self.ticket
        assert set(artifacts) == {
            "reportSha256",
            "eventsSha256",
            "initialReceiptSha256",
            "receiptSha256",
        }
        self.finished.append(artifacts)
        return {
            "ticketId": ticket["ticketId"],
            "status": "completed",
            "finishedSequence": 6,
            "observationIds": ticket["observationIds"],
            **artifacts,
        }


def test_ticket_ids_drive_native_events_and_all_receipts_before_capture(tmp_path, monkeypatch):
    guard = Guard()
    f = make_run(tmp_path, monkeypatch, execution_guard=guard)
    report = f["report"]
    assert report["run_context"]["run_id"] == guard.ticket["runId"]
    assert report["run_context"]["attempt_id"] == guard.ticket["attemptId"]
    assert report["execution_ticket"] == guard.ticket
    events = [json.loads(row) for row in (f["run"] / "events.jsonl").read_text().splitlines()]
    assert all(e["context"]["run_id"] == guard.ticket["runId"] for e in events)
    final = json.loads((f["run"] / "execution-ticket.json").read_text())
    assert final["status"] == "completed" and len(guard.finished) == 1
    assert (
        final["reportSha256"]
        == __import__("hashlib").sha256((f["run"] / "report.json").read_bytes()).hexdigest()
    )
    with pytest.raises(ValueError, match="already exists"):
        from memos.skill_p5.runner import run_extension_task
        from memos.skill_p5.tasks import get_suite, suite_digest

        suite = get_suite("update-repair-r1")
        run_extension_task(
            suite.suite_id,
            f["run"],
            f["config"],
            qualification_directory=f["qualification"],
            group="skill",
            experiment_id=report["experiment_id"],
            expected_suite_digest=suite_digest(suite),
            skill=f["skill"],
            expected_export_hash=f["export_hash"],
            execution_guard=guard,
            execution_metadata={
                k: guard.ticket[k]
                for k in (
                    "batchId",
                    "unitId",
                    "caseId",
                    "group",
                    "mode",
                    "contentHash",
                    "exportHash",
                )
            },
        )
    assert len(guard.finished) == 1
