"""Conservative Python syntax extraction; no runtime or inferred dispatch."""

import ast
import builtins
import copy
import hashlib
import io
import tokenize
from dataclasses import dataclass
from pathlib import PurePosixPath

EXTRACTOR_VERSION = "0.3.3"

_PY_BUILTINS = frozenset(dir(builtins)) - {
    "__import__",
    "eval",
    "exec",
    "getattr",
    "globals",
    "locals",
    "vars",
}


class CodeGraphError(ValueError):
    pass


class CodeGraphParseError(CodeGraphError):
    pass


@dataclass(frozen=True)
class Symbol:
    symbol_id: str
    kind: str
    signature: str
    body: str
    start: int
    end: int
    cx: int


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    kind: str
    line: int


@dataclass(frozen=True)
class FileAnalysis:
    path: str
    module: str
    blob_sha: str
    skeleton: str
    symbols: tuple[Symbol, ...]
    edges: tuple[Edge, ...]


# Ends the module path of an import target: `from pkg.sub import x` is "pkg.sub|x".
# The import system, not attribute lookup on `pkg`, finds the part before it.
MODULE_END = "|"
TS_JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
SUPPORTED_EXTENSIONS = (".py", ".java", *TS_JS_EXTENSIONS)
_EXCLUDED_SUFFIXES = (".d.ts", ".min.js", ".bundle.js")
_TS_JS_TEST_SUFFIXES = tuple(
    f"{tag}{ext}" for tag in (".test", ".spec") for ext in TS_JS_EXTENSIONS
)


def is_supported_source_path(path: str) -> bool:
    return path.endswith(SUPPORTED_EXTENSIONS) and not path.endswith(_EXCLUDED_SUFFIXES)


