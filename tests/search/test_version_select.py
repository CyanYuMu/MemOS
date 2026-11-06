import copy
import json

from datetime import UTC, datetime

import pytest

from memos.search.time_understanding import TimeIntent
from memos.search.version_select import apply_time_intent, widened_top_k


def _utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _as_of(start, end):
    return TimeIntent("as_of", _utc(start), _utc(end), "test")


def _recent(start, end):
    return TimeIntent("recent", _utc(start), _utc(end), "test")


def _memory(memory_id, text, memory_type="LongTermMemory", **meta):
    return {
        "id": memory_id,
        "memory": text,
        "metadata": {"status": "activated", "memory_type": memory_type, **meta},
    }


def _event(memory_id, start, end=None, **meta):
    return _memory(
        memory_id,
        memory_id,
        memory_form="event",
        event_time_start=start,
        event_time_end=end or start,
        **meta,
    )


def _version(number, text, start, end=None):
    spec = {"valid_from": start, **({"valid_to": end} if end else {})}
    return {"version": number, "memory": text, "update_type": "conflict", "timespec": spec}


def _results(*memories, **other):
    return {
        "text_mem": [{"cube_id": "c", "memories": list(memories), "total_nodes": len(memories)}],
        **other,
    }


def _run(memories, intent, top_k=10, **kwargs):
    out = apply_time_intent(_results(*memories), intent, top_k=top_k, **kwargs)
    return out["text_mem"][0]["memories"]


def _ids(memories):
    return [m["id"] for m in memories]


JUNE = _as_of("2025-06-01T00:00:00", "2025-06-30T23:59:59")


# ---- events ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "inside"),
    [
        ("2025-06-10T00:00:00+00:00", None, True),
        ("2025-05-30T00:00:00+00:00", "2025-06-02T00:00:00+00:00", True),
        ("2025-06-29T00:00:00+00:00", "2025-07-05T00:00:00+00:00", True),
        ("2025-05-01T00:00:00+00:00", "2025-05-31T23:59:59+00:00", False),
        ("2025-07-01T00:00:00+00:00", None, False),
        ("2025-05-01T00:00:00+00:00", "2025-08-01T00:00:00+00:00", True),
    ],
)
def test_an_event_matches_when_its_period_overlaps_the_window(start, end, inside):
    hits = _run([_event("e", start, end), _memory("other", "x")], JUNE)

    assert ("e" in _ids(hits)) is inside


def test_an_event_without_event_time_falls_back_to_when_it_was_said():
    inside = _memory("in", "x", memory_form="event", chat_time="2025-06-10T08:00:00+00:00")
    outside = _memory("out", "x", memory_form="event", created_at="2025-09-10T08:00:00+00:00")

    assert _ids(_run([inside, outside], JUNE)) == ["in"]


def test_an_event_with_no_time_at_all_never_matches():
    undated = _memory("undated", "x", memory_form="event")
    dated = _event("dated", "2025-06-10T00:00:00+00:00")

    assert _ids(_run([undated, dated], JUNE)) == ["dated"]


# ---- states ------------------------------------------------------------------------------------

BEIJING_THEN_SHANGHAI = _memory(
    "home",
    "lives in Shanghai",
    valid_from="2025-09-01T00:00:00+00:00",
    version=3,
    history=[
        _version(1, "lives in Shenzhen", "2024-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"),
        _version(2, "lives in Beijing", "2025-01-01T00:00:00+00:00", "2025-09-01T00:00:00+00:00"),
    ],
)


@pytest.mark.parametrize(
    ("start", "end", "text"),
    [
        ("2024-06-01T00:00:00", "2024-06-30T00:00:00", "lives in Shenzhen"),
        ("2025-06-01T00:00:00", "2025-06-30T00:00:00", "lives in Beijing"),
        ("2025-10-01T00:00:00", "2025-10-31T00:00:00", "lives in Shanghai"),
        ("2025-08-01T00:00:00", "2025-10-31T00:00:00", "lives in Shanghai"),
        ("2025-01-01T00:00:00", "2025-01-01T00:00:00", "lives in Beijing"),
    ],
)
def test_a_state_reads_as_the_version_valid_in_the_window(start, end, text):
    hit = _run([BEIJING_THEN_SHANGHAI], _as_of(start, end))[0]

    assert hit["memory"] == text


def test_a_past_version_is_marked_and_the_current_text_is_kept():
    hit = _run([BEIJING_THEN_SHANGHAI], JUNE)[0]

    meta = hit["metadata"]
    assert meta["current_memory"] == "lives in Shanghai"
    assert meta["matched_version"] == {
        "version": 2,
        "valid_from": "2025-01-01T00:00:00+00:00",
        "valid_to": "2025-09-01T00:00:00+00:00",
    }


def test_the_head_is_returned_as_is_when_it_is_the_match():
    hit = _run([BEIJING_THEN_SHANGHAI], _as_of("2025-10-01T00:00:00", "2025-10-31T00:00:00"))[0]

    assert "matched_version" not in hit["metadata"] and "current_memory" not in hit["metadata"]


