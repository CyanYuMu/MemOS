"""Frozen project interface context, separate from empirical Memory evidence.

Only host bindings choose a repository. Model-provided paths are never opened.
Hash self-consistency and authentic live readback are deliberately separate.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import stat
import subprocess
import textwrap

from pathlib import Path

from memos.skill_release_v2.gate import digest_json


CONTRACT_PATH = "src/memos/graph_dbs/base.py"
SYMBOL = "BaseGraphDB.update_node"
SELECTOR = "python-method-v1"
_FIELDS = {
    "schema_version",
    "kind",
    "project_id",
    "repository_revision",
    "repository_relative_path",
    "file_sha256",
    "symbol",
    "selector_version",
    "excerpt",
    "excerpt_sha256",
    "ast_sha256",
    "authority_kind",
    "empirical_sample",
    "extractor_digest",
    "context_id",
}
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_COMMIT = re.compile(r"[a-f0-9]{40}\Z")
_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_LIMIT = 2 * 1024 * 1024
_IMPORTS = {"abc": {"ABC", "abstractmethod"}, "typing": {"Any", "Literal"}}
_RESERVED = {"BaseGraphDB", "re", "ABC", "abstractmethod", "staticmethod", "Any", "Literal"}


def _require(value, code):
    if not value:
        raise ValueError("authority_" + code)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _method_ast(excerpt):
    parsed = ast.parse(textwrap.dedent(excerpt))
    _require(
        len(parsed.body) == 1
        and type(parsed.body[0]) is ast.FunctionDef
        and parsed.body[0].name == "update_node",
        "symbol",
    )
    return _sha(ast.dump(parsed.body[0], include_attributes=False).encode("utf-8"))


def _select(raw):
    text = raw.decode("utf-8")
    parsed = ast.parse(text)
    classes = [n for n in parsed.body if isinstance(n, ast.ClassDef) and n.name == "BaseGraphDB"]
    _require(len(classes) == 1, "symbol")
    cls = classes[0]
    _require(not cls.decorator_list and not cls.keywords, "symbol")
    _require(
        not cls.bases
        or (
            len(cls.bases) == 1 and isinstance(cls.bases[0], ast.Name) and cls.bases[0].id == "ABC"
        ),
        "symbol",
    )
    for statement in parsed.body:
        if statement is classes[0] or _docstring(statement):
            continue
        _module_declaration(statement)
    methods = [
        n
        for n in classes[0].body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == "update_node"
    ]
    _require(len(methods) == 1 and type(methods[0]) is ast.FunctionDef, "symbol")
    node = methods[0]
    for statement in classes[0].body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            _require(statement is node or statement.name != "update_node", "symbol")
            _require(statement.name not in _RESERVED, "symbol")
            _function_header(statement)
            continue
        _require(_docstring(statement), "symbol")
    start = min([node.lineno, *[d.lineno for d in node.decorator_list]])
    excerpt = "".join(text.splitlines(keepends=True)[start - 1 : node.end_lineno])
    return excerpt, _method_ast(excerpt)


def _literal(node):
    try:
        ast.literal_eval(node)
        return True
    except (ValueError, TypeError, SyntaxError, RecursionError):
        return False


def _module_declaration(statement):
    if isinstance(statement, ast.Import):
        _require(all(a.name == "re" and a.asname is None for a in statement.names), "symbol")
        return
    if isinstance(statement, ast.ImportFrom):
        _require(
            statement.level == 0
            and statement.module in _IMPORTS
            and all(
                a.asname is None and a.name in _IMPORTS[statement.module] for a in statement.names
            ),
            "symbol",
        )
        return
    # Only inert constants and the actual source's literal regex constructor
    # are supported. Arbitrary calls cannot be disguised as "constants".
    _require(isinstance(statement, ast.Assign | ast.AnnAssign), "symbol")
    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
    _require(all(isinstance(t, ast.Name) and t.id not in _RESERVED for t in targets), "symbol")
    value = statement.value
    regex = (
        len(targets) == 1
        and targets[0].id == "_VALID_FIELD_NAME_RE"
        and isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and isinstance(value.func.value, ast.Name)
        and value.func.value.id == "re"
        and value.func.attr == "compile"
        and len(value.args) == 1
        and not value.keywords
        and isinstance(value.args[0], ast.Constant)
        and type(value.args[0].value) is str
    )
    _require(regex or _literal(value), "symbol")
    if isinstance(statement, ast.AnnAssign):
        _require(_literal(statement.annotation), "symbol")


def _function_header(node):
    _require(
        all(
            isinstance(d, ast.Name) and d.id in {"abstractmethod", "staticmethod"}
            for d in node.decorator_list
        ),
        "symbol",
    )
    _require(
        all(_literal(d) for d in [*node.args.defaults, *node.args.kw_defaults] if d is not None),
        "symbol",
    )
    args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    args += [a for a in (node.args.vararg, node.args.kwarg) if a is not None]
    annotations = [a.annotation for a in args if a.annotation is not None]
    annotations += [node.returns] if node.returns is not None else []
    allowed = (
        ast.Name,
        ast.Load,
        ast.Attribute,
        ast.Subscript,
        ast.Tuple,
        ast.List,
        ast.Constant,
        ast.BinOp,
        ast.BitOr,
    )
    _require(all(isinstance(n, allowed) for a in annotations for n in ast.walk(a)), "symbol")


def _docstring(node):
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and type(node.value.value) is str
    )


def _read(repository):
    root = Path(repository).absolute()
    path = root / CONTRACT_PATH
    _require(not any(p.is_symlink() for p in (path, *path.parents)), "unsafe_path")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "unsafe_path")
            raw = stream.read(_LIMIT + 1)
        _require(len(raw) <= _LIMIT, "file_too_large")
        return root, raw
    except OSError:
        raise ValueError("authority_file_unavailable") from None


def _git(root, *args):
    # Neither ambient GIT_DIR nor local replace refs may change a frozen OID's
    # meaning. No network, hooks, worktree filters or shell evaluation are used.
    try:
        result = subprocess.run(
            ["/usr/bin/git", "--no-replace-objects", "-C", str(root), *args],
            capture_output=True,
            timeout=5,
            check=False,
            env={
                "PATH": "/usr/bin:/bin",
                "LC_ALL": "C",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            },
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError("authority_commit_unavailable") from None
    _require(result.returncode == 0, "commit_unavailable")
    _require(len(result.stdout) <= _LIMIT, "file_too_large")
    return result.stdout


def _committed_file(root, revision):
    actual_root = _git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    _require(Path(actual_root).resolve() == root.resolve(), "repository_root")
    _require(_git(root, "cat-file", "-t", revision) == b"commit\n", "revision_type")
    target = revision + ":" + CONTRACT_PATH
    _require(_git(root, "cat-file", "-t", target) == b"blob\n", "file_type")
    size = _git(root, "cat-file", "-s", target).strip()
    _require(size.isdigit() and int(size) <= _LIMIT, "file_too_large")
    return _git(root, "cat-file", "blob", target)


def extract_contract_context(repository, *, project_id: str, repository_revision: str) -> dict:
    """Extract the one allowed symbol from bytes verified against a Git commit."""
    _require(type(project_id) is str and _PROJECT.fullmatch(project_id), "project")
    _require(
        type(repository_revision) is str and _COMMIT.fullmatch(repository_revision), "revision"
    )
    root, raw = _read(repository)
    _require(raw == _committed_file(root, repository_revision), "commit_file_mismatch")
    try:
        excerpt, ast_digest = _select(raw)
    except (SyntaxError, UnicodeError, RecursionError):
        raise ValueError("authority_source_invalid") from None
    context = {
        "schema_version": 1,
        "kind": "project_contract_context",
        "project_id": project_id,
        "repository_revision": repository_revision,
        "repository_relative_path": CONTRACT_PATH,
        "file_sha256": _sha(raw),
        "symbol": SYMBOL,
        "selector_version": SELECTOR,
        "excerpt": excerpt,
        "excerpt_sha256": _sha(excerpt.encode("utf-8")),
        "ast_sha256": ast_digest,
        "authority_kind": "project_interface_contract",
        "empirical_sample": False,
        "extractor_digest": _sha(Path(__file__).read_bytes()),
    }
    context["context_id"] = digest_json(context)
    validate_contract_context(context)
    return context


def validate_contract_context(context: dict) -> None:
    """Check an embedded contract; does not claim the repository is still equal."""
    _require(type(context) is dict and set(context) == _FIELDS, "fields")
    _require(type(context["schema_version"]) is int and context["schema_version"] == 1, "schema")
    _require(
        context["kind"] == "project_contract_context"
        and context["authority_kind"] == "project_interface_contract"
        and context["empirical_sample"] is False,
        "kind",
    )
    _require(
        type(context["project_id"]) is str and _PROJECT.fullmatch(context["project_id"]), "project"
    )
    _require(
        type(context["repository_revision"]) is str
        and _COMMIT.fullmatch(context["repository_revision"]),
        "revision",
    )
    _require(
        context["repository_relative_path"] == CONTRACT_PATH
        and context["symbol"] == SYMBOL
        and context["selector_version"] == SELECTOR,
        "selector",
    )
    for key in ("file_sha256", "excerpt_sha256", "ast_sha256", "extractor_digest", "context_id"):
        _require(type(context[key]) is str and _DIGEST.fullmatch(context[key]), "digest")
    excerpt = context["excerpt"]
    _require(type(excerpt) is str and 0 < len(excerpt) <= 32768, "excerpt")
    try:
        _require(_sha(excerpt.encode("utf-8")) == context["excerpt_sha256"], "excerpt_digest")
        _require(_method_ast(excerpt) == context["ast_sha256"], "ast_digest")
        expected = digest_json({k: v for k, v in context.items() if k != "context_id"})
        _require(context["context_id"] == expected, "context_digest")
    except (SyntaxError, UnicodeError, RecursionError):
        raise ValueError("authority_excerpt_invalid") from None


def recheck_contract_context(context: dict, *, authority_bindings: dict) -> dict:
    """Recheck frozen authority without replacing its baseline or following a URI."""
    validate_contract_context(context)
    _require(context["extractor_digest"] == _sha(Path(__file__).read_bytes()), "extractor_changed")
    _require(
        type(authority_bindings) is dict and context["project_id"] in authority_bindings,
        "binding_missing",
    )
    root, raw = _read(authority_bindings[context["project_id"]])
    _require(_sha(raw) == context["file_sha256"], "file_changed")
    _require(raw == _committed_file(root, context["repository_revision"]), "commit_file_mismatch")
    try:
        excerpt, ast_digest = _select(raw)
    except (SyntaxError, UnicodeError, RecursionError):
        raise ValueError("authority_source_invalid") from None
    _require(
        excerpt == context["excerpt"] and ast_digest == context["ast_sha256"], "symbol_changed"
    )
    return {"context_id": context["context_id"], "status": "healthy"}
