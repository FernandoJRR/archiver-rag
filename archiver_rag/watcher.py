from pathlib import Path
import time
import sys
import signal
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from archiver_rag.core.ingest import ingest_file
from archiver_rag.core.db import collection
from archiver_rag.utils import get_vault_path, is_hidden_path, is_folder_note, is_indexable_note, log as _log
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


def _get_cluster_config() -> tuple[bool, int, float, bool]:
    """Return (auto_cluster, threshold, placement_similarity_threshold, type_fallback).

    IMPORTANT: Must NOT be refactored through load_config() — load_config() returns {} on
    error, and config.get("auto_cluster", True) would be True, which starts moving files
    on a corrupt config. The except path here returns auto_cluster=False (do nothing).
    """
    try:
        import json

        config_path = Path.home() / ".archiver-rag" / "config.json"
        config = json.loads(config_path.read_text())
        return (
            config.get("auto_cluster", True),
            int(config.get("cluster_threshold", 5)),
            float(config.get("placement_similarity_threshold", 0.55)),
            bool(config.get("type_fallback", True)),
        )
    except Exception:
        return False, 5, 0.55, True


def _folder_note_rel_folder(vault_path: str, folder_note_path: str) -> str | None:
    """vault-relative parent of a _folder.md path, or None if outside the vault."""
    try:
        rel = str(Path(folder_note_path).parent.relative_to(vault_path))
        return "." if rel == "" else rel
    except ValueError:
        return None


def _refresh_folder_centroid(path: str) -> None:
    """Recompute and cache the centroid for the folder whose _folder.md just changed.

    Does NOT call ingest_file or auto_link — those would put _folder.md chunks in
    ChromaDB, which prune_orphans cannot clean up because the file exists on disk.
    The only write is to ~/.archiver-rag/centroids.json, outside the vault, generating
    no watchdog event and no re-entrancy risk.
    """
    vault_path = get_vault_path()
    rel_folder = _folder_note_rel_folder(vault_path, path)
    if rel_folder is None:
        return
    from archiver_rag.graph.centroids import refresh_centroid
    changed = refresh_centroid(Path(vault_path), rel_folder)
    if changed:
        _log(f"Folder description changed: {rel_folder}/ → centroid recomputed")


