import pytest

from codegraph_slicer import GraphStore


def _sync(tmp_path, files):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    store = GraphStore(":memory:")
    store.sync_workspace(tmp_path)
    return store


def _edges(store, kind, target):
    return store.rows(
        "select source_id from edges where kind=? and target_id=? order by source_id",
        (kind, target),
    )


def test_call_through_package_reexport_resolves_to_definition(tmp_path):
    store = _sync(tmp_path, {
        "pkg/__init__.py": "from .impl import Loop as PublicLoop\n",
        "pkg/impl.py": "class Loop:\n    pass\n",
        "user.py": "import pkg\n\ndef make():\n    return pkg.PublicLoop()\n",
    })
    assert _edges(store, "CALLS", "pkg.impl:Loop") == [{"source_id": "user:make"}]
    assert store.rows(
        "select count(*) n from diagnostics where target='pkg.PublicLoop'"
    ) == [{"n": 0}]


def test_self_expanding_alias_cycle_stays_unresolved(tmp_path):
    store = _sync(tmp_path, {
        "a.py": "from a.X import Y as X\n",
        "user.py": "import a\n\ndef go():\n    a.X()\n",
    })
    assert store.rows("select target from diagnostics where source_id='user:go'") == [
        {"target": "a.X"}]


def test_chained_reexports_resolve_and_cycles_stay_unresolved(tmp_path):
    store = _sync(tmp_path, {
        "a/__init__.py": "from b import Thing\nfrom c import Ghost\n",
        "b/__init__.py": "from b.core import Thing\n",
        "b/core.py": "def Thing():\n    return 1\n",
        "c/__init__.py": "from a import Ghost\n",
        "user.py": "import a\n\ndef go():\n    a.Thing()\n    a.Ghost()\n",
    })
    assert _edges(store, "CALLS", "b.core:Thing") == [{"source_id": "user:go"}]
    assert store.rows(
        "select count(*) n from diagnostics where source_id='user:go' and target='a.Ghost'"
    ) == [{"n": 1}]



USE = "\n\ndef use():\n    return Loop()\n"
FILES = {
    "pkg/impl.py": "class Loop:\n    pass\n",
    "pkg/other.py": "Loop = None\n",
    "user.py": "import pkg\n\ndef make():\n    return pkg.Loop()\n",
}


@pytest.mark.parametrize("init", [
    "if False:\n    from .impl import Loop\n",
    "from .impl import Loop\nif c:\n    Loop = None\nelse:\n    from .impl import Loop\n",
    "try:\n    from .impl import Loop\nexcept ImportError:\n    Loop = None\n",
    "from .impl import Loop\nLoop = None\n",
    "from .impl import Loop\ndel Loop\n",
    "from .impl import Loop\n\ndef swap():\n    global Loop\n    Loop = None\n",
    "from .impl import Loop\ntry:\n    pass\nexcept Exception as Loop:\n    pass\n",
    "from .impl import Loop\n\ndef change(x=(Loop := None)):\n    pass\n",
    "from .impl import Loop\n\nclass K((Loop := object)):\n    pass\n",
    "from .impl import Loop\n\ndef change(x: (Loop := None)):\n    pass\n",
    "from .impl import Loop\n\ndef change() -> (Loop := None):\n    pass\n",
    "from .impl import Loop\nmatch 1:\n    case Loop:\n        pass\n",
    "from .impl import Loop\nmatch [1]:\n    case [*Loop]:\n        pass\n",
    "from .impl import Loop\nmatch {}:\n    case {**Loop}:\n        pass\n",
], ids=["if-false", "branch-restores", "try-fallback", "reassigned", "deleted", "global-rebind",
        "except-as", "walrus-default", "walrus-base", "walrus-annotation", "walrus-return", "match-capture", "match-star", "match-rest"])
def test_name_not_effectively_bound_stays_unresolved_in_module_and_reexport(tmp_path, init):
    store = _sync(tmp_path, {**FILES, "pkg/__init__.py": init + USE})
    assert _edges(store, "CALLS", "pkg.impl:Loop") == []
    assert store.rows(
        "select count(*) n from diagnostics where source_id='user:make' and target='pkg.Loop'"
    ) == [{"n": 1}]


