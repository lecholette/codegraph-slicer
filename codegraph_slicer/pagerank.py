"""Deterministic personalized PageRank and one skeleton per indexed file."""

import json

from .ast_extractor import CodeGraphError
from .slicer import InvalidBudgetError, _with_output_tokens, tokens_for_length


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


def build_repo_map(
    store, seeds=(), max_tokens=2000, *, seed_paths=(), seed_modules=(),
    render_estimator=None,
):
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
    seeded = bool(seeds or seed_paths or seed_modules)
    if seeded:
        file_scores = {path: score for path, score in file_scores.items() if score != 0}
    output = []
    total_files = len(by_path)
    ranked_paths = sorted(file_scores, key=lambda p: (-file_scores[p], p))
    top_file = ranked_paths[0] if ranked_paths else None

    def result(items, top_file_omitted=None):
        payload = {"tokens": 0, "items": items}
        omitted = total_files - len(items)
        if omitted:
            payload["omitted"] = {"files": omitted}
            payload["retrieve_details"] = "rerun with --max-tokens N"
        if top_file_omitted is not None:
            payload["top_file_omitted"] = top_file_omitted
        return _with_output_tokens(payload, render_estimator)

    complete_items = [
        {
            "symbol": by_path[path]["module"] + ":",
            "path": path,
            "text": f"{path}:1\n{by_path[path]['skeleton']}",
        }
        for path in ranked_paths
    ]
    # The default JSON representation is additive: each item is encoded once,
    # then candidate prefixes are measured from character counts. Keep the
    # general estimator path below for callers with arbitrary renderers.
    map_estimator = render_estimator if hasattr(render_estimator, "estimate_map") else None
    if render_estimator is None or map_estimator is not None:
        item_costs = (
            [map_estimator.item_cost(item) for item in complete_items]
            if map_estimator is not None
            else [
                len(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
                for item in complete_items
            ]
        )

        def estimate_cost(item_cost, count, top_file_omitted=None):
            if map_estimator is not None:
                try:
                    return map_estimator.estimate_map(
                        item_cost, count, total_files - count, top_file_omitted
                    )
                except TypeError:
                    return map_estimator.estimate_map(item_cost, count, total_files - count)
            # Match compact JSON key ordering and escaping exactly, including
            # the token and omitted-count digit transitions and final newline.
            size = (
                len('{"tokens":') + len(",\"items\":[")
                + item_cost + max(0, count - 1)
                + len("]}")
            )
            omitted = total_files - count
            if omitted:
                size += (
                    len(',"omitted":{"files":') + len(str(omitted))
                    + len('},"retrieve_details":"rerun with --max-tokens N"')
                )
            if top_file_omitted is not None:
                size += len(',"top_file_omitted":') + len(json.dumps(
                    top_file_omitted, ensure_ascii=False, separators=(",", ":")
                ))
            # json output has one printed newline. rendered_tokens' integer
            # arithmetic includes it without constructing the output string.
            tokens = 0
            while True:
                estimate = tokens_for_length(size + len(str(tokens)) + 1)
                if estimate == tokens:
                    return estimate
                tokens = estimate

        complete_cost = sum(item_costs)
        complete_count = len(complete_items)
        complete_tokens = estimate_cost(complete_cost, complete_count)
        if complete_tokens <= max_tokens:
            return result(complete_items)
        selected = []
        selected_cost = 0
        skipped_top = None
        for index, item_cost in enumerate(item_costs):
            candidate_count = len(selected) + 1
            if estimate_cost(selected_cost + item_cost, candidate_count, skipped_top) <= max_tokens:
                selected.append(complete_items[index])
                selected_cost += item_cost
            elif seeded and index == 0:
                # Iterate because the max_tokens field contributes to the output it describes.
                threshold = estimate_cost(item_cost, 1)
                for _ in range(4):
                    candidate = {"path": top_file, "max_tokens": threshold}
                    updated = estimate_cost(item_cost, 1, candidate)
                    if updated == threshold:
                        break
                    threshold = updated
                skipped_top = {"path": top_file, "max_tokens": threshold}
        final = result(selected, skipped_top)
        if final["tokens"] > max_tokens:
            raise InvalidBudgetError(
                f"map omission metadata requires {final['tokens']} tokens; budget is {max_tokens}"
            )
        return final

    complete = result(complete_items)
    if complete["tokens"] <= max_tokens:
        return complete
    skipped_top = None
    for index, item in enumerate(complete_items):
        output.append(item)
        if result(output, skipped_top)["tokens"] > max_tokens:
            output.pop()
            if seeded and index == 0:
                threshold = result([item])["tokens"]
                for _ in range(4):
                    candidate = {"path": top_file, "max_tokens": threshold}
                    updated = result([item], candidate)["tokens"]
                    if updated == threshold:
                        break
                    threshold = updated
                skipped_top = {"path": top_file, "max_tokens": threshold}
    final = result(output, skipped_top)
    if final["tokens"] > max_tokens:
        raise InvalidBudgetError(
            f"map omission metadata requires {final['tokens']} tokens; budget is {max_tokens}"
        )
    return final


def build_repo_outline(
    store, seeds=(), max_tokens=2000, *, seed_paths=(), seed_modules=(),
    render_estimator=None,
):
    """Build a versioned, bounded entity outline with local file IDs."""
    from .slicer import rendered_tokens

    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        raise InvalidBudgetError("max_tokens must be a positive integer")
    files = {row["path"]: row for row in store.rows("SELECT * FROM files")}
    symbol_rows = store.rows(
        "SELECT symbol_id,file_path,qualname,kind,start_line,signature FROM symbols "
        "ORDER BY file_path,start_line,symbol_id"
    )
    seeds = tuple(seeds)
    seed_paths = tuple(seed_paths)
    seed_modules = tuple(seed_modules)
    all_seeds = (*seeds, *seed_paths, *seed_modules)
    score_by_symbol = rank(
        store, seeds, seed_paths=seed_paths, seed_modules=seed_modules
    )
    file_scores = {path: 0.0 for path in files}
    file_by_symbol = {row["symbol_id"]: row["file_path"] for row in symbol_rows}
    rows_by_path = {path: [] for path in files}
    for row in symbol_rows:
        rows_by_path[row["file_path"]].append(row)
    for symbol_id, score in score_by_symbol.items():
        file_scores[file_by_symbol[symbol_id]] += score
    all_paths = sorted(file_scores, key=lambda path: (-file_scores[path], path))
    seeded = bool(all_seeds)
    candidate_paths = [
        path for path in all_paths if not seeded or file_scores[path] != 0
    ]
    seed_order_by_symbol = {}
    for seed_order, seed in enumerate(all_seeds):
        for row in store.rows(
            "SELECT symbol_id FROM symbols WHERE module=? OR file_path=?", (seed, seed)
        ):
            seed_order_by_symbol.setdefault(row["symbol_id"], seed_order)
    seed_ids = set(seed_order_by_symbol)
    seed_file_paths = {file_by_symbol[symbol_id] for symbol_id in seed_ids}
    seed_order_by_file = {}
    for symbol_id, seed_order in seed_order_by_symbol.items():
        path = file_by_symbol[symbol_id]
        seed_order_by_file[path] = min(seed_order_by_file.get(path, seed_order), seed_order)
    ranked_paths = sorted(
        candidate_paths,
        key=lambda path: (
            0 if path in seed_file_paths else 1,
            seed_order_by_file.get(path, len(all_seeds)),
            -file_scores[path], path,
        ),
    )
    ranked_symbols = sorted(symbol_rows, key=lambda row: (
        0 if row["symbol_id"] in seed_order_by_symbol else 1,
        seed_order_by_symbol.get(row["symbol_id"], len(all_seeds)),
        -file_scores.get(row["file_path"], 0.0), row["file_path"],
        row["start_line"], row["symbol_id"],
    ))
    ids = {}
    file_ids = {path: f"f{index:04d}" for index, path in enumerate(all_paths, 1)}
    entities = {}
    entity_by_symbol = {}
    for path in all_paths:
        rows = rows_by_path[path]
        ids[path] = {row["symbol_id"]: f"e{index:04d}" for index, row in enumerate(rows, 1)}
        display_rows = sorted(rows, key=lambda row: (
            0 if row["symbol_id"] in seed_order_by_symbol else 1,
            seed_order_by_symbol.get(row["symbol_id"], len(all_seeds)),
            row["start_line"], row["symbol_id"],
        ))
        entities[path] = [
            {
                "id": ids[path][row["symbol_id"]],
                "name": row["qualname"].rsplit(".", 1)[-1],
                "kind": row["kind"],
                "signature": row["signature"],
                "line": row["start_line"],
            }
            for row in display_rows
        ]
        entities_by_id = {entity["id"]: entity for entity in entities[path]}
        for row in display_rows:
            entity_by_symbol[row["symbol_id"]] = entities_by_id[
                ids[path][row["symbol_id"]]
            ]
    edge_rows = store.rows(
        "SELECT source_id,target_id,kind FROM edges WHERE kind IN "
        "('CALLS','IMPORTS','USES_TYPE','INHERITS','DEFINES') ORDER BY source_id,target_id,kind"
    )
    all_edges = []
    symbol_refs = {
        symbol_id: f"{file_ids[path]}:{ids[path][symbol_id]}"
        for path in all_paths for symbol_id in ids[path]
    }
    for edge in edge_rows:
        source_path = file_by_symbol.get(edge["source_id"])
        target_path = file_by_symbol.get(edge["target_id"])
        if source_path not in ids or target_path not in ids:
            continue
        all_edges.append({
            "from": symbol_refs[edge["source_id"]],
            "to": symbol_refs[edge["target_id"]],
            "kind": edge["kind"],
        })

    # Budget entities in seed priority order. Omitted records point to the
    # complete candidate budget, which is guaranteed to admit every candidate.
    ordered_entities = [
        (row["file_path"], entity_by_symbol[row["symbol_id"]])
        for row in ranked_symbols if row["file_path"] in ranked_paths
    ]
    selected = set()
    selected_empty_files = set()

    def make_payload(selected_pairs, omission_tokens=None, empty_files=None):
        empty_files = selected_empty_files if empty_files is None else empty_files
        selected_by_path = {}
        for path in ranked_paths:
            for entity in entities[path]:
                if (path, entity["id"]) in selected_pairs:
                    selected_by_path.setdefault(path, []).append(entity)
        listed_refs = {
            f"{file_ids[path]}:{item['id']}"
            for path, items in selected_by_path.items() for item in items
        }
        kept_edges = [
            edge for edge in all_edges
            if edge["from"] in listed_refs and edge["to"] in listed_refs
        ]
        skipped_files = []
        skipped_entities = []
        for path in ranked_paths:
            selected_in_file = {entity["id"] for entity in selected_by_path.get(path, [])}
            if not selected_in_file and path not in empty_files:
                skipped_files.append({"path": path, "max_tokens": (omission_tokens or {}).get(("file", path), 0)})
            for entity in entities[path]:
                if entity["id"] not in selected_in_file:
                    skipped_entities.append({
                        "path": path, "id": entity["id"], "name": entity["name"],
                        "max_tokens": (omission_tokens or {}).get(("entity", path, entity["id"]), 0),
                    })
        payload = {
            "schema_version": 1,
            "tokens": 0,
            "files": [
                {"id": file_ids[path], "path": path, "entities": selected_by_path.get(path, [])}
                for path in ranked_paths
                if path in selected_by_path or path in empty_files
            ],
            "edges": kept_edges,
            "omitted": {"files": skipped_files, "entities": skipped_entities},
        }
        if not skipped_files:
            del payload["omitted"]["files"]
        if not skipped_entities:
            del payload["omitted"]["entities"]
        if not payload["omitted"]:
            del payload["omitted"]
        return _with_output_tokens(payload, render_estimator)

    def printed_tokens(payload):
        if render_estimator is None:
            import json
            return rendered_tokens(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return render_estimator(payload)

    empty_paths = [path for path in ranked_paths if not entities[path]]

    complete_pairs = {
        (path, entity["id"])
        for path in ranked_paths for entity in entities[path]
    }
    complete_result = make_payload(complete_pairs, empty_files=set(empty_paths))
    complete_tokens = complete_result["tokens"]
    if complete_tokens <= max_tokens:
        return complete_result

    for path, entity in ordered_entities:
        candidate = set(selected)
        candidate.add((path, entity["id"]))
        if printed_tokens(make_payload(candidate)) <= max_tokens:
            selected = candidate
    for path in empty_paths:
        candidate = set(selected_empty_files)
        candidate.add(path)
        if printed_tokens(make_payload(selected, empty_files=candidate)) <= max_tokens:
            selected_empty_files = candidate

    def omission_budgets(selected_pairs, empty_files):
        budgets = {}
        for path in ranked_paths:
            selected_in_file = any(
                (path, entity["id"]) in selected_pairs for entity in entities[path]
            )
            if not selected_in_file and path not in empty_files:
                budgets[("file", path)] = complete_tokens
            for entity in entities[path]:
                if (path, entity["id"]) not in selected_pairs:
                    budgets[("entity", path, entity["id"])] = complete_tokens
        return budgets

    while True:
        omitted_budgets = omission_budgets(selected, selected_empty_files)
        final = make_payload(selected, omitted_budgets)
        if printed_tokens(final) <= max_tokens:
            break
        if selected_empty_files:
            removable_file = next(
                path for path in reversed(empty_paths) if path in selected_empty_files
            )
            selected_empty_files.remove(removable_file)
        elif selected:
            removable = next(
                (path, entity["id"])
                for path, entity in reversed(ordered_entities)
                if (path, entity["id"]) in selected
            )
            selected.remove(removable)
        else:
            raise InvalidBudgetError(
                f"required outline metadata needs {printed_tokens(final)} tokens; budget is {max_tokens}"
            )
    final = make_payload(selected, omitted_budgets)
    if printed_tokens(final) > max_tokens:
        raise InvalidBudgetError(
            f"required outline metadata needs {printed_tokens(final)} tokens; budget is {max_tokens}"
        )
    return final
