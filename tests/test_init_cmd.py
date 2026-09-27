"""init must accept the paths people actually type: ~/..., relative, trailing slash."""

import json

import pytest
import typer

from archiver_rag import init_cmd, paths


@pytest.fixture
def run_init(tmp_install, monkeypatch):
    """Run init with scripted answers, stopping before indexing and service setup."""

    def _run(answer: str):
        monkeypatch.setattr(init_cmd.Prompt, "ask", lambda *a, **kw: answer)
        monkeypatch.setattr(init_cmd.Confirm, "ask", lambda *a, **kw: False)
        monkeypatch.setattr(init_cmd, "_is_cached", lambda: True)

        class _Stop(Exception):
            pass

        def _stop(*a, **kw):
            raise _Stop

        monkeypatch.setattr("archiver_rag.core.ingest.ingest_vault", _stop)
        try:
            init_cmd.run_init()
        except _Stop:
            pass
        return json.loads(paths.config_path().read_text())

    return _run


def test_tilde_path_is_expanded_and_stored_absolute(run_init, tmp_path, monkeypatch):
    vault = tmp_path / "home" / "my-vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    config = run_init("~/my-vault")

    assert config["vault_path"] == str(vault.resolve())


def test_relative_path_is_stored_absolute(run_init, tmp_path, monkeypatch):
    (tmp_path / "vault").mkdir()
    monkeypatch.chdir(tmp_path)

    assert run_init("vault/")["vault_path"] == str((tmp_path / "vault").resolve())


def test_missing_path_exits_without_writing_config(tmp_install, monkeypatch, tmp_path):
    monkeypatch.setattr(init_cmd.Prompt, "ask", lambda *a, **kw: str(tmp_path / "nope"))
    with pytest.raises(typer.Exit):
        init_cmd.run_init()
    assert not paths.config_path().exists()
