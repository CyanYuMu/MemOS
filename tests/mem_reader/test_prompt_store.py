"""Registry, version store and prompt_scope behaviour.

The hash table below is the contract from ``spec.md`` section 2: extracting the
built-in templates into the registry must not change a single byte.
"""

import hashlib
import sqlite3

import pytest

from memos.exceptions import PromptNotFoundError, PromptValidationError, PromptVersionConflictError
from memos.mem_reader import prompt_store
from memos.mem_reader.prompt_registry import (
    MAX_PROMPT_CHARS,
    REGISTRY,
    default_body,
    get_spec,
    validate_body,
)
from memos.mem_reader.prompt_runtime import current_overrides, prompt_for, prompt_scope


DEFAULT_SHA256: dict[tuple[str, str], str] = {
    ("extract.chat", "zh"): "75d24eca65325c42",
    ("extract.chat", "en"): "31f5fad23154b9e7",
    ("extract.doc", "zh"): "800620cf640f21f6",
    ("extract.doc", "en"): "32e2213e4d57249b",
    ("extract.general_string", "zh"): "3e53e9c0660b2459",
    ("extract.general_string", "en"): "fe96fa082a3eea71",
    ("extract.preference_explicit", "zh"): "557bab5a472a6578",
    ("extract.preference_explicit", "en"): "d4a518a4b85ddbb9",
    ("extract.preference_implicit", "zh"): "798573849bb58a20",
    ("extract.preference_implicit", "en"): "3384cd982bd20b31",
    ("extract.event", "en"): "ddfa14f60824072f",
    ("extract.tool_trajectory", "zh"): "16becbd99b2d3330",
    ("extract.tool_trajectory", "en"): "3037164e72eacc22",
    ("process.hallucination_filter", "en"): "6adbc7c45d60998a",
    ("dream.motive", "en"): "44e8d82516536fc0",
    ("dream.reasoning", "en"): "8760189a24743738",
    ("dream.context_binding", "en"): "d01844794c514135",
    ("dream.context_summary", "en"): "8984087542b90fc2",
}

CUBE = "cube-store-test"


@pytest.fixture(autouse=True)
def prompt_db(tmp_path, monkeypatch):
    path = tmp_path / "prompts.db"
    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(path))
    return path


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_every_default_is_byte_identical_to_the_pre_registry_text():
    assert set(DEFAULT_SHA256) == {
        (spec.key, lang) for spec in REGISTRY.values() for lang in spec.languages
    }
    for (key, lang), expected in DEFAULT_SHA256.items():
        digest = hashlib.sha256(default_body(key, lang).encode()).hexdigest()[:16]
        assert digest == expected, f"{key}/{lang} default text changed"


def test_registry_covers_every_required_prompt():
    required = {
        "extract.chat",
        "extract.doc",
        "extract.preference_explicit",
        "extract.preference_implicit",
        "extract.event",
        "process.hallucination_filter",
        "dream.motive",
        "dream.reasoning",
        "dream.context_binding",
        "dream.context_summary",
    }
    assert required <= set(REGISTRY)
    assert len(REGISTRY) == 12


def test_every_built_in_default_passes_validation():
    for spec in REGISTRY.values():
        for lang in spec.languages:
            errors, _warnings = validate_body(spec.key, lang, spec.defaults[lang])
            assert not errors, f"{spec.key}/{lang}: {[e.as_dict() for e in errors]}"


def test_every_registration_has_sample_inputs():
    for spec in REGISTRY.values():
        assert spec.sample_inputs, spec.key
        for sample in spec.sample_inputs:
            assert {"id", "title", "input"} <= set(sample), (spec.key, sample)


def test_dream_motive_exposes_the_locked_output_suffix():
    assert get_spec("dream.motive").locked_suffix.startswith("\n\nOUTPUT FORMAT (STRICT)")


def test_unknown_key_is_not_found():
    with pytest.raises(PromptNotFoundError):
        default_body("extract.nope", "zh")
    with pytest.raises(PromptNotFoundError):
        get_spec("extract.nope")


