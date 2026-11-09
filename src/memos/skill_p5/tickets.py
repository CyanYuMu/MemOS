"""Bind host execution identities to immutable native and verification artifacts."""

from __future__ import annotations

import re

from memos.skill_management.domain import digest, require


METADATA_KEYS = {"batchId", "unitId", "caseId", "group", "mode", "contentHash", "exportHash"}
START_KEYS = METADATA_KEYS | {"ticketId", "runId", "attemptId", "startedSequence", "observationIds"}
ARTIFACT_KEYS = {"reportSha256", "eventsSha256", "initialReceiptSha256", "receiptSha256"}


def validate_start(ticket, expected):
    require(
        isinstance(expected, dict) and set(expected) == METADATA_KEYS, "execution_metadata_invalid"
    )
    require(isinstance(ticket, dict) and set(ticket) == START_KEYS, "execution_ticket_invalid")
    require(
        all(ticket[key] == expected[key] for key in METADATA_KEYS), "execution_ticket_binding", 409
    )
    for key in ("ticketId", "runId", "batchId", "contentHash", "exportHash"):
        digest(ticket[key])
    require(
        isinstance(ticket["attemptId"], str)
        and re.fullmatch(r"[a-f0-9]{32}", ticket["attemptId"]) is not None,
        "execution_ticket_identity",
    )
    require(
        type(ticket["startedSequence"]) is int and ticket["startedSequence"] >= 0,
        "execution_ticket_sequence",
    )
    require(isinstance(ticket["observationIds"], list), "execution_ticket_observations")
    for item in ticket["observationIds"]:
        digest(item)
    return ticket


def artifact_hashes(report, report_hash):
    return {
        "reportSha256": report_hash,
        "eventsSha256": report["events_sha256"],
        "initialReceiptSha256": report["initial_receipt_sha256"],
        "receiptSha256": report["receipt_sha256"],
    }


def validate_finish(value, ticket, artifacts):
    require(isinstance(value, dict), "execution_finish_invalid")
    require(
        value.get("ticketId") == ticket["ticketId"]
        and value.get("status") in {"completed", "invalid"},
        "execution_finish_binding",
        409,
    )
    require(
        type(value.get("finishedSequence")) is int
        and value["finishedSequence"] > ticket["startedSequence"],
        "execution_finish_sequence",
        409,
    )
    require(
        all(value.get(key) == artifacts[key] for key in ARTIFACT_KEYS),
        "execution_finish_artifacts",
        409,
    )
    return value
