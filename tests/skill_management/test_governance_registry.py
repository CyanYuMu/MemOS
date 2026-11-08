"""Private registry admission never executes a model or accepts a public upload."""

import json

import pytest

from memos.skill_governance.registry import read_registry


def value(tmp_path):
    return {
        "schemaVersion": 1,
        "serviceConfig": str(tmp_path / "service.json"),
        "allowedRoot": str(tmp_path),
        "outputDirectory": str(tmp_path / "import"),
        "entries": [
            {
                "projectId": "one",
                "skillId": "fixed-skill",
                "revision": "a" * 64,
                "validationId": "b" * 64,
                "runDirectory": str(tmp_path / "run"),
                "reportSha256": "c" * 64,
                "reassessment": None,
            }
        ],
    }


def test_registry_requires_private_file_and_does_not_allow_self_reported_judgment(tmp_path):
    path = tmp_path / "registry.json"
    data = value(tmp_path)
    path.write_text(json.dumps(data))
    path.chmod(0o644)
    with pytest.raises(ValueError):
        read_registry(path)
    path.chmod(0o600)
    assert read_registry(path) == data
    data["entries"][0]["judgment"] = {"passed": True}
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        read_registry(path)


def test_duplicate_target_and_escaped_paths_are_rejected_before_registration(tmp_path):
    path = tmp_path / "registry.json"
    path.touch(mode=0o600)
    data = value(tmp_path)
    data["entries"].append(data["entries"][0].copy())
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        read_registry(path)
    data["entries"].pop()
    data["entries"][0]["runDirectory"] = str(tmp_path.parent / "escaped")
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        read_registry(path)


def test_import_verifies_all_evidence_before_any_write_and_is_repeatable(tmp_path, monkeypatch):
    from test_assessment_collector import retained_run, sha

    import memos.skill_governance.registry as module

    c, v, root, _, _ = retained_run(tmp_path)
    writes = []

    class Repository:
        def __init__(self, **_):
            pass

        def get_revision(self, *_):
            return {"candidate": c, "validations": [v]}

        def record_assessment(self, *_, **kwargs):
            writes.append(kwargs)

        def close(self):
            pass

    monkeypatch.setattr(module, "ManagementRepository", Repository)
    monkeypatch.setattr(
        module,
        "read_config",
        lambda _: {"graph": {}, "projects": [{"projectId": "one", "graphProject": "graph"}]},
    )
    data = value(tmp_path)
    entry = data["entries"][0]
    entry.update(
        skillId=c["skill_id"],
        revision=c["revision"],
        validationId=v["validationId"],
        runDirectory=str(root),
        reportSha256=sha(root / "report.json"),
    )
    path = tmp_path / "registry.json"
    path.touch(mode=0o600)
    # The second entry is invalid: the first must not be registered prematurely.
    data["entries"].append({**entry, "validationId": "9" * 64})
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="validation_missing"):
        module.import_evidence(path)
    assert writes == []
    data["entries"].pop()
    path.write_text(json.dumps(data))
    first = module.import_evidence(path)
    second = module.import_evidence(path)
    assert first == second
    assert len(writes) == 2 and writes[0] == writes[1]
    assert first["humanJudgmentsCreated"] == first["modelCalls"] == first["releasesCreated"] == 0
    assert writes[0]["actor"] == "host_evidence_verifier"
    assert writes[0]["assessment"]["branchCandidates"] == []
