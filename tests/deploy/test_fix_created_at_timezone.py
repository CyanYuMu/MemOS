"""Regression tests for the historical timezone fix script's timestamp parser.

Neo4j returns its own ``DateTime`` type (nanosecond precision, not a
``datetime`` subclass) and stringifies with nine fractional digits. Python's
``datetime.fromisoformat`` rejects more than six, so a naive parser silently
skips every row and the migration reports "no parseable timestamp".
"""

import importlib.util

from datetime import timezone
from pathlib import Path

from pydantic import BaseModel


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/maintenance/fix_created_at_timezone.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("fix_created_at_timezone", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parses_neo4j_nanosecond_string():
    module = _load_script()

    parsed = module._parse_dt("2026-10-20T23:00:31.581067000+00:00")

    assert parsed is not None
    assert parsed.tzinfo == timezone.utc
    assert parsed.microsecond == 581067


def test_parses_neo4j_datetime_object():
    class FakeNeo4jDateTime(BaseModel):
        value: str

        def to_native(self):
            module = _load_script()
            return module._parse_dt(self.value)

    module = _load_script()
    value = FakeNeo4jDateTime(value="2026-10-20T23:00:31.581067000+00:00")

    parsed = module._parse_dt(value)

    assert parsed is not None
    assert parsed.year == 2026
