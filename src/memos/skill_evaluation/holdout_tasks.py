"""Independently authored transfer tasks, available only to trusted evaluation tooling.

These task assets and reference outcomes are verifier self-tests. They do not
constitute Agent runs, qualified source experience, or evidence of Skill benefit.
"""

from __future__ import annotations

import json

from textwrap import dedent

from memos.skill_evaluation.cases import CaseSpec


HOLDOUT_CASE_IDS = (
    "holdout-record",
    "holdout-deadline",
    "holdout-migration",
    "holdout-fixture",
    "holdout-discount",
    "holdout-ambiguous",
    "holdout-binding",
)


def _s(value: str) -> str:
    return dedent(value).lstrip("\n")


_ORACLE_HEADER = _s("""
    import importlib
    import json
    import sys
    from pathlib import Path

    root = Path(sys.argv[1]).resolve(strict=True)
    report_path = Path(sys.argv[2]).resolve()
    checks = []
    imported_files = {}

    def call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception as exc:
            raise AssertionError(f"candidate raised {type(exc).__name__}: {exc}") from exc

    def rejects(exception_type, function, *args, **kwargs):
        try:
            function(*args, **kwargs)
        except exception_type:
            return
        except Exception as exc:
            raise AssertionError(f"wrong rejection {type(exc).__name__}: {exc}") from exc
        raise AssertionError("invalid call was accepted")

    def check(name, function):
        try:
            function()
        except (AssertionError, AttributeError, TypeError, KeyError) as exc:
            checks.append({"name": name, "status": "failed", "detail": str(exc)})
        except Exception as exc:
            checks.append({"name": name, "status": "error",
                           "detail": f"{type(exc).__name__}: {exc}"})
        else:
            checks.append({"name": name, "status": "passed", "detail": "contract satisfied"})

    def load_candidate(modules):
        sys.path.insert(0, str(root))
        loaded = []
        for name, relative in modules.items():
            expected = (root / relative).resolve(strict=True)
            if not expected.is_relative_to(root):
                raise ImportError("module escapes candidate")
            module = importlib.import_module(name)
            actual = Path(module.__file__).resolve(strict=True)
            if actual != expected:
                raise ImportError(f"wrong module origin: {actual}")
            imported_files[name] = str(actual)
            loaded.append(module)
        return loaded
""")
_ORACLE_FOOTER = _s("""
    try:
        verify()
    except Exception as exc:
        checks.append({"name": "imports", "status": "error",
                       "detail": f"{type(exc).__name__}: {exc}"})
    report_path.write_text(json.dumps({"checks": checks, "imported_files": imported_files},
                                     indent=2), encoding="utf-8")
    states = {check["status"] for check in checks}
    sys.exit(2 if "error" in states else 1 if "failed" in states else 0)
""")
_VISIBLE_HEADER = _s("""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import caller
    import fake_graph
""")


_FIXTURE_RUNNER = _s("""
    import os
    import sys

    sys.path.insert(0, sys.argv[1])
    import pytest
    import conftest
    from types import ModuleType

    if any(name.startswith("pytest_") for name in vars(conftest)):
        print("Only the graph fixture is supported; pytest execution hooks are prohibited")
        sys.exit(3)
    fixture_proxy = ModuleType("trusted_fixture_proxy")
    fixture_proxy.graph = conftest.graph
    events = []

    class ExecutionAudit:
        def pytest_runtest_logreport(self, report):
            events.append((report.nodeid.rsplit("::", 1)[-1], report.when,
                           report.outcome, bool(getattr(report, "wasxfail", False))))

    code = pytest.main(
        ["-q", "-p", "no:cacheprovider", "-c", os.devnull, "--noconftest", *sys.argv[2:]],
        plugins=[fixture_proxy, ExecutionAudit()],
    )
    if code not in (0, 1):
        sys.exit(2)
    expected = [(item.rsplit("::", 1)[-1], phase)
                for item in sys.argv[2:] for phase in ("setup", "call", "teardown")]
    if ([(name, phase) for name, phase, outcome, xfail in events] != expected
            or any(outcome == "skipped" or xfail for name, phase, outcome, xfail in events)
            or (code == 0 and any(outcome != "passed" for _, _, outcome, _ in events))):
        print("External pytest execution is incomplete, skipped, or expected-failed")
        sys.exit(3)
    sys.exit(code)
""")


def _task(description: str, contract: str, editable: tuple[str, ...]) -> str:
    return (
        f"# {description}\n\n{contract}\n\n"
        f"允许修改：{', '.join(editable)}。保留任务说明和现有可见检查。"
        "先运行 python -B visible_checks.py 观察现象，再提交修复或具体阻塞说明。"
        "测试通过必须保留合法操作与真实错误检测。\n"
    )