@pytest.mark.parametrize(
    ("key", "lang", "body", "code"),
    [
        ("extract.chat", "zh", "", "empty_prompt"),
        ("extract.chat", "zh", "没有占位符", "missing_placeholder"),
        ("extract.chat", "zh", "占位 ${conversation} 和 ${nope}", "unknown_placeholder"),
        ("extract.event", "en", "{conversation", "bad_braces"),
        ("extract.event", "en", "{messages}", "unknown_field"),
        ("extract.event", "en", "{-1} {conversation}", "unsupported_field"),
        (
            "extract.chat",
            "zh",
            "${conversation}" + "x" * MAX_PROMPT_CHARS,
            "too_long",
        ),
        ("dream.motive", "zh", "{memories_block}", "unknown_language"),
    ],
)
def test_validate_reports_actionable_errors(key, lang, body, code):
    errors, _warnings = validate_body(key, lang, body)
    assert code in [issue.code for issue in errors]


def test_validate_warns_without_blocking_when_output_contract_is_missing():
    errors, warnings = validate_body(
        "extract.chat", "zh", "只抽取部署相关内容。\n\n${conversation}"
    )
    assert not errors
    assert [w.code for w in warnings] == ["missing_output_key"]


def test_validate_accepts_the_built_in_default_again():
    body = default_body("extract.chat", "zh")
    errors, warnings = validate_body("extract.chat", "zh", body)
    assert not errors and not warnings


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_resolve_returns_the_default_when_nothing_is_saved():
    resolved = prompt_store.resolve(CUBE, "extract.chat", "zh")
    assert resolved.source == "default"
    assert resolved.version_id == 0
    assert resolved.body == default_body("extract.chat", "zh")


def test_save_appends_versions_and_resolve_follows_the_latest():
    first = prompt_store.save_slot(CUBE, "extract.chat", "zh", body="甲 ${conversation}")
    assert first["version_id"] == 1
    second = prompt_store.save_slot(
        CUBE, "extract.chat", "zh", body="乙 ${conversation}", base_version_id=1
    )
    assert second["version_id"] == 2

    resolved = prompt_store.resolve(CUBE, "extract.chat", "zh")
    assert resolved.body == "乙 ${conversation}"
    assert resolved.source == "project"
    assert resolved.version_id == 2
    assert resolved.updated_at == second["created_at"]


def test_slots_are_isolated_by_language_and_project():
    prompt_store.save_slot(CUBE, "extract.chat", "zh", body="甲 ${conversation}")
    assert prompt_store.resolve(CUBE, "extract.chat", "en").source == "default"
    assert prompt_store.resolve("other-cube", "extract.chat", "zh").source == "default"


def test_saving_the_live_body_does_not_add_a_version():
    prompt_store.save_slot(CUBE, "extract.chat", "zh", body="甲 ${conversation}")
    again = prompt_store.save_slot(
        CUBE, "extract.chat", "zh", body="甲 ${conversation}", base_version_id=1
    )
    assert again["version_id"] == 1
    versions, _has_more = prompt_store.list_versions(CUBE, "extract.chat", "zh")
    assert len(versions) == 1


def test_stale_base_version_conflicts():
    prompt_store.save_slot(CUBE, "extract.chat", "zh", body="甲 ${conversation}")
    with pytest.raises(PromptVersionConflictError) as excinfo:
        prompt_store.save_slot(CUBE, "extract.chat", "zh", body="乙 ${conversation}")
    assert excinfo.value.current["version_id"] == 1


def test_invalid_body_is_rejected_before_it_is_stored():
    with pytest.raises(PromptValidationError) as excinfo:
        prompt_store.save_slot(CUBE, "extract.chat", "zh", body="没有占位符")
    assert excinfo.value.errors[0]["code"] == "missing_placeholder"
    assert prompt_store.resolve(CUBE, "extract.chat", "zh").source == "default"


def test_rollback_appends_the_old_body_as_a_rollback_row():
    prompt_store.save_slot(CUBE, "extract.chat", "zh", body="甲 ${conversation}")
    prompt_store.save_slot(CUBE, "extract.chat", "zh", body="乙 ${conversation}", base_version_id=1)
    rolled = prompt_store.save_slot(
        CUBE, "extract.chat", "zh", restore_from_version_id=1, base_version_id=2
    )
    assert rolled["effective"]["body"] == "甲 ${conversation}"
    assert rolled["effective"]["version_id"] == 3
    versions, _ = prompt_store.list_versions(CUBE, "extract.chat", "zh")
    assert versions[0]["kind"] == "rollback"
    assert versions[0]["parent_version_id"] == 2