# Documented limit (HELP.md "Analysis limits"): these forms are not modelled, so the
# re-export resolves exactly as the module's own reference does.
@pytest.mark.parametrize("init", [
    "from .impl import Loop\nfrom .other import *\n",
    "from .impl import Loop\nglobals()['Loop'] = None\n",
    "from .impl import Loop\nvars()['Loop'] = None\n",
    "from .impl import Loop\nexec('Loop = None')\n",
    "from .impl import Loop\neval('0')\n",
    "from .impl import Loop\n\ndef f(a):\n    return vars(a)\n",
], ids=["star-import", "globals-write", "vars-write", "exec", "eval", "vars-arg"])
def test_unmodelled_forms_resolve_reexport_like_module_reference(tmp_path, init):
    store = _sync(tmp_path, {**FILES, "pkg/__init__.py": init + USE})
    assert _edges(store, "CALLS", "pkg.impl:Loop") == [
        {"source_id": "pkg:use"}, {"source_id": "user:make"}]


def test_final_import_binding_beats_shadowed_definition(tmp_path):
    store = _sync(tmp_path, {**FILES, "pkg/__init__.py": "def Loop():\n    pass\nfrom .impl import Loop" + USE})
    assert _edges(store, "CALLS", "pkg.impl:Loop") == [
        {"source_id": "pkg:use"}, {"source_id": "user:make"}]
    assert _edges(store, "CALLS", "pkg:Loop") == []


def test_rebound_definition_stays_unresolved_through_reexport(tmp_path):
    store = _sync(tmp_path, {**FILES, "pkg/__init__.py": "def Loop():\n    pass\nLoop = None" + USE})
    assert _edges(store, "CALLS", "pkg:Loop") == []
    assert store.rows("select target from diagnostics where source_id='user:make'") == [
        {"target": "pkg.Loop"}]


# `import pkg.f` rebinding the attribute is not modelled (HELP.md "Analysis limits"),
# so the module binding wins inside the module and through the re-export alike.
@pytest.mark.parametrize("init, expected", [
    ("f = None\n", []),
    ("from .impl import f\n", ["pkg.impl:f", "pkg.impl:f"]),
    ("def f():\n    pass\n", ["pkg:f", "pkg:f"]),
], ids=["assigned", "imported", "defined"])
def test_module_binding_beats_same_named_submodule(tmp_path, init, expected):
    store = _sync(tmp_path, {
        "pkg/__init__.py": init + "\n\ndef use():\n    f()\n    f.g()\n",
        "pkg/impl.py": "def f():\n    pass\n",
        "pkg/f.py": "def g():\n    pass\n",
        "user.py": "import pkg\n\ndef go():\n    pkg.f()\n    pkg.f.g()\n",
    })
    calls = store.rows("select source_id, target_id from edges where kind='CALLS' order by 1")
    assert [row["target_id"] for row in calls] == expected
    assert _edges(store, "CALLS", "pkg.f:g") == []


def test_submodule_imported_into_its_package_resolves(tmp_path):
    store = _sync(tmp_path, {
        "pkg/__init__.py": "from . import f\n",
        "pkg/f.py": "def g():\n    pass\n",
        "user.py": "import pkg\n\ndef go():\n    return pkg.f.g()\n",
    })
    assert _edges(store, "CALLS", "pkg.f:g") == [{"source_id": "user:go"}]


def test_class_body_global_rebind_stays_unresolved(tmp_path):
    init = "from .impl import Loop\nclass K:\n    global Loop\n    Loop = None\n"
    store = _sync(tmp_path, {**FILES, "pkg/__init__.py": init + USE})
    assert _edges(store, "CALLS", "pkg.impl:Loop") == []
    assert store.rows("select target from diagnostics where source_id='user:make'") == [
        {"target": "pkg.Loop"}]


