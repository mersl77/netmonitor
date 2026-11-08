"""Integrační test GUI (offscreen): widget, nastavení, historie, živé změny konfigurace, bezpečné ukončení."""
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from netmonitor.core.config import AppConfig, PingTarget  # noqa: E402

from .fakes import FakePinger  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setProperty("tray_available", False)
    yield app


@pytest.mark.parametrize("state", ["normal", "minimized", "maximized", "maximized_minimized"])
def test_show_dialog_activates_and_restores(qapp, state):
    """Regrese L29: dialog z tray se musí zvednout, aktivovat a obnovit z minimalizace."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog
    from netmonitor.gui.widgets import show_dialog

    calls = []

    class Dialog(QDialog):
        def show(self):
            calls.append("show")
            super().show()

        def raise_(self):
            calls.append("raise")
            super().raise_()

        def activateWindow(self):
            calls.append("activate")
            super().activateWindow()

    d = Dialog()
    flags = Qt.WindowState.WindowNoState
    if "maximized" in state:
        flags |= Qt.WindowState.WindowMaximized
    if "minimized" in state:
        flags |= Qt.WindowState.WindowMinimized
    try:
        # První otevření, opakované vyvolání i znovuotevření zavřeného dialogu.
        for close_first in (False, False, True):
            if close_first:
                d.close()
            d.setWindowState(flags)
            calls.clear()
            show_dialog(d)
            assert calls == ["show", "raise", "activate"]
            assert d.isVisible() and not d.isMinimized()
            assert bool(d.windowState() & Qt.WindowState.WindowMaximized) == ("maximized" in state)
    finally:
        d.close()


def pump(sec):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.02)


@pytest.fixture
def ctrl(qapp, tmp_path, monkeypatch):
    import netmonitor.gui.controller as cmod
    monkeypatch.setattr(cmod, "select_pinger", lambda: FakePinger({"10.0.0.99": "timeout"}, default="ok:7.1"))
    cfg = AppConfig.from_dict({"network": {"external_api": False}})
    cfg.ping.targets = [PingTarget(name="Zelina", host="10.0.0.1", interval_s=1, id="z"),
                        PingTarget(name="Mrtvý", host="10.0.0.99", interval_s=1, timeout_s=0.2, retries=2, id="d")]
    c = cmod.Controller(cfg, tmp_path / "config.json", tmp_path / "h.sqlite3")
    yield c
    c.shutdown()


@pytest.mark.parametrize("kind", ["history", "ipconfig", "update", "traceroute"])
def test_dialog_entry_points_use_activation(ctrl, monkeypatch, kind):
    """Regrese L29: vlastní otevírací metody dialogů používají společnou aktivaci."""
    from netmonitor.gui import history_dialog, ipconfig_dialog, tools_dialog, updater_ui
    from netmonitor.gui.widgets import show_dialog

    opened = []

    def activate(dialog):
        opened.append(dialog)
        show_dialog(dialog)

    if kind == "history":
        module = history_dialog
        d = history_dialog.HistoryDialog(ctrl)
        open_dialog = d.show_target
    elif kind == "ipconfig":
        module = ipconfig_dialog
        d = ipconfig_dialog.IpConfigDialog(ctrl)
        open_dialog = d.open_for
    elif kind == "update":
        module = updater_ui
        d = updater_ui.UpdateDialog(updater_ui.UpdateManager(ctrl))
        open_dialog = d.open_for
    else:
        module = tools_dialog
        monkeypatch.setattr(tools_dialog.TracerouteDialog, "start", lambda _self: None)
        target = PingTarget(name="Test", host="192.0.2.1")
        open_dialog = lambda: tools_dialog.open_traceroute(ctrl, target)
    monkeypatch.setattr(module, "show_dialog", activate)
    try:
        open_dialog()
        opened[-1].showMinimized()
        open_dialog()
        assert len(opened) == 2
        assert opened[-1].isVisible() and not opened[-1].isMinimized()
    finally:
        for dialog in opened:
            dialog.close()
            if kind == "traceroute":
                tools_dialog._trace_dialogs.remove(dialog)
                dialog.deleteLater()


def test_widget_shows_real_measurements(ctrl):
    from netmonitor.gui.main_widget import MainWidget
    w = MainWidget(ctrl)
    ctrl.start()
    w.show()
    pump(3.0)
    rows = w.ping.rows
    assert rows["z"]._value == ("7.1 ms", "ok")
    assert rows["d"]._value[0] == "TIMEOUT"
    assert ctrl.snapshots["d"].up is False
    assert w.net.wan.value.text() == "vypnuto"          # externí API vypnuto => žádný dotaz
    from PySide6.QtCore import Qt
    assert w.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)     # skutečně průhledné okno
    w.close()


def test_live_config_change_and_save(ctrl, tmp_path):
    from netmonitor.gui.main_widget import MainWidget
    w = MainWidget(ctrl)
    ctrl.start()
    cfg = ctrl.cfg.copy()
    cfg.ping.targets.append(PingTarget(name="Nový", host="10.0.0.5", interval_s=1, id="n"))
    cfg.appearance.mode = "ontop"
    ctrl.update_config(cfg)
    pump(2.0)
    assert "n" in w.ping.rows
    assert ctrl.snapshots["n"].ok_count >= 1
    from PySide6.QtCore import Qt
    assert w.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    from netmonitor.core.config import load_config
    saved = load_config(tmp_path / "config.json")
    assert [t.id for t in saved.ping.targets] == ["z", "d", "n"]


def test_dialogs_open(ctrl):
    from netmonitor.gui.history_dialog import HistoryDialog
    from netmonitor.gui.main_widget import MainWidget
    from netmonitor.gui.settings_dialog import SettingsDialog
    w = MainWidget(ctrl)
    ctrl.start()
    pump(1.5)
    s = SettingsDialog(ctrl, w)
    s.show()
    assert s.table.topLevelItemCount() == 2
    from PySide6.QtCore import Qt
    s.table.topLevelItem(1).setCheckState(0, Qt.CheckState.Unchecked)
    pump(0.2)
    assert ctrl.cfg.target_by_id("d").enabled is False
    h = HistoryDialog(ctrl)
    h.show_target("z")
    pump(0.3)
    assert h.chart.points, "graf 1 h musí mít body z paměťového bufferu"
    menu = w.build_menu()
    assert any("Nastavení" in a.text() for a in menu.actions())
    h.close()
    s.close()


def test_shutdown_is_clean(ctrl):
    import threading
    ctrl.start()
    pump(1.0)
    ctrl.shutdown()
    time.sleep(0.2)
    assert not ctrl.engine.running
    alive = [t.name for t in threading.enumerate() if t.name.startswith(("ping", "bg"))]
    assert alive == []


def test_sections_in_widget_and_settings(ctrl):
    from netmonitor.core.config import new_section
    from netmonitor.gui.main_widget import MainWidget
    from netmonitor.gui.settings_dialog import SettingsDialog
    w = MainWidget(ctrl)
    ctrl.start()
    cfg = ctrl.cfg.copy()
    cfg.ping.targets.insert(0, new_section("Domény"))
    cfg.ping.targets.insert(2, new_section("Zařízení"))
    ctrl.update_config(cfg)
    pump(1.5)
    assert len(w.ping.sections) == 2 and len(w.ping.rows) == 2
    assert ctrl.snapshots["z"].ok_count >= 1
    s = SettingsDialog(ctrl, w)
    s.show()
    t = s.table
    assert t.topLevelItemCount() == 2 and t.topLevelItem(0).childCount() == 1
    assert t.flat_ids() == [x.id for x in ctrl.cfg.ping.targets]
    # přetažení (simulace výsledku dropu): sekce Zařízení nahoru
    sec2 = ctrl.cfg.ping.targets[2].id
    s._order_from_tree([sec2, "d", ctrl.cfg.ping.targets[0].id, "z"])
    pump(0.3)
    assert [x.id for x in ctrl.cfg.ping.targets][:2] == [sec2, "d"]
    assert list(w.ping._order)[:2] == [sec2, "d"]
    # šipka: sekce dolů
    t.select_id = sec2
    s._fill_table()
    s._move(1)
    assert [x.id for x in ctrl.cfg.ping.targets][-2:] == [sec2, "d"]
    s.close()


def test_ipconfig_dialog_prefill_and_validation(ctrl, monkeypatch):
    from netmonitor.core.network_info import InterfaceInfo, NetworkSnapshot
    from netmonitor.gui.ipconfig_dialog import IpConfigDialog
    import netmonitor.platform as plat
    a = InterfaceInfo(name="Ethernet 10Gb", display_name="Ethernet 10Gb", kind="ethernet", is_up=True,
                      ipv4=("192.168.14.112",), ipv4_masks=("255.255.255.0",), gateway="192.168.14.1",
                      dns=("192.168.14.2", "192.168.14.3"))
    ctrl.network = NetworkSnapshot(active=a, interfaces=(a,), has_default_route=True, default_iface=a.name)

    class FakePlat:
        applied = []

        @staticmethod
        def dhcp_enabled(_i):
            return True

        @staticmethod
        def apply_ip(s):
            FakePlat.applied.append(s)
            return True, "ok"
    monkeypatch.setattr(plat, "current", lambda: FakePlat)
    d = IpConfigDialog(ctrl)
    d.open_for("Ethernet 10Gb")
    assert d.rb_dhcp.isChecked() and d.ip.text() == "192.168.14.112" and d.mask.text() == "255.255.255.0"
    assert "DHCP" in d.current.text()
    d.rb_static.setChecked(True)
    d.rb_dns_static.setChecked(True)
    d.ip.setText("192.168.14.300")
    assert not d.btn_apply.isEnabled()
    d.ip.setText("192.168.14.112")
    assert d.btn_apply.isEnabled()
    s = d.settings()
    assert (s.dhcp, s.ip, s.prefix, s.gateway, s.dns_list) == (False, "192.168.14.112", 24, "192.168.14.1",
                                                              ["192.168.14.2", "192.168.14.3"])
    results = []
    ctrl.apply_ip(s, results.append)
    pump(0.5)
    assert results == [(True, "ok")] and FakePlat.applied[0].ip == "192.168.14.112"
    # profil
    monkeypatch.setattr("netmonitor.gui.ipconfig_dialog.QInputDialog.getText", lambda *a, **k: ("Kancelář", True))
    d._save_profile()
    assert ctrl.cfg.network.ip_profiles[0]["name"] == "Kancelář"
    d.close()


def test_update_badge_menu_and_dialog(ctrl):
    from netmonitor.core.updater import UpdateInfo
    from netmonitor.gui.main_widget import MainWidget
    from netmonitor.gui.updater_ui import AboutDialog, UpdateDialog, UpdateManager
    w = MainWidget(ctrl)
    mgr = UpdateManager(ctrl)
    mgr.available.connect(w.set_update)
    info = UpdateInfo(version="9.9.9", page_url="https://github.com/x/y/releases/tag/v9.9.9", notes="**Novinky**")
    mgr._checked((info, ""), manual=False)
    assert w.update_version == "9.9.9" and mgr.info == info
    menu = w.build_menu()
    texts = [a.text() for a in menu.actions()]
    assert any("9.9.9" in t for t in texts) and any("O aplikaci" in t for t in texts)
    d = UpdateDialog(mgr)
    d.open_for()
    assert "9.9.9" in d.head.text() and not d.btn_install.isVisible()      # ze zdrojů se neinstaluje
    mgr.skip()
    assert ctrl.cfg.general.skipped_version == "9.9.9" and w.update_version is None
    mgr._checked((info, ""), manual=False)
    assert mgr.info is None                                                  # přeskočená verze se nehlásí
    a = AboutDialog()
    a.show()
    d.close()
    a.close()


def test_background_transparency_menu(ctrl):
    from netmonitor.gui.main_widget import MainWidget
    w = MainWidget(ctrl)
    assert ctrl.cfg.appearance.glass_alpha == 1.0                            # výchozí = neprůhledné
    w._set_appearance(glass_alpha=0.4)
    assert abs(ctrl.cfg.appearance.glass_alpha - 0.4) < 1e-9


def test_row_context_menu_and_tool_dialogs(ctrl, monkeypatch):
    from PySide6.QtCore import QPoint
    from netmonitor.core import traceroute as tr
    from netmonitor.core.speedtest import SpeedResult
    from netmonitor.gui import tools_dialog as tools
    from netmonitor.gui.main_widget import MainWidget
    from netmonitor.gui.settings_dialog import TargetDialog
    w = MainWidget(ctrl)
    ctrl.start()
    w.show()
    pump(1.0)
    # editor cíle: pole MAC + normalizace
    d = TargetDialog(ctrl.cfg.target_by_id("z"))
    d.mac.setText("04-d9-f5-15-34-86")
    d._accept()
    assert d.result() and d.result_target().mac == "04:D9:F5:15:34:86"
    # traceroute dialog s podvrženým během
    def fake_run(host, on_hop, **kw):
        on_hop(tr.Hop(1, "192.168.14.1", [0.4, 0.5, 0.4]))
        on_hop(tr.Hop(2, None, [None, None, None]))
        on_hop(tr.Hop(3, "10.0.0.1", [5.0, 5.1, 5.2], reached=True))
        return "10.0.0.1", ""
    monkeypatch.setattr(tr, "run", fake_run)
    td = tools.TracerouteDialog(ctrl, ctrl.cfg.target_by_id("z"))
    td.start()
    pump(0.5)
    assert td.table.rowCount() == 3 and "dosažen" in td.status.text() and not td.busy()
    td.close()
    # test rychlosti – výsledek do UI a do historie
    sd = tools.SpeedTestDialog(ctrl)
    sd._result(SpeedResult(latency_ms=7.1, jitter_ms=0.8, download_mbps=940.0, upload_mbps=480.0, colo="PRG"))
    pump(0.3)
    assert sd.vals["download"].text() == "940 Mb/s" and "PRG" in sd.status.text()
    assert any(e.kind == "speedtest" for e in ctrl.history.events(0))
    sd.close()
    # menu widgetu má test rychlosti
    menu = w.build_menu()
    assert any("Test rychlosti" in a.text() for a in menu.actions())


def test_traceroute_window_survives_thread_end(ctrl, monkeypatch):
    """Regrese 1.3.0–1.3.2: okno bez trvalého odkazu zrušil Python po doběhnutí vlákna => pád aplikace."""
    import time

    from netmonitor.core import traceroute as tr
    from netmonitor.gui import tools_dialog as tools

    def fake(host, on_hop, **kw):
        for i in range(1, 5):
            time.sleep(0.15)
            on_hop(tr.Hop(i, f"10.0.0.{i}", [1.0, 1.0, 1.0]))
        return "10.0.0.4", ""
    monkeypatch.setattr(tr, "run", fake)
    tools._trace_dialogs.clear()
    tools.open_traceroute(ctrl, ctrl.cfg.target_by_id("z"))      # bez uložení odkazu – jako v aplikaci
    pump(0.4)
    tools._trace_dialogs[0].close()                               # zavření během běhu
    pump(1.5)
    d = tools.open_traceroute(ctrl, ctrl.cfg.target_by_id("z"))   # úklid starého okna + nové
    pump(1.5)
    assert tools._trace_dialogs == [d] and d.table.rowCount() == 4 and not d.busy()
    d.close()


@pytest.mark.parametrize("fallback", [None, "cursor", "other"])
def test_platform_notice_after_xcb_fallback(ctrl, qapp, monkeypatch, fallback):
    """Regrese 1.0–1.3.3: po selhání XWaylandu má notice nabídnout instalaci knihovny."""
    from PySide6.QtGui import QGuiApplication
    from netmonitor.gui.main_widget import MainWidget

    monkeypatch.setattr(QGuiApplication, "platformName", lambda: "wayland")
    previous = qapp.property("xcb_fallback")
    qapp.setProperty("xcb_fallback", fallback)
    w = None
    try:
        w = MainWidget(ctrl)
        if fallback == "cursor":
            assert "chybí knihovna libxcb-cursor0" in w.platform_notice
            assert "Debian/Ubuntu: sudo apt install libxcb-cursor0)" in w.platform_notice
            assert "libxkbcommon-x11-0" not in w.platform_notice
            assert "zapněte" not in w.platform_notice
        elif fallback == "other":
            assert "chybí knihovny xcb" in w.platform_notice
            assert "Debian/Ubuntu: sudo apt install libxcb-cursor0 libxkbcommon-x11-0)" in w.platform_notice
            assert "zapněte" not in w.platform_notice
        else:
            assert "zapněte" in w.platform_notice
            assert "libxcb-cursor0" not in w.platform_notice
        assert "`" not in w.platform_notice
    finally:
        if w is not None:
            w.close()
        qapp.setProperty("xcb_fallback", previous)