def test_rollback_to_a_reset_version_returns_to_the_default():
    prompt_store.save_slot(CUBE, "extract.chat", "zh", body="甲 ${conversation}")
    reset = prompt_store.reset_slots(CUBE, key="extract.chat", lang="zh")
    assert reset[0]["version_id"] == 2
    rolled = prompt_store.save_slot(
        CUBE, "extract.chat", "zh", restore_from_version_id=2, base_version_id=0
    )
    assert rolled["effective"]["source"] == "default"


def test_reset_only_touches_overridden_slots():
    prompt_store.save_slot(CUBE, "extract.chat", "zh", body="甲 ${conversation}")
    reset = prompt_store.reset_slots(CUBE, key="extract.chat", lang="zh")
    assert [(item["key"], item["lang"]) for item in reset] == [("extract.chat", "zh")]
    assert prompt_store.resolve(CUBE, "extract.chat", "zh").source == "default"
    assert prompt_store.reset_slots(CUBE, key="extract.chat", lang="zh") == []


def test_reset_whole_project_covers_every_overridden_slot():
    prompt_store.save_slot(CUBE, "extract.chat", "zh", body="甲 ${conversation}")
    prompt_store.save_slot(
        CUBE, "dream.motive", "en", body="{memories_block} {max_motives}", base_version_id=0
    )
    reset = prompt_store.reset_slots(CUBE)
    assert {(item["key"], item["lang"]) for item in reset} == {
        ("extract.chat", "zh"),
        ("dream.motive", "en"),
    }
    assert all(
        prompt_store.resolve(CUBE, key, lang).source == "default"
        for key, lang in (("extract.chat", "zh"), ("dream.motive", "en"))
    )


def test_versions_paginate_newest_first():
    for index in range(3):
        prompt_store.save_slot(
            CUBE, "extract.chat", "zh", body=f"第{index}版 ${{conversation}}", base_version_id=index
        )
    page, has_more = prompt_store.list_versions(CUBE, "extract.chat", "zh", limit=2)
    assert [v["version_id"] for v in page] == [3, 2]
    assert has_more is True
    page_two, has_more_two = prompt_store.list_versions(
        CUBE, "extract.chat", "zh", limit=2, before_version_id=2
    )
    assert [v["version_id"] for v in page_two] == [1]
    assert has_more_two is False


def test_list_and_detail_report_default_state():
    items = {item["key"]: item for item in prompt_store.list_items(CUBE)}
    assert items["extract.chat"]["state"]["zh"] == {
        "source": "default",
        "version_id": 0,
        "version_no": 0,
        "updated_at": None,
        "author": None,
    }
    detail = prompt_store.detail_item(CUBE, "extract.chat")
    assert detail["defaults"]["zh"] == default_body("extract.chat", "zh")
    assert detail["effective"]["zh"]["source"] == "default"


def test_legacy_rows_are_migrated_once(prompt_db):
    conn = sqlite3.connect(prompt_db)
    with conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS extract_prompt_configs ("
            "mem_cube_id TEXT PRIMARY KEY, prompt TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO extract_prompt_configs VALUES (?, ?, ?)",
            ("legacy-cube", "旧模板 ${conversation}", "2026-01-01T00:00:00+00:00"),
        )
    conn.close()

    for _ in range(3):
        resolved = prompt_store.resolve("legacy-cube", "extract.chat", "zh")
        assert resolved.body == "旧模板 ${conversation}"
        assert resolved.source == "project"

    conn = sqlite3.connect(prompt_db)
    rows = conn.execute(
        "SELECT lang, kind, body FROM prompt_versions WHERE mem_cube_id = 'legacy-cube' "
        "ORDER BY lang"
    ).fetchall()
    conn.close()
    assert rows == [
        ("en", "migrated", "旧模板 ${conversation}"),
        ("zh", "migrated", "旧模板 ${conversation}"),
    ]


def test_migration_does_not_resurrect_a_reset_slot(prompt_db):
    conn = sqlite3.connect(prompt_db)
    with conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS extract_prompt_configs ("
            "mem_cube_id TEXT PRIMARY KEY, prompt TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO extract_prompt_configs VALUES (?, ?, ?)",
            ("legacy-cube", "旧模板 ${conversation}", "2026-01-01T00:00:00+00:00"),
        )
    conn.close()

    assert prompt_store.resolve("legacy-cube", "extract.chat", "zh").source == "project"
    prompt_store.reset_slots("legacy-cube", key="extract.chat")
    assert prompt_store.resolve("legacy-cube", "extract.chat", "zh").source == "default"