def _files(caller: str, fake: str, visible: str, task: str, **extra: str) -> dict[str, str]:
    return {
        "caller.py": caller,
        "fake_graph.py": fake,
        "visible_checks.py": _VISIBLE_HEADER + visible,
        "TASK.md": task,
        **extra,
    }


def _spec(
    *,
    case_id: str,
    scenario: str,
    lineage: str,
    description: str,
    contract: str,
    initial: dict[str, str],
    reference: dict[str, str],
    unsafe: dict[str, str],
    oracle: str,
    checks: tuple[str, ...],
    failures: dict[str, tuple[str, ...]],
    editable: tuple[str, ...] = ("caller.py", "fake_graph.py"),
    assessment: str = "repair",
    rubric: tuple[str, ...] = (),
    imports: dict[str, str] | None = None,
) -> CaseSpec:
    return CaseSpec(
        case_id=case_id,
        revision="1",
        split="holdout",
        lineage_id=lineage,
        description=description,
        contract=contract,
        initial_files=initial,
        reference_files=reference,
        unsafe_files=unsafe,
        oracle_source=_ORACLE_HEADER + oracle + _ORACLE_FOOTER,
        required_checks=checks,
        expected_failure_checks=failures,
        imported_modules=imports or {"caller": "caller.py", "fake_graph": "fake_graph.py"},
        editable_paths=editable,
        assessment_kind=assessment,
        manual_rubric=rubric,
        provenance={
            "origin": "independently_authored_transfer_task",
            "scenario": scenario,
            "independence_basis": (
                "独立编写业务组件、接口合同及缺陷；未复制 S1/S2 生产路径、故障注入或补丁。"
                "与其他验证任务仅共享执行框架，不共享任务代码或故障谱系。"
            ),
            "baseline_kind": "manually_defined_contract_fixture",
            "historical_bug_claim": False,
        },
    )


def _record_case() -> CaseSpec:
    description = "修复图目录条目摘要的测试失败"
    contract = (
        "Graph.lookup(key) 对存在条目返回具有 key、label 属性的 Record（不是字典），"
        "不存在时返回 None。目录固定为 n1=map、n2=atlas。FakeGraph 必须保留该目录。"
        "caller.summarize(graph, key) 返回大写 label，不存在则返回 missing。"
    )
    editable = ("caller.py", "fake_graph.py")
    task = _task(description, contract, editable)
    caller = _s("""
        def summarize(graph, key):
            record = graph.lookup(key)
            return record.label.upper() if record is not None else "missing"
    """)
    fake = _s("""
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class Record:
            key: str
            label: str

        class FakeGraph:
            def lookup(self, key):
                label = {"n1": "map", "n2": "atlas"}.get(key)
                return Record(key, label) if label is not None else None
    """)
    broken = fake.replace("Record(key, label)", '{"key": key, "label": label}')
    visible = 'assert caller.summarize(fake_graph.FakeGraph(), "n1") == "MAP"\n'
    oracle = _s("""
        def verify():
            caller, fake = load_candidate({"caller": "caller.py", "fake_graph": "fake_graph.py"})

            class Record:
                def __init__(self, key, label):
                    self.key, self.label = key, label

            class IndependentCatalog:
                def lookup(self, key):
                    return Record(key, "gazetteer") if key == "n9" else None

            def caller_contract():
                graph = IndependentCatalog()
                assert call(caller.summarize, graph, "n9") == "GAZETTEER"
                assert call(caller.summarize, graph, "unknown") == "missing"

            def fake_contract():
                graph = call(fake.FakeGraph)
                for key, label in (("n1", "map"), ("n2", "atlas")):
                    record = call(graph.lookup, key)
                    assert not isinstance(record, dict)
                    assert (record.key, record.label) == (key, label)
                assert call(graph.lookup, "unknown") is None

            def integrated():
                graph = call(fake.FakeGraph)
                assert call(caller.summarize, graph, "n1") == "MAP"
                assert call(caller.summarize, graph, "n2") == "ATLAS"
                assert call(caller.summarize, graph, "unknown") == "missing"

            check("caller_contract", caller_contract)
            check("fake_contract", fake_contract)
            check("integrated", integrated)
    """)
    return _spec(
        case_id="holdout-record",
        scenario="图目录条目摘要",
        lineage="transfer-catalog-record-v1",
        description=description,
        contract=contract,
        initial=_files(caller, broken, visible, task),
        reference=_files(caller, fake, visible, task),
        unsafe=_files(caller.replace("record.label", 'record["label"]'), broken, visible, task),
        oracle=oracle,
        checks=("caller_contract", "fake_contract", "integrated"),
        failures={
            "initial": ("fake_contract", "integrated"),
            "unsafe": ("caller_contract", "fake_contract"),
        },
    )


