# archiver-rag — Agent Context

## What this project is

`archiver-rag` is a Python CLI tool and MCP server that provides semantic search and knowledge structuring for Obsidian vaults. It runs as a background service and exposes tools to any MCP-compatible AI agent (Claude Code, Cursor, Gemini CLI, etc).

It is **not** a project management tool. The vault is treated as a **knowledge graph** — notes are nodes, wikilinks are edges, some tightly related, others loosely. There are no mandatory project folders.

---

## Architecture

```
Obsidian vault (.md files)
        ↓ ingest pipeline
    ChromaDB (persistent vectors)
        ↓ MCP server
    Any MCP-compatible agent
```

### Ingest pipeline (3 approaches layered)

1. **Contextual prefix** — before embedding each chunk, prepend note metadata (location, tags, wikilinks) so embeddings carry structural context
2. **Rich metadata** — store folder, tags, links, incoming_count in ChromaDB for filtered search
3. **Graph reranking** — after ChromaDB returns candidates, re-score by graph proximity to query note and hub importance

### Auto-linking

After every ingest, `linker.py` runs semantic search against the vault and appends a `## Related` section with `[[wikilinks]]` to the note. This builds the knowledge graph automatically without agent intervention.

---

## Package structure

```
archiver_rag/               ← Python package (pip installable)
├── __init__.py
├── cli.py                  ← typer CLI, all user-facing commands
├── init_cmd.py             ← setup wizard, config at $XDG_CONFIG_HOME/archiver-rag/config.json
├── paths.py                ← XDG path resolution (config/data/cache dirs) + legacy ~/.archiver-rag migration
├── runtime.py              ← watcher heartbeat ($XDG_CACHE_HOME/archiver-rag/runtime.json) — written by watcher, read by status
├── report.py               ← compose + render for `status` and `health` (shared seam with --json)
├── service.py              ← launchd/systemd management for BOTH daemons (watcher + detached HTTP)
├── utils.py                ← get_vault_path(), build_link_map(), note_stems(), is_hidden_path(), is_indexable_note(), find_note(), extract_frontmatter(), load_config(), FOLDER_NOTE_NAME
├── wikilinks.py            ← context-aware wikilink extractor (offset-based code masking)
├── watcher.py              ← watchdog file watcher, calls ingest + auto_link + sweep_dead_links
├── core/
│   ├── embedder.py         ← sentence-transformers all-MiniLM-L6-v2, lazy loaded
│   ├── chunker.py          ← 500 word chunks, 50 word overlap
│   ├── db.py               ← ChromaDB PersistentClient, cosine distance
│   ├── ingest.py           ← full ingest pipeline + prune_orphans
│   ├── search.py           ← search_vault() with type/tags filter
│   └── index_stats.py      ← index-vs-disk drift (missing / orphaned / stale) for `health`
├── graph/
│   ├── rerank.py           ← approach 3 post-processing reranker
│   ├── connections.py      ← get_connections() — BFS wikilink traversal
│   ├── linker.py           ← auto-linking after ingest (not exposed to agents)
│   ├── clustering.py       ← label propagation clustering + cluster_note (internal helper, both signals)
│   ├── terms.py            ← term extraction: tags / c-TF-IDF + MMR + adaptive α (§4)
│   ├── centroids.py        ← fingerprint-keyed centroid cache ($XDG_DATA_HOME/archiver-rag/centroids.json) + weighted_cosine()
│   ├── placement.py        ← suggest_folder() — cosine vs descriptions + type: fallback
│   └── inbox.py            ← Gate 2: inbox clustering by embedding similarity (group_inbox_notes, name_cluster, maybe_spin_out_clusters)
├── vault/
│   ├── notes.py            ← log_note(), delete_notes(), sweep_dead_links()
│   ├── reorganize.py       ← move_notes() with wikilink + YAML rewriting
│   ├── health.py           ← vault_status() — structure + health + tags + recent
│   └── folder_notes.py     ← FolderNote dataclass, read/write/discover _folder.md
└── mcp/
    ├── server.py           ← tool schemas + dispatch (transport-agnostic low-level Server)
    ├── http.py             ← streamable HTTP transport: build_app() / serve_http()
    └── register.py         ← writes MCP entry to ~/.claude.json (stdio, or url= for HTTP)
tests/                      ← 447 tests, run with the pipx venv python (see Testing)
├── conftest.py             ← vault safety fixtures (_no_real_vault, tmp_vault) + home-path safety fixtures (_no_real_home_paths, tmp_install)
├── test_wikilinks.py       ← unit tests for wikilinks.py
├── test_linker_section.py  ← characterization tests for _append_links_section
├── test_linker_prune.py    ← dead-target pruning (valid_stems)
├── test_linker_margin.py   ← margin-based candidate selection (select_related_candidates) + keep_targets rebuild trimming
├── test_delete_note.py     ← delete_notes() — trash, collisions, traversal
├── test_sweep_dead_links.py ← sweep_dead_links() in isolation
├── test_watcher_moved.py   ← atomic-save rename handling in on_moved
├── test_watcher_delete.py  ← spurious-delete guard + sweep on real deletes
├── test_watcher_cluster.py ← _maybe_cluster gating (new notes only, no churn, Stage B) + birth description on new destination folders
├── test_watcher_folder_note.py ← _folder.md watcher branch: refresh/drop centroid
├── test_watcher_describe.py ← auto_describe config reader, _maybe_redescribe, structural-only wiring, vacancy tracking + archival
├── test_log_note.py        ← log_note() birth-time description: new folder described, manual/auto untouched, extraction failure non-fatal
├── test_folder_notes.py    ← FolderNote read/write/discovery, apply_extracted_terms, empty_sweeps, archive_folder_note (vault/folder_notes.py)
├── test_terms.py           ← term extraction: tokenizer, Related stripping, c-TF-IDF, MMR
├── test_centroids.py       ← fingerprint cache: hit/miss/staleness, drop, unit vectors, weighted_cosine
├── test_placement.py       ← suggest_folder: semantic win, type fallback, scores
├── test_inbox_clustering.py ← Gate 2: group_inbox_notes, name_cluster, maybe_spin_out_clusters end-to-end
├── test_folder_note_exclusion.py ← _folder.md absent from stems, link_map, adjacency, health
├── test_paths.py           ← XDG dir resolution + ensure_migrated() (fresh install, no-op, full/partial migration, never deletes legacy dir)
├── test_runtime_state.py   ← heartbeat round-trip, counters, corrupt/missing file, unwritable cache dir never raises
├── test_service_state.py   ← launchctl/systemctl parsing: running, crash-loop (loaded, no PID), not-loaded, unsupported platform
├── test_index_stats.py     ← drift categories, int-mtime staleness, _folder.md/.trash exclusion, unreachable Chroma reported not raised
├── test_report_render.py   ← status/health rendering from composed dicts: unconfigured, no-heartbeat, PID mismatch, crash-loop, JSON-serializable
├── test_watcher_heartbeat.py ← record_event fires on real events, NOT on spurious deletes or _folder.md writes
├── test_mcp_http.py        ← HTTP transport in-process via ASGITransport: handshake, 6 tools, real call, Host allowlist, SSE responses, in-flight notification streaming
├── test_mcp_dispatch.py    ← _dispatch routing, write lock held only by mutating tools, event loop not blocked, per-tool notification emission + Notifier fail-soft
├── test_serve_flags.py     ← serve --transport/host/port defaults, non-loopback warning, register_mcp both shapes
└── test_rerank.py          ← hub_boost saturation/scaling (graph/rerank.py)
persistence/
└── chroma_db/              ← dev only, gitignored. Production: $XDG_DATA_HOME/archiver-rag/chroma_db/ (~/.local/share/archiver-rag/chroma_db/)
```

---

## CLI commands

```bash
archiver-rag init           # one-time setup wizard
archiver-rag start          # start watcher service (default target)
archiver-rag start http     # install + start detached MCP HTTP server (launchd/systemd)
archiver-rag stop           # stop watcher service
archiver-rag stop http      # stop detached MCP HTTP server
archiver-rag restart        # restart watcher service
archiver-rag restart http   # restart detached MCP HTTP server
archiver-rag status         # service liveness + watcher activity + index drift + effective config
archiver-rag status --json  # same report as JSON (scriptable)
archiver-rag index          # force re-index entire vault (includes prune_orphans)
archiver-rag sync           # ingest only new/modified notes + prune_orphans
archiver-rag prune          # remove index chunks whose source file no longer exists
archiver-rag search "query" # test search directly
archiver-rag health         # index-vs-disk drift + vault health (orphans, broken links, tags)
archiver-rag health --json  # same report as JSON
archiver-rag logs           # tail /tmp/archiver-rag.log
archiver-rag uninstall      # remove all data, service, MCP registration
archiver-rag log "title" --type decision --tag arch --related NoteA  # create knowledge note (opens editor for content)
archiver-rag delete <note>... [--yes]  # move to .trash/ + sweep inbound wikilinks (recoverable)
archiver-rag relink         # dry-run: report per-note ## Related before/after under the margin rule + vault density
archiver-rag relink --apply [--margin <n>] [--yes] [--no-restart-watcher] [--no-sync]  # one-time repair — rebuild every note's Related section, then sync_vault to re-embed just what changed; stops/restarts the watcher itself around it unless --no-restart-watcher
archiver-rag cluster        # suggest folder groupings via label propagation
archiver-rag cluster --apply           # same, but moves files automatically
archiver-rag cluster --min-size <n>    # minimum notes per cluster (default 2)
archiver-rag place <note>              # suggest folder for a single note (semantic + type fallback)
archiver-rag place <note> --apply      # same, but moves the file
archiver-rag place --all               # dry-run: show current vs suggested for every note + distribution
archiver-rag place --all --apply       # batch move all notes to their semantic suggestion
archiver-rag config --auto-cluster     # enable auto-clustering in watcher
archiver-rag config --cluster-threshold <n>  # notes before full re-cluster (default 5)
archiver-rag config --placement-threshold 0.55  # cosine threshold for semantic placement
archiver-rag config --no-type-fallback  # disable type: fallback when below threshold
archiver-rag config --auto-describe    # enable watcher auto-regeneration of _folder.md on membership change (off by default)
archiver-rag serve          # internal — MCP server over stdio (called by the MCP client)
archiver-rag serve --transport http [--host --port --path --allowed-host --stateful]
archiver-rag watch <path>   # internal — watcher (called by service)
```

---

## MCP tools (exposed to agents)

### Search & discovery
- **`search_vault(query, n_results, min_score, context_note, type, tags)`** — semantic search with graph reranking. `context_note` boosts notes directly connected via wikilinks. `type` filters on frontmatter `type:` field (stable — unaffected by `auto_cluster` moves). `tags` post-filters on comma-string. Returns `relevance_score`, `base_score`, `graph_boost`, `hub_boost`, `type` per result.
- **`get_connections(note, depth)`** — BFS wikilink traversal. depth=1 direct links, depth=2 neighbors of neighbors. Returns outgoing, incoming, and flat all_connected list.

### Vault health
- **`vault_status()`** — single call returns structure (total notes, folders), health (orphaned, no frontmatter, empty, broken links), tags (most used, total unique), recent activity (modified, created).

### Reorganization
- **`move_notes(moves, update_links)`** — move 1 to N files. Rewrites [[wikilinks]] across vault after moving .md files. Returns structured success/failure report. Path traversal protected.

### Placement (suggestion only)
- **`suggest_folder(note)`** — semantic folder placement for a single note. Suggestion only — it never moves anything; call `move_notes` yourself to act. **Primary signal (Stage B):** cosine similarity against declared folder descriptions (`_folder.md`). Falls back to frontmatter `type:` field if no folder clears the threshold. Reads the same config as the CLI `place` command and the watcher (`placement_similarity_threshold`, `type_fallback`, `advanced.placement_weights`, `advanced.name_prefix_bonus`), so its suggestion always matches theirs. Returns `suggested_folder`, `similarity`, `reason` (`"semantic" | "type" | "none"`), `scores` (all folder similarities), and `neighbor_vote` (the old wikilink-neighbour vote, preserved as an informational secondary signal).

### Knowledge logging
- **`log_note(title, content, type, tags, related_notes)`** — creates a note in `vault/{type}/`. `type` is free-form and becomes the folder (e.g. `decision`, `meeting`, `lesson`, `idea`). Filename is the slug — no date prefix. Returns `created` (relative path), `type`, `title`, `tags`, `related`, `path`. Watcher auto-indexes and auto-links it — no extra steps needed.

### Not exposed to agents (internal)
- `auto_link()` — called by watcher automatically, not an MCP tool
- `cluster_vault()` / whole-vault label propagation — **removed as an MCP tool entirely** (nothing depended on it; superseded by `suggest_folder`, `place --all`, and Gate 2 inbox clustering). Stays reachable only via the manual, experimental `archiver-rag cluster` CLI command, which requires confirmation before `--apply`.

**In-flight status notifications** — while a tool call runs, the server pushes `notifications/message` log entries (level `info`, `logger` = tool name) to the connected client, plus `notifications/progress` when the client supplied a `progressToken` for the request. These reach the client's UI/log surface, **not** the LLM's context — see the "Server→client status notifications" entry under Key technical decisions.

---

## Key technical decisions

**ChromaDB with cosine distance** — must use `metadata={"hnsw:space": "cosine"}` when creating collection. L2 distance produces negative scores after `1 - dist` normalization.

**Absolute paths in db.py** — ChromaDB path must be absolute. Relative paths break when the MCP server is launched from a different working directory by the MCP client.

**Lazy model loading in embedder.py** — `SentenceTransformer` loads only when `embed()` is first called, not at import time. This prevents model loading on CLI commands like `status` and `stop`.

**Offline mode detection** — if model is cached at `~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2`, set `TRANSFORMERS_OFFLINE=1` to avoid network calls.

**Internal CLI commands** — `serve` and `watch` are registered with `hidden=True` in typer. Typer does not allow commands starting with `_` or `-`.

