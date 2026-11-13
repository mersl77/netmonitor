# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec – jeden soubor pro Windows, Linux i macOS.
#   pyinstaller packaging/netmonitor.spec --noconfirm
# Výstup: dist/NetMonitor/ (onedir – rychlý start, vhodné pro autostart widgetu)
#         macOS navíc dist/NetMonitor.app
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"
RES = SRC / "netmonitor" / "resources"

datas = [
    (str(RES / "fonts"), "netmonitor/resources/fonts"),
    (str(RES / "icons"), "netmonitor/resources/icons"),
]

# Nepotřebné části Qt – výrazně menší balíček
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DExtras", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQml", "PySide6.QtQuickWidgets", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtBluetooth", "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtOpenGLWidgets", "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSpatialAudio",
    "PySide6.QtTextToSpeech", "PySide6.QtWebSockets", "PySide6.QtHttpServer", "PySide6.QtNfc",
    "tkinter", "unittest", "pydoc", "pytest",
]

if sys.platform == "win32":
    icon = str(RES / "icons" / "netmonitor.ico")
elif sys.platform == "darwin":
    icon = str(RES / "icons" / "netmonitor.icns")
else:
    icon = None

a = Analysis(
    [str(ROOT / "packaging" / "netmonitor_entry.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=["netmonitor.platform.windows", "netmonitor.platform.linux", "netmonitor.platform.macos"],
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)
# Qt Quick/QML/PDF/VirtualKeyboard se do balíčku dostávají jen přes nepotřebné pluginy – vyřadit (≈ −25 MB)
_DROP = ("Qt6Quick", "Qt6Qml", "Qt6Pdf", "Qt6VirtualKeyboard", "qtvirtualkeyboardplugin", "qpdf",
         "Qt6QuickControls", "Qt6QuickTemplates")
a.binaries = [b for b in a.binaries if not any(d in Path(b[0]).name for d in _DROP)]
# Qt on Windows uses the OS ICU API. A same-named third-party ICU from
# PATH (e.g. Poppler) exports versioned symbols and breaks QtCore at startup.
if sys.platform == "win32":
    a.binaries = [b for b in a.binaries
                  if Path(b[0]).name.lower() not in ("icuuc.dll", "icuin.dll")]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NetMonitor",
    debug=False,
    strip=False,
    upx=False,                 # UPX často spouští falešné poplachy antivirů
    console=False,             # GUI aplikace bez konzole
    icon=icon,
    version=str(ROOT / "packaging" / "windows" / "version_info.txt") if sys.platform == "win32" else None,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="NetMonitor")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="NetMonitor.app",
        icon=icon,
        bundle_identifier="eu.zelina.netmonitor",
        info_plist={
            "CFBundleName": "NetMonitor",
            "CFBundleDisplayName": "NetMonitor",
            "CFBundleShortVersionString": "1.4.0",
            "CFBundleVersion": "1.4.0",
            "LSUIElement": True,                      # bez ikony v Docku – widget + ikona v menu baru
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "NSLocationWhenInUseUsageDescription":
                "NetMonitor potřebuje přístup k poloze jen kvůli zobrazení názvu Wi-Fi sítě (SSID).",
        },
    )