def is_test_path(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    if path.endswith(".py"):
        return any(part == "tests" for part in parts) or name.startswith("test_")
    if path.endswith(".java"):
        return (
            any(part in {"test", "tests"} for part in parts[:-1])
            or name.endswith(("Test.java", "Tests.java", "IT.java"))
            or name.startswith("Test")
        )
    if path.endswith(TS_JS_EXTENSIONS):
        return (
            any(part in {"test", "tests", "__tests__", "spec", "specs"} for part in parts[:-1])
            or name.endswith(_TS_JS_TEST_SUFFIXES)
        )
    return False


def plain_target(target: str) -> str:
    """Return an import-tagged target as a plain dotted name."""
    module, end, rest = target.partition(MODULE_END)
    return ".".join(part for part in (module, rest.lstrip(".")) if part) if end else target


def _c3_pick_head(seqs: list[list[str]]) -> str:
    for s in seqs:
        head = s[0]
        if all(head not in other[1:] for other in seqs):
            return head
    return seqs[0][0]


def _c3_merge(cls_sid: str, seqs: list[list[str]]) -> tuple[str, ...]:
    res = [cls_sid]
    active = [s for s in seqs if s]
    while active:
        cand = _c3_pick_head(active)
        res.append(cand)
        active = [[x for x in s if x != cand] for s in active]
        active = [s for s in active if s]
    return tuple(dict.fromkeys(res))


def compute_c3_mro(
    cls_sid: str, bases_map: dict[str, tuple[str, ...] | list[str]]
) -> tuple[str, ...]:
    """Compute Python C3 method resolution order over a class->bases mapping."""
    memo: dict[str, tuple[str, ...]] = {}
    stack: list[tuple[str, bool]] = [(cls_sid, False)]
    visiting: set[str] = set()
    while stack:
        curr, processed = stack.pop()
        if curr in memo:
            continue
        direct = [b for b in dict.fromkeys(bases_map.get(curr, ())) if b and b != curr]
        if not direct:
            memo[curr] = (curr,)
            visiting.discard(curr)
            continue
        if processed:
            seqs = [list(memo.get(b, (b,))) for b in direct] + [direct]
            memo[curr] = _c3_merge(curr, seqs)
            visiting.discard(curr)
            continue
        visiting.add(curr)
        stack.append((curr, True))
        for b in reversed(direct):
            if b not in memo and b not in visiting:
                stack.append((b, False))
    return memo.get(cls_sid, (cls_sid,))


def valid_path(path: str) -> str:
    p = PurePosixPath(path)
    if (
        p.is_absolute()
        or ".." in p.parts
        or "\\" in path
        or any(ord(c) < 32 for c in path)
        or not is_supported_source_path(path)
        or p.as_posix() != path
    ):
        raise CodeGraphError(f"invalid Python path: {path!r}")
    return path


def module_name(path: str, source_root: str = "") -> str:
    name = path.removeprefix(source_root)[:-3].replace("/", ".")
    return name.removesuffix(".__init__") if name != "__init__" else "__init__"


def _signature(node, text: str) -> str:
    """Take the original header up to its depth-zero colon, including multiline annotations."""
    segment = ast.get_source_segment(text, node) or ""
    depth = 0
    for token in tokenize.generate_tokens(io.StringIO(segment).readline):
        if token.type == tokenize.OP:
            if token.string in "([{":
                depth += 1
            elif token.string in ")]}":
                depth -= 1
            elif token.string == ":" and depth == 0:
                lines = segment.splitlines(keepends=True)
                return (
                    "".join(lines[: token.end[0] - 1])
                    + lines[token.end[0] - 1][: token.end[1]]
                )
    raise CodeGraphParseError(f"missing definition header on line {node.lineno}")


def _is_py_private_method(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
        and stmt.name.startswith("_")
        and not (stmt.name.startswith("__") and stmt.name.endswith("__"))
    )


class _Strip(ast.NodeTransformer):
    def visit_ClassDef(self, node: ast.ClassDef):
        self.generic_visit(node)
        priv_idxs = [i for i, s in enumerate(node.body) if _is_py_private_method(s)]
        if len(priv_idxs) <= 3:
            return node
        omitted = len(priv_idxs) - 3
        drop_set = set(priv_idxs[3:])
        node.body = [s for i, s in enumerate(node.body) if i not in drop_set]
        node.body.append(ast.Expr(value=ast.Constant(value=f"... (+{omitted} private methods)")))
        return node

    def _function(self, node):
        doc = node.body[:1] if ast.get_docstring(node, clean=False) is not None else []
        nested_cls = [self.visit_ClassDef(s) for s in node.body if isinstance(s, ast.ClassDef)]
        node.body = doc + nested_cls + [ast.Expr(value=ast.Constant(value=Ellipsis))]
        return node

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function


def detect_layout(paths) -> tuple[str, frozenset[str]]:
    """Return the import root ("src/" or "") and the top-level packages it holds."""
    paths = set(paths)
    # Pure src layout only: a top-level package or src/__init__.py means `src.` is imported as-is.
    source_root = "src/" if (
        "src/__init__.py" not in paths
        and not any(p.count("/") == 1 and p.endswith("/__init__.py") for p in paths)
        and any(p.startswith("src/") and p.count("/") == 2 and p.endswith("/__init__.py") for p in paths)
    ) else ""
    packages = frozenset(
        p.removeprefix(source_root).split("/")[0]
        for p in paths
        if p.removeprefix(source_root).count("/") == 1 and p.endswith("/__init__.py")
    )
    return source_root, packages


def extract_file(
    path: str,
    source: bytes,
    packages: frozenset[str] = frozenset(),
    source_root: str = "",
    path_aliases: dict[str, str] | None = None,
) -> FileAnalysis:
    """Extract one file; string constants naming a module in `packages` become IMPORTS."""
    valid_path(path)
    if path.endswith(".java"):
        from .java_extractor import extract_java_file

        return extract_java_file(path, source, source_root)
    if path.endswith(TS_JS_EXTENSIONS):
        from .ts_extractor import extract_typescript_file

        return extract_typescript_file(path, source, source_root, path_aliases)
    if b"\0" in source:
        raise CodeGraphParseError(f"binary source: {path}")
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
        text = source.decode(encoding)
        tree = ast.parse(text, filename=path)
    except (UnicodeError, SyntaxError, tokenize.TokenError, LookupError) as exc:
        raise CodeGraphParseError(f"cannot parse {path}: {exc}") from exc
    module = module_name(path, source_root)
    package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
    lines = text.splitlines()
    symbols: list[Symbol] = []
    edges: list[Edge] = []
    scope: list[str] = []
    scope_kinds: list[str] = []
    frames: list[dict[str, str]] = []
    frame_kinds: list[str] = []
    frame_globals: list[set[str]] = []
    frame_nonlocals: list[set[str]] = []
    frame_local_types: list[dict[str, tuple[str, ...]]] = []
    frame_local_elem_types: list[dict[str, tuple[str, ...]]] = []
    frame_local_deferred_ret: list[dict[str, tuple[str, ...]]] = []
    frame_local_callables: list[dict[str, tuple[str, ...]]] = []
    type_aliases: dict[str, tuple[str, ...]] = {}
    elem_type_aliases: dict[str, tuple[str, ...]] = {}
    class_bases: dict[str, tuple[str, ...]] = {}
    structural_protocols_of: dict[str, tuple[str, ...]] = {}
    class_methods: dict[str, set[str]] = {}
    method_arity: dict[tuple[str, str], tuple[int, int | None]] = {}
    class_fields: dict[str, dict[str, tuple[str, ...]]] = {}
    class_elem_fields: dict[str, dict[str, tuple[str, ...]]] = {}
    func_returns: dict[str, tuple[str, ...]] = {}
    func_elem_returns: dict[str, tuple[str, ...]] = {}
    container_callables: dict[str, tuple[str, ...]] = {}
    local_function_sids: set[str] = set()
    local_class_sids: set[str] = set()
    protocol_classes: set[str] = set()
    abc_classes: set[str] = set()
    is_test = is_test_path(path)

    def owner() -> str:
        return module + ":" + ".".join(scope)

    def import_base(node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        parts = package.split(".") if package else []
        if node.level > len(parts):
            raise CodeGraphParseError(
                f"relative import escapes package: {path}:{node.lineno}"
            )
        return ".".join(
            parts[: len(parts) - node.level + 1]
            + ([node.module] if node.module else [])
        )

    def bindings(statements) -> dict[str, str]:
        result: dict[str, str] = {}

        class Bind(ast.NodeVisitor):
            def visit_Import(self, node):
                for alias in node.names:
                    result[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    ) + MODULE_END

            def visit_ImportFrom(self, node):
                base = import_base(node)
                for alias in node.names:
                    result[alias.asname or alias.name] = (
                        f"{base}{MODULE_END}{alias.name}" if base else alias.name
                    )

            def definition(self, node):
                # Decorators, defaults, bases and (before Python 3.14, conservatively
                # always) annotations run when the definition does.
                args = getattr(node, "args", None)
                params = (
                    [*args.posonlyargs, *args.args, args.vararg, *args.kwonlyargs, args.kwarg]
                    if args else []
                )
                for child in [
                    *node.decorator_list,
                    *getattr(node, "bases", []),
                    *getattr(node, "keywords", []),
                    *([*args.defaults, *args.kw_defaults] if args else []),
                    *(param.annotation for param in params if param is not None),
                    getattr(node, "returns", None),
                ]:
                    if child is not None:
                        self.visit(child)
                result[node.name] = owner() + ("." if scope else "") + node.name

            visit_FunctionDef = definition
            visit_AsyncFunctionDef = definition
            visit_ClassDef = definition

            def visit_TypeAlias(self, node):
                result[node.name.id] = owner() + ("." if scope else "") + node.name.id

            def visit_Lambda(self, node):
                for default in [*node.args.defaults, *node.args.kw_defaults]:
                    if default is not None:
                        self.visit(default)

            def visit_comp_parts(self, generators, values):
                for generator in generators:
                    self.visit(generator.iter)
                    for condition in generator.ifs:
                        self.visit(condition)
                for value in values:
                    if value is not None:
                        self.visit(value)

            def visit_ListComp(self, node: ast.ListComp):
                self.visit_comp_parts(node.generators, (node.elt,))

            def visit_SetComp(self, node: ast.SetComp):
                self.visit_comp_parts(node.generators, (node.elt,))

            def visit_DictComp(self, node: ast.DictComp):
                self.visit_comp_parts(node.generators, (node.key, node.value))

            def visit_GeneratorExp(self, node: ast.GeneratorExp):
                self.visit_comp_parts(node.generators, (node.elt,))

            def conditional(self, node):
                # Any name written inside the block is unknown, even if a branch
                # restores its earlier value.
                before = result.copy()
                result.update(dict.fromkeys(before, "\0"))
                self.generic_visit(node)
                for name, value in result.items():
                    result[name] = before[name] if value == "\0" else "?" + name

            visit_If = conditional
            visit_Try = conditional
            visit_TryStar = conditional
            visit_For = conditional
            visit_AsyncFor = conditional
            visit_While = conditional
            visit_Match = conditional

            def visit_Name(self, node):
                if isinstance(node.ctx, (ast.Store, ast.Del)):
                    result[node.id] = "?" + node.id

            # Binding names the AST stores as plain strings, not Name nodes.
            def string_binding(self, node):
                name = getattr(node, "name", None) or getattr(node, "rest", None)
                if name:
                    result[name] = "?" + name
                self.generic_visit(node)

            visit_ExceptHandler = string_binding
            visit_MatchAs = string_binding
            visit_MatchStar = string_binding
            visit_MatchMapping = string_binding

        visitor = Bind()
        for statement in statements:
            visitor.visit(statement)
        return result

    def declarations(statements):
        global_names: set[str] = set()
        nonlocal_names: set[str] = set()

        class Declarations(ast.NodeVisitor):
            def visit_Global(self, node):
                global_names.update(node.names)

            def visit_Nonlocal(self, node):
                nonlocal_names.update(node.names)

            def scope(self, node):
                return

            visit_FunctionDef = scope
            visit_AsyncFunctionDef = scope
            visit_ClassDef = scope
            visit_Lambda = scope

        visitor = Declarations()
        for statement in statements:
            visitor.visit(statement)
        return global_names, nonlocal_names

    def walrus_bindings(node):
        result: set[str] = set()

        class Walrus(ast.NodeVisitor):
            def visit_NamedExpr(self, node: ast.NamedExpr):
                result.add(node.target.id)
                self.visit(node.value)

            def visit_Lambda(self, node: ast.Lambda):
                for default in [*node.args.defaults, *node.args.kw_defaults]:
                    if default is not None:
                        self.visit(default)

            def visit_FunctionDef(self, node: ast.FunctionDef):
                return

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
                return

            def visit_ClassDef(self, node: ast.ClassDef):
                return

        Walrus().visit(node)
        return result

    def reference(node) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                return reference(ast.parse(node.value, mode="eval").body)
            except SyntaxError:
                return "?" + node.value
        if isinstance(node, ast.Name):
            for index in range(len(frames) - 1, -1, -1):
                frame, frame_kind = frames[index], frame_kinds[index]
                if node.id in frame_globals[index]:
                    if node.id in frames[0]:
                        return frames[0][node.id]
                    if node.id in _PY_BUILTINS:
                        return "__builtin__:" + node.id
                    return "?" + node.id
                if node.id in frame_nonlocals[index]:
                    for outer in range(index - 1, 0, -1):
                        if frame_kinds[outer] in {"function", "method"} and node.id in frames[outer]:
                            return frames[outer][node.id]
                    return "?" + node.id
                if frame_kind == "class" and frame_kinds[-1] != "class":
                    continue
                if node.id in frame:
                    return frame[node.id]
            if node.id in _PY_BUILTINS:
                return "__builtin__:" + node.id
            return "?" + node.id
        if isinstance(node, ast.Attribute):
            return reference(node.value) + "." + node.attr
        return "?" + ast.unparse(node)

    def emit(target: str, kind: str, line: int):
        if target.startswith("__builtin__:"):
            return
        edges.append(Edge(owner(), target, kind, line))
        if is_test and kind == "CALLS":
            edges.append(Edge(owner(), target, "TESTS", line))

    def annotation(node, origin_line=None):
        if node is None:
            return
        if isinstance(node, (ast.Name, ast.Attribute)):
            emit(
                reference(node),
                "USES_TYPE",
                origin_line if origin_line is not None else getattr(node, "lineno", 1),
            )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                annotation(ast.parse(node.value, mode="eval").body, node.lineno)
            except SyntaxError:
                emit("?" + node.value, "USES_TYPE", node.lineno)
        else:
            for child in ast.iter_child_nodes(node):
                annotation(child, origin_line)

    _CONTAINER_HEADS = {
        "list", "List", "set", "Set", "frozenset", "FrozenSet",
        "tuple", "Tuple", "Sequence", "Iterable", "Iterator",
    }

    def _type_targets(node, seen_aliases: frozenset[str] = frozenset()) -> tuple[str, ...]:
        if node is None:
            return ()
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                return _type_targets(ast.parse(node.value, mode="eval").body, seen_aliases)
            except SyntaxError:
                return ()
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            left = _type_targets(node.left, seen_aliases)
            right = _type_targets(node.right, seen_aliases)
            return tuple(dict.fromkeys([*left, *right]))
        if isinstance(node, ast.Subscript):
            head = (
                node.value.id
                if isinstance(node.value, ast.Name)
                else (node.value.attr if isinstance(node.value, ast.Attribute) else "")
            )
            if head in {"Optional", "Union"}:
                elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
                out: list[str] = []
                for elt in elts:
                    out.extend(_type_targets(elt, seen_aliases))
                return tuple(dict.fromkeys(out))
            if head in {"Annotated", "ClassVar", "Final"}:
                first = (
                    node.slice.elts[0]
                    if isinstance(node.slice, ast.Tuple) and node.slice.elts
                    else node.slice
                )
                return _type_targets(first, seen_aliases)
            if head in _CONTAINER_HEADS:
                return ()
            return _type_targets(node.value, seen_aliases)
        if isinstance(node, (ast.Name, ast.Attribute)):
            if isinstance(node, ast.Name) and node.id in {
                "None", "Any", "object", "str", "int", "float", "bool", "bytes",
                "list", "dict", "set", "tuple", "frozenset", "Sequence", "Mapping",
                "Iterable", "Iterator", "Callable", "Path",
            }:
                return ()
            ref = reference(node)
            if ref.startswith("?"):
                return ()
            if ref in type_aliases and ref not in seen_aliases:
                return type_aliases[ref]
            return (ref,)
        return ()

    def _iter_element_types(node, seen_aliases: frozenset[str] = frozenset()) -> tuple[str, ...]:
        if node is None:
            return ()
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                return _iter_element_types(ast.parse(node.value, mode="eval").body, seen_aliases)
            except SyntaxError:
                return ()
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            left = _iter_element_types(node.left, seen_aliases)
            right = _iter_element_types(node.right, seen_aliases)
            return tuple(dict.fromkeys([*left, *right]))
        if isinstance(node, ast.Subscript):
            head = (
                node.value.id
                if isinstance(node.value, ast.Name)
                else (node.value.attr if isinstance(node.value, ast.Attribute) else "")
            )
            if head in {"Optional", "Union", "Annotated", "ClassVar", "Final"}:
                elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
                out_u: list[str] = []
                for elt in elts:
                    out_u.extend(_iter_element_types(elt, seen_aliases))
                return tuple(dict.fromkeys(out_u))
            if head in _CONTAINER_HEADS:
                elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
                out_c: list[str] = []
                for elt in elts:
                    out_c.extend(_type_targets(elt, seen_aliases))
                return tuple(dict.fromkeys(out_c))
        if isinstance(node, (ast.Name, ast.Attribute)):
            ref = reference(node)
            if ref in elem_type_aliases and ref not in seen_aliases:
                return elem_type_aliases[ref]
        return ()

    def _extract_callable_elements(node) -> tuple[str, ...]:
        elts: list[ast.AST] = []
        if isinstance(node, ast.Dict):
            elts = [v for v in node.values if v is not None]
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            elts = list(node.elts)
        else:
            return ()
        targets: list[str] = []
        for elt in elts:
            if isinstance(elt, (ast.Name, ast.Attribute)):
                ref = reference(elt)
                if not ref.startswith("?"):
                    targets.append(ref)
            elif isinstance(elt, ast.Lambda) and isinstance(elt.body, ast.Call):
                ref = reference(elt.body.func)
                if not ref.startswith("?"):
                    targets.append(ref)
        return tuple(dict.fromkeys(targets))

    def _container_lookup_targets(node) -> tuple[str, ...]:
        # Match `TABLE.get(...)` or `TABLE[...]`
        container_node = None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
        ):
            container_node = node.func.value
        elif isinstance(node, ast.Subscript):
            container_node = node.value
        if isinstance(container_node, ast.Name):
            for lcalls in reversed(frame_local_callables):
                if container_node.id in lcalls:
                    return lcalls[container_node.id]
            return container_callables.get(container_node.id, ())
        return ()

    def _resolve_class_method(cls_target: str, method_name: str) -> str:
        if cls_target in class_methods or cls_target in class_bases:
            for base in compute_c3_mro(cls_target, class_bases):
                if method_name in class_methods.get(base, ()):
                    return f"{base}.{method_name}"
        return f"{cls_target}.{method_name}"

    def _expr_elem_types(node) -> tuple[str, ...]:
        if isinstance(node, ast.Name):
            for index in range(len(frame_local_elem_types) - 1, -1, -1):
                if frame_kinds[index] == "class" and frame_kinds[-1] != "class":
                    continue
                if node.id in frame_local_elem_types[index]:
                    return frame_local_elem_types[index][node.id]
            ref = reference(node)
            if not ref.startswith("?"):
                return elem_type_aliases.get(ref, ())
            return ()
        if isinstance(node, ast.Attribute):
            owner_types = _expr_types(node.value)
            out_e: list[str] = []
            for ot in owner_types:
                for cls_cand in compute_c3_mro(ot, class_bases):
                    ftypes = class_elem_fields.get(cls_cand, {}).get(node.attr, ())
                    for ft in ftypes:
                        out_e.extend(type_aliases.get(ft, (ft,)))
            return tuple(dict.fromkeys(out_e))
        if isinstance(node, ast.Call):
            out_ce: list[str] = []
            for cref in _call_targets(node.func):
                if cref in func_elem_returns:
                    out_ce.extend(func_elem_returns[cref])
            return tuple(dict.fromkeys(out_ce))
        return ()

    def _expr_types(node) -> tuple[str, ...]:
        if isinstance(node, ast.Name):
            for index in range(len(frame_local_types) - 1, -1, -1):
                if frame_kinds[index] == "class" and frame_kinds[-1] != "class":
                    continue
                if node.id in frame_local_types[index]:
                    return frame_local_types[index][node.id]
            ref = reference(node)
            if not ref.startswith("?"):
                return type_aliases.get(ref, (ref,))
            return ()
        if isinstance(node, ast.Attribute):
            owner_types = _expr_types(node.value)
            out: list[str] = []
            for ot in owner_types:
                for cls_cand in compute_c3_mro(ot, class_bases):
                    ftypes = class_fields.get(cls_cand, {}).get(node.attr, ())
                    for ft in ftypes:
                        out.extend(type_aliases.get(ft, (ft,)))
            return tuple(dict.fromkeys(out))
        if isinstance(node, ast.Subscript):
            return _expr_elem_types(node.value)
        if isinstance(node, ast.Call):
            out_call: list[str] = []
            for cref in _call_targets(node.func):
                if cref in func_returns:
                    out_call.extend(func_returns[cref])
                elif not cref.startswith("?") and (
                    cref in local_class_sids
                    or cref.rsplit(".", 1)[-1].rsplit(":", 1)[-1].rsplit("|", 1)[-1][:1].isupper()
                ):
                    out_call.append(cref)
            return tuple(dict.fromkeys(out_call))
        return ()

    def _call_targets(func_node) -> tuple[str, ...]:
        primary = reference(func_node)
        if not primary.startswith("?"):
            if "." in primary:
                cls_part, _, m_part = primary.rpartition(".")
                if cls_part in class_methods:
                    return (_resolve_class_method(cls_part, m_part),)
            if not (
                isinstance(func_node, ast.Attribute)
                and isinstance(func_node.value, (ast.Attribute, ast.Call, ast.Subscript))
            ):
                return (primary,)
        if isinstance(func_node, ast.Name):
            for index in range(len(frame_local_callables) - 1, -1, -1):
                if frame_kinds[index] == "class" and frame_kinds[-1] != "class":
                    continue
                if func_node.id in frame_local_callables[index]:
                    return frame_local_callables[index][func_node.id]
            return (primary,)
        if isinstance(func_node, (ast.Call, ast.Subscript)):
            c_targets = _container_lookup_targets(func_node)
            if c_targets:
                return c_targets
        if isinstance(func_node, ast.Attribute):
            recv_types = _expr_types(func_node.value)
            if recv_types:
                resolved: list[str] = []
                for rt in recv_types:
                    resolved.append(_resolve_class_method(rt, func_node.attr))
                    for base in (*compute_c3_mro(rt, class_bases), *structural_protocols_of.get(rt, ())):
                        if base in (protocol_classes | abc_classes) and func_node.attr in class_methods.get(base, ()):
                            resolved.append(f"{base}.{func_node.attr}")
                return tuple(dict.fromkeys(resolved))
        return (primary,)

    class Visitor(ast.NodeVisitor):
        def visit_Import(self, node):
            for alias in node.names:
                emit(alias.name + MODULE_END, "IMPORTS", node.lineno)

        def visit_ImportFrom(self, node):
            base = import_base(node)
            if base:
                emit(base + MODULE_END, "IMPORTS", node.lineno)
            for alias in node.names:
                emit(
                    base + MODULE_END if alias.name == "*" else f"{base}{MODULE_END}{alias.name}",
                    "IMPORTS",
                    node.lineno,
                )

        def definition(self, node):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            end = node.end_lineno or node.lineno
            sid = owner() + ("." if scope else "") + node.name
            previous = next((s for s in symbols if s.symbol_id == sid), None)
            if previous is not None:
                symbols.remove(previous)
                # Python's final definition binds the name; property accessors share one contract.
                accessor = any(
                    isinstance(d, ast.Attribute)
                    and d.attr in {"setter", "getter", "deleter"}
                    for d in node.decorator_list
                )
                if accessor:
                    start = previous.start
                else:
                    symbols[:] = [
                        s for s in symbols if not s.symbol_id.startswith(sid + ".")
                    ]
                    edges[:] = [
                        e
                        for e in edges
                        if e.source != sid and not e.source.startswith(sid + ".")
                    ]
                edges.append(
                    Edge(
                        sid,
                        "?multiple definitions; final binding retained",
                        "DIAGNOSTIC",
                        node.lineno,
                    )
                )
            is_class = isinstance(node, ast.ClassDef)
            kind = (
                "class"
                if is_class
                else (
                    "method"
                    if scope_kinds and scope_kinds[-1] == "class"
                    else "function"
                )
            )

            # Nested definitions have independent complexity; they do not increase their parent's cx.
            def branches(current):
                for child in ast.iter_child_nodes(current):
                    if isinstance(
                        child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    ):
                        continue
                    yield child
                    yield from branches(child)

            branch_types = (
                ast.If,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.Try,
                ast.TryStar,
                ast.With,
                ast.AsyncWith,
                ast.BoolOp,
                ast.IfExp,
                ast.Match,
                ast.comprehension,
            )
            cx = (
                1
                if is_class
                else 1 + sum(isinstance(n, branch_types) for n in branches(node))
            )
            symbols.append(
                Symbol(
                    sid,
                    kind,
                    _signature(node, text),
                    "\n".join(lines[start - 1 : end]),
                    start,
                    end,
                    cx,
                )
            )
            for decorator in node.decorator_list:
                self.visit(decorator)
            scope.append(node.name)
            scope_kinds.append(kind)
            local = bindings(node.body)
            local_types: dict[str, tuple[str, ...]] = {}
            local_elem_types: dict[str, tuple[str, ...]] = {}
            local_deferred_ret: dict[str, tuple[str, ...]] = {}
            local_callables: dict[str, tuple[str, ...]] = {}
            if not is_class:
                args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                args += [
                    a for a in (node.args.vararg, node.args.kwarg) if a is not None
                ]
                body_rebound = set(local.keys())
                for arg in args:
                    local[arg.arg] = "?" + arg.arg
                # A conventional receiver is statically bound only inside its defining class.
                if kind == "method" and args and args[0].arg in {"self", "cls"}:
                    cls_sid = module + ":" + ".".join(scope[:-1])
                    local[args[0].arg] = cls_sid
                    local_types[args[0].arg] = (cls_sid,)
                for arg in args:
                    annotation(arg.annotation)
                    if arg.annotation is not None and arg.arg not in body_rebound:
                        tt = _type_targets(arg.annotation)
                        if tt:
                            local_types[arg.arg] = tt
                        et = _iter_element_types(arg.annotation)
                        if et:
                            local_elem_types[arg.arg] = et
                annotation(node.returns)
                for default in [*node.args.defaults, *node.args.kw_defaults]:
                    if default is not None:
                        self.visit(default)
            else:
                for base in node.bases:
                    emit(reference(base), "INHERITS", base.lineno)
            frames.append(local)
            frame_kinds.append(kind)
            global_names, nonlocal_names = declarations(node.body) if not is_class else (set(), set())
            frame_globals.append(global_names)
            frame_nonlocals.append(nonlocal_names)
            frame_local_types.append(local_types)
            frame_local_elem_types.append(local_elem_types)
            frame_local_deferred_ret.append(local_deferred_ret)
            frame_local_callables.append(local_callables)
            if not is_class:
                for stmt in branches(node):
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        vname = stmt.target.id
                        tt = _type_targets(stmt.annotation)
                        if tt:
                            local_types[vname] = tt
                        et = _iter_element_types(stmt.annotation)
                        if et:
                            local_elem_types[vname] = et
                        if stmt.value is not None:
                            ct = _extract_callable_elements(stmt.value) or _container_lookup_targets(stmt.value)
                            if ct:
                                local_callables[vname] = ct
                    elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                        target_node = stmt.targets[0]
                        is_unpack = isinstance(target_node, (ast.Tuple, ast.List))
                        target_names = (
                            [target_node.id]
                            if isinstance(target_node, ast.Name)
                            else (
                                [e.id for e in target_node.elts if isinstance(e, ast.Name)]
                                if is_unpack
                                else []
                            )
                        )
                        if len(target_names) == 1 and not is_unpack:
                            ct = _extract_callable_elements(stmt.value) or _container_lookup_targets(stmt.value)
                            if ct:
                                prev_ct = local_callables.get(target_names[0], ())
                                local_callables[target_names[0]] = tuple(dict.fromkeys([*prev_ct, *ct]))
                        if target_names and isinstance(stmt.value, ast.Call):
                            if is_unpack:
                                unpack_types = _expr_elem_types(stmt.value)
                                if unpack_types:
                                    for vname in target_names:
                                        prev_ut = local_types.get(vname, ())
                                        local_types[vname] = tuple(dict.fromkeys([*prev_ut, *unpack_types]))
                            else:
                                ret_types: list[str] = []
                                ret_elem_types: list[str] = []
                                deferred_refs: list[str] = []
                                for cref in _call_targets(stmt.value.func):
                                    if cref in func_returns:
                                        ret_types.extend(func_returns[cref])
                                    elif not cref.startswith("?") and (
                                        cref in local_class_sids
                                        or cref.rsplit(".", 1)[-1].rsplit(":", 1)[-1].rsplit("|", 1)[-1][:1].isupper()
                                    ):
                                        ret_types.append(cref)
                                    elif not cref.startswith("?") and cref not in local_function_sids:
                                        deferred_refs.append(cref)
                                    if cref in func_elem_returns:
                                        ret_elem_types.extend(func_elem_returns[cref])
                                if ret_types:
                                    dedup_rt = tuple(dict.fromkeys(ret_types))
                                    for vname in target_names:
                                        prev_rt = local_types.get(vname, ())
                                        local_types[vname] = tuple(dict.fromkeys([*prev_rt, *dedup_rt]))
                                elif deferred_refs:
                                    dedup_dr = tuple(dict.fromkeys(deferred_refs))
                                    for vname in target_names:
                                        prev_dr = local_deferred_ret.get(vname, ())
                                        local_deferred_ret[vname] = tuple(dict.fromkeys([*prev_dr, *dedup_dr]))
                                if ret_elem_types:
                                    dedup_re = tuple(dict.fromkeys(ret_elem_types))
                                    for vname in target_names:
                                        prev_re = local_elem_types.get(vname, ())
                                        local_elem_types[vname] = tuple(dict.fromkeys([*prev_re, *dedup_re]))
                    elif isinstance(stmt, (ast.For, ast.AsyncFor)):
                        iter_types = list(_expr_elem_types(stmt.iter))
                        if iter_types:
                            dedup_it = tuple(dict.fromkeys(iter_types))
                            target_names = (
                                [stmt.target.id]
                                if isinstance(stmt.target, ast.Name)
                                else (
                                    [e.id for e in stmt.target.elts if isinstance(e, ast.Name)]
                                    if isinstance(stmt.target, (ast.Tuple, ast.List))
                                    else []
                                )
                            )
                            for vname in target_names:
                                prev_it = local_types.get(vname, ())
                                local_types[vname] = tuple(dict.fromkeys([*prev_it, *dedup_it]))
            for statement in node.body:
                self.visit(statement)
            frame_local_callables.pop()
            frame_local_deferred_ret.pop()
            frame_local_elem_types.pop()
            frame_local_types.pop()
            frames.pop()
            frame_kinds.pop()
            frame_globals.pop()
            frame_nonlocals.pop()
            scope_kinds.pop()
            scope.pop()

        visit_FunctionDef = definition
        visit_AsyncFunctionDef = definition
        visit_ClassDef = definition

        def visit_Lambda(self, node):
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if default is not None:
                    self.visit(default)
            local = {}
            args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            args += [a for a in (node.args.vararg, node.args.kwarg) if a is not None]
            for arg in args:
                local[arg.arg] = "?" + arg.arg
            for name in walrus_bindings(node.body):
                local[name] = "?" + name
            frames.append(local)
            frame_kinds.append("function")
            frame_globals.append(set())
            frame_nonlocals.append(set())
            frame_local_types.append({})
            frame_local_elem_types.append({})
            frame_local_deferred_ret.append({})
            frame_local_callables.append({})
            self.visit(node.body)
            frame_local_callables.pop()
            frame_local_deferred_ret.pop()
            frame_local_elem_types.pop()
            frame_local_types.pop()
            frames.pop()
            frame_kinds.pop()
            frame_globals.pop()
            frame_nonlocals.pop()

        def visit_comp_parts(self, generators, values):
            self.visit(generators[0].iter)
            frames.append({})
            frame_kinds.append("comprehension")
            frame_globals.append(set())
            frame_nonlocals.append(set())
            frame_local_types.append({})
            frame_local_elem_types.append({})
            frame_local_deferred_ret.append({})
            frame_local_callables.append({})

            def bind_target(target):
                if isinstance(target, ast.Name):
                    frames[-1][target.id] = "?" + target.id
                elif isinstance(target, (ast.Tuple, ast.List)):
                    for element in target.elts:
                        bind_target(element)
                elif isinstance(target, ast.Starred):
                    bind_target(target.value)

            for index, generator in enumerate(generators):
                if index:
                    self.visit(generator.iter)
                bind_target(generator.target)
                iter_types = list(_expr_elem_types(generator.iter))
                if iter_types:
                    dedup_it = tuple(dict.fromkeys(iter_types))
                    target_names = (
                        [generator.target.id]
                        if isinstance(generator.target, ast.Name)
                        else (
                            [e.id for e in generator.target.elts if isinstance(e, ast.Name)]
                            if isinstance(generator.target, (ast.Tuple, ast.List))
                            else []
                        )
                    )
                    for vname in target_names:
                        frame_local_types[-1][vname] = dedup_it
                for condition in generator.ifs:
                    self.visit(condition)
            for value in values:
                if value is not None:
                    self.visit(value)

            frame_local_callables.pop()
            frame_local_deferred_ret.pop()
            frame_local_elem_types.pop()
            frame_local_types.pop()
            frames.pop()
            frame_kinds.pop()
            frame_globals.pop()
            frame_nonlocals.pop()

        def visit_ListComp(self, node: ast.ListComp):
            self.visit_comp_parts(node.generators, (node.elt,))

        def visit_SetComp(self, node: ast.SetComp):
            self.visit_comp_parts(node.generators, (node.elt,))

        def visit_DictComp(self, node: ast.DictComp):
            self.visit_comp_parts(node.generators, (node.key, node.value))

        def visit_GeneratorExp(self, node: ast.GeneratorExp):
            self.visit_comp_parts(node.generators, (node.elt,))

        def visit_TypeAlias(self, node):
            sid = owner() + ("." if scope else "") + node.name.id
            end = node.end_lineno or node.lineno
            body = "\n".join(lines[node.lineno - 1 : end])
            symbols.append(Symbol(sid, "type_alias", body, body, node.lineno, end, 1))
            scope.append(node.name.id)
            annotation(node.value)
            scope.pop()

        def visit_AnnAssign(self, node):
            annotation(node.annotation)
            if node.value is not None:
                self.visit(node.value)

        def visit_Call(self, node):
            targets = _call_targets(node.func)
            deferred_emitted = False
            if isinstance(node.func, ast.Attribute):
                recv = node.func.value
                if len(targets) == 1 and targets[0].startswith("?") and isinstance(recv, ast.Name):
                    for index in range(len(frame_local_deferred_ret) - 1, -1, -1):
                        if frame_kinds[index] == "class" and frame_kinds[-1] != "class":
                            continue
                        if recv.id in frame_local_deferred_ret[index]:
                            for dref in frame_local_deferred_ret[index][recv.id]:
                                edges.append(Edge(owner(), f"RET\t{dref}\t{node.func.attr}\t{targets[0]}", "DEFERRED_CALL", node.lineno))
                                deferred_emitted = True
                            break
                elif isinstance(recv, ast.Attribute) and not _expr_types(recv):
                    if isinstance(recv.value, ast.Name) and any(
                        recv.value.id in flt for flt in frame_local_types
                    ):
                        for ot in _expr_types(recv.value):
                            if not ot.startswith("?"):
                                edges.append(Edge(owner(), f"FLD\t{ot}\t{recv.attr}\t{node.func.attr}\t{targets[0]}", "DEFERRED_CALL", node.lineno))
                                deferred_emitted = True
            if not deferred_emitted:
                for target in targets:
                    emit(target, "CALLS", node.lineno)
            for arg in [*node.args, *(kw.value for kw in node.keywords if kw.value is not None)]:
                if isinstance(arg, (ast.Name, ast.Attribute)):
                    arg_ref = reference(arg)
                    if not arg_ref.startswith("?") and arg_ref in local_function_sids:
                        emit(arg_ref, "CALLS", getattr(arg, "lineno", node.lineno))
            self.generic_visit(node)

        def visit_Constant(self, node):
            if (
                isinstance(node.value, str)
                and "." in node.value
                and node.value.partition(".")[0] in packages
            ):
                emit(node.value, "IMPORTS", node.lineno)

    frames.append(bindings(tree.body))
    # A function or class body that declares `global Name` and binds it makes the module name unknown.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for name in declarations(node.body)[0] & bindings(node.body).keys():
                frames[0][name] = "?" + name
    # Re-exports follow the module's own final bindings (HELP.md "Analysis limits").
    for name, target in frames[0].items():
        edges.append(Edge(f"{module}.{name}", target, "ALIAS", 0))
    frame_kinds.append("module")
    frame_globals.append(set())
    frame_nonlocals.append(set())
    frame_local_types.append({})
    frame_local_elem_types.append({})
    frame_local_deferred_ret.append({})
    frame_local_callables.append({})

    def _calc_arity(fn_node: ast.FunctionDef | ast.AsyncFunctionDef, is_method: bool) -> tuple[int, int | None]:
        pos = [*fn_node.args.posonlyargs, *fn_node.args.args]
        if is_method and pos and pos[0].arg in {"self", "cls"}:
            pos = pos[1:]
        total_pos = len(pos)
        min_pos = max(0, total_pos - len(fn_node.args.defaults))
        req_kw = sum(1 for d in fn_node.args.kw_defaults if d is None)
        min_total = min_pos + req_kw
        max_total = None if (fn_node.args.vararg or fn_node.args.kwarg) else (total_pos + len(fn_node.args.kwonlyargs))
        return (min_total, max_total)

    def _arity_compatible(p_arity: tuple[int, int | None], c_arity: tuple[int, int | None]) -> bool:
        p_min, p_max = p_arity
        c_min, c_max = c_arity
        if c_min > p_min and (p_max is None or c_min > p_max):
            return False
        if c_max is not None and c_max < p_min:
            return False
        return True

    # Pre-scan top-level classes, methods, type aliases, fields, and dispatch tables
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local_function_sids.add(f"{module}:{stmt.name}")
        elif isinstance(stmt, ast.ClassDef):
            csid = f"{module}:{stmt.name}"
            local_class_sids.add(csid)
            class_bases[csid] = tuple(
                ref for b in stmt.bases if not (ref := reference(b)).startswith("?")
            )
            mset: set[str] = set()
            for member in stmt.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    mset.add(member.name)
                    local_function_sids.add(f"{csid}.{member.name}")
                    marity = _calc_arity(member, True)
                    method_arity[(csid, member.name)] = marity
                    edges.append(
                        Edge(
                            f"{csid}.{member.name}",
                            f"{marity[0]}\t{'' if marity[1] is None else marity[1]}",
                            "METHOD_ARITY",
                            0,
                        )
                    )
            class_methods[csid] = mset
            if any(
                (isinstance(b, ast.Name) and b.id == "Protocol")
                or (isinstance(b, ast.Attribute) and b.attr == "Protocol")
                for b in stmt.bases
            ):
                protocol_classes.add(csid)
            elif any(
                (isinstance(b, ast.Name) and b.id in {"ABC", "ABCMeta"})
                or (isinstance(b, ast.Attribute) and b.attr in {"ABC", "ABCMeta"})
                for b in stmt.bases
            ):
                abc_classes.add(csid)
    for asid in sorted(abc_classes):
        for m in sorted(class_methods.get(asid, ())):
            if not m.startswith("_"):
                edges.append(Edge(asid, m, "ABC_METHOD", 0))
        impls = sorted(
            csid
            for csid in (local_class_sids - abc_classes - protocol_classes)
            if asid in compute_c3_mro(csid, class_bases)
        )
        if impls:
            type_aliases[asid] = (asid, *impls)
    for psid in sorted(protocol_classes):
        req_methods = {m for m in class_methods.get(psid, ()) if not m.startswith("_")}
        if req_methods:
            for m in sorted(req_methods):
                p_min, p_max = method_arity.get((psid, m), (0, None))
                edges.append(
                    Edge(
                        psid,
                        f"{m}\t{p_min}\t{'' if p_max is None else p_max}",
                        "PROTOCOL_METHOD",
                        0,
                    )
                )
            impls = sorted(
                csid
                for csid in (local_class_sids - protocol_classes - abc_classes)
                if psid in compute_c3_mro(csid, class_bases)
                or (
                    req_methods <= class_methods.get(csid, set())
                    and all(
                        _arity_compatible(
                            method_arity.get((psid, m), (0, None)),
                            method_arity.get((csid, m), (0, None)),
                        )
                        for m in req_methods
                    )
                )
            )
            if impls:
                type_aliases[psid] = (psid, *impls)
                for impl_sid in impls:
                    if psid not in structural_protocols_of.get(impl_sid, ()):
                        structural_protocols_of[impl_sid] = (*structural_protocols_of.get(impl_sid, ()), psid)
    for stmt in tree.body:
        if isinstance(stmt, ast.TypeAlias):
            tsid = f"{module}:{stmt.name.id}"
            tt = _type_targets(stmt.value, frozenset({tsid}))
            if tt:
                type_aliases[tsid] = tt
            et = _iter_element_types(stmt.value, frozenset({tsid}))
            if et:
                elem_type_aliases[tsid] = et
        elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            tname = stmt.targets[0].id
            tsid = f"{module}:{tname}"
            if isinstance(stmt.value, (ast.Subscript, ast.BinOp)):
                tt = _type_targets(stmt.value, frozenset({tsid}))
                if tt:
                    type_aliases[tsid] = tt
                    frames[0][tname] = tsid
                et = _iter_element_types(stmt.value, frozenset({tsid}))
                if et:
                    elem_type_aliases[tsid] = et
            ct = _extract_callable_elements(stmt.value)
            if ct:
                container_callables[tname] = ct
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            ct = _extract_callable_elements(stmt.value)
            if ct:
                container_callables[stmt.target.id] = ct
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.returns is not None:
            fsid = f"{module}:{stmt.name}"
            rt = _type_targets(stmt.returns)
            if rt:
                func_returns[fsid] = rt
                for r in rt:
                    edges.append(Edge(fsid, r, "RETURN_TYPE", 0))
            ert = _iter_element_types(stmt.returns)
            if ert:
                func_elem_returns[fsid] = ert
        elif isinstance(stmt, ast.ClassDef):
            csid = f"{module}:{stmt.name}"
            fmap: dict[str, tuple[str, ...]] = {}
            efmap: dict[str, tuple[str, ...]] = {}
            for member in stmt.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.returns is not None:
                    msid = f"{csid}.{member.name}"
                    mrt = _type_targets(member.returns)
                    if mrt:
                        func_returns[msid] = mrt
                        for r in mrt:
                            edges.append(Edge(msid, r, "RETURN_TYPE", 0))
                    mert = _iter_element_types(member.returns)
                    if mert:
                        func_elem_returns[msid] = mert
                if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
                    tt = _type_targets(member.annotation)
                    if tt:
                        fmap[member.target.id] = tt
                    et = _iter_element_types(member.annotation)
                    if et:
                        efmap[member.target.id] = et
                elif isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name == "__init__":
                    init_args = [*member.args.posonlyargs, *member.args.args, *member.args.kwonlyargs]
                    self_name = init_args[0].arg if init_args else "self"
                    param_types: dict[str, tuple[str, ...]] = {}
                    param_elem_types: dict[str, tuple[str, ...]] = {}
                    for a in init_args[1:]:
                        if a.annotation is not None:
                            att = _type_targets(a.annotation)
                            if att:
                                param_types[a.arg] = att
                            aet = _iter_element_types(a.annotation)
                            if aet:
                                param_elem_types[a.arg] = aet
                    for istmt in member.body:
                        if (
                            isinstance(istmt, ast.AnnAssign)
                            and isinstance(istmt.target, ast.Attribute)
                            and isinstance(istmt.target.value, ast.Name)
                            and istmt.target.value.id == self_name
                        ):
                            att = _type_targets(istmt.annotation)
                            if att:
                                fmap[istmt.target.attr] = att
                            aet = _iter_element_types(istmt.annotation)
                            if aet:
                                efmap[istmt.target.attr] = aet
                        elif (
                            isinstance(istmt, ast.Assign)
                            and len(istmt.targets) == 1
                            and isinstance(istmt.targets[0], ast.Attribute)
                            and isinstance(istmt.targets[0].value, ast.Name)
                            and istmt.targets[0].value.id == self_name
                        ):
                            fname = istmt.targets[0].attr
                            if isinstance(istmt.value, ast.Name):
                                if istmt.value.id in param_types:
                                    fmap[fname] = param_types[istmt.value.id]
                                if istmt.value.id in param_elem_types:
                                    efmap[fname] = param_elem_types[istmt.value.id]
                            elif isinstance(istmt.value, ast.Call) and isinstance(
                                istmt.value.func, (ast.Name, ast.Attribute)
                            ):
                                cref = reference(istmt.value.func)
                                if not cref.startswith("?"):
                                    fmap[fname] = (cref,)
            class_fields[csid] = fmap
            class_elem_fields[csid] = efmap
            for fname, fts in fmap.items():
                for ft in fts:
                    edges.append(Edge(f"{csid}.{fname}", ft, "FIELD_TYPE", 0))

    Visitor().visit(tree)
    skeleton = ast.unparse(_Strip().visit(copy.deepcopy(tree))) + "\n"
    symbols.insert(
        0, Symbol(module + ":", "module", module, text, 1, max(1, len(lines)), 1)
    )
    return FileAnalysis(
        path,
        module,
        hashlib.sha256(source).hexdigest(),
        skeleton,
        tuple(symbols),
        tuple(sorted(set(edges), key=lambda e: (e.source, e.target, e.kind, e.line))),
    )
