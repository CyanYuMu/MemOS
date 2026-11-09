"""Small private HTTP surface behind the authenticated Go control plane."""

from __future__ import annotations

import hmac
import json
import logging
import re

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from memos.skill_management.domain import (
    ManagementError,
    digest,
    identity,
    operation,
    require,
    text,
)


_KEY = {"projectId", "skillId", "revision"}
_MUTATION = _KEY | {"expectedSequence", "operationId", "reason"}
# Required fields and the capability checked before dispatching a command.
COMMANDS = {
    "experiments_list": ({"projectId"}, "view"),
    "experiment_detail": ({"projectId", "experimentId"}, "view"),
    "experiment_start": ({"projectId", "experimentId", "operationId"}, "write"),
    "list": ({"projectId"}, "view"),
    "detail": (_KEY, "view"),
    "evidence": (_KEY | {"ruleId"}, "view"),
    "evaluate": (_KEY | {"caseId", "operationId"}, "write"),
    "job": ({"projectId", "jobId"}, "view"),
    "review_prepare": (_KEY, "manage"),
    "adjudication_prepare": (_KEY | {"assessmentId"}, "manage"),
    "adjudicate": (_MUTATION | {"packageId", "packageHash", "judgment"}, "manage"),
    "review_decide": (_MUTATION | {"decision"}, "manage"),
    "publish": (_MUTATION | {"packageId", "packageHash"}, "manage"),
    "status": (_MUTATION | {"action"}, "manage"),
    "challenge": (_MUTATION, "manage"),
    "impact_scan": (_MUTATION, "manage"),
    "impact_prepare": (_KEY, "manage"),
    "impact_review": (
        _MUTATION
        | {
            "packageId",
            "packageHash",
            "decision",
            "rootIds",
            "validationIds",
            "replacementRevision",
        },
        "manage",
    ),
    "challenge_resolve": (
        _MUTATION
        | {
            "packageId",
            "packageHash",
            "challengeId",
            "decision",
            "evidenceIds",
            "replacementRevision",
        },
        "manage",
    ),
    "load_preview": (_KEY | {"caseId", "mode"}, "view"),
}
_CAPABILITIES = {"view": 1, "write": 2, "manage": 3}
_LOG = logging.getLogger(__name__)


def decode(raw: bytes) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value)
            value[key] = item
        return value

    def constant(_):
        raise ManagementError("invalid_input")

    require(len(raw) <= 32768)
    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ManagementError("invalid_input") from None
    require(isinstance(value, dict))
    return value


def _validate(action, body):
    required, _ = COMMANDS[action]
    optional = (
        {"query"} if action == "list" else {"supersedes"} if action == "adjudicate" else set()
    )
    require(required <= set(body) <= required | optional)
    for key, value in body.items():
        if key == "judgment":
            if (
                isinstance(value, dict)
                and isinstance(value.get("taskDecision"), str)
                and value.get("taskDecision")
                in {
                    "healthy_confirmed",
                    "unresolved_confirmed",
                }
            ):
                from memos.skill_release.governance import validate_judgment_shape
            else:
                from memos.skill_governance.domain import validate_judgment_shape
            validate_judgment_shape(value)
        elif key in {"rootIds", "validationIds", "evidenceIds"}:
            from memos.skill_lifecycle.repository import digests

            digests(value)
        elif key in {"supersedes", "replacementRevision"}:
            if value is not None:
                digest(value)
        elif key == "expectedSequence":
            require(type(value) is int and 0 <= value <= 2**53 - 1)
        elif key == "query":
            require(isinstance(value, str) and len(value) <= 200)
        else:
            text(value, maximum=2000 if key == "reason" else 300)
    if "skillId" in body:
        identity(body["skillId"], body["revision"])
    if "operationId" in body:
        operation(body["operationId"])
    for key in ("packageHash", "packageId", "challengeId"):
        if key in body:
            digest(body[key])
    if "assessmentId" in body:
        digest(body["assessmentId"])
    if "experimentId" in body:
        digest(body["experimentId"])
    if action == "impact_review":
        require(body["decision"] in {"restore", "retain", "supersede"})
        require((body["replacementRevision"] is not None) == (body["decision"] == "supersede"))
        require(body["decision"] != "restore" or bool(body["validationIds"]))
        require(
            body["decision"] != "supersede" or (not body["rootIds"] and not body["validationIds"])
        )
    if action == "challenge_resolve":
        require(body["decision"] in {"addressed", "upheld", "superseded"})
        require((body["replacementRevision"] is not None) == (body["decision"] == "superseded"))
        require(body["decision"] != "addressed" or bool(body["evidenceIds"]))
    if action == "review_decide":
        require(body["decision"] in {"needs_evidence", "block"})
    if action == "status":
        require(body["action"] in {"pause", "withdraw"})
    if action == "load_preview":
        require(body["mode"] == "normal")


