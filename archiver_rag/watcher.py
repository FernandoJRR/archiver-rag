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
from archiver_rag import runtime

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
        from archiver_rag import paths

        config = json.loads(paths.config_path().read_text())
        return (
            config.get("auto_cluster", True),
            int(config.get("cluster_threshold", 5)),
            float(config.get("placement_similarity_threshold", 0.55)),
            bool(config.get("type_fallback", True)),
        )
    except Exception:
        return False, 5, 0.55, True


def _get_placement_weights_config() -> tuple[float, float, float]:
    """Return (w_identity, w_content, name_prefix_bonus) for suggest_folder.

    Separate reader rather than extending _get_cluster_config's tuple — several
    tests monkeypatch _get_cluster_config with a fixed-arity lambda, and changing
    its return shape would silently break every one of them. Same safe-default
    contract as the other config readers here: any read error resolves to the
    documented defaults (0.6/0.4/0.15), never a crash.
    """
    try:
        import json
        from archiver_rag import paths

        config = json.loads(paths.config_path().read_text())
        advanced = config.get("advanced", {})
        weights = advanced.get("placement_weights", {})
        return (
            float(weights.get("identity", 0.6)),
            float(weights.get("content", 0.4)),
            float(advanced.get("name_prefix_bonus", 0.15)),
        )
    except Exception:
        return 0.6, 0.4, 0.15


def _get_describe_config() -> tuple[bool, int, int, float, float, bool]:
    """Return (auto_describe, term_extraction_min_notes, max_terms, mmr_lambda, alpha_scale,
    tag_terms_in_description).

    Same safety contract as _get_cluster_config: must NOT go through load_config(),
    whose {} error-return would make config.get("auto_describe", True) default to True.
    The except path here returns auto_describe=False (do nothing) — uncertainty must
    never turn on a behavior that rewrites _folder.md files.
    """
    try:
        import json
        from archiver_rag import paths

        config = json.loads(paths.config_path().read_text())
        advanced = config.get("advanced", {})
        return (
            bool(config.get("auto_describe", False)),
            int(advanced.get("term_extraction_min_notes", 4)),
            int(advanced.get("max_terms", 6)),
            float(advanced.get("mmr_lambda", 0.5)),
            float(advanced.get("alpha_curve", {}).get("scale", 1.0)),
            bool(advanced.get("tag_terms_in_description", True)),
        )
    except Exception:
        return False, 4, 6, 0.5, 1.0, True


def _get_folder_vacancy_grace_periods() -> int:
    """Gate 1 vaciado — consecutive empty structural-change checks before a
    `source: auto` _folder.md is archived. Same safe-default contract as the other
    readers here: any read error resolves to the documented default (3), never a crash.
    Not gated separately — piggybacks on _maybe_redescribe's own auto_describe check.
    """
    try:
        import json
        from archiver_rag import paths

        config = json.loads(paths.config_path().read_text())
        return int(config.get("advanced", {}).get("folder_vacancy_grace_periods", 3))
    except Exception:
        return 3


def _get_inbox_config() -> tuple[bool, int, float]:
    """Return (auto_inbox, inbox_min_cluster_size, inbox_similarity_threshold).

    Gate 2 (folder-lifecycle-splits-from-autonaming-two-gates-and-inbox-clustering-hole):
    notes with no semantic match and no type: fallback route to inbox/ and get grouped
    by embedding similarity (graph/inbox.py) — never by wikilink-graph topology, per
    the incident that gated this feature in the first place.

    Same safety contract as _get_cluster_config/_get_describe_config: must NOT go
    through load_config(), whose {} error-return would make config.get("auto_inbox",
    True) default to True. The except path returns auto_inbox=False (do nothing) —
    uncertainty must never turn on a behavior that moves notes into a staging folder
    and creates new folders from it. Both numeric defaults are placeholders, not
    empirically validated — same status as inbox_min_cluster_size in the vault spec
    and folder_vacancy_grace_periods when it first shipped.
    """
    try:
        import json
        from archiver_rag import paths

        config = json.loads(paths.config_path().read_text())
        advanced = config.get("advanced", {})
        return (
            bool(config.get("auto_inbox", False)),
            int(advanced.get("inbox_min_cluster_size", 3)),
            float(advanced.get("inbox_similarity_threshold", 0.5)),
        )
    except Exception:
        return False, 3, 0.5


