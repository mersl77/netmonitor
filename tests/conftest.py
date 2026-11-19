import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Každý test má vlastní konfigurační/datový adresář – nikdy nesahá na skutečná data."""
    monkeypatch.setenv("NETMONITOR_HOME", str(tmp_path / "nmhome"))
    yield


def _install_network_change_guard(monkeypatch):
    """Pojistka AC-005: skutečné zápisy sítě nesmí spustit žádný test sady."""
    from netmonitor.platform import linux, macos, windows

    def forbidden(*args, **kwargs):
        raise AssertionError("Test se pokusil skutečně změnit nastavení sítě")

    for module in (linux, macos, windows):
        monkeypatch.setattr(module, "apply_ip", forbidden)
    popen = subprocess.Popen

    def guarded_popen(args, *positional, **kwargs):
        parts = shlex.split(args) if isinstance(args, str) else [os.fsdecode(a) for a in args]
        program = Path(parts[0].replace("\\", "/")).name.lower() if parts else ""
        options = [a.lower() for a in parts[1:]]
        if program == "pkexec":
            forbidden()
        if program == "networksetup" and any(a.startswith(("-set", "-create", "-remove", "-add", "-delete"))
                                              for a in options):
            forbidden()
        if program in ("netsh", "netsh.exe") and options[:1] == ["interface"] \
                and any(a in ("set", "add", "delete", "reset") for a in options[1:]):
            forbidden()
        if program == "nmcli" and any(
                obj in ("connection", "con", "c", "device", "dev", "d")
                and action in ("modify", "mod", "up", "reapply", "connect")
                for obj, action in zip(options, options[1:])):
            forbidden()
        if program == "osascript" and any("administrator privileges" in a for a in options):
            forbidden()
        return popen(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)


@pytest.fixture(autouse=True)
def _no_network_changes(monkeypatch):
    _install_network_change_guard(monkeypatch)
