from pathlib import Path
import time
import sys
import signal
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from archiver_rag.core.ingest import ingest_file
from archiver_rag.core.db import collection
from archiver_rag.utils import get_vault_path, is_hidden_path, log as _log
import os

from archiver_rag.graph.linker import auto_link

_new_notes_since_cluster = 0

# How long to wait for a "deleted" file to reappear before believing the delete.
DELETE_SETTLE_SECONDS = 1.0


def _is_indexed(source: str) -> bool:
    """True if ChromaDB already holds chunks for this vault-relative source.

    Used to tell a brand-new note from a save of an existing one. Errors resolve to
    True: clustering moves files, so uncertainty must never trigger it.
    """
    try:
        return bool(collection.get(where={"source": source}, limit=1).get("ids"))
    except Exception:
        return True


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
    def _maybe_cluster(self, path: str) -> None:
        """Auto-place a newly created note, or re-cluster once enough have accumulated.

        Only ever call this for genuinely NEW notes. It moves files, and `on_moved`
        fires on every save, so running it on edits would relocate notes as you type.
        """
        global _new_notes_since_cluster
        auto_cluster, threshold = _get_cluster_config()
        if not auto_cluster:
            return

        from archiver_rag.graph.clustering import cluster_note, cluster_vault, apply_clusters
        from archiver_rag.vault.reorganize import move_notes

        suggestion = cluster_note(Path(path).name)
        target = suggestion.get("suggested_folder")
        if target:
            # Already where clustering wants it. Without this the handler kept
            # re-issuing a move onto the note's own path — move_notes rejects it as
            # "destination already exists", but each attempt still logged a bogus
            # "Auto-placed" and re-triggered ingest + auto_link.
            if Path(path).parent.name == target:
                return
            vault = Path(get_vault_path())
            try:
                src = str(Path(path).relative_to(vault))
            except ValueError:
                return
            result = move_notes([{"source": src, "destination": f"{target}/{Path(path).name}"}])
            if result.get("moved"):
                _log(f"Auto-placed {Path(path).name} → {target}/")
            return

        _new_notes_since_cluster += 1
        if _new_notes_since_cluster >= threshold:
            _new_notes_since_cluster = 0
            _log("Auto-clustering vault...")
            result = cluster_vault(min_cluster_size=2)
            if result["clusters"]:
                apply_clusters(result["clusters"])
                _log(f"Clustered into {result['total_clusters']} groups")

    def on_created(self, event):
        path = str(event.src_path)
        if event.is_directory or not path.endswith(".md") or is_hidden_path(Path(path)):
            return
        _log(f"New file detected: {path}")
        ingest_file(path)
        auto_link(path)
        self._maybe_cluster(path)

    def on_modified(self, event):
        path = str(event.src_path)
        if event.is_directory or not path.endswith(".md") or is_hidden_path(Path(path)):
            return
        _log(f"File modified: {path}")
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
        _log(f"File deleted: {source}")
        collection.delete(where={"source": source})
        from archiver_rag.vault.notes import sweep_dead_links
        sweep_result = sweep_dead_links(Path(vault_path), [Path(path).stem])
        if sweep_result["swept"]:
            _log(f"Swept links in: {', '.join(sweep_result['swept'])}")

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
            _log(f"File renamed/moved: {old_source} → {dst}")

        if dst_is_note:
            # Decide BEFORE ingesting, or the note we are about to index would always
            # look pre-existing. An unindexed destination reached via a non-note source
            # is a brand-new note written atomically — the only case that should
            # cluster. Renames and ordinary saves must not move files.
            try:
                dst_source = str(Path(dst).relative_to(vault_path))
            except ValueError:
                dst_source = Path(dst).name
            is_new_note = not src_is_note and not _is_indexed(dst_source)

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
            if is_new_note:
                self._maybe_cluster(dst)
            return

        # A note moved out of note-space entirely — into .trash/ (how Obsidian
        # deletes) or to a non-.md name. Treat it as a deletion and sweep the
        # wikilinks that now point nowhere.
        from archiver_rag.vault.notes import sweep_dead_links
        sweep_result = sweep_dead_links(Path(vault_path), [Path(src).stem])
        if sweep_result["swept"]:
            _log(f"Swept links in: {', '.join(sweep_result['swept'])}")

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

    _log(f"Watching vault at {vault_path}...")
    while observer.is_alive():
        time.sleep(1)

if __name__ == "__main__":
    import sys
    watch(sys.argv[1])
