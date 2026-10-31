"""Bounded contexts. The budget counts compact JSON of the sections value."""

import json
from .ast_extractor import CodeGraphError


class AnchorNotFoundError(CodeGraphError):
    pass


class InvalidBudgetError(CodeGraphError):
    pass


class RequiredAnchorOverflow(CodeGraphError):
    pass


def serialized_tokens(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) // 4 + 1


def _anchor(store, anchor):
    exact = store.rows("SELECT * FROM symbols WHERE symbol_id=?", (anchor,))
    if exact:
        return exact[0]
    path, separator, target = anchor.rpartition(":")
    if separator and path.endswith(".py"):
        if target.isdigit() and int(target) > 0:
            rows = store.rows(
                """SELECT * FROM symbols WHERE file_path=?
                AND start_line<=? AND end_line>=?
                ORDER BY end_line-start_line, length(qualname) DESC, symbol_id""",
                (path, int(target), int(target)),
            )
        else:
            rows = store.rows(
                "SELECT * FROM symbols WHERE file_path=? AND qualname=?", (path, target)
            )
        if len(rows) >= 1:
            return rows[0]
    raise AnchorNotFoundError(anchor)


def slice_context(store, anchors, max_tokens=8000, include_callee_bodies=False):
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens <= 0
    ):
        raise InvalidBudgetError("max_tokens must be a positive integer")
    if not anchors:
        raise AnchorNotFoundError("at least one anchor is required")
    chosen = {row["symbol_id"]: row for row in (_anchor(store, a) for a in anchors)}
    sections = {
        name: []
        for name in (
            "types_and_schemas",
            "upstream_callers",
            "target_implementations",
            "downstream_dependencies",
        )
    }

    def item(row, full):
        text = row["body"] if full else row["signature"]
        return {
            "symbol": row["symbol_id"],
            "text": f"{row['file_path']}:{row['start_line']}-{row['end_line']}\n{text}",
        }

    types = {}
    callers = {}
    callees = {}
    for sid, row in sorted(chosen.items()):
        sections["target_implementations"].append(item(row, True))
        for related in store.rows(
            """WITH RECURSIVE closure(node,depth) AS (
                SELECT ?,0 UNION SELECT e.target_id,c.depth+1 FROM edges e
                JOIN closure c ON e.source_id=c.node
                WHERE e.kind IN ('USES_TYPE','INHERITS') AND c.depth<2
            ) SELECT DISTINCT s.* FROM symbols s JOIN closure c ON s.symbol_id=c.node
                WHERE c.depth>0 AND s.kind IN ('class','type_alias') ORDER BY s.symbol_id""",
            (sid,),
        ):
            if related["symbol_id"] not in chosen:
                types[related["symbol_id"]] = related
        for related in store.rows(
            """SELECT DISTINCT s.* FROM symbols s JOIN edges e
                ON s.symbol_id=e.source_id WHERE e.target_id=? AND e.kind='CALLS' """,
            (sid,),
        ):
            if related["symbol_id"] not in chosen:
                callers[related["symbol_id"]] = related
        for related in store.rows(
            """SELECT DISTINCT s.* FROM symbols s JOIN edges e
                ON s.symbol_id=e.target_id WHERE e.source_id=? AND e.kind='CALLS' """,
            (sid,),
        ):
            if related["symbol_id"] not in chosen and related["symbol_id"] not in types:
                callees[related["symbol_id"]] = related
    callees = {sid: row for sid, row in callees.items() if sid not in types}
    sections["types_and_schemas"] = [
        item(row, True) for _, row in sorted(types.items())
    ]
    required = serialized_tokens(sections)
    if required > max_tokens:
        raise RequiredAnchorOverflow(
            f"required definitions need {required}, budget is {max_tokens}"
        )
    from .pagerank import rank

    scores = rank(store)
    for name, candidates in (
        ("upstream_callers", callers),
        ("downstream_dependencies", callees),
    ):
        for sid in sorted(candidates, key=lambda s: (-scores.get(s, 0), s)):
            full = name == "downstream_dependencies" and include_callee_bodies
            candidate = item(candidates[sid], full)
            sections[name].append(candidate)
            if serialized_tokens(sections) > max_tokens:
                sections[name].pop()
                if full:
                    sections[name].append(item(candidates[sid], False))
                    if serialized_tokens(sections) > max_tokens:
                        sections[name].pop()
    return {"tokens": serialized_tokens(sections), "sections": sections}