def test_reexport_through_submodule_imported_into_its_package(tmp_path):
    store = _sync(tmp_path, {
        "pkg/__init__.py": "from . import sub\n",
        "pkg/sub.py": "from .impl import Loop\n",
        "pkg/impl.py": "class Loop:\n    pass\n",
        "user.py": "import pkg\n\ndef go():\n    return pkg.sub.Loop()\n",
    })
    assert _edges(store, "CALLS", "pkg.impl:Loop") == [{"source_id": "user:go"}]


SUB_BINDINGS = {
    "unbound": "",
    "imported": "from .impl import sub\n",
    "defined": "def sub():\n    pass\n",
    "assigned": "sub = None\n",
    "guarded": "try:\n    from . import sub\nexcept ImportError:\n    sub = None\n",
    "same-name-reexport": "from .sub import sub\n",
    "external": "import json as sub\n",
}


def _user_edges(store):
    return store.rows(
        "select source_id, kind, target_id from edges where source_id like 'user:%'"
        " and kind in ('IMPORTS', 'CALLS') order by 1, 2, 3"
    )


# Import statements reach a submodule through the import system, so a package
# binding with the submodule's name never redirects them.
@pytest.mark.parametrize("init", SUB_BINDINGS.values(), ids=SUB_BINDINGS.keys())
@pytest.mark.parametrize("user, expected", [
    ("from pkg.sub import helper\n\ndef go():\n    helper()\n", [
        ("user:", "IMPORTS", "pkg.sub:"), ("user:", "IMPORTS", "pkg.sub:helper"),
        ("user:go", "CALLS", "pkg.sub:helper")]),
    ("import pkg.sub as s\n\ndef go():\n    s.helper()\n", [
        ("user:", "IMPORTS", "pkg.sub:"), ("user:go", "CALLS", "pkg.sub:helper")]),
], ids=["from-submodule", "import-submodule-as"])
def test_import_statement_reaches_submodule_despite_package_binding(tmp_path, init, user, expected):
    store = _sync(tmp_path, {
        "pkg/__init__.py": init,
        "pkg/impl.py": "def sub():\n    pass\n",
        "pkg/sub.py": "def sub():\n    pass\n\ndef helper():\n    pass\n",
        "user.py": user,
    })
    assert [tuple(row.values()) for row in _user_edges(store)] == expected
    assert store.rows("select target from diagnostics where source_file='user.py'") == []


# `from pkg import sub` reads the attribute first and falls back to the submodule.
@pytest.mark.parametrize("init, expected", [
    ("", "pkg.sub:"),
    ("def sub():\n    pass\n", "pkg:sub"),
    ("from .impl import sub\n", "pkg.impl:sub"),
    ("from .sub import sub\n", "pkg.sub:sub"),
], ids=["unbound", "defined", "imported", "same-name-reexport"])
def test_from_package_import_reads_attribute_before_submodule(tmp_path, init, expected):
    store = _sync(tmp_path, {
        "pkg/__init__.py": init,
        "pkg/impl.py": "def sub():\n    pass\n",
        "pkg/sub.py": "def sub():\n    pass\n",
        "user.py": "from pkg import sub\n",
    })
    assert [tuple(row.values()) for row in _user_edges(store)] == sorted([
        ("user:", "IMPORTS", "pkg:"), ("user:", "IMPORTS", expected)])


# An import statement's module path must name a module: a same-named package
# attribute does not satisfy `from pkg.sub import m` (ModuleNotFoundError).
@pytest.mark.parametrize("user", [
    "from pkg.sub import m\n\ndef go():\n    m()\n",
    "import pkg.sub as s\n\ndef go():\n    s.m()\n",
], ids=["from-import", "import-as"])
def test_import_module_path_never_resolves_to_package_attribute(tmp_path, user):
    store = _sync(tmp_path, {
        "pkg/__init__.py": "class sub:\n    @staticmethod\n    def m():\n        pass\n",
        "user.py": user,
    })
    assert _user_edges(store) == []
    assert sorted(row["target"] for row in store.rows(
        "select target from diagnostics where source_id='user:go'")) == ["pkg.sub.m"]
