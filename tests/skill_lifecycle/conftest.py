"""Each lifecycle test owns an isolated real Neo4j project."""

import json
import os
import uuid

from pathlib import Path

import pytest

from memos.skill_management.repository import ManagementRepository


@pytest.fixture
def repo():
    path = os.environ.get("MEMOS_SKILL_GRAPH_TEST_CONFIG")
    if not path:
        pytest.skip("isolated graph configuration required")
    config = json.loads(Path(path).read_text())
    result = ManagementRepository(
        **{k: config[k] for k in ("uri", "user", "password", "database", "memory_database_id")},
        project_id="lifecycle-test-" + uuid.uuid4().hex,
    )
    yield result
    result.close()
