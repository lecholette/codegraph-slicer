"""Lightweight JPA/ORM and Python data-model contract extraction for slices."""
from __future__ import annotations

import ast
from dataclasses import dataclass
import re

_PII_PATTERN = re.compile(
    r"(?:^|_)(email|tax_id|ssn|social_security|phone|mobile|passport|driver_license|dob|birth_date|iban|routing_number|card_number)(?:$|_)",
    re.IGNORECASE,
)

_ANNOTATION_TABLE_RE = re.compile(
    r"""@(?:Table|Entity)\s*\([^)]*\bname\s*=\s*["']([^"']+)["']""",
)
_RESTRICTION_RE = re.compile(
    r"""@(?:SQLRestriction|Where)\s*\(\s*(?:clause\s*=\s*)?["']([^"']+)["']""",
)
_COL_ANN_RE = re.compile(
    r"""@(?:Column|JoinColumn)\s*\([^)]*\bname\s*=\s*["']([^"']+)["']""",
)
_REL_ANN_RE = re.compile(r"@(OneToMany|ManyToMany|ManyToOne|OneToOne)\b")
_FIELD_DECL_RE = re.compile(
    r"(?:private|protected|public)?\s*(?:final\s+)?([A-Za-z_][\w.<>,\s\[\]?]*)\s+([A-Za-z_]\w*)\s*(?:=|;)"
)
_REL_CARDINALITY = {
    "OneToMany": "1:N",
    "ManyToMany": "M:N",
    "ManyToOne": "N:1",
    "OneToOne": "1:1",
}


@dataclass(frozen=True)
class _SymbolRecord:
    symbol_id: str
    kind: str
    file_path: str
    module: str
    start_line: int
    body: str

    @classmethod
    def from_row(cls, row: dict) -> "_SymbolRecord":
        return cls(
            symbol_id=str(row["symbol_id"]),
            kind=str(row["kind"]),
            file_path=str(row["file_path"]),
            module=str(row["module"]),
            start_line=int(row["start_line"]),
            body=str(row["body"]),
        )


@dataclass(frozen=True)
class DataContract:
    symbol: str
    path: str
    table: str
    filters: tuple[str, ...]
    columns: dict[str, str]
    relations: tuple[str, ...]
    pii_columns: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "path": self.path,
            "table": self.table,
            "filters": list(self.filters),
            "columns": dict(self.columns),
            "relations": list(self.relations),
            "pii_columns": list(self.pii_columns),
        }


def _is_pii_name(name: str) -> bool:
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()
    return bool(_PII_PATTERN.search(snake))


def _class_preamble_lines(store, symbol: _SymbolRecord) -> list[str]:
    mod_rows = store.rows(
        "SELECT body FROM symbols WHERE symbol_id=?",
        (f"{symbol.module}:",),
    )
    if not mod_rows:
        return []
    lines = mod_rows[0]["body"].splitlines()
    start = max(0, symbol.start_line - 1)
    preamble: list[str] = []
    for idx in range(start - 1, max(-1, start - 12), -1):
        stripped = lines[idx].strip()
        if not stripped or stripped.startswith(("@", "//", "/*", "*")):
            preamble.append(lines[idx])
        else:
            break
    preamble.reverse()
    return preamble


def _format_java_relation(pending_rel: str | None, fname: str, col_name: str, ftype: str) -> str | None:
    card = _REL_CARDINALITY.get(pending_rel or "")
    if not card:
        return None
    if card in {"1:N", "M:N"}:
        return f"{card} {fname} -> {ftype}"
    return f"{card} {fname} ({col_name}) -> {ftype}"


def _record_java_field(
    line: str,
    pending_col: str | None,
    pending_rel: str | None,
    columns: dict[str, str],
    relations: list[str],
    pii: list[str],
) -> None:
    field_m = _FIELD_DECL_RE.match(line)
    if not field_m:
        return
    ftype, fname = field_m.group(1).strip(), field_m.group(2).strip()
    col_name = pending_col or fname
    if pending_col is not None:
        columns[fname] = pending_col
    if rel_str := _format_java_relation(pending_rel, fname, col_name, ftype):
        relations.append(rel_str)
    if _is_pii_name(col_name) or _is_pii_name(fname):
        pii.append(col_name)