def dispatch(service, token: str, path: str, headers, raw: bytes) -> tuple[int, dict]:
    """Return safe errors without trusting upstream exception text or headers."""
    try:
        auth = headers.get("Authorization", "")
        require(
            hmac.compare_digest(auth.encode(), ("Bearer " + token).encode()), "unauthorized", 401
        )
        prefix = "/v1/skills/"
        require(path.startswith(prefix) and path[len(prefix) :] in COMMANDS, "not_found", 404)
        action = path[len(prefix) :]
        actor = headers.get("X-Skill-Actor", "")
        capability = headers.get("X-Skill-Capability", "")
        require(
            isinstance(actor, str) and 0 < len(actor.strip()) <= 300 and "\n" not in actor,
            "forbidden",
            403,
        )
        require(
            capability in _CAPABILITIES
            and _CAPABILITIES[capability] >= _CAPABILITIES[COMMANDS[action][1]],
            "forbidden",
            403,
        )
        body = decode(raw)
        _validate(action, body)
        require(headers.get("X-Skill-Project") == body["projectId"], "forbidden", 403)
        return 200, {"data": service.call(action, body, actor)}
    except ManagementError as error:
        code = error.code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error.code) else "invalid_input"
        details = {}
        sequence = error.details.get("currentSequence")
        if type(sequence) is int:
            details["currentSequence"] = sequence
        return error.http_status, {"error": {"code": code, "message": code, "details": details}}
    except Exception:
        _LOG.warning("skill request unavailable")
        return 503, {
            "error": {
                "code": "service_unavailable",
                "message": "service_unavailable",
                "details": {},
            }
        }


def server_for(service, token: str, address: tuple[str, int]) -> ThreadingHTTPServer:
    require(len(token) >= 32 and not any(c.isspace() for c in token))
    require(address[0] in {"127.0.0.1", "localhost"})

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib HTTP callback name
            self.connection.settimeout(15)
            try:
                require(not self.headers.get("Transfer-Encoding"))
                sizes = self.headers.get_all("Content-Length", [])
                require(len(sizes) == 1 and sizes[0].isdigit())
                size = int(sizes[0])
                require(0 < size <= 32768)
                for key in (
                    "Authorization",
                    "X-Skill-Actor",
                    "X-Skill-Capability",
                    "X-Skill-Project",
                ):
                    require(len(self.headers.get_all(key, [])) == 1, "unauthorized", 401)
                raw = self.rfile.read(size)
                require(len(raw) == size)
                status, response = dispatch(service, token, self.path, self.headers, raw)
            except (ValueError, OSError):
                status, response = (
                    400,
                    {"error": {"code": "invalid_input", "message": "invalid_input", "details": {}}},
                )
            data = json.dumps(response, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            # BaseHTTPRequestHandler normally repeats arbitrary client paths.
            _LOG.info("skill HTTP request finished")

    return ThreadingHTTPServer(address, Handler)
