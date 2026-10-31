import pytest
from codegraph_slicer import (
    extract_file,
    CodeGraphParseError,
    GraphStore,
    slice_context,
    AnchorNotFoundError,
    InvalidBudgetError,
    RequiredAnchorOverflow,
)


def test_extract_rejects_invalid_syntax():
    with pytest.raises(CodeGraphParseError, match="cannot parse"):
        extract_file("m.py", b"def broken(:\n")


def test_sync_caches_identical_blob(tmp_path):
    (tmp_path / "m.py").write_text(
        "def f(x: int) -> int:\n    if x: return 1\n    return 0\n"
    )
    store = GraphStore(":memory:")
    assert store.sync_workspace(tmp_path)["parsed_files"] == 1
    assert store.sync_workspace(tmp_path)["parsed_files"] == 0
    assert (
        store.rows("select cx from symbols where symbol_id=?", ("m:f",))[0]["cx"] == 2
    )


def test_slice_requires_anchor_and_budget(tmp_path):
    (tmp_path / "m.py").write_text("def f():\n    return 3\n")
    store = GraphStore(":memory:")
    store.sync_workspace(tmp_path)
    with pytest.raises(AnchorNotFoundError, match="absent:f"):
        slice_context(store, ["absent:f"])
    with pytest.raises(InvalidBudgetError, match="positive"):
        slice_context(store, ["m:f"], max_tokens=0)


def test_slice_refuses_required_anchor_overflow(tmp_path):
    (tmp_path / "m.py").write_text('def f():\n    return "' + "x" * 100 + '"\n')
    store = GraphStore(":memory:")
    store.sync_workspace(tmp_path)
    with pytest.raises(RequiredAnchorOverflow, match="required definitions need"):
        slice_context(store, ["m:f"], max_tokens=2)