def test_a_window_before_the_first_version_matches_nothing_so_the_hit_falls_back():
    old = _as_of("2020-01-01T00:00:00", "2020-01-31T00:00:00")

    hits = _run([BEIJING_THEN_SHANGHAI], old)

    assert _ids(hits) == ["home"] and hits[0]["memory"] == "lives in Shanghai"


def test_a_version_without_valid_to_ends_where_the_next_one_begins():
    legacy = _memory(
        "legacy",
        "now",
        created_at="2025-09-01T00:00:00+00:00",
        history=[
            {"version": 1, "memory": "first", "created_at": "2025-01-01T00:00:00+00:00"},
            {"version": 2, "memory": "second", "created_at": "2025-05-01T00:00:00+00:00"},
        ],
    )

    assert _run([legacy], JUNE)[0]["memory"] == "second"
    jan = _as_of("2025-02-01T00:00:00", "2025-02-28T00:00:00")
    assert _run([legacy], jan)[0]["memory"] == "first"


def test_history_stored_as_json_strings_is_understood_and_bad_entries_are_skipped():
    raw = [
        json.dumps(
            _version(
                1, "lives in Beijing", "2025-01-01T00:00:00+00:00", "2025-09-01T00:00:00+00:00"
            )
        ),
        "not json",
        {"memory": 3},
        None,
    ]
    memory = _memory("m", "lives in Shanghai", valid_from="2025-09-01T00:00:00+00:00", history=raw)

    assert _run([memory], JUNE)[0]["memory"] == "lives in Beijing"
    as_string = _memory("m", "now", valid_from="2025-09-01T00:00:00+00:00", history=json.dumps(raw))
    assert _run([as_string], JUNE)[0]["memory"] == "lives in Beijing"


def test_chat_time_is_preferred_over_created_at_for_when_a_state_began():
    memory = _memory(
        "m",
        "now",
        chat_time="2025-06-10T00:00:00+00:00",
        created_at="2026-10-01T00:00:00+00:00",
    )

    assert sorted(_ids(_run([memory, _event("e", "2025-06-12T00:00:00+00:00")], JUNE))) == [
        "e",
        "m",
    ]


def test_a_head_that_ended_before_the_window_does_not_match():
    ended = _memory(
        "ended",
        "x",
        valid_from="2024-01-01T00:00:00+00:00",
        valid_to="2025-01-01T00:00:00+00:00",
    )

    assert _run([ended, _event("e", "2025-06-12T00:00:00+00:00")], JUNE) == [
        _event("e", "2025-06-12T00:00:00+00:00")
    ]


# ---- recent ------------------------------------------------------------------------------------

LAST_WEEK = _recent("2026-10-14T00:00:00", "2026-10-21T23:59:59")


def test_recent_keeps_what_began_in_the_window_newest_first():
    changed = _memory("changed", "new job", valid_from="2026-10-18T00:00:00+00:00")
    steady = _memory("steady", "likes tea", valid_from="2024-01-01T00:00:00+00:00")
    trip = _event("trip", "2026-10-20T00:00:00+00:00")
    old_trip = _event("old_trip", "2026-09-01T00:00:00+00:00")

    assert _ids(_run([changed, steady, trip, old_trip], LAST_WEEK)) == ["trip", "changed"]


def test_recent_keeps_the_given_order_when_the_caller_sorts_explicitly():
    first = _event("first", "2026-10-15T00:00:00+00:00")
    second = _event("second", "2026-10-20T00:00:00+00:00")

    assert _ids(_run([first, second], LAST_WEEK, rerank=False)) == ["first", "second"]


# ---- current / past / none ---------------------------------------------------------------------

NOW = datetime(2026, 10, 21, tzinfo=UTC)


def test_current_drops_a_head_whose_validity_has_ended():
    ended = _memory("ended", "x", valid_to="2020-01-01T00:00:00+00:00")
    live = _memory("live", "y")
    future = _memory("future", "z", valid_to="2030-01-01T00:00:00+00:00")
    dated_event = _event("ev", "2020-01-01T00:00:00+00:00", valid_to="2020-01-02T00:00:00+00:00")

    hits = _run([ended, live, future, dated_event], TimeIntent("current"), now=NOW)

    assert _ids(hits) == ["live", "future", "ev"]


def test_current_never_empties_the_answer():
    ended = _memory("ended", "x", valid_to="2020-01-01T00:00:00+00:00")

    assert _ids(_run([ended], TimeIntent("current"), now=NOW)) == ["ended"]


@pytest.mark.parametrize("kind", ["past", "none"])
def test_past_and_none_only_trim(kind):
    memories = [_memory(f"m{i}", "x") for i in range(5)]

    assert _ids(_run(memories, TimeIntent(kind), top_k=3)) == ["m0", "m1", "m2"]


