"""A multi-row table task authored from the read contract, not source code slices."""

from memos.skill_branch_validation.harness import FOOTER, HEADER, source
from memos.skill_branch_validation.tasks import BranchTask


FAKE = source("""
    from copy import deepcopy

    class FakeGraph:
        def __init__(self, rows):
            self.table = {key: (owner, deepcopy(value)) for key, owner, value in rows}
            self.writes = []

        def get_node(self, memory_id, user_name=None):
            pair = self.table.get(memory_id)
            if pair is None or pair[0] != user_name:
                return None
            return deepcopy(pair[1])

        def get_user_names_by_memory_ids(self, ids):
            return {key: self.table[key][0] for key in ids if key in self.table}

        def update_node(self, memory_id, fields, user_name=None):
            pair = self.table.get(memory_id)
            if pair is None or pair[0] != user_name or not isinstance(fields, dict):
                raise TypeError("invalid scoped update")
            self.table[memory_id] = (pair[0], deepcopy(fields))
            self.writes.append((memory_id, deepcopy(fields), user_name))
""")
BROKEN = FAKE.replace("if pair is None or pair[0] != user_name:\n", "if pair is None:\n", 1)
UNSAFE = FAKE.replace("return deepcopy(pair[1])", "return None")

CALLER = source("""
    from copy import deepcopy

    def revise(graph, memory_id, tenant, text, embed):
        before = graph.get_node(memory_id, user_name=tenant)
        if before is None or before["owner"] != tenant or before["status"] != "activated":
            return False
        try:
            vector = embed(text)
        except RuntimeError:
            return False
        after = deepcopy(before)
        after["history"].append({"text": before["text"], "version": before["version"]})
        after.update(text=text, embedding=vector, version=before["version"] + 1)
        graph.update_node(memory_id, after, user_name=tenant)
        return True
""")

CONTRACT = """# MemOS scoped-read contract probe

This is an independently authored, offline MemOS-contract task, not a production API replica.
Repair fake_graph.py only. caller.py is a correct client and must remain unchanged.

FakeGraph stores multiple rows (memory_id, tenant, document). IDs are globally unique.
get_node(memory_id, user_name=tenant) returns a deep copy only when both ID and tenant
match. Missing or wrong tenant/ID returns None. Reads never write. A document has
owner, status, text, version and history. Owner metadata may disagree with the storage
tenant; the caller must still refuse that document. Tenant lookup returns only existing IDs.

revise changes only an activated, correctly owned document. It increments its version,
preserves the prior text/version in history, stores the supplied embedding, and writes
once to the same ID and tenant. Missing/wrong scope, archived rows, wrong owner metadata,
or embedding RuntimeError return False with no writes. Valid updates must still work.

Use the fixed run_visible_checks tool before/after edits. Public check names are
caller_contract, fake_scope_contract, scope_generalization, caller_with_fake,
update_regressions and embedding_failure. Preserve their real error detection.
Do not modify TASK.md, visible_checks.py or caller.py. Do not add files.
"""

VISIBLE = source("""
    from copy import deepcopy
    from caller import revise
    from fake_graph import FakeGraph

    def document(owner="red", status="activated"):
        return dict(owner=owner, status=status, text="old", version=2, history=[])

    def graph():
        return FakeGraph([("one", "red", document()), ("two", "blue", document("blue"))])

    checks = []
    def check(name, function):
        try:
            function()
            checks.append((name, True))
            print(name + ": passed")
        except Exception as error:
            checks.append((name, False))
            print(name + ": failed (" + type(error).__name__ + ")")

    def positive():
        g = graph()
        assert revise(g, "one", "red", "new", lambda _: [0.5])
        assert len(g.writes) == 1 and g.writes[0][1]["version"] == 3

    def scope():
        g = graph()
        assert g.get_node("one", user_name="blue") is None
        assert g.get_node("one") is None
        assert g.get_node("absent", user_name="red") is None
        assert g.get_node("one", user_name="red") == document()

    def generalize():
        g = graph()
        assert g.get_node("two", user_name="blue") == document("blue")
        assert g.get_user_names_by_memory_ids(["two", "absent"]) == {"two": "blue"}

    def regressions():
        g = FakeGraph([("one", "red", document(status="archived"))])
        assert not revise(g, "one", "red", "new", lambda _: [1]) and not g.writes

    def embedding():
        g = graph()
        before = deepcopy(g.table)
        def fail(_): raise RuntimeError("offline")
        assert not revise(g, "one", "red", "new", fail)
        assert g.table == before and not g.writes

    check("caller_contract", positive)
    check("fake_scope_contract", scope)
    check("scope_generalization", generalize)
    check("caller_with_fake", positive)
    check("update_regressions", regressions)
    check("embedding_failure", embedding)
    raise SystemExit(0 if all(ok for _, ok in checks) else 1)
""")

