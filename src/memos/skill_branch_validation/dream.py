"""A journaled action interpreter, independently authored from the UPDATE contract."""

from memos.skill_branch_validation.harness import FOOTER, HEADER, source
from memos.skill_branch_validation.tasks import BranchTask


FAKE = source("""
    from copy import deepcopy

    class FakeGraphDB:
        def __init__(self):
            self.nodes = {}
            self.added = []
            self.updated = []

        def update_node(self, id, fields, user_name=None):
            if not isinstance(fields, dict):
                raise TypeError("fields must be a dictionary")
            self.nodes.setdefault((user_name, id), {}).update(deepcopy(fields))
            self.updated.append((id, deepcopy(fields), user_name))

        def add_node(self, id, fields, user_name=None):
            self.nodes[(user_name, id)] = deepcopy(fields)
            self.added.append((id, deepcopy(fields), user_name))
""")
BROKEN_FAKE = FAKE.replace(
    'if not isinstance(fields, dict):\n            raise TypeError("fields must be a dictionary")',
    "fields = dict(fields)",
)
UNSAFE_FAKE = FAKE.replace(
    'def update_node(self, id, fields, user_name=None):\n        if not isinstance(fields, dict):\n            raise TypeError("fields must be a dictionary")',
    "def update_node(self, id, *args, user_name=None):\n        fields = next((x for x in args if isinstance(x, dict)), {})",
)

CALLER = source("""
    def apply_action(graph, action, cube, enabled=True):
        if not enabled or action.get("confidence", 1) <= 0:
            return False
        kind = action["kind"]
        target = action.get("target")
        content = action.get("content")
        if kind == "update":
            if not target:
                return False
            fields = {"dream_action_type": "update", "last_action": action["action_id"]}
            if content:
                fields["memory"] = content
            graph.update_node(target, fields, user_name=cube)
            return True
        if kind == "archive":
            if not target:
                return False
            graph.update_node(target, {"status": "archived"}, user_name=cube)
            return True
        if kind in ("create", "merge"):
            sources = action.get("sources", [])
            if not content or (kind == "merge" and not sources):
                return False
            new_id = "dream-" + action["action_id"]
            fields = {"memory": content, "status": "activated"}
            if kind == "merge":
                fields["merged_from"] = list(sources)
            graph.add_node(new_id, fields, user_name=cube)
            for old in sources if kind == "merge" else []:
                graph.update_node(old, {"status": "archived", "merged_into": new_id}, user_name=cube)
            return True
        return False
""")
BROKEN_CALLER = CALLER.replace(
    "graph.update_node(target, fields, user_name=cube)",
    "graph.update_node(target, list(fields.items()), user_name=cube)",
    1,
)

CONTRACT = """# MemOS Dream UPDATE contract probe

This independently authored action interpreter models the published MemOS UPDATE
boundary; it is not the production persistence class. Repair caller.py and
fake_graph.py. Preserve every other file, action, and valid update.

FakeGraphDB.update_node has the exact signature (id, fields, user_name=None).
fields must be a dictionary. Lists of pairs, strings, and an extra positional
content argument must raise TypeError before any state or write-log change.
Valid calls merge dictionary fields into nodes[(user_name,id)] and append a deep
copy to updated. add_node replaces a node and appends a deep copy to added.

apply_action(graph, action, cube, enabled=True) is a journaled interpreter:
UPDATE sends a dictionary containing dream_action_type='update' and last_action
to update_node(target, fields, user_name=cube). Nonempty content also sets memory;
empty/None content preserves existing memory. CREATE writes dream-{action_id};
ARCHIVE changes the target status; MERGE creates a node with merged_from and
archives its sources with merged_into. Do not widen read scope or bypass rejection.
Disabled storage, confidence <= 0, missing targets, missing new content, empty
MERGE sources and unsupported actions produce no write and return False.

Run all public checks through run_visible_checks before/after edits:
caller_contract, fake_contract, caller_with_fake, other_actions, no_unintended_write.
Do not change TASK.md or visible_checks.py. Do not add files.
"""

VISIBLE = source("""
    from copy import deepcopy
    from caller import apply_action
    from fake_graph import FakeGraphDB

    results = []
    def check(name, fn):
        try:
            fn()
            results.append(True)
            print(name + ": passed")
        except Exception as error:
            results.append(False)
            print(name + ": failed (" + type(error).__name__ + ")")

    class StrictBackend:
        def __init__(self): self.updated = []
        def update_node(self, id, fields, user_name=None):
            if not isinstance(fields, dict): raise TypeError("dict required")
            self.updated.append((id, fields, user_name))

    def positive(kind):
        g = kind()
        assert apply_action(g, {"kind":"update", "target":"n", "content":"next", "action_id":"a"}, "cube")
        assert g.updated == [("n", {"dream_action_type":"update", "last_action":"a", "memory":"next"}, "cube")]

    def rejection():
        g = FakeGraphDB()
        try: g.update_node("n", [("memory", "wrong")], user_name="cube")
        except TypeError: pass
        else: raise AssertionError("a list is not dictionary fields")
        assert not g.updated and not g.nodes

    def other():
        g = FakeGraphDB()
        assert apply_action(g, {"kind":"create", "content":"hello", "action_id":"b"}, "cube")
        assert g.added[0][0] == "dream-b"
        assert apply_action(g, {"kind":"archive", "target":"dream-b", "action_id":"c"}, "cube")
        assert g.nodes[("cube", "dream-b")]["status"] == "archived"

    def denied():
        g = FakeGraphDB()
        assert not apply_action(g, {"kind":"update", "action_id":"d"}, "cube")
        assert not g.added and not g.updated and not g.nodes

    check("caller_contract", lambda: positive(StrictBackend))
    check("fake_contract", rejection)
    check("caller_with_fake", lambda: positive(FakeGraphDB))
    check("other_actions", other)
    check("no_unintended_write", denied)
    raise SystemExit(0 if all(results) else 1)
""")

