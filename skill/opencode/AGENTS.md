# archiver-rag — OpenCode instructions

> **Install:** Copy this file to your project root as `AGENTS.md` (or merge it into
> an existing one), or place it globally at `~/.config/opencode/AGENTS.md`.
> OpenCode loads `AGENTS.md` into every session automatically — these rules are
> always active, there is no on-demand skill to invoke.

## MCP registration

Add the archiver-rag server to `opencode.json` (project root) or
`~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "archiver-rag": {
      "type": "local",
      "command": ["/Users/fernanrod/.local/bin/archiver-rag", "serve"],
      "enabled": true
    }
  }
}
```

Run `which archiver-rag` to confirm the executable path on your machine.

---

## Vault-first memory — MANDATORY

**The Obsidian vault is the primary memory system. Your internal context is a
fallback only.** The archiver-rag MCP server exposes 7 tools for working with the
vault as a knowledge graph. Apply these rules at the start of every task and every
memory operation:

1. **Before answering any question** — call `search_vault(query)` first. Only rely
   on your own context if the vault returns no relevant results (low score or server
   unavailable).
2. **Before reading any source file** — call `search_vault` first. Skip the file
   read if the vault result is sufficient.
3. **Before writing notes or memory anywhere else** — call `log_note` first. The
   vault entry is authoritative.
4. **After solving any non-trivial problem** — call `log_note` with the right type
   (`decision`, `lesson`, `gotcha`, `pattern`). Do not skip this step.

Never skip `search_vault` to save time. A vault miss is fast; redundant guessing
wastes context and diverges from the knowledge graph.

## Rules

- Always call `search_vault` before reading source files
- `context_note` accepts a bare stem (`AuditTrail`) or a relative path (`folder/AuditTrail.md`)
- `get_connections` depth maximum is 3 — BFS grows quickly beyond that
- Never edit `## Related` sections manually — `linker.py` owns them
- `move_notes` rewrites wikilinks and aliases automatically — do not patch links by hand
- Preview `cluster_vault` with `apply=false` before using `apply=true`

## Decision flow

```
Starting any task or question?
  → search_vault first (before reading source files OR using internal context)
  → add context_note= if you already know which note the query relates to

Recalling something?
  1. search_vault(query)          — vault is primary source of truth
  2. if score < threshold or vault unavailable → fall back to internal context

Saving something?
  1. log_note(title, content, type, tags) — vault is primary source of truth

Finished a non-trivial task?
  → log_note with an appropriate type (decision, lesson, gotcha, pattern)

Considering vault reorganization?
  1. vault_status                    — current structure and health
  2. get_connections(note, depth=2)  — what would break when moving a note
  3. cluster_note(note) OR cluster_vault
  4. move_notes (or apply=true on cluster tools)

Placing a newly created note?
  → cluster_note(note) first — fast neighbor-vote, no label propagation
```

## Tool reference

### search_vault
Semantic search over the vault with graph reranking.

```
query          string   required   What to search for
n_results      int      default 3  Number of chunks to return
min_score      float    default 0.35  Minimum base cosine score to include
context_note   string   optional   Note stem or path — boosts wikilink neighbors
```

Returns array of chunks with `content`, `source`, `relevance_score`, `base_score`,
`graph_boost`, `hub_boost`.

### vault_status
Single call returning structure, health, tag stats, and recent activity. No
parameters. Returns `structure` (total_notes, total_folders, folders), `health`
(orphaned, no_frontmatter, empty, broken_links), `tags` (most_used, total_unique),
`recent` (modified, created). Use before any reorganization.

### get_connections
BFS wikilink traversal.

```
note   string   required          Note stem or relative path
depth  int      default 1 (max 3) Hops to traverse
```

Returns `connections.direct` / `connections.depth_2` / ... with `outgoing` and
`incoming` arrays, plus a flat `all_connected` list. Use before moving/renaming a
note to see what links would break.

### move_notes
Move one or more files and automatically rewrite all `[[wikilinks]]` across the vault.

```
moves   array   required   List of { source, destination } (paths relative to vault root)
```

Returns `{ moved, failed, succeeded[], errors[] }`. Never manually update wikilinks
after a move — this tool handles it.

### log_note
Create a dated knowledge note. The watcher auto-indexes and auto-links it.

```
title          string     required          Note title
content        string     required          Body in markdown
type           string     default "note"    Category, becomes the folder (decision, lesson, gotcha, pattern, ...)
tags           string[]   optional          Tags
related_notes  string[]   optional          Note stems to link to
```

Returns `{ created, type, title, tags, related, path }`. Filename:
`{vault}/{type}/{date}-{slug}.md`, collision-safe. Do not manually edit the
`## Related` section — `linker.py` overwrites it on next ingest.

### cluster_note
Suggest a folder for a single note based on where its wikilink neighbors live.
Lightweight, no label propagation.

```
note   string    required        Note filename (e.g. 'AuditTrail.md')
apply  boolean   default false   Move the note immediately if true
```

Returns `{ note, suggested_folder, votes, total_neighbors, reason }`.
`suggested_folder` is `null` if the note has no neighbors or all are in vault root.

### cluster_vault
Full label-propagation clustering of the entire vault wikilink graph.

```
min_cluster_size   int       default 2      Minimum notes to form a cluster
apply              boolean   default false  Move all files automatically if true
```

Returns `{ total_notes, total_clusters, unclustered[], clusters[] }`. Always run
with `apply=false` first to preview.
