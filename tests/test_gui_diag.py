"""Diagnostika Qt: čistý snímek, anonymizace, X11 selekce a oddělení CLI od sítě."""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from netmonitor.gui import diag


def test_snapshot_with_mock_screens_and_environment():
    screen = Mock()
    screen.geometry.return_value = QRect(-1280, 0, 1280, 720)
    screen.devicePixelRatio.return_value = 1.5
    screen.logicalDotsPerInch.return_value = 96.0
    screen.physicalDotsPerInch.return_value = 110.2
    screen.refreshRate.return_value = 59.95
    env = {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "zorin:GNOME", "DISPLAY": "private-display",
           "WAYLAND_DISPLAY": "/run/user/private/wayland", "QT_SCALE_FACTOR": "1.5",
           "QT_SCREEN_SCALE_FACTORS": "1;1.5", "QT_ENABLE_HIGHDPI_SCALING": "1", "QT_QPA_PLATFORM": "xcb"}
    original = env.copy()
    result = diag.build_gui_diagnostics(qt_platform="xcb", qt_version="6.6.0", pyside_version="6.6.1",
                                        environ=env, screens=[screen, screen], tray_available=True,
                                        compositing={"_NET_WM_CM_S0": True})
    assert result["qt_platform"] == "xcb"
    assert result["qt_version"] == "6.6.0" and result["pyside_version"] == "6.6.1"
    assert result["screens"] == [{"geometry": {"x": -1280, "y": 0, "width": 1280, "height": 720},
                                  "devicePixelRatio": 1.5, "logicalDotsPerInch": 96.0,
                                  "physicalDotsPerInch": 110.2, "refreshRate": 59.95}] * 2
    assert result["tray_available"] is True
    assert result["compositing"] == {"_NET_WM_CM_S0": True}
    assert result["environment"] == dict(env, DISPLAY=True, WAYLAND_DISPLAY=True)
    assert env == original
    assert not screen.name.called and not screen.manufacturer.called and not screen.serialNumber.called
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(("key", "value", "expected"), [
    ("QT_SCREEN_SCALE_FACTORS", "private-monitor=1.25;private-other=2", "1.25;2"),
    ("QT_SCREEN_SCALE_FACTORS", "/home/private=secret;1.5", "[redacted];1.5"),
    ("QT_SCALE_FACTOR", "/home/private", "[redacted]"),
    ("QT_SCALE_FACTOR", "1.25", "1.25"),
    ("QT_ENABLE_HIGHDPI_SCALING", "private", "[redacted]"),
    ("QT_QPA_PLATFORM", "xcb:private=/home/private;wayland", "xcb;wayland"),
    ("QT_QPA_PLATFORM", "/home/private", "[redacted]"),
    ("XDG_CURRENT_DESKTOP", "GNOME:/home/private", "GNOME:[redacted]"),
    ("XDG_SESSION_TYPE", "/home/private", "[redacted]"),
])
def test_environment_never_copies_private_parameters(key, value, expected):
    result = diag._safe_environment({key: value, "QT_PLUGIN_PATH": "/home/private"})
    assert result[key] == expected
    assert "private" not in json.dumps(result)
    assert "QT_PLUGIN_PATH" not in result


def test_custom_session_and_desktop_tokens_and_empty_list_parts():
    result = diag._safe_environment({"XDG_SESSION_TYPE": "custom-session_2", "XDG_CURRENT_DESKTOP": "NewWM:GNOME:",
                                     "QT_SCREEN_SCALE_FACTORS": ";monitor-name=1.25;;2;",
                                     "QT_QPA_PLATFORM": ";xcb;;wayland;"})
    assert result["XDG_SESSION_TYPE"] == "custom-session_2"
    assert result["XDG_CURRENT_DESKTOP"] == "NewWM:GNOME"
    assert result["QT_SCREEN_SCALE_FACTORS"] == "1.25;2"
    assert result["QT_QPA_PLATFORM"] == "xcb;wayland"
    assert diag._safe_environment({"XDG_SESSION_TYPE": "x" * 33})["XDG_SESSION_TYPE"] == "[redacted]"


def test_empty_environment_and_no_screens():
    result = diag.build_gui_diagnostics(qt_platform="offscreen", qt_version="6.6.0", pyside_version="6.6.0",
                                        environ={}, screens=[], tray_available=False, compositing="neznámo")
    assert result["screens"] == [] and result["tray_available"] is False
    assert result["compositing"] == "neznámo"
    assert result["environment"]["QT_SCALE_FACTOR"] is None
    assert result["environment"]["DISPLAY"] is False
    assert result["environment"]["WAYLAND_DISPLAY"] is False


