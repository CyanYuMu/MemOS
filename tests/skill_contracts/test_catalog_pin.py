"""The published catalog must stay bound to the exported Skill it claims to describe."""

import hashlib
import json

from pathlib import Path

from memos.skill_adapter.artifacts import _digest, _json_bytes
from memos.skill_contracts.mapping import catalog


EXPORT = (
    Path(__file__).resolve().parents[2]
    / "examples/skill_prototype/p5"
    / "mem-python-mock-fixture-cont-rc7fb3c895b-10c46854f64f"
)


def manifest():
    return json.loads((EXPORT / "manifest.json").read_text(encoding="utf-8"))


def test_catalog_revision_and_export_hash_match_the_published_export():
    value, exported = catalog(), manifest()
    assert value["skillRevision"] == exported["revision"]
    assert value["exportHash"] == exported["export_hash"]
    assert value["skillFileSha256"] == exported["files"]["SKILL.md"]


def test_export_hash_recomputes_from_the_manifest_without_its_own_digest():
    exported = dict(manifest())
    stored = exported.pop("export_hash")
    assert _digest(_json_bytes(exported)) == stored


def test_skill_file_digest_matches_the_retained_export_bytes():
    exported = manifest()
    body = (EXPORT / "SKILL.md").read_bytes()
    assert hashlib.sha256(body).hexdigest() == exported["files"]["SKILL.md"]
