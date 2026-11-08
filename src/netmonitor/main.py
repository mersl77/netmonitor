"""Vstupní bod NetMonitoru.

    netmonitor                 spustí widget
    netmonitor --minimized     spustí skrytě (jen ikona v oznamovací oblasti)
    netmonitor --diagnose      textová diagnostika bez GUI (síť, DNS, WAN, ping)
    netmonitor --diagnose-gui  diagnostika vykreslování (inicializuje Qt, bez síťových dotazů)
    netmonitor --debug         podrobný log
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import signal
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from . import APP_NAME, __version__
from .core import config as cfgmod
from .utils.logging_setup import setup_logging
from .utils.paths import data_dir, ensure_dirs

log = logging.getLogger("netmonitor")


def xcb_available() -> bool:
    """Před startem Qt ověří načtení knihovny vyžadované pluginem xcb."""
    try:
        ctypes.CDLL("libxcb-cursor.so.0")
    except OSError:
        return False
    return True


def select_qt_platform(env: Mapping[str, str], prefer_xwayland: bool, platform: str,
                       xcb_ok: Callable[[], bool]) -> tuple[str | None, str | None]:
    """Vrátí platformu a varování; prostředí nemění a Qt neimportuje."""
    if "QT_QPA_PLATFORM" in env or not platform.startswith("linux"):
        return None, None
    if not env.get("DISPLAY"):
        return None, None
    wayland = bool(env.get("WAYLAND_DISPLAY"))
    if wayland and not prefer_xwayland:
        return None, None
    selected = "xcb;wayland" if wayland else None
    if xcb_ok():
        return selected, None
    warning = ("Nelze načíst knihovnu libxcb-cursor0 (libxcb-cursor.so.0) potřebnou pro XWayland / X11 (xcb). "
               "Po instalaci (Debian/Ubuntu: sudo apt install libxcb-cursor0) aplikaci restartujte. ")
    warning += ("Qt zkusí použít Wayland s omezenou pozicí a vrstvou okna." if wayland else
                "Bez této knihovny nelze na X11 spustit Qt.")
    return selected, warning


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="netmonitor", description="NetMonitor – desktopový síťový monitor")
    p.add_argument("--minimized", action="store_true", help="spustit skrytě v oznamovací oblasti")
    p.add_argument("--debug", action="store_true", help="podrobné logování")
    p.add_argument("--config", type=Path, help="cesta ke config.json")
    p.add_argument("--diagnose", action="store_true", help="vypsat diagnostiku a skončit")
    p.add_argument("--diagnose-gui", action="store_true", help="vypsat prostředí GUI (inicializuje Qt) a skončit")
    p.add_argument("--reset-position", action="store_true", help="vrátit widget do pravého horního rohu")
    p.add_argument("--screenshot", type=Path, help=argparse.SUPPRESS)       # interní: render do PNG a konec
    p.add_argument("--screenshot-delay", type=float, default=6.0, help=argparse.SUPPRESS)
    p.add_argument("--screenshot-bg", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return p.parse_args(argv)


def diagnose(cfg: cfgmod.AppConfig, gui: dict | None = None) -> int:
    from .core.network_info import NetworkInfoProvider
    from .core.ping_engine import Resolver, ping_host, select_pinger
    from .core.wan_info import WanState, fetch_wan_ip
    out: dict = {"version": __version__, "platform": sys.platform, "python": sys.version.split()[0]}
    if gui is not None:
        out["gui"] = gui
    snap = NetworkInfoProvider().snapshot()
    a = snap.active
    out["network"] = {"default_iface": snap.default_iface, "has_default_route": snap.has_default_route,
                      "active": a.__dict__ if a else None, "error": snap.error}
    pinger = select_pinger()
    out["icmp"] = pinger.name
    r = Resolver()
    out["ping"] = {f"{t.name} ({t.host})": (lambda res: {"status": res.status.value,
                                                          "rtt_ms": round(res.rtt_ms, 2) if res.rtt_ms is not None else None,
                                                          "ip": res.address, "error": res.error})(
        ping_host(t.host, t.timeout_s, pinger, r)) for t in cfg.real_targets() if t.enabled}
    if cfg.network.external_api:
        w = fetch_wan_ip()
        out["wan"] = {"ip": w.ip, "provider": w.provider, "error": w.error}
        if w.ip and cfg.network.isp_detection:
            out["wan"]["isp"] = WanState(None).isp_for(w.ip, cfg.network.isp_provider, 0)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    ensure_dirs()
    setup_logging(args.debug)
    log.info("%s %s start (%s, Python %s)", APP_NAME, __version__, sys.platform, sys.version.split()[0])
    cfg_path = args.config or cfgmod.default_config_path()
    cfg = cfgmod.load_config(cfg_path)
    if not cfg_path.exists():
        cfgmod.save_config(cfg, cfg_path)
    if args.diagnose and not args.diagnose_gui:
        return diagnose(cfg)
    if (args.diagnose_gui and sys.platform.startswith("linux") and not os.environ.get("DISPLAY")
            and not os.environ.get("WAYLAND_DISPLAY") and "QT_QPA_PLATFORM" not in os.environ):
        error = {"error": "bez grafické relace"}
        log.error("GUI diagnostika: bez grafické relace (chybí DISPLAY i WAYLAND_DISPLAY)")
        if args.diagnose:
            diagnose(cfg, gui=error)
        else:
            print(json.dumps(error, ensure_ascii=False))
        return 2
    if args.reset_position:
        cfg.appearance.x = cfg.appearance.y = None

    # Preferuj XWayland kvůli pozici a vrstvě okna, při selhání xcb nech Qt zkusit Wayland.
    selected, warning = select_qt_platform(os.environ, cfg.network.linux_prefer_xwayland,
                                           sys.platform, xcb_available)
    if selected is not None:
        os.environ["QT_QPA_PLATFORM"] = selected
        log.info("Wayland detekován – preferuji XWayland (xcb) s fallbackem na Wayland")
    if warning:
        log.warning(warning)

    from PySide6.QtCore import QLockFile, Qt, QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

    from .gui.i18n import set_language
    from .gui.icons import app_icon

    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName("Zelina")
    QApplication.setApplicationVersion(__version__)
    app = QApplication(sys.argv[:1])
    if selected == "xcb;wayland" and app.platformName() == "wayland":
        app.setProperty("xcb_fallback", "cursor" if warning is not None else "other")
        log.warning("XWayland (xcb) se nepodařilo načíst – používám Wayland s omezenou pozicí a vrstvou okna.")
    tray_ok = QSystemTrayIcon.isSystemTrayAvailable()
    try:
        from .gui.diag import collect_gui_diagnostics
        gui_diag = collect_gui_diagnostics(app, platform_selected_by_app=selected, tray_available=tray_ok)
        log.info("GUI diagnostika: %s", json.dumps(gui_diag, ensure_ascii=False, allow_nan=False))
    except Exception:  # noqa: BLE001 – diagnostika nesmí zabránit startu aplikace
        log.debug("Sběr GUI diagnostiky selhal", exc_info=True)
        gui_diag = {"error": "Sběr GUI diagnostiky selhal"}
    if args.diagnose_gui:
        if args.diagnose:
            diagnose(cfg, gui=gui_diag)
        else:
            output = gui_diag if "error" in gui_diag else {"version": __version__, "gui": gui_diag}
            print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
        return 2 if "error" in gui_diag else 0
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(app_icon("ok"))
    set_language(cfg.general.language)

    lock = QLockFile(str(data_dir() / "netmonitor.lock"))
    lock.setStaleLockTime(0)
    if not args.screenshot and not lock.tryLock(100):
        QMessageBox.information(None, APP_NAME, "NetMonitor už běží (ikona v oznamovací oblasti).")
        return 0

    from .gui.controller import Controller
    from .gui.history_dialog import HistoryDialog
    from .gui.main_widget import MainWidget
    from .gui.settings_dialog import SettingsDialog
    from .gui.tray import Tray

    app.setProperty("tray_available", tray_ok)
    if not tray_ok and cfg.appearance.click_through:
        cfg.appearance.click_through = False      # bez tray by nešlo vypnout
        log.warning("Oznamovací oblast nedostupná – click-through vypnut")

    hist_path = None
    if args.screenshot:
        import tempfile
        hist_path = Path(tempfile.mkdtemp()) / "h.sqlite3"
    ctrl = Controller(cfg, None if args.screenshot else cfg_path, hist_path)
    widget = MainWidget(ctrl)
    tray = None
    if tray_ok and not args.screenshot:
        tray = Tray(widget)
        tray.show()

    dialogs: dict[str, object] = {}

    def history(tid: str = ""):
        d = dialogs.get("history")
        if d is None:
            d = dialogs["history"] = HistoryDialog(ctrl)
            d.tool_requested.connect(lambda k, i: tool(k, i))
        d.show_target(tid)

    def settings():
        d = dialogs.get("settings")
        if d is None:
            d = dialogs["settings"] = SettingsDialog(ctrl, widget)
            d.history_requested.connect(history)
        d.load()
        d.show()
        d.raise_()
        d.activateWindow()

    def ipconfig(iface: str = ""):
        from .gui.ipconfig_dialog import IpConfigDialog
        d = dialogs.get("ipconfig")
        if d is None:
            d = dialogs["ipconfig"] = IpConfigDialog(ctrl)
        d.open_for(iface or None)

    from .gui.updater_ui import AboutDialog, UpdateDialog, UpdateManager
    updates = UpdateManager(ctrl)

    def update_dialog():
        d = dialogs.get("update")
        if d is None:
            d = dialogs["update"] = UpdateDialog(updates)
        d.open_for()

    def about():
        d = dialogs.get("about")
        if d is None:
            d = dialogs["about"] = AboutDialog()
        d.show()
        d.raise_()

    def manual_check():
        def once(info, err):
            updates.check_done.disconnect(once)
            if info is None:
                QMessageBox.information(None, "Aktualizace", err or f"Máte aktuální verzi {__version__}.")
            else:
                update_dialog()
        updates.check_done.connect(once)
        updates.check(manual=True)

    updates.available.connect(widget.set_update)
    if tray:
        updates.available.connect(tray.set_update)
        tray.messageClicked.connect(lambda: updates.info and update_dialog())
    from .gui import tools_dialog as tools

    def traceroute(tid: str):
        t = ctrl.cfg.target_by_id(tid)
        if t and not t.is_section:
            tools.open_traceroute(ctrl, t)

    def wake(tid: str):
        t = ctrl.cfg.target_by_id(tid)
        if t and not t.is_section:
            tools.wake(ctrl, t)

    def speedtest():
        d = dialogs.get("speedtest")
        if d is None:
            d = dialogs["speedtest"] = tools.SpeedTestDialog(ctrl)
        d.show()
        d.raise_()

    def tool(kind: str, tid: str):
        {"traceroute": traceroute, "wake": wake}.get(kind, lambda _t: speedtest())(tid)

    widget.open_traceroute.connect(traceroute)
    widget.wake_target.connect(wake)
    widget.open_speedtest.connect(speedtest)
    widget.open_update.connect(update_dialog)
    widget.open_about.connect(about)

    _orig_settings = settings

    def settings():  # noqa: F811 – rozšíření o tlačítko "Zkontrolovat nyní"
        first = "settings" not in dialogs
        _orig_settings()
        if first:
            dialogs["settings"].check_updates_requested.connect(manual_check)

    widget.open_settings.connect(settings)
    widget.open_ipconfig.connect(ipconfig)
    widget.open_history.connect(history)
    widget.quit_requested.connect(app.quit)
    app.aboutToQuit.connect(ctrl.shutdown)

    # synchronizace autostartu s realitou (např. po přesunu EXE)
    try:
        from . import platform as plat
        real = plat.current().autostart_enabled()
        if cfg.general.autostart:
            plat.current().set_autostart(True, ctrl.autostart_command())
        elif real and not args.screenshot:    # zapnuto zvenku (např. instalátorem) => převezmi stav
            cfg.general.autostart = True
            ctrl.schedule_save()
    except Exception:  # noqa: BLE001
        log.exception("Autostart sync selhal")

    signal.signal(signal.SIGINT, lambda *_: app.quit())
    sig_timer = QTimer(interval=500, timeout=lambda: None)   # umožní Pythonu zpracovat SIGINT
    sig_timer.start()

    ctrl.start()
    start_hidden = (args.minimized or cfg.general.start_minimized) and tray_ok
    if not start_hidden:
        widget.show()

    if not args.screenshot:
        updates.start()

    if args.screenshot:
        QTimer.singleShot(int(args.screenshot_delay * 1000),
                          lambda: (_save_screenshot(widget, args.screenshot, args.screenshot_bg), app.quit()))
    rc = app.exec()
    lock.unlock()
    log.info("Konec (%s)", rc)
    return rc


def _save_screenshot(widget, path: Path, bg: Path | None) -> None:
    """Vykreslí průhledný widget na zadané pozadí (ověření vzhledu bez kompozitoru)."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter
    img = widget.grab().toImage()
    out = QImage(img.width() + 80, img.height() + 80, QImage.Format.Format_ARGB32)
    p = QPainter(out)
    if bg and bg.exists():
        b = QImage(str(bg)).scaled(out.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                   Qt.TransformationMode.SmoothTransformation)
        p.drawImage(0, 0, b)
    else:
        g = QLinearGradient(0, 0, out.width(), out.height())
        g.setColorAt(0, QColor("#041a3d"))
        g.setColorAt(0.6, QColor("#0a4c9c"))
        g.setColorAt(1, QColor("#08305f"))
        p.fillRect(out.rect(), g)
    p.drawImage(QPoint(40, 40), img)
    p.end()
    out.save(str(path))


if __name__ == "__main__":
    sys.exit(main())
