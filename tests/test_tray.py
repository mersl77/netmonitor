"""Regrese menu tray: stejné Qt objekty i funkční akce po změnách stavu."""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from shiboken6 import isValid  # noqa: E402

from netmonitor.core.config import AppConfig, MODE_ONTOP  # noqa: E402
from netmonitor.core.network_info import InterfaceInfo, NetworkSnapshot  # noqa: E402
from netmonitor.core.updater import UpdateInfo  # noqa: E402
from netmonitor.gui.main_widget import MainWidget  # noqa: E402
from netmonitor.gui.tray import Tray  # noqa: E402

from .fakes import FakePinger  # noqa: E402


def drain_events():
    QCoreApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()


def all_actions(menu):
    result = []
    for action in menu.actions():
        result.append(action)
        if action.menu() is not None:
            result.extend(all_actions(action.menu()))
    return result


def action_named(menu, text):
    return next(a for a in all_actions(menu) if a.text() == text)


@pytest.fixture
def tray_widget(tmp_path, monkeypatch):
    import netmonitor.gui.controller as cmod

    app = QApplication.instance() or QApplication([])
    old_tray = app.property("tray_available")
    app.setProperty("tray_available", True)
    monkeypatch.setattr(cmod, "select_pinger", lambda: FakePinger())
    cfg = AppConfig.from_dict({"network": {"external_api": False, "gateway_probe": False},
                              "ping": {"targets": [{"id": "test", "name": "Test",
                                                    "host": "192.0.2.1", "enabled": False}]}})
    ctrl = cmod.Controller(cfg, tmp_path / "config.json", tmp_path / "h.sqlite3")
    widget = MainWidget(ctrl)
    tray = Tray(widget)
    monkeypatch.setattr(tray, "showMessage", lambda *args: None)
    yield tray, widget, ctrl
    tray.hide()
    widget.close()
    ctrl.shutdown()
    tray.deleteLater()
    widget.deleteLater()
    drain_events()
    app.setProperty("tray_available", old_tray)


@pytest.mark.parametrize("change", ["config", "visibility"])
def test_tray_menu_and_actions_survive_state_change(tray_widget, change):
    """Regrese L14: výměna menu rušila akce a ID exportovaná přes dbusmenu."""
    tray, widget, ctrl = tray_widget
    menu = tray.contextMenu()
    actions = all_actions(menu)
    calls = []
    for name in ("open_settings", "open_history", "open_speedtest", "open_about"):
        getattr(widget, name).connect(lambda *args, n=name: calls.append((n, args)))

    if change == "config":
        cfg = ctrl.cfg.copy()
        cfg.appearance.locked = not cfg.appearance.locked
        ctrl.update_config(cfg)
    else:
        widget.toggle_visible()
    drain_events()  # v původní implementaci zde proběhlo deleteLater starého menu
    assert tray.contextMenu() is menu
    assert all(isValid(action) for action in actions)
    assert all_actions(menu) == actions

    for label in ("Nastavení…", "Historie…", "Test rychlosti internetu…", "O aplikaci…"):
        action_named(menu, label).trigger()
    assert calls == [("open_settings", ()), ("open_history", ("",)),
                     ("open_speedtest", ()), ("open_about", ())]

    toggle = menu.actions()[0]
    for _ in range(2):
        visible = widget.isVisible()
        toggle.trigger()
        drain_events()
        assert widget.isVisible() is not visible
        assert toggle.text() == ("Skrýt do oznamovací oblasti" if widget.isVisible() else "Zobrazit widget")
        assert tray.contextMenu() is menu
        assert all_actions(menu) == actions


def test_tray_menu_syncs_checks_and_current_interface(tray_widget):
    """Regrese L14: trvalé akce musí číst aktuální konfiguraci a aktivní adaptér."""
    tray, widget, ctrl = tray_widget
    menu = tray.contextMenu()
    locked = action_named(menu, "Uzamknout pozici")
    old_locked = ctrl.cfg.appearance.locked
    locked.trigger()
    drain_events()
    assert tray.contextMenu() is menu
    assert locked.isChecked() is not old_locked
    assert ctrl.cfg.appearance.locked is not old_locked

    cfg = ctrl.cfg.copy()
    cfg.appearance.locked = old_locked
    cfg.appearance.mode = MODE_ONTOP
    cfg.appearance.click_through = True
    cfg.appearance.glass_alpha = 0.4
    ctrl.update_config(cfg)
    drain_events()
    assert locked.isChecked() is old_locked
    assert action_named(menu, "Vždy navrchu").isChecked()
    assert not action_named(menu, "Na ploše (pod okny)").isChecked()
    assert action_named(menu, "Ignorovat myš (click-through)").isChecked()
    assert action_named(menu, "60 %").isChecked()
    assert not action_named(menu, "20 %").isChecked()
    action_named(menu, "Ignorovat myš (click-through)").trigger()
    assert not ctrl.cfg.appearance.click_through
    action_named(menu, "Na ploše (pod okny)").trigger()
    assert action_named(menu, "Na ploše (pod okny)").isChecked()
    assert not action_named(menu, "Vždy navrchu").isChecked()
    action_named(menu, "20 %").trigger()
    assert ctrl.cfg.appearance.glass_alpha == 0.8
    assert action_named(menu, "20 %").isChecked()
    assert not action_named(menu, "60 %").isChecked()
    assert tray.contextMenu() is menu

    interfaces = []
    widget.open_ipconfig.connect(interfaces.append)
    ip_action = action_named(menu, "Nastavit IP adresu…")
    for name in ("test-adapter-1", "test-adapter-2", ""):
        ctrl.network = NetworkSnapshot(InterfaceInfo(name, name) if name else None)
        ip_action.trigger()
    assert interfaces == ["test-adapter-1", "test-adapter-2", ""]


def test_tray_update_action_keeps_identity(tray_widget):
    """Regrese L14: dostupná aktualizace pouze mění text a viditelnost akcí."""
    tray, widget, ctrl = tray_widget
    menu = tray.contextMenu()
    actions = all_actions(menu)
    calls = []
    widget.open_update.connect(lambda: calls.append("update"))
    update_action = None
    for version in ("9.8.7", "9.8.8", None):
        info = UpdateInfo(version, "https://example.com/release") if version else None
        widget.set_update(info)
        tray.set_update(info)
        drain_events()
        assert tray.contextMenu() is menu
        assert all_actions(menu) == actions
        if version:
            action = action_named(menu, f"⬆  Aktualizovat na verzi {version}…")
            assert action.isVisible()
            if update_action is not None:
                assert action is update_action
            update_action = action
            action.trigger()
            assert menu.actions()[menu.actions().index(action) + 1].isVisible()
        else:
            assert update_action is not None and not update_action.isVisible()
            assert not menu.actions()[menu.actions().index(update_action) + 1].isVisible()
    assert calls == ["update", "update"]


def test_widget_context_menu_uses_same_state_rules(tray_widget):
    """Sdílená stavba menu zachovává chování kontextového menu widgetu."""
    tray, widget, ctrl = tray_widget
    cfg = ctrl.cfg.copy()
    cfg.appearance.click_through = True
    ctrl.update_config(cfg)
    menu = widget.build_menu(widget)
    assert next(a for a in menu.actions() if a.isVisible()).text() == "Nastavení…"
    assert not action_named(menu, "Ignorovat myš (click-through)").isEnabled()
    assert action_named(tray.contextMenu(), "Ignorovat myš (click-through)").isEnabled()
    assert action_named(menu, "Skrýt do oznamovací oblasti")