def _deadline_case() -> CaseSpec:
    description = "修复图遍历请求的超时参数错误"
    contract = (
        "caller.reachable(graph, budget_ms) 接收毫秒预算并调用 graph.walk(timeout=秒)。"
        "walk 只接受非布尔的有限数字且 0.001 <= timeout <= 30，否则抛 ValueError。"
        "FakeGraph.walk 返回 3，并在 calls 中记录每次合法 timeout；拒绝调用不得记录。"
        "caller 返回 walk 的结果。"
    )
    task = _task(description, contract, ("caller.py", "fake_graph.py"))
    caller = _s("""
        def reachable(graph, budget_ms):
            return graph.walk(timeout=budget_ms / 1000)
    """)
    broken = caller.replace("budget_ms / 1000", "budget_ms")
    fake = _s("""
        import math

        class FakeGraph:
            def __init__(self):
                self.calls = []

            def walk(self, *, timeout):
                if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                        or not 0.001 <= timeout <= 30):
                    raise ValueError("timeout must be finite seconds in [0.001, 30]")
                self.calls.append(timeout)
                return 3
    """)
    lenient = _s("""
        class FakeGraph:
            def __init__(self):
                self.calls = []

            def walk(self, *, timeout):
                self.calls.append(timeout)
                return 3
    """)
    visible = "assert caller.reachable(fake_graph.FakeGraph(), 250) == 3\n"
    oracle = _s("""
        def verify():
            caller, fake = load_candidate({"caller": "caller.py", "fake_graph": "fake_graph.py"})

            class IndependentTraversal:
                def __init__(self):
                    self.calls = []

                def walk(self, *, timeout):
                    assert 0.001 <= timeout <= 30
                    self.calls.append(timeout)
                    return 17

            def caller_contract():
                graph = IndependentTraversal()
                for budget in (1, 25, 500, 12345, 30000):
                    assert call(caller.reachable, graph, budget) == 17
                    assert graph.calls[-1] == budget / 1000
                assert len(graph.calls) == 5

            def fake_contract():
                graph = call(fake.FakeGraph)
                for seconds in (0.001, 0.75, 30):
                    assert call(graph.walk, timeout=seconds) == 3
                assert graph.calls == [0.001, 0.75, 30]
                for invalid in (0, -1, 31, float("nan"), float("inf"), True, "1"):
                    rejects(ValueError, graph.walk, timeout=invalid)
                assert graph.calls == [0.001, 0.75, 30]

            def integrated():
                graph = call(fake.FakeGraph)
                assert call(caller.reachable, graph, 900) == 3
                assert graph.calls == [0.9]

            check("caller_contract", caller_contract)
            check("fake_contract", fake_contract)
            check("integrated", integrated)
    """)
    return _spec(
        case_id="holdout-deadline",
        scenario="图遍历预算换算",
        lineage="transfer-traversal-deadline-v1",
        description=description,
        contract=contract,
        initial=_files(broken, fake, visible, task),
        reference=_files(caller, fake, visible, task),
        unsafe=_files(broken, lenient, visible, task),
        oracle=oracle,
        checks=("caller_contract", "fake_contract", "integrated"),
        failures={
            "initial": ("caller_contract", "integrated"),
            "unsafe": ("caller_contract", "fake_contract", "integrated"),
        },
    )


