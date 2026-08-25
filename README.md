<p align="center">
  <img src="assets/archiver-rag-lockup.svg" alt="Archiver RAG" width="320" />
</p>

<p align="center">
  <em>A finding aid for your knowledge graph</em>
</p>

<p align="center">
  The agent-agnostic memory management system for your Obsidian vault
</p>

Archiver RAG turns your Obsidian vault into a live, queryable knowledge graph that any MCP-compatible AI agent can search, update, and reorganize — without ever leaving its native interface.

Connect it once. Every agent you use (Claude Code, Cursor, Gemini CLI, or your own) gets semantic search, automatic knowledge logging, wikilink-aware graph traversal, and vault health monitoring out of the box.

---

## How it works

```
Your Obsidian vault (.md files)
         ↓  file watcher + ingest pipeline
     ChromaDB  (persistent vector store)
         ↓  MCP server
     Any MCP-compatible agent
```

Three layers make search smarter than plain embeddings:

1. **Contextual prefix** — each chunk is embedded with its note's metadata (folder, tags, wikilinks), so vectors carry structural context
2. **Rich metadata filtering** — ChromaDB stores folder, type, tags, incoming link count, and wikilinks for filtered retrieval
3. **Graph reranking** — after vector search, results are re-scored by wikilink proximity to a context note and hub importance

The file watcher runs as a background service. Edit a note in Obsidian, save it, and it's indexed and auto-linked within seconds — no manual sync needed.

---

## Features

- **Semantic search** with graph reranking — finds notes by meaning, then boosts results connected via wikilinks
- **Auto-linking** — after every ingest, appends a `## Related` section with `[[wikilinks]]` to build the knowledge graph automatically
- **Knowledge logging** — create categorized notes (`decision`, `lesson`, `gotcha`, `pattern`, …) from any agent; date lives in frontmatter, filename is the slug identity wikilinks are written against
- **Vault health** — single call returns orphaned notes, broken links, missing frontmatter, tag stats, and recent activity
- **Wikilink-aware reorganization** — move files and every `[[link]]` across the vault is rewritten automatically
- **Smart clustering** — label-propagation algorithm groups notes by wikilink structure and suggests folder organization
- **Agent-agnostic** — exposes a standard MCP interface; works with any MCP-compatible client

---

## Requirements

- Python >= 3.10
- [pipx](https://pipx.pypa.io/) (recommended for installation)
- An Obsidian vault (local `.md` files)
- An MCP-compatible agent (Claude Code, Cursor, etc.)

---

## Installation

```bash
pipx install archiver-rag
```

> Use `pipx`, not `pip install` — pipx creates an isolated environment and exposes the CLI globally on `PATH`, which is required for MCP registration to find the correct executable.

For local development from a clone of this repo, use `pipx install --editable .` instead.

---

## Development

```bash
git clone https://github.com/FernandoJRR/archiver-rag && cd archiver-rag
pipx install --editable .   # global CLI — required for MCP registration
pip install -e ".[dev]"     # adds pytest
pytest                      # 250 tests, ~5 s
```

Tests marked `slow` load the sentence-transformers model; skip them with `-m "not slow"`.

`tests/` layout (highlights — see `CLAUDE.md` for the full file-by-file list):
- `conftest.py` — `_no_real_vault` (autouse): patches `get_vault_path` in every module that imports it, so no test ever touches the real vault. `_no_real_home_paths` (autouse): redirects `archiver_rag.paths`' config/data/cache dirs so no test ever touches the real `~/.config/archiver-rag/`, `~/.local/share/archiver-rag/`, or `~/.cache/archiver-rag/`. Opt-in `tmp_vault` and `tmp_install` fixtures for tests that need real files.
- `test_wikilinks.py` — 28 unit tests for the offset-based wikilink extractor
- `test_linker_section.py` — 11 characterization tests for `_append_links_section`

---

## Setup

Run the one-time setup wizard:

```bash
archiver-rag init
```

This will:
1. Ask for your vault path
2. Index your vault into ChromaDB
3. Register the MCP server in `~/.claude.json` (or prompt you to do it manually for other clients)
4. Install the background watcher as a launchd agent (Mac) or systemd service (Linux)

---

## MCP registration (manual)

If you prefer to register manually, add this to your MCP client config:

```json
{
  "mcpServers": {
    "archiver-rag": {
      "command": "/path/to/archiver-rag",
      "args": ["serve"]
    }
  }
}
```

Find the executable path with `which archiver-rag`.

For Claude Code specifically, use:

```bash
claude mcp add --scope user archiver-rag $(which archiver-rag) serve
```

---

## HTTP transport

By default the server speaks MCP over **stdio**: your client spawns it as a child
process. That means one client per server, and no way to reach the vault from another
machine. `--transport http` serves the same seven tools over streamable HTTP instead, so
several clients can share one warm process:

```bash
archiver-rag serve --transport http            # http://127.0.0.1:8077/mcp
```

Register it with Claude Code:

```bash
claude mcp add --scope user --transport http archiver-rag http://127.0.0.1:8077/mcp
```

### Other clients

Both transports work with any MCP-compatible client. Replace `/path/to/archiver-rag`
with the output of `which archiver-rag`.

#### opencode

Add an `mcp` block to `~/.config/opencode/opencode.jsonc` (or `opencode.json`, or a
project-local file of either name — opencode's schema allows comments and trailing
commas in both):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    // stdio — opencode launches the server itself
    "archiver-rag": {
      "type": "local",
      "command": ["/path/to/archiver-rag", "serve"],
      "enabled": true,
      "timeout": 30000
    },
    // HTTP — connects to a server you started separately
    "archiver-rag-http": {
      "type": "remote",
      "url": "http://127.0.0.1:8077/mcp",
      "enabled": true,
      "timeout": 30000
    }
  }
}
```

Use one or the other — both are shown here only to give the shape of each.

> **Raise `timeout`.** opencode defaults to 5000 ms per request, and the *first*
> `search_vault` call loads the embedding model — measured at ~5 s on a warm disk, right
> at the limit. Without a higher timeout the first search may fail and then succeed on a
> retry, which is a confusing way to meet the tool. Later calls take ~70 ms.

#### Codex CLI

```bash
# stdio — Codex launches the server itself
codex mcp add archiver-rag -- $(which archiver-rag) serve