def test_a_windowed_kind_without_a_window_only_trims():
    memories = [_memory(f"m{i}", "x") for i in range(5)]

    assert _ids(_run(memories, TimeIntent("as_of"), top_k=2)) == ["m0", "m1"]


# ---- what is judged and what is not ------------------------------------------------------------


def test_documents_tools_and_preferences_are_not_time_bound():
    doc = _memory(
        "doc", "manual", memory_type="OuterMemory", created_at="2026-10-01T00:00:00+00:00"
    )
    tool = _memory("tool", "deploy", memory_type="ToolTrajectoryMemory")
    pref = _memory("pref", "tea", memory_type="PreferenceMemory")
    june = _event("june", "2025-06-10T00:00:00+00:00")
    old = _event("old", "2025-01-10T00:00:00+00:00")

    assert _ids(_run([doc, old, tool, june, pref], JUNE)) == ["doc", "tool", "june", "pref"]


def test_when_nothing_is_judged_to_match_the_original_hits_come_back_trimmed():
    hits = [_event(f"e{i}", "2025-01-10T00:00:00+00:00") for i in range(5)]

    assert _ids(_run(hits, JUNE, top_k=3)) == ["e0", "e1", "e2"]


def test_the_result_is_trimmed_to_top_k_after_selection():
    hits = [_event(f"e{i}", "2025-06-10T00:00:00+00:00") for i in range(6)]

    assert _ids(_run(hits, JUNE, top_k=4)) == ["e0", "e1", "e2", "e3"]


def test_other_buckets_and_group_fields_survive_and_the_input_is_not_mutated():
    results = _results(
        BEIJING_THEN_SHANGHAI,
        _event("old", "2025-01-10T00:00:00+00:00"),
        pref_mem=[{"cube_id": "c", "memories": [_memory("p", "tea")], "total_nodes": 1}],
    )
    snapshot = copy.deepcopy(results)

    out = apply_time_intent(results, JUNE, top_k=10)

    assert results == snapshot
    assert out["pref_mem"] == snapshot["pref_mem"]
    group = out["text_mem"][0]
    assert group["cube_id"] == "c" and group["total_nodes"] == len(group["memories"]) == 1


def test_missing_or_empty_text_mem_is_returned_as_is():
    assert apply_time_intent({}, JUNE, top_k=5) == {}
    empty = {"text_mem": []}
    assert apply_time_intent(empty, JUNE, top_k=5) is empty


def test_each_cube_group_is_judged_and_trimmed_on_its_own():
    results = {
        "text_mem": [
            {"cube_id": "a", "memories": [_event("a1", "2025-06-10T00:00:00+00:00")]},
            {"cube_id": "b", "memories": [_event("b1", "2025-01-10T00:00:00+00:00")]},
        ]
    }

    out = apply_time_intent(results, JUNE, top_k=5)

    assert [_ids(g["memories"]) for g in out["text_mem"]] == [["a1"], []]


@pytest.mark.parametrize(
    ("top_k", "wide"), [(1, 3), (4, 12), (10, 30), (29, 30), (30, 30), (50, 50)]
)
def test_widened_top_k(top_k, wide):
    assert widened_top_k(top_k) == wide


# ---- time rerank -------------------------------------------------------------------------------


def _trip(memory_id, start, relativity=None):
    meta = {"relativity": relativity} if relativity is not None else {}
    return _event(memory_id, start, **meta)


def test_a_recent_query_without_a_window_leans_to_newer_hits_and_leaves_other_hits_in_place():
    old, new = _trip("old", "2025-01-01T00:00:00+00:00"), _trip("new", "2026-05-01T00:00:00+00:00")
    pref = _memory("pref", "likes tea", memory_type="PreferenceMemory")

    hits = _run([old, pref, new], TimeIntent("recent", source="rules"))

    assert _ids(hits) == ["new", "pref", "old"]


def test_the_weight_env_bounds_how_far_time_can_lift_a_hit(monkeypatch):
    old = _trip("old", "2025-01-01T00:00:00+00:00", relativity=0.8)
    new = _trip("new", "2026-05-01T00:00:00+00:00", relativity=0.7)
    intent = TimeIntent("recent", source="rules")

    assert _ids(_run([old, new], intent)) == ["new", "old"]
    monkeypatch.setenv("SEARCH_TIME_RERANK_WEIGHT", "0.05")
    assert _ids(_run([old, new], intent)) == ["old", "new"]
    monkeypatch.setenv("SEARCH_TIME_RERANK_WEIGHT", "0")
    assert _ids(_run([old, new], intent)) == ["old", "new"]


def test_reordering_by_time_leaves_the_callers_results_untouched():
    old, new = _trip("old", "2025-01-01T00:00:00+00:00"), _trip("new", "2026-05-01T00:00:00+00:00")
    results = _results(old, new)
    snapshot = copy.deepcopy(results)

    out = apply_time_intent(results, TimeIntent("recent", source="rules"), top_k=10)

    assert _ids(out["text_mem"][0]["memories"]) == ["new", "old"]
    assert results == snapshot
