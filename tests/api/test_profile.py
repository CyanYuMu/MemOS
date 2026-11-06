"""T3.2 profile: template tree, binding, extraction guard, cascade and recall."""

import pytest

from memos.api.cloud_store import JsonMetadataStore
from memos.api.profile import ProfileService, ProfileValidationError


class FakeLLM:
    def __init__(self, output):
        self.output = output
        self.prompts = []

    def generate(self, messages, **kwargs):
        self.prompts.append(messages[0]["content"])
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


TEMPLATE = {
    "basic": {
        "name": {"value": None, "algorithm_updatable": True},
        "city": {"value": None, "algorithm_updatable": False},
    },
    "work": {"role": {"value": None, "algorithm_updatable": True}},
}


def _service(tmp_path):
    return ProfileService(store=JsonMetadataStore(tmp_path / "store"))


def _template(service, tenant="tenant-1", metadata=TEMPLATE):
    return service.add_template(user_name=tenant, name="basic profile", metadata=metadata)


def _bound(service, template, tenant="tenant-1", user_id="u1"):
    return service.bind(
        user_name=tenant,
        bind_list=[{"profile_template_id": template["entity_id"], "user_id": user_id}],
    )[0]


def test_template_requires_name_and_a_tree(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(ProfileValidationError, match="name is required"):
        service.add_template(user_name="tenant-1", name=" ", metadata=TEMPLATE)
    with pytest.raises(ProfileValidationError, match="non-empty object"):
        service.add_template(user_name="tenant-1", name="x", metadata={})


def test_template_depth_is_limited_to_three_levels(tmp_path):
    service = _service(tmp_path)
    too_deep = {"a": {"b": {"c": {"d": {"value": 1}}}}}

    with pytest.raises(ProfileValidationError, match="3 levels"):
        service.add_template(user_name="tenant-1", name="deep", metadata=too_deep)

    allowed = {"a": {"b": {"c": {"value": 1}}}}
    assert service.add_template(user_name="tenant-1", name="ok", metadata=allowed)


def test_template_leaves_are_normalised(tmp_path):
    service = _service(tmp_path)
    template = _template(service)

    assert template["metadata"]["basic"]["name"] == {
        "value": None,
        "algorithm_updatable": True,
    }
    # algorithm_updatable defaults to True when omitted.
    template2 = service.add_template(
        user_name="tenant-1", name="t2", metadata={"x": {"value": "1"}}
    )
    assert template2["metadata"]["x"]["algorithm_updatable"] is True


def test_templates_are_tenant_scoped(tmp_path):
    service = _service(tmp_path)
    template = _template(service)

    assert service.get_template(user_name="tenant-2", template_id=template["entity_id"]) is None
    assert service.list_templates(user_name="tenant-2") == ([], 0)
    assert (
        service.update_template(
            user_name="tenant-2", template_id=template["entity_id"], name="hijack"
        )
        is None
    )
    assert service.delete_template(user_name="tenant-2", template_id=template["entity_id"]) is False


def test_template_list_paginates_on_a_page_size_alone(tmp_path):
    service = _service(tmp_path)
    for index in range(15):
        service.add_template(user_name="tenant-1", name=f"t{index}", metadata={"x": {"value": "1"}})

    first, total = service.list_templates(user_name="tenant-1", page_size=10)
    second, _ = service.list_templates(user_name="tenant-1", page=2, page_size=10)
    everything, _ = service.list_templates(user_name="tenant-1")

    assert (len(first), len(second), len(everything), total) == (10, 5, 15, 15)


def test_bind_creates_one_instance_per_subject_and_is_idempotent(tmp_path):
    service = _service(tmp_path)
    template = _template(service)

    first = _bound(service, template, user_id="u1")
    second = _bound(service, template, user_id="u1")
    other = _bound(service, template, user_id="u2")

    assert first["id"] == second["id"]
    assert other["id"] != first["id"]
    assert first["status"] == "bound"
    assert first["subject_type"] == "user"
    assert first["metadata"]["basic.name"]["value"] is None


def test_bind_requires_exactly_one_subject(tmp_path):
    service = _service(tmp_path)
    template = _template(service)

    with pytest.raises(ProfileValidationError, match="exactly one"):
        service.bind(
            user_name="tenant-1",
            bind_list=[
                {
                    "profile_template_id": template["entity_id"],
                    "user_id": "u1",
                    "agent_id": "a1",
                }
            ],
        )


def test_bind_unknown_template_is_rejected(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(ProfileValidationError, match="not found"):
        service.bind(
            user_name="tenant-1", bind_list=[{"profile_template_id": "missing", "user_id": "u1"}]
        )


def test_manual_edit_accepts_flat_and_nested_values(tmp_path):
    service = _service(tmp_path)
    template = _template(service)
    _bound(service, template)

    edited = service.edit(
        user_name="tenant-1",
        template_id=template["entity_id"],
        user_id="u1",
        metadata={"basic": {"name": {"value": "Alice"}}, "work.role": "engineer"},
    )

    assert edited["metadata"]["basic.name"]["value"] == "Alice"
    assert edited["metadata"]["work.role"]["value"] == "engineer"


def test_an_edit_can_lock_a_field_and_add_a_new_one_and_both_survive_a_template_update(tmp_path):
    service = _service(tmp_path)
    template = _template(service)
    _bound(service, template)

    service.edit(
        user_name="tenant-1",
        template_id=template["entity_id"],
        user_id="u1",
        metadata={
            "basic": {"name": {"value": "Alice", "algorithm_updatable": False}},
            "extra": {"salary": {"value": "100"}},
        },
    )
    service.update_template(
        user_name="tenant-1",
        template_id=template["entity_id"],
        metadata={**TEMPLATE, "work": {"role": {"value": None, "algorithm_updatable": False}}},
    )

    fields = service.query(user_name="tenant-1", user_id="u1")[0]["metadata"]
    assert fields["basic.name"] == {"value": "Alice", "algorithm_updatable": False}
    assert fields["extra.salary"]["value"] == "100" and "deprecated" not in fields["extra.salary"]
    assert fields["work.role"]["algorithm_updatable"] is False


def test_an_edit_keeps_the_instance_flag_unless_the_request_sets_one(tmp_path):
    service = _service(tmp_path)
    template = _template(service)
    _bound(service, template)
    args = {"user_name": "tenant-1", "template_id": template["entity_id"], "user_id": "u1"}

    kept = service.edit(**args, metadata={"basic.city": "Beijing"})
    unlocked = service.edit(
        **args, metadata={"basic": {"city": {"value": "Shanghai", "algorithm_updatable": True}}}
    )

    assert kept["metadata"]["basic.city"]["algorithm_updatable"] is False
    assert unlocked["metadata"]["basic.city"] == {"value": "Shanghai", "algorithm_updatable": True}


def test_manual_edit_remove_fields_keeps_the_path_but_clears_value(tmp_path):
    service = _service(tmp_path)
    template = _template(service)
    _bound(service, template)
    service.edit(
        user_name="tenant-1",
        template_id=template["entity_id"],
        user_id="u1",
        metadata={"basic.name": "Alice"},
    )

    edited = service.edit(
        user_name="tenant-1",
        template_id=template["entity_id"],
        user_id="u1",
        remove_fields=["basic.name"],
    )

    assert edited["metadata"]["basic.name"]["value"] is None
    assert edited["metadata"]["basic.name"]["deprecated"] is True


def test_extraction_updates_only_algorithm_updatable_fields(tmp_path):
    service = _service(tmp_path)
    template = _template(service)
    _bound(service, template)
    llm = FakeLLM('{"basic.name": "Alice", "basic.city": "Shanghai", "work.role": "PM"}')

    updated = service.extract_and_update(
        user_name="tenant-1", llm=llm, conversation="I am Alice, a PM in Shanghai", user_id="u1"
    )

    fields = updated[0]["metadata"]
    assert fields["basic.name"]["value"] == "Alice"
    assert fields["work.role"]["value"] == "PM"
    assert fields["basic.city"]["value"] is None  # locked field untouched
    assert "basic.name" in llm.prompts[0]


def test_extraction_ignores_unknown_paths_and_empty_values(tmp_path):
    service = _service(tmp_path)
    template = _template(service)
    _bound(service, template)
    llm = FakeLLM('{"basic.name": null, "basic.hacker": "x"}')

    updated = service.extract_and_update(
        user_name="tenant-1", llm=llm, conversation="nothing", user_id="u1"
    )

    assert updated == []


def test_extraction_failure_leaves_instance_untouched(tmp_path):
    service = _service(tmp_path)
    template = _template(service)
    _bound(service, template)

    updated = service.extract_and_update(
        user_name="tenant-1",
        llm=FakeLLM(RuntimeError("llm down")),
        conversation="I am Alice",
        user_id="u1",
    )

    assert updated == []


def test_template_update_adds_fields_and_deprecates_removed_ones(tmp_path):
    service = _service(tmp_path)
    template = _template(service)
    _bound(service, template)
    service.edit(
        user_name="tenant-1",
        template_id=template["entity_id"],
        user_id="u1",
        metadata={"basic.name": "Alice", "basic.city": "Beijing"},
    )

    updated = service.update_template(
        user_name="tenant-1",
        template_id=template["entity_id"],
        metadata={
            "basic": {"name": {"value": None, "algorithm_updatable": True}},
            "contact": {"email": {"value": None, "algorithm_updatable": False}},
        },
    )

    assert "basic.city" in updated["deprecated_paths"]
    instances = service.query(user_name="tenant-1", user_id="u1")
    fields = instances[0]["metadata"]
    assert fields["basic.name"]["value"] == "Alice"  # kept
    assert fields["basic.city"]["value"] == "Beijing"  # retained, marked deprecated
    assert fields["basic.city"]["deprecated"] is True
    assert fields["contact.email"]["value"] is None  # new field


def test_template_delete_detaches_instances_without_losing_values(tmp_path):
    service = _service(tmp_path)
    template = _template(service)
    _bound(service, template)
    service.edit(
        user_name="tenant-1",
        template_id=template["entity_id"],
        user_id="u1",
        metadata={"basic.name": "Alice"},
    )

    assert service.delete_template(user_name="tenant-1", template_id=template["entity_id"]) is True

    instances = service.query(user_name="tenant-1", user_id="u1")
    assert instances[0]["status"] == "detached"
    assert instances[0]["metadata"]["basic.name"]["value"] == "Alice"


def test_delete_instance_is_explicit(tmp_path):
    service = _service(tmp_path)
    template = _template(service)
    _bound(service, template)

    assert (
        service.delete_instance(
            user_name="tenant-1", template_id=template["entity_id"], user_id="u1"
        )
        is True
    )
    assert service.query(user_name="tenant-1", user_id="u1") == []


def test_recall_shapes_profile_detail_list(tmp_path):
    service = _service(tmp_path)
    template = _template(service)
    _bound(service, template)
    service.edit(
        user_name="tenant-1",
        template_id=template["entity_id"],
        user_id="u1",
        metadata={"basic.name": "Alice", "work.role": "PM"},
    )

    memories = service.recall(user_name="tenant-1", user_id="u1", relativity=0.9)

    by_path = {mem["profile_path"]: mem for mem in memories}
    assert set(by_path) == {"basic.name", "work.role"}
    name = by_path["basic.name"]
    assert name["memory_type"] == "ProfileMemory"
    assert name["profile_category"] == "basic"
    assert name["profile_field"] == "name"
    assert name["memory"] == "basic.name: Alice"
    assert name["status"] == "activated"
    assert name["relativity"] == 0.9
    assert name["algorithm_updatable"] is True


def test_recall_skips_empty_and_deprecated_fields_and_applies_top_k(tmp_path):
    service = _service(tmp_path)
    template = _template(service)
    _bound(service, template)
    service.edit(
        user_name="tenant-1",
        template_id=template["entity_id"],
        user_id="u1",
        metadata={"basic.name": "Alice", "work.role": "PM"},
    )
    service.edit(
        user_name="tenant-1",
        template_id=template["entity_id"],
        user_id="u1",
        remove_fields=["work.role"],
    )

    memories = service.recall(user_name="tenant-1", user_id="u1", top_k=1)

    assert [mem["profile_path"] for mem in memories] == ["basic.name"]


def test_recall_isolated_between_subjects(tmp_path):
    service = _service(tmp_path)
    template = _template(service)
    _bound(service, template, user_id="u1")
    _bound(service, template, user_id="u2")
    service.edit(
        user_name="tenant-1",
        template_id=template["entity_id"],
        user_id="u1",
        metadata={"basic.name": "Alice"},
    )

    assert service.recall(user_name="tenant-1", user_id="u2") == []
    assert [m["memory"] for m in service.recall(user_name="tenant-1", user_id="u1")] == [
        "basic.name: Alice"
    ]


def test_search_collects_user_and_agent_profiles_separately(monkeypatch, tmp_path):
    """``_subject`` accepts exactly one id, so the handler must call recall twice."""
    from types import SimpleNamespace

    from memos.api.handlers import search_handler as module
    from memos.api.product_models import APISearchRequest

    service = _service(tmp_path)
    template = _template(service)
    service.bind(
        user_name="tenant-1",
        bind_list=[
            {"profile_template_id": template["entity_id"], "user_id": "u1"},
            {"profile_template_id": template["entity_id"], "agent_id": "a1"},
        ],
    )
    service.edit(
        user_name="tenant-1",
        template_id=template["entity_id"],
        user_id="u1",
        metadata={"basic.name": "Alice"},
    )
    service.edit(
        user_name="tenant-1",
        template_id=template["entity_id"],
        agent_id="a1",
        metadata={"basic.city": "Beijing"},
    )

    monkeypatch.setattr("memos.api.cloud_store.build_metadata_store", lambda _graph: service.store)
    handler = object.__new__(module.SearchHandler)
    handler.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    handler.deps = SimpleNamespace(graph_db=object(), searcher=None)
    handler._resolve_cube_ids = lambda _req: ["tenant-1"]
    req = APISearchRequest(
        query="q",
        user_id="u1",
        agent_id="a1",
        include_memory_view=["profile"],
    )

    results = handler._append_profile_memories({}, req)

    memories = results["profile_mem"][0]["memories"]
    assert {mem["profile_path"] for mem in memories} == {"basic.name", "basic.city"}