def _migration_case() -> CaseSpec:
    description = "恢复图节点导出分页在接口升级后的行为"
    contract = (
        "新接口 Graph.scan(*, after, limit) 使用 after 游标（None 表示开头），limit 为非布尔正整数，"
        "非法 limit 抛 ValueError。"
        "返回有 records(tuple[str,...])、next_after(str|None) 属性的 Page；还有剩余项时"
        "next_after 为本页末项，否则为 None。不支持 start 关键字。"
        "FakeGraph 数据顺序固定为 a,b,c，未知游标抛 ValueError。"
        "caller.export_page(graph, cursor, size) 返回 (记录列表, next_after)。"
    )
    task = _task(description, contract, ("caller.py", "fake_graph.py"))
    caller = _s("""
        def export_page(graph, cursor, size):
            page = graph.scan(after=cursor, limit=size)
            return list(page.records), page.next_after
    """)
    broken = caller.replace("after=cursor", "start=cursor")
    fake = _s("""
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class Page:
            records: tuple[str, ...]
            next_after: str | None

        class FakeGraph:
            def scan(self, *, after, limit):
                nodes = ("a", "b", "c")
                if type(limit) is not int or limit <= 0:
                    raise ValueError("limit must be a positive integer")
                if after is not None and after not in nodes:
                    raise ValueError("unknown cursor")
                begin = 0 if after is None else nodes.index(after) + 1
                records = nodes[begin:begin + limit]
                next_after = records[-1] if records and begin + limit < len(nodes) else None
                return Page(records, next_after)
    """)
    old_fake = fake.replace("after", "start").replace(
        "return Page(records, next_start)", 'return {"items": records, "cursor": next_start}'
    )
    private_caller = _s("""
        def export_page(graph, cursor, size):
            page = graph.scan(start=cursor, limit=size)
            return list(page["items"]), page["cursor"]
    """)
    visible = 'assert caller.export_page(fake_graph.FakeGraph(), None, 2) == (["a", "b"], "b")\n'
    oracle = _s("""
        def verify():
            caller, fake = load_candidate({"caller": "caller.py", "fake_graph": "fake_graph.py"})
            from types import SimpleNamespace

            class IndependentExport:
                def scan(self, *, after, limit):
                    assert (after, limit) == ("q", 3)
                    return SimpleNamespace(records=("r", "s"), next_after=None)

            def caller_contract():
                assert call(caller.export_page, IndependentExport(), "q", 3) == (["r", "s"], None)

            def fake_contract():
                graph = call(fake.FakeGraph)
                for cursor, size, expected, following in (
                    (None, 1, ("a",), "a"), ("a", 2, ("b", "c"), None),
                    ("c", 1, (), None), (None, 8, ("a", "b", "c"), None),
                ):
                    page = call(graph.scan, after=cursor, limit=size)
                    assert not isinstance(page, dict)
                    assert page.records == expected and page.next_after == following
                rejects(TypeError, graph.scan, start=None, limit=1)
                for size in (0, -1, True, 1.5, "2", None):
                    rejects(ValueError, graph.scan, after=None, limit=size)
                rejects(ValueError, graph.scan, after="unknown", limit=1)

            def integrated():
                graph = call(fake.FakeGraph)
                assert call(caller.export_page, graph, None, 2) == (["a", "b"], "b")
                assert call(caller.export_page, graph, "b", 2) == (["c"], None)

            check("caller_contract", caller_contract)
            check("fake_contract", fake_contract)
            check("integrated", integrated)
    """)
    return _spec(
        case_id="holdout-migration",
        scenario="图节点分页导出协议迁移",
        lineage="transfer-export-migration-v1",
        description=description,
        contract=contract,
        initial=_files(broken, old_fake, visible, task),
        reference=_files(caller, fake, visible, task),
        unsafe=_files(private_caller, old_fake, visible, task),
        oracle=oracle,
        checks=("caller_contract", "fake_contract", "integrated"),
        failures={
            "initial": ("caller_contract", "fake_contract", "integrated"),
            "unsafe": ("caller_contract", "fake_contract"),
        },
    )


