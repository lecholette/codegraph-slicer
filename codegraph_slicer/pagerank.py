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
                return map_estimator.estimate_map(
                    item_cost, count, total_files - count, top_file_omitted
                )
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
        if source_path not in candidate_paths or target_path not in candidate_paths:
            continue
        all_edges.append({
            "from": symbol_refs[edge["source_id"]],
            "to": symbol_refs[edge["target_id"]],
            "kind": edge["kind"],
        })

    def compact(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    entity_costs = {
        (path, entity["id"]): len(compact(entity))
        for path in all_paths for entity in entities[path]
    }
    file_prefix_costs = {
        path: (
            len('{"id":') + len(compact(file_ids[path]))
            + len(',"path":') + len(compact(path))
            + len(',"entities":[')
        )
        for path in all_paths
    }
    edge_costs = {
        (edge["from"], edge["to"], edge["kind"]): len(compact(edge))
        for edge in all_edges
    }

    # Budget entities in seed priority order. Omitted records point to the
    # complete candidate budget, which is guaranteed to admit every candidate.
    ordered_entities = [
        (row["file_path"], entity_by_symbol[row["symbol_id"]])
        for row in ranked_symbols if row["file_path"] in ranked_paths
    ]
    selected = set()
    selected_empty_files = set()

    def estimate_selected(selected_pairs, omission_budget=None, empty_files=None):
        empty_files = selected_empty_files if empty_files is None else empty_files
        selected_by_path = {}
        selected_refs = set()
        files_cost = 0
        file_count = 0
        for path in ranked_paths:
            selected_entities = [
                entity for entity in entities[path]
                if (path, entity["id"]) in selected_pairs
            ]
            if selected_entities:
                file_count += 1
                files_cost += (
                    file_prefix_costs[path]
                    + sum(entity_costs[(path, entity["id"])] for entity in selected_entities)
                    + max(0, len(selected_entities) - 1) + len("]}")
                )
                selected_refs.update(
                    f"{file_ids[path]}:{entity['id']}" for entity in selected_entities
                )
            elif path in empty_files:
                file_count += 1
                files_cost += file_prefix_costs[path] + len("]}")
        files_cost += max(0, file_count - 1)

        kept_edge_count = 0
        edges_cost = 0
        for edge in all_edges:
            if edge["from"] in selected_refs and edge["to"] in selected_refs:
                kept_edge_count += 1
                edges_cost += edge_costs[(edge["from"], edge["to"], edge["kind"])]
        edges_cost += max(0, kept_edge_count - 1)

        omitted = {}
        skipped_files = []
        skipped_entities = []
        for path in ranked_paths:
            selected_count = sum(
                (path, entity["id"]) in selected_pairs for entity in entities[path]
            )
            if not selected_count and path not in empty_files:
                skipped_files.append({"path": path, "count": 1})
            omitted_count = len(entities[path]) - selected_count
            if omitted_count:
                skipped_entities.append({"path": path, "count": omitted_count})
        if skipped_files:
            omitted["files"] = skipped_files
        if skipped_entities:
            omitted["entities"] = skipped_entities
        omitted_edge_count = len(all_edges) - kept_edge_count
        if omitted_edge_count:
            omitted["edges"] = omitted_edge_count
        if omitted and omission_budget is not None:
            omitted["max_tokens"] = omission_budget

        if render_estimator is not None and hasattr(render_estimator, "estimate_outline"):
            markdown_files = [
                (file_ids[path], path, [
                    entity for entity in entities[path]
                    if (path, entity["id"]) in selected_pairs
                ])
                for path in ranked_paths
                if any((path, entity["id"]) in selected_pairs for entity in entities[path])
                or path in empty_files
            ]
            markdown_edges = [
                edge for edge in all_edges
                if edge["from"] in selected_refs and edge["to"] in selected_refs
            ]
            return render_estimator.estimate_outline(markdown_files, markdown_edges, omitted)

        size_without_tokens = (
            len('{"schema_version":1,"tokens":') + len(',"files":[')
            + files_cost + len('],"edges":[') + edges_cost
            + len("]") + len("}")
        )
        if omitted:
            size_without_tokens += len(',"omitted":') + len(compact(omitted))
        tokens = 0
        for _ in range(16):
            estimate = tokens_for_length(size_without_tokens + len(str(tokens)) + 1)
            if estimate == tokens:
                return estimate
            tokens = estimate
        raise InvalidBudgetError("repository outline JSON token estimate did not reach a fixed point")

    def make_payload(selected_pairs, omission_budget=None, empty_files=None):
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
                skipped_files.append({"path": path, "count": 1})
            omitted_entity_count = sum(
                entity["id"] not in selected_in_file for entity in entities[path]
            )
            if omitted_entity_count:
                skipped_entities.append({"path": path, "count": omitted_entity_count})
        omitted_edge_count = sum(
            edge["from"] not in listed_refs or edge["to"] not in listed_refs
            for edge in all_edges
        )
        payload = {
            "schema_version": 1,
            "tokens": 0,
            "files": [
                {"id": file_ids[path], "path": path, "entities": selected_by_path.get(path, [])}
                for path in ranked_paths
                if path in selected_by_path or path in empty_files
            ],
            "edges": kept_edges,
        }
        omitted = {}
        if skipped_files:
            omitted["files"] = skipped_files
        if skipped_entities:
            omitted["entities"] = skipped_entities
        if omitted_edge_count:
            omitted["edges"] = omitted_edge_count
        if omitted and omission_budget is not None:
            omitted["max_tokens"] = omission_budget
        if omitted:
            payload["omitted"] = omitted
        if render_estimator is not None:
            return _with_output_tokens(payload, render_estimator)

        # JSON admission is additive: file/entity and edge records are encoded
        # once, then candidate prefixes are measured from their cached sizes.
        files_cost = 0
        for index, file in enumerate(payload["files"]):
            path = file["path"]
            items = file["entities"]
            file_cost = (
                file_prefix_costs[path]
                + sum(entity_costs[(path, item["id"])] for item in items)
                + max(0, len(items) - 1)
                + len("]}")
            )
            files_cost += file_cost + int(index > 0)
        edges_cost = sum(
            edge_costs[(edge["from"], edge["to"], edge["kind"])]
            for edge in payload["edges"]
        )
        edges_cost += max(0, len(payload["edges"]) - 1)
        json_size_without_token = (
            len('{"schema_version":1,"tokens":')
            + len(',"files":[') + files_cost
            + len('],"edges":[') + edges_cost
            + len("]")
        )
        if "omitted" in payload:
            json_size_without_token += len(',"omitted":') + len(compact(payload["omitted"]))
        json_size_without_token += len("}")
        tokens = 0
        for _ in range(16):
            estimate = tokens_for_length(json_size_without_token + len(str(tokens)) + 1)
            if estimate == tokens:
                payload["tokens"] = estimate
                return payload
            tokens = estimate
        raise InvalidBudgetError("repository outline JSON token estimate did not reach a fixed point")

    def printed_tokens(payload):
        if render_estimator is None or hasattr(render_estimator, "estimate_outline"):
            return payload["tokens"]
        return render_estimator(payload)

    empty_paths = [path for path in ranked_paths if not entities[path]]

    complete_pairs = {
        (path, entity["id"])
        for path in ranked_paths for entity in entities[path]
    }
    complete_tokens = estimate_selected(complete_pairs, empty_files=set(empty_paths))
    if complete_tokens <= max_tokens:
        return make_payload(complete_pairs, empty_files=set(empty_paths))

    seed_pairs = {
        (file_by_symbol[symbol_id], ids[file_by_symbol[symbol_id]][symbol_id])
        for symbol_id in seed_ids
    }
    # Seed entities are mandatory whenever a seeded outline is requested. Test
    # the complete seed set alone before filling the remaining rank order.
    seed_required_tokens = estimate_selected(seed_pairs, complete_tokens)
    if seed_pairs and seed_required_tokens > max_tokens:
        retry_tokens = min(complete_tokens, seed_required_tokens)
        raise InvalidBudgetError(
            f"required outline metadata: seed entities need more than the "
            f"{max_tokens}-token budget; rerun with --max-tokens "
            f"{retry_tokens}"
        )
    selected.update(seed_pairs)
    for path, entity in ordered_entities:
        if (path, entity["id"]) in selected:
            continue
        candidate = set(selected)
        candidate.add((path, entity["id"]))
        if estimate_selected(candidate, complete_tokens) <= max_tokens:
            selected = candidate
    for path in empty_paths:
        candidate = set(selected_empty_files)
        candidate.add(path)
        if printed_tokens(make_payload(selected, empty_files=candidate)) <= max_tokens:
            selected_empty_files = candidate

    while True:
        final = make_payload(selected, complete_tokens)
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
    final = make_payload(selected, complete_tokens)
    if printed_tokens(final) > max_tokens:
        raise InvalidBudgetError(
            f"required outline metadata needs {printed_tokens(final)} tokens; budget is {max_tokens}"
        )
    return final
