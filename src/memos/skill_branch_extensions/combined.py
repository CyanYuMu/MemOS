"""One shared state backend exposes independently observable read and write defects."""

from memos.skill_branch_extensions.tasks import ExtensionSuite
from memos.skill_branch_validation.harness import FOOTER, HEADER, source


FAKE = source("""
    from copy import deepcopy

    class FakeGraphDB:
        def __init__(self, rows=()):
            self.docs = {key: {"tenant": tenant, "fields": deepcopy(fields)}
                         for key, tenant, fields in rows}
            self.writes = []

        def get_node(self, memory_id, user_name=None):
            item = self.docs.get(memory_id)
            if item is None or item["tenant"] != user_name:
                return None
            return deepcopy(item["fields"])

        def get_user_names_by_memory_ids(self, ids):
            return {key: self.docs[key]["tenant"] for key in ids if key in self.docs}

        def update_node(self, id, fields, user_name=None):
            if not isinstance(fields, dict):
                raise TypeError("dictionary fields required")
            current = self.docs.get(id)
            if current is not None and current["tenant"] != user_name:
                raise TypeError("tenant mismatch")
            item = self.docs.setdefault(id, {"tenant": user_name, "fields": {}})
            item["fields"].update(deepcopy(fields))
            self.writes.append(("update", id, deepcopy(fields), user_name))

        def add_node(self, id, fields, user_name=None):
            if not isinstance(fields, dict) or id in self.docs:
                raise TypeError("invalid create")
            self.docs[id] = {"tenant": user_name, "fields": deepcopy(fields)}
            self.writes.append(("add", id, deepcopy(fields), user_name))
""")
READ_BROKEN = FAKE.replace('if item is None or item["tenant"] != user_name:', "if item is None:")
WRITE_GUARD = (
    'if not isinstance(fields, dict):\n            raise TypeError("dictionary fields required")'
)
WRITE_BROKEN = FAKE.replace(WRITE_GUARD, "fields = dict(fields)", 1)
ALL_BROKEN = READ_BROKEN.replace(WRITE_GUARD, "fields = dict(fields)", 1)
EMPTY_READ = FAKE.replace('return deepcopy(item["fields"])', "return None", 1)
WIDE_ARGUMENTS = FAKE.replace(
    "def update_node(self, id, fields, user_name=None):\n        " + WRITE_GUARD,
    "def update_node(self, id, *values, user_name=None):\n        fields = next((v for v in values if isinstance(v, dict)), {})",
    1,
)

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

    def apply_action(graph, action, cube, enabled=True):
        if not enabled or action.get("confidence", 1) <= 0:
            return False
        kind, target, content = action["kind"], action.get("target"), action.get("content")
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

CONTRACT = """# Combined MemOS contract task

This is one new combined offline scenario derived from the two existing contract probes,
not an additional independent support sample or the production persistence/API.
Repair caller.py and fake_graph.py. Do not add files or modify TASK.md or visible_checks.py.
The same FakeGraphDB state and write journal are shared by two business entry points.

IDs are globally unique. docs[id] contains tenant and fields. get_node(id, user_name)
returns a deep copy only if BOTH ID and tenant match; missing/wrong ID/tenant returns None.
get_user_names_by_memory_ids returns only existing IDs. Reads never write.
revise only changes activated documents whose owner matches the tenant. It keeps prior
text/version in history, increments version once, stores the supplied embedding and
writes once to the same ID/tenant. Missing scope, wrong owner, archived documents or
embedding RuntimeError return False without writes. Normal updates must remain usable.

The independent Dream UPDATE contract also applies. update_node(id, fields, user_name=None)
accepts ONLY dictionary fields. Lists, strings, None, integers and extra positional content
raise TypeError before any docs/writes change. A valid call merges a deep copy of fields,
preserves other fields and appends ('update', id, fields, tenant). Existing nodes cannot
change tenant. add_node creates a new ID and appends ('add', id, fields, tenant).

apply_action UPDATE passes dictionary fields with dream_action_type='update', last_action,
and nonempty content as memory; empty/None content preserves memory. CREATE adds
dream-{action_id}; ARCHIVE marks target archived; MERGE creates merged_from and archives
its source IDs with merged_into. Disabled storage, confidence<=0, missing target/new
content, empty merge sources and unsupported actions return False with no writes.

Checks are grouped to avoid duplicate names. The scoped group contains caller_contract,
fake_scope_contract, scope_generalization, caller_with_fake, update_regressions,
embedding_failure. The dream group contains caller_contract, fake_contract,
caller_with_fake, other_actions, no_unintended_write. IDs are group.name; the suffix is
the original Skill check name, but the group is essential. Each named check starts with
fresh state; explicit cross-entry checks use the same state inside that one check.

Run all fixed visible checks before and after edits. Both groups must pass in the final
state. Preserve true error detection and report actual group results, including residual
failures. Do not claim an intermediate state that was not observed or infer hidden results.
"""

