from codegraph_slicer import GraphStore


def test_lambda_parameter_shadows_module_binding_through_graph_store(tmp_path):
    (tmp_path / "m.py").write_text(
        "def target():\n    return 1\ndef build():\n    return lambda target: target()\n"
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        assert store.rows(
            "SELECT source_id, target_id FROM edges WHERE kind='CALLS'"
        ) == []
        assert store.rows(
            "SELECT source_id, target, kind FROM diagnostics WHERE kind='CALLS'"
        ) == [{"source_id": "m:build", "target": "?target", "kind": "CALLS"}]


def test_global_lookup_uses_module_binding_through_nested_function(tmp_path):
    (tmp_path / "m.py").write_text(
        "def target():\n    return 1\ndef outer():\n    def target():\n        return 2\n    def inner():\n        global target\n        return target()\n    return inner\n"
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        assert store.rows(
            "SELECT source_id, target_id FROM edges WHERE kind='CALLS'"
        ) == [{"source_id": "m:outer.inner", "target_id": "m:target"}]
        assert store.rows(
            "SELECT source_id, target FROM diagnostics WHERE kind='CALLS'"
        ) == []


def test_nonlocal_and_class_namespace_scope_resolution(tmp_path):
    (tmp_path / "m.py").write_text(
        "def target():\n    return 1\ndef outer():\n    def target():\n        return 2\n    def inner():\n        nonlocal target\n        return target()\n    return inner\nclass C:\n    target = None\n    def method(self):\n        return target()\n"
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        assert store.rows(
            "SELECT source_id, target_id FROM edges WHERE kind='CALLS' ORDER BY source_id"
        ) == [
            {"source_id": "m:C.method", "target_id": "m:target"},
            {"source_id": "m:outer.inner", "target_id": "m:outer.target"},
        ]
        assert store.rows("SELECT source_id, target FROM diagnostics WHERE kind='CALLS'") == []


def test_lambda_default_uses_enclosing_scope_but_body_parameter_shadows(tmp_path):
    (tmp_path / "m.py").write_text(
        "def target():\n    return 1\ndef build():\n    return lambda target=target(): target()\n"
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        assert store.rows(
            "SELECT source_id, target_id, line FROM edges WHERE kind='CALLS'"
        ) == [{"source_id": "m:build", "target_id": "m:target", "line": 4}]
        assert store.rows(
            "SELECT source_id, target, line FROM diagnostics WHERE kind='CALLS'"
        ) == [{"source_id": "m:build", "target": "?target", "line": 4}]


def test_nonlocal_resolves_method_local_binding(tmp_path):
    (tmp_path / "m.py").write_text(
        "class C:\n    def outer(self):\n        def target():\n            return 1\n        def inner():\n            nonlocal target\n            return target()\n        return inner\n"
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        assert store.rows(
            "SELECT source_id, target_id FROM edges WHERE kind='CALLS'"
        ) == [{"source_id": "m:C.outer.inner", "target_id": "m:C.outer.target"}]
        assert store.rows("SELECT source_id, target FROM diagnostics WHERE kind='CALLS'") == []


def test_lambda_in_class_body_skips_class_namespace(tmp_path):
    (tmp_path / "m.py").write_text(
        "def target():\n    return 0\nclass C:\n    def target():\n        return 1\n    call = lambda: target()\n"
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        assert store.rows(
            "SELECT source_id, target_id FROM edges WHERE kind='CALLS'"
        ) == [{"source_id": "m:C", "target_id": "m:target"}]


def test_comprehension_target_does_not_leak_and_shadows_inside_body(tmp_path):
    (tmp_path / "m.py").write_text(
        "def target():\n    return 1\ndef use(callbacks):\n    values = [target() for target in callbacks]\n    return target()\n"
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        assert store.rows(
            "SELECT source_id, target_id FROM edges WHERE kind='CALLS'"
        ) == [{"source_id": "m:use", "target_id": "m:target"}]
        assert store.rows(
            "SELECT source_id, target FROM diagnostics WHERE kind='CALLS'"
        ) == [{"source_id": "m:use", "target": "?target"}]


def test_lambda_walrus_binding_stays_local_to_lambda(tmp_path):
    (tmp_path / "m.py").write_text(
        "def target():\n    return 1\ndef build():\n    callback = lambda: ((target := None), target())\n    return target()\n"
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        assert store.rows(
            "SELECT source_id, target_id FROM edges WHERE kind='CALLS'"
        ) == [{"source_id": "m:build", "target_id": "m:target"}]
        assert store.rows(
            "SELECT source_id, target FROM diagnostics WHERE kind='CALLS'"
        ) == [{"source_id": "m:build", "target": "?target"}]
