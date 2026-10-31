"""Read-only flow queries over the exact source observed by a GraphStore."""

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
    return slice_flow(graph, variable=variable, line=line, direction=direction,
                      include_control=include_control, max_tokens=max_tokens)
