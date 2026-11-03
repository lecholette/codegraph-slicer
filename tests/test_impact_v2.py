import json
import shlex

import pytest

from codegraph_slicer import GraphStore
from codegraph_slicer.cli import main


def database(tmp_path, count=30):
    root = tmp_path / 'src'
    root.mkdir()
    source = 'def target():\n    return 1\n\n'
    for n in range(count):
        source += f'def caller_{n:03}():\n    return target()\n\n'
    source += 'def parent():\n    return caller_000()\n\ndef unknown():\n    return missing()\n'
    (root / 'sample.py').write_text(source)
    db = tmp_path / 'graph.sqlite'
    with GraphStore(db) as store:
        store.sync_workspace(root)
    return db


def invoke(db, capsys, *flags):
    assert main(['impact', 'sample:target', '--db', str(db), '--snapshot', *flags]) == 0
    return json.loads(capsys.readouterr().out)


def callers(result):
    value = result['callers']
    if isinstance(value, dict):
        return [dict(zip(value['columns'], row)) for row in value['rows']]
    return value


def test_compact_profile_edges_and_smallest_layout(tmp_path, capsys):
    db = database(tmp_path)
    full = invoke(db, capsys)
    assert full['profile'] == 'python-call-impact-v2'
    rows = callers(full)
    assert rows == [
        {'symbol': f'sample:caller_{n:03}', 'depth': 1, 'to': None, 'line': 5 + n * 3}
        for n in range(30)
    ] + [{'symbol': 'sample:parent', 'depth': 2, 'to': 'sample:caller_000', 'line': 95}]
    columns = ['symbol', 'depth', 'to', 'line']
    columnar = {'columns': columns, 'rows': [[row[key] for key in columns] for row in rows]}
    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    assert len(encode(full['callers'])) == min(len(encode(rows)), len(encode(columnar)))
    assert full['omitted'] == {'max_nodes': 0, 'budget': 0}
    assert full['unknown_boundary_count'] == 1
    assert full['unknown_boundaries_omitted'] == 1
    assert full['truncated'] is False
    one = invoke(db, capsys, '--max-nodes', '1')
    assert one['callers'] == [rows[0]]
    assert one['omitted'] == {'max_nodes': 30, 'budget': 0}


def test_budget_fills_ordered_prefix_and_retrieves_all(tmp_path, capsys):
    db = database(tmp_path, 80)
    full = invoke(db, capsys, '--unknown-boundaries', '10', '--max-tokens', '100000')
    partial = invoke(db, capsys, '--max-nodes', '60', '--unknown-boundaries', '10', '--max-tokens', '650')
    rows = callers(partial)
    assert 1 < len(rows) < 60
    assert rows == callers(full)[:len(rows)]
    assert partial['omitted'] == {'max_nodes': 21, 'budget': 60 - len(rows)}
    assert partial['unknown_boundary_count'] == 1
    assert partial['truncated'] is True
    args = shlex.split(partial['retrieve_details'])
    assert args[:2] == ['codegraph-slicer', 'impact']
    assert main(args[1:]) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert callers(recovered) == callers(full)
    assert recovered['unknown_boundaries'] == full['unknown_boundaries']
    assert recovered['omitted'] == {'max_nodes': 0, 'budget': 0}
    assert recovered['tokens'] <= int(args[args.index('--max-tokens') + 1])
    assert partial['tokens'] <= 650


def test_markdown_agrees_and_tiny_budget_refuses(tmp_path, capsys):
    db = database(tmp_path)
    assert main(['impact', 'sample:target', '--db', str(db), '--snapshot', '--format', 'markdown', '--max-tokens', '650']) == 0
    markdown = capsys.readouterr().out
    result = json.loads(markdown.split('```json\n', 1)[1].split('\n```', 1)[0])
    assert result['profile'] == 'python-call-impact-v2'
    assert callers(result)[0] == {'symbol': 'sample:caller_000', 'depth': 1, 'to': None, 'line': 5}
    assert result['tokens'] == len(markdown) // 4 + 1
    assert result['omitted']['budget'] == 31 - len(callers(result))
    args = shlex.split(result['retrieve_details'])
    assert main(args[1:]) == 0
    recovery_markdown = capsys.readouterr().out
    recovered = json.loads(recovery_markdown.split('```json\n', 1)[1].split('\n```', 1)[0])
    assert [row['symbol'] for row in callers(recovered)] == [
        f'sample:caller_{n:03}' for n in range(30)
    ] + ['sample:parent']
    assert recovered['omitted'] == {'max_nodes': 0, 'budget': 0}
    assert recovered['tokens'] == len(recovery_markdown) // 4 + 1
    assert recovered['tokens'] <= int(args[args.index('--max-tokens') + 1])
    with pytest.raises(SystemExit) as error:
        main(['impact', 'sample:target', '--db', str(db), '--snapshot', '--max-tokens', '1'])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'header and first caller' in captured.err


def test_refusal_boundary_preserves_first_caller(tmp_path, capsys):
    db = database(tmp_path)

    def attempt(budget):
        try:
            status = main([
                'impact', 'sample:target', '--db', str(db), '--snapshot',
                '--max-tokens', str(budget),
            ])
        except SystemExit as error:
            status = error.code
        captured = capsys.readouterr()
        return status, captured.out, captured.err

    lower, upper = 1, 100000
    while lower < upper:
        middle = (lower + upper) // 2
        status, _, _ = attempt(middle)
        if status == 0:
            upper = middle
        else:
            assert status == 2
            lower = middle + 1
    minimum = lower
    status, output, error = attempt(minimum - 1)
    assert status == 2
    assert output == ''
    assert f'header and first caller require {minimum} tokens' in error
    status, output, error = attempt(minimum)
    assert status == 0
    assert error == ''
    result = json.loads(output)
    assert callers(result) == [
        {'symbol': 'sample:caller_000', 'depth': 1, 'to': None, 'line': 5}
    ]
    assert result['omitted'] == {'max_nodes': 0, 'budget': 30}
    assert result['tokens'] == minimum
    assert result['tokens'] == len(output) // 4 + 1