def _fixture_case() -> CaseSpec:
    description = "修复图游标测试在组合运行时的失败"
    contract = (
        "每个测试拥有独立图游标，初始依次包含 a,b。FakeGraph.take_all() 取出并消耗剩余项，"
        "同一实例再次调用应为空；caller.collect(graph) 原样返回本次取出的列表。"
        "tests 的单独、组合和倒序执行必须一致。本任务只支持 conftest.py 中名为 graph 的"
        "fixture，不支持额外 fixture 依赖；不得定义 pytest_* 执行/收集钩子或 pytest_plugins。"
    )
    editable = ("caller.py", "fake_graph.py", "conftest.py")
    task = _task(description, contract, editable)
    caller = _s("""
        def collect(graph):
            return graph.take_all()
    """)
    fake = _s("""
        class FakeGraph:
            def __init__(self):
                self.remaining = ["a", "b"]

            def take_all(self):
                result = list(self.remaining)
                self.remaining.clear()
                return result
    """)
    fixture = _s("""
        import pytest
        from fake_graph import FakeGraph

        @pytest.fixture(scope="function")
        def graph():
            return FakeGraph()
    """)
    tests = _s("""
        import caller

        def test_export(graph):
            assert caller.collect(graph) == ["a", "b"]

        def test_preview(graph):
            assert caller.collect(graph) == ["a", "b"]
    """)
    visible = _s("""
        import os
        import subprocess
        root = Path(__file__).resolve().parent
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-c", os.devnull, "--confcutdir=" + str(root), str(root / "test_visible.py")],
            cwd=root, env={**{key: value for key, value in os.environ.items()
                             if key not in {"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST"}},
                           "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            check=False,
        )
        sys.exit(result.returncode)
    """)
    initial = _files(
        caller,
        fake,
        visible,
        task,
        **{"conftest.py": fixture.replace('"function"', '"session"'), "test_visible.py": tests},
    )
    reference = _files(
        caller, fake, visible, task, **{"conftest.py": fixture, "test_visible.py": tests}
    )
    unsafe = {**initial, "fake_graph.py": fake.replace("self.remaining.clear()", "pass")}
    oracle = _s("""
        def verify():
            caller, fake, fixture = load_candidate({
                "caller": "caller.py", "fake_graph": "fake_graph.py", "conftest": "conftest.py"
            })

            class IndependentCursor:
                def __init__(self):
                    self.items = ["q", "r", "s"]
                def take_all(self):
                    result, self.items = self.items, []
                    return result

            def caller_contract():
                graph = IndependentCursor()
                assert call(caller.collect, graph) == ["q", "r", "s"]
                assert call(caller.collect, graph) == []

            def fake_state_contract():
                graph = call(fake.FakeGraph)
                assert call(graph.take_all) == ["a", "b"]
                assert call(graph.take_all) == []
                assert call(fake.FakeGraph().take_all) == ["a", "b"]

            def fixture_isolation():
                import os
                import subprocess
                import tempfile
                tests = ("import caller\\n"
                         "def test_export(graph):\\n"
                         "    assert caller.collect(graph) == ['a', 'b']\\n"
                         "def test_preview(graph):\\n"
                         "    assert caller.collect(graph) == ['a', 'b']\\n")
                runner = TRUSTED_FIXTURE_RUNNER
                with tempfile.TemporaryDirectory(prefix="fixture-oracle-") as directory:
                    suite = Path(directory) / "test_external.py"
                    suite.write_text(tests, encoding="utf-8")
                    first, second = str(suite) + "::test_export", str(suite) + "::test_preview"
                    for order in ((first,), (second,), (first, second), (second, first)):
                        result = subprocess.run(
                            [sys.executable, "-I", "-B", "-c", runner, str(root), *order],
                            cwd=directory, capture_output=True, text=True, timeout=5,
                            env={**{key: value for key, value in os.environ.items()
                                    if key not in {"PYTEST_ADDOPTS", "PYTEST_PLUGINS",
                                                   "PYTEST_CURRENT_TEST"}},
                                 "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
                            check=False,
                        )
                        if result.returncode not in (0, 1):
                            raise RuntimeError("invalid external pytest execution: "
                                               + result.stdout + result.stderr)
                        assert result.returncode == 0, result.stdout + result.stderr

            check("caller_contract", caller_contract)
            check("fake_state_contract", fake_state_contract)
            check("fixture_isolation", fixture_isolation)
    """).replace("TRUSTED_FIXTURE_RUNNER", repr(_FIXTURE_RUNNER))
    return _spec(
        case_id="holdout-fixture",
        scenario="图游标消费测试的 fixture 生命周期",
        lineage="transfer-cursor-fixture-v1",
        description=description,
        contract=contract,
        initial=initial,
        reference=reference,
        unsafe=unsafe,
        oracle=oracle,
        checks=("caller_contract", "fake_state_contract", "fixture_isolation"),
        failures={"initial": ("fixture_isolation",), "unsafe": ("fake_state_contract",)},
        editable=editable,
        assessment="boundary_repair",
        rubric=("轨迹是否先确认双方接口合同成立，再依据共享实例与组合测试结果定位状态隔离。",),
        imports={"caller": "caller.py", "fake_graph": "fake_graph.py", "conftest": "conftest.py"},
    )


