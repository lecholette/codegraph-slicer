from codegraph_slicer import GraphStore


def _sync(tmp_path, files):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    store = GraphStore(":memory:")
    return store, store.sync_workspace(tmp_path)


def _import_targets(store, source):
    return [
        row["target_id"]
        for row in store.rows(
            "select target_id from edges where kind='IMPORTS' and source_id=? order by target_id",
            (source,),
        )
    ]


def test_string_module_reference_links_into_any_indexed_package(tmp_path):
    store, result = _sync(tmp_path, {
        "acme/__init__.py": "",
        "acme/plugins.py": "def fast():\n    pass\n",
        "acme/registry.py": "TABLE = {'fast': 'acme.plugins'}\nOTHER = 'os.path'\n",
    })
    assert _import_targets(store, "acme.registry:") == ["acme.plugins:"]
    assert result["packages"] == ["acme"]


def test_src_layout_indexes_modules_by_import_name(tmp_path):
    store, result = _sync(tmp_path, {
        "src/acme/__init__.py": "",
        "src/acme/core.py": "def run():\n    pass\n",
        "tests/test_core.py": "from acme.core import run\n\ndef test_run():\n    run()\n",
    })
    assert store.rows("select module from files order by path") == [
        {"module": "acme"}, {"module": "acme.core"}, {"module": "tests.test_core"},
    ]
    assert store.rows(
        "select source_id from edges where kind='CALLS' and target_id='acme.core:run'"
    ) == [{"source_id": "tests.test_core:test_run"}]
    assert result["source_root"] == "src/"


def test_mixed_layout_keeps_src_in_module_names(tmp_path):
    store, result = _sync(tmp_path, {
        "app/__init__.py": "",
        "src/rag/__init__.py": "",
        "src/rag/index.py": "def build():\n    pass\n",
        "app/main.py": "from src.rag.index import build\n\ndef run():\n    build()\n",
    })
    assert store.rows(
        "select source_id from edges where kind='CALLS' and target_id='src.rag.index:build'"
    ) == [{"source_id": "app.main:run"}]
    assert result["source_root"] == ""
