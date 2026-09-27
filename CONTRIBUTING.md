# Contributing

Thanks for helping. archiver-rag is a small, one-person beta, so bug reports and feedback are as valuable as code. Use the [issue forms](https://github.com/FernandoJRR/archiver-rag/issues/new/choose); for security problems, follow [SECURITY.md](SECURITY.md) instead of opening an issue.

## Development setup

Requires macOS or Linux and Python 3.11 or newer.

```bash
git clone https://github.com/FernandoJRR/archiver-rag && cd archiver-rag
python3 -m venv .venv
.venv/bin/pip install --upgrade pip          # dependency groups need pip >= 25.1
.venv/bin/pip install --group dev -e .       # package + pytest + ruff
```

On Linux, add `--extra-index-url https://download.pytorch.org/whl/cpu` to the last command to avoid downloading the CUDA build of PyTorch.

To use your working copy as your real CLI and MCP server, also run `pipx install --editable .`. Restart the watcher (`archiver-rag restart`) and your MCP client after changing code; neither reloads it on its own.

[`AGENTS.md`](AGENTS.md) describes the architecture and the reasoning behind most design decisions. Read the relevant part before changing a module.

## Tests

```bash
.venv/bin/pytest -m "not slow and not vault"   # what CI runs
.venv/bin/pytest                               # everything, including slow tests
```

- `slow` tests load the embedding model (about 90 MB, downloaded once).
- `vault` tests run against a real vault and only when `ARCHIVER_RAG_VAULT` is set.

Tests must never touch your real vault or config. `tests/conftest.py` enforces this: every test gets a `get_vault_path()` that raises, and redirected config/data/cache dirs. Use the `tmp_vault` and `tmp_install` fixtures when a test needs real files. If a new module imports `get_vault_path` at module level, add it to `_MODULES_WITH_VAULT` in `conftest.py`.

## Lint

```bash
.venv/bin/ruff check .
```

The rule set is pinned in `pyproject.toml`, so results match CI.

## Commits and pull requests

- Commit messages use a type prefix and a short summary: `feat: …`, `fix: …`, `docs: …`, `refactor: …`, `chore: …`.
- Keep a pull request to one change, with tests for new behaviour.
- Add a line to [`CHANGELOG.md`](CHANGELOG.md) for anything users will notice.
- CI (tests on Python 3.11–3.14, macOS and Linux, plus ruff) must pass before merging.

## Releasing (maintainer)

1. Bump `version` in `pyproject.toml` and the version shown in `docs/index.html` (hero label and footer). `tests/test_site_version.py` fails until both match.
2. Add the release to `CHANGELOG.md` with its date.
3. Push, wait for CI, build, upload to PyPI, then tag `vX.Y.Z` and create the GitHub Release from the changelog entry.