def test_nonfinite_screen_metrics_produce_valid_json():
    """Regrese: Wayland při nedostupném monitoru vrací NaN DPI, které není platné JSON číslo."""
    screen = Mock()
    screen.geometry.return_value = QRect(0, 0, 0, 0)
    screen.devicePixelRatio.return_value = float("inf")
    screen.logicalDotsPerInch.return_value = float("nan")
    screen.physicalDotsPerInch.return_value = float("nan")
    screen.refreshRate.return_value = float("-inf")
    result = diag.build_gui_diagnostics(qt_platform="wayland", qt_version="6.12.0", pyside_version="6.12.0",
                                        environ={}, screens=[screen], tray_available=False, compositing="neznámo")
    metrics = result["screens"][0]
    for key in ("devicePixelRatio", "logicalDotsPerInch", "physicalDotsPerInch", "refreshRate"):
        assert metrics[key] is None
    assert json.loads(json.dumps(result, allow_nan=False)) == result


def test_x11_selection_owners_for_all_x_screens(monkeypatch):
    app = Mock()
    app.platformName.return_value = "xcb"
    app.nativeInterface.return_value.display.return_value = 123
    x11 = Mock()
    x11.XScreenCount.return_value = 3
    x11.XInternAtom.side_effect = [10, 20, 0]
    x11.XGetSelectionOwner.side_effect = [42, 0]
    monkeypatch.setattr(diag.ctypes, "CDLL", lambda _: x11)
    assert diag._x11_compositing(app) == {"_NET_WM_CM_S0": True, "_NET_WM_CM_S1": False,
                                         "_NET_WM_CM_S2": False}
    assert [call.args[1] for call in x11.XInternAtom.call_args_list] == [b"_NET_WM_CM_S0", b"_NET_WM_CM_S1",
                                                                      b"_NET_WM_CM_S2"]
    assert x11.XGetSelectionOwner.call_count == 2  # atom None se nesmí poslat X serveru
    x11.XCloseDisplay.assert_not_called()         # spojení patří Qt


@pytest.mark.parametrize("platform", ["wayland", "offscreen", "windows", "cocoa"])
def test_non_xcb_never_calls_xlib(monkeypatch, platform):
    app = Mock()
    app.platformName.return_value = platform
    loader = Mock(side_effect=AssertionError("Xlib mimo xcb"))
    monkeypatch.setattr(diag.ctypes, "CDLL", loader)
    assert diag._x11_compositing(app) == "neznámo"
    loader.assert_not_called()
    app.nativeInterface.assert_not_called()


@pytest.mark.parametrize("failure", ["no_native", "no_pointer", "no_library"])
def test_x11_unavailable_is_unknown(monkeypatch, failure):
    app = Mock()
    app.platformName.return_value = "xcb"
    native = Mock()
    native.display.return_value = None if failure == "no_pointer" else 123
    app.nativeInterface.return_value = None if failure == "no_native" else native
    monkeypatch.setattr(diag.ctypes, "CDLL", Mock(side_effect=OSError("private path must not be logged")))
    assert diag._x11_compositing(app) == "neznámo"


@pytest.mark.parametrize("fallback", [None, "cursor", "other"])
def test_collector_offscreen(monkeypatch, fallback):
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    monkeypatch.setenv("DISPLAY", "private-display")
    monkeypatch.setenv("WAYLAND_DISPLAY", "/run/user/private/wayland")
    previous = app.property("xcb_fallback")
    app.setProperty("xcb_fallback", fallback)
    try:
        result = diag.collect_gui_diagnostics(app, tray_available=False, platform_selected_by_app="xcb;wayland")
    finally:
        app.setProperty("xcb_fallback", previous)
    assert result["qt_platform"] == "offscreen"
    assert result["screens"] and result["qt_version"] and result["pyside_version"]
    assert result["compositing"] == "neznámo"
    assert result["platform_selected_by_app"] == "xcb;wayland"
    assert result["xcb_fallback"] == fallback
    assert result["tray_available"] is False
    assert "private" not in json.dumps(result)


