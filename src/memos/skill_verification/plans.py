"""Versioned, host-owned plans; locked task bytes are never modified in place."""

from __future__ import annotations

import hashlib
import json

from memos.skill_evaluation.holdout_tasks import _FIXTURE_RUNNER
from memos.skill_evaluation.tasks import case_digest, get_case


PLAN_VERSION = "p41-plan-2"
FIXTURE_ORACLE_SHA256 = "158c096e47337cd6c197536cf7daf57db02bd41ca4036aff2e8bbf2c3bb46ef5"
FIXTURE_RUNNER_SHA256 = "942edc6afaddfd05bbb57b0e9747d9b89c823d86fe1375eb947b097386297c13"
_EXTERNAL_TESTS = (
    "import caller\n"
    "def test_export(graph):\n"
    "    assert caller.collect(graph) == ['a', 'b']\n"
    "def test_preview(graph):\n"
    "    assert caller.collect(graph) == ['a', 'b']\n"
)


def digest_json(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _phase(identity, kind, program, *, files=None, arguments=()):
    return {
        "id": identity,
        "kind": kind,
        "program": program,
        "files": files or {},
        "arguments": list(arguments),
    }


def _fixture_runner() -> str:
    if _sha(_FIXTURE_RUNNER) != FIXTURE_RUNNER_SHA256:
        raise ValueError("fixture_harness_version_changed")
    needle = '"--noconftest", *sys.argv[2:]'
    if _FIXTURE_RUNNER.count(needle) != 1:
        raise ValueError("fixture_harness_transform_invalid")
    # Every pytest invocation is its own no-fork sandbox, created by the host.
    # Both options are necessary: rootdir alone does not stop ancestor lookup.
    program = _FIXTURE_RUNNER.replace(
        needle,
        '"--noconftest", "--rootdir=" + os.getcwd(), "--confcutdir=" + os.getcwd(), *sys.argv[2:]',
    )
    # Retain actual setup/call/teardown evidence for independent host rechecking.
    # The extra argument is host-owned scratch, never an Agent-supplied path.
    program = program.replace("sys.argv[2:]", "sys.argv[3:]")
    program = program.replace("sys.exit(", "finish(")
    return program.replace(
        "import sys\n",
        "import sys\nimport json\n"
        "def finish(exit_code):\n"
        "    with open(sys.argv[2], 'x') as stream:\n"
        "        json.dump({'schema_version': 1, 'runner_exit_code': int(exit_code),\n"
        "                   'events': globals().get('events', [])}, stream)\n"
        "    raise SystemExit(exit_code)\n",
        1,
    )


def _plan(case, purpose, source, phases):
    value = {
        "schema_version": 1,
        "plan_version": PLAN_VERSION,
        "purpose": purpose,
        "case_id": case.case_id,
        "asset_digest": case_digest(case),
        "source_sha256": _sha(source),
        "phases": phases,
    }
    value["plan_digest"] = digest_json(value)
    return value


def acceptance_plan(case_id: str) -> dict:
    case = get_case(case_id)
    source = case.oracle_source
    if case_id != "holdout-fixture":
        return _plan(case, "acceptance", source, [_phase("oracle", "oracle", source)])
    if _sha(source) != FIXTURE_ORACLE_SHA256:
        raise ValueError("fixture_oracle_version_changed")
    needle = '    check("fixture_isolation", fixture_isolation)\n'
    if source.count(needle) != 1:
        raise ValueError("fixture_oracle_transform_invalid")
    phases = [_phase("oracle_contracts", "oracle", source.replace(needle, ""))]
    first, second = (
        "{control}/test_external.py::test_export",
        "{control}/test_external.py::test_preview",
    )
    for index, order in enumerate(((first,), (second,), (first, second), (second, first)), 1):
        phases.append(
            _phase(
                f"fixture_order_{index}",
                "fixture_pytest",
                _fixture_runner(),
                files={"test_external.py": _EXTERNAL_TESTS},
                arguments=("{candidate}", "{report}", *order),
            )
        )
    return _plan(case, "acceptance", source, phases)


def visible_plan(case_id: str) -> dict:
    case = get_case(case_id)
    source = case.initial_files["visible_checks.py"]
    if case_id == "holdout-fixture":
        # Only the task's existing public tests are copied. The hidden oracle
        # and its test suite are absent from the visible-check plan and output.
        phase = _phase(
            "visible",
            "fixture_pytest",
            _fixture_runner(),
            files={"test_visible.py": case.initial_files["test_visible.py"]},
            arguments=(
                "{candidate}",
                "{report}",
                "{control}/test_visible.py::test_export",
                "{control}/test_visible.py::test_preview",
            ),
        )
    else:
        phase = _phase(
            "visible",
            "visible",
            "import runpy, sys\nfrom pathlib import Path\n"
            "script = Path(sys.argv[1]) / 'visible_checks.py'\n"
            "sys.argv = [str(script)]\nrunpy.run_path(str(script), run_name='__main__')\n",
        )
    return _plan(case, "visible", source, [phase])