# ---------------------------------------------------------------------------
# prompt_scope / prompt_for
# ---------------------------------------------------------------------------


def test_prompt_for_returns_the_default_outside_a_scope():
    prompt_store.save_slot(CUBE, "extract.chat", "zh", body="甲 ${conversation}")
    assert prompt_for("extract.chat", "zh", "内置") == "内置"


def test_prompt_scope_applies_overrides_and_restores_afterwards():
    prompt_store.save_slot(CUBE, "extract.chat", "zh", body="甲 ${conversation}")
    with prompt_scope(CUBE):
        assert prompt_for("extract.chat", "zh", "内置") == "甲 ${conversation}"
        assert prompt_for("extract.chat", "en", "内置") == "内置"
        assert current_overrides()
    assert prompt_for("extract.chat", "zh", "内置") == "内置"


def test_prompt_scope_nests_and_restores_the_outer_project():
    prompt_store.save_slot("cube-a", "extract.chat", "zh", body="A ${conversation}")
    prompt_store.save_slot("cube-b", "extract.chat", "zh", body="B ${conversation}")
    with prompt_scope("cube-a"):
        assert prompt_for("extract.chat", "zh", "内置") == "A ${conversation}"
        with prompt_scope("cube-b"):
            assert prompt_for("extract.chat", "zh", "内置") == "B ${conversation}"
        assert prompt_for("extract.chat", "zh", "内置") == "A ${conversation}"


