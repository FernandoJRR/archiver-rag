import re
from pathlib import Path
from archiver_rag.utils import get_vault_path
from archiver_rag.wikilinks import extract_wikilinks

def vault_status() -> dict:
    vault = Path(get_vault_path())
    all_notes = [
        f for f in vault.rglob("*.md")
        if not any(p.startswith(".") for p in f.parts)
    ]
    all_folders = [
        d for d in vault.rglob("*")
        if d.is_dir() and not any(p.startswith(".") for p in d.parts)
    ]

    all_stems = {f.stem for f in all_notes}

    link_map = {}        # note stem → list of stems it links to
    incoming = {}        # note stem → count of notes linking to it
    no_frontmatter = []
    empty_notes = []
    broken_links = []

    for note in all_notes:
        rel = str(note.relative_to(vault))
        content = note.read_text(encoding="utf-8", errors="ignore")

        # Empty notes
        if not content.strip():
            empty_notes.append(rel)
            continue

        # Missing frontmatter
        if not content.startswith("---"):
            no_frontmatter.append(rel)

        # Parse wikilinks
        links = extract_wikilinks(content)
        link_map[note.stem] = links

        for link in links:
            incoming[link] = incoming.get(link, 0) + 1
            # Check for broken links
            if link not in all_stems:
                broken_links.append(f"{rel} → [[{link}]]")

    # Orphaned notes — no incoming links
    orphaned = [
        str(n.relative_to(vault))
        for n in all_notes
        if n.stem not in incoming
    ]

    # Tags
    tag_pattern = re.compile(r'(?:^tags:\s*\[([^\]]+)\]|#([\w/-]+))', re.MULTILINE)
    tag_counts = {}
    for note in all_notes:
        content = note.read_text(encoding="utf-8", errors="ignore")
        for match in tag_pattern.finditer(content):
            if match.group(1):  # frontmatter tags
                for tag in match.group(1).split(","):
                    t = tag.strip().strip('"').strip("'")
                    if t:
                        tag_counts[t] = tag_counts.get(t, 0) + 1
            elif match.group(2):  # inline #tags
                t = match.group(2)
                tag_counts[t] = tag_counts.get(t, 0) + 1

    most_used = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    # Recent activity
    sorted_by_modified = sorted(all_notes, key=lambda f: f.stat().st_mtime, reverse=True)
    sorted_by_created = sorted(all_notes, key=lambda f: f.stat().st_ctime, reverse=True)

    return {
        "structure": {
            "total_notes": len(all_notes),
            "total_folders": len(all_folders),
            "folders": [
                str(d.relative_to(vault))
                for d in sorted(all_folders)
            ]
        },
        "health": {
            "orphaned_notes": orphaned[:20],
            "no_frontmatter": no_frontmatter[:20],
            "empty_notes": empty_notes[:20],
            "broken_links": broken_links[:20]
        },
        "tags": {
            "most_used": most_used,
            "total_unique": len(tag_counts)
        },
        "recent": {
            "modified": [
                str(f.relative_to(vault))
                for f in sorted_by_modified[:5]
            ],
            "created": [
                str(f.relative_to(vault))
                for f in sorted_by_created[:5]
            ]
        }
    }
