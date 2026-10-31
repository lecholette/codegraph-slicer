from codegraph_slicer import GraphStore, slice_context


def test_decorator_line_anchor_selects_decorated_method(tmp_path):
    (tmp_path / "m.py").write_text(
        "class Service:\n    @property\n    def value(self):\n        return 42\n"
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        result = slice_context(store, ["m.py:2"])
    assert [row["symbol"] for row in result["sections"]["target_implementations"]] == [
        "m:Service.value"
    ]


def test_type_closure_terminates_and_deduplicates_cycles(tmp_path):
    (tmp_path / "m.py").write_text(
        "class A:\n    value: 'B'\nclass B:\n    value: 'A'\n"
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        result = slice_context(store, ["m:A"])
    assert [row["symbol"] for row in result["sections"]["types_and_schemas"]] == [
        "m:B"
    ]


def test_required_type_is_not_repeated_as_downstream_callee_for_later_anchor(tmp_path):
    (tmp_path / "m.py").write_text(
        "class Payload:\n    value: int\n"
        "def a():\n    return Payload()\n"
        "def z(value: Payload):\n    return value\n"
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        result = slice_context(store, ["m:a", "m:z"])
    sections = result["sections"]
    assert [row["symbol"] for row in sections["types_and_schemas"]] == ["m:Payload"]
    assert "m:Payload" not in [
        row["symbol"] for row in sections["downstream_dependencies"]
    ]