CHECKS = tuple(
    {"id": group + "." + name, "group": group, "ruleCheck": name}
    for group, names in (
        (
            "scoped",
            (
                "caller_contract",
                "fake_scope_contract",
                "scope_generalization",
                "caller_with_fake",
                "update_regressions",
                "embedding_failure",
            ),
        ),
        (
            "dream",
            (
                "caller_contract",
                "fake_contract",
                "caller_with_fake",
                "other_actions",
                "no_unintended_write",
            ),
        ),
    )
    for name in names
)

# The public program is exported as a protected file. The hidden oracle below
# owns different inputs and additional checks, never materialized to the Agent.
PUBLIC = (
    HEADER
    + source("""
    def doc(owner="alpha", status="activated"):
        return {"owner": owner, "status": status, "text": "old", "version": 2, "history": [], "memory": "kept"}

    class StrictBackend:
        def __init__(self, rows=()):
            self.docs = {key: {"tenant": tenant, "fields": deepcopy(fields)} for key, tenant, fields in rows}
            self.writes = []
        def get_node(self, key, user_name=None):
            row = self.docs.get(key)
            return deepcopy(row["fields"]) if row and row["tenant"] == user_name else None
        def update_node(self, id, fields, user_name=None):
            if not isinstance(fields, dict): raise TypeError("dict required")
            row = self.docs.get(id)
            if row and row["tenant"] != user_name: raise TypeError("scope")
            self.docs.setdefault(id, {"tenant": user_name, "fields": {}})["fields"].update(deepcopy(fields))
            self.writes.append(("update", id, deepcopy(fields), user_name))

    def scoped_positive(kind):
        g = kind([("p1", "alpha", doc())])
        assert caller.revise(g, "p1", "alpha", "new", lambda _: [0.5]) is True
        expected = doc()
        expected.update(text="new", version=3, history=[{"text":"old", "version":2}], embedding=[0.5])
        assert g.writes == [("update", "p1", expected, "alpha")]
        assert g.docs == {"p1":{"tenant":"alpha", "fields":expected}}

    def scoped_guard():
        g = fake.FakeGraphDB([("p1", "alpha", doc())])
        assert g.get_node("p1", user_name="beta") is None
        assert g.get_node("p1") is None and g.get_node("missing", user_name="alpha") is None
        assert g.get_node("p1", user_name="alpha") == doc()
        assert not g.writes

    def scoped_many():
        g = fake.FakeGraphDB([("p1", "alpha", doc()), ("p2", "beta", doc("beta"))])
        assert g.get_node("p2", user_name="beta") == doc("beta")
        assert g.get_node("p2", user_name="alpha") is None
        assert g.get_user_names_by_memory_ids(["p2", "unknown"]) == {"p2":"beta"}

    def scoped_regressions():
        for fields in (doc(status="archived"), doc(owner="someone")):
            g = fake.FakeGraphDB([("p1", "alpha", fields)])
            before = deepcopy(g.docs)
            assert caller.revise(g, "p1", "alpha", "bad", lambda _: [1]) is False
            assert g.docs == before and not g.writes

    def scoped_embedding():
        g = fake.FakeGraphDB([("p1", "alpha", doc())])
        before = deepcopy(g.docs)
        def offline(_): raise RuntimeError("offline")
        assert caller.revise(g, "p1", "alpha", "bad", offline) is False
        assert g.docs == before and not g.writes

    def dream_positive(kind):
        g = kind([("d1", "alpha", doc())])
        item = {"kind":"update", "target":"d1", "content":"next", "action_id":"pub"}
        assert caller.apply_action(g, item, "alpha") is True
        fields = {"dream_action_type":"update", "last_action":"pub", "memory":"next"}
        assert g.writes == [("update", "d1", fields, "alpha")]
        expected = doc()
        expected.update(fields)
        assert g.docs == {"d1":{"tenant":"alpha", "fields":expected}}

    def dream_rejection():
        g = fake.FakeGraphDB([("d1", "alpha", doc())])
        values = {"nested":{"items":[1,3]}}
        g.update_node("d1",values,user_name="alpha")
        values["nested"]["items"].append(9)
        expected = doc()
        expected["nested"] = {"items":[1,3]}
        assert g.docs == {"d1":{"tenant":"alpha", "fields":expected}}
        assert g.writes == [("update","d1",{"nested":{"items":[1,3]}},"alpha")]
        before = deepcopy((g.docs, g.writes))
        rejects(g.update_node, "d1", [("memory","bad")], user_name="alpha")
        assert (g.docs, g.writes) == before
        rejects(g.update_node, "d1", "extra", {"memory":"bad"}, user_name="alpha")
        assert (g.docs, g.writes) == before

    def dream_other():
        g = fake.FakeGraphDB([("d1", "alpha", doc())])
        assert caller.apply_action(g, {"kind":"archive", "target":"d1", "action_id":"ar"}, "alpha")
        before = deepcopy((g.docs, g.writes))
        # Cross-entry observation: Dream archive must prevent a later scoped edit.
        assert caller.revise(g, "d1", "alpha", "bad", lambda _: [1]) is False
        assert (g.docs, g.writes) == before
        assert caller.apply_action(g, {"kind":"create", "content":"born", "action_id":"cr"}, "alpha")
        assert caller.apply_action(g, {"kind":"merge", "content":"merged", "sources":["d1","dream-cr"], "action_id":"mg"}, "alpha")
        archived = doc()
        archived.update(status="archived",merged_into="dream-mg")
        merged = {"memory":"merged","status":"activated","merged_from":["d1","dream-cr"]}
        assert g.docs == {
            "d1":{"tenant":"alpha","fields":archived},
            "dream-cr":{"tenant":"alpha","fields":{"memory":"born","status":"archived","merged_into":"dream-mg"}},
            "dream-mg":{"tenant":"alpha","fields":merged},
        }
        assert g.writes[-3:] == [("add","dream-mg",merged,"alpha"),
            ("update","d1",{"status":"archived","merged_into":"dream-mg"},"alpha"),
            ("update","dream-cr",{"status":"archived","merged_into":"dream-mg"},"alpha")]

    def dream_denied():
        g = fake.FakeGraphDB([("d1", "alpha", doc())])
        before = deepcopy((g.docs, g.writes))
        assert caller.apply_action(g, {"kind":"update", "action_id":"no"}, "alpha") is False
        rejects(g.update_node, "d1", {"memory":"bad"}, user_name="wrong")
        assert (g.docs, g.writes) == before

    check("scoped.caller_contract", lambda: scoped_positive(StrictBackend))
    check("scoped.fake_scope_contract", scoped_guard)
    check("scoped.scope_generalization", scoped_many)
    check("scoped.caller_with_fake", lambda: scoped_positive(fake.FakeGraphDB))
    check("scoped.update_regressions", scoped_regressions)
    check("scoped.embedding_failure", scoped_embedding)
    check("dream.caller_contract", lambda: dream_positive(StrictBackend))
    check("dream.fake_contract", dream_rejection)
    check("dream.caller_with_fake", lambda: dream_positive(fake.FakeGraphDB))
    check("dream.other_actions", dream_other)
    check("dream.no_unintended_write", dream_denied)
""")
    + FOOTER
)

