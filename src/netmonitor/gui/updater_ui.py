"""Aktualizace v GUI: pravidelná kontrola, odznak ve widgetu a v liště, dialog se stažením a instalací."""
from __future__ import annotations

import logging
import subprocess
import sys

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton,
                               QTextBrowser, QVBoxLayout)

from .. import __version__
from ..core import updater as upd
from ..utils.paths import data_dir
from . import theme
from .widgets import show_dialog

log = logging.getLogger(__name__)

CHECK_INTERVAL_MS = 6 * 3600 * 1000
FIRST_CHECK_DELAY_MS = 20_000


class UpdateManager(QObject):
    available = Signal(object)          # UpdateInfo | None (None = žádná nová verze)
    progress = Signal(int, int)         # staženo, celkem
    finished_download = Signal(object)  # Path | str (chyba)
    check_done = Signal(object, str)    # UpdateInfo | None, chyba

    def __init__(self, ctrl, parent=None):
        super().__init__(parent)
        self.ctrl = ctrl
        self.info: upd.UpdateInfo | None = None
        self.last_error = ""
        self._cancel = False
        self._timer = QTimer(self, interval=CHECK_INTERVAL_MS, timeout=lambda: self.check(False))

    def start(self) -> None:
        QTimer.singleShot(FIRST_CHECK_DELAY_MS, lambda: self.check(False))
        self._timer.start()

    # ------------------------------------------------------------------ kontrola
    def check(self, manual: bool = False) -> None:
        g = self.ctrl.cfg.general
        if not manual and not g.check_updates:
            return
        if not self.ctrl.cfg.network.external_api and not manual:
            return
        src = g.update_url or upd.DEFAULT_SOURCE

        def job():
            try:
                return upd.check(src), ""
            except upd.UpdateError as e:
                return None, str(e)
        self.ctrl._run_bg("update-check", job, lambda r: self._checked(r, manual))

    def _checked(self, res, manual: bool) -> None:
        info, err = res
        self.last_error = err
        if err:
            log.info("Kontrola aktualizací: %s", err)
            self.check_done.emit(None, err)
            return
        g = self.ctrl.cfg.general
        newer = info is not None and upd.is_newer(info.version)
        if newer and (manual or info.version != g.skipped_version):
            log.info("Dostupná nová verze %s (%s)", info.version, info.page_url)
            self.info = info
            self.available.emit(info)
            if g.auto_update and not manual and upd.can_self_install() and info.can_auto_install:
                self.download_and_install()
        else:
            if self.info is not None and not newer:
                self.info = None
                self.available.emit(None)
        self.check_done.emit(info if newer else None, "")

    def skip(self) -> None:
        if not self.info:
            return
        cfg = self.ctrl.cfg.copy()
        cfg.general.skipped_version = self.info.version
        self.ctrl.update_config(cfg)
        self.info = None
        self.available.emit(None)

    # ------------------------------------------------------------------ instalace
    def download_and_install(self) -> None:
        info = self.info
        if not info:
            return
        self._cancel = False

        def job():
            try:
                return upd.download(info, data_dir() / "updates",
                                    progress=lambda d, t: self.progress.emit(d, t),
                                    cancelled=lambda: self._cancel)
            except upd.UpdateError as e:
                return str(e)
        self.ctrl._run_bg("update-download", job, self._downloaded)

    def cancel(self) -> None:
        self._cancel = True

    def _downloaded(self, res) -> None:
        self.finished_download.emit(res)
        if isinstance(res, str):
            log.warning("Aktualizace: %s", res)
            return
        log.info("Spouštím instalátor %s", res)
        try:
            flags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0   # DETACHED | NEW_PROCESS_GROUP
            subprocess.Popen(upd.installer_args(res), creationflags=flags, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            self.finished_download.emit(f"Instalátor nelze spustit: {e}")
            return
        QTimer.singleShot(300, QApplication.instance().quit)   # instalátor nahradí soubory a aplikaci znovu spustí


class UpdateDialog(QDialog):
    def __init__(self, mgr: UpdateManager, parent=None):
        super().__init__(parent)
        self.mgr = mgr
        self.setWindowTitle("NetMonitor – Aktualizace")
        self.setStyleSheet(theme.DIALOG_QSS)
        self.resize(520, 420)
        v = QVBoxLayout(self)
        self.head = QLabel()
        self.head.setWordWrap(True)
        v.addWidget(self.head)
        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        v.addWidget(self.notes, 1)
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        v.addWidget(self.bar)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setProperty("hint", True)
        v.addWidget(self.status)
        self.auto = QCheckBox("Příště instalovat aktualizace automaticky")
        self.auto.toggled.connect(self._set_auto)
        v.addWidget(self.auto)
        row = QHBoxLayout()
        self.btn_install = QPushButton("Aktualizovat nyní")
        self.btn_install.setProperty("accent", True)
        self.btn_install.clicked.connect(self._install)
        self.btn_page = QPushButton("Stránka vydání")
        self.btn_page.clicked.connect(self._open_page)
        self.btn_skip = QPushButton("Přeskočit tuto verzi")
        self.btn_skip.clicked.connect(self._skip)
        self.btn_later = QPushButton("Později")
        self.btn_later.clicked.connect(self.close)
        for b in (self.btn_install, self.btn_page, self.btn_skip):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.btn_later)
        v.addLayout(row)
        mgr.progress.connect(self._progress)
        mgr.finished_download.connect(self._finished)

    def open_for(self) -> None:
        info = self.mgr.info
        self.bar.setVisible(False)
        self.status.clear()
        self.auto.blockSignals(True)
        self.auto.setChecked(self.mgr.ctrl.cfg.general.auto_update)
        self.auto.blockSignals(False)
        if not info:
            self.head.setText(f"<b>Máte aktuální verzi {__version__}.</b>")
            self.notes.setPlainText(self.mgr.last_error or "")
            for b in (self.btn_install, self.btn_page, self.btn_skip):
                b.setVisible(False)
        else:
            self.head.setText(f"<b style='color:#38d9ff;font-size:15px'>Je dostupná nová verze {info.version}</b>"
                              f"<br>Nainstalovaná verze: {__version__}")
            self.notes.setMarkdown(info.notes or "_Bez poznámek k vydání._")
            selfinst = upd.can_self_install() and info.can_auto_install
            self.btn_install.setVisible(selfinst)
            self.btn_install.setEnabled(True)
            self.btn_page.setVisible(bool(info.page_url))
            self.btn_skip.setVisible(True)
            self.auto.setVisible(upd.can_self_install())
            if not selfinst:
                self.status.setText("Automatická instalace je dostupná jen v nainstalované verzi pro Windows. "
                                    "Novou verzi stáhněte ze stránky vydání.")
        show_dialog(self)

    def _set_auto(self, on: bool) -> None:
        cfg = self.mgr.ctrl.cfg.copy()
        cfg.general.auto_update = on
        self.mgr.ctrl.update_config(cfg)

    def _install(self) -> None:
        self.btn_install.setEnabled(False)
        self.btn_skip.setEnabled(False)
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)
        self.status.setText("Stahuji a ověřuji instalátor…")
        self.mgr.download_and_install()

    def _progress(self, done: int, total: int) -> None:
        if total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(done * 1000 / total))
        self.status.setText(f"Staženo {done / 1048576:.1f} MB" + (f" z {total / 1048576:.1f} MB" if total else ""))

    def _finished(self, res) -> None:
        if isinstance(res, str):
            self.bar.setVisible(False)
            self.btn_install.setEnabled(True)
            self.btn_skip.setEnabled(True)
            self.status.setText(f"<span style='color:#ff6b78'>✖ {res}</span>")
        else:
            self.bar.setRange(0, 1)
            self.bar.setValue(1)
            self.status.setText("✔ Ověřeno (SHA-256). Instaluji – NetMonitor se za chvíli sám znovu spustí.")

    def _open_page(self) -> None:
        if self.mgr.info and self.mgr.info.page_url.startswith("https://"):
            QDesktopServices.openUrl(QUrl(self.mgr.info.page_url))

    def _skip(self) -> None:
        self.mgr.skip()
        self.close()

    def closeEvent(self, e):
        self.mgr.cancel()
        super().closeEvent(e)


PROJECTS_URL = "https://zelina.eu/projekty.html"


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("O aplikaci NetMonitor")
        self.setStyleSheet(theme.DIALOG_QSS)
        v = QVBoxLayout(self)
        lab = QLabel(
            f"<div style='font-size:18px;color:#38d9ff'><b>NetMonitor {__version__}</b></div>"
            "<p>Desktopový widget pro sledování sítě a dostupnosti zařízení.</p>"
            "<p>Autor: Marceli Zelina<br>"
            f"Další projekty: <a style='color:#38d9ff' href='{PROJECTS_URL}'>zelina.eu/projekty</a></p>"
            "<p style='color:#8fb3d1'>Bez telemetrie – všechna data zůstávají v tomto počítači.<br>"
            "Písma Michroma a Exo 2: SIL Open Font License 1.1</p>")
        lab.setOpenExternalLinks(True)
        lab.setWordWrap(True)
        v.addWidget(lab)
        b = QPushButton("Zavřít")
        b.clicked.connect(self.close)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(b)
        v.addLayout(row)
        self.resize(420, 0)
