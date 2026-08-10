from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from pathlib import Path
import json
import asyncio

from archiver_rag.core.search import search_vault
from archiver_rag.graph.connections import get_connections
from archiver_rag.vault.reorganize import move_notes
from archiver_rag.vault.health import vault_status

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


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "search_vault":
        reranked = search_vault(
            query=arguments["query"],
            n_results=arguments.get("n_results", 3),
            min_score=arguments.get("min_score", 0.35),
            context_note=arguments.get("context_note"),
            type=arguments.get("type"),
            tags=arguments.get("tags"),
        )
        return [TextContent(type="text", text=json.dumps(reranked, indent=2))]
    elif name == "vault_status":
        result = vault_status()
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    elif name == "move_notes":
        result = move_notes(
            arguments["moves"],
        )
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
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    elif name == "cluster_vault":
        from archiver_rag.graph.clustering import cluster_vault as _cv, apply_clusters

        result = _cv(min_cluster_size=int(arguments.get("min_cluster_size", 2)))
        if arguments.get("apply") and result["clusters"]:
            result["moves"] = apply_clusters(result["clusters"])
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    elif name == "cluster_note":
        from archiver_rag.graph.clustering import cluster_note as _cn

        result = _cn(arguments["note"], apply=bool(arguments.get("apply", False)))
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    elif name == "get_connections":
        result = get_connections(arguments["note"], arguments.get("depth", 1))
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    else:
        raise ValueError(f"Unknown tool: {name}")


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