ORACLE = (
    HEADER
    + source("""
    def record(owner, status="activated"):
        return {"owner":owner, "status":status, "text":"previous", "version":8,
                "history":[{"text":"older", "version":7}], "memory":"preserve", "extra":{"nested":[1]}}

    class ReferenceBackend:
        def __init__(self, rows=()):
            self.rows = deepcopy(list(rows))
            self.writes = []
        def get_node(self, key, user_name=None):
            for id, tenant, fields in self.rows:
                if id == key and tenant == user_name: return deepcopy(fields)
            return None
        def update_node(self, id, fields, user_name=None):
            if type(fields) is not dict: raise TypeError("strict dict")
            self.writes.append(("update", id, deepcopy(fields), user_name))
        def add_node(self, id, fields, user_name=None):
            self.writes.append(("add", id, deepcopy(fields), user_name))

    def valid_reads(kind):
        for index, tenant in enumerate(("hidden-A", "隐藏乙", "tenant/5")):
            key = "h" + str(83 + index)
            original = record(tenant)
            g = kind([(key,tenant,original), ("h91","other",record("other"))])
            assert caller.revise(g,key,tenant,"changed",lambda value:[len(value),2]) is True
            expected = deepcopy(original)
            expected["history"].append({"text":"previous", "version":8})
            expected.update(text="changed",version=9,embedding=[7,2])
            assert g.writes == [("update",key,expected,tenant)]
            if kind is fake.FakeGraphDB:
                assert g.docs == {key:{"tenant":tenant,"fields":expected},
                                  "h91":{"tenant":"other","fields":record("other")}}

    def guard_reads():
        g = fake.FakeGraphDB([("h83","hidden-A",record("hidden-A")),("h91","隐藏乙",record("隐藏乙"))])
        before = deepcopy((g.docs,g.writes))
        for key,tenant in (("h83",None),("h83","隐藏乙"),("absent","hidden-A"),(None,"hidden-A")):
            assert g.get_node(key,user_name=tenant) is None
        value = g.get_node("h91",user_name="隐藏乙")
        assert value == record("隐藏乙")
        value["extra"]["nested"].append(99)
        assert (g.docs,g.writes) == before

    def many_reads():
        rows = [("hidden-"+str(i),"tenant-"+str(i%4),record("tenant-"+str(i%4))) for i in range(9)]
        g = fake.FakeGraphDB(rows)
        for key,tenant,value in rows:
            assert g.get_node(key,user_name=tenant) == value
            assert g.get_node(key,user_name=tenant+"!") is None
        assert g.get_user_names_by_memory_ids(["hidden-7","missing","hidden-0"]) == {"hidden-7":"tenant-3","hidden-0":"tenant-0"}
        assert not g.writes

    def regressions():
        for kind in (ReferenceBackend,fake.FakeGraphDB):
            for fields,scope in ((record("hidden-A","archived"),"hidden-A"),(record("wrong"),"hidden-A"),(record("hidden-A"),"other")):
                g = kind([("h83","hidden-A",fields)])
                assert caller.revise(g,"h83",scope,"no",lambda _:[1]) is False
                assert not g.writes
            g = kind([("h83","hidden-A",record("hidden-A"))])
            assert caller.revise(g,"missing","hidden-A","no",lambda _:[1]) is False
            assert not g.writes

    def embedding_failure():
        for kind in (ReferenceBackend,fake.FakeGraphDB):
            g = kind([("h83","hidden-A",record("hidden-A"))])
            def fail(_): raise RuntimeError("unavailable")
            assert caller.revise(g,"h83","hidden-A","no",fail) is False
            assert not g.writes
            if kind is fake.FakeGraphDB:
                assert g.docs["h83"]["fields"] == record("hidden-A")

    def item(kind, **values):
        return {"kind":kind,"action_id":"hidden31",**values}

    def valid_updates(kind):
        for content in ("new hidden text","",None):
            g = kind([("h83","hidden-A",record("hidden-A"))])
            assert caller.apply_action(g,item("update",target="h83",content=content),"hidden-A") is True
            fields = {"dream_action_type":"update","last_action":"hidden31"}
            if content: fields["memory"] = content
            assert g.writes == [("update","h83",fields,"hidden-A")]
            if kind is fake.FakeGraphDB:
                expected = record("hidden-A")
                expected.update(fields)
                assert g.docs == {"h83":{"tenant":"hidden-A","fields":expected}}

    def rejection():
        g = fake.FakeGraphDB([("h83","hidden-A",record("hidden-A"))])
        values = {"details":{"nested":[2,4]}}
        g.update_node("h83",values,user_name="hidden-A")
        values["details"]["nested"].append(9)
        assert g.writes == [("update","h83",{"details":{"nested":[2,4]}},"hidden-A")]
        expected = record("hidden-A")
        expected["details"] = {"nested":[2,4]}
        assert g.docs == {"h83":{"tenant":"hidden-A","fields":expected}}
        before = deepcopy((g.docs,g.writes))
        for invalid in ([("memory","bad")],"text",None,17):
            rejects(g.update_node,"h83",invalid,user_name="hidden-A")
            assert (g.docs,g.writes) == before
        rejects(g.update_node,"h83","content",{"memory":"bad"},user_name="hidden-A")
        assert (g.docs,g.writes) == before

    def other_actions():
        for kind in (ReferenceBackend,fake.FakeGraphDB):
            g = kind([("h83","hidden-A",record("hidden-A")),("h91","hidden-A",record("hidden-A"))])
            assert caller.apply_action(g,item("merge",sources=["h83","h91"],content="merged"),"hidden-A") is True
            assert g.writes == [("add","dream-hidden31",{"memory":"merged","status":"activated","merged_from":["h83","h91"]},"hidden-A"),
                ("update","h83",{"status":"archived","merged_into":"dream-hidden31"},"hidden-A"),
                ("update","h91",{"status":"archived","merged_into":"dream-hidden31"},"hidden-A")]
            if kind is fake.FakeGraphDB:
                archived = record("hidden-A")
                archived.update(status="archived",merged_into="dream-hidden31")
                assert g.docs == {
                    "h83":{"tenant":"hidden-A","fields":archived},
                    "h91":{"tenant":"hidden-A","fields":archived},
                    "dream-hidden31":{"tenant":"hidden-A","fields":{"memory":"merged","status":"activated","merged_from":["h83","h91"]}},
                }
        g = fake.FakeGraphDB([("h83","hidden-A",record("hidden-A"))])
        assert caller.apply_action(g,item("archive",target="h83"),"hidden-A") is True
        before = deepcopy((g.docs,g.writes))
        assert caller.revise(g,"h83","hidden-A","illegal after archive",lambda _:[1]) is False
        assert (g.docs,g.writes) == before
        g = fake.FakeGraphDB()
        assert caller.apply_action(g,item("create",content="born"),"隐藏乙") is True
        assert g.docs == {"dream-hidden31":{"tenant":"隐藏乙","fields":{"memory":"born","status":"activated"}}}

    def denied_actions():
        for kind in (ReferenceBackend,fake.FakeGraphDB):
            for action in (item("update"),item("archive"),item("create"),item("merge",content="bad"),item("unknown"),item("update",target="h83",confidence=0)):
                g = kind([("h83","hidden-A",record("hidden-A"))])
                assert caller.apply_action(g,action,"hidden-A") is False
                assert not g.writes
            g = kind()
            assert caller.apply_action(g,item("create",content="disabled"),"hidden-A",enabled=False) is False
            assert not g.writes
        g = fake.FakeGraphDB([("h83","hidden-A",record("hidden-A"))])
        before = deepcopy((g.docs,g.writes))
        rejects(g.update_node,"h83",{"memory":"intrude"},user_name="other")
        assert (g.docs,g.writes) == before

    check("scoped.caller_contract",lambda:valid_reads(ReferenceBackend))
    check("scoped.fake_scope_contract",guard_reads)
    check("scoped.scope_generalization",many_reads)
    check("scoped.caller_with_fake",lambda:valid_reads(fake.FakeGraphDB))
    check("scoped.update_regressions",regressions)
    check("scoped.embedding_failure",embedding_failure)
    check("dream.caller_contract",lambda:valid_updates(ReferenceBackend))
    check("dream.fake_contract",rejection)
    check("dream.caller_with_fake",lambda:valid_updates(fake.FakeGraphDB))
    check("dream.other_actions",other_actions)
    check("dream.no_unintended_write",denied_actions)
""")
    + FOOTER
)


