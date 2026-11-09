"""All trial facts must be verified before graph mutations begin."""

import pytest

from memos.skill_management.domain import ManagementError
from memos.skill_release import importer


def test_unverified_c2_trial_prevents_any_graph_write(tmp_path, monkeypatch):
    class Repository:
        def __init__(self, **kwargs):
            self.writes = []

        def get_revision(self, *args):
            return {"candidate": {}, "management": {"exportHash": "a" * 64}}

        def record_validation(self, *args, **kwargs):
            pytest.fail("partial write")

        def record_assessment(self, *args, **kwargs):
            pytest.fail("partial write")

        def close(self):
            pass

    monkeypatch.setattr(importer, "ManagementRepository", Repository)
    monkeypatch.setattr(
        importer,
        "read_config",
        lambda _: {
            "projects": [{"projectId": "project", "graphProject": "graph"}],
            "graph": {},
            "runtime": {"providerEnvironment": {}},
        },
    )
    (tmp_path / "batch").mkdir()
    (tmp_path / "batch/manifest.json").write_text(
        '{"candidateRevision":"'
        + "a" * 64
        + '","units":[{"id":"update-repair-r1-skill","group":"skill"}]}'
    )
    calls = []

    def collect(*args, **kwargs):
        calls.append(args[2])
        raise ManagementError("invalid_fixture", 422)

    monkeypatch.setattr(importer, "collect_trial", collect)
    monkeypatch.setattr(
        importer, "validated_pair", lambda *args: {"validationId": "v", "assessmentId": "a"}
    )
    with pytest.raises(ManagementError):
        importer.import_batch(
            tmp_path / "config",
            "project",
            "skill",
            tmp_path / "batch",
            tmp_path / "qualification",
            tmp_path,
            tmp_path / "imports",
        )
    assert len(calls) == 1
    assert not (tmp_path / "imports").exists()
