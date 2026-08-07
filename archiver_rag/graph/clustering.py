from pathlib import Path
from collections import Counter
from archiver_rag.utils import get_vault_path
from archiver_rag.wikilinks import extract_wikilinks


def _build_adjacency(vault: Path) -> dict[str, set[str]]:
    real_notes = [
        n for n in vault.rglob("*.md")
        if not any(p.startswith(".") for p in n.parts)
    ]
    all_stems = {n.stem for n in real_notes}
    adjacency: dict[str, set[str]] = {stem: set() for stem in all_stems}

    for note in real_notes:
        try:
            content = note.read_text(encoding="utf-8", errors="ignore")
            stem = note.stem
            for link in extract_wikilinks(content):
                if link != stem and link in all_stems:
                    adjacency[stem].add(link)
                    adjacency[link].add(stem)
        except Exception:
            continue

    return adjacency


def _label_propagation(adjacency: dict[str, set[str]], max_iterations: int = 50) -> dict[str, str]:
    labels = {stem: stem for stem in adjacency}

    for _ in range(max_iterations):
        changed = False
        for stem in adjacency:
            neighbors = adjacency[stem]
            if not neighbors:
                continue
            neighbor_labels = [labels[n] for n in neighbors if n in labels]
            if not neighbor_labels:
                continue
            best = Counter(neighbor_labels).most_common(1)[0][0]
            if best != labels[stem]:
                labels[stem] = best
                changed = True
        if not changed:
            break

    return labels


def _name_community(stems: list[str], adjacency: dict[str, set[str]]) -> str:
    stem_set = set(stems)
    internal_degrees = {
        stem: len(adjacency.get(stem, set()) & stem_set)
        for stem in stems
    }
    return max(internal_degrees, key=lambda s: (internal_degrees[s], s))


def cluster_vault(min_cluster_size: int = 2) -> dict:
    vault = Path(get_vault_path())
    adjacency = _build_adjacency(vault)
    labels = _label_propagation(adjacency)

    communities: dict[str, list[str]] = {}
    for stem, label in labels.items():
        communities.setdefault(label, []).append(stem)

    clusters = []
    unclustered = []

    for members in communities.values():
        if len(members) < min_cluster_size:
            unclustered.extend(members)
            continue
        name = _name_community(members, adjacency)
        suggested_folder = name.lower().replace(" ", "-")
        notes = []
        for stem in sorted(members):
            found = list(vault.rglob(f"{stem}.md"))
            if found:
                notes.append(str(found[0].relative_to(vault)))
        clusters.append({
            "name": name,
            "size": len(members),
            "notes": notes,
            "suggested_folder": suggested_folder,
        })

    clusters.sort(key=lambda c: c["size"], reverse=True)

    return {
        "total_notes": len(adjacency),
        "total_clusters": len(clusters),
        "unclustered": sorted(unclustered),
        "clusters": clusters,
    }


def apply_clusters(clusters: list[dict]) -> list[dict]:
    from archiver_rag.vault.reorganize import move_notes
    moves = []
    for cluster in clusters:
        for note_path in cluster["notes"]:
            if Path(note_path).parent.name == cluster["suggested_folder"]:
                continue
            destination = f"{cluster['suggested_folder']}/{Path(note_path).name}"
            moves.append({"source": note_path, "destination": destination})
    if not moves:
        return []
    return [move_notes(moves)]


def cluster_note(note_name: str) -> dict:
    vault = Path(get_vault_path())
    adjacency = _build_adjacency(vault)
    stem = Path(note_name).stem

    if stem not in adjacency:
        return {"note": note_name, "suggested_folder": None, "votes": 0,
                "total_neighbors": 0, "reason": "Note not found in vault"}

    neighbors = adjacency[stem]
    if not neighbors:
        return {"note": note_name, "suggested_folder": None, "votes": 0,
                "total_neighbors": 0, "reason": "Note has no wikilink neighbors"}

    folder_votes: list[str] = []
    for neighbor in neighbors:
        found = list(vault.rglob(f"{neighbor}.md"))
        if found:
            folder = found[0].parent
            if folder != vault:
                folder_votes.append(folder.name)

    if not folder_votes:
        return {"note": note_name, "suggested_folder": None, "votes": 0,
                "total_neighbors": len(neighbors),
                "reason": "All neighbors are in the vault root — no folder to suggest"}

    best_folder, votes = Counter(folder_votes).most_common(1)[0]
    return {
        "note": note_name,
        "suggested_folder": best_folder,
        "votes": votes,
        "total_neighbors": len(neighbors),
        "reason": f"{votes}/{len(neighbors)} neighbors are in '{best_folder}'",
    }
