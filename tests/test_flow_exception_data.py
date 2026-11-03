from codegraph_slicer.flow import analyze_flow, slice_flow


def _slice(source, function, variable, line):
    graph = analyze_flow(source, function, path="example.py")
    return graph, slice_flow(graph, variable=variable, line=line, include_control=False)["flow"]


def _records(result):
    nodes = {node["id"]: (node["kind"], node["line"], node["text"]) for node in result["nodes"]}
    return (
        sorted(nodes.values()),
        sorted((edge["kind"], edge["label"], nodes[edge["source"]], nodes[edge["target"]]) for edge in result["edges"]),
    )


def test_explicit_raise_value_reaches_handler_binding_in_data_only_slice():
    source = """def f(exc):
    try:
        raise exc
    except Exception as e:
        return e
"""
    _, result = _slice(source, "f", "e", 5)
    assert _records(result) == (
        [
            ("except_handler", 4, "except Exception"),
            ("exception_name_delete", 4, "delete e"),
            ("exception_name_delete", 4, "delete e"),
            ("parameter", 1, "exc"),
            ("raise", 3, "raise exc"),
            ("return", 5, "return e"),
        ],
        [
            ("AST", "body", ("except_handler", 4, "except Exception"), ("return", 5, "return e")),
            ("CFG", "exception-after-statement", ("raise", 3, "raise exc"), ("except_handler", 4, "except Exception")),
            ("CFG", "implicit-delete", ("return", 5, "return e"), ("exception_name_delete", 4, "delete e")),
            ("CFG", "implicit-delete", ("return", 5, "return e"), ("exception_name_delete", 4, "delete e")),
            ("DATA_DEP", "e", ("except_handler", 4, "except Exception"), ("return", 5, "return e")),
            ("DATA_DEP", "exc", ("parameter", 1, "exc"), ("raise", 3, "raise exc")),
            ("DATA_DEP", "exception-value", ("raise", 3, "raise exc"), ("except_handler", 4, "except Exception")),
        ],
    )


def test_implicit_call_exception_does_not_invent_argument_provenance():
    source = """def f(arg):
    try:
        g(arg)
    except Exception as e:
        return e
"""
    _, result = _slice(source, "f", "e", 5)
    assert _records(result) == (
        [
            ("except_handler", 4, "except Exception"),
            ("exception_name_delete", 4, "delete e"),
            ("exception_name_delete", 4, "delete e"),
            ("return", 5, "return e"),
        ],
        [
            ("AST", "body", ("except_handler", 4, "except Exception"), ("return", 5, "return e")),
            ("CFG", "implicit-delete", ("return", 5, "return e"), ("exception_name_delete", 4, "delete e")),
            ("CFG", "implicit-delete", ("return", 5, "return e"), ("exception_name_delete", 4, "delete e")),
            ("DATA_DEP", "e", ("except_handler", 4, "except Exception"), ("return", 5, "return e")),
        ],
    )


def test_bare_reraise_preserves_bound_exception_to_outer_handler():
    source = """def f(exc):
    try:
        raise exc
    except Exception as inner:
        try:
            raise
        except Exception as outer:
            return outer
"""
    _, result = _slice(source, "f", "outer", 8)
    assert _records(result) == (
        [
            ("except_handler", 4, "except Exception"),
            ("except_handler", 7, "except Exception"),
            ("exception_name_delete", 7, "delete outer"),
            ("exception_name_delete", 7, "delete outer"),
            ("parameter", 1, "exc"),
            ("raise", 3, "raise exc"),
            ("raise", 6, "raise"),
            ("return", 8, "return outer"),
        ],
        [
            ("AST", "body", ("except_handler", 7, "except Exception"), ("return", 8, "return outer")),
            ("CFG", "exception-after-statement", ("raise", 3, "raise exc"), ("except_handler", 4, "except Exception")),
            ("CFG", "exception-after-statement", ("raise", 6, "raise"), ("except_handler", 7, "except Exception")),
            ("CFG", "implicit-delete", ("return", 8, "return outer"), ("exception_name_delete", 7, "delete outer")),
            ("CFG", "implicit-delete", ("return", 8, "return outer"), ("exception_name_delete", 7, "delete outer")),
            ("DATA_DEP", "exc", ("parameter", 1, "exc"), ("raise", 3, "raise exc")),
            ("DATA_DEP", "exception-value", ("raise", 3, "raise exc"), ("except_handler", 4, "except Exception")),
            ("DATA_DEP", "exception-value", ("raise", 6, "raise"), ("except_handler", 7, "except Exception")),
            ("DATA_DEP", "inner", ("except_handler", 4, "except Exception"), ("raise", 6, "raise")),
            ("DATA_DEP", "outer", ("except_handler", 7, "except Exception"), ("return", 8, "return outer")),
        ],
    )


def test_finally_bare_reraise_keeps_pending_explicit_value():
    source = """def f(exc):
    try:
        try:
            raise exc
        finally:
            raise
    except Exception as e:
        return e
"""
    _, result = _slice(source, "f", "e", 8)
    assert _records(result) == (
        [
            ("except_handler", 7, "except Exception"),
            ("exception_name_delete", 7, "delete e"),
            ("exception_name_delete", 7, "delete e"),
            ("parameter", 1, "exc"),
            ("raise", 4, "raise exc"),
            ("raise", 6, "raise"),
            ("return", 8, "return e"),
        ],
        [
            ("AST", "body", ("except_handler", 7, "except Exception"), ("return", 8, "return e")),
            ("CFG", "exception-after-statement", ("raise", 6, "raise"), ("except_handler", 7, "except Exception")),
            ("CFG", "implicit-delete", ("return", 8, "return e"), ("exception_name_delete", 7, "delete e")),
            ("CFG", "implicit-delete", ("return", 8, "return e"), ("exception_name_delete", 7, "delete e")),
            ("DATA_DEP", "e", ("except_handler", 7, "except Exception"), ("return", 8, "return e")),
            ("DATA_DEP", "exc", ("parameter", 1, "exc"), ("raise", 4, "raise exc")),
            ("DATA_DEP", "exception", ("raise", 4, "raise exc"), ("raise", 6, "raise")),
            ("DATA_DEP", "exception-value", ("raise", 6, "raise"), ("except_handler", 7, "except Exception")),
        ],
    )


def test_normal_finally_preserves_enclosing_handler_for_bare_reraise():
    source = """def f(exc):
    try:
        try:
            raise exc
        except Exception as inner:
            try:
                pass
            finally:
                raise
    except Exception as outer:
        return outer
"""
    _, result = _slice(source, "f", "outer", 11)
    nodes = {node["id"]: (node["kind"], node["line"], node["text"]) for node in result["nodes"]}
    deps = sorted(
        (nodes[edge["source"]], nodes[edge["target"]], edge["label"])
        for edge in result["edges"] if edge["kind"] == "DATA_DEP"
    )
    assert deps == [
        (("except_handler", 5, "except Exception"), ("raise", 9, "raise"), "inner"),
        (("except_handler", 10, "except Exception"), ("return", 11, "return outer"), "outer"),
        (("parameter", 1, "exc"), ("raise", 4, "raise exc"), "exc"),
        (("raise", 4, "raise exc"), ("except_handler", 5, "except Exception"), "exception-value"),
        (("raise", 4, "raise exc"), ("except_handler", 10, "except Exception"), "exception-value"),
        (("raise", 9, "raise"), ("except_handler", 10, "except Exception"), "exception-value"),
        (("raise", 9, "raise"), ("except_handler", 10, "except Exception"), "exception-value"),
    ]
