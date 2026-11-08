"""Regrese L12: WoL ověřuje i neaktivní cíle a zjištění MAC nezamrazí GUI."""
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from netmonitor.core import wol
from netmonitor.core import ping_engine
from netmonitor.core.config import AppConfig, PingTarget
from netmonitor.gui import controller as cmod, tools_dialog as tools
from netmonitor.gui.settings_dialog import TargetDialog

from .fakes import FakePinger

MAC = "02:00:00:00:00:01"
HOST = "192.0.2.10"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setProperty("tray_available", False)
    yield app


def wait_until(predicate, timeout=3):
    end = time.monotonic() + timeout
    while not predicate() and time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)
    assert predicate(), "Výsledek nebyl doručen do GUI včas"


@pytest.fixture
def ctrl(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(cmod, "select_pinger", lambda: FakePinger())
    monkeypatch.setattr(ping_engine, "select_pinger", lambda: FakePinger())
    cfg = AppConfig.from_dict({"network": {"external_api": False}})
    cfg.ping.targets = [PingTarget(name="Test PC", host=HOST, id="pc", enabled=False, mac=MAC)]
    c = cmod.Controller(cfg, tmp_path / "config.json", tmp_path / "history.sqlite3")
    yield c
    c.shutdown()
    for dialog in list(getattr(tools, "_wol_dialogs", [])):
        if dialog.ctrl is c:
            tools._wol_dialogs.remove(dialog)
            dialog.close()
            dialog.deleteLater()


@pytest.fixture(autouse=True)
def no_packets(monkeypatch):
    send = Mock(return_value=["192.0.2.255:9"])
    monkeypatch.setattr(wol, "send", send)
    # Úspěch nesmí otevřít modální informační dialog.
    monkeypatch.setattr(QMessageBox, "information", Mock(side_effect=AssertionError("Modální potvrzení WoL")))
    yield send


@pytest.fixture
def slow_lookup(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    threads = []

    def lookup(ip):
        assert ip == HOST
        threads.append(threading.current_thread())
        entered.set()
        assert threading.current_thread() is not threading.main_thread(), "ARP běží v GUI vlákně"
        assert release.wait(3), "Test neuvolnil ARP úlohu"
        return MAC

    monkeypatch.setattr(wol, "arp_lookup", lookup)
    yield entered, release, threads
    release.set()


def test_disabled_wake_really_pings(ctrl, monkeypatch, no_packets):
    """Regrese: původní test_now tiše ignoroval neaktivní cíl; test používá původní API wake."""
    target = ctrl.cfg.ping.targets[0]
    ctrl.engine.set_targets([target])
    assert ctrl.engine.test_now(target.id) is False
    monkeypatch.setattr(tools, "QTimer", SimpleNamespace(singleShot=lambda delay, callback: QTimer.singleShot(0, callback)))
    # Staré potvrzení necháme projít, abychom ověřili přímo chybějící ping.
    monkeypatch.setattr(QMessageBox, "information", Mock())
    tools.wake(ctrl, target)
    wait_until(lambda: ctrl.pinger.calls.get(HOST) == 3)
    no_packets.assert_called_once()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("reply", ["ok:5", "timeout"])
def test_wake_checks_even_disabled_or_paused_target(ctrl, monkeypatch, no_packets, caplog, enabled, reply):
    """Regrese: neaktivní nebo pozastavený cíl opravdu dostane všechny tři pingy."""
    target = ctrl.cfg.ping.targets[0]
    target.enabled = enabled
    ctrl.pinger.default = reply
    ctrl.engine.set_targets([target])
    ctrl.engine.pause(True)
    if not enabled:
        assert ctrl.engine.test_now(target.id) is False
    assert tools.WOL_CHECK_DELAYS_MS == (15_000, 30_000, 60_000)
    monkeypatch.setattr(tools, "WOL_CHECK_DELAYS_MS", (5, 15, 25))
    scheduled = []

    def schedule(delay, callback):
        scheduled.append(delay)
        QTimer.singleShot(delay, callback)

    monkeypatch.setattr(tools, "QTimer", SimpleNamespace(singleShot=schedule))
    caplog.set_level(logging.INFO, logger=tools.__name__)
    tools.wake(ctrl, target)
    dialog = tools._wol_dialogs[-1]
    assert not dialog.isModal()
    assert "odeslán" in dialog.status.text()
    assert scheduled == [5, 15, 25]
    # Zavření stavového okna nesmí zastavit ověřování.
    dialog.close()
    wait_until(lambda: not dialog.busy())
    assert ctrl.pinger.calls == {HOST: 3}
    assert ctrl.cfg.ping.targets[0].enabled is enabled
    assert "Ověření" in dialog.status.text()
    assert ("dostupný" if reply.startswith("ok:") else "neodpovídá") in dialog.status.text()
    assert caplog.text.count("Ověření") == 3
    no_packets.assert_called_once_with(MAC, None, None, None)


def test_detect_mac_runs_off_gui_thread(ctrl, slow_lookup):
    """Regrese: pomalé ARP i DNS/ping běží ve workeru, event loop zatím odpovídá."""
    entered, release, threads = slow_lookup
    resolver_threads = []
    ping_threads = []

    def resolve(host):
        resolver_threads.append(threading.current_thread())
        return HOST, ""

    ctrl.resolver.resolve = resolve
    ctrl.pinger.default = lambda: ping_threads.append(threading.current_thread()) or "ok:5"
    # Původní konstruktor umožní spustit stejnou regresi i proti upstream/main.
    dialog = TargetDialog(ctrl.cfg.ping.targets[0])
    dialog.ctrl = ctrl
    dialog.btn_mac.setEnabled(True)
    dialog.mac.clear()
    dialog.show()
    dialog.btn_mac.click()
    wait_until(entered.is_set)
    assert not dialog.btn_mac.isEnabled()
    assert dialog.btn_mac.text() == "Zjišťuji…"
    heartbeat = []
    QTimer.singleShot(0, lambda: heartbeat.append(True))
    wait_until(lambda: bool(heartbeat))
    assert threads and resolver_threads and ping_threads
    assert all(t is not threading.main_thread() for t in threads + resolver_threads + ping_threads)
    release.set()
    wait_until(dialog.btn_mac.isEnabled)
    assert dialog.btn_mac.text() == "Zjistit"
    assert dialog.mac.text() == MAC and not dialog.err.text()
    dialog.close()


@pytest.mark.parametrize("change", ["host", "mac", "close", "delete"])
def test_detect_mac_ignores_stale_result(ctrl, slow_lookup, change):
    """Regrese: pozdní výsledek nepřepíše upravená data ani nesahá na zrušený editor."""
    entered, release, _ = slow_lookup
    dialog = TargetDialog(ctrl.cfg.ping.targets[0], ctrl=ctrl)
    dialog.mac.clear()
    dialog.show()
    dialog.btn_mac.click()
    wait_until(entered.is_set)
    if change == "host":
        dialog.host.setText("198.51.100.10")
    elif change == "mac":
        dialog.mac.setText("02:00:00:00:00:02")
    elif change == "close":
        dialog.reject()
    else:
        from shiboken6 import delete
        delete(dialog)
    release.set()
    wait_until(lambda: not ctrl._busy)
    if change != "delete":
        assert dialog.mac.text() == ("02:00:00:00:00:02" if change == "mac" else "")
        dialog.close()


@pytest.mark.parametrize("failure", [None, RuntimeError("test ARP error")])
def test_detect_mac_failure_restores_button(ctrl, monkeypatch, failure):
    """Regrese: nenalezená MAC i výjimka obnoví tlačítko a zobrazí chybu."""
    def lookup(ip):
        if failure:
            raise failure
        return None

    monkeypatch.setattr(wol, "arp_lookup", lookup)
    dialog = TargetDialog(ctrl.cfg.ping.targets[0], ctrl=ctrl)
    dialog.mac.clear()
    dialog.btn_mac.click()
    wait_until(dialog.btn_mac.isEnabled)
    assert dialog.btn_mac.text() == "Zjistit"
    assert dialog.err.text() and not dialog.mac.text()
    dialog.close()


def test_wake_discovers_missing_mac_in_background(ctrl, slow_lookup, monkeypatch, no_packets):
    """Regrese: také WoL bez MAC hledá mimo GUI a ptá se pouze na uložení adresy."""
    target = ctrl.cfg.ping.targets[0]
    target.mac = ""
    entered, release, threads = slow_lookup
    monkeypatch.setattr(tools, "WOL_CHECK_DELAYS_MS", (5, 15, 25))
    questions = []

    def question(*args):
        questions.append(threading.current_thread())
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", question)
    tools.wake(ctrl, target)
    dialog = tools._wol_dialogs[-1]
    wait_until(entered.is_set)
    assert "Zjišťuji" in dialog.status.text()
    assert threads[0] is not threading.main_thread()
    # Opakované kliknutí během hledání nespustí druhé odeslání.
    tools.wake(ctrl, target)
    assert tools._wol_dialogs[-1] is dialog
    no_packets.assert_not_called()
    release.set()
    wait_until(lambda: not dialog.busy())
    assert questions == [threading.main_thread()]
    assert ctrl.cfg.ping.targets[0].mac == MAC
    assert ctrl.pinger.calls == {HOST: 4}   # osvěžení ARP + 3 ověření
    no_packets.assert_called_once_with(MAC, None, None, None)


def test_wake_send_error_does_not_schedule_checks(ctrl, monkeypatch, no_packets):
    """Regrese: po chybě odeslání neslibovat ani nespouštět ověřování."""
    no_packets.side_effect = OSError("test send error")
    warning = Mock()
    monkeypatch.setattr(QMessageBox, "warning", warning)
    tools.wake(ctrl, ctrl.cfg.ping.targets[0])
    dialog = tools._wol_dialogs[-1]
    assert not dialog.busy()
    assert "nezdařilo" in dialog.status.text()
    assert not ctrl.pinger.calls
    warning.assert_called_once()


@pytest.mark.parametrize("change", ["decline", "host", "remove"])
def test_wake_discovery_does_not_send_without_confirmation(ctrl, slow_lookup, monkeypatch, no_packets, change):
    """Regrese: odmítnutí uložení nebo změna cíle během hledání nesmí odeslat WoL."""
    target = ctrl.cfg.ping.targets[0]
    target.mac = ""
    entered, release, _ = slow_lookup
    question = Mock(return_value=QMessageBox.StandardButton.No)
    monkeypatch.setattr(QMessageBox, "question", question)
    tools.wake(ctrl, target)
    dialog = tools._wol_dialogs[-1]
    wait_until(entered.is_set)
    if change == "host":
        target.host = "198.51.100.10"
    elif change == "remove":
        ctrl.cfg.ping.targets.clear()
    release.set()
    wait_until(lambda: not dialog.busy())
    no_packets.assert_not_called()
    assert question.call_count == (1 if change == "decline" else 0)
    assert not target.mac


@pytest.fixture
def scheduled_checks(monkeypatch):
    """Řídí doručování jednotlivých kontrol bez skutečného čekání 15/30/60 sekund."""
    callbacks = []
    monkeypatch.setattr(tools, "QTimer", SimpleNamespace(singleShot=lambda delay, callback: callbacks.append(callback)))
    return callbacks


@pytest.mark.parametrize("visible", [False, True])
@pytest.mark.parametrize("replies", [("ok:5", "timeout", "timeout"), ("timeout", "ok:5", "timeout"),
                                    ("timeout", "timeout", "ok:5"), ("ok:5", "ok:5", "ok:5")])
def test_wake_keeps_success_and_notifies_when_hidden(ctrl, scheduled_checks, visible, replies):
    """Regrese R2: skryté okno oznámí první úspěch jen jednou a pozdější timeout jej nepřepíše."""
    messages = []
    ctrl.notification.connect(messages.append)
    tools.wake(ctrl, ctrl.cfg.ping.targets[0])
    dialog = tools._wol_dialogs[-1]
    if not visible:
        dialog.close()
    first_success = next(i for i, reply in enumerate(replies, 1) if reply.startswith("ok:"))
    for attempt, (callback, reply) in enumerate(zip(scheduled_checks, replies), 1):
        ctrl.pinger.default = reply
        callback()
        wait_until(lambda: dialog.pending == 3 - attempt)
        assert len(messages) == int(not visible and attempt >= first_success)
    assert ctrl.pinger.calls == {HOST: 3}
    assert "dostupný (5.0 ms)" in dialog.status.text()
    if replies[-1] == "timeout":
        assert "Cíl odpověděl při dřívějším ověření." in dialog.status.text()
    assert "neodpovídá" not in dialog.status.text()
    if visible:
        assert not messages
    else:
        assert len(messages) == 1
        assert f"Ověření {first_success}/3" in messages[0].message
        assert "dostupný (5.0 ms)" in messages[0].message
        assert messages[-1].kind == "wol" and messages[-1].key == "wol:pc"
        assert "Wake-on-LAN" in messages[-1].title


@pytest.mark.parametrize("status, error, expected", [
    (ping_engine.PingStatus.TIMEOUT, "", "vypršel časový limit"),
    (ping_engine.PingStatus.HOST_UNREACHABLE, "", "host nedostupný"),
    (ping_engine.PingStatus.NET_UNREACHABLE, "", "síť nedostupná"),
    (ping_engine.PingStatus.DNS_ERROR, "", "doménu nelze přeložit"),
    (ping_engine.PingStatus.ERROR, "", "chyba"),
    (ping_engine.PingStatus.ERROR, "Vlastní popis chyby", "Vlastní popis chyby"),
])
def test_wake_uses_czech_error_descriptions(ctrl, status, error, expected):
    """Regrese R3: stavový text používá ERR_DESC, podrobná chyba má přednost."""
    dialog = tools.WolDialog(ctrl, ctrl.cfg.ping.targets[0])
    tools._wol_dialogs.append(dialog)
    messages = []
    ctrl.notification.connect(messages.append)
    dialog.pending = 1
    dialog._checked(3, ping_engine.PingResult(status, error=error))
    assert f"neodpovídá ({expected})" in dialog.status.text()
    assert messages[-1].message == dialog.status.text()


@pytest.mark.parametrize("state", ["enabled", "disabled", "removed", "host_changed"])
def test_wake_refreshes_only_current_enabled_target(ctrl, monkeypatch, state):
    """Regrese R4: po ověření se aktivní cíl obnoví v plánovači podle aktuální konfigurace."""
    target = ctrl.cfg.ping.targets[0]
    target.enabled = state != "enabled"     # změna enabled až po vytvoření okna
    dialog = tools.WolDialog(ctrl, target)
    tools._wol_dialogs.append(dialog)
    target.enabled = state != "disabled"
    if state == "removed":
        ctrl.cfg.ping.targets.clear()
    elif state == "host_changed":
        target.host = "198.51.100.10"
    test_now = Mock()
    monkeypatch.setattr(ctrl, "test_now", test_now)
    dialog.pending = 1
    dialog._checked(3, ping_engine.PingResult(ping_engine.PingStatus.OK, rtt_ms=5))
    if state == "enabled":
        test_now.assert_called_once_with("pc")
    else:
        test_now.assert_not_called()


def test_wake_reuses_completed_visible_window_and_resets_result(ctrl, scheduled_checks, no_packets):
    """Regrese R6: další probuzení použije stejné viditelné okno a nepřevezme starý úspěch."""
    target = ctrl.cfg.ping.targets[0]
    tools.wake(ctrl, target)
    dialog = tools._wol_dialogs[-1]
    for attempt, callback in enumerate(list(scheduled_checks), 1):
        callback()
        wait_until(lambda: dialog.pending == 3 - attempt)
    assert dialog.isVisible() and not dialog.busy()
    assert "dostupný" in dialog.status.text()
    count = len(tools._wol_dialogs)
    target.name = "Přejmenovaný test PC"
    target.mac = "02:00:00:00:00:02"
    ctrl.pinger.default = "timeout"
    tools.wake(ctrl, target)
    assert len(tools._wol_dialogs) == count
    assert tools._wol_dialogs[-1] is dialog
    assert dialog.target.name == target.name
    assert target.name in dialog.windowTitle()
    assert dialog.pending == 3
    assert no_packets.call_count == 2
    no_packets.assert_called_with(target.mac, None, None, None)
    for attempt, callback in enumerate(scheduled_checks[3:], 1):
        callback()
        wait_until(lambda: dialog.pending == 3 - attempt)
    assert "neodpovídá" in dialog.status.text()
    assert "Cíl odpověděl" not in dialog.status.text()


@pytest.mark.parametrize("when", ["during_lookup", "during_question"])
def test_wake_preserves_manually_entered_mac(ctrl, slow_lookup, monkeypatch, scheduled_checks, no_packets, when):
    """Regrese R6: ruční MAC zadaná během ARP nebo potvrzovacího dialogu má přednost."""
    target = ctrl.cfg.ping.targets[0]
    target.mac = ""
    manual_mac = "02:00:00:00:00:02"
    entered, release, _ = slow_lookup

    def enter_mac():
        cfg = ctrl.cfg.copy()
        cfg.ping.targets[0].mac = manual_mac
        ctrl.update_config(cfg, save=False)

    def question(*args):
        enter_mac()
        return QMessageBox.StandardButton.Yes

    ask = Mock(side_effect=question)
    monkeypatch.setattr(QMessageBox, "question", ask)
    tools.wake(ctrl, target)
    dialog = tools._wol_dialogs[-1]
    wait_until(entered.is_set)
    if when == "during_lookup":
        enter_mac()
    release.set()
    wait_until(lambda: not dialog.detecting)
    assert ctrl.cfg.target_by_id("pc").mac == manual_mac
    no_packets.assert_called_once_with(manual_mac, None, None, None)
    assert ask.call_count == (1 if when == "during_question" else 0)
    for attempt, callback in enumerate(scheduled_checks, 1):
        callback()
        wait_until(lambda: dialog.pending == 3 - attempt)


def test_wake_refreshes_widget_before_regular_interval(ctrl):
    """Regrese R4: test_now po ověření změní skutečný widget bez čekání na 60s interval."""
    from netmonitor.gui.main_widget import MainWidget

    target = ctrl.cfg.ping.targets[0]
    target.enabled = True
    target.interval_s = 60
    ctrl.pinger.default = "timeout"
    widget = MainWidget(ctrl)
    ctrl.engine.set_targets([target])
    ctrl.engine.start()
    try:
        wait_until(lambda: widget.ping.rows["pc"]._value[0] == "TIMEOUT")
        ctrl.pinger.default = "ok:9"
        dialog = tools.WolDialog(ctrl, target)
        tools._wol_dialogs.append(dialog)
        dialog.pending = 1
        dialog._checked(1, ping_engine.PingResult(ping_engine.PingStatus.OK, rtt_ms=9))
        wait_until(lambda: widget.ping.rows["pc"]._value[0] == "9.0 ms")
    finally:
        ctrl.engine.stop()
        widget.close()


@pytest.mark.parametrize("visible", [False, True])
def test_wake_notifies_failure_only_after_all_checks(ctrl, scheduled_checks, visible):
    """Regrese: při třech neúspěších se oznámení odešle až po posledním výsledku, jen ve skrytém okně."""
    messages = []
    ctrl.notification.connect(messages.append)
    ctrl.pinger.default = "timeout"
    tools.wake(ctrl, ctrl.cfg.ping.targets[0])
    dialog = tools._wol_dialogs[-1]
    if not visible:
        dialog.close()
    for attempt, callback in enumerate(scheduled_checks, 1):
        callback()
        wait_until(lambda: dialog.pending == 3 - attempt)
        assert len(messages) == int(not visible and attempt == 3)
    if not visible:
        assert "Ověření 3/3" in messages[0].message
        assert "neodpovídá (vypršel časový limit)" in messages[0].message


def test_wake_does_not_repeat_visible_success_after_hiding(ctrl, scheduled_checks):
    """Regrese: úspěch už zobrazený v okně se po jeho zavření znovu neoznamuje při dalších pokusech."""
    messages = []
    ctrl.notification.connect(messages.append)
    tools.wake(ctrl, ctrl.cfg.ping.targets[0])
    dialog = tools._wol_dialogs[-1]
    scheduled_checks[0]()
    wait_until(lambda: dialog.pending == 2)
    assert "dostupný" in dialog.status.text() and not messages
    dialog.close()
    for attempt, callback in enumerate(scheduled_checks[1:], 2):
        callback()
        wait_until(lambda: dialog.pending == 3 - attempt)
        assert not messages


def test_wake_notification_limit_resets_for_reused_window(ctrl, scheduled_checks):
    """Regrese: opakované probuzení ve stejném okně má vlastní limit jedné notifikace."""
    messages = []
    ctrl.notification.connect(messages.append)
    target = ctrl.cfg.ping.targets[0]
    tools.wake(ctrl, target)
    dialog = tools._wol_dialogs[-1]
    dialog.close()
    for attempt, callback in enumerate(list(scheduled_checks), 1):
        callback()
        wait_until(lambda: dialog.pending == 3 - attempt)
        assert len(messages) == 1
    assert "dostupný" in messages[0].message
    dialog.show()
    ctrl.pinger.default = "timeout"
    tools.wake(ctrl, target)
    assert tools._wol_dialogs[-1] is dialog
    dialog.close()
    for attempt, callback in enumerate(scheduled_checks[3:], 1):
        callback()
        wait_until(lambda: dialog.pending == 3 - attempt)
        assert len(messages) == 1 + int(attempt == 3)
    assert "neodpovídá" in messages[1].message
