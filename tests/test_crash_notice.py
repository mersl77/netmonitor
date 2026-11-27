"""L02: oznámení růstu crash.log bez sítě a bez skutečných uživatelských dat."""
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox

from netmonitor import main as entry
from netmonitor.utils import logging_setup
from netmonitor.utils.paths import data_dir, log_dir


@pytest.fixture
def crash_log():
    path = log_dir() / "crash.log"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"stary pad\n")
    return path


def seen_size():
    return json.loads((data_dir() / "crash-notice.json").read_text(encoding="utf-8"))["seen_size"]


def test_first_start_ignores_old_crash(crash_log):
    from netmonitor.utils.crash_notice import pending_crash

    assert pending_crash() is None
    assert seen_size() == crash_log.stat().st_size


def test_growth_is_pending_until_acknowledged(crash_log):
    from netmonitor.utils.crash_notice import mark_seen, pending_crash

    assert pending_crash() is None
    crash_log.write_bytes(crash_log.read_bytes() + b"novy pad\n")
    size = crash_log.stat().st_size
    assert pending_crash() == size
    assert pending_crash() == size
    mark_seen(size)
    assert pending_crash() is None
    crash_log.write_bytes(crash_log.read_bytes() + b"dalsi pad\n")
    assert pending_crash() == crash_log.stat().st_size


@pytest.mark.parametrize("deleted", [False, True])
def test_log_shrink_resets_marker(crash_log, deleted):
    from netmonitor.utils.crash_notice import pending_crash

    assert pending_crash() is None
    if deleted:
        crash_log.unlink()
    else:
        crash_log.write_bytes(b"x")
    assert pending_crash() is None
    assert seen_size() == (0 if deleted else 1)


def test_first_start_without_log():
    from netmonitor.utils.crash_notice import pending_crash

    assert pending_crash() is None
    assert seen_size() == 0


@pytest.mark.parametrize("contents", ["{", "[]", "null", '{}', '{"seen_size": "5"}',
                                     '{"seen_size": true}', '{"seen_size": -1}',
                                     '{"seen_size": 1.5}'])
def test_bad_marker_does_not_break_start(crash_log, contents, caplog):
    from netmonitor.utils.crash_notice import pending_crash

    (data_dir() / "crash-notice.json").write_text(contents, encoding="utf-8")
    assert pending_crash() is None
    assert seen_size() == crash_log.stat().st_size
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    crash_log.write_bytes(crash_log.read_bytes() + b"novy pad\n")
    assert pending_crash() == crash_log.stat().st_size


def test_deeply_nested_marker_does_not_break_start(crash_log, caplog):
    from netmonitor.utils.crash_notice import pending_crash

    depth = 10_000
    contents = "[" * depth + "0" + "]" * depth
    (data_dir() / "crash-notice.json").write_text(contents, encoding="utf-8")
    assert pending_crash() is None
    assert seen_size() == crash_log.stat().st_size
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    crash_log.write_bytes(crash_log.read_bytes() + b"novy pad\n")
    assert pending_crash() == crash_log.stat().st_size


@pytest.mark.parametrize("clean_exit", [False, True])
def test_only_clean_exit_acknowledges_runtime_writes(crash_log, clean_exit):
    from netmonitor.utils.crash_notice import mark_clean_exit, pending_crash

    assert pending_crash() is None
    crash_log.write_bytes(crash_log.read_bytes() + b"Windows fatal exception: code 0x8001010d\n")
    if clean_exit:
        mark_clean_exit()
        assert seen_size() == crash_log.stat().st_size
    assert pending_crash() == (None if clean_exit else crash_log.stat().st_size)
    # Skutečný pád následujícího běhu se i po čistém ukončení musí znovu ohlásit.
    crash_log.write_bytes(crash_log.read_bytes() + b"novy pad\n")
    assert pending_crash() == crash_log.stat().st_size


