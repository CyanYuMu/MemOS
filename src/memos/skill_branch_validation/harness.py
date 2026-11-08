"""Host-defined oracle shell; candidate code only runs in the confined executor."""

from textwrap import dedent


def source(text: str) -> str:
    return dedent(text).lstrip("\n")


HEADER = source("""
    import importlib.util
    import json
    import sys
    from copy import deepcopy
    from pathlib import Path

    root = Path(sys.argv[1]).resolve()
    report_path = Path(sys.argv[2])
    checks = []

    def load(name):
        spec = importlib.util.spec_from_file_location(name, root / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert Path(module.__file__).resolve() == root / (name + ".py")
        return module

    def check(name, test):
        try:
            test()
            checks.append({"name": name, "status": "passed"})
        except (AssertionError, TypeError) as error:
            checks.append({"name": name, "status": "failed", "error": type(error).__name__})
        except Exception as error:
            checks.append({"name": name, "status": "error", "error": type(error).__name__})

    def rejects(function, *args, **kwargs):
        try:
            function(*args, **kwargs)
        except TypeError:
            return
        raise AssertionError("invalid input was accepted")

    caller = load("caller")
    fake = load("fake_graph")
""")

FOOTER = source("""
    report_path.write_text(json.dumps({"checks": checks, "imported": {
        "caller": str(Path(caller.__file__).resolve()),
        "fake_graph": str(Path(fake.__file__).resolve()),
    }}))
    raise SystemExit(0 if all(c["status"] == "passed" for c in checks) else 1)
""")
