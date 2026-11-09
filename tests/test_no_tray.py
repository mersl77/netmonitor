"""Regrese B07/L18: bez tray musí widget zůstat ovladatelný a upozornění viditelná."""
import logging
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from netmonitor.core.config import AppConfig, export_config
from netmonitor.core.notifier import Notification
from netmonitor.gui import i18n, theme
from netmonitor.gui.controller import Controller
from netmonitor.gui.main_widget import MainWidget
from netmonitor.gui.settings_dialog import SettingsDialog
from netmonitor.gui.tray import Tray

from .fakes import FakePinger


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    previous = (app.property("tray_available"), app.applicationName(), app.organizationName(),
                app.applicationVersion(), app.quitOnLastWindowClosed(), app.windowIcon(),
                app.property("xcb_fallback"), i18n.language())
    app.setProperty("tray_available", False)
    try:
        yield app
    finally:
        tray, name, organization, version, quit_on_close, icon, fallback, language = previous
        app.setProperty("tray_available", tray)
        app.setApplicationName(name)
        app.setOrganizationName(organization)
        app.setApplicationVersion(version)
        app.setQuitOnLastWindowClosed(quit_on_close)
        app.setWindowIcon(icon)
        app.setProperty("xcb_fallback", fallback)
        i18n.set_language(language)


@pytest.fixture
def cfg():
    return AppConfig.from_dict({"network": {"external_api": False}, "ping": {"targets": []}})


@pytest.fixture
def controller_factory(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr("netmonitor.gui.controller.select_pinger", FakePinger)
    monkeypatch.setattr("netmonitor.gui.controller.NetworkInfoProvider.list_interfaces", lambda _self: [])
    controllers = []

    def create(cfg):
        c = Controller(cfg, tmp_path / "config.json", tmp_path / f"h{len(controllers)}.sqlite3")
        controllers.append(c)
        return c

    yield create
    for c in controllers:
        c.shutdown()


def test_menu_and_toggle_cannot_hide_without_tray(cfg, controller_factory):
    """Regrese B07: menu ani přímé přepnutí nesmí ztratit jediný widget."""
    w = MainWidget(controller_factory(cfg))
    w.show()
    try:
        for for_tray in (False, True):
            menu = w.build_menu(for_tray=for_tray)
            assert not any("Skrýt do oznamovací oblasti" in a.text() and a.isEnabled()
                           for a in menu.actions())
            ct = next(a for a in menu.actions() if "click-through" in a.text())
            assert not ct.isEnabled()
        w.toggle_visible()
        assert w.isVisible()
    finally:
        w.close()


@pytest.mark.parametrize("route", ["initial", "update", "menu", "settings", "import"])
@pytest.mark.parametrize("tray_available", [False, True])
def test_click_through_all_config_paths(qapp, cfg, controller_factory, tmp_path, monkeypatch,
                                       route, tray_available):
    """Regrese B07: import i obecný update_config procházejí stejnou ochranou jako start."""
    qapp.setProperty("tray_available", tray_available)
    cfg.appearance.click_through = route == "initial"
    c = controller_factory(cfg)
    w = MainWidget(c)
    dialog = None
    try:
        if route == "update":
            updated = c.cfg.copy()
            updated.appearance.click_through = True
            c.update_config(updated, save=False)
        elif route == "menu":
            w._set_click_through(True)
        elif route in ("settings", "import"):
            dialog = SettingsDialog(c, w)
            if route == "settings":
                cb = next(cb for cb, section, key in dialog._binds
                          if section == "appearance" and key == "click_through")
                assert cb.isEnabled() is tray_available
                # I programové zapnutí zakázaného checkboxu musí projít ochranou.
                cb.setChecked(True)
            else:
                imported = c.cfg.copy()
                imported.appearance.click_through = True
                path = tmp_path / "import.json"
                export_config(imported, path)
                monkeypatch.setattr("netmonitor.gui.settings_dialog.QFileDialog.getOpenFileName",
                                    lambda *a, **kw: (str(path), "JSON (*.json)"))
                dialog._import()
        assert c.cfg.appearance.click_through is tray_available
        assert bool(w.windowFlags() & Qt.WindowType.WindowTransparentForInput) is tray_available
    finally:
        if dialog is not None:
            dialog.close()
        w.close()


@pytest.mark.parametrize("source", ["cli", "config"])
@pytest.mark.parametrize("tray_available", [False, True])
def test_minimized_start(qapp, cfg, controller_factory, monkeypatch, caplog, source, tray_available):
    """Regrese B07: skutečná startovací cesta bez tray ukáže widget a zapíše varování."""
    from netmonitor import main as entry

    cfg.general.start_minimized = source == "config"
    cfg.general.language = "en"  # main změní jazyk; fixture jej musí vrátit dalším testům.
    qapp.setProperty("tray_available", tray_available)
    c = controller_factory(cfg)
    monkeypatch.setattr(entry.cfgmod, "load_config", lambda _path: cfg)
    monkeypatch.setattr(entry.cfgmod, "save_config", lambda *a: None)
    monkeypatch.setattr(entry, "setup_logging", lambda *_a: None)
    monkeypatch.setattr(entry.signal, "signal", lambda *a: None)
    monkeypatch.setattr("netmonitor.gui.controller.Controller", lambda *a: c)
    monkeypatch.setattr(c, "start", lambda: None)
    monkeypatch.setattr("netmonitor.gui.updater_ui.UpdateManager", Mock())
    monkeypatch.setattr("netmonitor.platform.current", lambda: Mock(autostart_enabled=lambda: False))
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: tray_available)
    monkeypatch.setattr(QSystemTrayIcon, "show", lambda _self: None)
    # main normálně vytváří QApplication; v offscreen testu sdílí již existující instanci.
    monkeypatch.setattr("PySide6.QtWidgets.QApplication", Mock(wraps=QApplication, return_value=qapp))
    widgets = []

    def create_widget(ctrl):
        w = MainWidget(ctrl)
        widgets.append(w)
        return w

    monkeypatch.setattr("netmonitor.gui.main_widget.MainWidget", create_widget)

    def check_visibility():
        assert widgets[0].isVisible() is not tray_available
        return 0

    monkeypatch.setattr(qapp, "exec", check_visibility)
    with caplog.at_level(logging.WARNING):
        try:
            assert entry.main(["--minimized"] if source == "cli" else []) == 0
            assert any("minimaliz" in r.message and "nedostupná" in r.message
                       for r in caplog.records) is not tray_available
        finally:
            qapp.aboutToQuit.disconnect(c.shutdown)
            for w in widgets:
                w.close()


