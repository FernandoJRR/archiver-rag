from pathlib import Path
import time
import sys
import signal
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from archiver_rag.core.ingest import ingest_file
from archiver_rag.core.db import collection
import os

from archiver_rag.graph.linker import auto_link

_new_notes_since_cluster = 0


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
        if event.is_directory or not path.endswith(".md"):
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
        if event.is_directory or not path.endswith(".md"):
            return
        print(f"File modified: {event.src_path}")
        ingest_file(path)
        auto_link(path)

    def on_deleted(self, event):
        path = str(event.src_path)
        if event.is_directory or not path.endswith(".md"):
            return
        filename = Path(path).name
        print(f"File deleted: {filename}")
        collection.delete(where={"source": filename})

    def on_moved(self, event):
        src = str(event.src_path)
        dst = str(event.dest_path)
        if not src.endswith(".md"):
            return
        old_filename = Path(src).name
        collection.delete(where={"source": old_filename})
        print(f"File renamed/moved: {old_filename} → {event.dest_path}")
        ingest_file(dst)

def shutdown(observer, signum, frame):
    print("\nStopping Watcher")
    observer.stop()
    observer.join()
    sys.exit(0)

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
