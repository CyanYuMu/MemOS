"""Read-only audit rejects changed final evidence and ambiguous input files."""

import json

import pytest

from memos.skill_contracts.audit import audit_batch, read_json, verified_receipt
from memos.skill_transfer.tasks import TASKS, materialize_case
from memos.skill_transfer.verification import verify_candidate


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}'])
def test_ambiguous_json_is_rejected(tmp_path, raw):
    path = tmp_path / "input.json"
    path.write_text(raw)
    with pytest.raises(ValueError):
        read_json(path)


def test_symlink_input_is_rejected(tmp_path):
    path = tmp_path / "real.json"
    path.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        read_json(link)


def test_output_alias_cannot_write_inside_original_batch(tmp_path):
    batch = tmp_path / "batch"
    batch.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(batch, target_is_directory=True)
    with pytest.raises(ValueError, match="contract_output_inside_history"):
        audit_batch(batch, alias / "new-audit")
    assert list(batch.iterdir()) == []


def test_final_patch_or_receipt_changes_cannot_support_audit(tmp_path):
    candidate = materialize_case(TASKS[0], tmp_path / "candidate", "reference")
    verify_candidate(TASKS[0], candidate, tmp_path / "validation", run_id="run", attempt_id="one")
    assert verified_receipt(tmp_path)["status"] == "passed"
    caller = candidate / "caller.py"
    old = caller.read_text()
    caller.write_text(old + "\n# Changed after acceptance\n")
    with pytest.raises(ValueError, match="contract_receipt_unverified"):
        verified_receipt(tmp_path)
    caller.write_text(old)
    path = tmp_path / "validation/receipt.json"
    receipt = json.loads(path.read_text())
    receipt["status"] = "failed"
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="contract_receipt_unverified"):
        verified_receipt(tmp_path)


@pytest.mark.parametrize("rows", [[{"name": "x"}], [{"passed": True}], [1], ["x"], {"name": "x"}])
def test_rows_rejects_shape_errors_as_artifact_mismatch(rows):
    """A corrupt check row is bad evidence, not an AttributeError/KeyError escape."""
    from memos.skill_contracts.audit import _rows

    with pytest.raises(ValueError, match="contract_artifact_mismatch"):
        _rows(rows)


def test_read_json_does_not_depend_on_the_host_locale(tmp_path, monkeypatch):
    """Retained UTF-8 artifacts must decode the same under a C/ASCII locale."""
    from memos.skill_contracts.audit import read_json

    path = tmp_path / "artifact.json"
    path.write_text(json.dumps({"note": "中文"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr("locale.getpreferredencoding", lambda do_setlocale=True: "ascii")
    assert read_json(path) == {"note": "中文"}
