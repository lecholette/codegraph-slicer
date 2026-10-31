"""Conservative Python syntax extraction; no runtime or inferred dispatch."""

import ast
import copy
import hashlib
import io
import tokenize
from dataclasses import dataclass
from pathlib import PurePosixPath


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


def plain_target(target: str) -> str:
    """Return an import-tagged target as a plain dotted name."""
    module, end, rest = target.partition(MODULE_END)
    return ".".join(part for part in (module, rest.lstrip(".")) if part) if end else target


def valid_path(path: str) -> str:
    p = PurePosixPath(path)
    if (
        p.is_absolute()
        or ".." in p.parts
        or "\\" in path
        or any(ord(c) < 32 for c in path)
        or not path.endswith(".py")
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


class _Strip(ast.NodeTransformer):
    def _function(self, node):
        doc = node.body[:1] if ast.get_docstring(node, clean=False) is not None else []
        node.body = doc + [ast.Expr(value=ast.Constant(value=Ellipsis))]
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
    path: str, source: bytes, packages: frozenset[str] = frozenset(), source_root: str = ""
) -> FileAnalysis:
    """Extract one file; string constants naming a module in `packages` become IMPORTS."""
    valid_path(path)
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
    is_test = any(
        part == "tests" for part in PurePosixPath(path).parts
    ) or PurePosixPath(path).name.startswith("test_")

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
                    return frames[0].get(node.id, "?" + node.id)
                if node.id in frame_nonlocals[index]:
                    for outer in range(index - 1, 0, -1):
                        if frame_kinds[outer] in {"function", "method"} and node.id in frames[outer]:
                            return frames[outer][node.id]
                    return "?" + node.id
                if frame_kind == "class" and frame_kinds[-1] != "class":
                    continue
                if node.id in frame:
                    return frame[node.id]
            return "?" + node.id
        if isinstance(node, ast.Attribute):
            return reference(node.value) + "." + node.attr
        return "?" + ast.unparse(node)

    def emit(target: str, kind: str, line: int):
        edges.append(Edge(owner(), target, kind, line))
        if is_test and kind in {"CALLS", "IMPORTS"}:
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
            if not is_class:
                args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                args += [
                    a for a in (node.args.vararg, node.args.kwarg) if a is not None
                ]
                for arg in args:
                    local[arg.arg] = "?" + arg.arg
                # A conventional receiver is statically bound only inside its defining class.
                if kind == "method" and args and args[0].arg in {"self", "cls"}:
                    local[args[0].arg] = module + ":" + ".".join(scope[:-1])
                for arg in args:
                    annotation(arg.annotation)
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
            for statement in node.body:
                self.visit(statement)
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
            self.visit(node.body)
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
                for condition in generator.ifs:
                    self.visit(condition)
            for value in values:
                if value is not None:
                    self.visit(value)

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
            emit(reference(node.func), "CALLS", node.lineno)
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