def _discount_case() -> CaseSpec:
    description = "修复会员图策略驱动的订单金额边界错误"
    contract = (
        "Graph.policy(member) 返回 threshold_cents、percent 字段字典。FakeGraph 的 gold 策略"
        "为 threshold_cents=10000、percent=10，其他会员 percent=0 且 threshold_cents=10000。"
        "caller.quote(graph, member, cents) 对非负整数金额在达到（含）阈值时优惠，"
        "折后为 cents*(100-percent)//100，低于阈值保留原金额；非法金额抛 ValueError。"
        "policy 返回独立字典，修改返回值不影响后续查询；查询不能修改策略记录。"
    )
    task = _task(description, contract, ("caller.py", "fake_graph.py"))
    caller = _s("""
        def quote(graph, member, cents):
            if type(cents) is not int or cents < 0:
                raise ValueError("cents must be a nonnegative integer")
            policy = graph.policy(member)
            if cents >= policy["threshold_cents"]:
                return cents * (100 - policy["percent"]) // 100
            return cents
    """)
    broken = caller.replace("cents >=", "cents >")
    fake = _s("""
        class FakeGraph:
            def policy(self, member):
                return {"threshold_cents": 10000, "percent": 10 if member == "gold" else 0}
    """)
    unsafe = fake.replace('"threshold_cents": 10000', '"threshold_cents": 9999')
    visible = 'assert caller.quote(fake_graph.FakeGraph(), "gold", 10000) == 9000\n'
    oracle = _s("""
        def verify():
            caller, fake = load_candidate({"caller": "caller.py", "fake_graph": "fake_graph.py"})

            class IndependentPolicy:
                def __init__(self):
                    self.calls = []
                    self.value = {"threshold_cents": 5000, "percent": 20}
                def policy(self, member):
                    self.calls.append(member)
                    return self.value

            def caller_contract():
                graph = IndependentPolicy()
                assert call(caller.quote, graph, "a17", 6000) == 4800
                assert graph.calls == ["a17"]
                assert graph.value == {"threshold_cents": 5000, "percent": 20}
                for value in (-1, True, 0.5):
                    rejects(ValueError, caller.quote, graph, "a17", value)
                assert graph.calls == ["a17"]

            def fake_contract():
                graph = call(fake.FakeGraph)
                assert call(graph.policy, "gold") == {"threshold_cents": 10000, "percent": 10}
                assert call(graph.policy, "silver") == {"threshold_cents": 10000, "percent": 0}
                row = call(graph.policy, "gold")
                row["percent"] = 100
                assert call(graph.policy, "gold")["percent"] == 10

            def business_boundary():
                graph = IndependentPolicy()
                for cents, expected in ((0, 0), (4999, 4999), (5000, 4000),
                                        (5001, 4000), (20000, 16000)):
                    assert call(caller.quote, graph, "a17", cents) == expected
                graph = call(fake.FakeGraph)
                for cents, expected in ((9999, 9999), (10000, 9000), (10001, 9000)):
                    assert call(caller.quote, graph, "gold", cents) == expected
                    assert call(caller.quote, graph, "silver", cents) == cents

            check("caller_contract", caller_contract)
            check("fake_contract", fake_contract)
            check("business_boundary", business_boundary)
    """)
    return _spec(
        case_id="holdout-discount",
        scenario="会员图策略与订单金额的业务边界",
        lineage="transfer-membership-discount-v1",
        description=description,
        contract=contract,
        initial=_files(broken, fake, visible, task),
        reference=_files(caller, fake, visible, task),
        unsafe=_files(broken, unsafe, visible, task),
        oracle=oracle,
        checks=("caller_contract", "fake_contract", "business_boundary"),
        failures={
            "initial": ("business_boundary",),
            "unsafe": ("fake_contract", "business_boundary"),
        },
        assessment="boundary_repair",
        rubric=("轨迹是否保持双方存储合同，依据明确的含等号业务要求将问题分流到金额计算。",),
    )