@pytest.mark.parametrize("failure", ["stat", "write"])
def test_clean_exit_io_failure_is_safe(crash_log, monkeypatch, caplog, failure):
    from netmonitor.utils import crash_notice

    def fail(*args, **kwargs):
        raise PermissionError("synteticka chyba I/O")

    if failure == "stat":
        monkeypatch.setattr(type(crash_log), "stat", fail)
    else:
        monkeypatch.setattr(crash_notice, "atomic_write_json", fail)
    crash_notice.mark_clean_exit()
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_startup_checks_crash_after_dirs_before_logging(monkeypatch):
    from netmonitor.utils.crash_notice import pending_crash

    assert not data_dir().exists()
    calls = []
    ensure_dirs = entry.ensure_dirs

    def ensure():
        ensure_dirs()
        calls.append("dirs")

    def check():
        assert data_dir().is_dir() and log_dir().is_dir()
        calls.append("pending")
        return pending_crash()

    def setup(*args):
        assert seen_size() == 0
        calls.append("logging")

    monkeypatch.setattr(entry, "ensure_dirs", ensure)
    monkeypatch.setattr(entry, "pending_crash", check)
    monkeypatch.setattr(entry, "setup_logging", setup)
    monkeypatch.setattr(entry, "diagnose", Mock(return_value=0))
    assert entry._main(["--diagnose"]) == 0
    assert calls == ["dirs", "pending", "logging"]


@pytest.mark.parametrize("failure", ["stat", "read", "write"])
def test_io_failure_is_safe(crash_log, monkeypatch, failure):
    from netmonitor.utils import crash_notice

    def fail(*args, **kwargs):
        raise PermissionError("synteticka chyba I/O")

    if failure == "stat":
        monkeypatch.setattr(type(crash_log), "stat", fail)
    elif failure == "read":
        monkeypatch.setattr(type(crash_log), "read_text", fail)
    else:
        monkeypatch.setattr(crash_notice, "atomic_write_json", fail)
        crash_notice.mark_seen(10)
    assert crash_notice.pending_crash() is None


@pytest.mark.parametrize("error", [RuntimeError("synteticky pad"), SystemExit(2), KeyboardInterrupt()])
def test_main_records_only_exceptions(crash_log, monkeypatch, error):
    assert hasattr(entry, "_main"), "main potřebuje obal pro trvalý zápis výjimky"
    monkeypatch.setattr(entry, "_main", Mock(side_effect=error))
    monkeypatch.setattr(sys, "stderr", None)
    # Stejný otevřený soubor jako pro faulthandler, bez změny jeho globálního nastavení.
    with crash_log.open("a", encoding="utf-8") as stream:
        monkeypatch.setattr(logging_setup, "_crash_file", stream)
        before = crash_log.read_bytes()
        with pytest.raises(type(error)) as caught:
            entry.main([])
        assert caught.value is error
        after = crash_log.read_bytes()
    if isinstance(error, Exception):
        assert after.startswith(before)
        assert b"Traceback (most recent call last)" in after
        assert b"RuntimeError: synteticky pad" in after
    else:
        assert after == before


def test_main_preserves_exception_when_log_write_fails(monkeypatch):
    assert hasattr(entry, "_main")
    error = RuntimeError("puvodni chyba")
    monkeypatch.setattr(entry, "_main", Mock(side_effect=error))
    monkeypatch.setattr(logging_setup, "_crash_file", Mock(write=Mock(side_effect=OSError("plny disk"))))
    with pytest.raises(RuntimeError) as caught:
        entry.main([])
    assert caught.value is error


def test_main_exception_before_logging_setup(monkeypatch):
    assert hasattr(entry, "_main")
    error = RuntimeError("pad pred inicializaci logu")
    monkeypatch.setattr(entry, "_main", Mock(side_effect=error))
    monkeypatch.setattr(logging_setup, "_crash_file", None)
    monkeypatch.setattr("faulthandler.enable", Mock())
    try:
        with pytest.raises(RuntimeError) as caught:
            entry.main([])
        assert caught.value is error
        assert "RuntimeError: pad pred inicializaci logu" in (log_dir() / "crash.log").read_text(encoding="utf-8")
    finally:
        if logging_setup._crash_file is not None:
            logging_setup._crash_file.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Izolovaný SIGABRT bez core dumpu používá resource")
