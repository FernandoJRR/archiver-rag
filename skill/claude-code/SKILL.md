---
name: archiver-rag
description: Guide for using the archiver-rag MCP tools — semantic search, vault health, wikilink traversal, knowledge logging, and vault reorganization. Use when working with an Obsidian vault via the archiver-rag server.
---

Overview:
The archiver-rag MCP server exposes 7 tools for working with an Obsidian vault as a knowledge graph. This skill describes when and how to call each tool. $ARGUMENTS[0] is optional context about the current task.

## Memory priority rule

**The vault is the first source of truth for all memory operations.** Before reading from or writing to the auto-memory system (`~/.claude/projects/.../memory/`), always go through the RAG:

| Operation | Do first | Fallback |
|-----------|----------|----------|
| Recall a fact / past decision | `search_vault` | read auto-memory files |
| Store a lesson, decision, gotcha | `log_note` | write auto-memory files |
| Check what is already known | `search_vault` | scan MEMORY.md |

Only fall back to the auto-memory system if the vault search returns no relevant results (score below threshold or vault unavailable).

When writing a new memory via `log_note`, pick the `type` that matches the auto-memory category:

| Auto-memory type | `log_note` type |
|------------------|-----------------|
| `user` | `note` (tag: `user-profile`) |
| `feedback` | `lesson` or `pattern` |
| `project` | `decision` |
| `reference` | `note` (tag: `reference`) |

After logging to the vault, you may also write the auto-memory file so the index stays populated — but the vault entry is authoritative.

---

## Rules

- Always call `search_vault` before reading source files
- Always call `search_vault` before reading auto-memory files
- `context_note` accepts a bare stem (`AuditTrail`) or a relative path (`folder/AuditTrail.md`)
- `get_connections` depth maximum is 3 — BFS grows quickly beyond that
- Never edit `## Related` sections manually — `linker.py` owns them
- `move_notes` rewrites wikilinks and aliases automatically — do not patch links by hand
- Preview `cluster_vault` with `apply=false` before using `apply=true`

## Decision flow

```
Starting any task?
  → search_vault first (before reading source files OR auto-memory files)
  → add context_note= if you already know which note the query relates to

Recalling something from memory?
  1. search_vault(query)          — vault is primary source of truth
  2. if score < threshold or vault unavailable → fall back to auto-memory files

Saving something to memory?
  1. log_note(title, content, type, tags) — vault is primary source of truth
  2. optionally mirror to auto-memory file so MEMORY.md index stays populated

Finished a non-trivial task?
  → offer log_note with an appropriate type (decision, lesson, gotcha, pattern)

Considering vault reorganization?
  1. vault_status          — understand current structure and health
  2. get_connections(note, depth=2) — understand what would break when moving a note
  3. cluster_note(note)    — lightweight suggestion for a single note
     OR cluster_vault      — full label-propagation for the whole vault
  4. move_notes            — execute moves (or use apply=true on cluster tools)

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

Returns array of chunks with `content`, `source`, `relevance_score`, `base_score`, `graph_boost`, `hub_boost`.

When to use:
- Always call before reading source files — vault context saves tokens
- Always call before reading auto-memory files — vault is the primary source of truth
- Pass `context_note` when the query is about a specific existing note
- Lower `min_score` only if results are too sparse; raising it tightens precision

---

### vault_status
Single call that returns structure, health, tag stats, and recent activity. No parameters.

Returns:
- `structure` — total_notes, total_folders, folder list
- `health` — orphaned_notes, no_frontmatter, empty_notes, broken_links (up to 20 each)
- `tags` — most_used (top 10), total_unique
- `recent` — modified and created (last 5 each)

When to use:
- Before any reorganization to understand vault state
- To spot health issues (orphaned notes, broken links, missing frontmatter)
- Quick orientation when starting work on an unfamiliar vault

---

### get_connections
BFS wikilink traversal — returns outgoing and incoming links at each depth level.

```
note   string   required         Note stem (e.g. 'AuditTrail') or relative path
depth  int      default 1 (max 3) Hops to traverse
```

Returns `connections.direct` / `connections.depth_2` / ..., each with `outgoing` and `incoming` arrays, plus a flat `all_connected` list.

When to use:
- Before moving or renaming a note — understand what links would be affected
- To discover the neighborhood of a note (depth=2 for two-hop view)
- To gauge how central a note is (large `incoming` = hub note)

---

### move_notes
Move one or more files and automatically rewrite all `[[wikilinks]]` across the vault.

```
moves   array   required   List of { source, destination } (paths relative to vault root)
```

Returns `{ moved, failed, succeeded[], errors[] }`.

When to use:
- Reorganizing notes into folders
- Renaming notes (wikilinks auto-update, including aliases)
- Batch moves — pass all moves in one call
- Called automatically by cluster tools when `apply=true`

Never manually update wikilinks after a move — this tool handles it.

---

### log_note
Create a dated knowledge note in the vault. The watcher auto-indexes and auto-links it.

```
title          string         required              Note title
content        string         required              Body in markdown
type           string         default "note"        Category, becomes the folder (decision, lesson, gotcha, pattern, meeting, idea, ...)
tags           string[]       optional              Tags
related_notes  string[]       optional              Note stems to link to
```

Returns `{ created, type, title, tags, related, path }`.

Filename format: `{vault}/{type}/{date}-{slug}.md`  
Collision-safe: appends `-1`, `-2` if name exists.

When to use:
- **Primary memory write path** — call this before writing to auto-memory files
- After solving a non-trivial problem — offer to log a `lesson` or `decision`
- Capture architectural decisions (`type=decision`)
- Document surprising behavior or gotchas (`type=gotcha`)
- Record patterns worth reusing (`type=pattern`)

Do not manually edit the `## Related` section at the bottom of any note — `linker.py` overwrites it on next ingest.

---

### cluster_note
Suggest a folder for a single note based on where its wikilink neighbors currently live. Lightweight — no label propagation.

```
note   string    required          Note filename (e.g. 'AuditTrail.md')
apply  boolean   default false     Move the note immediately if true
```

Returns `{ note, suggested_folder, votes, total_neighbors, reason }`.  
`suggested_folder` is `null` if note has no neighbors or all neighbors are in vault root.

When to use:
- Immediately after creating a note — fast placement suggestion
- Before deciding manually where a note belongs
- Called automatically by the watcher when `auto_cluster=true` in config

---

### cluster_vault
Full label-propagation clustering of the entire vault wikilink graph.

```
min_cluster_size   int       default 2     Minimum notes to form a cluster
apply              boolean   default false Move all files automatically if true
```

Returns `{ total_notes, total_clusters, unclustered[], clusters[] }`.  
Each cluster has `name`, `size`, `notes[]`, `suggested_folder`.

When to use:
- Organizing a messy or new vault
- After significant vault growth (many new notes)
- Always run with `apply=false` first to preview before committing
- Raise `min_cluster_size` to 3–5 to reduce noise in large vaults