class VaultHandler(FileSystemEventHandler):
    def _maybe_cluster(self, path: str) -> None:
        """Auto-place a newly created note, or re-cluster once enough have accumulated.

        Only ever call this for genuinely NEW notes. It moves files, and `on_moved`
        fires on every save, so running it on edits would relocate notes as you type.

        Stage B: placement is now by cosine similarity against declared folder descriptions
        (graph.placement.suggest_folder), not by wikilink-neighbour vote. The is_new_note
        gate and the accumulator → cluster_vault fallback are unchanged.
        """
        global _new_notes_since_cluster
        auto_cluster, cluster_threshold, sim_threshold, type_fallback = _get_cluster_config()
        if not auto_cluster:
            return

        from archiver_rag.graph.placement import suggest_folder
        from archiver_rag.graph.clustering import cluster_vault, apply_clusters
        from archiver_rag.vault.reorganize import move_notes

        vault = Path(get_vault_path())
        note_path = Path(path)
        suggestion = suggest_folder(
            vault,
            note_path,
            threshold=sim_threshold,
            type_fallback=type_fallback,
        )
        target = suggestion.get("suggested_folder")
        if target:
            # Anti-churn: compare the full vault-relative parent path, not just the
            # immediate directory name. A note in "Projects/Weekly" whose target is
            # "Weekly" must NOT be skipped by a name-only match — and conversely, a note
            # already in its target folder must not be re-moved. Use "." for vault root.
            try:
                current_rel_parent = str(note_path.parent.relative_to(vault))
            except ValueError:
                return
            if current_rel_parent == target or (current_rel_parent == "." and target == "."):
                return

            try:
                src = str(note_path.relative_to(vault))
            except ValueError:
                return

            # If the destination folder does not exist yet, write its _folder.md first
            # (§6 "Carpetas nuevas") so it is not born orphaned. move_notes handles mkdir.
            dest_dir = vault / target
            if not dest_dir.exists():
                from archiver_rag.vault.folder_notes import FolderNote, write_folder_note
                from datetime import date
                new_sidecar = FolderNote(
                    rel_folder=target,
                    description_terms=[],
                    note_count=0,
                    updated=date.today().isoformat(),
                    source="auto",
                )
                write_folder_note(vault, new_sidecar)

            result = move_notes(
                [{"source": src, "destination": f"{target}/{note_path.name}"}]
            )
            if result.get("moved"):
                reason = suggestion.get("reason", "")
                sim = suggestion.get("similarity", 0.0)
                _log(
                    f"Auto-placed {note_path.name} → {target}/ "
                    f"({reason}, {sim:.2f})"
                )
            return

        _new_notes_since_cluster += 1
        if _new_notes_since_cluster >= cluster_threshold:
            _new_notes_since_cluster = 0
            _log("Auto-clustering vault...")
            result = cluster_vault(min_cluster_size=2)
            if result["clusters"]:
                apply_clusters(result["clusters"])
                _log(f"Clustered into {result['total_clusters']} groups")

    def on_created(self, event):
        path = str(event.src_path)
        if event.is_directory:
            return
        p = Path(path)
        if is_folder_note(p) and not is_hidden_path(p):
            _refresh_folder_centroid(path)
            return
        if not is_indexable_note(p):
            return
        _log(f"New file detected: {path}")
        ingest_file(path)
        auto_link(path)
        self._maybe_cluster(path)

    def on_modified(self, event):
        path = str(event.src_path)
        if event.is_directory:
            return
        p = Path(path)
        if is_folder_note(p) and not is_hidden_path(p):
            _refresh_folder_centroid(path)
            return
        if not is_indexable_note(p):
            return
        _log(f"File modified: {path}")
        ingest_file(path)
        auto_link(path)

    def on_deleted(self, event):
        path = str(event.src_path)
        if event.is_directory:
            return
        p = Path(path)
        if is_folder_note(p) and not is_hidden_path(p):
            # Spurious-delete guard applies to sidecars too
            if _is_spurious_delete(p):
                return
            vault_path = get_vault_path()
            rel_folder = _folder_note_rel_folder(vault_path, path)
            if rel_folder:
                from archiver_rag.graph.centroids import drop_centroid
                if drop_centroid(rel_folder):
                    _log(f"Folder sidecar deleted: {rel_folder}/ → centroid dropped")
            return
        if not is_indexable_note(p):
            return
        if _is_spurious_delete(p):
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

        The same atomic-save pattern applies to _folder.md: the tmp→sidecar rename
        arrives here, not in on_modified. So _folder.md changes land in this branch.
        """
        src = str(event.src_path)
        dst = str(event.dest_path)
        if event.is_directory:
            return

        src_p = Path(src)
        dst_p = Path(dst)

        # ── _folder.md handling ──────────────────────────────────────────────
        src_is_folder_note = is_folder_note(src_p) and not is_hidden_path(src_p)
        dst_is_folder_note = is_folder_note(dst_p) and not is_hidden_path(dst_p)

        if dst_is_folder_note:
            # Atomic save of _folder.md: tmp → sidecar — refresh centroid
            _refresh_folder_centroid(dst)
        elif src_is_folder_note:
            # Sidecar moved out of its folder or renamed away — drop centroid
            vault_path = get_vault_path()
            rel_folder = _folder_note_rel_folder(vault_path, src)
            if rel_folder:
                from archiver_rag.graph.centroids import drop_centroid
                if drop_centroid(rel_folder):
                    _log(f"Folder sidecar moved: {rel_folder}/ → centroid dropped")
        # ── end _folder.md handling ──────────────────────────────────────────

        src_is_note = is_indexable_note(src_p)
        dst_is_note = is_indexable_note(dst_p)
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
