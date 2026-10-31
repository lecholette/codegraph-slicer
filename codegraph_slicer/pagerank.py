"""Deterministic personalized PageRank and one skeleton per indexed file."""

from .ast_extractor import CodeGraphError
from .slicer import InvalidBudgetError, serialized_tokens


class UnknownSeedError(CodeGraphError):
    pass


def rank(store, seeds=(), alpha=0.85, iterations=40, *, seed_paths=(), seed_modules=()):
    nodes = sorted(
        row["symbol_id"] for row in store.rows("SELECT symbol_id FROM symbols")
    )
    graph = {node: set() for node in nodes}
    for edge in store.rows("""SELECT source_id,target_id FROM edges
            WHERE kind IN ('CALLS','IMPORTS','USES_TYPE','INHERITS','DEFINES')"""):
        if edge["source_id"] in graph and edge["target_id"] in graph:
            graph[edge["source_id"]].add(edge["target_id"])
    seed_nodes = set()
    for seed in (*seeds, *seed_paths, *seed_modules):
        found = store.rows(
            "SELECT symbol_id FROM symbols WHERE module=? OR file_path=?", (seed, seed)
        )
        if not found:
            raise UnknownSeedError(seed)
        seed_nodes.update(row["symbol_id"] for row in found)
    if not nodes:
        return {}
    if not seed_nodes:
        seed_nodes = set(nodes)
    personalization = {n: 1 / len(seed_nodes) if n in seed_nodes else 0 for n in nodes}
    scores = personalization.copy()
    for _ in range(iterations):
        dangling = sum(scores[n] for n in nodes if not graph[n])
        new = {n: (1 - alpha + alpha * dangling) * personalization[n] for n in nodes}
        for source in nodes:
            for target in sorted(graph[source]):
                new[target] += alpha * scores[source] / len(graph[source])
        if max(abs(new[n] - scores[n]) for n in nodes) < 1e-7:
            scores = new
            break
        scores = new
    return dict(sorted(scores.items(), key=lambda pair: (-pair[1], pair[0])))


def build_repo_map(store, seeds=(), max_tokens=2000, *, seed_paths=(), seed_modules=()):
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens <= 0
    ):
        raise InvalidBudgetError("max_tokens must be a positive integer")
    scores = rank(store, seeds, seed_paths=seed_paths, seed_modules=seed_modules)
    by_path = {row["path"]: row for row in store.rows("SELECT * FROM files")}
    symbols = {
        row["symbol_id"]: row
        for row in store.rows("SELECT symbol_id,file_path FROM symbols")
    }
    file_scores = {path: 0.0 for path in by_path}
    for sid, score in scores.items():
        file_scores[symbols[sid]["file_path"]] += score
    output = []
    if serialized_tokens(output) > max_tokens:
        raise InvalidBudgetError("budget cannot contain an empty map")
    for path in sorted(file_scores, key=lambda p: (-file_scores[p], p)):
        row = by_path[path]
        output.append(
            {
                "symbol": row["module"] + ":",
                "path": path,
                "text": f"{path}:1\n{row['skeleton']}",
            }
        )
        if serialized_tokens(output) > max_tokens:
            output.pop()
    return {"tokens": serialized_tokens(output), "items": output}
