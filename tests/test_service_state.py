"""service.state() / service_state() — structured liveness parsed from launchctl/systemctl.

The case that motivated this: `launchctl list <label>` exits 0 for a job that is loaded
but keeps dying, so the old status(), which looked only at the return code, printed
"✅ Running" at a crash-loop. Requiring a PID is what separates the two, and that
distinction is what most of these tests pin.

Since the generalization over WATCHER/HTTP, these tests drive state() through explicit
ServiceDef instances with tmp paths — nothing here touches a real supervisor or the
real install directories. subprocess.run is monkeypatched throughout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from archiver_rag import service


# Trimmed from real `launchctl list com.archiver-rag` output.
RUNNING = """{
	"LimitLoadToSessionType" = "Aqua";
	"Label" = "{label}";
	"OnDemand" = false;
	"LastExitStatus" = 0;
	"PID" = 17249;
	"Program" = "/Users/x/.local/bin/archiver-rag";
};
"""

CRASH_LOOPING = """{
	"Label" = "{label}";
	"OnDemand" = false;
	"LastExitStatus" = 1;
	"Program" = "/Users/x/.local/bin/archiver-rag";
};
"""


@pytest.fixture
def fake_run(monkeypatch):
    calls = []

    def _install(stdout: str = "", returncode: int = 0):
        def _run(cmd, *a, **kw):
            calls.append(cmd)
            return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)

        monkeypatch.setattr(subprocess, "run", _run)
        return calls

    return _install


def make_def(
    tmp_path: Path,
    label: str = "com.archiver-rag",
    *,
    installed: bool = True,
) -> service.ServiceDef:
    """A ServiceDef rooted in tmp_path — optionally with its service file present.

    plist_path and unit_path point at the same tmp file so the installed check
    works under either faked platform.
    """
    path = tmp_path / f"{label}.service-file"
    if installed:
        path.write_text(f"label={label}", encoding="utf-8")
    return service.ServiceDef(
        label=label,
        plist_path=path,
        unit_path=path,
        stdout_log=f"/tmp/{label}.out.log",
        stderr_log=f"/tmp/{label}.err.log",
    )


def test_watcher_state_running_reports_pid(monkeypatch, fake_run, tmp_path):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    defn = service.ServiceDef(
        label="com.archiver-rag",
        plist_path=_existing(),
        unit_path=tmp_path / "u",
        stdout_log="/tmp/w.out",
        stderr_log="/tmp/w.err",
    )
    fake_run(RUNNING.replace("{label}", defn.label), 0)

    state = service.state(defn)
    assert state["platform"] == "darwin"
    assert state["running"] is True
    assert state["loaded"] is True
    assert state["pid"] == 17249
    assert state["last_exit_status"] == 0
    assert state["stdout_log"] == defn.stdout_log
    assert state["label"] == defn.label


def test_service_state_defaults_to_the_watcher(monkeypatch, fake_run, tmp_path):
    """The zero-arg entry point must stay watcher-bound — relink depends on it."""
    monkeypatch.setattr(service.sys, "platform", "darwin")
    watcher = make_def(tmp_path, "com.watcher-test")
    monkeypatch.setattr(service, "WATCHER", watcher)
    fake_run(RUNNING.replace("{label}", watcher.label), 0)

    state = service.service_state()
    assert state["label"] == "com.watcher-test"


def test_watcher_loaded_but_dead_is_not_running(monkeypatch, fake_run, tmp_path):
    """launchctl still exits 0 here — the old status() called this '✅ Running'."""
    monkeypatch.setattr(service.sys, "platform", "darwin")
    defn = make_def(tmp_path)
    fake_run(CRASH_LOOPING.replace("{label}", defn.label), 0)

    state = service.state(defn)
    assert state["loaded"] is True
    assert state["running"] is False
    assert state["pid"] is None
    assert state["last_exit_status"] == 1


def test_watcher_not_loaded(monkeypatch, fake_run, tmp_path):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    fake_run("Could not find service", 113)

    state = service.state(make_def(tmp_path))
    assert state["loaded"] is False
    assert state["running"] is False
    assert state["pid"] is None


def test_reports_not_installed_when_service_file_is_absent(monkeypatch, fake_run, tmp_path):
    monkeypatch.setattr(service.sys, "platform", "darwin")

    assert service.state(make_def(tmp_path, installed=False))["installed"] is False


def test_linux_active(monkeypatch, fake_run, tmp_path):
    defn = make_def(tmp_path)
    monkeypatch.setattr(service.sys, "platform", "linux")
    calls = fake_run("ActiveState=active\nMainPID=4242\nExecMainStatus=0\n", 0)

    state = service.state(defn)
    assert state["platform"] == "linux"
    assert state["running"] is True
    assert state["pid"] == 4242
    assert state["installed"] is True
    # `show`, not `status` — machine-readable, and it keeps raw systemd prose out of
    # our own report.
    assert "show" in calls[0]


def test_linux_inactive(monkeypatch, fake_run, tmp_path):
    monkeypatch.setattr(service.sys, "platform", "linux")
    defn = make_def(tmp_path, installed=False)
    fake_run("ActiveState=inactive\nMainPID=0\nExecMainStatus=\n", 0)

    state = service.state(defn)
    assert state["running"] is False
    assert state["loaded"] is False
    assert state["pid"] is None
    assert state["last_exit_status"] is None


def test_unit_name_derivation_matches_both_daemons():
    """The watcher's Linux unit name predates this module — it must not change."""
    assert service.WATCHER.unit_name == "archiver-rag"
    assert service.HTTP.unit_name == "archiver-rag-http"


def test_unsupported_platform_reports_rather_than_raising(monkeypatch, tmp_path):
    monkeypatch.setattr(service.sys, "platform", "sunos5")

    state = service.state(make_def(tmp_path))
    assert state["running"] is False
    assert state["installed"] is False
    assert "unsupported platform" in state["error"]


def test_subprocess_failure_is_reported_not_raised(monkeypatch, tmp_path):
    monkeypatch.setattr(service.sys, "platform", "darwin")

    def _boom(*a, **kw):
        raise FileNotFoundError("launchctl: not found")

    monkeypatch.setattr(subprocess, "run", _boom)

    state = service.state(make_def(tmp_path))
    assert state["running"] is False
    assert "FileNotFoundError" in state["error"]


def _existing():
    """A path object that reports itself as existing, without touching the real FS."""

    class _P:
        def exists(self):
            return True

        def __str__(self):
            return "/fake/com.archiver-rag.plist"

    return _P()
