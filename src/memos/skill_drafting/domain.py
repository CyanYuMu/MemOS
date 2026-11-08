"""Content-bound candidate contracts; references are not semantic approval.

The source collector owns live Memory/Evidence authenticity. This module checks
the immutable documents it supplies, and never opens a model-provided URI.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re


DOMAIN_VERSION = "1"
EXITS = ("success", "not_applicable", "needs_evidence", "unresolved")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_SKILL_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
_SCOPE_KEYS = {"service_identity", "cube_id", "user_id", "namespace"}
_PROPOSAL_KEYS = {
    "title",
    "goal",
    "purpose",
    "tags",
    "applicability",
    "exclusions",
    "prerequisites",
    "constraints",
    "rules",
    "acceptance",
    "limitations",
    "entry_rule",
    "exits",
}
_RULE_KEYS = {
    "rule_id",
    "condition",
    "observation",
    "action",
    "expected_result",
    "basis",
    "branches",
    "evidence",
}
_HOST_LIMITS = [
    "Structural and reference checks do not establish semantic entailment.",
    "No independent Skill task validation or causal benefit evaluation has run.",
    "This candidate is not authorized for ordinary Agent use or publication.",
]
_PRIVATE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "access_token",
    "auth_token",
    "reasoning",
    "reasoning_content",
    "thinking",
    "chain_of_thought",
    "private_key",
}


def json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def hash_json(value: object) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def _require(condition: object, code: str) -> None:
    if not condition:
        raise ValueError("draft_" + code)


def _keys(value: object, names: set[str], code: str) -> None:
    _require(isinstance(value, dict) and set(value) == names, code)


def _text(value: object, *, limit: int = 4000) -> None:
    _require(isinstance(value, str) and value.strip() and len(value) <= limit, "invalid_text")
    _require(
        "\x00" not in value
        and not any(marker in value for marker in ("!`", "$(", "${", "$ARGUMENTS"))
        and re.search(r"\$[0-9]+", value) is None,
        "dynamic_content",
    )


def _texts(value: object, *, maximum: int = 40) -> None:
    _require(isinstance(value, list) and 1 <= len(value) <= maximum, "invalid_text_list")
    for item in value:
        _text(item)
    _require(len(set(value)) == len(value), "duplicate_text")


def _digest(value: object) -> None:
    _require(isinstance(value, str) and _DIGEST.fullmatch(value), "invalid_digest")


def resolve_pointer(document: object, pointer: str) -> object:
    """Resolve a bounded JSON Pointer inside a supplied document, never a URI."""
    _require(
        isinstance(pointer, str) and pointer.startswith("/") and len(pointer) < 500,
        "invalid_locator",
    )
    current = document
    parts = pointer[1:].split("/")
    _require(len(parts) <= 12, "invalid_locator")
    for raw in parts:
        _require(re.search(r"~(?![01])", raw) is None, "invalid_locator")
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            _require(key in current, "unresolved_locator")
            current = current[key]
        elif isinstance(current, list):
            _require(re.fullmatch(r"0|[1-9][0-9]*", key), "invalid_locator")
            _require(int(key) < len(current), "unresolved_locator")
            current = current[int(key)]
        else:
            raise ValueError("draft_unresolved_locator")
    return current


def _category_matches(category: str, target: str, locator: str) -> bool:
    if category == "memory":
        return target == "MemorySnapshot" and locator == "/memory"
    if target != "EvidenceRevision":
        return False
    patterns = {
        "contract": r"/case/contract",
        "initial_check": r"/preflight/variants/initial/checks/(0|[1-9][0-9]*)",
        "final_check": r"/final/checks/(0|[1-9][0-9]*)",
        "change": r"/changes/(files|text_diffs)/(0|[1-9][0-9]*)",
        "environment": r"/environment",
        "unknown": r"/unknowns/[A-Za-z0-9_-]+",
    }
    return category in patterns and re.fullmatch(patterns[category], locator) is not None


def _validate_packet(packet: dict) -> None:
    _keys(
        packet,
        {
            "schema_version",
            "project_id",
            "scope",
            "plan",
            "plan_sha256",
            "sources",
            "facts",
            "watermark",
            "independent_lineages",
            "packet_id",
        },
        "packet_fields",
    )
    _require(
        type(packet["schema_version"]) is int and packet["schema_version"] == 1, "packet_version"
    )
    _require(len(json_bytes(packet)) <= 32 * 1024 * 1024, "packet_too_large")
    _text(packet["project_id"], limit=120)
    _keys(packet["scope"], _SCOPE_KEYS, "scope_fields")
    for value in packet["scope"].values():
        _text(value, limit=300)
    plan = packet["plan"]
    _keys(plan, {"schema_version", "project_id", "scope", "sources"}, "plan_fields")
    _require(
        type(plan["schema_version"]) is int
        and plan["schema_version"] == 1
        and plan["project_id"] == packet["project_id"]
        and plan["scope"] == packet["scope"],
        "plan_scope_mismatch",
    )
    _require(packet["plan_sha256"] == hash_json(plan), "plan_digest_mismatch")
    _require(
        packet["packet_id"] == hash_json({k: v for k, v in packet.items() if k != "packet_id"}),
        "packet_digest_mismatch",
    )
    planned = set()
    _require(
        isinstance(plan["sources"], list) and 1 <= len(plan["sources"]) <= 100,
        "invalid_source_plan",
    )
    for source in plan["sources"]:
        _keys(source, {"evidence_path", "evidence_revision", "memory_ids"}, "plan_source_fields")
        _digest(source["evidence_revision"])
        _text(source["evidence_path"])
        _require(
            isinstance(source["memory_ids"], list) and source["memory_ids"], "invalid_memory_ids"
        )
        for memory_id in source["memory_ids"]:
            _text(memory_id, limit=300)
            identity = (source["evidence_revision"], source["evidence_path"], memory_id)
            _require(identity not in planned, "duplicate_planned_source")
            planned.add(identity)
    _require(
        isinstance(packet["sources"], list) and 1 <= len(packet["sources"]) <= 100,
        "invalid_sources",
    )
    indexed, actual, lineages, success = {}, set(), set(), set()
    for source in packet["sources"]:
        _keys(source, {"source_id", "snapshot", "evidence"}, "source_fields")
        snapshot, evidence = source["snapshot"], source["evidence"]
        _require(isinstance(snapshot, dict) and isinstance(evidence, dict), "invalid_source")
        evidence_revision = evidence["evidence_revision"]
        _require(
            evidence_revision
            == hashlib.sha256(
                json_bytes({k: v for k, v in evidence.items() if k != "evidence_revision"}) + b"\n"
            ).hexdigest(),
            "evidence_digest_mismatch",
        )
        _require(evidence["collection"]["trusted_local_collection"] is True, "untrusted_source")
        qualification = evidence["qualification"]
        _require(
            qualification in {"qualified_success", "qualified_failure"}
            and snapshot["qualification"] == qualification,
            "ineligible_source",
        )
        case, context = evidence["case"], evidence["context"]
        _require(
            snapshot["split"] == case["split"] == context["split"] == "source", "non_source_input"
        )
        for key in ("case_id", "lineage_id"):
            _require(snapshot[key] == case[key] == context[key], "case_identity_mismatch")
        _require(context["case_revision"] == case["revision"], "case_revision_mismatch")
        _require(
            all(snapshot.get(key) == value for key, value in packet["scope"].items()),
            "snapshot_scope_mismatch",
        )
        _require(snapshot["evidence_revision"] == evidence_revision, "evidence_identity_mismatch")
        _require(
            snapshot["snapshot_id"]
            == hash_json(
                {k: v for k, v in snapshot.items() if k not in {"snapshot_id", "snapshot_path"}}
            ),
            "snapshot_digest_mismatch",
        )
        memory = snapshot["memory"]
        _require(isinstance(memory, str) and memory.strip(), "empty_memory")
        _require(
            snapshot["body_sha256"] == hashlib.sha256(memory.encode()).hexdigest(),
            "memory_digest_mismatch",
        )
        record = {"id": snapshot["memory_id"], "memory": memory, "metadata": snapshot["metadata"]}
        _require(snapshot["content_sha256"] == hash_json(record), "record_digest_mismatch")
        _require(
            snapshot["sources"] == snapshot["metadata"]["sources"] and snapshot["sources"],
            "memory_sources_mismatch",
        )
        _require(
            snapshot["metadata"].get("user_id") == packet["scope"]["user_id"],
            "memory_owner_mismatch",
        )
        source_id = hash_json(
            [
                snapshot["service_identity"],
                snapshot["cube_id"],
                snapshot["memory_id"],
                snapshot["content_sha256"],
                evidence_revision,
            ]
        )
        _require(
            source["source_id"] == source_id and source_id not in indexed,
            "duplicate_or_invalid_source",
        )
        identity = (evidence_revision, snapshot["evidence_path"], snapshot["memory_id"])
        _require(identity not in actual, "duplicate_memory")
        actual.add(identity)
        indexed[source_id] = source
        lineages.add(case["lineage_id"])
        if qualification == "qualified_success":
            _require(evidence["final"]["status"] == "passed", "success_without_acceptance")
            success.add(case["lineage_id"])
    _require(actual == planned, "source_plan_incomplete")
    _require(len(lineages) >= 2 and success, "insufficient_independent_sources")
    _require(packet["independent_lineages"] == sorted(lineages), "lineage_count_mismatch")
    _require(packet["watermark"] == sorted(indexed), "watermark_mismatch")
    _require(
        isinstance(packet["facts"], list) and 1 <= len(packet["facts"]) <= 2000, "invalid_facts"
    )
    fact_ids, categories = set(), {key: set() for key in indexed}
    for fact in packet["facts"]:
        _keys(
            fact,
            {
                "fact_id",
                "source_id",
                "target_kind",
                "target_id",
                "category",
                "locator",
                "value",
                "value_sha256",
                "qualification",
                "failure_attribution",
            },
            "fact_fields",
        )
        _require(
            fact["fact_id"] == hash_json({k: v for k, v in fact.items() if k != "fact_id"})
            and fact["fact_id"] not in fact_ids,
            "fact_identity_mismatch",
        )
        fact_ids.add(fact["fact_id"])
        _require(fact["source_id"] in indexed, "unknown_fact_source")
        source = indexed[fact["source_id"]]
        _require(
            _category_matches(fact["category"], fact["target_kind"], fact["locator"]),
            "fact_category_mismatch",
        )
        document = (
            source["snapshot"] if fact["target_kind"] == "MemorySnapshot" else source["evidence"]
        )
        identity = (
            document["snapshot_id"]
            if fact["target_kind"] == "MemorySnapshot"
            else document["evidence_revision"]
        )
        _require(identity == fact["target_id"], "fact_target_mismatch")
        _require(
            hash_json(resolve_pointer(document, fact["locator"]))
            == hash_json(fact["value"])
            == fact["value_sha256"],
            "fact_value_mismatch",
        )
        _require(
            fact["qualification"] == source["evidence"]["qualification"]
            and fact["failure_attribution"]
            == source["evidence"]["unknowns"]["failure_attribution"],
            "fact_qualification_mismatch",
        )
        categories[fact["source_id"]].add(fact["category"])
    _require(
        all(
            {"memory", "contract", "final_check", "unknown"} <= value
            for value in categories.values()
        ),
        "missing_source_context",
    )


def validate_packet(packet: dict) -> None:
    """Check a collector packet without treating its hashes as third-party proof."""
    try:
        _validate_packet(packet)
    except (KeyError, TypeError, IndexError, AttributeError, RecursionError, OverflowError):
        raise ValueError("draft_invalid_packet") from None


def proposal_schema() -> dict:
    """A portable JSON schema; semantic and cross-reference checks are separate."""
    text = {"type": "string", "minLength": 1, "maxLength": 4000}
    texts = {"type": "array", "minItems": 1, "maxItems": 40, "uniqueItems": True, "items": text}
    reference = {
        "type": "object",
        "additionalProperties": False,
        "required": ["fact_id", "kind", "claim"],
        "properties": {
            "fact_id": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "kind": {"enum": ["supports", "contradicts", "precondition", "background"]},
            "claim": text,
        },
    }
    branch = {
        "type": "object",
        "additionalProperties": False,
        "required": ["when", "target"],
        "properties": {
            "when": text,
            "target": {
                "type": "string",
                "description": "An existing rule_id, or exactly exit:success, exit:not_applicable, exit:needs_evidence, or exit:unresolved. Bare exit names are invalid.",
                "pattern": "^(?:[A-Za-z0-9][A-Za-z0-9_-]{0,79}|exit:(?:success|not_applicable|needs_evidence|unresolved))$",
            },
        },
    }
    rule = {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(_RULE_KEYS),
        "properties": {
            **dict.fromkeys(("condition", "observation", "action", "expected_result"), text),
            "rule_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$"},
            "basis": {"enum": ["empirical", "normative", "hypothesis"]},
            "branches": {"type": "array", "minItems": 1, "maxItems": 12, "items": branch},
            "evidence": {"type": "array", "minItems": 2, "maxItems": 100, "items": reference},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(_PROPOSAL_KEYS),
        "properties": {
            "title": text,
            "goal": text,
            "purpose": {"enum": ["diagnose", "repair", "verify"]},
            **dict.fromkeys(
                (
                    "tags",
                    "applicability",
                    "exclusions",
                    "prerequisites",
                    "constraints",
                    "acceptance",
                    "limitations",
                ),
                texts,
            ),
            "entry_rule": text,
            "exits": {
                "type": "object",
                "additionalProperties": False,
                "required": list(EXITS),
                "properties": dict.fromkeys(EXITS, text),
            },
            "rules": {"type": "array", "minItems": 1, "maxItems": 20, "items": rule},
        },
    }


def _validate_proposal(proposal: dict, packet: dict) -> None:
    _keys(proposal, _PROPOSAL_KEYS, "proposal_fields")
    for key in ("title", "goal", "entry_rule"):
        _text(proposal[key])
    _require(proposal["purpose"] in {"diagnose", "repair", "verify"}, "invalid_purpose")
    for key in (
        "tags",
        "applicability",
        "exclusions",
        "prerequisites",
        "constraints",
        "acceptance",
        "limitations",
    ):
        _texts(proposal[key])
    _keys(proposal["exits"], set(EXITS), "invalid_exits")
    for text in proposal["exits"].values():
        _text(text)
    rules = proposal["rules"]
    _require(isinstance(rules, list) and 1 <= len(rules) <= 20, "invalid_rules")
    facts = {fact["fact_id"]: fact for fact in packet["facts"]}
    ids, used_sources, adjacency = set(), set(), {}
    for rule in rules:
        _keys(rule, _RULE_KEYS, "rule_fields")
        identity = rule["rule_id"]
        _require(
            isinstance(identity, str) and _IDENTITY.fullmatch(identity) and identity not in ids,
            "invalid_rule_identity",
        )
        ids.add(identity)
        for key in ("condition", "observation", "action", "expected_result"):
            _text(rule[key])
        _require(rule["basis"] in {"empirical", "normative", "hypothesis"}, "invalid_rule_basis")
        _require(
            isinstance(rule["branches"], list) and 1 <= len(rule["branches"]) <= 12,
            "invalid_branches",
        )
        adjacency[identity] = []
        branch_ids = set()
        for branch in rule["branches"]:
            _keys(branch, {"when", "target"}, "branch_fields")
            _text(branch["when"])
            _text(branch["target"], limit=100)
            _require(hash_json(branch) not in branch_ids, "duplicate_branch")
            branch_ids.add(hash_json(branch))
            adjacency[identity].append(branch["target"])
        _require(
            isinstance(rule["evidence"], list) and 2 <= len(rule["evidence"]) <= 100,
            "invalid_rule_evidence",
        )
        kinds, norm, empirical, links = set(), False, False, set()
        for reference in rule["evidence"]:
            _keys(reference, {"fact_id", "kind", "claim"}, "reference_fields")
            _require(reference["fact_id"] in facts, "unknown_fact_reference")
            _require(
                reference["kind"] in {"supports", "contradicts", "precondition", "background"},
                "invalid_evidence_kind",
            )
            _text(reference["claim"])
            _require(hash_json(reference) not in links, "duplicate_evidence_link")
            links.add(hash_json(reference))
            fact = facts[reference["fact_id"]]
            kinds.add(fact["target_kind"])
            used_sources.add(fact["source_id"])
            # A known failed check is a fact; it is not an attributed cause.
            if (
                fact["qualification"] == "qualified_failure"
                and fact["failure_attribution"] == "unknown"
                and fact["category"] != "contract"
            ):
                _require(reference["kind"] == "background", "unattributed_failure_support")
            if fact["category"] == "contract" and reference["kind"] in {"supports", "precondition"}:
                norm = True
            if (
                fact["category"] in {"initial_check", "final_check", "change"}
                and fact["qualification"] == "qualified_success"
                and reference["kind"] == "supports"
            ):
                empirical = True
        _require(kinds == {"MemorySnapshot", "EvidenceRevision"}, "missing_memory_or_evidence_link")
        _require(rule["basis"] != "normative" or norm, "normative_rule_without_contract")
        _require(rule["basis"] != "empirical" or empirical, "empirical_rule_without_success")
    _require(used_sources == set(packet["watermark"]), "omitted_source")
    _require(proposal["entry_rule"] in ids, "invalid_entry_rule")
    exits, visited, active = set(), set(), set()

    def walk(identity):
        if identity.startswith("exit:"):
            _require(identity[5:] in EXITS, "invalid_exit_target")
            exits.add(identity[5:])
            return
        _require(identity in ids, "dangling_branch")
        _require(identity not in active, "rule_cycle")
        if identity in visited:
            return
        active.add(identity)
        for target in adjacency[identity]:
            walk(target)
        active.remove(identity)
        visited.add(identity)

    walk(proposal["entry_rule"])
    _require(visited == ids and exits == set(EXITS), "unreachable_rule_or_exit")


def _validate_generation(generation: dict, packet: dict, proposal: dict) -> None:
    # This lazy import shares the generator's public projection without importing
    # the optional provider SDK or making a request. All providers must satisfy
    # the same content checks; test fixtures receive no provenance exemption.
    from memos.skill_drafting.generation import assert_public_json, build_messages

    _keys(
        generation,
        {
            "provider",
            "sdk_version",
            "service_identity",
            "requested_model",
            "reported_model",
            "model_identity_match",
            "parameters",
            "prompt_sha256",
            "schema_sha256",
            "response_sha256",
            "messages",
            "response_content",
            "finish_reason",
            "usage",
            "usage_status",
            "elapsed_seconds",
        },
        "generation_fields",
    )
    _require(generation["provider"] == "openai_compatible_json", "unsupported_generation_provider")
    for key in ("provider", "requested_model", "reported_model", "sdk_version"):
        _text(generation.get(key), limit=300)
    for key in ("prompt_sha256", "schema_sha256", "response_sha256", "service_identity"):
        _digest(generation.get(key))
    _require(
        generation["model_identity_match"]
        is (generation["requested_model"] == generation["reported_model"]),
        "model_identity_mismatch",
    )
    _require(generation["finish_reason"] == "stop", "incomplete_generation")
    elapsed = generation["elapsed_seconds"]
    _require(
        type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0,
        "invalid_generation_elapsed",
    )
    parameters = generation["parameters"]
    _keys(
        parameters,
        {"timeout_seconds", "max_tokens", "temperature", "max_retries", "tools"},
        "generation_parameter_fields",
    )
    timeout = parameters["timeout_seconds"]
    _require(
        type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 300,
        "invalid_generation_timeout",
    )
    _require(
        type(parameters["max_tokens"]) is int and 1 <= parameters["max_tokens"] <= 16000,
        "invalid_generation_token_limit",
    )
    _require(
        type(parameters["temperature"]) in (int, float)
        and parameters["temperature"] == 0
        and type(parameters["max_retries"]) is int
        and parameters["max_retries"] == 0
        and parameters["tools"] == [],
        "invalid_generation_capabilities",
    )
    usage = generation["usage"]
    _require(
        isinstance(usage, dict)
        and set(usage) <= {"prompt_tokens", "completion_tokens", "total_tokens"}
        and all(type(value) is int and value >= 0 for value in usage.values()),
        "invalid_generation_usage",
    )
    _require(
        (generation["usage_status"] == "reported" and bool(usage))
        or (generation["usage_status"] in {"missing", "invalid"} and not usage),
        "invalid_generation_usage_status",
    )

    def check(value):
        if isinstance(value, dict):
            _require(
                all(isinstance(key, str) and key.lower() not in _PRIVATE_KEYS for key in value),
                "private_generation_metadata",
            )
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)

    check(generation)
    _require(len(json_bytes(generation)) <= 8 * 1024 * 1024, "generation_too_large")
    _require(
        generation["schema_sha256"] == hash_json(proposal_schema()), "generation_schema_mismatch"
    )
    _require(
        hash_json(generation["messages"])
        == generation["prompt_sha256"]
        == hash_json(build_messages(packet)),
        "generation_prompt_mismatch",
    )
    response = generation["response_content"]
    _require(
        isinstance(response, str) and response.strip() and len(response.encode()) <= 131_072,
        "invalid_generation_response",
    )
    _require(
        hashlib.sha256(response.encode()).hexdigest() == generation["response_sha256"],
        "generation_response_digest_mismatch",
    )

    def unique(pairs):
        value = {}
        for key, item in pairs:
            _require(key not in value, "generation_duplicate_json_key")
            value[key] = item
        return value

    def invalid_constant(_):
        raise ValueError("draft_generation_nonfinite_json")

    try:
        parsed = json.loads(response, object_pairs_hook=unique, parse_constant=invalid_constant)
    except ValueError:
        raise ValueError("draft_invalid_generation_response_json") from None
    _require(
        isinstance(parsed, dict) and hash_json(parsed) == hash_json(proposal),
        "generation_proposal_mismatch",
    )
    assert_public_json(generation)


def _build(proposal: dict, packet: dict, skill_id: str, generation_sha256: str) -> dict:
    _require(isinstance(skill_id, str) and _SKILL_ID.fullmatch(skill_id), "invalid_skill_id")
    _digest(generation_sha256)
    _validate_proposal(proposal, packet)
    candidate = {
        **copy.deepcopy(proposal),
        "schema_version": 1,
        "domain_version": DOMAIN_VERSION,
        "project_id": packet["project_id"],
        "skill_id": skill_id,
        "type": "procedure",
        "packet_id": packet["packet_id"],
        "generation_sha256": generation_sha256,
        "status": "candidate",
        "semantic_review": "pending",
        "independent_validation": "not_run",
        "release_eligibility": "not_evaluated",
        "requires": [],
        "host_limits": list(_HOST_LIMITS),
        "source_refs": [
            {
                "source_id": source["source_id"],
                "snapshot_id": source["snapshot"]["snapshot_id"],
                "evidence_revision": source["evidence"]["evidence_revision"],
                "case_id": source["evidence"]["case"]["case_id"],
                "lineage_id": source["evidence"]["case"]["lineage_id"],
                "qualification": source["evidence"]["qualification"],
            }
            for source in sorted(packet["sources"], key=lambda source: source["source_id"])
        ],
    }
    # Revision fixes the normative content and its complete inputs. Links are a
    # deterministic projection of those inputs; their IDs also bind the revision.
    revision = hash_json(candidate)
    candidate["revision"] = revision
    facts = {fact["fact_id"]: fact for fact in packet["facts"]}
    for rule in candidate["rules"]:
        rule["validation_status"] = "needs_evidence" if rule["basis"] == "hypothesis" else "not_run"
        links = []
        for reference in rule["evidence"]:
            fact = facts[reference["fact_id"]]
            link = {
                **reference,
                **{
                    key: fact[key]
                    for key in ("source_id", "target_kind", "target_id", "locator", "value_sha256")
                },
                "producer": "memos-skill-drafting-v1",
                "semantic_review": "pending",
                "skill_id": skill_id,
                "revision": revision,
                "rule_id": rule["rule_id"],
            }
            link["link_id"] = hash_json(link)
            links.append(link)
        rule["evidence_links"] = links
    candidate["content_hash"] = hash_json(candidate)
    return candidate


def make_candidate(proposal: dict, packet: dict, *, skill_id: str, generation: dict) -> dict:
    try:
        validate_packet(packet)
        _validate_generation(generation, packet, proposal)
        return _build(proposal, packet, skill_id, hash_json(generation))
    except (KeyError, TypeError, IndexError, AttributeError, RecursionError, OverflowError):
        raise ValueError("draft_invalid_candidate") from None


def verify_candidate_document(candidate: dict, packet: dict) -> None:
    try:
        validate_packet(packet)
        proposal = {key: copy.deepcopy(candidate[key]) for key in _PROPOSAL_KEYS}
        proposal["rules"] = [{key: rule[key] for key in _RULE_KEYS} for rule in candidate["rules"]]
        expected = _build(proposal, packet, candidate["skill_id"], candidate["generation_sha256"])
        _require(candidate == expected, "candidate_content_changed")
    except (KeyError, TypeError, IndexError, AttributeError, RecursionError, OverflowError):
        raise ValueError("draft_invalid_candidate") from None
