# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
archiver-rag is in beta: MCP tools, CLI commands and config may still change before 1.0.

## [0.2.0] — 2026-09-XX (beta)

First public beta. Feedback is very welcome: please [open an issue](https://github.com/FernandoJRR/archiver-rag/issues/new/choose).

### Upgrading from 0.1.0

```bash
pipx upgrade archiver-rag     # or: uv tool upgrade archiver-rag
archiver-rag sync             # re-indexes notes under the new path format
archiver-rag restart          # the watcher picks up the new code
```

If `pipx list` still shows 0.1.0 after upgrading, your install runs on Python 3.10, which 0.2.0 no longer supports: reinstall on a newer Python with `pipx install --force --python python3.12 archiver-rag` (or `uv tool install --python 3.12 archiver-rag`).

Then restart your MCP client so agents see the renamed tools. The first command you run migrates your config and index to the new locations automatically.

### ⚠️ Breaking changes

- **Config and data moved to XDG paths.** `~/.archiver-rag/` is replaced by `~/.config/archiver-rag/` (config), `~/.local/share/archiver-rag/` (index, centroids) and `~/.cache/archiver-rag/` (runtime state). Existing installs are migrated automatically on first run; the old folder is never deleted.
- **Python 3.11 or newer is required** (was 3.10).
- **MCP tool `cluster_vault` removed.** Whole-vault clustering is now CLI-only and experimental (`archiver-rag cluster`).
- **MCP tool `cluster_note` renamed to `suggest_folder`** and made suggestion-only: it no longer moves notes (`apply` removed). Use `move_notes` to act on a suggestion.
- **`log_note` filenames no longer start with a date.** The filename is the slugified title; the date lives in frontmatter.

### Added

- Semantic folder placement: each folder describes itself in a `_folder.md` sidecar, and notes are placed by similarity to those descriptions, falling back to their `type:` field.
- `archiver-rag describe` to generate or set folder descriptions; the optional `auto_describe` setting keeps them up to date as notes move.
- Inbox (off by default, `auto_inbox`): notes with no good folder wait in `inbox/` and are grouped into new folders once enough similar notes collect.
- Streamable HTTP transport (`archiver-rag serve --transport http`), plus a detached daemon managed with `archiver-rag start|stop|restart http`.
- `archiver-rag relink`: one-time repair that trims overgrown `## Related` sections.
- `archiver-rag delete`: moves notes to `.trash/` and removes links pointing at them.
- `archiver-rag sync` and `archiver-rag prune`: incremental re-index and removal of index entries for deleted files.
- `status` and `health` now explain service state, watcher activity, index-vs-disk drift and placement, and both support `--json`.
- Progress and log notifications from the MCP server during long tool calls.

### Changed

- `archiver-rag place` uses semantic placement instead of the wikilink-neighbour vote, and gains `--all`.
- Auto-linking keeps only close matches instead of a fixed number per save, so `## Related` no longer grows without bound.
- Search reranking recalibrated for the less dense link graph.
- `archiver-rag cluster --apply` now asks for confirmation before moving files.
- Dependencies now have tested version ranges.

### Fixed

- Notes saved by editors and agents through an atomic rename are now indexed; previously some were missed or evicted from the index.
- A deleted note's same-named copy in `.trash/` can no longer be picked up instead of the real note.
- Watcher log lines now appear immediately.

## [0.1.0] — 2026-06-12

Initial release: indexing of an Obsidian vault into ChromaDB, a file watcher with auto-linking, graph-aware semantic search, and an MCP server over stdio with `search_vault`, `get_connections`, `vault_status`, `move_notes`, `log_note`, `cluster_vault` and `cluster_note`.

[0.2.0]: https://github.com/FernandoJRR/archiver-rag/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/FernandoJRR/archiver-rag/releases/tag/v0.1.0