# HTTP — connects to a server you started separately
codex mcp add archiver-rag-http --url http://127.0.0.1:8077/mcp
```

Both write to `~/.codex/config.toml`, and you can equally hand-edit it:

```toml
[mcp_servers.archiver-rag]
command = "/path/to/archiver-rag"
args = ["serve"]

[mcp_servers.archiver-rag-http]
url = "http://127.0.0.1:8077/mcp"
```

Verify with `codex mcp list`. If your server sits behind a proxy that wants a token,
`--bearer-token-env-var NAME` reads it from the environment — archiver-rag itself never
checks it (see below).

### Reaching it from another machine

> **archiver-rag performs no authentication and terminates no TLS.** Anyone who can
> reach the port has full access: the entire vault is readable, and `log_note`,
> `move_notes` and `cluster_vault` can modify it.

It is deliberately not this tool's job to decide how you secure that. Keep the server on
loopback and put a layer you already trust in front of it — a reverse proxy terminating
TLS, an SSH tunnel, a VPN, or a private overlay network. The server does not need to know
which; it stays on plain HTTP at `127.0.0.1:8077` in every case.

If you bind beyond loopback (`--host 0.0.0.0`), the CLI prints a warning — heed it. You
can additionally enable DNS-rebinding protection by naming the hostnames you expect to
serve:

```bash
archiver-rag serve --transport http --allowed-host vault.internal.example
```

Requests arriving with any other `Host` header are rejected with `421`. The bind address
itself is always allowed, so local access keeps working.

### Options

| Flag | Default | Meaning |
|---|---|---|
| `--transport` | `stdio` | `stdio` or `http` |
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8077` | Bind port |
| `--path` | `/mcp` | HTTP route |
| `--allowed-host` | *(none)* | Enable DNS-rebinding protection for this Host (repeatable) |
| `--stateful` | off | Use HTTP sessions + SSE instead of stateless JSON |

`http_host`, `http_port` and `http_path` in `config.json` supply defaults for the
corresponding flags.

---

## Agent instructions (skills)

Registering the MCP server gives an agent *access* to the tools — but agents tend to fall back on their own internal memory instead of reaching for the vault. The instruction files in [`skill/`](skill/) fix that: they enforce a **vault-first rule** so the agent searches and stores knowledge in your vault before anything else.

**What the skill enforces:**

- **Before answering or reading source files** — call `search_vault` first; only fall back to internal memory if the vault returns nothing relevant
- **When something important is missing from the vault** — proactively `log_note` it. If a fact, decision, or piece of context matters to the overall picture and a `search_vault` came back empty, record it so the knowledge graph grows instead of letting that context die in a single session
- **After solving a non-trivial problem** — call `log_note` to capture the decision/lesson/gotcha back into the vault
- **The vault is the authoritative memory system** — internal agent memory is a fallback only

A version is provided for each agent, since each loads instructions differently:

