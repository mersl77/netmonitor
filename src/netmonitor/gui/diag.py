"""Diagnostika vykreslování bez identifikátorů monitorů a adres displejů."""
from __future__ import annotations

import ctypes
import math
import os
import re
from collections.abc import Iterable, Mapping
from typing import Protocol

import PySide6
from PySide6.QtCore import QRect, qVersion
from PySide6.QtWidgets import QApplication


class _Screen(Protocol):
    def geometry(self) -> QRect: ...
    def devicePixelRatio(self) -> float: ...
    def logicalDotsPerInch(self) -> float: ...
    def physicalDotsPerInch(self) -> float: ...
    def refreshRate(self) -> float: ...


def _finite(value: float) -> float | None:
    """Qt může při nedostupném monitoru vrátit NaN; JSON pak musí obsahovat null."""
    return value if math.isfinite(value) else None


def _safe_environment(environ: Mapping[str, str]) -> dict:
    """Povolí jen diagnostické hodnoty; názvy monitorů a cesty se nekopírují."""
    session = environ.get("XDG_SESSION_TYPE", "")
    desktops = environ.get("XDG_CURRENT_DESKTOP", "").split(":")
    token = r"[A-Za-z0-9_-]{1,32}"
    out = {
        "XDG_SESSION_TYPE": session if not session or re.fullmatch(token, session) else "[redacted]",
        "XDG_CURRENT_DESKTOP": ":".join(d if re.fullmatch(token, d) else "[redacted]"
                                         for d in desktops if d),
        "WAYLAND_DISPLAY": bool(environ.get("WAYLAND_DISPLAY")),
        "DISPLAY": bool(environ.get("DISPLAY")),
    }
    number = r"[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
    for key in ("QT_SCALE_FACTOR", "QT_SCREEN_SCALE_FACTORS", "QT_ENABLE_HIGHDPI_SCALING", "QT_QPA_PLATFORM"):
        value = environ.get(key)
        if value is None:
            out[key] = None
        elif key == "QT_SCREEN_SCALE_FACTORS":
            # Qt dovoluje i jméno-monitoru=faktor; do logu patří pouze faktory v původním pořadí.
            factors = [part.rsplit("=", 1)[-1] for part in value.split(";") if part]
            out[key] = ";".join(f if re.fullmatch(number, f) else "[redacted]" for f in factors)
        elif key == "QT_QPA_PLATFORM":
            plugins = {"xcb", "wayland", "wayland-egl", "wayland-brcm", "windows", "cocoa", "offscreen",
                       "minimal", "minimalegl", "eglfs", "linuxfb", "vnc", "vkkhrdisplay"}
            # Za dvojtečkou mohou být parametry obsahující cesty či adresy.
            out[key] = ";".join(p.split(":", 1)[0] if p.split(":", 1)[0] in plugins else "[redacted]"
                                for p in value.split(";") if p)
        elif key == "QT_ENABLE_HIGHDPI_SCALING":
            out[key] = value if value in ("0", "1") else "[redacted]"
        else:
            out[key] = value if re.fullmatch(number, value) else "[redacted]"
    return out


def build_gui_diagnostics(*, qt_platform: str, qt_version: str, pyside_version: str,
                          environ: Mapping[str, str], screens: Iterable[_Screen], tray_available: bool,
                          compositing: dict[str, bool] | str, platform_selected_by_app: str | None = None,
                          xcb_fallback: str | None = None) -> dict:
    """Čistý snímek předaných údajů; nečte procesní prostředí ani nemění nastavení Qt."""
    screen_info = []
    for screen in screens:
        rect = screen.geometry()
        screen_info.append({
            "geometry": {"x": rect.x(), "y": rect.y(), "width": rect.width(), "height": rect.height()},
            "devicePixelRatio": _finite(screen.devicePixelRatio()),
            "logicalDotsPerInch": _finite(screen.logicalDotsPerInch()),
            "physicalDotsPerInch": _finite(screen.physicalDotsPerInch()),
            "refreshRate": _finite(screen.refreshRate()),
        })
    return {"qt_platform": qt_platform, "qt_version": qt_version, "pyside_version": pyside_version,
            "platform_selected_by_app": platform_selected_by_app, "xcb_fallback": xcb_fallback,
            "environment": _safe_environment(environ), "screens": screen_info,
            "tray_available": tray_available, "compositing": compositing}


def _x11_compositing(app: QApplication) -> dict[str, bool] | str:
    """Vlastník _NET_WM_CM_S<n> pro každý X screen (nikoli výstup/monitor RandR).

    Používá vypůjčené spojení Qt přes QX11Application a volitelnou systémovou libX11;
    spojení neotevírá ani nezavírá. Selekce není vlastnost root okna čitelná přes xprop.
    """
    if app.platformName() != "xcb":
        return "neznámo"
    try:
        native = app.nativeInterface()
        display_method = getattr(native, "display", None)
        display = display_method() if callable(display_method) else None
        if not isinstance(display, int) or not display:
            return "neznámo"
        x11 = ctypes.CDLL("libX11.so.6")
        x11.XScreenCount.argtypes = [ctypes.c_void_p]
        x11.XScreenCount.restype = ctypes.c_int
        x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x11.XInternAtom.restype = ctypes.c_ulong
        x11.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XGetSelectionOwner.restype = ctypes.c_ulong
        owners = {}
        for index in range(x11.XScreenCount(display)):
            selection = f"_NET_WM_CM_S{index}"
            atom = x11.XInternAtom(display, selection.encode("ascii"), 1)
            owners[selection] = bool(atom and x11.XGetSelectionOwner(display, atom))
        return owners or "neznámo"
    except (AttributeError, OSError, TypeError, RuntimeError):
        # Starší binding nebo chybějící Xlib nesmí zabránit spuštění aplikace.
        return "neznámo"


def collect_gui_diagnostics(app: QApplication, *, tray_available: bool,
                            platform_selected_by_app: str | None = None) -> dict:
    """Read-only sběr po vytvoření QApplication, společný pro start a CLI."""
    return build_gui_diagnostics(qt_platform=app.platformName(), qt_version=qVersion(),
                                 pyside_version=PySide6.__version__, environ=os.environ,
                                 screens=app.screens(), tray_available=tray_available,
                                 compositing=_x11_compositing(app), platform_selected_by_app=platform_selected_by_app,
                                 xcb_fallback=app.property("xcb_fallback"))