def test_prompt_scope_does_not_block_when_storage_is_broken(monkeypatch):
    def boom(_mem_cube_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("memos.mem_reader.prompt_store.project_overrides", boom)
    with prompt_scope(CUBE):
        assert prompt_for("extract.chat", "zh", "内置") == "内置"


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def test_concurrent_saves_with_the_same_base_version_produce_one_winner():
    """Check-then-insert must be atomic, or two editors both "win"."""
    import threading

    prompt_store.save_slot(CUBE, "extract.chat", "zh", body="v1 ${conversation}")  # version 1

    barrier = threading.Barrier(8)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        try:
            prompt_store.save_slot(
                CUBE,
                "extract.chat",
                "zh",
                body=f"并发草稿 {index} ${{conversation}}",
                base_version_id=1,
            )
            with lock:
                outcomes.append("ok")
        except PromptVersionConflictError:
            with lock:
                outcomes.append("conflict")

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert outcomes.count("ok") == 1, outcomes
    assert outcomes.count("conflict") == 7
    versions, _has_more = prompt_store.list_versions(CUBE, "extract.chat", "zh")
    assert len(versions) == 2


def test_concurrent_first_connections_migrate_exactly_once(prompt_db):
    """Eight connections opening the same legacy database at once."""
    import threading

    conn = sqlite3.connect(prompt_db)
    with conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS extract_prompt_configs ("
            "mem_cube_id TEXT PRIMARY KEY, prompt TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO extract_prompt_configs VALUES (?, ?, ?)",
            ("legacy-cube", "旧模板 ${conversation}", "2026-01-01T00:00:00+00:00"),
        )
    conn.close()

    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        prompt_store._connect().close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    conn = sqlite3.connect(prompt_db)
    rows = conn.execute(
        "SELECT lang FROM prompt_versions WHERE mem_cube_id = 'legacy-cube' ORDER BY lang"
    ).fetchall()
    conn.close()
    assert rows == [("en",), ("zh",)]


# ---------------------------------------------------------------------------
# version_no
# ---------------------------------------------------------------------------


def test_version_no_counts_every_kind_inside_the_slot():
    first = prompt_store.save_slot(CUBE, "extract.chat", "zh", body="甲 ${conversation}")
    assert first["version_no"] == 1
    second = prompt_store.save_slot(
        CUBE, "extract.chat", "zh", body="乙 ${conversation}", base_version_id=first["version_id"]
    )
    assert second["version_no"] == 2

    reset = prompt_store.reset_slots(CUBE, key="extract.chat", lang="zh")
    assert reset[0]["version_no"] == 3
    # The slot is back to the default, so the live state reports no version.
    assert prompt_store.resolve(CUBE, "extract.chat", "zh").version_no == 0

    rolled = prompt_store.save_slot(
        CUBE, "extract.chat", "zh", restore_from_version_id=first["version_id"], base_version_id=0
    )
    assert rolled["version_no"] == 4
    assert rolled["effective"]["version_no"] == 4

    versions, _ = prompt_store.list_versions(CUBE, "extract.chat", "zh")
    assert [version["version_no"] for version in versions] == [4, 3, 2, 1]


def test_version_no_is_per_slot_and_per_project():
    prompt_store.save_slot(CUBE, "extract.chat", "zh", body="甲 ${conversation}")
    prompt_store.save_slot(CUBE, "extract.chat", "en", body="a ${conversation}")
    prompt_store.save_slot("other-cube", "extract.chat", "zh", body="乙 ${conversation}")
    prompt_store.save_slot(CUBE, "extract.event", "en", body="事件 {conversation}")

    assert prompt_store.resolve(CUBE, "extract.chat", "zh").version_no == 1
    assert prompt_store.resolve(CUBE, "extract.chat", "en").version_no == 1
    assert prompt_store.resolve("other-cube", "extract.chat", "zh").version_no == 1
    assert prompt_store.resolve(CUBE, "extract.event", "en").version_no == 1


def test_version_no_appears_in_state_detail_and_conflict():
    prompt_store.save_slot(CUBE, "extract.chat", "zh", body="甲 ${conversation}")
    items = {item["key"]: item for item in prompt_store.list_items(CUBE)}
    assert items["extract.chat"]["state"]["zh"]["version_no"] == 1
    assert items["extract.chat"]["state"]["en"]["version_no"] == 0

    detail = prompt_store.detail_item(CUBE, "extract.chat")
    assert detail["effective"]["zh"]["version_no"] == 1

    with pytest.raises(PromptVersionConflictError) as excinfo:
        prompt_store.save_slot(CUBE, "extract.chat", "zh", body="乙 ${conversation}")
    assert excinfo.value.current["version_no"] == 1


# ---------------------------------------------------------------------------
# Sample inputs
# ---------------------------------------------------------------------------


def _schema_problems(value, schema, path: str = "input") -> list[str]:
    """Minimal JSON-Schema check for the subset the registry uses."""
    problems: list[str] = []
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected object"]
        for name in schema.get("required", []):
            if name not in value:
                problems.append(f"{path}.{name}: required")
        for name, sub in (schema.get("properties") or {}).items():
            if name in value:
                problems.extend(_schema_problems(value[name], sub, f"{path}.{name}"))
    elif expected == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array"]
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                problems.extend(_schema_problems(item, item_schema, f"{path}[{index}]"))
    elif expected == "string":
        if not isinstance(value, str):
            problems.append(f"{path}: expected string")
    elif expected == "integer":
        if not isinstance(value, int):
            problems.append(f"{path}: expected integer")
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: {value!r} not in {schema['enum']}")
    return problems


def test_samples_carry_the_prompt_language_and_cover_both_sides():
    for spec in REGISTRY.values():
        langs = [sample.get("lang") for sample in spec.sample_inputs]
        assert set(langs) <= set(spec.languages), (spec.key, langs, spec.languages)
        if set(spec.languages) == {"zh", "en"}:
            assert {"zh", "en"} <= set(langs), (spec.key, langs)
        else:
            assert set(langs) == set(spec.languages), (spec.key, langs)


def test_every_sample_input_matches_the_prompt_input_schema():
    for spec in REGISTRY.values():
        for sample in spec.sample_inputs:
            problems = _schema_problems(sample["input"], spec.input_schema)
            assert problems == [], (spec.key, sample["id"], problems)


# ---------------------------------------------------------------------------
# C3: format-syntax hardening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("{conversation.__class__}", "unsupported_field"),
        ("{conversation[0]}", "unsupported_field"),
        ("{0} {conversation}", "unsupported_field"),
        ("{conversation!r}", "unsupported_conversion"),
        ("{conversation:>10}", "unsupported_format_spec"),
    ],
)
def test_format_syntax_rejects_fields_that_are_not_plain_names(body, code):
    errors, _warnings = validate_body("extract.event", "en", body)
    assert code in [issue.code for issue in errors], [issue.code for issue in errors]
    assert all(issue.message for issue in errors)


def test_hardened_format_checks_still_accept_every_built_in_default():
    for spec in REGISTRY.values():
        if spec.syntax != "format":
            continue
        for lang in spec.languages:
            errors, _warnings = validate_body(spec.key, lang, spec.defaults[lang])
            assert not errors, (spec.key, lang, [issue.code for issue in errors])