def _ambiguous_case() -> CaseSpec:
    description = "调查并修复关系排序测试的失败"
    contract = (
        "Graph.edges() 返回包含 target、weight 的记录，ranked_targets(graph) 返回目标列表。"
        "请结合现有代码、注释和测试调查失败，恢复有业务依据支持的行为。"
        "不得仅以某段现有实现或让测试通过为依据创造业务规则。"
        "如无法确定修复，提交 diagnosis.json：disposition 为 needs_clarification，"
        "missing_definition 为非空字符串，evidence 为分别引用 caller.py 与 fake_graph.py 的"
        "至少两项 {path, observation}，"
        "question 为非空字符串，proposed_change 为 null。说明仍需人工核对。"
    )
    editable = ("caller.py", "fake_graph.py", "diagnosis.json")
    task = _task(description, contract, editable)
    caller = _s("""
        def ranked_targets(graph):
            # Existing consumer interprets weight as a traversal cost.
            return [row["target"] for row in sorted(graph.edges(), key=lambda row: row["weight"])]
    """)
    fake = _s("""
        class FakeGraph:
            def edges(self):
                # Existing fixture interprets larger weight as stronger affinity.
                return [{"target": "x", "weight": 2}, {"target": "y", "weight": 8}]
    """)
    visible = 'assert caller.ranked_targets(fake_graph.FakeGraph()) == ["y", "x"]\n'
    initial = _files(caller, fake, visible, task)
    diagnosis = {
        "disposition": "needs_clarification",
        "missing_definition": "weight 是代价还是相关度，以及排序方向和有效范围均无权威定义。",
        "evidence": [
            {"path": "caller.py", "observation": "消费者注释称遍历成本并使用升序。"},
            {"path": "fake_graph.py", "observation": "替身注释称较大 weight 表示较强相关度。"},
        ],
        "question": "请接口维护者确认 weight 含义、排序方向及有效范围。",
        "proposed_change": None,
    }
    reference = {**initial, "diagnosis.json": json.dumps(diagnosis, ensure_ascii=False, indent=2)}
    unsafe = {**reference, "caller.py": caller.replace('row["weight"]', '-row["weight"]')}
    oracle = _s("""
        def verify():
            caller, fake = load_candidate({"caller": "caller.py", "fake_graph": "fake_graph.py"})

            def unchanged_semantics():
                expected = EXPECTED_CODE
                for name, source in expected.items():
                    assert (root / name).read_text(encoding="utf-8") == source, (
                        "unresolved contract does not authorize changing task semantics"
                    )

            def diagnosis_structure():
                path = root / "diagnosis.json"
                assert path.is_file(), "a specific clarification report is required"
                try:
                    report = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, OSError) as exc:
                    raise AssertionError("invalid diagnosis JSON") from exc
                assert isinstance(report, dict)
                assert report.get("disposition") == "needs_clarification"
                for key in ("missing_definition", "question"):
                    assert isinstance(report.get(key), str) and report[key].strip()
                assert "proposed_change" in report and report["proposed_change"] is None
                evidence = report.get("evidence")
                assert isinstance(evidence, list) and len(evidence) >= 2
                assert all(isinstance(item, dict) for item in evidence)
                paths = set()
                for item in evidence:
                    assert item.get("path") in {"caller.py", "fake_graph.py"}
                    assert isinstance(item.get("observation"), str) and item["observation"].strip()
                    paths.add(item["path"])
                assert paths == {"caller.py", "fake_graph.py"}
                # This only validates shape; a human must judge claims and evidence.

            check("unchanged_semantics", unchanged_semantics)
            check("diagnosis_structure", diagnosis_structure)
    """).replace("EXPECTED_CODE", repr({"caller.py": caller, "fake_graph.py": fake}))
    return _spec(
        case_id="holdout-ambiguous",
        scenario="关系权重缺少权威语义定义",
        lineage="transfer-edge-semantics-v1",
        description=description,
        contract=contract,
        initial=initial,
        reference=reference,
        unsafe=unsafe,
        oracle=oracle,
        checks=("unchanged_semantics", "diagnosis_structure"),
        failures={"initial": ("diagnosis_structure",), "unsafe": ("unchanged_semantics",)},
        editable=editable,
        assessment="manual_boundary",
        rubric=(
            "是否指出 weight 含义、排序方向等具体缺失定义。",
            "是否提供能核对的调用方和替身冲突依据，未伪称已有权威合同。",
            "是否停止猜测性行为修改，并明确向合同负责人请求澄清的原因。",
        ),
    )


