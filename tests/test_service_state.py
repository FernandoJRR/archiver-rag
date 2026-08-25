"""service_state() — structured liveness parsed from launchctl / systemctl.

The case that motivated this: `launchctl list <label>` exits 0 for a job that is loaded
but keeps dying, so the old status(), which looked only at the return code, printed
"✅ Running" at a crash-loop. Requiring a PID is what separates the two, and that
distinction is what most of these tests pin.

subprocess.run is monkeypatched throughout — no test touches a real service.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from archiver_rag import service


# Trimmed from real `launchctl list com.archiver-rag` output.
RUNNING = """{
	"LimitLoadToSessionType" = "Aqua";
	"Label" = "com.archiver-rag";
	"OnDemand" = false;
	"LastExitStatus" = 0;
	"PID" = 17249;
	"Program" = "/Users/x/.local/bin/archiver-rag";
};
"""

CRASH_LOOPING = """{
	"Label" = "com.archiver-rag";
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


def test_darwin_running_reports_pid(monkeypatch, fake_run):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service, "PLIST_PATH", _existing(monkeypatch))
    fake_run(RUNNING, 0)

    state = service.service_state()
    assert state["platform"] == "darwin"
    assert state["running"] is True
    assert state["loaded"] is True
    assert state["pid"] == 17249
    assert state["last_exit_status"] == 0
    assert state["stdout_log"] == service.STDOUT_LOG


def test_darwin_loaded_but_dead_is_not_running(monkeypatch, fake_run):
    """launchctl still exits 0 here — the old status() called this '✅ Running'."""
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service, "PLIST_PATH", _existing(monkeypatch))
    fake_run(CRASH_LOOPING, 0)

    state = service.service_state()
    assert state["loaded"] is True
    assert state["running"] is False
    assert state["pid"] is None
    assert state["last_exit_status"] == 1


def test_darwin_not_loaded(monkeypatch, fake_run):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service, "PLIST_PATH", _existing(monkeypatch))
    fake_run("Could not find service", 113)

    state = service.service_state()
    assert state["loaded"] is False
    assert state["running"] is False
    assert state["pid"] is None


def test_darwin_reports_not_installed_when_plist_is_absent(monkeypatch, fake_run, tmp_path):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service, "PLIST_PATH", tmp_path / "nope.plist")
    fake_run("", 113)

    assert service.service_state()["installed"] is False


def test_linux_active(monkeypatch, fake_run, tmp_path):
    unit = tmp_path / "archiver-rag.service"
    unit.write_text("[Unit]", encoding="utf-8")
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service, "UNIT_PATH", unit)
    calls = fake_run("ActiveState=active\nMainPID=4242\nExecMainStatus=0\n", 0)

    state = service.service_state()
    assert state["platform"] == "linux"
    assert state["running"] is True
    assert state["pid"] == 4242
    assert state["installed"] is True
    # `show`, not `status` — machine-readable, and it keeps raw systemd prose out of
    # our own report.
    assert "show" in calls[0]


def test_linux_inactive(monkeypatch, fake_run, tmp_path):
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service, "UNIT_PATH", tmp_path / "absent.service")
    fake_run("ActiveState=inactive\nMainPID=0\nExecMainStatus=\n", 0)

    state = service.service_state()
    assert state["running"] is False
    assert state["loaded"] is False
    assert state["pid"] is None
    assert state["last_exit_status"] is None


def test_unsupported_platform_reports_rather_than_raising(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "sunos5")

    state = service.service_state()
    assert state["running"] is False
    assert state["installed"] is False
    assert "unsupported platform" in state["error"]


def test_subprocess_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "darwin")

    def _boom(*a, **kw):
        raise FileNotFoundError("launchctl: not found")

    monkeypatch.setattr(subprocess, "run", _boom)

    state = service.service_state()
    assert state["running"] is False
    assert "FileNotFoundError" in state["error"]


def _existing(monkeypatch):
    """A path object that reports itself as existing, without touching the real FS."""

    class _P:
        def exists(self):
            return True

        def __str__(self):
            return "/fake/com.archiver-rag.plist"

    return _P()
