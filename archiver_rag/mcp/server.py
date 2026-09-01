import asyncio
import json
import threading
from pathlib import Path

import anyio.to_thread
from mcp.server import Server
from mcp.server.lowlevel.server import request_ctx
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from archiver_rag import utils
from archiver_rag.core.search import search_vault
from archiver_rag.graph.connections import get_connections
from archiver_rag.vault.health import vault_status
from archiver_rag.vault.reorganize import move_notes

app = Server("obsidian-rag")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_vault",
            description="Semantically search your Obsidian vault for relevant notes",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What you want to search for",
                    },
                    "n_results": {
                        "type": "integer",
                        "description": "Number of chunks to return (default 3)",
                        "default": 3,
                    },
                    "context_note": {
                        "type": "string",
                        "description": "Note name or path used as graph context (e.g. 'AuditTrail'). Boosts results directly connected via wikilinks.",
                    },
                    "type": {
                        "type": "string",
                        "description": "Filter by frontmatter type: field, e.g. 'decision', 'gotcha', 'pattern', 'lesson', 'reference'. Stable taxonomy — unaffected by auto_cluster folder moves.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter to notes containing any of these tags. Matched case-insensitively; any overlap returns the note.",
                    },
                    "min_score": {
                        "type": "number",
                        "description": "Minimum semantic score before graph reranking (default 0.35). Lower to widen recall.",
                        "default": 0.35,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="vault_status",
            description="Get vault structure, health report, tag stats, and recent activity",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="move_notes",
            description="Move one or more files to new locations in the vault. Fixes wikilinks automatically after moving .md files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "moves": {
                        "type": "array",
                        "description": "List of moves. Single item moves one file.",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "source": {
                                    "type": "string",
                                    "description": "Current path relative to vault root",
                                },
                                "destination": {
                                    "type": "string",
                                    "description": "New path relative to vault root",
                                },
                            },
                            "required": ["source", "destination"],
                        },
                    },
                },
                "required": ["moves"],
            },
        ),
        Tool(
            name="log_note",
            description=(
                "Create a knowledge note in the vault. "
                "Use 'type' to categorize — decision, meeting, lesson, idea, "
                "reference, pattern, or anything that fits. "
                "The note is indexed and auto-linked immediately."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Note title"},
                    "content": {
                        "type": "string",
                        "description": "Note body in markdown.",
                    },
                    "type": {
                        "type": "string",
                        "description": "Note category, becomes the folder. E.g. decision, meeting, lesson, idea.",
                        "default": "note",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags",
                    },
                    "related_notes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Note stems to link to, e.g. 'AsyncLocalStorage'",
                    },
                },
                "required": ["title", "content"],
            },
        ),
        Tool(
            name="cluster_vault",
            description="Analyze wikilink structure and suggest folder groupings using label propagation. Set apply=true to move files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "min_cluster_size": {
                        "type": "integer",
                        "description": "Minimum notes per cluster. Default 2.",
                        "default": 2,
                    },
                    "apply": {
                        "type": "boolean",
                        "description": "Move files automatically. Default false.",
                        "default": False,
                    },
                },
            },
        ),
        Tool(
            name="cluster_note",
            description=(
                "Suggest a folder for a single note using semantic similarity against folder descriptions. "
                "Returns suggested_folder (primary, semantic), similarity score, reason "
                "('semantic' | 'type' | 'none'), and neighbor_vote (secondary wikilink-based vote for reference). "
                "Set apply=true to move the note to the semantic suggestion immediately."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "Note filename e.g. 'AuditTrail.md'",
                    },
                    "apply": {
                        "type": "boolean",
                        "description": "Move the note automatically. Default false.",
                        "default": False,
                    },
                },
                "required": ["note"],
            },
        ),
        Tool(
            name="get_connections",
            description=(
                "Get all notes connected to a given note via wikilinks. "
                "depth=1 returns direct links only. "
                "depth=2 returns connections of connections. "
                "Returns both outgoing and incoming links per depth level."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "Note name or path (e.g. 'AuditTrail' or 'knowledge/AuditTrail.md')",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "How many hops to traverse. Default 1, max recommended 3.",
                        "default": 1,
                        "minimum": 1,
                        "maximum": 3,
                    },
                },
                "required": ["note"],
            },
        ),
    ]


# Tools that read-modify-write files in the vault. move_notes rewrites [[wikilinks]]
# across every note, and log_note / cluster_* mutate on disk — two of those running
# concurrently would interleave rewrites and lose edits. This is a correctness lock, not
# a performance tweak: under stdio the transport serialized requests so it was
# unreachable, but the HTTP transport can have several calls in flight at once. Reads
# (search_vault, vault_status, get_connections) stay parallel.
#
# Scope is this process only. The watcher runs separately and is unaffected — that
# cross-process story is exactly as it was before HTTP existed.
_VAULT_WRITE_LOCK = threading.Lock()
_MUTATING_TOOLS = {"move_notes", "log_note", "cluster_vault", "cluster_note"}