def _ensure_inbox_locked(vault: Path) -> None:
    """Write inbox/_folder.md as source: manual with empty terms, once.

    Reuses the exact mechanism already used to permanently lock decision/gotcha/
    lesson/pattern/reference out of placement (CLAUDE.md, "type-folders locked
    description-less"): apply_extracted_terms never touches a source: manual folder
    regardless of its terms, and folder_centroids() skips any folder whose
    description_text() is empty. inbox/ is therefore simultaneously immune to
    auto-description AND absent from placement candidacy, with zero changes to either
    of those functions. Without this, _maybe_redescribe("inbox") — called
    unconditionally by on_created/on_moved on whatever folder a note lands in — would
    give inbox/ a real auto description the first time auto_describe runs, and it
    would start competing as an ordinary placement destination: backwards, since
    inbox is a staging net, not a topical folder.
    """
    from archiver_rag.vault.folder_notes import FolderNote, read_folder_note, write_folder_note

    if read_folder_note(vault, "inbox") is None:
        write_folder_note(
            vault, FolderNote(rel_folder="inbox", source="manual", description_terms=[])
        )


# Recovery fix (folder collapse incident): a batch move (e.g. `place --all --apply`,
# or a manual `archiver-rag cluster --apply`) fires one on_moved per note, and every one
# of them redescribes its destination folder — N notes landing in the same folder meant
# N back-to-back corpus-rebuild+MMR passes on that folder, and _maybe_cluster's own
# freshly-created-folder path was one such repeat-move source. Not what caused the
# collapse (that was cluster_vault's community naming, removed above), but real waste
# this guards against: at most one regeneration per folder per debounce window.
_REDESCRIBE_DEBOUNCE_SECONDS = 5.0
_last_redescribed: dict[str, float] = {}


def _maybe_redescribe(rel_folder: str) -> None:
    """Regenerate rel_folder's _folder.md when its note membership changed.

    Structural changes only (note created/deleted/moved in or out) — never called from
    on_modified, since that fires per keystroke-batch save with no debouncing and this
    does real work (corpus rebuild + MMR). Debounced per rel_folder (see
    _REDESCRIBE_DEBOUNCE_SECONDS above) so a batch of moves into the same folder produces
    at most one regeneration per window instead of one per note. write_folder_note's
    resulting event is absorbed by _refresh_folder_centroid's own fingerprint no-op — no
    re-entrancy into note-space (see module docstring for write_folder_note's
    re-entrancy analysis).
    """
    auto_describe, min_notes, max_terms, mmr_lambda, alpha_scale, tag_terms_in_description = (
        _get_describe_config()
    )
    if not auto_describe or rel_folder == ".":
        return

    now = time.monotonic()
    last = _last_redescribed.get(rel_folder)
    if last is not None and now - last < _REDESCRIBE_DEBOUNCE_SECONDS:
        return
    _last_redescribed[rel_folder] = now

    vault = Path(get_vault_path())
    folder_dir = vault / rel_folder
    if not folder_dir.is_dir():
        return
    if not any(f.is_file() and is_indexable_note(f) for f in folder_dir.iterdir()):
        _maybe_archive_if_empty(vault, rel_folder)
        return

    from archiver_rag.graph.terms import extract_terms
    from archiver_rag.vault.folder_notes import apply_extracted_terms

    desc, dist = extract_terms(
        vault, rel_folder,
        term_extraction_min_notes=min_notes,
        max_terms=max_terms,
        mmr_lambda=mmr_lambda,
        tag_terms_in_description=tag_terms_in_description,
    )
    result = apply_extracted_terms(
        vault, rel_folder, desc, dist,
        alpha_scale=alpha_scale, max_terms=max_terms,
    )
    if result["action"] == "skipped_manual":
        return
    runtime.record_event("described", rel_folder, counter="described")
    _log(f"Auto-described {rel_folder}/ → {result['action']} ({desc[:4]})")
    if result["gravity_well_warning"]:
        note = result["folder_note"]
        _log(
            f"  ⚠️ gravity-well forming: {rel_folder} "
            f"(count → {note.note_count}, α={result['alpha']:.2f})"
        )


