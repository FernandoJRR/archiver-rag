from pathlib import Path
from archiver_rag.utils import get_vault_path, build_link_map


def get_connections(note: str, depth: int = 1) -> dict:
    vault = Path(get_vault_path())
    outgoing_map, incoming_map = build_link_map(vault)

    # Normalize — strip .md extension if passed
    note_stem = Path(note).stem

    result = {
        "note": note_stem,
        "depth": depth,
        "connections": {},
        "all_connected": set(),
    }

    # BFS traversal up to depth
    current_layer = {note_stem}

    for d in range(1, depth + 1):
        next_layer = set()
        layer_out = set()
        layer_in = set()

        for stem in current_layer:
            for target in outgoing_map.get(stem, []):
                if target != note_stem:  # exclude the root note
                    layer_out.add(target)
                    next_layer.add(target)

            for source in incoming_map.get(stem, []):
                if source != note_stem:
                    layer_in.add(source)
                    next_layer.add(source)

        label = "direct" if d == 1 else f"depth_{d}"
        result["connections"][label] = {
            "outgoing": sorted(layer_out),
            "incoming": sorted(layer_in),
        }

        result["all_connected"].update(layer_out)
        result["all_connected"].update(layer_in)

        # Next layer excludes already visited nodes
        current_layer = next_layer - result["all_connected"]

    # Remove root note from all_connected if it crept in
    result["all_connected"].discard(note_stem)
    result["all_connected"] = sorted(result["all_connected"])

    return result
