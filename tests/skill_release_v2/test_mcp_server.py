"""The isolated MCP process exposes only the frozen two-tool protocol."""

import json
import shutil
import subprocess
import sys

from pathlib import Path

from test_transport import host as host_fixture
from test_transport import pre


host = host_fixture
SOURCE = Path(__file__).parents[2] / "src/memos/skill_release_v2/mcp_server.py"


def invoke(tmp_path, host, messages):
    script = tmp_path / "mcp.py"
    shutil.copyfile(SOURCE, script)
    config_path = tmp_path / "mcp.json"
    config_path.write_text(json.dumps(host.credentials("mcp")))
    config_path.chmod(0o600)
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(script), "--config", str(config_path)],
        input="".join(json.dumps(value) + "\n" for value in messages),
        text=True,
        capture_output=True,
        timeout=6,
    )
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines()]


def rpc(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def test_mcp_exposes_exact_names_and_schemas(tmp_path, host):
    responses = invoke(tmp_path, host, [rpc("initialize"), rpc("tools/list", request_id=2)])
    assert responses[0]["result"]["serverInfo"]["name"] == "memos_v2"
    tools = responses[1]["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["record_rule", "run_visible_checks"]
    assert set(tools[0]["inputSchema"]["properties"]) == {
        "rule_id",
        "phase",
        "branch_index",
        "observation",
        "decision",
        "evidence_refs",
    }
    assert tools[0]["inputSchema"]["additionalProperties"] is False
    assert tools[1]["inputSchema"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def test_mcp_delivers_exact_host_result_in_one_text_block(tmp_path, host):
    host.start()
    pre(host, "check", "mcp__memos_v2__run_visible_checks", {})
    responses = invoke(
        tmp_path, host, [rpc("tools/call", {"name": "run_visible_checks", "arguments": {}})]
    )
    result = responses[0]["result"]
    assert result["isError"] is False
    assert len(result["content"]) == 1
    assert (
        json.loads(result["content"][0]["text"]) == host.delivery_material("check")["host_result"]
    )


def test_mcp_rejects_agent_identity_arguments_and_unknown_tools(tmp_path, host):
    host.start()
    responses = invoke(
        tmp_path,
        host,
        [
            rpc(
                "tools/call", {"name": "run_visible_checks", "arguments": {"tool_use_id": "forged"}}
            ),
            rpc("tools/call", {"name": "complete_native", "arguments": {}}, 2),
        ],
    )
    assert all(row["result"]["isError"] is True for row in responses)
    assert not host.gate.audit()


def test_mcp_transport_loss_returns_real_error_without_credential_disclosure(tmp_path, host):
    responses = invoke(
        tmp_path, host, [rpc("tools/call", {"name": "run_visible_checks", "arguments": {}})]
    )
    assert responses[0]["result"]["isError"] is True
    assert host.credentials("mcp")["token"] not in json.dumps(responses)


def test_fixed_cli_metadata_is_validated_then_excluded_from_host_request(tmp_path, host):
    host.start()
    pre(host, "check", "mcp__memos_v2__run_visible_checks", {})
    responses = invoke(
        tmp_path,
        host,
        [
            rpc(
                "tools/call",
                {
                    "name": "run_visible_checks",
                    "arguments": {},
                    "_meta": {
                        "progressToken": 3,
                        "claudecode/toolUseId": "different-untrusted-hint",
                    },
                },
            )
        ],
    )
    assert responses[0]["result"]["isError"] is False
    material = host.delivery_material("check")
    assert material["request"].tool_use_id == "check"
    assert material["request"].input == {}
    assert "different-untrusted-hint" not in host.journal_path.read_text()


def test_declaration_metadata_is_not_a_seventh_business_argument(tmp_path, host):
    host.start()
    value = {
        "rule_id": "entry",
        "phase": "select",
        "branch_index": 0,
        "observation": "Observed.",
        "decision": "Repair.",
        "evidence_refs": ["unknown"],
    }
    pre(host, "rule", "mcp__memos_v2__record_rule", value)
    responses = invoke(
        tmp_path,
        host,
        [
            rpc(
                "tools/call",
                {
                    "name": "record_rule",
                    "arguments": value,
                    "_meta": {"progressToken": "progress", "claudecode/toolUseId": "rule"},
                },
            )
        ],
    )
    result = json.loads(responses[0]["result"]["content"][0]["text"])
    assert result == host.delivery_material("rule")["host_result"]
    assert result["error"] == "unknown_evidence"


def test_mcp_progress_metadata_accepts_protocol_scalar_boundaries():
    from memos.skill_release_v2.mcp_server import validate_call

    for token in (0, -1, 0.0, 1.5, "", "x" * 300, 10**400):
        validate_call(
            {"name": "run_visible_checks", "arguments": {}, "_meta": {"progressToken": token}}
        )


def test_mcp_unknown_or_malformed_metadata_is_rejected():
    import pytest

    from memos.skill_release_v2.mcp_server import validate_call

    bad_metadata = [
        None,
        [],
        {"progressToken": None},
        {"progressToken": True},
        {"progressToken": []},
        {"progressToken": {}},
        {"progressToken": "x" * 301},
        {"progressToken": float("nan")},
        {"progressToken": float("inf")},
        {"claudecode/toolUseId": ""},
        {"claudecode/toolUseId": "x" * 301},
        {"claudecode/toolUseId": 3},
        {"tool_use_id": "override"},
        {"progressToken": 1, "other": "not-allowed"},
    ]
    for meta in bad_metadata:
        with pytest.raises(ValueError):
            validate_call({"name": "run_visible_checks", "arguments": {}, "_meta": meta})


def test_metadata_fields_inside_business_arguments_are_still_rejected(tmp_path, host):
    host.start()
    responses = invoke(
        tmp_path,
        host,
        [
            rpc(
                "tools/call",
                {
                    "name": "run_visible_checks",
                    "arguments": {"progressToken": 1},
                    "_meta": {"progressToken": 1},
                },
            )
        ],
    )
    assert responses[0]["result"]["isError"] is True
    assert not host.gate.audit()