@pytest.mark.parametrize("kind,critical", [("down", True), ("up", False), ("wan_change", False)])
def test_notification_without_tray(qapp, cfg, controller_factory, caplog, kind, critical):
    """Regrese L18: upozornění se bez tray zaznamená a dočasně zvýrazní ve widgetu."""
    c = controller_factory(cfg)
    w = MainWidget(c)
    w.show()
    n = Notification(kind, "test", "Test upozornění <test>", "Zařízení 192.0.2.1", critical)
    try:
        with caplog.at_level(logging.INFO):
            c.notification.emit(n)
        qapp.processEvents()
        assert w.notification_notice.isVisible()
        assert w.notification_notice.textFormat() == Qt.TextFormat.PlainText
        assert n.title in w.notification_notice.text() and n.message in w.notification_notice.text()
        assert any(n.title in r.message and n.message in r.message for r in caplog.records)
        assert w._notification_timer.isActive()
        # Méně závažná zpráva nesmí přepsat právě zobrazený výpadek.
        c.notification.emit(Notification("up", "test", "Obnoveno", "192.0.2.1 odpovídá"))
        qapp.processEvents()
        assert (n.title if critical else "Obnoveno") in w.notification_notice.text()
        w._notification_timer.timeout.emit()
        assert w.notification_notice.isHidden()
    finally:
        w.close()


def test_notification_batch_count_expires(qapp, cfg, controller_factory):
    """Regrese L34 review: dávka jednoho ticku ukáže hlavní zprávu a počet nezobrazených."""
    c = controller_factory(cfg)
    w = MainWidget(c)
    w.show()
    try:
        assert w._notification_timer.interval() == 8000
        w._notification_timer.setInterval(10)
        for title in ("První", "Druhé", "Poslední"):
            c.notification.emit(Notification("down", title, title, "192.0.2.1 neodpovídá", True))
        qapp.processEvents()
        assert "Poslední" in w.notification_notice.text()
        assert "(+2 dalších)" in w.notification_notice.text()
        assert "První" not in w.notification_notice.text()
        QTest.qWait(30)
        assert w.notification_notice.isHidden()
        c.notification.emit(Notification("up", "test", "Obnoveno", "192.0.2.1 odpovídá"))
        qapp.processEvents()
        assert "Obnoveno" in w.notification_notice.text()
        assert "dalších" not in w.notification_notice.text()
        assert w.notification_notice.isVisible()
    finally:
        w.close()


def test_separate_notification_ticks_do_not_accumulate(qapp, cfg, controller_factory):
    """Regrese L34 kolo 3: samostatně zobrazené zprávy v dalších ticích nezvyšují počet dalších."""
    c = controller_factory(cfg)
    w = MainWidget(c)
    w.show()
    try:
        for title in ("První", "Druhé", "Třetí"):
            c.notification.emit(Notification("wan_change", title, title, "192.0.2.1"))
            # Oddělené průchody event loop, stále uvnitř osmi sekund předchozí zprávy.
            qapp.processEvents()
            assert title in w.notification_notice.text()
            assert "dalších" not in w.notification_notice.text()
            assert w._notification_timer.isActive()
    finally:
        w.close()