def combined_suite() -> ExtensionSuite:
    common = {"TASK.md": CONTRACT, "visible_checks.py": PUBLIC}

    def files(caller, fake):
        return {**common, "caller.py": caller, "fake_graph.py": fake}

    variants = {
        "initial": files(BROKEN_CALLER, ALL_BROKEN),
        "scoped_only": files(BROKEN_CALLER, WRITE_BROKEN),
        "dream_only": files(CALLER, READ_BROKEN),
        "reference": files(CALLER, FAKE),
        "unsafe_empty_read": files(CALLER, EMPTY_READ),
        "unsafe_update_arguments": files(CALLER, WIDE_ARGUMENTS),
    }
    read_failures = ("scoped.fake_scope_contract", "scoped.scope_generalization")
    write_failures = ("dream.caller_contract", "dream.fake_contract")
    expected = {
        "initial": read_failures + write_failures,
        "scoped_only": write_failures,
        "dream_only": read_failures,
        "reference": (),
        "unsafe_empty_read": (*read_failures, "scoped.caller_with_fake"),
        "unsafe_update_arguments": ("dream.fake_contract",),
    }
    return ExtensionSuite(
        suite_id="combined-c1-v1",
        task_id="branch-combined-state",
        revision="1",
        lineage_id="combined-shared-state-20261109",
        initial_files=variants["initial"],
        variants=variants,
        editable_paths=("caller.py", "fake_graph.py"),
        checks=CHECKS,
        oracle=ORACLE,
        public_program=PUBLIC,
        expected_failures={
            name: {"public": failed, "hidden": failed} for name, failed in expected.items()
        },
        target_branches=(
            "confirm_contract#3",
            "repair_scoped_reads#0",
            "verify_scoped_reads#0",
            "repair_dream_update#0",
            "verify_dream_update#0",
        ),
        provenance={
            "kind": "derived_contract_composition",
            "sourceMemoryConsumed": False,
            "derivedFromTasks": ["branch-scoped-table", "branch-dream-journal"],
            "composition": "shared state and grouped checks derived from the two core probes",
            "independentSourceSamples": 0,
            "scenario": "simultaneous_read_and_update_defects",
            "limitations": [
                "offline controlled state; not a production API replica",
                "asset qualification grants no Agent or branch evidence",
            ],
        },
    )