# ── Server→client status notifications ──────────────────────────────────────
#
# During a long-running tool call the server can push human-readable status to the
# connected client over two standard channels: logging notifications
# (notifications/message — the client's log surface) and progress notifications
# (notifications/progress — rendered as request progress, only when the client
# supplied a progressToken for the in-flight request). These reach the client's
# UI/log surface, never the LLM's context.
#
# The dispatch runs in a worker thread (see call_tool), while the session's send
# methods are async and belong to the event loop. Notifier therefore schedules each
# send with asyncio.run_coroutine_threadsafe and never waits for it: calling .result()
# from the worker thread would deadlock if the loop is busy with this very tool call.
# Everything fails soft — a failed or unsent notification must never fail the tool
# call (same contract as the runtime heartbeat).

_LOG_LEVELS = [
    "debug",
    "info",
    "notice",
    "warning",
    "error",
    "critical",
    "alert",
    "emergency",
]

# Client-requested minimum via logging/setLevel. Best-effort by design: under
# stateless HTTP the module global cannot persist across requests, so a client that
# cares should also filter locally.
_min_log_level = "info"


def _level_allows(level: str) -> bool:
    try:
        return _LOG_LEVELS.index(level) >= _LOG_LEVELS.index(_min_log_level)
    except ValueError:
        return True


@app.set_logging_level()
async def _handle_set_logging_level(level: str) -> None:
    """Registering this handler is what makes get_capabilities() advertise
    LoggingCapability — there is no server_options flag for it."""
    global _min_log_level
    _min_log_level = level


class Notifier:
    """Status notifications for one in-flight tool call (see block comment above)."""

    active = True

    def __init__(
        self, session, loop, tool: str, request_id, progress_token=None
    ) -> None:
        self._session = session
        self._loop = loop
        self._tool = tool
        self._request_id = str(request_id)
        self._progress_token = progress_token

    def _schedule(self, send) -> None:
        try:
            fut = asyncio.run_coroutine_threadsafe(send(), self._loop)
        except RuntimeError:
            return  # loop already closed — nothing to notify, never raise

        def _report(fut):
            try:
                exc = fut.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                utils.log(f"[mcp] notification failed for {self._tool}: {exc}")

        fut.add_done_callback(_report)

    def log(self, data: str) -> None:
        if not _level_allows("info"):
            return
        self._schedule(
            lambda: self._session.send_log_message(
                level="info",
                data=data,
                logger=self._tool,
                related_request_id=self._request_id,
            )
        )

    def progress(
        self, progress: float, total: float | None, message: str | None = None
    ) -> None:
        if self._progress_token is None:
            return
        self._schedule(
            lambda: self._session.send_progress_notification(
                progress_token=self._progress_token,
                progress=progress,
                total=total,
                message=message,
                related_request_id=self._request_id,
            )
        )


class _NullNotifier:
    """No-op default. Used outside a request context (direct call_tool() calls — the
    existing dispatch tests) and by callers that pass no notifier; results are
    byte-identical to the pre-notification code."""

    active = False

    def log(self, data: str) -> None:
        pass

    def progress(
        self, progress: float, total: float | None, message: str | None = None
    ) -> None:
        pass


_NULL_NOTIFIER = _NullNotifier()