def _scan_java_fields(body: str) -> tuple[dict[str, str], tuple[str, ...], tuple[str, ...]]:
    columns: dict[str, str] = {}
    relations: list[str] = []
    pii: list[str] = []
    pending_col: str | None = None
    pending_rel: str | None = None

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if col_m := _COL_ANN_RE.search(line):
            pending_col = col_m.group(1)
        if rel_m := _REL_ANN_RE.search(line):
            pending_rel = rel_m.group(1)
        if not line or line.startswith("@") or line.endswith("{"):
            continue
        _record_java_field(line, pending_col, pending_rel, columns, relations, pii)
        pending_col = None
        pending_rel = None
    return columns, tuple(relations), tuple(dict.fromkeys(pii))


def _extract_java_entity_contract(
    symbol: str, path: str, header_text: str, body: str
) -> DataContract | None:
    if "@Entity" not in header_text and "@Table" not in header_text:
        return None
    table_m = _ANNOTATION_TABLE_RE.search(header_text)
    table = table_m.group(1) if table_m else symbol.rsplit(".", 1)[-1]
    filters = tuple(_RESTRICTION_RE.findall(header_text))
    columns, relations, pii = _scan_java_fields(body)
    return DataContract(
        symbol=symbol,
        path=path,
        table=table,
        filters=filters,
        columns=columns,
        relations=relations,
        pii_columns=pii,
    )


def _py_stmt_col_and_table(stmt: ast.stmt) -> tuple[str | None, str | None]:
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        name = stmt.targets[0].id
        if name == "__tablename__" and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
            return stmt.value.value, None
        return None, name
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return None, stmt.target.id
    return None, None


def _parse_first_classdef(body: str) -> ast.ClassDef | None:
    if "__tablename__" not in body:
        return None
    try:
        mod = ast.parse(body)
    except SyntaxError:
        return None
    if not mod.body or not isinstance(mod.body[0], ast.ClassDef):
        return None
    return mod.body[0]


def _extract_python_orm_contract(symbol: str, path: str, body: str) -> DataContract | None:
    cls_node = _parse_first_classdef(body)
    if cls_node is None:
        return None
    table = ""
    columns: dict[str, str] = {}
    pii: list[str] = []
    for stmt in cls_node.body:
        tbl_val, col_name = _py_stmt_col_and_table(stmt)
        if tbl_val:
            table = tbl_val
        elif col_name:
            columns[col_name] = col_name
            if _is_pii_name(col_name):
                pii.append(col_name)
    if not table:
        return None
    return DataContract(
        symbol=symbol,
        path=path,
        table=table,
        filters=(),
        columns=columns,
        relations=(),
        pii_columns=tuple(dict.fromkeys(pii)),
    )


def _contract_for_row(store, symbol: _SymbolRecord) -> DataContract | None:
    if symbol.kind != "class":
        return None
    if symbol.file_path.endswith(".java"):
        preamble = "\n".join(_class_preamble_lines(store, symbol))
        return _extract_java_entity_contract(
            symbol.symbol_id,
            symbol.file_path,
            f"{preamble}\n{symbol.body[:400]}",
            symbol.body,
        )
    if symbol.file_path.endswith(".py"):
        return _extract_python_orm_contract(symbol.symbol_id, symbol.file_path, symbol.body)
    return None


def extract_slice_data_contracts(store, sections: dict) -> list[dict]:
    """Extract compact JPA/ORM data contracts for symbols present in slice sections."""
    seen_sids: list[str] = []
    for sec_name in ("types_and_schemas", "target_implementations"):
        for entry in sections.get(sec_name, ()):
            sid = entry.get("symbol", "")
            if sid and sid not in seen_sids:
                seen_sids.append(sid)
    if not seen_sids:
        return []
    placeholders = ",".join("?" for _ in seen_sids)
    rows_by_id = {
        str(r["symbol_id"]): _SymbolRecord.from_row(r)
        for r in store.rows(
            f"SELECT * FROM symbols WHERE symbol_id IN ({placeholders})",
            tuple(seen_sids),
        )
    }
    contracts = [
        c.to_dict()
        for sid in seen_sids
        if sid in rows_by_id and (c := _contract_for_row(store, rows_by_id[sid])) is not None
    ]
    return contracts