def _binding_case() -> CaseSpec:
    description = "修复图搜索依赖替换后仍读取旧数据的问题"
    contract = (
        "caller.install_client(client) 为后续搜索安装指定依赖，caller.read_title(key) 必须"
        "通过实际安装实例的 lookup 读取标题；缺失 key 抛 KeyError。"
        "caller.SERVICE.client 可观测实际绑定对象，caller.LOCAL 保留原本地实例。"
        "FakeGraph(titles) 复制传入映射并在 calls 中记录每次 lookup 的 key；不得修改输入，"
        "调用者后续修改输入也不影响实例。"
        "现有本地依赖初始 n1=local，替换后旧实例不得收到新请求。"
    )
    task = _task(description, contract, ("caller.py", "fake_graph.py"))
    caller = _s("""
        class LocalGraph:
            def __init__(self):
                self.calls = []
            def lookup(self, key):
                self.calls.append(key)
                return {"n1": "local"}[key]

        class Catalog:
            def __init__(self, client):
                self.client = client
            def read_title(self, key):
                return self.client.lookup(key)

        LOCAL = LocalGraph()
        DEFAULT_CLIENT = LOCAL
        SERVICE = Catalog(DEFAULT_CLIENT)

        def install_client(client):
            global DEFAULT_CLIENT, SERVICE
            DEFAULT_CLIENT = client
            SERVICE = Catalog(client)

        def read_title(key):
            return SERVICE.read_title(key)
    """)
    broken = caller.replace("            SERVICE = Catalog(client)\n", "")
    # The source was dedented, so remove the function's actual four-space statement.
    broken = broken.replace("    SERVICE = Catalog(client)\n", "")
    fake = _s("""
        class FakeGraph:
            def __init__(self, titles):
                self.titles = dict(titles)
                self.calls = []
            def lookup(self, key):
                self.calls.append(key)
                return self.titles[key]
    """)
    visible = _s("""
        graph = fake_graph.FakeGraph({"n1": "replacement"})
        caller.install_client(graph)
        assert caller.read_title("n1") == "replacement"
    """)
    unsafe = broken.replace("return SERVICE.read_title(key)", "return DEFAULT_CLIENT.titles[key]")
    oracle = _s("""
        def verify():
            caller, fake = load_candidate({"caller": "caller.py", "fake_graph": "fake_graph.py"})

            class IndependentClient:
                def __init__(self, prefix):
                    self.prefix, self.calls = prefix, []
                def lookup(self, key):
                    self.calls.append(key)
                    if key == "missing":
                        raise KeyError(key)
                    return self.prefix + key

            def fake_contract():
                original = {"n1": "replacement", "n2": "atlas"}
                graph = call(fake.FakeGraph, original)
                assert call(graph.lookup, "n2") == "atlas"
                rejects(KeyError, graph.lookup, "missing")
                assert graph.calls == ["n2", "missing"]
                assert original == {"n1": "replacement", "n2": "atlas"}
                original["n2"] = "changed outside"
                assert call(graph.lookup, "n2") == "atlas"
                custom = {"node-83": "field guide", "node-29": "regional chart"}
                graph = call(fake.FakeGraph, custom)
                for key, title in custom.items():
                    assert call(graph.lookup, key) == title
                rejects(KeyError, graph.lookup, "n1")
                assert graph.calls == ["node-83", "node-29", "n1"]
                custom["node-83"] = "mutated outside"
                assert call(graph.lookup, "node-83") == "field guide"

            def initial_local_behavior():
                assert caller.SERVICE.client is caller.LOCAL
                assert caller.LOCAL.calls == []
                assert call(caller.read_title, "n1") == "local"
                rejects(KeyError, caller.read_title, "unknown-local-key")
                assert caller.LOCAL.calls == ["n1", "unknown-local-key"]

            def installed_identity_and_behavior():
                old_calls = list(caller.LOCAL.calls)
                first = IndependentClient("one:")
                call(caller.install_client, first)
                assert caller.SERVICE.client is first, "service retains an earlier dependency"
                assert call(caller.read_title, "node-7") == "one:node-7"
                assert first.calls == ["node-7"]
                second = IndependentClient("two:")
                call(caller.install_client, second)
                assert caller.SERVICE.client is second
                assert call(caller.read_title, "node-8") == "two:node-8"
                rejects(KeyError, caller.read_title, "missing")
                assert second.calls == ["node-8", "missing"]
                assert first.calls == ["node-7"]
                assert caller.LOCAL.calls == old_calls

            def integrated():
                graph = call(fake.FakeGraph, {"n1": "replacement", "n2": "atlas"})
                call(caller.install_client, graph)
                assert caller.SERVICE.client is graph
                assert call(caller.read_title, "n2") == "atlas"
                assert graph.calls == ["n2"]

            check("fake_contract", fake_contract)
            check("initial_local_behavior", initial_local_behavior)
            check("installed_identity_and_behavior", installed_identity_and_behavior)
            check("integrated", integrated)
    """)
    return _spec(
        case_id="holdout-binding",
        scenario="图搜索服务对象构造与依赖替换时机",
        lineage="transfer-search-binding-v1",
        description=description,
        contract=contract,
        initial=_files(broken, fake, visible, task),
        reference=_files(caller, fake, visible, task),
        unsafe=_files(unsafe, fake, visible, task),
        oracle=oracle,
        checks=(
            "fake_contract",
            "initial_local_behavior",
            "installed_identity_and_behavior",
            "integrated",
        ),
        failures={
            "initial": ("installed_identity_and_behavior", "integrated"),
            "unsafe": ("installed_identity_and_behavior", "integrated"),
        },
        assessment="boundary_repair",
        rubric=("轨迹是否比较服务持有的旧实例与拟替换实例，并依据实际调用确认绑定时机问题。",),
    )


def get_holdout_case(case_id: str) -> CaseSpec:
    """Return a fresh task; only the trusted registry chooses its holdout identity."""
    factories = dict(
        zip(
            HOLDOUT_CASE_IDS,
            (
                _record_case,
                _deadline_case,
                _migration_case,
                _fixture_case,
                _discount_case,
                _ambiguous_case,
                _binding_case,
            ),
            strict=True,
        )
    )
    try:
        factory = factories[case_id]
    except KeyError as exc:
        raise ValueError(f"Unknown holdout case: {case_id}") from exc
    return factory()