**pipx for installation** — use `pipx install --editable .` not `pip install -e .`. pipx creates an isolated venv and exposes the CLI globally on PATH, which is required for MCP registration to find the correct executable.

**MCP registration** — `mcp/register.py` uses `which archiver-rag` to find the global executable and writes it to `~/.claude.json`. Must use `--scope user` when registering via `claude mcp add` so the server is available in all directories.

**Watcher event paths** — `event.src_path` is `bytes | str` in watchdog. Always cast with `str(event.src_path)` before use.

**Atomic saves arrive as `moved`, not `modified` (`watcher.py`)** — editors and agent file-write tools (Claude Code's Write/Edit tool, and equivalents in other coding agents) do not write notes in place. They write `<name>.md.tmp.<pid>.<rand>` and rename it over the target. Verified against the live vault with a watchdog spy:

```
created  spy-scratch.md.tmp.17249.14dfc4f46218
modified spy-scratch.md.tmp.17249.14dfc4f46218
moved    spy-scratch.md.tmp.17249.14dfc4f46218 → spy-scratch.md
deleted  spy-scratch.md          ← spurious, fires after the file is back
```

The **only** event naming the real file is the move, and its *source* is not a `.md`. `on_moved` therefore checks `src` and `dst` independently — keying off `src` alone silently dropped every such save and the note never reached the index. `dst_is_note` → ingest + auto_link; `src_is_note` → evict the old index entry; both → also rewrite wikilinks when the stem changed. `src_is_note and not dst_is_note` means the note left note-space (moved into `.trash/`, which is how Obsidian deletes, or renamed to a non-`.md`) so that branch sweeps.

**Spurious delete guard (`watcher.py::_is_spurious_delete`)** — the trailing `deleted` above fires *after* the file is back on disk. Acting on it ran `collection.delete()` and evicted the note on every save; three notes were found on disk but absent from the index this way. With the sweep wired into `on_deleted` it became a data-loss risk, since the sweep rewrites *other* notes. The guard returns immediately when the file is already back (a normal save costs nothing) and otherwise polls up to `DELETE_SETTLE_SECONDS` (1.0). A bare existence check is insufficient: the `unlink + rewrite` save pattern emits `deleted → created → modified`, where the file genuinely is absent at delete time.

**`utils.log()` — always flush (`utils.py`)** — the service redirects stdout to `/tmp/archiver-rag.log`, so Python block-buffers it. Before this, `archiver-rag logs` showed an *empty tail* for events already processed, which sent a debugging session chasing ghosts. Anything running inside the watcher process must print through `log()`, not `print()` — that includes `core/ingest.py`, whose "Indexed N chunks" lines share the same stdout. The two `print()` calls left in `ingest.py` are inside its `__main__` dev block and never run under the service.

**Clustering fires once, and only for new notes (`watcher.py::_maybe_cluster`)** — shared by `on_created` and `on_moved`. Two guards, both learned from live misbehaviour:
- **New notes only.** `on_moved` fires on every save, so it passes `is_new_note = not src_is_note and not _is_indexed(dst_source)` — computed *before* ingest, or the note being indexed would always look pre-existing. Without this, editing a note would relocate it mid-typing. `_is_indexed` returns `True` on any ChromaDB error: clustering moves files, so uncertainty must resolve to doing nothing.
- **Skip when already in the target folder.** `cluster_note` happily suggests the folder a note is already in. The handler used to re-issue a move onto the note's own path; `move_notes` rejected it, but the log still claimed `Auto-placed` and each attempt re-triggered ingest + auto_link. One new note produced five such rounds. Now it returns early, and `Auto-placed` is logged only when `move_notes` reports an actual move.

**ChromaDB type stubs** — query results are typed as potentially `None` in stubs. Use `(results.get("documents") or [[]])[0]` pattern and `# type: ignore[index]` where needed.

**`is_indexable_note(path)` (`utils.py`) — single gate for all 11 note-enumeration sites** — a file is a real vault note iff it ends in `.md`, is not hidden (`is_hidden_path`), and is not the folder-description sidecar (`is_folder_note`). The `.md` suffix alone is insufficient: `_folder.md` passes the suffix check and has no dot-prefixed path component, so it was previously invisible to all guards. Every `rglob("*.md")` loop and every watcher handler goes through this function. Do not add a fourth note-type by checking for its name in scattered `if` blocks — extend `is_indexable_note`.

**`_folder.md` sidecar (`FOLDER_NOTE_NAME` in `utils.py`)** — per-folder description file for semantic placement (Stage A). Excluded from ChromaDB, from `auto_link`, and from the wikilink graph. Writable and readable in Obsidian. Frontmatter: `description_terms`, `distinctive`, `note_count`, `updated`, `source: auto|manual`. `source: manual` is never overwritten by `archiver-rag describe --all`. The file is a metadata artifact, not a note: it should not appear in `vault_status`, `note_stems`, `build_link_map`, or `_build_adjacency`. If you find it appearing in any of those, check `is_indexable_note`.

**Term extraction (`graph/terms.py`)** — `extract_terms(vault, rel_folder)` returns `(description_terms, distinctive)`. Small folder (< `term_extraction_min_notes`, default 4): frontmatter tag frequency. Large folder: c-TF-IDF (`tf * log(1 + A / f_t)`) against all describable folders, then MMR for diversity. **Critical:** the `## Related` section is stripped before counting tokens — `linker.py` writes several neighbour slugs per note, and counting them produces a list of other notes' filenames, not a description of the folder. `_strip_related_section` was promoted to `utils.py::strip_related_section` — see "Desaturating the wikilink graph" below, it now has consumers outside this module too. Tokenizer: `_normalize` (Unicode NFKD, accent-strip) then `[a-z0-9][a-z0-9-]{2,}` — hyphenated identifiers like `wikilink-resolver` survive as one term. `embed()` output is unnormalized; every cosine must divide by norms explicitly. `distinctive` is highest raw IDF (rarest across folders), not MMR-diversified.

**`extract_frontmatter` promoted to `utils.py`** — was `_extract_frontmatter` in `core/ingest.py`. Two modules need it; the old location was private and duplicated. `core/ingest.py` now imports it from `utils`.

**`load_config()` in `utils.py`** — returns `{}` on any read/parse error; callers must use `config.get(key, default)`. Do not change `_get_cluster_config` (`watcher.py:54`) to call `load_config()` naively: its error path currently returns `(False, 5)` (auto_cluster **off**), while `load_config()` returning `{}` would give `config.get("auto_cluster", True)` = **on**. A corrupt config would start moving files. Keep the two branches separate.

**Graph reranker (`graph/rerank.py`)** — runs after ChromaDB returns candidates. Three signals combined additively: `base_score = 1 - dist/2` (semantic), `graph_boost` (+0.10 per direct wikilink direction, max +0.20), `hub_boost = min(incoming_count * 0.02, 0.10)`. `min_score` is applied to `base_score` before boosting. `vault_path` defaults to `utils.get_vault_path()` when not passed. Returns `type` field from metadata alongside scores. **The 0.02/0.10 constants were calibrated for a sparse graph and are stale post-desaturation** (see immediately below) — `hub_boost` capped out at just 5 inbound links while the live vault's mean was 25.7 (90% of notes ≥5, i.e. maxed), making it a near-constant that ranks nothing; `graph_boost` fired on ~44% of candidates for the same reason. Not yet recalibrated — deliberately deferred until `relink --apply` has actually run against the live vault and a real post-repair inbound-link distribution exists to calibrate against, rather than guessing new constants in the same change that changed the input distribution.

**Desaturating the wikilink graph — margin-based candidate selection replaces `auto_link`'s per-run cap (`graph/linker.py`, `core/ingest.py`).** Measured on the live vault before this change: density 0.66 (2138 edges / 81 notes), mean 25.2 `## Related` links per note (max 42, against 1.2 hand-written links/note), average note a direct neighbour of 44% of the vault. This wasn't cosmetic — it made `hub_boost`/`graph_boost` near-constant (see above) and is the documented mechanism behind the 2026-08-20 folder-collapse incident (label propagation on a near-complete graph has one giant community) and the reason Gate 2's inbox-clustering design deliberately never clusters by wikilink topology at all (see "Folder lifecycle (Gate 2)" below). Three root causes, all measured, not inferred:

1. **The old cap (`max_links=5`) was per-run, not per-note.** Nothing bounded the total across saves; existing dead-target pruning (`valid_stems`) only dropped links whose target file no longer exists, never a link that still resolves but is no longer relevant. Links accumulated ≈ 5 × times-saved.
2. **`min_score=0.55` filtered almost nothing.** Measured across 10 random notes: ~30 of ~80 candidates cleared 0.55 *every time* — this vault's three projects share enough technical vocabulary under `all-MiniLM-L6-v2` that "no close relatives" essentially never happens. The per-run cap therefore saturated on every single run.
3. **`## Related` was itself embedded, closing a feedback loop.** `ingest_file` chunked the raw body with nothing stripped, unlike `graph/terms.py` and `graph/placement.py`, which both already stripped it for exactly this reason. A growing list of neighbour filenames became embedded chunk text, so notes became "similar" partly *because* they linked to the same notes — confirmed live: a chunk that was nothing but wikilinks ranked #1 at 0.835 in a real search, and stripping Related shifted top candidate scores by up to 0.06. `auto_link`'s own query (`content.split()[:500]`) had the identical problem, compounding it.

**Fix, in two parts.** First, `strip_related_section` (promoted from `graph/terms.py::_strip_related_section` to `utils.py`, now with three consumers) is applied before embedding in both `core/ingest.py::ingest_file` (the indexed chunks) and `graph/linker.py::auto_link` (the query itself) — breaks the feedback loop. Second, `auto_link`'s fixed cap is replaced by `graph/linker.py::select_related_candidates`: fetch a wide candidate pool (`n_results=40` — wide on purpose, since the margin rule needs a score for *currently-linked* notes too, not just first-time candidates), then keep every candidate within `advanced.link_margin` (default `0.05`) of the *top* candidate's score, capped by `advanced.max_total_links` (default `15`, a safety ceiling well above the observed range — not the primary control). Measured at margin 0.05 across 10 real notes: kept counts range 3–12 (mean 5.7–7.3, median 6–8) — genuinely adaptive, not a disguised fixed cap: a note in a dense neighbourhood (e.g. `weekly-cuisine-architecture-spec-v1`) keeps more links than an isolated one. Margin 0.03 was too tight (median 3); 0.08 drifted back toward saturation (median 12–13) — 0.05 was chosen as the point where the range stayed genuinely note-dependent rather than collapsing toward either extreme.

`## Related` changed from **append-only to rebuilt**: `_append_links_section` gained a `keep_targets` parameter (independent of, and composable with, the existing `valid_stems` dead-link pruning) — existing links whose target falls outside the margin-selected set are trimmed on rebuild, not just left to accumulate forever. Conservative by construction, `keep_targets=None` (default) disables trimming entirely so every pre-existing test in `test_linker_section.py`/`test_linker_prune.py` passes unchanged; when trimming is active, a target is still spared if it also appears as a wikilink in the note body outside `## Related` (user-authored, deliberately duplicated — never drop those) or is path-style (`[[folder/Note]]`, same conservative exception `valid_stems` pruning already used, since path stems can't be resolved by `Path.stem`).

`select_related_candidates` is shared by `auto_link` (single note, called by the watcher) and the `archiver-rag relink` CLI command (whole-vault repair pass) — one selection rule, not two that could drift apart. `relink` exists because the margin rule only stops *future* growth; pre-existing notes already carried the backlog and nothing retroactively trims it. Dry-run by default (per-note before/after counts, vault-wide density estimate, no writes); `--apply` rewrites. **`--apply` stops the watcher itself for the duration of the rewrite and restarts it afterward** (`--no-restart-watcher` to opt out and manage it yourself) — each rewrite would otherwise fire a watcher re-ingest + `auto_link` mid-pass, and worse, interleave with `relink`'s own in-progress `collection.query` calls for other notes still being analyzed, since both processes read/write the same ChromaDB collection concurrently. This isn't a bypass of AGENTS.md's "don't stop the watcher without asking" rule: the stop/restart is gated behind the same confirmation prompt every other `--apply` command in this codebase already uses (`place --all --apply`, `cluster --apply`) — the prompt *is* the asking — and it's wrapped in `try/finally` so an exception mid-write still restarts the watcher rather than leaving it down. Dry-run analysis alone never writes and is always safe to run regardless of watcher state. **`--apply` also re-embeds afterward by default** (`--no-sync` to opt out) — via `sync_vault`, not `index`: `sync_vault` is mtime-based and re-embeds only the notes `relink` actually rewrote, where `index` (`ingest_vault`) unconditionally re-embeds the entire vault regardless of which notes changed. The sync runs inside the same stopped-watcher window as the rewrite, before the watcher restarts, so nothing observes a stale index in between. Procedure is now just `relink` (review) → `relink --apply` (rewrite, sync, and the watcher stop/restart are all handled by the one command). **Run live 2026-08-25** against the real vault — see the Closed entry below and Pending work for the measured before/after numbers.

**Knowledge logging (`vault/notes.py`)** — single `log_note()` function; `type` is free-form and becomes the folder name. Filename: `{slug}.md` — **no date prefix**. The filename is the note's identity and wikilinks are written against it; the date lives in frontmatter (`date:`) where it stays queryable. `_slugify` lowercases, strips non-word chars, replaces spaces/underscores with `-`, caps at `SLUG_MAX` (120) — deliberately generous, because a truncated slug makes the filename disagree with the identity that links target. Collision-safe: appends `-1`, `-2` if file exists. `type` field strips path separators to prevent traversal. Frontmatter is built manually as a string. Watcher picks up the new file automatically.

**Wikilink extraction (`wikilinks.py`)** — offset-based code masking; never mutates the text. `extract_wikilinks(text)` is the drop-in for `WIKILINK_RE.findall`. Requires closing `]]` and excludes `\n` from target — prevents `[[Foo` eating a paragraph. Frontmatter excluded from code detection (4-space YAML would be misread as indented code). `skip_code=False` in `_get_existing_links` — permissive on write to avoid auto_link writing duplicates of real links the masker misclassified.

**`_append_links_section` (`graph/linker.py`)** — uses heading-match + slicing, not `re.sub`. Finds `## Related` with `_RELATED_HEADING_RE` (MULTILINE, tolerates any heading level and trailing spaces), determines body end at the next same-or-higher-level heading. Existing `[[Foo|alias]]` and `[[Foo#Section]]` are kept verbatim; deduplication compares on `wl.target`. Returns the original string object unchanged when nothing is added (no-op write guard in `auto_link`).

**`_update_wikilinks` (`vault/reorganize.py`)** — handles `[[old#heading]]`, `[[old|alias]]`, `[[old#heading|alias]]` via one regex with optional tail groups. Also rewrites bare names in YAML `related:` blocks via a separate MULTILINE pattern. Left code-unaware deliberately (see `wikilinks.py` for context-aware extraction).

**Clustering algorithm (`graph/clustering.py`)** — label propagation on an undirected wikilink graph. Each note starts as its own label; each iteration adopts the most common label among its neighbors (`Counter.most_common`); stops when no labels change or `max_iterations=50` reached. Communities are named after their most internally-connected note (highest internal degree). `_build_adjacency` is called by both `cluster_vault` and `cluster_note` — consider caching if vault is large. `apply_clusters` calls `move_notes` which rewrites wikilinks and triggers watcher re-ingest — this is expected. No external libraries: pure stdlib (`re`, `collections.Counter`, `pathlib`).

**Centroid cache (`graph/centroids.py`)** — `centroids.json` in the XDG data dir (see "XDG Base Directory paths" under Config), keyed `{rel_folder: {"fp": sha256_16, "vec": [...]}}`. Correctness is fingerprint-based, not event-based: a changed `description_text` changes `fp`, which invalidates the cached vector on the next `folder_centroids()` call. This means the embedding follows `_folder.md` even if edited outside the vault while the watcher is stopped. `folder_centroids(vault)` batches all cache misses into one `embed()` call. All I/O fails soft — corrupt or absent cache triggers recompute, never a raise. Keys are full vault-relative folder paths so subfolders are independent (e.g. `Projects/WeeklyCuisine`, not just `WeeklyCuisine`). `description_text` embeds only `description_terms`, not `distinctive` — `distinctive` is highest-raw-IDF and not MMR-diversified, so it carries noise.

**Strengthened domain signal in placement (`graph/placement.py`, `graph/terms.py` — spec fortalecer-dominios-de-conocimiento-en-colocacion-semantica)** — measured that project notes scored below the placement threshold against their own project folder (weekly-cuisine 0.35–0.64, archiver-rag ~0.31) because a single dense embedding of stem+tags+body let ~500 words of generic technical vocabulary numerically dominate the identity signal. Three complementary fixes, implemented and measured in order (each widened the correct folder's margin against the runner-up, not just its absolute score — the criterion the earlier tags-in-embedding work already established):

1. **Tag symmetry in c-TF-IDF (`graph/terms.py::_terms_by_ctfidf_corpus`)** — tags were already merged into the same term pool as body tokens for large-folder description extraction, but two bugs diluted them: raw unnormalized tags (`"Weekly-Cuisine"` and `"weekly-cuisine"` counted as different terms — unlike small folders' `_terms_by_tags_corpus`, which already normalizes) and a single shared tf denominator that let a handful of tag mentions get outscored by moderately-frequent body vocabulary despite the tag's higher IDF. Fixed: tags normalized the same way as the small-folder path, and scored in their own tf pool (own denominator) before merging with body-token scores — a term scoring in both pools keeps its higher score. Config: `advanced.tag_terms_in_description` (default `true`).

2. **Split identity/content embedding (`graph/placement.py`)** — `note_text()` (single combined string) replaced by `note_identity_text()` (stem + tags only) and `note_content_text()` (body only, frontmatter/`## Related` stripped, ~512-word cap). `suggest_folder()` embeds both in one batched `embed()` call and combines per-folder as `w_identity·cos(identity) + w_content·cos(content)` — the folder side stays a single centroid embedding, unchanged. Falls back to identity-only (effective weight 1.0) when a note has no body, rather than embedding `""`. Config: `advanced.placement_weights.{identity,content}` (default `0.6`/`0.4`). Deliberately does **not** reuse `core/ingest.py::_build_context_prefix` for identity text — that helper includes `folder`/wikilinks, both wrong signals for placement (folder is circular; the wikilink graph was saturated by `auto_link`'s links — measured mean 25.2/note at the time this decision was made, see "Desaturating the wikilink graph" — confirmed not discriminative for domain).

3. **Name-prefix bonus (`graph/placement.py::_folder_prefix`, `_matches_prefix`)** — notes like `weekly-cuisine-phase-3` carry their project literally in the stem. `_folder_prefix(rel_folder)` normalizes a folder's last path segment to kebab-case, handling acronym runs correctly (`ArchiverRAG` → `archiver-rag`, not `archiver-r-a-g` — regex boundary goes before the *last* capital in a capital run). If the note's stem starts with a candidate folder's prefix (whole segment, not free substring), `name_prefix_bonus` (default `0.15`) is added to that folder's score **before** picking the winner — additive, not a short-circuit, so a misleading name can still be overridden by the semantic signal. Eligibility uses `folder_centroids()`'s keys directly (no new `FolderNote` field): type-folders structurally have no `_folder.md` post-recovery (see the folder-collapse incident below), so "currently described" already means "project folder" in this vault — an assumption documented in the code, to revisit if a type-folder is ever described again.

Measured on 4 real notes (2 archiver-rag-internal, 2 weekly-cuisine): margin against the runner-up folder widened for all 4 across the three fixes, and 2 of 4 flipped from "wrong folder wins" to "correct folder wins" (neither crossed the absolute 0.5 threshold, since neither had crossed it before either — the fixes are about ranking correctness, not universally lowering the bar). No false-magnet regressions observed (checked full `scores` dicts, not just the winner, at each step). Vault: *fortalecer-dominios-de-conocimiento-en-colocacion-semantica* (the spec).

**`described_folders()` (`vault/folder_notes.py`) excludes folders with 0 real notes on disk (added after the 2026-08-20 folder-collapse incident).** It's the single production source for `folder_centroids()`'s candidate list, so this filter reaches every placement/clustering candidate from one place. Before this fix, a folder that emptied out (all its notes moved or deleted elsewhere) kept its stale `_folder.md` on disk indefinitely — nothing deletes that sidecar automatically — so it kept competing as a placement candidate forever, which is how the phantom folders left behind by the incident stayed viable magnets even at 0 members. Counts real files via `is_indexable_note`, not the frontmatter `note_count` field — that field goes stale exactly when a folder empties, which is the case this guards against. Consequence to remember: a legitimate project folder that transiently empties out (e.g. mid-reorganization) also stops competing until it has ≥1 note again — seed it with one note manually if you need it to participate in the next placement pass.

**Adaptive α (`graph/terms.py::alpha_for`)** — `α = 1 / (1 + scale·log1p(note_count))`. At 0 notes: 1.0 (full replacement). At 40 notes: ~0.21. `blend_terms(old, new, alpha, max_terms)` applies this on rank scores, not strings — each term contributes `1 − i/len` weighted by `(1−α)` or `α`. `term_dispersion` and `embedding_compactness` are computed but weighted 0.0 per §4.1 staggered rollout; they are stored in `_folder.md` frontmatter for observation. A gravity-well warning fires in `describe` when `note_count` rises but α is already below 0.4 and growth > 15%.

**`_get_cluster_config` returns 4-tuple** — `(auto_cluster, cluster_threshold, placement_similarity_threshold, type_fallback)`. Still reads from the config file directly and returns `(False, 5, 0.55, True)` on any error. Must NOT be refactored through `load_config()` — `load_config()` returns `{}` on error and `config.get("auto_cluster", True)` would be `True`, starting file moves off a corrupt config. The two readers stay separate.

**Orphan pruning (`core/ingest.py`)** — `prune_orphans(vault_path)` compares distinct `source` values in ChromaDB against files on disk; deletes chunks for any missing source. Called at end of `ingest_vault`, `sync_vault`, `move_notes`, and `delete_notes`. Also exposed as `archiver-rag prune` for use after out-of-band renames (e.g. migration scripts run while watcher is stopped).

**Deletion (`vault/notes.py`)** — `delete_notes(notes)` moves each note to `vault/.trash/` (Obsidian's soft-delete convention — recoverable without git), flattening the folder structure with collision-safe `-1`/`-2` suffixes. It never calls `unlink`. `sweep_dead_links(vault, stems)` is the shared core, used by both the CLI and `watcher.py::on_deleted`: it finds inbound linkers via `build_link_map`, then calls `_append_links_section(content, [], valid_stems)` per linker — reusing P1's pruning, with no embeddings or search. **`valid_stems` must be computed after the files have moved**, or the deleted stem still counts as valid and nothing is pruned. The sweep runs once for all deleted stems, not once per note, so a linker pointing at several deleted notes is rewritten a single time. CLI-only by design: agents already delete raw via mcpvault, and this variant stays under the user's hand.

**Hidden-path filtering** — `is_hidden_path(path)` in `utils.py` returns true if any path component starts with `.`. Before P2 only `.obsidian` was excluded, so `.trash` (and `.git`) would have been indexed and `on_moved` would re-ingest a note the moment it was trashed. Now filtered in: both `os.walk` loops in `core/ingest.py` (`not d.startswith(".")`), all four `watcher.py` handlers, and `_update_wikilinks`. `note_stems` and `build_link_map` already filtered correctly.

**Sweep re-entrancy** — `on_deleted`'s sweep rewrites N linker files, firing N `on_modified` events, each running `ingest_file` + `auto_link`. This converges because `_append_links_section` returns the original string object when nothing changes, so `auto_link`'s identity check skips the write and no further event fires. Same fan-out `move_notes` already produces from `on_created`.

**Folder lifecycle (Gate 1) — birth with a description, archival when emptied (`vault/notes.py`, `vault/folder_notes.py`, `watcher.py`)** — the vault spec *folder-lifecycle-splits-from-autonaming-two-gates-and-inbox-clustering-hole* splits the older single-gate lifecycle spec in two, on the argument that the lifecycle half is a *measured robustness fix* (it closes the "phantom folder competes forever" class of bug from the 2026-08-20 collapse) while autonaming is genuinely speculative. Gate 1 is implemented; **Gate 2 (autonaming/`inbox/`) is now also implemented, but ships with `auto_inbox: false` by default** — see "Folder lifecycle (Gate 2)" below and Pending work. Two of Gate 1's four items were already shipped by the incident recovery (`described_folders()` excluding 0-note folders; `_maybe_cluster`'s full-relative-path anti-churn guard) and a third (folders are never auto-renamed) was already true by construction, so only these two were built:

- **Birth with a real description.** A folder created by `log_note()` used to `mkdir` and leave the folder with no `_folder.md` at all, so it sat out of `folder_centroids()` candidacy until `auto_describe` (if even on) eventually noticed. Now, *only when the folder is currently undescribed* (`read_folder_note(...) is None`), `log_note` runs `extract_terms` + `apply_extracted_terms` on the one note that just landed there. At n=1 that takes `extract_terms`' cheap small-folder tag-frequency branch — no embeddings, no cross-folder corpus. The "only when undescribed" condition is load-bearing, not an optimization detail: `log_note` is called synchronously by agents via MCP, and re-describing unconditionally would make every note logged into a large folder (e.g. `decision/`, well past `term_extraction_min_notes`) pay a corpus-wide c-TF-IDF+MMR pass. Ongoing freshness stays `auto_describe`'s job. Wrapped in a bare `except Exception: pass` — a missing description must never fail note creation.
- **Same bug, second site.** `_maybe_cluster` already pre-empted an auto-placement destination folder from being born orphaned, but wrote `FolderNote(description_terms=[], ...)` — and since `description_text()` of an empty list is `""` (falsy), `folder_centroids()` skips it, so the folder was *still* absent from placement until `auto_describe` filled it in. With `auto_cluster` on and `auto_describe` off it would have stayed undescribed permanently. Now it writes real terms, and the write moved to **after** `move_notes()` succeeds — the note has to physically be in the folder before its tags can be counted.
- **Vaciado.** `FolderNote` gained `empty_sweeps: int`, persisted in `_folder.md` frontmatter alongside `note_count` (deliberately on-disk, not in-memory, so the count survives watcher restarts). `_maybe_redescribe`'s empty-folder early return now calls `_maybe_archive_if_empty`: `source: manual` folders are never touched (per spec, a declared description stays a valid magnet even at 0 notes — not even counted); `source: auto` folders increment, and on reaching `folder_vacancy_grace_periods` get their sidecar **moved** to `.archive/{rel_folder}/_folder.md` (never deleted — a folder that comes back into use shouldn't lose what was known about it) followed by `drop_centroid()`. `apply_extracted_terms` constructs a fresh `FolderNote` each run, so a folder regaining a note resets the counter with no explicit reset code.

Two deliberate non-decisions worth knowing. **No periodic mechanism was built**: nothing in the codebase evaluates vault state on a clock (`watch()`'s only loop is a liveness check), and "N consecutive empty *sweeps*" doesn't require one — piggybacking on `_maybe_redescribe`'s existing structural-change triggers and per-folder debounce means a "sweep" is one evaluation per debounce window, which is the same granularity the regenerate path already had. **`.archive/` needed zero exclusion code**: every enumeration site already skips dot-prefixed path components, the same mechanism that hides `.trash/` and `.obsidian/`.

**Folder lifecycle (Gate 2) — inbox routing + embedding-similarity clustering, shipped gated off (`graph/inbox.py`, `watcher.py`).** Resolves the blocking design question left open by Gate 1: *which algorithm clusters notes sitting in `inbox/`?* The 2026-08-20 folder-collapse incident was label propagation over a dense `auto_link` wikilink graph collapsing into giant communities (see "Folder-collapse incident" below) — the vault's own refinement note concluded that pathology reproduces at inbox scale with *any* graph-topology clustering, even post-desaturation, and that inbox clustering must be by **embedding similarity, never wikilink topology**. Decision made here: **greedy cosine-threshold connected components**, not k-means or HDBSCAN — the inbox holds a handful of notes at a time in this vault, so a pre-chosen `k` (k-means) or density assumptions under ~20 points (HDBSCAN) are a worse fit than a threshold check needing zero new dependencies, consistent with this codebase's history of favoring minimal deps and inspectable logic over opaque graph approaches. Known, accepted tradeoff: single-link clustering can chain (A–B similar, B–C similar, A–C not, all three still grouped) — flagged for re-measurement once `auto_inbox` sees live use, same treatment as every other new constant here.

- **Hook point.** `_maybe_cluster` (`watcher.py`) already distinguished every placement outcome via `suggest_folder()`'s `reason` field; `reason == "none"` (no semantic match *and* no `type:` fallback) previously just left the note in place. That branch now checks `auto_inbox` (new gating flag, off by default, same fail-safe-off contract as `auto_cluster`/`auto_describe` via a dedicated `_get_inbox_config()` reader — not folded into `_get_cluster_config`'s tuple, for the same reason `_get_placement_weights_config` is separate: tests monkeypatch it with a fixed-arity lambda) and, if enabled, moves the note into `inbox/` and immediately checks whether inbox now has a cluster ready to spin out.
- **`inbox/` must never compete in placement itself.** `_maybe_redescribe(final_folder)` runs unconditionally after every `_maybe_cluster` call, on whatever folder the note landed in — so if `auto_describe` were also on, it would give `inbox/` a real auto-description the first time a note landed there, making it an ordinary (and backwards) placement candidate. Fixed with the **exact existing mechanism** already used to lock `decision/gotcha/lesson/pattern/reference` out of placement (see "type-folders locked description-less" below): `_ensure_inbox_locked()` writes `inbox/_folder.md` as `source: manual` with empty `description_terms`, once, before the first note lands. `apply_extracted_terms` already refuses to touch `source: manual` folders and `folder_centroids()` already skips folders with empty `description_text()` — zero changes to either function.
- **Shared `weighted_cosine` primitive (`graph/centroids.py`), not a duplicated formula.** `suggest_folder()` already computed `w_identity·cos(identity, centroid) + w_content·cos(content, centroid)` with a "no body → identity-only" fallback. Rather than re-deriving that formula a second time for note-vs-note similarity, it was extracted into `weighted_cosine(channels: list[tuple[weight, vec_a, vec_b]])` — drops any channel missing either vector and renormalizes remaining weights to sum to 1, generalizing the existing fallback to N channels. `suggest_folder` now calls it (passing the folder centroid as both sides of each channel); `graph/inbox.py`'s pairwise note similarity calls it with two notes' vectors instead. One implementation, not a class-based interface — this codebase has no classes/ABCs/Protocols anywhere in `graph/` or `vault/`, and both call sites are static (nothing to dispatch on at runtime).
- **`graph/inbox.py`** — `group_inbox_notes` (union-find over pairs clearing `inbox_similarity_threshold`, default `0.5`, returns every component including singletons), `name_cluster` (in-memory tag-frequency naming via `graph/terms.py::_terms_by_tags_corpus`, reused directly rather than duplicated — c-TF-IDF is deliberately not attempted here since it needs an on-disk folder corpus that doesn't exist yet for an unmaterialized cluster), and `maybe_spin_out_clusters` (orchestrates: list `inbox/`'s direct notes → group → filter by `inbox_min_cluster_size`, default `3` → for each qualifying group, name it, resolve a collision-safe folder slug, `move_notes` the cluster in, then run `extract_terms` + `apply_extracted_terms` on the now-real folder — Gate 1's birth idiom, verbatim, so the new folder needs no special-casing anywhere else: it competes in placement and can empty out via the existing vacancy/archival policy like any other `source: auto` folder). Event-driven, not periodic: called synchronously right after a note lands in `inbox/`, same convention as Gate 1's `empty_sweeps` — no timer/thread exists anywhere in this codebase by deliberate design.
- **Both `inbox_min_cluster_size` (3) and `inbox_similarity_threshold` (0.5) are placeholders, not empirically validated** — same status as every other new constant when it first shipped here (`link_margin`, `folder_vacancy_grace_periods`).
- **Ships gated off** (`auto_inbox: false` by default) — per the vault's own suggested implementation order, live validation of semantic placement and the `decision/` gravity well question (see Pending work) were still open/inconclusive when this landed, so activation is a separate, later decision, not part of this work.

**Streamable HTTP transport (`mcp/http.py`) — stdio stays the default.** `StreamableHTTPSessionManager` accepts the **low-level `Server`** directly, so `mcp/server.py`'s `app`, its seven `Tool` schemas and the whole dispatch chain are reused verbatim — no FastMCP migration, and structurally no second definition of the tools that could drift from the stdio one. `starlette`/`uvicorn` were already present transitively via `mcp` (1.27.2 installed); they are now declared explicitly in `pyproject.toml` alongside an `mcp>=1.9` floor, because relying on an unpinned transitive dep for a required feature is how installs break silently. Four decisions:

- **`Route` with a callable object, not `Mount`, and not a plain function.** `Mount("/mcp")` only matches `/mcp/…`, so a client POSTing the bare `/mcp` it was configured with gets a **307 redirect** instead of a response (observed, then fixed). And Starlette's `Route` treats a *function* endpoint as `func(request) -> response`; only a non-function callable is passed through as a raw ASGI app, which is what `handle_request` needs. Hence the `_Handler` class.
- **Stateless + SSE responses by default (`json_response=False`).** The session is still per-request — no affinity, no reconnect handling — but in-flight status notifications (see "Server→client status notifications" below) require SSE responses: JSON-response mode silently drops them (the SDK's POST loop consumes non-response messages at debug level). SSE — the SDK's original streamable-HTTP behavior — delivers the notifications inside the POST response body, before the final result frame. `json_response=True` remains selectable for clients that want plain JSON. `--stateful` exists for anyone who needs resumability. A fresh `StreamableHTTPSessionManager` is built per `build_app()` call — the SDK documents it as single-use, unable to restart after its `run()` context exits, and `run()` must be entered from the Starlette lifespan.
- **The DNS-rebinding allowlist has a trap at both ends.** Passing `security_settings=None` **disables** protection (SDK backwards compatibility), but constructing `TransportSecuritySettings(enable_dns_rebinding_protection=True)` with an *empty* `allowed_hosts` rejects **every** request. So `_security_settings()` returns `None` unless the operator actually supplied `--allowed-host`. Second half of the trap, found live: naming any `--allowed-host` left the server unreachable at its own `127.0.0.1:8077` (`421 Misdirected Request`), so `serve_http()` always appends the bind host and `host:port` to the allowlist. That costs nothing defensively — DNS rebinding works by pointing a *hostname* at loopback, and such a request carries the attacker's hostname in `Host`, never a literal IP.
- **No TLS, no auth, by design.** Both belong to whatever layer the operator already trusts — a reverse proxy, a VPN, an SSH tunnel — and picking one for them is not this tool's job, so the docs name no product. The SDK's `AuthSettings`/`TokenVerifier` are deliberately unused and the codebase has zero auth anywhere. Default bind is loopback, and a non-loopback `--host` prints a loud warning (not a prompt — it must never block a service start). `_is_loopback()` returns **False** for a hostname it cannot classify without resolving, so unknown means "warn", not "assume safe".

**Tool dispatch offloads to a thread and serializes vault mutation (`mcp/server.py`).** Both fixes exist because HTTP can have several calls in flight; stdio serialized everything at the transport, so neither was reachable before.

- **Thread offload.** The seven handler bodies are synchronous and slow — `search_vault` embeds a query and hits ChromaDB, `vault_status` reads every note. Calling that from the `async def` handler blocks the event loop. `call_tool` is now a one-line `anyio.to_thread.run_sync(...)` around `_dispatch`. Measured live: with a 130 ms `vault_status` in flight, a concurrent `tools/list` returned in **8 ms** instead of queueing behind it. Note this buys *responsiveness*, not throughput — four concurrent warm `search_vault` calls took the same wall time as four sequential ones, because the work is GIL-bound Python. Head-of-line blocking is the thing that was fixed.
- **`_VAULT_WRITE_LOCK` is a correctness guard, not a performance tweak.** `move_notes` rewrites `[[wikilinks]]` across every note in the vault; `log_note` and `cluster_*` read-modify-write files. Two of those interleaving would lose edits. The lock is held only by `_MUTATING_TOOLS`; reads stay parallel. Scope is this process — the watcher is separate, and that cross-process story is unchanged from before HTTP existed.

Also fixed while in the file: `search_vault` read `arguments["min_score"]` but never declared it in its `inputSchema`, so no client could actually set it.

**Server→client status notifications during in-flight tool calls (`mcp/server.py`, `mcp/http.py`).** While a long-running tool call runs, the server pushes human-readable status to the connected client over `notifications/message` (logging — the client's log surface) and `notifications/progress` (request progress — only when the client supplied a `progressToken`). These reach the client's UI/log surface, **never the LLM's context**. Mechanics and traps, in the order they bit:

- **The logging capability is the `@app.set_logging_level()` handler, not a server option.** `get_capabilities()` advertises `LoggingCapability` only when a `SetLevelRequest` handler is registered; there is no `Server(server_options=…)` flag for it (verified in SDK 1.27.2 — a misconception baked into the original spec). The handler stores the client's minimum level in a module global (`_min_log_level`, default `"info"`); `Notifier.log` skips sending when the stored level is above `info`. Best-effort under stateless HTTP (cannot persist across requests) — clients should filter locally anyway.
- **Deadlock trap: fire-and-forget, never `.result()`.** Dispatch runs in a worker thread (`call_tool` → `anyio.to_thread.run_sync`); the session's send methods are async on the event loop. `Notifier._schedule` uses `asyncio.run_coroutine_threadsafe(...)` with a done-callback that reports send failures via `utils.log()` — and never waits. Calling `.result()` from the worker thread would deadlock if the loop were busy with this very tool call. Same fail-soft contract as the runtime heartbeat: a failed or unsent notification never fails the tool call (pinned by a test where a failing send leaves the tool result intact and logs `[mcp] notification failed for <tool>`).
- **Regression trap caught live: `Server.request_context` is a `@property` in SDK 1.27.2.** Calling it as a method raises `TypeError('RequestContext' object is not callable)` *only inside a real request* — direct `call_tool()` tests never see it. The correct access is the module-level `request_ctx.get()` from `mcp.server.lowlevel.server` (raises `LookupError` outside a request → `_NULL_NOTIFIER`, which is how the existing direct-call tests keep working). A full-bridge regression test sets `request_ctx` with a fake session and asserts the emissions land on the loop after the worker thread finished.
- **HTTP: JSON-response mode silently drops notifications — `build_app` default is now `json_response=False` (SSE responses).** Verified in `mcp/server/streamable_http.py::_handle_post_request`: the JSON mode's POST loop consumes every non-response message at debug level and returns only the final result. SSE responses (the SDK's original streamable-HTTP behavior) stream in-flight notifications inside the POST response body, before the final result frame. Still stateless — per-request session, no affinity, no reconnect handling; `json_response=True` remains selectable. The earlier "JSON by default" rationale (entry above) is superseded: in-flight notifications require SSE. The end-to-end test `test_in_flight_notifications_stream_before_the_result` proves a `notifications/message` frame lands before the result frame — its fake `move_notes` sleeps to give the loop a window to flush the fire-and-forget send, the same ordering a real slow tool gives naturally.
- **Emission points live only in `_call`** (core modules stay transport-agnostic), budget ≤ 2 logging messages per tool call phase, progress granular because only opted-in clients receive it: search_vault ("searching: '<q>'" + "N results, re-ranked"); log_note ("created <path>" — the spec's "auto-linking…" point was wrong: auto-linking is the watcher's job, not the dispatcher's); move_notes ("moving N files" + per-move progress + "moved N/N" where N counts successes); cluster_vault apply ("analyzing N notes" + per-move progress); cluster_note ("placing '<note>'" + progress 1/1 + "moved" when it moved); vault_status / get_connections: nothing.
- **`move_notes` / `cluster_vault` split their batches per move only when a notifier is active** (for live progress) — behaviour-preserving: `move_notes` validates and rewrites wikilinks per move anyway, and the only difference is `prune_orphans` running per successful move instead of once at the end (idempotent). With the null notifier the original single batched call / `apply_clusters` path is kept verbatim, so results without a client are byte-identical to before.
- **`cluster_note`'s moved check is a before/after folder compare on disk.** It performs its move internally without reporting it; and its internal `move_notes` call is a no-op failure when the note already sits in the suggested folder, so a naive "is it in the folder now?" check reports phantom moves.

**Watcher heartbeat (`runtime.py`) — the only external evidence the watcher is *working*, not just loaded.** `watch()`'s loop is a bare `observer.is_alive()` poll at 1 Hz: before this there was no PID file, no last-event timestamp, no counters, so the only signal outside the process was launchctl state plus the mtime of `/tmp/archiver-rag.log`. `runtime.py` writes `{pid, started_at, vault_path, last_event, last_event_kind, last_event_path, counts}` to `paths.cache_dir()/runtime.json` after every event a handler actually acts on; `archiver-rag status` reads it. Four things to preserve:

- **Cache, not Data.** `record_start` regenerates the file wholesale on each watcher boot, so losing it costs only the counters since the last restart. Contrast `centroids.json`, which went to Data as install-derived index state.
- **Every write fails soft** (`try/except Exception: pass`), matching `graph/centroids.py`'s I/O contract. Observability must never be able to break ingestion — a `status` that says "no heartbeat" beats a watcher that dies because `~/.cache` is read-only. Pinned by a test that makes the cache dir unwritable and drives a real `on_created` through it.
- **The `record_event` calls sit *inside* the handlers' existing guards** — after `is_indexable_note`, after `_is_spurious_delete`, after the `_folder.md` branch. A spurious delete from an atomic save must not register as activity, or `status` would report a busy watcher on a vault where nothing happened. Do not hoist these to the top of a handler "to catch everything".
- **Counters are per-process by design.** `status` reports them as "since start", and a `KeepAlive` restart after a crash genuinely starts a new count. The stored `pid` is what lets `status` notice launchd restarted the watcher behind its back (`⚠️ watcher restarted (PID a → b)`). Paths are stored vault-relative — relativized inside `record_event` using the `vault_path` already in the state it just read, so the watcher's hot path pays nothing.

**`launchctl list` exits 0 for a crash-looping job (`service.py::service_state`).** A loaded-but-dead launchd job reports `LastExitStatus` and simply omits `PID`, while still exiting 0 — so the old one-line `status()`, which looked only at the return code, rendered a restart loop as `✅ Running`. `service_state()` requires a PID before reporting running, and otherwise prints `⚠️ Loaded but not running (last exit N)`. Linux parses `systemctl --user show` rather than `systemctl status` — machine-readable, and it keeps raw systemd prose out of our own report. `STDOUT_LOG`/`STDERR_LOG` are module constants interpolated into the plist template *and* reported by `service_state()`, so the paths `status` prints cannot drift from the ones launchd writes to.

**`status` must never embed and must never abort (`report.py`).** The Placement section reports how many folders can actually win a placement, and reads `_folder.md` frontmatter plus `centroids.cached_centroids()` — **not** `folder_centroids()`, which re-embeds every stale folder as a side effect of being called. A status readout must be cheap and must not mutate the cache just by being run. Config comes from `utils.load_config()` (returns `{}`), never `init_cmd.load_config()` (raises `typer.Exit(1)`): an unconfigured install should render as a diagnosis pointing at `archiver-rag init`. Note the live vault reports `4 of 8 folders competing (4 manual-locked)` — a folder that is *described* but has empty `description_terms` is declared-but-not-competing, since `description_text()` is falsy and `folder_centroids()` skips it, which is exactly how the type-folders are locked out.

**`index_stats()` (`core/index_stats.py`) — staleness compares `int(disk_mtime)`.** `ingest_file` stores `int(os.path.getmtime(...))`, so comparing against the raw float would flag every note with a sub-second remainder as stale forever. Drift categories map onto existing repair commands: `missing_from_index` → `archiver-rag sync`, `orphaned_in_index` → `archiver-rag prune`, and every problem line in `health` names its fix. `collection` is a lazy proxy whose *first attribute access* opens the database, so an unconfigured install raises inside this function and nowhere earlier — the error is returned in an `error` key so `health` can still report the vault side. `vault_status()` gained an additive `health.counts` (its four lists are truncated at 20, so true totals were unrecoverable) and its two full-vault read passes were merged into one.

**Both commands share one composed dict (`report.py`).** `compose_status()`/`compose_health()` build a plain dict; `render_status()`/`render_health()` take that dict as their only argument; `--json` prints the same dict. Human and machine output therefore cannot disagree about what "in sync" means, and rendering is testable without a Typer runner. `--json` prints through `builtins.print`, **not** `rich.print` — rich interprets `[...]` in paths as markup and hard-wraps long lines, neither of which survives a pipe into `jq`.

**XDG Base Directory migration (`paths.py` — spec spec-xdg-base-directories-config-data-cache-paths)** — `~/.archiver-rag/` was independently hardcoded 9 times across 6 files before this, each with different error-handling on a missing/corrupt config (raise vs. `{}` vs. `typer.Exit(1)` vs. safe-off tuple) — there was no single point to redirect, despite that being the implicit assumption in the original spec. `paths.py` centralizes path *resolution* (config/data/cache dirs, forced onto the Unix backend even on macOS, migration of a legacy install) without touching any callsite's own error-handling contract — see "XDG Base Directory paths" under Config for the full picture, including why `centroids.json` went to Data rather than Cache and why the HuggingFace model cache was deliberately left alone.

---

## Pending work — known gaps

Last updated 2026-08-31. **Two items open.** 436 tests green. Vault note count has drifted upward across this work (73 → 79 as of 2026-08-24 → 83 as of 2026-08-25) from ordinary vault usage between sessions plus one `log_note` call recording the desaturation work itself — not a sign of data loss, `place`/`move_notes` never reduce the total. **Watcher is running and `auto_cluster`/`auto_describe` are ON as of 2026-08-22** (re-enabled on explicit user request after the post-incident fixes below had been sitting untested under live traffic) — **do not stop the service or flip either flag off without asking**, mirroring the previous standing instruction in the opposite direction. `archiver-rag relink --apply` is a documented exception, not a bypass: it stops the watcher itself for the duration of the rewrite and restarts it afterward, but only after its own confirmation prompt — the prompt *is* the asking, same as any other `--apply` confirmation in this codebase (`place --all --apply`, `cluster --apply`). Note that the watcher process must be restarted to pick up code changes (Python does not hot-reload); CLI commands get new code for free since each invocation is a fresh process.

The one deliberate non-change: the vault has no git remote. That is intentional, not an oversight — do not suggest adding one.

### Open

- ⚠️ **`decision/` gravity well — flat across the first days of live traffic, but not yet enough data to call it resolved.** The pre-incident measurement (`decision/` at 38/71, 53%) is now moot: `decision/`'s own `_folder.md` was deleted during recovery and it's now pure `type:` fallback with no semantic description at all, alongside `gotcha`/`lesson`/`pattern`/`reference` — as of 2026-08-22 this is a **permanent, explicit policy**, not just an absent file, see the "type-folders locked description-less" entry below. Sat at 32/73 (44%) right after recovery; 29 as of 2026-08-21. **Re-checked 2026-08-24 — still exactly 29, now out of 78 notes (37%), while every project folder grew around it** (`Projects/ArchiverRAG` 5→8, `bakery-api-overview` at 15, `Projects/WeeklyCuisine` at 12). That is the first real evidence in favour of "legitimate resting state": across ~2 days of live `auto_cluster` traffic and 5 new notes, `decision/` absorbed none of them — a gravity well would have kept pulling. Not conclusive (5 notes is a small sample, and no note has *left* `decision/` either), but the trend to watch is now "does it stay flat", not "is it still growing". Whether the remaining 29 are a legitimate resting state (archiver-rag-internal notes with no dedicated project folder, landing there by `type:`) or still a gravity well is an open question. Vault: *decision-folder-is-a-gravity-well---auto-cluster-is-collapsing-the-taxonomy*, *folder-collapse-incident-cluster-vault-fallback-and-recovery*.

- ⚠️ **`auto_cluster`/`auto_describe` re-enabled 2026-08-22 — first live run since the incident, not yet observed over time.** The three post-incident fixes (`cluster_vault` no longer an automatic path, `described_folders()` excluding 0-note folders, per-folder debounce on `_maybe_redescribe`) were verified by unit tests and by the recovery's one-time `place --all --apply`, but this is their first exposure to organic, continuous watcher-driven `auto_cluster`/`auto_describe` traffic rather than a single batch operation. Watch for: any folder rapidly accumulating notes across multiple auto-placements in a short window (the original collapse's signature), `_folder.md` churn beyond the debounce window, and whether the `decision/` gravity well question above starts resolving now that live placement is running again instead of sitting frozen. No action needed unless one of those patterns shows up — this is a monitoring item, not a known problem.

### Closed (2026-08-31)

- ✅ **Server→client status notifications for in-flight MCP tool calls.** While a long-running tool call runs, the server pushes human-readable status to the connected client: `notifications/message` (the client's log surface) plus `notifications/progress` (only when the client supplied a `progressToken`). Full mechanics and traps in the "Server→client status notifications" entry under Key technical decisions. Two spec corrections discovered during verification: the logging capability is advertised via the `@app.set_logging_level()` handler (no `server_options` flag exists), and HTTP JSON-response mode silently drops in-flight notifications — so `build_app`'s default is now `json_response=False` (SSE responses, still stateless; `json_response=True` remains selectable). Regression trap caught live: `Server.request_context` is a `@property` in SDK 1.27.2, and calling it as a method fails only inside a real request — correct access is the module-level `request_ctx.get()`. 22 new tests (19 in `test_mcp_dispatch.py` — per-tool emission sequence, null-notifier byte-identical results, fail-soft send, level filtering, capabilities, full bridge through `call_tool`; 3 SSE-specific in `test_mcp_http.py`, including the end-to-end proof that a `notifications/message` frame lands in the POST body before the result frame). 436 tests green (+22 from the 414 at HEAD).

### Closed (2026-08-29)

- ✅ **Gate 2 — inbox routing + embedding-similarity clustering, implemented and shipped gated off.** Resolves the blocking design question left open since Gate 1 (see the removed "Gate 2" entry that used to sit in Open, above): which algorithm clusters notes sitting in `inbox/`. Decision: **greedy cosine-threshold connected components over note embeddings**, never wikilink-graph topology (that pathology is exactly what caused the 2026-08-20 collapse and would reproduce at inbox scale) and not k-means/HDBSCAN either (unneeded complexity and a new heavy dependency at this vault's scale — a handful of inbox notes at a time). Full mechanics in the "Folder lifecycle (Gate 2)" entry under Key technical decisions. New `graph/inbox.py` (`group_inbox_notes`, `name_cluster`, `maybe_spin_out_clusters`) reuses Gate 1's birth idiom (`extract_terms` + `apply_extracted_terms`) verbatim once a cluster is moved into a real folder, so newly-spun-out folders need no special-casing anywhere else in the lifecycle machinery. `inbox/` itself is locked out of placement candidacy using the exact same `source: manual` + empty-terms mechanism already used for the type-folders, not new code. A duplicated weighted-cosine formula was caught before it shipped twice: `suggest_folder`'s existing `w_identity·cos + w_content·cos` combination was extracted into a shared `graph/centroids.py::weighted_cosine()`, used by both `suggest_folder` (note vs. folder centroid) and the new inbox pairwise similarity (note vs. note) — one function, not a class-based interface (this codebase has none). New config: `auto_inbox` (default `false`), `advanced.inbox_min_cluster_size` (default `3`, placeholder), `advanced.inbox_similarity_threshold` (default `0.5`, placeholder) — both flagged not-empirically-validated, same status every other new constant here ships with. **Ships gated off**: per the vault's own suggested implementation order, live validation of semantic placement and the `decision/` gravity well question (previously the other two open items in this section) were still open/inconclusive when this landed — turning `auto_inbox` on is a separate, later decision, not part of this work, same as `auto_cluster`/`auto_describe` were introduced off and enabled later on explicit go-ahead. 26 new tests across 3 files (`test_centroids.py` +4 for `weighted_cosine`, `test_inbox_clustering.py` new ×15, `test_watcher_cluster.py` +10 for the routing hook, including an end-to-end test through the real `graph/inbox.py` — not mocked — that caught a pre-move-vs-post-move path mismatch in the "was my note promoted" check before it shipped). 410 tests green (+26 from 384). `numpy` also declared explicitly in `pyproject.toml` — was already transitively available but imported directly in three modules now, same reasoning `starlette`/`uvicorn` were pinned for.

### Closed (2026-08-25)

- ✅ **Detached MCP HTTP server — `archiver-rag start|stop|restart [watcher|http]`.** The HTTP transport can now run as its own supervised daemon instead of a terminal-holding foreground `serve`; stdio remains the default MCP registration and the watcher is untouched. One abstraction, two definitions: `service.py::ServiceDef` parameterizes label/plist/unit/log paths over `WATCHER` and `HTTP`, so start/stop/liveness cannot mix one daemon's files with the other's — full mechanics in the Service section ("Detached MCP HTTP server"). Kill = `stop http` (launchctl unload / systemctl stop; uvicorn exits cleanly on SIGTERM); no PID files, the supervisor is the source of truth, same as the watcher. **Port pre-flight** (`service.port_in_use`) aborts `start http` *before* writing/loading when anything already owns host:port — loading into a busy port would KeepAlive crash-loop, and this exact case was caught live during development (a foreground serve held 8077). Boot persistence is asked at runtime (`--login/--no-login` to script it); agent registration stays manual by decision (`~/.claude.json` never written by start). Endpoint resolution consolidated in `mcp/http.py::configured_endpoint()` (config → loopback defaults) plus `service.py::daemon_endpoint()`, which layers any flags baked into the installed service file on top — `serve` uses the former, while `start http` and `status` use the latter, so they name what the daemon *actually* listens on even after an overridden `start http --port N` (drift caught live: status reported 8077 for a daemon running 8099 before daemon_endpoint existed). `status` gained an additive `"http"` section (state + URL), rendered as its own block; `--json` shape extended, not changed. `uninstall` removes both services. Foreground `serve --transport http` unchanged. 30 new tests (`test_http_service.py` ×23 new file, `test_service_state.py` rewritten def-based +2, `test_report_render.py` +5). 384 tests green (+30 from 354).

- ✅ **Wikilink graph desaturated — margin-based `auto_link` selection, live-vault repair, `hub_boost`/`graph_boost` recalibrated, `relink --apply` automates the watcher stop/restart.** Full mechanics under "Desaturating the wikilink graph" in Key technical decisions and the vault note *wikilink-graph-desaturated-margin-based-linking-replaces-per-run-cap*. Measured before: density 0.66, mean 25.2 `## Related` links/note (max 42), 90% of notes at `hub_boost`'s old saturation point (incoming≥5) making it a near-constant. Fix: `strip_related_section` promoted to `utils.py` and applied before every embed call (broke a feedback loop where the growing link list was itself indexed); `auto_link`'s fixed per-run cap replaced by `graph/linker.py::select_related_candidates` (keep candidates within `link_margin`, default 0.05, of the top score — measured adaptive: kept counts 3–12 across 10 real notes, not a disguised fixed cap); `## Related` changed from append-only to rebuilt via `_append_links_section`'s new `keep_targets` param, with `None` preserving old behaviour exactly so every pre-existing test passed unchanged. New `archiver-rag relink [--apply]` CLI command for the one-time backlog repair. **Run live 2026-08-25** against the real vault (83 notes): dry-run confirmed 2071→513 total links, density 0.304→0.075; `--apply` rewrote all 83; `archiver-rag index` re-embedded; watcher restarted; index-matches-disk and 83/83 notes intact confirmed via `status`. Post-repair measurement (incoming-link mean 6.9, p75=9, p90=12, max=25) showed `hub_boost`'s old cap (saturating at incoming=5, `graph/rerank.py`) still maxed out 68.7% of notes — the constant, not just its input, was stale. Recalibrated to saturate at 12 (~p90) instead, as named constants (`HUB_BOOST_SATURATION_INCOMING`, `HUB_BOOST_MAX`) with the calibration history in a comment; verified live via direct Python invocation (bypassing the long-running `archiver-rag serve` MCP process, which — same no-hot-reload caveat as the watcher — kept running pre-fix code until its next restart). Finally, `relink --apply` was changed to stop the watcher itself for the duration of the rewrite and restart it afterward (`--no-restart-watcher` to opt out), removing the manual `archiver-rag stop`/`start` bracketing step — gated behind the same `--yes`/confirm prompt every other `--apply` command in this codebase uses, and wrapped in `try/finally` so a mid-run exception still restarts the watcher rather than leaving it down. It also re-embeds afterward by default now (`--no-sync` to opt out), inside that same stopped-watcher window — via `sync_vault` rather than `archiver-rag index`, since `sync_vault` is mtime-based and re-embeds only the notes the rewrite actually touched, where `index` would unconditionally re-embed the whole vault. The manual `archiver-rag index` step used during the live run below predates this automation; going forward `relink --apply` alone is the full procedure. 21 new tests total (`test_linker_margin.py` ×15, `test_rerank.py` ×6). 354 tests green (+21 from 333).

- ✅ **Streamable HTTP transport for the MCP server.** `archiver-rag serve --transport http` serves the same seven tools over HTTP; **stdio remains the default**, so every existing install and the `~/.claude.json` entry written by `init` are untouched. The SDK side was nearly free — `StreamableHTTPSessionManager` takes the existing low-level `Server` — so the real work was an ASGI entrypoint plus two correctness issues stdio had been hiding (event-loop blocking, unserialized vault mutation). Full mechanics in the two new entries under Key technical decisions. Scope decision: **archiver-rag terminates no TLS and performs no authentication**, by design — both belong to whatever layer the operator already trusts, and prescribing a specific product is out of scope for the project's docs. The deliverable is a server safe to put *behind* such a layer, and a README that says so without naming one. Verified live against a real `mcp` client (not just curl) on both transports: HTTP gave `initialize` → 7 tools → real `search_vault`/`get_connections` results, and a stdio subprocess with the new code did the same. The `Host` allowlist was measured through all three cases (bind address 200, allowed host 200, `evil.example.com` 421). 34 new tests across 3 files. 333 tests green (+34 from 299). **Two findings worth remembering, both caught only by running it:** `Mount("/mcp")` 307-redirects the bare `/mcp` a client is configured with, and `--allowed-host` alone made the server unreachable at its own loopback address — see the Key technical decisions entry for both fixes.

### Closed (2026-08-24)

- ✅ **`status` and `health` made explicative.** `status` was one line off `launchctl`'s exit code; `health` was a chunk count plus a 3-chunk `peek()`. Neither could answer the question you run them to answer. The blocking gap was structural — the watcher recorded no runtime state at all — so the work began with a heartbeat (`runtime.py`, `runtime.json` in the XDG cache dir), then `service_state()`, `index_stats()`, and a shared compose/render seam in `report.py` with `--json` on both commands. Full mechanics in the four new entries under Key technical decisions ("Watcher heartbeat", "`launchctl list` exits 0 for a crash-looping job", "`status` must never embed and must never abort", "`index_stats()`"). Verified live end to end: `status` run *before* restarting the watcher rendered the `no heartbeat` path correctly (that process was running pre-change code), and after `archiver-rag restart` a real `log_note` produced `6 ingested` — matching the watcher log's 1 `New file detected` + 5 `File modified` exactly. `--json` on both parses through `python -m json.tool`; `chunks: 109` cross-checks against a raw `collection.count()`, `notes_on_disk: 79` against `vault_status()`, and the launchctl PID against the heartbeat's. 49 new tests across 5 new files. 299 tests green (+49 from 250). **One behaviour worth knowing:** the heartbeat's counters reset on every watcher restart by design, so "since start" is not a lifetime total.

- ✅ **Gate 1 of the folder-lifecycle spec — implemented.** The 2026-08-23 vault note *folder-lifecycle-splits-from-autonaming-two-gates-and-inbox-clustering-hole* split the original single-gate lifecycle spec, arguing the lifecycle half is a measured robustness fix (not speculative like autonaming) because it closes the "phantom folder competes as a placement magnet forever" bug class from the 2026-08-20 collapse. Source inspection before implementing found Gate 1 was **already half-done by the incident recovery**: `described_folders()` already excluded 0-note folders, `_maybe_cluster` already compared full vault-relative paths (Gate 1.5), and "folders are never auto-renamed" was already true by construction — so only two items were actually built: birth-time descriptions (in `log_note` *and* a second, previously unnoticed instance of the same bug in `_maybe_cluster`, which wrote an empty-terms placeholder that `folder_centroids()` silently skips) and the `empty_sweeps`/`.archive/` vaciado policy. Full mechanics in the "Folder lifecycle (Gate 1)" entry under Key technical decisions. Verified live: `log_note` into a fresh `gate1smoketest/` produced `description_terms: [watcher, clustering]` — real tag-derived terms, not an empty placeholder — and the watcher log confirmed `Auto-described gate1smoketest/ → created` with no re-entrancy; test folder removed afterwards and `archiver-rag prune` reported no orphans. 19 new tests across 4 files (`test_log_note.py` new; `test_folder_notes.py`, `test_watcher_describe.py`, `test_watcher_cluster.py` extended). 250 tests green (+19 from 231). **Not yet exercised in production: the archival path** — no folder has emptied out since, so `.archive/` does not exist in the real vault and that branch is covered by unit tests only.

### Closed (2026-08-22)

- ✅ **XDG Base Directory migration — spec spec-xdg-base-directories-config-data-cache-paths, implemented.** `archiver_rag/paths.py` replaces 9 independent hardcodes of `~/.archiver-rag/` across 6 files with one function-based module (`platformdirs`, forced onto the Unix backend on macOS to match this machine's `~/.config` convention — see "XDG Base Directory paths" under Config for the full rationale, including why `centroids.json` went to Data and why the HF model cache was left alone). `paths.ensure_migrated()` runs once per process from a new `cli.py` `@app.callback()`, migrating an existing `~/.archiver-rag/` install's `config.json`/`chroma_db/`/`centroids.json` on first run and rewriting `chroma_path`/`install_path` inside the migrated config — never deletes the legacy dir. Verified live on this install: `archiver-rag status` triggered the migration, printed the move summary, left `~/.archiver-rag/backup-folder-collapse-2026-08-20/` untouched, and a follow-up `archiver-rag search` returned real results against the migrated `chroma_db` — proving the rewritten `chroma_path` actually resolves, not just that files moved. 6 new tests (`test_paths.py`); `conftest.py` gained `_no_real_home_paths` (autouse) and `tmp_install` (opt-in) fixtures mirroring the existing vault-safety pattern. 231 tests green (+6 from 225).

- ✅ **Type-folders locked description-less — permanent, not incidental.** `decision/gotcha/lesson/pattern/reference` losing their `_folder.md` during the recovery was fragile: `_maybe_redescribe()` doesn't check whether a folder already has a description before writing one, so reactivating `auto_describe` — or even just running `archiver-rag describe --all` by hand, which is what accidentally re-populated all five earlier in this session — would silently recreate them and put them back in semantic competition. Fixed by giving all five `source: manual` with `description_terms: []` (`archiver-rag describe --folder <X> --set ""`), reusing existing, already-tested mechanisms rather than adding new ones: `apply_extracted_terms()` already refuses to touch any folder with `source: manual` (both `_maybe_redescribe` and `describe_cmd` call through it), and `folder_centroids()` already skips any folder whose `description_text()` is empty (`", ".join([])` = `""`, falsy) before ever computing a centroid — so these folders are simultaneously immune to regeneration *and* absent from placement candidacy, with zero new code. Verified live: `describe --all` now prints `skipped (manual)` for all five, and `place` no longer lists `decision` in a note's `scores`. Intent: these folders should keep shrinking over time as `auto_cluster`/`suggest_folder` (once re-enabled) pulls their notes into real project folders, landing only stragglers via `type:` fallback — not stabilize as a competing semantic destination again.

### Closed (2026-08-21)

- ✅ **`Projects/ArchiverRAG` populated.** Was at 0 notes right after the recovery's `place --all --apply` (its seeded note didn't clear threshold, fell to `reference/` by type). Manually moved back, and now sits at 5 notes — some via direct `move_notes` calls, some via subsequent `place --apply` activity outside this session. No longer a distinct open item.

- ✅ **Domain-signal strengthening in placement — spec fortalecer-dominios-de-conocimiento-en-colocacion-semantica.** Three fixes (tag symmetry in `graph/terms.py`, split identity/content embedding + weighting in `graph/placement.py`, name-prefix bonus) — full detail in the "Strengthened domain signal in placement" entry under Key technical decisions. Measured on 4 real notes across all three fixes: margin against the runner-up folder widened for all 4, and 2 flipped from wrong-folder-wins to correct-folder-wins. No false-magnet regressions observed. 225 tests green (+12 from the incident recovery's 203 — new coverage for `_terms_by_ctfidf_corpus` tag pools, `note_identity_text`/`note_content_text`, `_folder_prefix`/`_matches_prefix`, and the weighting/bonus math in `suggest_folder`).

### Closed (2026-08-20)

- ✅ **Folder-collapse incident — `cluster_vault` removed as an automatic watcher path.** `graph/clustering.py::cluster_vault()` (pre-existing label-propagation fallback, not new this session) collapsed 61/73 notes into two note-stem-named folders across several automatic passes triggered by `_maybe_cluster`'s `cluster_threshold` counter, on this vault's dense auto-linked wikilink graph — see the new "`cluster_vault` is never called automatically" entry in the Service section above for the full mechanism, and vault note *folder-collapse-incident-cluster-vault-fallback-and-recovery* for the complete diagnosis-and-recovery writeup. Three fixes, 203 tests green: (1) `cluster_vault`/`apply_clusters` no longer imported by `watcher.py` — manual-only via `archiver-rag cluster`. (2) `described_folders()` excludes folders with 0 real notes on disk, closing the "empty phantom folder stays a candidate forever" hole. (3) `_maybe_redescribe` debounced per folder (`_REDESCRIBE_DEBOUNCE_SECONDS = 5.0`) — the original "no debounce needed" assumption from the `auto_describe` work below was wrong in practice. Recovery: `decision/gotcha/lesson/pattern/reference` lost their `_folder.md` (backed up to `~/.archiver-rag/backup-folder-collapse-2026-08-20/`, now pure `type:` fallback); 73/73 notes confirmed intact throughout; `place --all --apply` redistributed 62 notes. `type_fallback` flipped `true` (was `false`) — required for the now-description-less type-folders to catch anything at all.

### Closed (2026-08-19)

- ✅ **Tags feed the placement embedding.** `graph/placement.py::note_text()` discarded frontmatter entirely, so a note's `tags:` — its cleanest, undiluted project-identity signal — never reached the embedding used by `suggest_folder()`. Generic technical body content pulled project notes toward generic technical folders instead of their own project folder. Fix: `note_text()` now splices `_note_tags(fm)` (reused from `graph/terms.py`, previously terms-only) into the embedded text — additive, not replacing stem or body. Measured against 3 real WeeklyCuisine notes stuck in `decision/`: margin vs. runner-up folder (`bakery-api-overview`) roughly doubled in all three (0.095→0.188, 0.137→0.189, 0.038→0.077) — confirmed discriminative (target folder's score rose, runner-up's stayed flat), not a threshold-shift artifact. Does not by itself resolve the gravity well — see Open above. Vault: *note-tags-should-feed-the-placement-embedding-measured-signal-dilution* (the spec), *note-tags-in-placement-embedding-implemented-margin-confirmed-widened* (the measurement). 3 new tests. **Superseded 2026-08-21:** `note_text()` no longer exists — split into `note_identity_text()`/`note_content_text()`, see the "Strengthened domain signal in placement" entry above.

- ✅ **Watcher auto-describe (`auto_describe` config flag, off by default).** `describe` had always been manual-only, so a folder's `_folder.md` (`description_terms`, `note_count`) could go silently stale as notes accumulated or left — confirmed live on `Projects/WeeklyCuisine/_folder.md`, which still read `note_count: 5` with 6 notes actually present. New `watcher.py::_maybe_redescribe()` hook, gated by `_get_describe_config()` (same error-safe-off contract as `_get_cluster_config`). Fires only on structural changes (note created/deleted/moved in or out of a folder) — never on `on_modified`, since that fires per keystroke-batch save with no debouncing and this does real work (corpus rebuild + MMR). `_maybe_cluster` now returns the folder it actually moved a note into, so `on_created`/`on_moved` redescribe the note's *final* folder rather than its original location. The decide/blend/write policy (skip `source: manual`, blend via `alpha_for`/`blend_terms`, gravity-well pathology check, write) was extracted out of `describe_cmd`'s loop into `vault/folder_notes.py::apply_extracted_terms()`, shared by both the CLI and the watcher — the CLI still uses `extract_terms_all()` for the amortized bulk case, the watcher uses the (previously dead-code-in-production) single-folder `extract_terms()`. Verified live against the real vault: regenerating `Projects/WeeklyCuisine/_folder.md` corrected `note_count` 5→6 and surfaced the gravity-well pathology warning on real data (α=0.36, count 5→6). 24 new tests.

### Closed (2026-08-10)

- ✅ **Stage B — semantic folder placement.** `graph/centroids.py`: fingerprint-keyed centroid cache (`~/.archiver-rag/centroids.json`) — embedding follows `_folder.md` without depending on watcher events, because a changed description changes the fingerprint and triggers a re-embed on next placement call. `graph/placement.py`: `suggest_folder()` — embed note, cosine vs all described folders, best above threshold (0.55 default) wins; falls back to frontmatter `type:`; returns `scores` dict for debugging. `watcher.py`: all four event handlers detect `_folder.md` changes independently (`on_moved` handles atomic saves); refresh centroid on write, drop on delete, spurious-delete guard respected; `ingest_file` / `auto_link` explicitly not called. `_maybe_cluster` uses `suggest_folder` instead of `cluster_note`; anti-churn guard widened to full vault-relative parent path; new-folder case writes `_folder.md` sidecar so it is not born orphaned; log line includes reason and similarity. `cluster_note` now returns both signals: semantic primary (`suggested_folder`, `similarity`, `reason`, `scores`) and neighbour vote as `neighbor_vote` (informational); `apply=True` acts on semantic result. `graph/terms.py`: `alpha_for()` (logarithmic decay), `blend_terms()` (rank-weighted blend), `term_dispersion()`, `embedding_compactness()` added; last two computed at weight 0.0 per §4.1 staggered rollout. `describe` blends via α when regenerating. `FolderNote` gains `term_dispersion` and `embedding_compactness` fields. `place --all` / `--all --apply` for §9.7 measurement. Config: `placement_similarity_threshold`, `type_fallback`. 172 tests green (+43).

### Closed (2026-08-09)

- ✅ **Stage A — folder description infrastructure.** `_folder.md` sidecar per folder, excluded from ingest/auto_link/wikilink graph at all 11 enumeration sites via `is_indexable_note()`. Term extraction: tags (small folders) or c-TF-IDF + MMR (large), with `## Related` stripped before counting, Unicode-normalized tokenizer, and stopwords covering both languages plus project-ubiquitous terms. `archiver-rag describe` / `--all` / `--folder` / `--set` command. `extract_frontmatter` and `load_config` promoted to `utils.py`. 129 tests green (+35). Cold-start: `archiver-rag describe` seeds all 8 describable folders; `decision/_folder.md` terms reflect actual folder content (not slug lists). Watcher confirmed to not index `_folder.md` after service restart.

### Closed (2026-08-08)

- ✅ **Buffered logs** — `utils.log()` prints with `flush=True`; `watcher.py` and `core/ingest.py` both route through it. Verified live: `File modified` and `Indexed N chunks` now appear the moment they happen, where previously the tail stayed empty.
- ✅ **Clustering now fires for tool-written notes** — the `auto_cluster` hook moved out of `on_created` into a shared `_maybe_cluster`, called from `on_moved` too but gated on `is_new_note`. Verified live: a new note is auto-placed exactly once; editing it afterwards indexes without relocating.
- ✅ **Auto-place churn** — one new note logged `Auto-placed … → decision/` five times because `cluster_note` kept suggesting the folder it already sat in. Guarded, and `Auto-placed` is now logged only on a real move. Live count went 5 → 1.
- ✅ **`.trash/` no longer created by a failed delete** — `mkdir` moved past validation, so a call that deletes nothing leaves no stray directory.
- ✅ **Watcher dropped every atomic save** — `on_moved` bailed on `not src.endswith(".md")`, so the `tmp.NNN → note.md` rename that *is* how editors save was thrown away and the note never got indexed. Combined with the spurious `deleted` that follows, notes edited through Claude Code were being evicted and never re-added. Both fixed and verified live: a scratch note went 0 chunks → indexed with current content. 17 new watcher tests.
- ✅ **P2 verified end-to-end** — `archiver-rag delete decision/spy-scratch.md` moved the note to `.trash/`, pruned `[[spy-scratch]]` from its linker, left the index at 63/63 parity with no ghosts, and kept `.trash` out of the index across a full `archiver-rag index`. Watcher idle at 0% CPU afterwards — no re-ingest loop. `.trash/` added to the vault's `.gitignore`.
- ✅ **P2** — `archiver-rag delete <note>... [--yes]`. Moves notes to `vault/.trash/` (Obsidian convention, recoverable). `sweep_dead_links(vault, stems)` runs once after all moves and prunes inbound dead links from `## Related`. `watcher.py::on_deleted` also calls `sweep_dead_links` for Finder/external deletes. Hidden-dir filtering fixed vault-wide (was `.obsidian` only; now `not d.startswith(".")`). `is_hidden_path()` added to `utils.py`. 14 new tests; 66 total, all green.
- ✅ **P1** — `_append_links_section` now prunes dead targets from `## Related` when `valid_stems` is provided. `auto_link` passes it unconditionally. 13 characterization tests still pass unchanged; 13 new pruning tests added.
- ✅ **P3** — `on_moved` now calls `_update_wikilinks` when the stem changes. Added missing `event.is_directory` guard.
- ✅ **Broken link** — triple-dash `[[wikilink-extractor-uses-offset-masking---not-text-mutation]]` pruned from `## Related` by first `auto_link` run. `vault_status → broken_links: []`.
- ✅ **Cleanup** — deleted `const.py`, `search_index.py`, `check_health.py`; removed dead module-level `shutdown()` from `watcher.py`.
- ✅ **`note_stems(vault)`** — extracted to `utils.py`, replaces the duplicated dotfile-filter pattern in `health.py` and `clustering.py`.
- ✅ **Vault hygiene** — obsolete gotcha `wikilink-resolver-does-not-match-undated-stems-to-dated-filenames` marked superseded; triple-dash entry removed from the `related:` YAML in `append-links-section-had-4-data-loss-bugs-rewritten-with-slicing`.
- ✅ **Orphan — not a bug.** `decision/weekly-cuisine-help-overlay.md` is a normal note with 16 outgoing links that nobody linked back. Nothing to fix.
- ✅ **`_slugify` does not regenerate the triple-dash bug.** `[^\w\s-]` strips em-dashes, so `masking — not` yields `masking-not`. A literal `" - "` in a title still yields `---` (because `[\s_]+` does not collapse adjacent hyphens), which is where `spec---type-and-tags...` came from. The broken link came from writing the same concept with an em-dash once and a hyphen another time — not a live bug.

---

## Config

All runtime config lives at the XDG config path, resolved by `paths.py` (see "XDG Base Directory paths" below) — `~/.config/archiver-rag/config.json` on this machine:

```json
{
  "vault_path": "/Users/fernanrod/Programacion/Proyectos/obsidian-vault",
  "install_path": "/Users/fernanrod/.local/share/archiver-rag",
  "chroma_path": "/Users/fernanrod/.local/share/archiver-rag/chroma_db",
  "auto_cluster": true,
  "cluster_threshold": 5,
  "placement_similarity_threshold": 0.55,
  "type_fallback": true,
  "auto_describe": true,
  "auto_inbox": false,
  "advanced": {
    "term_extraction_min_notes": 4,
    "alpha_curve": {"type": "log", "scale": 1.0},
    "mmr_lambda": 0.5,
    "max_terms": 6,
    "tag_terms_in_description": true,
    "placement_weights": {"identity": 0.6, "content": 0.4},
    "name_prefix_bonus": 0.15,
    "folder_vacancy_grace_periods": 3,
    "link_margin": 0.05,
    "max_total_links": 15,
    "inbox_min_cluster_size": 3,
    "inbox_similarity_threshold": 0.5
  }
}
```

`auto_cluster` — watcher triggers semantic placement automatically on new notes (`suggest_folder`/`move_notes` only — **not** `cluster_vault`, removed as an automatic path after the 2026-08-20 folder-collapse incident, see Service section). `cluster_threshold` — **vestigial**, still read and returned by `_get_cluster_config()` but no longer consumed anywhere inside `_maybe_cluster`; it only mattered for the removed `cluster_vault` fallback. `placement_similarity_threshold` — cosine threshold for semantic placement (0–1, default 0.55; currently `0.5` in this install). `type_fallback` — when no folder clears the threshold, fall back to the note's frontmatter `type:` field. `auto_describe` — watcher regenerates a folder's `_folder.md` (blended via adaptive α, same as `describe --all`) whenever a note is created/deleted/moved in or out of it, debounced per folder (`_REDESCRIBE_DEBOUNCE_SECONDS`). **Off by default** — see the Service section for the exact trigger scope and why body-only edits are excluded. `tag_terms_in_description` — normalize + separately-weighted tag scoring in c-TF-IDF description extraction (`false` reverts to the old diluted/unnormalized merge). `placement_weights.{identity,content}` — how `suggest_folder()` combines the note's identity (stem+tags) vs. content (body) cosine similarities; read by `cli.py::place`, `watcher.py::_maybe_cluster`, and the `suggest_folder` MCP tool (via the shared `graph/placement.py::resolve_placement_config()` helper), so all three agree on a suggestion for the same note. `name_prefix_bonus` — additive bonus when a note's stem starts with a described project folder's normalized name. `folder_vacancy_grace_periods` — consecutive empty structural-change checks before an emptied `source: auto` folder's `_folder.md` is archived to `.archive/` (default 3, read by `watcher.py::_get_folder_vacancy_grace_periods`; gated behind `auto_describe`, not a flag of its own — see "Folder lifecycle (Gate 1)" under Key technical decisions). **The default 3 is a placeholder carried over from the spec, not an empirically validated number.** `link_margin` / `max_total_links` — `auto_link`'s candidate-selection window (see "Desaturating the wikilink graph" under Key technical decisions): keep every candidate within `link_margin` of the top candidate's score, capped by `max_total_links`. Read by `graph/linker.py::_get_link_margin_config` — unlike `auto_cluster`/`auto_describe`, these are tuning floats rather than gating flags, so (deliberately, unlike `_get_cluster_config`) they go through `utils.load_config()` directly; a `{}` on error resolves to the same defaults via `.get()`, with no unsafe-default hazard. `http_host` / `http_port` / `http_path` — bind address for the HTTP transport, read by `mcp/http.py::configured_endpoint()` (defaults 127.0.0.1 / 8077 / `/mcp`; a corrupt config degrades to those loopback defaults via load_config's `{}` contract). Used identically by foreground `serve`, `start http`, and `status`, so all three describe the daemon by construction; explicit CLI flags override at `start http` time and are baked into ProgramArguments until the next rewrite. `auto_inbox` — Gate 2: routes a note into `inbox/` when `suggest_folder()` finds no semantic match and no `type:` fallback either, then checks whether `inbox/` has a cluster ready to spin out into a new real folder (see "Folder lifecycle (Gate 2)" under Key technical decisions). **Off by default** — same fail-safe-off contract as `auto_cluster`/`auto_describe`, read by its own `watcher.py::_get_inbox_config()` (not folded into `_get_cluster_config`'s tuple). `inbox_min_cluster_size` / `inbox_similarity_threshold` — the inbox clustering gate (minimum group size to spin out) and the greedy cosine-threshold used to group inbox notes by embedding similarity; both placeholders, not empirically validated, same status `link_margin`/`folder_vacancy_grace_periods` had when they first shipped.

**`auto_cluster` and `auto_describe` were re-enabled and the watcher restarted on 2026-08-22**, on the user's explicit go-ahead (both had been OFF and the watcher stopped since the 2026-08-20 folder-collapse recovery, per this file's own prior standing instruction not to flip them back on without asking). The three post-incident fixes were already in place before this re-enablement — `cluster_vault` removed as an automatic path, `described_folders()` excluding 0-note folders, and `_maybe_redescribe` debounced per folder (see Service section) — this is the first time they're being exercised under live `auto_cluster`/`auto_describe` traffic. When `auto_cluster` is on: the watcher runs `suggest_folder()` (Stage B semantic placement), computes cosine similarity against declared folder descriptions, and moves the file to the best match above `placement_similarity_threshold`. If no folder clears the threshold, falls back to the note's frontmatter `type:` field (`type_fallback`, currently `true` in this install — flipped from `false` during the recovery because the now-description-less type-folders, see Pending Work, need it to function as fallback destinations at all). Previously used wikilink-neighbour vote (`cluster_note`) — that signal is still returned by `cluster_note` as `neighbor_vote` but is no longer used for auto-placement. **Do not flip either flag back off, or stop the watcher, without asking** — same standing rule as before, just inverted: this is now the live configuration to preserve, not revert.

Two consequences:
- **Never assume a note's path from where it was created.** Resolve by stem (`vault.rglob(f"{stem}.md")`), which is what `cluster_note`, `place`, and the `delete` CLI already do.
- **Filter searches on `type=`, not folder.** The frontmatter `type:` is stable taxonomy; the folder drifts. This is intended behaviour, not a bug — see the vault note *frontmatter-type-field-is-stable-taxonomy-folder-drifts-with-auto-cluster*.

### XDG Base Directory paths

`archiver_rag/paths.py` is the single source of truth for where config, data, and cache live — `config_dir()`, `data_dir()`, `cache_dir()`, plus `config_path()`, `default_chroma_path()`, `centroids_path()`. On this machine: `~/.config/archiver-rag/` (config.json), `~/.local/share/archiver-rag/` (chroma_db/, centroids.json), `~/.cache/archiver-rag/` (`runtime.json`, the watcher heartbeat — see "Watcher heartbeat" under Key technical decisions). Backed by the `platformdirs` dependency, but **forced onto the Unix backend even on macOS** rather than platformdirs' native `~/Library/Application Support/...` default — this machine's other CLIs (nvim, git, gh, zed, starship) all already live under `~/.config`, so that's the idiomatic location for a tool in this world, not just on Linux. `centroids.json` lives in Data, not Cache, alongside `chroma_db/` — treated as install-derived index state rather than disposable, even though re-embedding it after a loss is cheap (one batched `embed()` call per stale folder, not a full reindex). The sentence-transformers/HuggingFace model cache is deliberately left untouched (still `~/.cache/huggingface/`, shared with other HF-based tools) — `embedder.py` was not changed.

Every consumer imports this module qualified (`from archiver_rag import paths`, then `paths.config_path()`) rather than `from archiver_rag.paths import config_path` — that way a single test monkeypatch of `archiver_rag.paths.config_path` reaches every call site, without the per-module rebinding `conftest.py` has to do for `get_vault_path` (`_MODULES_WITH_VAULT`).

**Migration from the pre-XDG `~/.archiver-rag/` layout is automatic and non-destructive.** `paths.ensure_migrated()` runs once per process from a `cli.py` `@app.callback()` — which fires before every subcommand, including the hidden `serve`/`watch` entry points, so this one call site covers the CLI, the MCP server, and the watcher. If `~/.archiver-rag/` exists and the new config path does not, it moves `config.json`, `chroma_db/`, and `centroids.json` into the new dirs, rewrites `chroma_path`/`install_path` inside the migrated `config.json` to point at the new data dir, and logs what moved. It **never deletes** `~/.archiver-rag/` — anything else left in there (e.g. this install's `backup-folder-collapse-2026-08-20/`) is untouched, and even the three migrated items are moved, not copied, so nothing duplicates. `uninstall` removes the new config/data/cache dirs and, if still present, the legacy dir too.

Before this migration, `~/.archiver-rag/` was independently hardcoded in **9 places across 6 files** (`utils.py` ×2, `init_cmd.py` ×2, `core/db.py`, `watcher.py` ×3), each with subtly different error-handling — raise vs. `{}` vs. `typer.Exit(1)` vs. a safe-off tuple. `paths.py` centralizes the path *resolution* only; every callsite kept its own documented error-handling contract (e.g. `watcher.py`'s `_get_*_config()` readers still resolve to safe-off defaults on any error — corrupt config must never turn on file-moving behavior). Full rationale: vault spec note *spec-xdg-base-directories-config-data-cache-paths*.

---

## Service

On Mac, the watcher runs as a launchd agent:
- Plist: `~/Library/LaunchAgents/com.archiver-rag.plist`
- Calls: `archiver-rag watch <vault_path>`
- Logs: `/tmp/archiver-rag.log` and `/tmp/archiver-rag.error.log`
- `KeepAlive: true` — auto-restarts on crash

**Detached MCP HTTP server (second launchd agent, `archiver-rag start http`).** The HTTP transport can run as its own daemon — label `com.archiver-rag.http`, plist `com.archiver-rag.http.plist`, unit `archiver-rag-http.service`, logs `/tmp/archiver-rag-http{,.error}.log`. One abstraction, two definitions in `service.py` (`ServiceDef`: label, plist/unit paths, log paths; `WATCHER`, `HTTP`) — `start`/`stop`/`state` are parameterized over it, so the watcher and the daemon cannot mix each other's files. Key properties:

- **No PID files.** launchd/systemd is the source of truth, same as the watcher; liveness parsing (`_darwin_state`/`_linux_state`) is shared verbatim, so a KeepAlive crash-loop renders as "Loaded but not running" for both.
- **Port pre-flight before load** (`service.port_in_use`, socket `connect_ex`): if anything already listens on host:port (a foreground `serve --transport http`, most likely), `start http` aborts with exit 1 *before* writing/loading — otherwise KeepAlive would crash-loop on the bind failure. Explicitly given `--host/--port/--path/--stateful/--allowed-host` are baked into ProgramArguments; everything else comes from config at process start, so `http_host/http_port/http_path` edits apply on restart. Every `start http` rewrites the service file, so flags persist until the next one.
- **Boot persistence asked at runtime**: `start http` prompts "Start automatically at login?" unless `--login/--no-login` is passed (scriptable).
- **Agent registration stays manual by decision** — the command prints the URL and a hint but never writes `~/.claude.json`; `mcp/register.py::register_mcp(url=...)` exists for that and is not wired into start.
- `stop http` unloads but keeps the service file (settings survive), matching `stop` semantics. `restart http` refuses to run when not installed rather than silently creating it.
- Foreground `serve --transport http` unchanged; the two transports are separate processes by nature (stdio IS its process; HTTP owns the port). Endpoint resolution is split by perspective: `mcp/http.py::configured_endpoint()` (config → loopback defaults) for `serve`, and `service.py::daemon_endpoint()` = configured_endpoint + flags baked into the installed service file for `start http` and `status` — the two together mean nothing describes a bind address the daemon isn't actually on.

**Watcher clustering hook (`watcher.py` — Stage B):** On new notes only (`is_new_note` gate), after `ingest_file` + `auto_link`: if `auto_cluster` is enabled, calls `suggest_folder()` (semantic cosine vs declared descriptions). If a folder clears `placement_similarity_threshold`, moves via `move_notes`. Anti-churn guard compares full vault-relative parent path (not just folder name) so subfolders are independent. Log: `Auto-placed foo.md → gotcha/ (semantic, 0.61)`.

**`cluster_vault` is never called automatically — removed after the 2026-08-20 folder-collapse incident.** `_maybe_cluster` used to fall back to `cluster_vault()` + `apply_clusters()` (label propagation on the whole-vault wikilink graph) whenever `cluster_threshold` notes in a row got no semantic suggestion. On this vault's dense auto-linked graph (`auto_link` adds ~12 `## Related` links per note), that fallback collapsed 61/73 notes into two folders named after the highest-internal-degree note in the runaway community (`_name_community` in `graph/clustering.py` literally turns a note's stem into a folder name), across a handful of automatic re-cluster passes within minutes — `type_fallback: false` at the time removed the only other escape valve. `cluster_vault`/`apply_clusters` are no longer imported by `watcher.py` at all (verify with `grep cluster_vault watcher.py` — it should only appear in comments/docstrings). They remain reachable exclusively via the explicit manual `archiver-rag cluster` CLI command, which the user runs deliberately and reviews before `--apply`. Do not reintroduce this as an automatic watcher path. Full incident writeup: vault note *folder-collapse-incident-cluster-vault-fallback-and-recovery*.

**`_folder.md` watcher branch** — `on_created`, `on_modified`, and `on_moved` (for atomic saves, which arrive as `moved`) detect `is_folder_note(p) and not is_hidden_path(p)` before the `is_indexable_note` check. On a folder-note event: call `_refresh_folder_centroid()` (recomputes and caches the embedding if description changed), but **do NOT call `ingest_file` or `auto_link`** — doing so puts `_folder.md` chunks in ChromaDB that `prune_orphans` cannot remove (the file still exists on disk). `on_deleted` for `_folder.md` runs the spurious-delete guard, then calls `drop_centroid`. The only write is to the XDG data dir's `centroids.json`, outside the vault — no re-entrancy risk.

**Watcher auto-describe hook (`_maybe_redescribe`, `watcher.py`)** — `describe` (per-folder term extraction + adaptive-α blend) had always been manual-only; `decision/`'s description was generated once and never revisited as ~38 notes piled into it, one of the causes of the gravity well (see `decision/note-tags-in-placement-embedding-implemented-margin-confirmed-widened` in the vault). Gated behind `auto_describe` (default `false`, same error-safe-off contract as `_get_cluster_config`).

**Trigger scope is structural changes only** — a note created into, deleted from, or moved between folders — never `on_modified` (body-only edit, no folder change). `on_modified` can fire multiple times per logical save with no debouncing anywhere in the watcher, and a redescribe does real work (corpus rebuild + MMR `embed()` calls); triggering it there would be chatty for no benefit, since membership — not a single note's wording — is what was going stale. The resulting `_folder.md` event is absorbed by `_refresh_folder_centroid`'s own fingerprint no-op — confirmed no re-entrancy into `ingest_file`/`auto_link`/`_maybe_cluster`, matching the `_folder.md` branch's existing analysis above.

**Debounced per folder — `_REDESCRIBE_DEBOUNCE_SECONDS = 5.0`, `_last_redescribed: dict[str, float]` (added after the 2026-08-20 folder-collapse incident).** The original assumption — "no debounce needed, each call is cheap" — was wrong in practice: a batch move (`place --all --apply`, or the now-removed `cluster_vault` fallback) fires one `on_moved` per note, and every one of them redescribed its destination folder. When dozens of notes landed in the same folder in one incident, that meant dozens of back-to-back corpus-rebuild+MMR passes on it — evidenced by a `_folder.md` frozen with `note_count: 73` (the entire vault, mid-collapse). At most one regeneration per folder per debounce window now.

`_maybe_cluster` now returns the rel_folder it actually moved a note into (or `None`) so `on_created`/`on_moved` can redescribe the note's **final** resting folder rather than assuming its original location — a note auto-placed into a different folder must redescribe *that* folder, not the one it briefly landed in. `on_moved` redescribes both sides of a cross-folder move (source lost a note, destination gained one); a same-folder atomic-save rename has `src_folder == dst_folder` and is skipped as not a membership change.

The decide/blend/write policy itself (skip `source: manual`, blend via `alpha_for`/`blend_terms` if a description exists, gravity-well pathology check, write) lives in `vault/folder_notes.py::apply_extracted_terms()` — extracted out of `describe_cmd`'s per-folder loop so the CLI and the watcher share one code path. The two callers differ only in extraction strategy: `describe_cmd --all` still uses `extract_terms_all()` to amortize the cross-folder IDF table across many folders in one run; the watcher uses the cheaper single-folder `extract_terms()` (previously dead code in production, only used in tests) since it only ever redescribes one folder per event.

---

## MCP registration (per-agent)

**Claude Code** — `~/.claude.json`:

```json
{
  "mcpServers": {
    "archiver-rag": {
      "command": "/Users/fernanrod/.local/bin/archiver-rag",
      "args": ["serve"]
    },
    "obsidian": {
      "command": "npx",
      "args": ["@bitbonsai/mcpvault@latest", "/path/to/vault"]
    }
  }
}
```

An HTTP-transport entry uses a different shape — `{"type": "http", "url": "http://127.0.0.1:8077/mcp"}` — and `register_mcp(url=..., name=...)` writes it. The two can coexist under different names; stdio remains what `archiver-rag init` registers.

Both servers run side by side:
- **archiver-rag** — semantic search, graph traversal, vault health, reorganization, knowledge logging
- **mcpvault** — raw file read/write/delete/move (14 tools, no plugins required)

**Other agents** — `archiver_rag/mcp/register.py` only writes `~/.claude.json`; registering with another agent's MCP client is a manual step, documented per agent in `skill/`:
- `skill/codex/AGENTS.md` — `~/.codex/config.toml`
- `skill/copilot/copilot-instructions.md` — `.vscode/mcp.json`
- `skill/opencode/AGENTS.md` — `opencode.json`

---

## Vault knowledge model

The vault is a **knowledge graph**, not a project hierarchy. Notes are independent nodes connected by `[[wikilinks]]`. Relationships range from tight (direct links) to loose (semantic proximity).

Note types are expressed via frontmatter `type:` field and tags, not folder structure:

```yaml
---
type: decision
tags: [architecture, prisma]
related:
  - AsyncLocalStorage
  - PrismaExtensions
date: 2026-04-18
---
```

The `## Related` section at the bottom of each note is managed automatically by `linker.py`. Do not manually edit this section — it will be overwritten on next ingest.

---

## Vault-first memory — MANDATORY

**The Obsidian vault is the primary memory system. The agent's own memory system (if any) is a fallback only.**

Rules that apply at the start of every task and every memory operation:

1. **Before answering any question** — call `search_vault(query)` first. Only use the agent's own memory if the vault returns no relevant results (score below threshold or server unavailable).
2. **Before reading any source file** — call `search_vault` first. Skip the file read if the vault result is sufficient.
3. **Before writing to the agent's own auto-memory store** (e.g. Claude Code's `~/.claude/projects/.../memory/`) — call `log_note` first. The vault entry is authoritative; the agent-local file is optional and secondary.
4. **After solving any non-trivial problem** — call `log_note` with the appropriate type (`decision`, `lesson`, `gotcha`, `pattern`). Do not skip this step.

Never skip `search_vault` to save time. A vault miss is fast; redundant agent-local memory use wastes context and diverges from the knowledge graph.

## Vault search tips

- Use `context_note` parameter when you know which note the query relates to — boosts connected results
- Use `type=` to filter by frontmatter taxonomy (stable across `auto_cluster` moves)
- `get_connections(note, depth=2)` before reorganizing to understand what would break
- `suggest_folder(note)` before manually placing a new note — semantic suggestion, matches `place` and the watcher
- After any out-of-band rename run `archiver-rag prune` before searching

---

## Dependencies

```toml
dependencies = [
    "chromadb>=1.5,<2",
    "sentence-transformers>=5,<6",
    "watchdog>=6,<7",
    "numpy",
    "mcp>=1.9,<2",
    "starlette>=1.2",
    "uvicorn>=0.49",
    "typer>=0.25,<0.26",
    "rich",
    "pyyaml",
    "platformdirs",
]
```

## Python version

Requires Python >= 3.11. Currently running 3.14.3.

## Testing

```bash
/Users/fernanrod/.local/pipx/venvs/archiver-rag/bin/python -m pytest tests/ -q
```

There is no `python` on PATH and no pytest in the system venv — the pipx venv is the only interpreter with `chromadb` installed. (pi-lens's pytest adapter hardcodes `command: "python"` and therefore cannot run this suite on this machine — verify with the pipx venv python above.) 447 tests, all green. Tests marked `slow` load the sentence-transformers model (`embed()` calls) and take ~8 seconds; run with `-m "not slow"` to skip them.

`tests/conftest.py` carries the safety net: `_no_real_vault` is **autouse** and makes `get_vault_path()` raise in every test, so a test can never touch the real vault by accident. Each module binds its own reference via `from archiver_rag.utils import get_vault_path`, so the fixture patches every module in `_MODULES_WITH_VAULT` individually — **add new modules to that list** when they import `get_vault_path` at module level. `tmp_vault` opts back in, repointing those same bindings at a temp dir.

## Pyright config

```json
{
  "typeCheckingMode": "basic",
  "reportMissingModuleSource": "none",
  "reportUnknownMemberType": "none",
  "reportUnknownVariableType": "none"
}
```

ChromaDB and watchdog stubs are imprecise — use `# type: ignore[index]` and `# type: ignore[arg-type]` at specific call sites rather than disabling checks globally.