def test_native_abort_in_qt_loop_is_pending(crash_log, tmp_path):
    from netmonitor.utils.crash_notice import pending_crash

    assert pending_crash() is None
    # Tvrdý pád během Qt event loopu: žádný finally ani Python excepthook se neprovede.
    code = """
import os
import resource
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from netmonitor.utils.logging_setup import setup_logging
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
setup_logging()
app = QApplication([])
QTimer.singleShot(0, os.abort)
app.exec()
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
               QT_QPA_PLATFORM="offscreen")
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                            capture_output=True, timeout=10)
    assert result.returncode == -entry.signal.SIGABRT
    assert b"Fatal Python error: Aborted" in crash_log.read_bytes()
    assert pending_crash() == crash_log.stat().st_size


@pytest.mark.parametrize("mode", ["normal", "exception", "diagnose", "diagnose-gui", "screenshot", "second-instance"])
def test_startup_routes_notice_after_lock_and_event_loop(crash_log, monkeypatch, mode):
    from netmonitor.core.config import AppConfig
    from netmonitor.utils.crash_notice import mark_clean_exit, mark_seen, pending_crash

    assert pending_crash() is None
    crash_log.write_bytes(crash_log.read_bytes() + b"novy pad\n")
    size = crash_log.stat().st_size
    cfg = AppConfig()
    monkeypatch.setattr(entry.cfgmod, "load_config", Mock(return_value=cfg))
    monkeypatch.setattr(entry.cfgmod, "save_config", Mock())
    monkeypatch.setattr(entry, "diagnose", Mock(return_value=0))
    monkeypatch.setattr(entry.signal, "signal", Mock())
    # Simuluje zápis aktuálního běhu: kontrola musí zachytit velikost ještě před ním.
    monkeypatch.setattr(entry, "setup_logging", lambda *_: crash_log.write_bytes(
        crash_log.read_bytes() + b"zapis aktualniho behu\n"))
    app = Mock()
    quit_slots = []
    app.aboutToQuit.connect.side_effect = quit_slots.append
    app.aboutToQuit.emit.side_effect = lambda: [slot() for slot in quit_slots]
    clean_exit = Mock(wraps=mark_clean_exit)
    monkeypatch.setattr(entry, "mark_clean_exit", clean_exit)
    monkeypatch.setattr("PySide6.QtWidgets.QApplication", Mock(return_value=app))
    monkeypatch.setattr("netmonitor.gui.icons.app_icon", Mock())
    monkeypatch.setattr("PySide6.QtWidgets.QSystemTrayIcon.isSystemTrayAvailable", lambda: False)
    monkeypatch.setattr("netmonitor.gui.diag.collect_gui_diagnostics", Mock(return_value={}))
    lock = Mock()
    lock.tryLock.return_value = mode != "second-instance"
    monkeypatch.setattr("PySide6.QtCore.QLockFile", Mock(return_value=lock))
    monkeypatch.setattr(QMessageBox, "information", Mock())
    ctrl = Mock(sync=None)
    monkeypatch.setattr("netmonitor.gui.controller.Controller", Mock(return_value=ctrl))
    monkeypatch.setattr("netmonitor.core.profiles.ProfileStore", Mock())
    monkeypatch.setattr("netmonitor.gui.sync_service.SyncService", Mock(return_value=None))
    widget = Mock()
    monkeypatch.setattr("netmonitor.gui.main_widget.MainWidget", Mock(return_value=widget))
    monkeypatch.setattr("netmonitor.gui.updater_ui.UpdateManager", Mock())
    monkeypatch.setattr("netmonitor.platform.current", Mock(return_value=Mock(autostart_enabled=lambda: False)))
    callbacks = []
    timer = Mock()
    timer.singleShot.side_effect = lambda delay, callback: callbacks.append(callback)
    monkeypatch.setattr("PySide6.QtCore.QTimer", timer)
    monkeypatch.setattr(entry, "_save_screenshot", Mock())

    def show_notice(snapshot_size):
        assert widget.show.called and lock.tryLock.called
        mark_seen(snapshot_size)
        assert seen_size() == size

    notice = Mock(side_effect=show_notice)
    monkeypatch.setattr(entry, "_show_crash_notice", notice)

    def event_loop():
        notice.assert_not_called()
        for callback in callbacks:
            callback()
        if mode == "exception":
            raise RuntimeError("pad v event loopu")
        if mode == "normal":
            assert quit_slots == [ctrl.shutdown, clean_exit]
            clean_exit.assert_not_called()
            app.aboutToQuit.emit()
            clean_exit.assert_called_once_with()
            assert seen_size() == crash_log.stat().st_size
        return 0

    app.exec.side_effect = event_loop
    flags = []
    if mode == "screenshot":
        flags = ["--screenshot", str(crash_log.parent / "widget.png")]
    elif mode in ("diagnose", "diagnose-gui"):
        flags = [f"--{mode}"]
    if mode == "exception":
        with pytest.raises(RuntimeError, match="pad v event loopu"):
            entry._main(flags)
        clean_exit.assert_not_called()
        assert pending_crash() == crash_log.stat().st_size
        return
    assert entry.main(flags) == 0
    if mode == "normal":
        clean_exit.assert_called_once_with()
        notice.assert_called_once_with(size)
        assert seen_size() == crash_log.stat().st_size
        assert pending_crash() is None
    else:
        assert clean_exit not in quit_slots
        clean_exit.assert_not_called()
        notice.assert_not_called()
        assert seen_size() < size


@pytest.mark.parametrize("action", ["open", "close", "cross"])
@pytest.mark.parametrize("lang", ["cs", "en"])
@pytest.mark.parametrize("during_start", [False, True])
def test_nonmodal_notice_opens_local_log(crash_log, monkeypatch, action, lang, during_start):
    from netmonitor.gui import i18n
    from netmonitor.utils.crash_notice import pending_crash

    app = QApplication.instance() or QApplication([])
    previous_language = i18n.language()
    i18n.set_language(lang)
    opener = Mock(return_value=True)
    monkeypatch.setattr(QDesktopServices, "openUrl", opener)
    assert pending_crash() is None
    crash_log.write_bytes(crash_log.read_bytes() + b"novy pad\n")
    size = pending_crash()
    assert size is not None
    # Nový pád mezi kontrolou a dialogem nesmí být potvrzen společně s předchozím.
    if during_start:
        crash_log.write_bytes(crash_log.read_bytes() + b"pad aktualniho behu\n")
    dialog = None
    try:
        dialog = entry._show_crash_notice(size)
        app.processEvents()
        assert isinstance(dialog, QMessageBox)
        assert dialog.isVisible() and not dialog.isModal()
        assert dialog.text() == i18n.tr("Minule NetMonitor spadl. Otevřít log?")
        assert dialog.textFormat() == Qt.TextFormat.PlainText
        buttons = {b.text(): b for b in dialog.buttons()}
        assert set(buttons) == {i18n.tr("Otevřít log"), i18n.tr("Zavřít")}
        assert seen_size() == size
        if action == "cross":
            dialog.close()
        else:
            buttons[i18n.tr("Otevřít log" if action == "open" else "Zavřít")].click()
        app.processEvents()
        assert not dialog.isVisible()
        if action == "open":
            opener.assert_called_once()
            url = opener.call_args.args[0]
            # Qt vrací na Windows cestu s „/“, proto porovnáváme Path, ne řetězce.
            assert url.isLocalFile() and Path(url.toLocalFile()) == crash_log
        else:
            opener.assert_not_called()
        assert pending_crash() == (crash_log.stat().st_size if during_start else None)
    finally:
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()
        i18n.set_language(previous_language)