| Agent | File | Install to |
|---|---|---|
| Claude Code | [`skill/claude-code/SKILL.md`](skill/claude-code/SKILL.md) | `~/.claude/skills/archiver-rag/SKILL.md` (on-demand skill) |
| OpenCode | [`skill/opencode/AGENTS.md`](skill/opencode/AGENTS.md) | project root `AGENTS.md` or `~/.config/opencode/AGENTS.md` |
| Codex CLI | [`skill/codex/AGENTS.md`](skill/codex/AGENTS.md) | project root `AGENTS.md` or `~/.codex/AGENTS.md` |
| GitHub Copilot | [`skill/copilot/copilot-instructions.md`](skill/copilot/copilot-instructions.md) | `.github/copilot-instructions.md` |

Each file is self-contained — it includes the MCP registration snippet for that agent plus the full vault-first rules and tool reference. For Claude Code the file is an on-demand skill; for the others it's an always-on instruction file (loaded into every session), which makes the vault-first behavior unconditional.

---

## CLI reference

```bash
archiver-rag init              # one-time setup wizard
archiver-rag start             # start the background watcher service
archiver-rag stop              # stop the service
archiver-rag restart           # restart the service
archiver-rag status            # service liveness, watcher activity, index drift, config
archiver-rag status --json     # same report as JSON
archiver-rag index             # force re-index the entire vault (runs prune_orphans)
archiver-rag sync              # ingest only new/modified notes + prune orphaned chunks
archiver-rag prune             # remove index chunks whose source file no longer exists
archiver-rag search "query"    # test semantic search from the terminal
archiver-rag health            # index-vs-disk drift + vault health (orphans, broken links)
archiver-rag health --json     # same report as JSON
archiver-rag logs              # tail the service log

# Knowledge logging
archiver-rag log "Title" --type decision --tag arch --related NoteA

# Clustering
archiver-rag cluster                      # suggest folder groupings
archiver-rag cluster --apply              # move files automatically
archiver-rag place <note>                 # suggest folder for a single note
archiver-rag place <note> --apply         # move it immediately

# Config
archiver-rag config --auto-cluster        # enable auto-clustering in the watcher
archiver-rag config --cluster-threshold 5 # notes before a full re-cluster

archiver-rag uninstall         # remove all data, service, and MCP registration
```

---

## MCP tools (for agents)

Once registered, agents have access to 7 tools:

| Tool | What it does |
|---|---|
| `search_vault` | Semantic search with graph reranking. `context_note` boosts wikilink neighbors. `type` filters by frontmatter `type:` (stable across folder moves). `tags` post-filters by tag overlap. |
| `vault_status` | Vault structure, health diagnostics, tag stats, and recent activity in one call. |
| `get_connections` | BFS wikilink traversal — outgoing and incoming links up to depth 3. |
| `move_notes` | Move files and auto-rewrite all `[[wikilinks]]` across the vault. |
| `log_note` | Create a knowledge note at `{type}/{slug}.md`; watcher indexes and auto-links it immediately. |
| `cluster_note` | Suggest a folder for one note based on where its wikilink neighbors live. |
| `cluster_vault` | Label-propagation clustering of the entire vault with folder suggestions. |

---

## Configuration

All runtime config lives at the XDG config path (`~/.config/archiver-rag/config.json` on Linux/macOS, resolved by `archiver_rag/paths.py`):

```json
{
  "vault_path": "/path/to/your/vault",
  "install_path": "/Users/you/.local/share/archiver-rag",
  "chroma_path": "/Users/you/.local/share/archiver-rag/chroma_db",
  "auto_cluster": false,
  "cluster_threshold": 5
}
```

Data (the ChromaDB index, `centroids.json`) lives at `~/.local/share/archiver-rag/`, and a reserved-for-future cache dir at `~/.cache/archiver-rag/`. An existing pre-XDG `~/.archiver-rag/` install is migrated automatically and non-destructively the first time any `archiver-rag` command runs — the old directory is left in place, never deleted.

`auto_cluster` — automatically suggest and apply folder placement for new notes via the watcher.  
`cluster_threshold` — number of new notes created before triggering a full `cluster_vault` run.

---

## The knowledge graph model

The vault is treated as a **knowledge graph**, not a file hierarchy. Notes are nodes; wikilinks are edges. Relationships range from tight (direct links) to loose (semantic proximity surfaced by search).

Note types are expressed through frontmatter, not folder structure:

```yaml
---
type: decision
tags: [architecture, async]
related:
  - AsyncLocalStorage
  - PrismaExtensions
date: 2026-04-27
---
```

The `## Related` section at the bottom of each note is managed automatically by the auto-linker after every ingest. Don't edit it manually — it will be overwritten.

---

## Roadmap

Features on the way:

- **RAG-Anything integration** — extend ingestion beyond Markdown to handle PDFs, Office documents, images, and other file types, so the vault can become a true multi-format knowledge base rather than `.md`-only.
- **Archiver subagents** — dedicated subagents that take over vault management (search, logging, reorganization, clustering) on the main agent's behalf, so the primary agent can delegate knowledge work instead of context-switching into it.

---

## License

MIT
