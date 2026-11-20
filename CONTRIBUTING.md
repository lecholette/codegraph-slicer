# Contributing to `codegraph-slicer`

Pull requests and issue reports are welcome!

## Development Setup

`codegraph-slicer` requires **Python 3.12+** (`3.12`, `3.13`, or `3.14`) and `git` on `PATH`. Its only runtime dependencies are the prebuilt `tree-sitter` wheels (`tree-sitter`, `tree-sitter-java`, `tree-sitter-typescript`, `tree-sitter-javascript`).

Clone a fork of the repository and run the test suite with [`uv`](https://docs.astral.sh/uv/) (or a standard `venv`):

```bash
git clone https://github.com/<your-username>/codegraph-slicer.git
cd codegraph-slicer
uv run --with pytest pytest -q
```

Or with standard `venv` + `pip`:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e . pytest
.venv/bin/pytest -q
```

## Submitting a Pull Request

1. Create a feature or bugfix branch from `main`:
   ```bash
   git checkout -b my-change
   ```
2. Add or update unit tests under `tests/` covering the behavior you are adding or fixing.
3. Verify the full test suite passes (`uv run --with pytest pytest -q`).
4. Open a pull request against `lecholette/codegraph-slicer` (`main`). GitHub Actions CI will run the test suite automatically across Python 3.12 and 3.13.

## License

By contributing to `codegraph-slicer`, you agree that your contributions will be licensed under the [Apache-2.0 License](LICENSE).