def _maybe_archive_if_empty(vault: Path, rel_folder: str) -> None:
    """Gate 1 vaciado — called by _maybe_redescribe when rel_folder has 0 notes.

    source: manual folders are never touched (per spec: a declared description stays a
    valid magnet even empty — not even counted). source: auto folders accumulate
    empty_sweeps across structural-change checks (same debounce as the caller, so this
    is roughly one increment per _REDESCRIBE_DEBOUNCE_SECONDS window) and are archived
    to .archive/ once the configured grace period is reached.
    """
    from archiver_rag.vault.folder_notes import (
        archive_folder_note,
        read_folder_note,
        write_folder_note,
    )

    note = read_folder_note(vault, rel_folder)
    if note is None or note.source == "manual":
        return

    note.empty_sweeps += 1
    grace_periods = _get_folder_vacancy_grace_periods()
    if note.empty_sweeps >= grace_periods:
        archive_folder_note(vault, rel_folder)
        from archiver_rag.graph.centroids import drop_centroid

        drop_centroid(rel_folder)
        _log(f"Archived {rel_folder}/_folder.md → .archive/ (empty {note.empty_sweeps} checks in a row)")
    else:
        write_folder_note(vault, note)


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
    The only write is to the XDG data dir's centroids.json (see paths.py), outside the
    vault, generating no watchdog event and no re-entrancy risk.
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
    def _maybe_cluster(self, path: str) -> str | None:
        """Auto-place a newly created note, or re-cluster once enough have accumulated.

        Only ever call this for genuinely NEW notes. It moves files, and `on_moved`
        fires on every save, so running it on edits would relocate notes as you type.

        Stage B: placement is now by cosine similarity against declared folder descriptions
        (graph.placement.suggest_folder), not by wikilink-neighbour vote. The is_new_note
        gate is unchanged.

        The label-propagation cluster_vault() fallback that used to fire automatically
        here (after cluster_threshold notes in a row got no semantic suggestion) has been
        removed. cluster_vault runs on the whole-vault wikilink graph and names each
        community after its most internally-connected note — on this vault's dense
        auto-linked graph it collapsed 61/73 notes into two note-stem-named folders
        (archiver-rag-sync-command/, spec---wikilink-resolver-.../) across a handful of
        automatic re-cluster passes. Semantic placement (suggest_folder) and cluster_vault
        must not both run as automatic signals — cluster_vault remains available as an
        explicit manual action via `archiver-rag cluster` (cli.py), which the user runs
        deliberately and reviews before --apply.

        Returns the rel_folder the note was actually moved into, or None if no move
        happened (auto_cluster off or no folder cleared threshold). Callers use this to
        know the note's final resting folder for redescribing it.
        """
        auto_cluster, cluster_threshold, sim_threshold, type_fallback = _get_cluster_config()
        if not auto_cluster:
            return None

        from archiver_rag.graph.placement import suggest_folder
        from archiver_rag.vault.reorganize import move_notes

        w_identity, w_content, name_prefix_bonus = _get_placement_weights_config()
        vault = Path(get_vault_path())
        note_path = Path(path)
        suggestion = suggest_folder(
            vault,
            note_path,
            threshold=sim_threshold,
            type_fallback=type_fallback,
            w_identity=w_identity,
            w_content=w_content,
            name_prefix_bonus=name_prefix_bonus,
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
                return None
            if current_rel_parent == target or (current_rel_parent == "." and target == "."):
                return None

            try:
                src = str(note_path.relative_to(vault))
            except ValueError:
                return None

            from archiver_rag.vault.folder_notes import (
                apply_extracted_terms,
                read_folder_note,
            )

            # §6 "Carpetas nuevas" — a destination folder move_notes is about to create
            # (or one that already exists but was never described) must not stay orphaned.
            was_undescribed = read_folder_note(vault, target) is None

            result = move_notes(
                [{"source": src, "destination": f"{target}/{note_path.name}"}]
            )
            if result.get("moved"):
                if was_undescribed:
                    # Give it a real description from the note that just landed there
                    # (Gate 1 folder birth) instead of an empty placeholder that would
                    # otherwise sit out of placement candidacy until auto_describe (if
                    # even on) eventually catches up.
                    try:
                        from archiver_rag.graph.terms import extract_terms

                        desc, dist = extract_terms(vault, target)
                        apply_extracted_terms(vault, target, desc, dist)
                    except Exception:
                        pass
                reason = suggestion.get("reason", "")
                sim = suggestion.get("similarity", 0.0)
                runtime.record_event("placed", f"{target}/{note_path.name}", counter="placed")
                _log(
                    f"Auto-placed {note_path.name} → {target}/ "
                    f"({reason}, {sim:.2f})"
                )
                return target
            return None

        # Gate 2 — no semantic match and no type: fallback either (reason == "none").
        # Route to inbox/ for embedding-based clustering, never wikilink-graph
        # topology (see graph/inbox.py's module docstring for why that pathology is
        # what gated this feature in the first place). Off by default: auto_inbox
        # ships gated, same as auto_cluster/auto_describe were when first introduced.
        if suggestion.get("reason") == "none":
            auto_inbox, min_cluster_size, sim_threshold_inbox = _get_inbox_config()
            if not auto_inbox:
                return None
            try:
                current_rel_parent = str(note_path.parent.relative_to(vault))
            except ValueError:
                return None
            if current_rel_parent == "inbox":
                return None  # defensive; is_new_note already prevents re-entry

            _ensure_inbox_locked(vault)

            try:
                src = str(note_path.relative_to(vault))
            except ValueError:
                return None

            result = move_notes([{"source": src, "destination": f"inbox/{note_path.name}"}])
            if not result.get("moved"):
                return None
            runtime.record_event("placed", f"inbox/{note_path.name}", counter="placed")
            _log(f"Routed {note_path.name} → inbox/ (no semantic or type match)")

            from archiver_rag.graph.inbox import maybe_spin_out_clusters

            _, _, max_terms, mmr_lambda, _, _ = _get_describe_config()
            spun_out = maybe_spin_out_clusters(
                vault,
                min_cluster_size=min_cluster_size,
                threshold=sim_threshold_inbox,
                w_identity=w_identity,
                w_content=w_content,
                max_terms=max_terms,
                mmr_lambda=mmr_lambda,
            )
            dst_inbox_path = f"inbox/{note_path.name}"
            for group in spun_out:
                _log(
                    f"Inbox cluster spun out → {group['folder']}/ "
                    f"({len(group['notes'])} notes)"
                )
                if dst_inbox_path in group["notes"]:
                    return group["folder"]
            return "inbox"

        return None

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
        runtime.record_event("created", path, counter="ingested")
        ingest_file(path)
        auto_link(path)
        moved_to = self._maybe_cluster(path)
        final_folder = moved_to
        if final_folder is None:
            try:
                final_folder = str(p.parent.relative_to(get_vault_path()))
            except ValueError:
                final_folder = None
        if final_folder is not None:
            _maybe_redescribe(final_folder)

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
        runtime.record_event("modified", path, counter="ingested")
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
        runtime.record_event("deleted", source, counter="deleted")
        collection.delete(where={"source": source})
        from archiver_rag.vault.notes import sweep_dead_links

        sweep_result = sweep_dead_links(Path(vault_path), [Path(path).stem])
        if sweep_result["swept"]:
            runtime.record_event("swept", source, counter="swept")
            _log(f"Swept links in: {', '.join(sweep_result['swept'])}")

        try:
            deleted_from = str(Path(path).parent.relative_to(vault_path))
        except ValueError:
            deleted_from = None
        if deleted_from is not None:
            _maybe_redescribe(deleted_from)

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
        src_folder = None
        if src_is_note:
            try:
                old_source = str(Path(src).relative_to(vault_path))
                src_folder = str(Path(src).parent.relative_to(vault_path))
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

            runtime.record_event("moved", dst_source, counter="ingested")
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

            moved_to = self._maybe_cluster(dst) if is_new_note else None
            dst_folder = moved_to
            if dst_folder is None:
                try:
                    dst_folder = str(Path(dst).parent.relative_to(vault_path))
                except ValueError:
                    dst_folder = None
            # Structural changes only: redescribe the folder the note landed in, and
            # (if different) the folder it left — a same-folder atomic-save rename has
            # src_folder == dst_folder and is not a membership change, so skip it there.
            if dst_folder is not None:
                _maybe_redescribe(dst_folder)
            if src_folder is not None and src_folder != dst_folder:
                _maybe_redescribe(src_folder)
            return

        # A note moved out of note-space entirely — into .trash/ (how Obsidian
        # deletes) or to a non-.md name. Treat it as a deletion and sweep the
        # wikilinks that now point nowhere.
        from archiver_rag.vault.notes import sweep_dead_links

        sweep_result = sweep_dead_links(Path(vault_path), [Path(src).stem])
        if sweep_result["swept"]:
            runtime.record_event("swept", Path(src).stem, counter="swept")
            _log(f"Swept links in: {', '.join(sweep_result['swept'])}")
        if src_folder is not None:
            _maybe_redescribe(src_folder)


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

    runtime.record_start(vault_path)
    _log(f"Watching vault at {vault_path}...")
    while observer.is_alive():
        time.sleep(1)


if __name__ == "__main__":
    import sys

    watch(sys.argv[1])