ORACLE = (
    HEADER
    + source("""
    class Backend:
        def __init__(self):
            self.nodes = {}
            self.added = []
            self.updated = []
        def update_node(self, id, fields, user_name=None):
            if type(fields) is not dict: raise TypeError("independent dict contract")
            self.updated.append((id, deepcopy(fields), user_name))
            self.nodes.setdefault((user_name, id), {}).update(deepcopy(fields))
        def add_node(self, id, fields, user_name=None):
            self.added.append((id, deepcopy(fields), user_name))
            self.nodes[(user_name, id)] = deepcopy(fields)

    def action(kind, **more):
        return {"kind": kind, "action_id": "journal-31", **more}

    def updates(graph_type):
        for content in ("independent text", "", None):
            g = graph_type()
            g.nodes[("cube-9", "record-81")] = {"memory": "keep", "other": "preserved"}
            assert caller.apply_action(g, action("update", target="record-81", content=content), "cube-9") is True
            expected = {"dream_action_type":"update", "last_action":"journal-31"}
            if content: expected["memory"] = content
            assert g.updated == [("record-81", expected, "cube-9")]
            assert g.nodes[("cube-9", "record-81")]["memory"] == (content or "keep")
            assert g.nodes[("cube-9", "record-81")]["other"] == "preserved"
            assert not g.added

    def fake_contract():
        g = fake.FakeGraphDB()
        fields = {"metadata": {"nested": [1, 2]}}
        g.update_node("one", fields, user_name="A")
        fields["metadata"]["nested"].append(3)
        assert g.updated == [("one", {"metadata": {"nested": [1, 2]}}, "A")]
        before = deepcopy((g.nodes, g.added, g.updated))
        for invalid in ([("memory", "not dict")], "raw content", None, 7):
            rejects(g.update_node, "bad", invalid, user_name="A")
            assert (g.nodes, g.added, g.updated) == before
        rejects(g.update_node, "bad", "extra content", {"memory":"value"}, user_name="A")
        assert (g.nodes, g.added, g.updated) == before

    def other_actions():
        for graph_type in (Backend, fake.FakeGraphDB):
            g = graph_type()
            assert caller.apply_action(g, action("create", content="created"), "C")
            assert g.added == [("dream-journal-31", {"memory":"created", "status":"activated"}, "C")]
            assert not g.updated
            g = graph_type()
            assert caller.apply_action(g, action("archive", target="old"), "C")
            assert g.updated == [("old", {"status":"archived"}, "C")] and not g.added
            g = graph_type()
            assert caller.apply_action(g, action("merge", sources=["left", "right"], content="combined"), "C")
            assert g.added == [("dream-journal-31", {"memory":"combined", "status":"activated", "merged_from":["left", "right"]}, "C")]
            assert g.updated == [(key, {"status":"archived", "merged_into":"dream-journal-31"}, "C") for key in ("left", "right")]

    def no_unintended_write():
        for graph_type in (Backend, fake.FakeGraphDB):
            for item in (action("update"), action("archive"), action("create"),
                         action("merge", content="missing sources"), action("unsupported"),
                         action("update", target="node", content="low", confidence=0)):
                g = graph_type()
                assert caller.apply_action(g, item, "tenant") is False
                assert not g.nodes and not g.added and not g.updated
            g = graph_type()
            assert caller.apply_action(g, action("update", target="node"), "tenant", enabled=False) is False
            assert not g.nodes and not g.added and not g.updated

    check("caller_contract", lambda: updates(Backend))
    check("fake_contract", fake_contract)
    check("caller_with_fake", lambda: updates(fake.FakeGraphDB))
    check("other_actions", other_actions)
    check("no_unintended_write", no_unintended_write)
""")
    + FOOTER
)


def dream_task() -> BranchTask:
    common = {"TASK.md": CONTRACT, "visible_checks.py": VISIBLE}
    return BranchTask(
        task_id="branch-dream-journal",
        revision="1",
        lineage_id="independent-action-journal-20261109",
        purpose="branch_validation",
        initial_files={**common, "caller.py": BROKEN_CALLER, "fake_graph.py": BROKEN_FAKE},
        reference_files={**common, "caller.py": CALLER, "fake_graph.py": FAKE},
        unsafe_files={**common, "caller.py": CALLER, "fake_graph.py": UNSAFE_FAKE},
        editable_paths=("caller.py", "fake_graph.py"),
        oracle=ORACLE,
        required_checks=(
            "caller_contract",
            "fake_contract",
            "caller_with_fake",
            "other_actions",
            "no_unintended_write",
        ),
        expected_failures={
            "initial": ("caller_contract", "fake_contract"),
            "reference": (),
            "unsafe": ("fake_contract",),
        },
        target_branches=("confirm_contract#4", "repair_dream_update#0", "verify_dream_update#0"),
        provenance={
            "kind": "independently_authored_contract_probe",
            "source_code_copied": False,
            "contract_overlap": "MemOS Dream dictionary-fields UPDATE and rejection-without-write semantics",
            "structural_difference": "Dictionary action interpreter and stateful journal, list-of-pairs mismatch; no production persistence class or source patch copied",
            "limitations": "Contract-directed probe, deterministic IDs replace production UUIDs; not a generalization or causal-benefit experiment",
        },
    )