def _run_main(tmp_path, flags, *, setup="", environment=None):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"network": {"external_api": False, "gateway_probe": False},
                                  "general": {"check_updates": False}, "ping": {"targets": []}}))
    # Bez sítě a bez údajů lokálních rozhraní i u původního --diagnose.
    code = """
import sys
from unittest.mock import patch
from netmonitor.core.network_info import NetworkSnapshot
from netmonitor.main import main
if '--diagnose' in sys.argv and '--diagnose-gui' not in sys.argv:
    sys.modules['PySide6'] = None  # původní CLI musí fungovat i bez Qt
""" + setup + """
with patch('netmonitor.core.network_info.NetworkInfoProvider.snapshot', return_value=NetworkSnapshot(active=None)):
    raise SystemExit(main(sys.argv[1:]))
"""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    if environment is not None:
        env = environment
    return subprocess.run([sys.executable, "-c", code, *flags, "--config", str(config)],
                          env=env, capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize("flags", [["--diagnose"], ["--diagnose-gui"], ["--diagnose", "--diagnose-gui"]])
def test_diagnostic_cli_modes(tmp_path, flags):
    setup = ""
    if "--diagnose-gui" in flags:
        setup = "patch('PySide6.QtWidgets.QSystemTrayIcon.isSystemTrayAvailable', side_effect=[False]).start()\n"
    process = _run_main(tmp_path, flags, setup=setup)
    assert process.returncode == 0, process.stderr
    output = json.loads(process.stdout)
    assert ("gui" in output) == ("--diagnose-gui" in flags)
    assert ("network" in output) == ("--diagnose" in flags)
    assert ("GUI diagnostika: " in process.stderr) == ("--diagnose-gui" in flags)
    if "gui" in output:
        assert output["gui"]["qt_platform"] == "offscreen"
        assert output["gui"]["platform_selected_by_app"] is None
        assert output["gui"]["xcb_fallback"] is None


@pytest.mark.parametrize("failure", ["collector", "json"])
def test_diagnostic_failure_does_not_stop_startup(tmp_path, failure):
    """Regrese review L10: výjimka z diagnostiky nesmí zastavit spuštění widgetu."""
    setup = "patch('PySide6.QtWidgets.QSystemTrayIcon.isSystemTrayAvailable', side_effect=[False]).start()\n"
    if failure == "collector":
        setup += "patch('netmonitor.gui.diag.collect_gui_diagnostics', side_effect=RuntimeError('private failure')).start()\n"
    else:
        setup += "patch('netmonitor.gui.diag.collect_gui_diagnostics', return_value={'bad': object()}).start()\n"
    screenshot = tmp_path / "widget.png"
    process = _run_main(tmp_path, ["--debug", "--screenshot", str(screenshot), "--screenshot-delay", "0.05"], setup=setup)
    assert process.returncode == 0, process.stderr
    assert screenshot.is_file()
    assert "DEBUG" in process.stderr and "Sběr GUI diagnostiky selhal" in process.stderr
    assert "Konec (0)" in process.stderr


@pytest.mark.parametrize("flags", [["--diagnose-gui"], ["--diagnose", "--diagnose-gui"]])
def test_diagnostic_collector_error_is_safe_json(tmp_path, flags):
    setup = "patch('netmonitor.gui.diag.collect_gui_diagnostics', side_effect=RuntimeError('/home/private')).start()\n"
    process = _run_main(tmp_path, flags, setup=setup)
    assert process.returncode == 2, process.stderr
    output = json.loads(process.stdout)
    error = output["gui"] if "--diagnose" in flags else output
    assert error == {"error": "Sběr GUI diagnostiky selhal"}
    assert "private" not in process.stdout and "private" not in process.stderr


@pytest.mark.parametrize("flags", [["--diagnose-gui"], ["--diagnose", "--diagnose-gui"]])
def test_gui_diagnostic_without_display_never_loads_qt(tmp_path, flags):
    """Regrese review L10: na headless Linuxu návratový kód 2 místo nativního abortu Qt."""
    if not sys.platform.startswith("linux"):
        pytest.skip("Ochrana před inicializací Qt se týká Linuxu")
    env = {"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
           "NETMONITOR_HOME": str(tmp_path / "home"), "PATH": os.defpath}
    process = _run_main(tmp_path, flags, setup="sys.modules['PySide6'] = None\n", environment=env)
    assert process.returncode == 2, process.stderr
    output = json.loads(process.stdout)
    if "--diagnose" in flags:
        assert "network" in output
        assert output["gui"] == {"error": "bez grafické relace"}
    else:
        assert output == {"error": "bez grafické relace"}
    assert "bez grafické relace" in process.stderr
