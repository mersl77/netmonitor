"""Vygeneruje ikony aplikace (PNG, ICO, ICNS) z vektorové kresby v netmonitor.gui.icons.

Build skripty jej spouštějí při chybějících ikonách nebo s --icons (PowerShell: -Icons).
Ruční regenerace: python scripts/make_icons.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication, QImageWriter  # noqa: E402

from netmonitor.gui.icons import app_icon_pixmap  # noqa: E402


def main() -> int:
    app = QGuiApplication(sys.argv[:1])  # noqa: F841
    out = ROOT / "src" / "netmonitor" / "resources" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    app_icon_pixmap(512, "ok").save(str(out / "netmonitor.png"))
    for sz in (16, 32, 48, 64, 128, 256):
        app_icon_pixmap(sz, "ok").save(str(out / f"netmonitor-{sz}.png"))
    fmts = {bytes(f).decode() for f in QImageWriter.supportedImageFormats()}
    try:   # vícevelikostní ICO (ostré v Průzkumníku i v tray), pokud je k dispozici Pillow
        from PIL import Image
        Image.open(out / "netmonitor-256.png").save(
            out / "netmonitor.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    except ImportError:
        if "ico" in fmts:
            app_icon_pixmap(256, "ok").save(str(out / "netmonitor.ico"), "ICO")
        else:
            print("Varování: Qt bez ICO zapisovače – .ico nevytvořen")
    if "icns" in fmts:
        app_icon_pixmap(512, "ok").save(str(out / "netmonitor.icns"), "ICNS")
    print("Ikony uloženy do", out, "| formáty:", ", ".join(sorted(p.suffix for p in out.iterdir())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
