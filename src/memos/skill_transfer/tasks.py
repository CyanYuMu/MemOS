"""Two new caller contexts for the existing Dream UPDATE argument contract."""

from pathlib import Path
from textwrap import dedent

from memos.skill_evaluation.cases import CaseSpec
from memos.skill_evaluation.tasks import case_digest as case_digest


TASKS = ("transfer-batch-update", "transfer-version-update")
FAKE = dedent("""\
class FakeGraph:
    def __init__(self):
        self.updated = []
    def update_node(self, id, fields, *, user_name):
        if not isinstance(fields, dict):
            raise TypeError("fields must be a dict")
        self.updated.append((id, dict(fields), user_name))
""")
CALLERS = {
    TASKS[0]: dedent("""\
        def persist(graph, plan, cube):
            done = []
            for item in plan:
                if item["action"] != "UPDATE" or item.get("new_content") is None:
                    continue
                fields = {"memory": item["new_content"], "status": "activated"}
                graph.update_node(item["id"], item["new_content"], fields, user_name=cube)
                done.append(item["id"])
            return done
        """),
    TASKS[1]: dedent("""\
        def persist(graph, item, cube, current_version):
            if item["action"] != "UPDATE" or item.get("new_content") is None:
                return False
            if item["expected_version"] != current_version:
                return False
            fields = {"memory": item["new_content"], "version": current_version + 1}
            graph.update_node(item["id"], item["new_content"], fields, user_name=cube)
            return True
        """),
}


def program(task_id, *, hidden):
    """Visible smoke checks and separately fixed host acceptance share the contract."""
    setup = """\
import importlib.util, json, sys
from pathlib import Path
root = Path(sys.argv[1])
def load(name):
    spec = importlib.util.spec_from_file_location(name, root / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
caller, fake = load("caller"), load("fake_graph")
checks = []
def check(name, fn):
    try:
        fn()
        checks.append({"name": name, "passed": True})
    except Exception as error:
        checks.append({"name": name, "passed": False, "error": type(error).__name__})
def eq(left, right):
    assert left == right, (left, right)
"""
    if task_id == TASKS[0]:
        checks = """\
def update():
    graph = fake.FakeGraph()
    plan = [{"id": "one", "action": "UPDATE", "new_content": "text"}]
    eq(caller.persist(graph, plan, "cube-a"), ["one"])
    eq(graph.updated, [("one", {"memory": "text", "status": "activated"}, "cube-a")])
check("update_contract", update)
"""
        extended = """\
def mixed():
    graph = fake.FakeGraph()
    plan = [{"id": "skip", "action": "ADD", "new_content": "unused"},
            {"id": "none", "action": "UPDATE", "new_content": None},
            {"id": "empty", "action": "UPDATE", "new_content": ""},
            {"id": "unicode", "action": "UPDATE", "new_content": "中文\\n下一行"}]
    eq(caller.persist(graph, plan, "cube-b"), ["empty", "unicode"])
    eq(graph.updated, [("empty", {"memory": "", "status": "activated"}, "cube-b"),
                       ("unicode", {"memory": "中文\\n下一行", "status": "activated"}, "cube-b")])
    eq(caller.persist(graph, [], "cube-c"), [])
check("mixed_actions_order_empty_content", mixed)
"""
    else:
        checks = """\
def update():
    graph = fake.FakeGraph()
    item = {"id": "one", "action": "UPDATE", "new_content": "text", "expected_version": 4}
    eq(caller.persist(graph, item, "cube-a", 4), True)
    eq(graph.updated, [("one", {"memory": "text", "version": 5}, "cube-a")])
check("update_contract", update)
"""
        extended = """\
def guards():
    graph = fake.FakeGraph()
    base = {"id": "one", "action": "UPDATE", "new_content": "", "expected_version": 0}
    eq(caller.persist(graph, base, "cube-b", 0), True)
    eq(graph.updated, [("one", {"memory": "", "version": 1}, "cube-b")])
    before = list(graph.updated)
    for changes in ({"expected_version": 3}, {"new_content": None}, {"action": "DELETE"}):
        eq(caller.persist(graph, {**base, **changes}, "cube-c", 0), False)
        eq(graph.updated, before)
check("version_other_actions_no_write", guards)
"""
    negative = """\
def invalid():
    graph = fake.FakeGraph()
    for args in (("id", "content"), ("id", "content", {})):
        try:
            graph.update_node(*args, user_name="cube")
        except TypeError:
            pass
        else:
            raise AssertionError("invalid signature was accepted")
        eq(graph.updated, [])
check("invalid_parameters_no_write", invalid)
"""
    end = """\
Path(sys.argv[2]).write_text(json.dumps({"checks": checks}))
print(json.dumps({"checks": checks}))
raise SystemExit(0 if all(c["passed"] for c in checks) else 1)
"""
    return setup + checks + (extended + negative if hidden else "") + end


def get_case(task_id):
    if task_id not in TASKS:
        raise ValueError("unknown_transfer_task")
    contract = (
        "Repair the Dream UPDATE caller in caller.py. The authoritative signature is "
        "update_node(id, fields, user_name=cube), with dictionary fields and keyword tenant. "
        "Only caller.py is editable. Preserve other actions and None-content no-write behavior; "
        "empty string IS content. Preserve batch order or the optimistic version guard as applicable. "
        "Do not change the fake or checks, skip valid writes, or broaden the signature. "
        "Use the visible-check tool; the host also checks separate inputs after completion.\n"
    )
    initial = {
        "caller.py": CALLERS[task_id],
        "fake_graph.py": FAKE,
        "TASK.md": contract,
        "visible_checks.py": program(task_id, hidden=False),
    }
    correct = CALLERS[task_id].replace(
        'item["id"], item["new_content"], fields', 'item["id"], fields'
    )
    reference = {**initial, "caller.py": correct}
    unsafe = {
        **reference,
        "caller.py": correct.replace(
            'graph.update_node(item["id"], fields, user_name=cube)', "pass"
        ),
    }
    return CaseSpec(
        case_id=task_id,
        revision="1",
        split="holdout",
        lineage_id=task_id + "-v1",
        description="Same-contract Dream UPDATE transfer instance",
        contract=contract,
        initial_files=initial,
        reference_files=reference,
        unsafe_files=unsafe,
        oracle_source=program(task_id, hidden=True),
        required_checks=("update_contract",),
        expected_failure_checks={},
        editable_paths=("caller.py",),
        provenance={
            "kind": "new_same_contract_instance",
            "family": "dream-update-extra-positional",
            "developer_blind": False,
            "new_problem_family": False,
        },
    )


def materialize_case(task_id, destination, variant="initial"):
    case = get_case(task_id)
    files = dict(case.initial_files if variant == "initial" else case.reference_files)
    if variant in {"skip", "unsafe"}:
        files = case.unsafe_files
    elif variant == "lenient":
        files["fake_graph.py"] = FAKE.replace(
            "def update_node(self, id, fields, *, user_name):",
            "def update_node(self, id, fields, *extra, user_name):",
        )
    elif variant == "weaken_checks":
        files["visible_checks.py"] = "pass\n"
    elif variant not in {"initial", "reference"}:
        raise ValueError("unknown_transfer_variant")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        (destination / name).write_text(content)
    return destination