@pytest.mark.parametrize("critical_first", [False, True])
@pytest.mark.parametrize("separate_ticks", [False, True])
def test_critical_notification_keeps_priority(qapp, cfg, controller_factory, critical_first, separate_ticks):
    """Regrese L34 kolo 3: výpadek si zachová hlavní text i barvu před obnovením v témže okně."""
    c = controller_factory(cfg)
    w = MainWidget(c)
    w.show()
    down = Notification("down", "test", "Výpadek", "192.0.2.1 neodpovídá", True)
    up = Notification("up", "test", "Obnoveno", "192.0.2.1 odpovídá")
    try:
        first, second = (down, up) if critical_first else (up, down)
        c.notification.emit(first)
        if separate_ticks:
            qapp.processEvents()
        c.notification.emit(second)
        qapp.processEvents()
        assert "Výpadek" in w.notification_notice.text()
        assert "Obnoveno" not in w.notification_notice.text()
        assert f"color: {c.cfg.appearance.colors.bad}" in w.notification_notice.styleSheet()
        # Již samostatně zobrazené obnovení se nepočítá; ostatní zůstala bez hlavního místa.
        if separate_ticks and not critical_first:
            assert "dalších" not in w.notification_notice.text()
        else:
            assert "(+1 dalších)" in w.notification_notice.text()
    finally:
        w.close()


@pytest.mark.parametrize("font_scale", [1.0, 2.0])
def test_notification_two_scaled_lines(qapp, cfg, controller_factory, font_scale):
    """Regrese L34 review: dlouhá zpráva při 260×340 nepřeroste dva řádky ani skryje počítadlo."""
    cfg.appearance.width, cfg.appearance.height = 260, 340
    cfg.appearance.font_scale = font_scale
    c = controller_factory(cfg)
    w = MainWidget(c)
    w.show()
    try:
        for _ in range(2):
            c.notification.emit(Notification("down", "test", "Dlouhý název " * 100,
                                             "192.0.2.1 neodpovídá\n" * 100, True))
        qapp.processEvents()
        notice = w.notification_notice
        assert w.size().width() == 260 and w.size().height() == 340
        assert notice.font().pixelSize() == theme.font("body", 16, scale=w._scale).pixelSize()
        assert theme.GLASS_BOTTOM.name() in notice.styleSheet()
        assert notice.contentsRect().height() <= 2 * notice.fontMetrics().lineSpacing()
        lines = notice.text().splitlines()
        assert len(lines) == 2 and "(+1 dalších)" in lines[0]
        assert all(notice.fontMetrics().horizontalAdvance(line) <= notice.contentsRect().width()
                   for line in lines)
        small_font = notice.font().pixelSize()
        w.resize(600, 700)
        qapp.processEvents()
        assert notice.font().pixelSize() > small_font
        assert "(+1 dalších)" in notice.text()
        bigger_font = notice.font().pixelSize()
        changed = c.cfg.copy()
        changed.appearance.font_scale = font_scale * 0.6
        c.update_config(changed, save=False)
        qapp.processEvents()
        assert notice.font().pixelSize() < bigger_font
    finally:
        w.close()


def test_available_tray_keeps_visibility_and_notifications(qapp, cfg, controller_factory, monkeypatch):
    """Regrese L34: s dostupnou tray zůstane skrytí i doručování přes showMessage."""
    qapp.setProperty("tray_available", True)
    c = controller_factory(cfg)
    w = MainWidget(c)
    tray = Tray(w)
    show_message = Mock()
    monkeypatch.setattr(tray, "showMessage", show_message)
    try:
        w.show()
        menu = w.build_menu()
        hide = next(a for a in menu.actions() if "Skrýt do oznamovací oblasti" in a.text())
        hide.trigger()
        assert w.isHidden()
        w.toggle_visible()
        assert w.isVisible()
        n = Notification("down", "test", "Výpadek", "192.0.2.1 neodpovídá", True)
        c.notification.emit(n)
        show_message.assert_called_once_with(n.title, n.message, QSystemTrayIcon.MessageIcon.Warning, 8000)
        assert w.notification_notice.isHidden()
    finally:
        tray.deleteLater()
        w.close()


@pytest.mark.parametrize("success", [False, True])
def test_wol_result_without_tray_characterization(cfg, controller_factory, monkeypatch, success):
    """Charakterizační test: WoL doručí výsledek dialogem i bez tray (prochází také na main)."""
    from netmonitor.core.config import PingTarget
    from netmonitor.gui import tools_dialog as tools

    c = controller_factory(cfg)
    target = PingTarget(name="Test", host="192.0.2.1", mac="02:00:00:00:00:01")
    send = Mock(return_value=["192.0.2.255:9"]) if success else Mock(side_effect=OSError("Test chyby"))
    monkeypatch.setattr(tools.wol, "send", send)
    info, warning = Mock(), Mock()
    monkeypatch.setattr(tools.QMessageBox, "information", info)
    monkeypatch.setattr(tools.QMessageBox, "warning", warning)
    # V testu se nespouštějí pozdější sondy ani skutečné odeslání WoL.
    monkeypatch.setattr(tools.QTimer, "singleShot", Mock())
    tools.wake(c, target)
    if success:
        info.assert_called_once()
        assert "Probouzecí paket odeslán" in info.call_args.args[2]
        warning.assert_not_called()
    else:
        warning.assert_called_once()
        assert "Test chyby" in warning.call_args.args[2]
        info.assert_not_called()