ORACLE = (
    HEADER
    + source("""
    def node(owner, status="activated", version=7):
        return {"owner": owner, "status": status, "text": "original",
                "version": version, "history": [{"text": "older", "version": version-1}]}

    class Backend:
        def __init__(self, rows):
            self.rows = deepcopy(rows)
            self.writes = []
        def get_node(self, memory_id, user_name=None):
            for key, owner, value in self.rows:
                if key == memory_id and owner == user_name:
                    return deepcopy(value)
            return None
        def update_node(self, memory_id, fields, user_name=None):
            assert isinstance(fields, dict)
            self.writes.append((memory_id, deepcopy(fields), user_name))

    def valid(graph_type):
        for index, owner in enumerate(("tenant-Z", "租户乙", "tenant/29")):
            key = "record-" + str(index + 41)
            before = node(owner)
            g = graph_type([(key, owner, before), ("other", "elsewhere", node("elsewhere"))])
            assert caller.revise(g, key, owner, "replacement", lambda text: [len(text), 1]) is True
            expected = deepcopy(before)
            expected["history"].append({"text": "original", "version": 7})
            expected.update(text="replacement", embedding=[11, 1], version=8)
            assert g.writes == [(key, expected, owner)]

    def scope():
        g = fake.FakeGraph([("left", "A", node("A")), ("right", "B", node("B"))])
        for key, tenant in (("left", None), ("left", "B"), ("right", "A"), ("missing", "A")):
            assert g.get_node(key, user_name=tenant) is None
        returned = g.get_node("left", user_name="A")
        assert returned == node("A")
        returned["history"][0]["text"] = "alias leak"
        assert g.get_node("left", user_name="A") == node("A")
        assert not g.writes

    def generalization():
        rows = [("id-"+str(i), "owner-"+str(i%3), node("owner-"+str(i%3))) for i in range(8)]
        g = fake.FakeGraph(rows)
        for key, owner, payload in rows:
            assert g.get_node(key, user_name=owner) == payload
            assert g.get_node(key, user_name=owner+"-wrong") is None
        assert g.get_user_names_by_memory_ids(["id-5", "missing", "id-1"]) == {
            "id-5": "owner-2", "id-1": "owner-1"}
        assert not g.writes

    def regressions():
        for graph_type in (Backend, fake.FakeGraph):
            for payload, tenant in ((node("T", "archived"), "T"), (node("other"), "T"), (node("T"), "wrong")):
                g = graph_type([("target", "T", payload)])
                assert caller.revise(g, "target", tenant, "no write", lambda _: [1]) is False
                assert not g.writes
            g = graph_type([("target", "T", node("T"))])
            assert caller.revise(g, "absent", "T", "no write", lambda _: [1]) is False
            assert not g.writes

    def embedding():
        for graph_type in (Backend, fake.FakeGraph):
            g = graph_type([("target", "T", node("T"))])
            def unavailable(_): raise RuntimeError("encoder unavailable")
            assert caller.revise(g, "target", "T", "new", unavailable) is False
            assert not g.writes and g.get_node("target", user_name="T") == node("T")

    check("caller_contract", lambda: valid(Backend))
    check("fake_scope_contract", scope)
    check("scope_generalization", generalization)
    check("caller_with_fake", lambda: valid(fake.FakeGraph))
    check("update_regressions", regressions)
    check("embedding_failure", embedding)
""")
    + FOOTER
)


def scoped_task() -> BranchTask:
    common = {"TASK.md": CONTRACT, "caller.py": CALLER, "visible_checks.py": VISIBLE}
    return BranchTask(
        task_id="branch-scoped-table",
        revision="1",
        lineage_id="independent-row-table-20261109",
        purpose="branch_validation",
        initial_files={**common, "fake_graph.py": BROKEN},
        reference_files={**common, "fake_graph.py": FAKE},
        unsafe_files={**common, "fake_graph.py": UNSAFE},
        editable_paths=("fake_graph.py",),
        oracle=ORACLE,
        required_checks=(
            "caller_contract",
            "fake_scope_contract",
            "scope_generalization",
            "caller_with_fake",
            "update_regressions",
            "embedding_failure",
        ),
        expected_failures={
            "initial": ("fake_scope_contract", "scope_generalization"),
            "reference": (),
            "unsafe": (
                "fake_scope_contract",
                "scope_generalization",
                "caller_with_fake",
                "embedding_failure",
            ),
        },
        target_branches=("confirm_contract#3", "repair_scoped_reads#0", "verify_scoped_reads#2"),
        provenance={
            "kind": "independently_authored_contract_probe",
            "source_code_copied": False,
            "contract_overlap": "MemOS ID-plus-tenant read, copy isolation and failed-read no-write contract",
            "structural_difference": "Multi-row ID table and an independent revision client; no production handler or source patch copied",
            "limitations": "Same published API semantics; not a random sample or full production handler reproduction",
        },
    )
