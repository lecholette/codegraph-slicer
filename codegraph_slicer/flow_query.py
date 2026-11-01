"""Read-only flow queries over the exact source observed by a GraphStore."""

import ast

from .flow import FlowQueryError, analyze_flow, slice_flow
from .slicer import _anchor


def query_flow(store, anchor, *, variable=None, line=None, direction="backward",
               include_control=True, max_tokens=8000):
    row = _anchor(store, anchor)
    if row["kind"] not in {"function", "method"}:
        raise FlowQueryError(f"flow requires an observed function or method anchor: {anchor}")
    modules = store.rows(
        "SELECT body FROM symbols WHERE file_path=? AND kind='module'",
        (row["file_path"],),
    )
    if len(modules) != 1:
        raise FlowQueryError(f"expected exactly one cached module source for {row['file_path']}")
    files = store.rows("SELECT blob_sha FROM files WHERE path=?", (row["file_path"],))
    if len(files) != 1:
        raise FlowQueryError(f"expected exactly one observed file identity for {row['file_path']}")
    graph = analyze_flow(modules[0]["body"], row["qualname"], path=row["file_path"])
    graph["identity"] = {
        "symbol_id": row["symbol_id"],
        "path": row["file_path"],
        "blob_sha": files[0]["blob_sha"],
    }
    try:
        return slice_flow(graph, variable=variable, line=line, direction=direction,
                          include_control=include_control, max_tokens=max_tokens)
    except FlowQueryError as error:
        if variable is not None:
            tree = ast.parse(modules[0]["body"], filename=row["file_path"])
            target = next((node for node in ast.walk(tree)
                           if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and min([node.lineno] + [decorator.lineno
                                                    for decorator in node.decorator_list])
                           == row["start_line"]
                           and node.name == row["qualname"].rsplit(".", 1)[-1]), None)
            lines = set()
            if target is not None:
                lines.update(arg.lineno for arg in (*target.args.posonlyargs,
                              *target.args.args, *target.args.kwonlyargs,
                              *((target.args.vararg,) if target.args.vararg else ()),
                              *((target.args.kwarg,) if target.args.kwarg else ()))
                             if arg.arg == variable)

                def visit(node):
                    for child in ast.iter_child_nodes(node):
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                              ast.ClassDef, ast.Lambda)):
                            continue
                        if isinstance(child, ast.Name) and child.id == variable:
                            lines.add(child.lineno)
                        visit(child)

                visit(target)
            error.observed_lines = sorted(lines)[:5]
        raise
