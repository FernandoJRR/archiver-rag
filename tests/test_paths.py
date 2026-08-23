"""Tests for archiver_rag.paths — XDG dir resolution and legacy-install migration."""

from __future__ import annotations

import json

from archiver_rag import paths


def test_fresh_install_no_legacy_dir_is_noop(tmp_install):
    paths.ensure_migrated()

    assert not tmp_install["config_dir"].exists()
    assert not tmp_install["data_dir"].exists()


def test_already_migrated_is_noop_when_new_config_exists(tmp_install):
    tmp_install["config_dir"].mkdir(parents=True)
    new_config = tmp_install["config_dir"] / "config.json"
    new_config.write_text(json.dumps({"vault_path": "/already/migrated"}))

    tmp_install["legacy_dir"].mkdir(parents=True)
    (tmp_install["legacy_dir"] / "config.json").write_text(
        json.dumps({"vault_path": "/legacy"})
    )

    paths.ensure_migrated()

    assert json.loads(new_config.read_text())["vault_path"] == "/already/migrated"


def test_full_migration_moves_config_chroma_and_centroids_and_rewrites_paths(
    tmp_install,
):
    legacy = tmp_install["legacy_dir"]
    legacy.mkdir(parents=True)
    (legacy / "config.json").write_text(
        json.dumps(
            {
                "vault_path": "/my/vault",
                "install_path": str(legacy),
                "chroma_path": str(legacy / "chroma_db"),
            }
        )
    )
    (legacy / "chroma_db").mkdir()
    (legacy / "chroma_db" / "marker.bin").write_text("data")
    (legacy / "centroids.json").write_text(json.dumps({"decision": {"fp": "abc"}}))

    paths.ensure_migrated()

    new_config_path = tmp_install["config_dir"] / "config.json"
    assert new_config_path.exists()
    cfg = json.loads(new_config_path.read_text())
    assert cfg["vault_path"] == "/my/vault"
    assert cfg["chroma_path"] == str(tmp_install["data_dir"] / "chroma_db")
    assert cfg["install_path"] == str(tmp_install["data_dir"])

    new_chroma = tmp_install["data_dir"] / "chroma_db"
    assert (new_chroma / "marker.bin").read_text() == "data"

    new_centroids = tmp_install["data_dir"] / "centroids.json"
    assert json.loads(new_centroids.read_text()) == {"decision": {"fp": "abc"}}


def test_migration_skips_missing_pieces_without_error(tmp_install):
    legacy = tmp_install["legacy_dir"]
    legacy.mkdir(parents=True)
    (legacy / "config.json").write_text(json.dumps({"vault_path": "/my/vault"}))
    # No chroma_db/, no centroids.json — migration must not error on their absence.

    paths.ensure_migrated()

    new_config_path = tmp_install["config_dir"] / "config.json"
    assert new_config_path.exists()
    assert not (tmp_install["data_dir"] / "chroma_db").exists()
    assert not (tmp_install["data_dir"] / "centroids.json").exists()


def test_migration_leaves_legacy_dir_in_place_and_logs(tmp_install, capsys):
    legacy = tmp_install["legacy_dir"]
    legacy.mkdir(parents=True)
    (legacy / "config.json").write_text(json.dumps({"vault_path": "/my/vault"}))

    paths.ensure_migrated()

    assert legacy.exists()
    out = capsys.readouterr().out
    assert "Migrated legacy" in out
    assert str(legacy) in out


def test_ensure_migrated_only_touches_disk_once_per_process(tmp_install):
    legacy = tmp_install["legacy_dir"]
    legacy.mkdir(parents=True)
    (legacy / "config.json").write_text(json.dumps({"vault_path": "/my/vault"}))

    paths.ensure_migrated()
    new_config_path = tmp_install["config_dir"] / "config.json"
    new_config_path.write_text(json.dumps({"vault_path": "/edited/after/migration"}))

    # A second call must not re-run migration logic (guarded by _migration_checked),
    # so a manual edit made after the first call survives untouched.
    paths.ensure_migrated()

    assert json.loads(new_config_path.read_text())["vault_path"] == (
        "/edited/after/migration"
    )
