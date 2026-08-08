from pathlib import Path
import time
import sys
import signal
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from archiver_rag.core.ingest import ingest_file
from archiver_rag.core.db import collection
from archiver_rag.utils import get_vault_path, is_hidden_path
import os

from archiver_rag.graph.linker import auto_link

_new_notes_since_cluster = 0

# How long to wait for a "deleted" file to reappear before believing the delete.
DELETE_SETTLE_SECONDS = 1.0


def _is_spurious_delete(path: Path, settle: float = DELETE_SETTLE_SECONDS) -> bool:
    """True if `path` exists or comes back within `settle` seconds — a save, not a delete.

    Editors (and Claude Code's Edit tool) save atomically: write a temp file, then
    os.replace() it over the original. watchdog reports that as modified→deleted with
    the delete arriving *last*, after the file is already back on disk. Acting on it
    evicted the note from ChromaDB on every single save, and would now also sweep live
    wikilinks to it out of unrelated notes — damage the follow-up create never undoes.

    Returns immediately in the common case (file already back), so a normal save pays
    nothing. Only a genuine delete waits out the full settle window.
    """
    if path.exists():
        return True
    deadline = time.monotonic() + settle
    while time.monotonic() < deadline:
        time.sleep(0.05)
        if path.exists():
            return True
    return False


def _get_cluster_config() -> tuple[bool, int]:
    try:
        import json
        config_path = Path.home() / ".archiver-rag" / "config.json"
        config = json.loads(config_path.read_text())
        return config.get("auto_cluster", True), int(config.get("cluster_threshold", 5))
    except Exception:
        return False, 5


class VaultHandler(FileSystemEventHandler):
    def on_created(self, event):
        global _new_notes_since_cluster
        path = str(event.src_path)
        if event.is_directory or not path.endswith(".md") or is_hidden_path(Path(path)):
            return
        print(f"New file detected: {path}")
        ingest_file(path)
        auto_link(path)

        auto_cluster, threshold = _get_cluster_config()
        if auto_cluster:
            from archiver_rag.graph.clustering import cluster_note, cluster_vault, apply_clusters
            from archiver_rag.vault.reorganize import move_notes
            from archiver_rag.utils import get_vault_path
            suggestion = cluster_note(Path(path).name)
            if suggestion.get("suggested_folder"):
                vault = Path(get_vault_path())
                src = str(Path(path).relative_to(vault))
                dst = f"{suggestion['suggested_folder']}/{Path(path).name}"
                move_notes([{"source": src, "destination": dst}])
                print(f"Auto-placed {Path(path).name} → {suggestion['suggested_folder']}/")
            else:
                _new_notes_since_cluster += 1
                if _new_notes_since_cluster >= threshold:
                    _new_notes_since_cluster = 0
                    print("Auto-clustering vault...")
                    result = cluster_vault(min_cluster_size=2)
                    if result["clusters"]:
                        apply_clusters(result["clusters"])
                        print(f"Clustered into {result['total_clusters']} groups")

    def on_modified(self, event):
        path = str(event.src_path)
        if event.is_directory or not path.endswith(".md") or is_hidden_path(Path(path)):
            return
        print(f"File modified: {event.src_path}")
        ingest_file(path)
        auto_link(path)

    def on_deleted(self, event):
        path = str(event.src_path)
        if event.is_directory or not path.endswith(".md") or is_hidden_path(Path(path)):
            return
        if _is_spurious_delete(Path(path)):
            return
        vault_path = get_vault_path()
        try:
            source = str(Path(path).relative_to(vault_path))
        except ValueError:
            source = Path(path).name
        print(f"File deleted: {source}")
        collection.delete(where={"source": source})
        from archiver_rag.vault.notes import sweep_dead_links
        sweep_result = sweep_dead_links(Path(vault_path), [Path(path).stem])
        if sweep_result["swept"]:
            print(f"Swept links in: {', '.join(sweep_result['swept'])}")

    def on_moved(self, event):
        """Handle renames, moves, and — critically — atomic saves.

        Editors do not write notes in place. They write `<name>.md.tmp.<rand>` and
        rename it over the target, so the ONLY event naming the real file is this
        move, with a source that is not a `.md` at all. Keying off `src` alone
        silently dropped every such save: the note never reached the index.
        Both ends must be considered independently.
        """
        src = str(event.src_path)
        dst = str(event.dest_path)
        if event.is_directory:
            return

        src_is_note = src.endswith(".md") and not is_hidden_path(Path(src))
        dst_is_note = dst.endswith(".md") and not is_hidden_path(Path(dst))
        if not src_is_note and not dst_is_note:
            return

        vault_path = get_vault_path()

        # The note left its old location — drop the stale index entry.
        if src_is_note:
            try:
                old_source = str(Path(src).relative_to(vault_path))
            except ValueError:
                old_source = Path(src).name
            collection.delete(where={"source": old_source})
            print(f"File renamed/moved: {old_source} → {dst}")

        if dst_is_note:
            ingest_file(dst)
            auto_link(dst)
            # Rewrite [[wikilinks]] that pointed at the old stem. Only when the
            # filename changed — same-folder moves are stem-equal, and an atomic
            # save has no meaningful old stem (src is the temp file).
            if src_is_note:
                old_stem = Path(src).stem
                new_stem = Path(dst).stem
                if old_stem != new_stem:
                    from archiver_rag.vault.reorganize import _update_wikilinks
                    _update_wikilinks(Path(vault_path), old_stem, new_stem)
            return

        # A note moved out of note-space entirely — into .trash/ (how Obsidian
        # deletes) or to a non-.md name. Treat it as a deletion and sweep the
        # wikilinks that now point nowhere.
        from archiver_rag.vault.notes import sweep_dead_links
        sweep_result = sweep_dead_links(Path(vault_path), [Path(src).stem])
        if sweep_result["swept"]:
            print(f"Swept links in: {', '.join(sweep_result['swept'])}")

def watch(vault_path: str):
    """Called by the service via `archiver-rag _watch`"""
    import time
    import signal
    from watchdog.observers import Observer

    handler = VaultHandler()
    observer = Observer()
    observer.schedule(handler, vault_path, recursive=True)
    observer.start()

    def shutdown(signum, frame):
        observer.stop()
        observer.join()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"Watching vault at {vault_path}...")
    while observer.is_alive():
        time.sleep(1)

if __name__ == "__main__":
    import sys
    watch(sys.argv[1])