def _bridge_notifier(name: str):
    """Capture the request context on the event-loop side of the thread offload.

    app.request_context() raises LookupError outside a request — direct call_tool()
    calls get the null notifier and behave exactly as before."""
    try:
        ctx = request_ctx.get()
    except LookupError:
        return _NULL_NOTIFIER
    loop = asyncio.get_running_loop()
    meta = ctx.meta
    token = meta.progressToken if meta is not None else None
    return Notifier(ctx.session, loop, name, ctx.request_id, progress_token=token)


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Run the tool off the event loop.

    Every handler body below is synchronous and slow — search_vault embeds a query and
    hits ChromaDB, vault_status reads every note in the vault. Calling that directly
    from this coroutine blocks the event loop, which stdio hid (one client, one request
    at a time) and HTTP would not: one in-flight search would stall every other client
    and the protocol traffic with it.

    The request context (session, progress token) is captured here on the event loop
    and bridged into the worker thread as a Notifier, so long-running tools can push
    status to the client while they run.
    """
    notify = _bridge_notifier(name)
    return await anyio.to_thread.run_sync(
        lambda: _dispatch(name, arguments, notify=notify)
    )


def _dispatch(name: str, arguments: dict, notify=_NULL_NOTIFIER) -> list[TextContent]:
    if name in _MUTATING_TOOLS:
        with _VAULT_WRITE_LOCK:
            return _call(name, arguments, notify=notify)
    return _call(name, arguments, notify=notify)


def _call(name: str, arguments: dict, notify=_NULL_NOTIFIER) -> list[TextContent]:
    # Emission discipline: ≤ 2 logging messages per tool call phase. Progress
    # notifications are granular because only clients that opted in (progressToken)
    # receive them. All emissions live here, not in core modules — those stay
    # transport-agnostic.
    if name == "search_vault":
        notify.log(f"searching: '{arguments['query']}'")
        reranked = search_vault(
            query=arguments["query"],
            n_results=arguments.get("n_results", 3),
            min_score=arguments.get("min_score", 0.35),
            context_note=arguments.get("context_note"),
            type=arguments.get("type"),
            tags=arguments.get("tags"),
        )
        notify.log(f"{len(reranked)} results, re-ranked")
        return [TextContent(type="text", text=json.dumps(reranked, indent=2))]
    elif name == "vault_status":
        result = vault_status()
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    elif name == "move_notes":
        moves = arguments["moves"]
        notify.log(f"moving {len(moves)} files")
        if len(moves) > 1 and notify.active:
            # Live per-move progress. move_notes validates and rewrites wikilinks per
            # move anyway, so splitting the batch is behaviour-preserving; the only
            # difference is that prune_orphans runs per successful move instead of
            # once at the end, which is idempotent. With the null notifier the
            # original single batched call is kept exactly as before.
            result = {"moved": 0, "failed": 0, "succeeded": [], "errors": []}
            for i, move in enumerate(moves, 1):
                r = move_notes([move])
                result["moved"] += r["moved"]
                result["failed"] += r["failed"]
                result["succeeded"].extend(r["succeeded"])
                result["errors"].extend(r["errors"])
                notify.progress(
                    i, len(moves), f"{move.get('source')} → {move.get('destination')}"
                )
        else:
            result = move_notes(moves)
            for i, m in enumerate(result["succeeded"], 1):
                notify.progress(
                    i, len(result["succeeded"]), f"{m['source']} → {m['destination']}"
                )
        notify.log(f"moved {result['moved']}/{len(moves)}")
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    elif name == "log_note":
        from archiver_rag.vault.notes import log_note as _log_note

        result = _log_note(
            title=arguments["title"],
            content=arguments["content"],
            type=arguments.get("type", "note"),
            tags=arguments.get("tags"),
            related_notes=arguments.get("related_notes"),
        )
        notify.log(f"created {result.get('created', '')}")
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    elif name == "cluster_vault":
        from archiver_rag.graph.clustering import apply_clusters
        from archiver_rag.graph.clustering import cluster_vault as _cv

        result = _cv(min_cluster_size=int(arguments.get("min_cluster_size", 2)))
        if arguments.get("apply") and result["clusters"]:
            notify.log(f"analyzing {result['total_notes']} notes")
            if notify.active:
                # Same move-building rule as graph.clustering.apply_clusters, split
                # per move for live progress (see the move_notes branch). Duplicated
                # here — on the notifier-active path only — because apply_clusters
                # offers no per-move hook and must stay transport-agnostic.
                planned = []
                for cluster in result["clusters"]:
                    for note_path in cluster["notes"]:
                        if Path(note_path).parent.name == cluster["suggested_folder"]:
                            continue
                        planned.append(
                            (
                                note_path,
                                f"{cluster['suggested_folder']}/{Path(note_path).name}",
                            )
                        )
                result["moves"] = []
                for i, (src, dst) in enumerate(planned, 1):
                    result["moves"].append(
                        move_notes([{"source": src, "destination": dst}])
                    )
                    notify.progress(i, len(planned), f"moved {i}/{len(planned)}")
            else:
                result["moves"] = apply_clusters(result["clusters"])
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    elif name == "cluster_note":
        from archiver_rag.graph.clustering import cluster_note as _cn

        notify.log(f"placing '{arguments['note']}'")
        before_folder = _note_folder(arguments["note"])
        result = _cn(arguments["note"], apply=bool(arguments.get("apply", False)))
        suggested = result.get("suggested_folder")
        if arguments.get("apply") and suggested:
            # A move only happened if the note is in the suggested folder now and
            # was not there before — cluster_note's internal move_notes call is a
            # no-op failure when the note already sits in its suggested folder.
            after_folder = _note_folder(arguments["note"])
            if after_folder == suggested and before_folder != suggested:
                notify.progress(1, 1, f"moved to {suggested}")
                notify.log(f"moved {arguments['note']} → {suggested}")
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    elif name == "get_connections":
        result = get_connections(arguments["note"], arguments.get("depth", 1))
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    else:
        raise ValueError(f"Unknown tool: {name}")


def _note_folder(note_name: str) -> str | None:
    """Vault-relative folder the note currently sits in (None if not found) — the
    before/after moved check for cluster_note. cluster_note performs its move
    internally and does not report whether it happened, so verify on disk
    (full vault-relative folder compare, since suggested_folder can be a nested
    folder path)."""
    vault = Path(utils.get_vault_path())
    stem = Path(note_name).stem
    found = list(vault.rglob(f"{stem}.md"))
    if not found:
        return None
    return str(found[0].parent.relative_to(vault))


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
