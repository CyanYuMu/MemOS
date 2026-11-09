"""A public-only repair must fail hidden inputs and cannot alter sealed C1."""

import sys

import pytest

from memos.skill_branch_c2.broker import VisibleCheckBroker, decode_public_result
from memos.skill_branch_c2.tasks import get_suite, materialize
from memos.skill_branch_c2.verifier import check_receipt, verify_suite
from memos.skill_branch_extensions.broker import decode_public_result as c1_decode
from memos.skill_branch_extensions.verifier import check_receipt as c1_check
from memos.skill_experiments.recording_server import _request_visible_checks


SUITE = "scoped-healthy-dream-c2-v1"
MACOS = pytest.mark.skipif(sys.platform != "darwin", reason="macOS fixed no-fork executor")


@MACOS
def test_public_specific_scope_fix_cannot_pass_hidden_generalization(tmp_path):
    candidate = materialize(SUITE, tmp_path / "candidate")
    source = (
        get_suite(SUITE)
        .variants["reference"]["fake_graph.py"]
        .replace(
            'if item is None or item["tenant"] != user_name:',
            'if item is None or (memory_id in {"p1", "p2"} and item["tenant"] != user_name):',
        )
    )
    (candidate / "fake_graph.py").write_text(source)
    with VisibleCheckBroker(SUITE, candidate, tmp_path / "public") as broker:
        response = _request_visible_checks({"test_broker_socket": str(broker.socket_path)})
    assert decode_public_result(SUITE, response)["status"] == "passed"
    result = verify_suite(SUITE, candidate, tmp_path / "hidden")
    assert result.status == "failed"
    assert check_receipt(result.receipt_path, candidate, tmp_path / "hidden").matches
    assert not c1_check(result.receipt_path, candidate, tmp_path / "hidden").matches
    with pytest.raises(ValueError):
        c1_decode("combined-c1-v1", response)


def test_fake_mutants_do_not_remove_correct_caller_and_initial_dream_is_reference():
    suite = get_suite(SUITE)
    reference = suite.variants["reference"]
    for name in ("initial", "unsafe_empty_read", "unsafe_update_arguments"):
        assert suite.variants[name]["caller.py"] == reference["caller.py"]
        assert {key for key in reference if suite.variants[name][key] != reference[key]} == {
            "fake_graph.py"
        }
