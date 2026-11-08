"""Dialog nastavení. Každá změna se okamžitě promítne do widgetu i monitoringu
(a s krátkou prodlevou uloží do config.json)."""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Callable

from shiboken6 import isValid

from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton, QSlider, QSpinBox,
                               QTableWidget, QTableWidgetItem, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget, QInputDialog)

from .. import __version__
from .. import platform as plat
from ..core import config as cfgmod
from ..core import target_order
from ..core.config import (ICON_CHOICES, MODE_DESKTOP, MODE_ONTOP, AppConfig, PingTarget, default_targets,
                           new_section)
from ..utils.paths import config_dir, data_dir, log_dir
from ..utils.validation import host_kind
from . import theme
from .controller import Controller

log = logging.getLogger(__name__)

ICON_LABELS = {"auto": "Automaticky", "globe": "Globus (web)", "server": "Server", "dns": "DNS / databáze",
               "wifi": "Wi-Fi / anténa", "router": "Router", "modem": "Modem", "box": "Box", "cloud": "Cloud"}


def hint(text: str) -> QLabel:
    l = QLabel(text)
    l.setWordWrap(True)
    l.setProperty("hint", True)
    return l


class TargetDialog(QDialog):
    """Přidání/úprava jednoho cíle s validací."""

    def __init__(self, target: PingTarget | None, parent=None, *, ctrl: Controller | None = None):
        super().__init__(parent)
        self.ctrl = ctrl
        self._lookup_pending = False
        self._finished = False
        self.finished.connect(self._dialog_finished)
        self.setWindowTitle("Upravit cíl" if target else "Přidat cíl")
        self.setStyleSheet(theme.DIALOG_QSS)
        self.t = target or PingTarget(name="", host="")
        f = QFormLayout(self)
        self.name = QLineEdit(self.t.name)
        self.host = QLineEdit(self.t.host)
        self.host.setPlaceholderText("např. 8.8.8.8, 2001:4860:4860::8888 nebo seznam.cz")
        self.enabled = QCheckBox("Aktivní")
        self.enabled.setChecked(self.t.enabled)
        self.interval = QDoubleSpinBox(minimum=1, maximum=60, decimals=1, singleStep=0.5, suffix=" s")
        self.interval.setValue(self.t.interval_s)
        self.timeout = QDoubleSpinBox(minimum=0.2, maximum=10, decimals=1, singleStep=0.1, suffix=" s")
        self.timeout.setValue(self.t.timeout_s)
        self.retries = QSpinBox(minimum=1, maximum=20)
        self.retries.setValue(self.t.retries)
        self.icon = QComboBox()
        for k in ICON_CHOICES:
            self.icon.addItem(ICON_LABELS[k], k)
        self.icon.setCurrentIndex(ICON_CHOICES.index(self.t.icon))
        self.show_rtt = QCheckBox("Zobrazovat odezvu v ms (jinak 'OK')")
        self.show_rtt.setChecked(self.t.show_rtt)
        self.probe = QComboBox()
        for k, lab in (("auto", "Automaticky (veřejné adresy ano)"), ("yes", "Ano"), ("no", "Ne")):
            self.probe.addItem(lab, k)
        self.probe.setCurrentIndex(["auto", "yes", "no"].index(self.t.internet_probe))
        self.mac = QLineEdit(self.t.mac)
        self.mac.setPlaceholderText("pro Wake-on-LAN, např. 04:D9:F5:15:34:86 (nepovinné)")
        self.btn_mac = QPushButton("Zjistit")
        self.btn_mac.setEnabled(ctrl is not None)
        self.btn_mac.setToolTip("Pingne zařízení a přečte jeho MAC z ARP tabulky (musí být zapnuté a v této síti)")
        self.btn_mac.clicked.connect(self._detect_mac)
        mac_row = QHBoxLayout()
        mac_row.addWidget(self.mac, 1)
        mac_row.addWidget(self.btn_mac)
        self.wol_bc = QLineEdit(self.t.wol_broadcast)
        self.wol_bc.setPlaceholderText("automaticky (jen pro zařízení v jiné podsíti)")
        self.err = QLabel()
        self.err.setProperty("error", True)
        self.err.setWordWrap(True)
        f.addRow("Název", self.name)
        f.addRow("IP / doména", self.host)
        f.addRow("", self.enabled)
        f.addRow("Interval", self.interval)
        f.addRow("Timeout", self.timeout)
        f.addRow("Opakování před výpadkem", self.retries)
        f.addRow("Ikona", self.icon)
        f.addRow("", self.show_rtt)
        f.addRow("Počítat do stavu internetu", self.probe)
        f.addRow("MAC (Wake-on-LAN)", mac_row)
        f.addRow("Broadcast pro WoL", self.wol_bc)
        f.addRow(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.host.textChanged.connect(self._live_validate)
        self.resize(460, 0)

    def _live_validate(self, text: str) -> None:
        bad = bool(text.strip()) and host_kind(text) == "invalid"
        self.host.setProperty("invalid", bad)
        self.host.style().unpolish(self.host)
        self.host.style().polish(self.host)

    def result_target(self) -> PingTarget:
        return PingTarget(name=self.name.text().strip() or self.host.text().strip(), host=self.host.text().strip(),
                          enabled=self.enabled.isChecked(), interval_s=self.interval.value(),
                          timeout_s=self.timeout.value(), retries=self.retries.value(),
                          icon=self.icon.currentData(), show_rtt=self.show_rtt.isChecked(),
                          internet_probe=self.probe.currentData(), id=self.t.id,
                          mac=self.mac.text().strip(), wol_broadcast=self.wol_bc.text().strip())

    def _detect_mac(self) -> None:
        if self.ctrl is None or self._lookup_pending:
            return
        host = self.host.text().strip()
        if not host or host_kind(host) == "invalid":
            self.err.setText("Zadejte platnou IP adresu nebo doménu.")
            return
        old_mac = self.mac.text()
        self._lookup_pending = True
        self.btn_mac.setEnabled(False)
        self.btn_mac.setText("Zjišťuji…")
        self.err.setText("")

        def finished(res):
            if not isValid(self) or self._finished:
                return
            self._lookup_pending = False
            self.btn_mac.setEnabled(True)
            self.btn_mac.setText("Zjistit")
            if self.host.text().strip() != host or self.mac.text() != old_mac:
                self.err.setText("Host nebo MAC se během zjišťování změnily; výsledek nebyl použit.")
                return
            mac, err = res
            if mac:
                self.mac.setText(mac)
            self.err.setText(err)

        self.ctrl.detect_mac(f"mac-editor-{id(self)}", host, finished)

    def _dialog_finished(self, _result: int) -> None:
        self._finished = True

    def _accept(self) -> None:
        if self.mac.text().strip():
            from ..core import wol
            try:
                self.mac.setText(wol.format_mac(self.mac.text()))
            except ValueError:
                pass
        errs = self.result_target().validate()
        if errs:
            self.err.setText("\n".join(errs))
            return
        self.accept()


ROLE_SECTION = Qt.ItemDataRole.UserRole + 1


class TargetTree(QTreeWidget):
    """Strom cílů: sekce = uzly, cíle = listy. Přetahování myší (InternalMove).

    Pravidla: cíl nemůže mít potomky; sekci nelze vložit do jiné sekce.
    Po přetažení se odešle nové ploché pořadí ID a strom se znovu sestaví z konfigurace.
    """
    order_changed = Signal(list)

    def __init__(self, cols: list[str]):
        super().__init__()
        self.setColumnCount(len(cols))
        self.setHeaderLabels(cols)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setAlternatingRowColors(True)
        self.setRootIsDecorated(True)
        self.setIndentation(18)
        self.setUniformRowHeights(True)
        hh = self.header()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.filling = False
        self.select_id: str | None = None

    def fill(self, targets: list[PingTarget], selected: str | None = None) -> None:
        self.filling = True
        self.blockSignals(True)
        want = self.select_id or selected
        self.select_id = None
        try:
            self.clear()
            parent = None
            sel_item = None
            bold = self.font()
            bold.setBold(True)
            for t in targets:
                it = QTreeWidgetItem()
                it.setData(0, Qt.ItemDataRole.UserRole, t.id)
                if t.is_section:
                    it.setData(0, ROLE_SECTION, True)
                    it.setText(0, t.name.upper())
                    it.setFont(0, bold)
                    it.setForeground(0, QColor("#38d9ff"))
                    it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable |
                                Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled)
                    self.addTopLevelItem(it)
                    it.setFirstColumnSpanned(True)
                    parent = it
                else:
                    it.setData(0, ROLE_SECTION, False)
                    for c, val in enumerate([t.name, t.host, f"{t.interval_s:g} s", f"{t.timeout_s:g} s",
                                             str(t.retries), "ms" if t.show_rtt else "OK"]):
                        it.setText(c, val)
                    it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable |
                                Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
                    it.setCheckState(0, Qt.CheckState.Checked if t.enabled else Qt.CheckState.Unchecked)
                    if not t.enabled:
                        for c in range(self.columnCount()):
                            it.setForeground(c, QColor("#6a86a0"))
                    if parent is None:
                        self.addTopLevelItem(it)
                    else:
                        parent.addChild(it)
                if t.id == want:
                    sel_item = it
            self.expandAll()
            if sel_item is not None:
                self.setCurrentItem(sel_item)
                self.scrollToItem(sel_item)
        finally:
            self.blockSignals(False)
            self.filling = False

    def selected_id(self) -> str | None:
        it = self.currentItem()
        if it is None or not it.isSelected():
            return None
        return it.data(0, Qt.ItemDataRole.UserRole)

    def flat_ids(self) -> list[str]:
        ids = []
        for i in range(self.topLevelItemCount()):
            top = self.topLevelItem(i)
            ids.append(top.data(0, Qt.ItemDataRole.UserRole))
            for j in range(top.childCount()):
                ids.append(top.child(j).data(0, Qt.ItemDataRole.UserRole))
        return ids

    def _drop_parent(self, e):
        """Rodič, do kterého by položka po upuštění patřila (None = nejvyšší úroveň)."""
        target = self.itemAt(e.position().toPoint())
        if target is None:
            return None
        pos = self.dropIndicatorPosition()
        if pos == QAbstractItemView.DropIndicatorPosition.OnItem:
            return target
        return target.parent()

    def dragMoveEvent(self, e):
        super().dragMoveEvent(e)
        dragged = self.currentItem()
        if dragged is not None and dragged.data(0, ROLE_SECTION) and self._drop_parent(e) is not None:
            e.ignore()          # sekce nesmí do sekce

    def dropEvent(self, e):
        dragged = self.currentItem()
        dragged_id = dragged.data(0, Qt.ItemDataRole.UserRole) if dragged else None
        if dragged is not None and dragged.data(0, ROLE_SECTION) and self._drop_parent(e) is not None:
            e.ignore()
            return
        super().dropEvent(e)
        self.select_id = dragged_id
        self.order_changed.emit(self.flat_ids())


class SettingsDialog(QDialog):
    history_requested = Signal(str)
    check_updates_requested = Signal()

    def __init__(self, ctrl: Controller, main_widget, parent=None):
        super().__init__(parent)
        self.ctrl = ctrl
        self.mw = main_widget
        self.setWindowTitle(f"NetMonitor {__version__} – Nastavení")
        self.setStyleSheet(theme.DIALOG_QSS)
        self.resize(820, 640)
        self._loading = False
        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs)
        self.tabs.addTab(self._tab_general(), "Obecné")
        self.tabs.addTab(self._tab_appearance(), "Vzhled")
        self.tabs.addTab(self._tab_network(), "Síť")
        self.tabs.addTab(self._tab_targets(), "Ping cíle")
        self.tabs.addTab(self._tab_notifications(), "Upozornění")
        self.tabs.addTab(self._tab_history(), "Historie")
        bottom = QHBoxLayout()
        self.status = hint("Změny se projeví okamžitě a automaticky se uloží.")
        bottom.addWidget(self.status, 1)
        close = QPushButton("Zavřít")
        close.clicked.connect(self.close)
        bottom.addWidget(close)
        lay.addLayout(bottom)
        ctrl.config_changed.connect(self._on_external_change)
        self.load()

    # ------------------------------------------------------------------ pomocné
    def _apply(self, fn: Callable[[AppConfig], None]) -> None:
        if self._loading:
            return
        cfg = self.ctrl.cfg.copy()
        fn(cfg)
        self._applying = True
        try:
            self.ctrl.update_config(cfg)
        finally:
            self._applying = False

    def _setter(self, section: str, key: str, conv=lambda v: v):
        return lambda v: self._apply(lambda c: setattr(getattr(c, section), key, conv(v)))

    def _check(self, text: str, section: str, key: str) -> QCheckBox:
        cb = QCheckBox(text)
        cb.toggled.connect(self._setter(section, key, bool))
        self._binds.append((cb, section, key))
        return cb

    def _spin(self, section: str, key: str, lo, hi, suffix: str = "", step=1, decimals: int | None = None):
        sp = QDoubleSpinBox(decimals=decimals) if decimals is not None else QSpinBox()
        sp.setRange(lo, hi)
        sp.setSingleStep(step)
        if suffix:
            sp.setSuffix(suffix)
        sp.setKeyboardTracking(False)
        sp.valueChanged.connect(self._setter(section, key, float if decimals is not None else int))
        self._binds.append((sp, section, key))
        return sp

    _binds: list = []

    def _on_external_change(self, cfg: AppConfig) -> None:
        # změna vyvolaná tímto dialogem: ovládací prvky už odpovídají. Synchronní znovunačtení by navíc
        # smazalo položku stromu, jejíž signál (např. zaškrtnutí) se právě zpracovává -> pád (use-after-free).
        if not self.isVisible() or self._loading or getattr(self, "_applying", False):
            return
        if not getattr(self, "_reload_pending", False):
            self._reload_pending = True

            def later():
                self._reload_pending = False
                if self.isVisible() and not self._loading:
                    self.load()
            QTimer.singleShot(0, later)

    def load(self) -> None:
        """Načte hodnoty z aktuální konfigurace do ovládacích prvků (bez vyvolání změn)."""
        self._loading = True
        try:
            c = self.ctrl.cfg
            for w, section, key in self._binds:
                v = getattr(getattr(c, section), key)
                if isinstance(w, QCheckBox):
                    w.setChecked(bool(v))
                elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                    w.setValue(v)
            g, a, n = c.general, c.appearance, c.network
            self.lang.setCurrentIndex(0 if g.language == "cs" else 1)
            self.update_url.setText(g.update_url)
            self.rb_desktop.setChecked(a.mode == MODE_DESKTOP)
            self.rb_ontop.setChecked(a.mode == MODE_ONTOP)
            self.opacity.setValue(int(round((1 - a.opacity) * 100)))
            self.opacity_lbl.setText(f"{self.opacity.value()} %")
            self.glass.setValue(int(round((1 - a.glass_alpha) * 100)))
            x = self.glass.value()
            self.glass_lbl.setText(f"{x} %" + ("  neprůhledné" if x == 0 else "  jen text" if x == 100 else ""))
            self.w_spin.setValue(self.mw.width())
            self.h_spin.setValue(self.mw.height())
            for k, b in self.color_btns.items():
                self._paint_color_btn(b, getattr(a.colors, k))
            self.notice.setText(self.mw.platform_notice)
            self.notice.setVisible(bool(self.mw.platform_notice))
            self.rb_auto.setChecked(n.adapter_auto)
            self.rb_manual.setChecked(not n.adapter_auto)
            self._fill_adapters()
            self.isp_provider.setCurrentIndex(0 if n.isp_provider == "ipinfo" else 1)
            self.green.setValue(c.ping.thresholds.green_max_ms)
            self.orange.setValue(c.ping.thresholds.orange_max_ms)
            self.pinger_info.setText(f"Použitá ICMP implementace: <b>{self.ctrl.pinger.name}</b>")
            self._fill_table()
        finally:
            self._loading = False

    # ------------------------------------------------------------------ Obecné
    def _tab_general(self) -> QWidget:
        self._binds = []
        w = QWidget()
        f = QFormLayout(w)
        self.lang = QComboBox()
        self.lang.addItems(["Čeština", "English (připraveno – částečný překlad)"])
        self.lang.currentIndexChanged.connect(
            lambda i: self._apply(lambda c: setattr(c.general, "language", "cs" if i == 0 else "en")))
        f.addRow("Jazyk", self.lang)
        f.addRow(hint("Změna jazyka se plně projeví po restartu aplikace."))
        self.autostart = QCheckBox("Spustit automaticky po přihlášení")
        self.autostart.toggled.connect(self._toggle_autostart)
        self._binds.append((self.autostart, "general", "autostart"))
        f.addRow(self.autostart)
        f.addRow(self._check("Spouštět minimalizované (jen ikona v oznamovací oblasti)", "general", "start_minimized"))
        f.addRow(self._check("Při skrytí minimalizovat do oznamovací oblasti", "general", "minimize_to_tray"))
        f.addRow(self._check("Kontrolovat nové verze (při startu a každých 6 hodin)", "general", "check_updates"))
        f.addRow(self._check("Instalovat aktualizace automaticky (bez ptaní)", "general", "auto_update"))
        self.update_url = QLineEdit()
        self.update_url.setPlaceholderText("https://github.com/<účet>/<repozitář>/releases  nebo JSON manifest")
        self.update_url.editingFinished.connect(
            lambda: self._apply(lambda c: setattr(c.general, "update_url", self.update_url.text().strip())))
        urow = QHBoxLayout()
        urow.addWidget(self.update_url, 1)
        chk = QPushButton("Zkontrolovat nyní")
        chk.clicked.connect(self.check_updates_requested.emit)
        urow.addWidget(chk)
        f.addRow("Zdroj aktualizací", urow)
        row = QHBoxLayout()
        for label, path in (("Složka konfigurace", config_dir()), ("Složka dat", data_dir()), ("Logy", log_dir())):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, p=path: QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))))
            row.addWidget(b)
        f.addRow(row)
        f.addRow(hint(f"Konfigurace: {cfgmod.default_config_path()}\nBez telemetrie, bez účtů, "
                      f"veškerá data zůstávají v tomto počítači."))
        link = QLabel(f"NetMonitor {__version__} · další projekty: "
                      "<a style='color:#38d9ff' href='https://zelina.eu/projekty.html'>zelina.eu/projekty</a>")
        link.setOpenExternalLinks(True)
        f.addRow(link)
        return w

    def _toggle_autostart(self, on: bool) -> None:
        if self._loading:
            return
        try:
            plat.current().set_autostart(on, self.ctrl.autostart_command())
            self._apply(lambda c: setattr(c.general, "autostart", on))
            self.status.setText("Automatické spuštění " + ("zapnuto." if on else "vypnuto."))
        except Exception as e:  # noqa: BLE001
            log.exception("Autostart")
            QMessageBox.warning(self, "Autostart", f"Nepodařilo se nastavit automatické spuštění:\n{e}")

    # ------------------------------------------------------------------ Vzhled
    def _tab_appearance(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        gm = QGroupBox("Režim zobrazení")
        gl = QVBoxLayout(gm)
        self.rb_desktop = QRadioButton("A – Desktop widget (pod běžnými okny, bez titulku)")
        self.rb_ontop = QRadioButton("B – Vždy navrchu")
        self.rb_desktop.toggled.connect(lambda on: on and self._apply(lambda c: setattr(c.appearance, "mode", MODE_DESKTOP)))
        self.rb_ontop.toggled.connect(lambda on: on and self._apply(lambda c: setattr(c.appearance, "mode", MODE_ONTOP)))
        gl.addWidget(self.rb_desktop)
        gl.addWidget(self.rb_ontop)
        self.notice = hint("")
        gl.addWidget(self.notice)
        v.addWidget(gm)

        f = QFormLayout()
        self.glass = QSlider(Qt.Orientation.Horizontal, minimum=0, maximum=100)
        self.glass_lbl = QLabel()
        self.glass_lbl.setMinimumWidth(130)

        def glass_changed(x: int) -> None:
            self.glass_lbl.setText(f"{x} %" + ("  neprůhledné" if x == 0 else "  jen text" if x == 100 else ""))
            self._apply(lambda c: setattr(c.appearance, "glass_alpha", 1 - x / 100))
        self.glass.valueChanged.connect(glass_changed)
        gr = QHBoxLayout()
        gr.addWidget(self.glass, 1)
        gr.addWidget(self.glass_lbl)
        f.addRow("Průhlednost pozadí", gr)
        self.opacity = QSlider(Qt.Orientation.Horizontal, minimum=0, maximum=80)
        self.opacity_lbl = QLabel()
        self.opacity_lbl.setMinimumWidth(130)

        def opacity_changed(x: int) -> None:
            self.opacity_lbl.setText(f"{x} %")
            self._apply(lambda c: setattr(c.appearance, "opacity", 1 - x / 100))
        self.opacity.valueChanged.connect(opacity_changed)
        orow = QHBoxLayout()
        orow.addWidget(self.opacity, 1)
        orow.addWidget(self.opacity_lbl)
        f.addRow("Průhlednost celého widgetu (i text)", orow)
        size = QHBoxLayout()
        self.w_spin = QSpinBox(minimum=260, maximum=2000, suffix=" px", keyboardTracking=False)
        self.h_spin = QSpinBox(minimum=340, maximum=2400, suffix=" px", keyboardTracking=False)
        self.w_spin.valueChanged.connect(lambda _: self._resize_from_spins())
        self.h_spin.valueChanged.connect(lambda _: self._resize_from_spins())
        size.addWidget(self.w_spin)
        size.addWidget(QLabel("×"))
        size.addWidget(self.h_spin)
        reset = QPushButton("Výchozí 600 × 850")
        reset.clicked.connect(lambda: (self.w_spin.setValue(600), self.h_spin.setValue(850)))
        size.addWidget(reset)
        f.addRow("Velikost (logické px, respektuje škálování)", size)
        pos = QPushButton("Přesunout do pravého horního rohu")
        pos.clicked.connect(self._reset_position)
        f.addRow("Pozice", pos)
        f.addRow(self._check("Uzamknout pozici a velikost", "appearance", "locked"))
        ct = self._check("Ignorovat kliknutí myší (click-through) – vypnout lze z ikony v oznamovací oblasti",
                         "appearance", "click_through")
        from PySide6.QtWidgets import QApplication
        if not QApplication.instance().property("tray_available"):
            ct.setEnabled(False)
            ct.setToolTip("Není dostupná oznamovací oblast – click-through by nešel vypnout")
        f.addRow(ct)
        f.addRow(self._check("Neonové efekty (záře)", "appearance", "neon_effects"))
        f.addRow(self._check("Kompaktní režim", "appearance", "compact"))
        f.addRow("Velikost písma", self._spin("appearance", "font_scale", 0.6, 2.0, " ×", 0.05, 2))
        colors = QHBoxLayout()
        self.color_btns = {}
        for k, label in (("ok", "OK"), ("warn", "Varování"), ("bad", "Chyba"), ("unknown", "Neznámé")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, k=k: self._pick_color(k))
            self.color_btns[k] = b
            colors.addWidget(b)
        f.addRow("Barvy stavů", colors)
        v.addLayout(f)
        v.addStretch(1)
        return w

    def _resize_from_spins(self) -> None:
        if self._loading:
            return
        self.mw.set_size(self.w_spin.value(), self.h_spin.value())

    def _reset_position(self) -> None:
        scr = self.mw.screen().availableGeometry()
        self.mw.move(scr.right() - self.mw.width() - 40, scr.top() + 40)
        self.mw.remember_geometry()

    def _paint_color_btn(self, b: QPushButton, color: str) -> None:
        b.setStyleSheet(f"QPushButton{{border:2px solid {color}; color:{color};}}")

    def _pick_color(self, key: str) -> None:
        cur = QColor(getattr(self.ctrl.cfg.appearance.colors, key))
        c = QColorDialog.getColor(cur, self, "Barva stavu")
        if c.isValid():
            self._apply(lambda cfg: setattr(cfg.appearance.colors, key, c.name()))
            self._paint_color_btn(self.color_btns[key], c.name())

    # ------------------------------------------------------------------ Síť
    def _tab_network(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        ga = QGroupBox("Síťový adaptér")
        fa = QFormLayout(ga)
        self.rb_auto = QRadioButton("Automaticky (rozhraní s výchozí trasou)")
        self.rb_manual = QRadioButton("Ručně:")
        self.adapters = QComboBox()
        self.rb_auto.toggled.connect(lambda on: self._apply(lambda c: setattr(c.network, "adapter_auto", on)))
        self.adapters.currentIndexChanged.connect(self._adapter_chosen)
        fa.addRow(self.rb_auto)
        m = QHBoxLayout()
        m.addWidget(self.rb_manual)
        m.addWidget(self.adapters, 1)
        fa.addRow(m)
        fa.addRow("Obnovovat síťové údaje každých", self._spin("network", "refresh_s", 2, 300, " s"))
        fa.addRow(self._check("Zobrazit IPv6", "network", "show_ipv6"))
        fa.addRow(self._check("Pingovat výchozí bránu (pro rozlišení LAN × internet)", "network", "gateway_probe"))
        if sys.platform.startswith("linux"):
            fa.addRow(self._check("Na Waylandu použít XWayland (pozice a režim okna) – po restartu",
                                  "network", "linux_prefer_xwayland"))
        v.addWidget(ga)

        ge = QGroupBox("Externí služby (WAN IP, ISP)")
        fe = QFormLayout(ge)
        fe.addRow(self._check("Povolit externí dotazy (HTTPS, bez klíče, bez osobních dat)", "network", "external_api"))
        fe.addRow("Interval aktualizace WAN IP", self._spin("network", "wan_interval_s", 30, 86400, " s", 30))
        fe.addRow(self._check("Detekovat ISP / ASN", "network", "isp_detection"))
        self.isp_provider = QComboBox()
        self.isp_provider.addItems(["ipinfo.io (formát 'AS… název')", "RIPEstat (RIPE NCC)"])
        self.isp_provider.currentIndexChanged.connect(
            lambda i: self._apply(lambda c: setattr(c.network, "isp_provider", "ipinfo" if i == 0 else "ripestat")))
        fe.addRow("Zdroj ISP", self.isp_provider)
        fe.addRow("Cache ISP", self._spin("network", "isp_cache_hours", 1, 720, " h"))
        fe.addRow(hint("WAN IP: api.ipify.org → ipv4.icanhazip.com → checkip.amazonaws.com → ifconfig.me "
                       "(záložní řetězec). Podrobnosti v docs/EXTERNAL_APIS.md."))
        v.addWidget(ge)

        gp = QGroupBox("Ping")
        fp = QFormLayout(gp)
        self.green = QDoubleSpinBox(minimum=1, maximum=10000, suffix=" ms", decimals=0, keyboardTracking=False)
        self.orange = QDoubleSpinBox(minimum=1, maximum=20000, suffix=" ms", decimals=0, keyboardTracking=False)
        self.green.valueChanged.connect(lambda x: self._apply(lambda c: setattr(c.ping.thresholds, "green_max_ms", x)))
        self.orange.valueChanged.connect(lambda x: self._apply(lambda c: setattr(c.ping.thresholds, "orange_max_ms", x)))
        fp.addRow("Zelená do", self.green)
        fp.addRow("Oranžová do (nad = červená)", self.orange)
        fp.addRow("Max. současných pingů", self._spin("ping", "max_concurrent", 1, 128))
        self.pinger_info = QLabel()
        fp.addRow(self.pinger_info)
        v.addWidget(gp)
        v.addStretch(1)
        return w

    def _fill_adapters(self) -> None:
        self.adapters.blockSignals(True)
        self.adapters.clear()
        names = self.ctrl.net_provider.list_interfaces()
        cur = self.ctrl.cfg.network.adapter_name
        if cur and cur not in names:
            names.append(cur)
        for n in names:
            self.adapters.addItem(n, n)
        if cur:
            self.adapters.setCurrentIndex(max(0, names.index(cur)))
        self.adapters.blockSignals(False)

    def _adapter_chosen(self, _i: int) -> None:
        name = self.adapters.currentData()
        if name:
            self.rb_manual.setChecked(True)
            self._apply(lambda c: (setattr(c.network, "adapter_name", name), setattr(c.network, "adapter_auto", False)))

    # ------------------------------------------------------------------ Ping cíle
    COLS = ["Název", "IP / doména", "Interval", "Timeout", "Opak.", "Zobrazení"]

    def _tab_targets(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.table = TargetTree(self.COLS)
        self.table.itemChanged.connect(self._table_item_changed)
        self.table.itemDoubleClicked.connect(lambda _it, _c: self._edit_target())
        self.table.order_changed.connect(self._order_from_tree)
        v.addWidget(self.table, 1)
        row1 = QHBoxLayout()
        for label, fn in (("Přidat cíl", self._add_target), ("Přidat sekci", self._add_section),
                          ("Upravit", self._edit_target), ("Smazat", self._del_target),
                          ("▲", lambda: self._move(-1)), ("▼", lambda: self._move(1)),
                          ("Testovat nyní", self._test_target), ("Graf…", self._graph_target)):
            b = QPushButton(label)
            if label in ("▲", "▼"):
                b.setFixedWidth(40)
                b.setToolTip("Posunout nahoru" if label == "▲" else "Posunout dolů")
            b.clicked.connect(fn)
            row1.addWidget(b)
        v.addLayout(row1)
        row2 = QHBoxLayout()
        for label, fn in (("Export konfigurace…", self._export), ("Import konfigurace…", self._import),
                          ("Obnovit výchozí cíle", self._reset_targets)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row2.addWidget(b)
        row2.addStretch(1)
        v.addLayout(row2)
        v.addWidget(hint("Pořadí měníte přetažením myší (i mezi sekcemi) nebo šipkami ▲▼. Sekce se přesouvá celá "
                         "i se svými cíli. Nový cíl se vloží za vybraný řádek. Dvojklik = úprava, zaškrtávátko = "
                         "zapnout/vypnout. Stejná IP adresa u více zařízení je povolená."))
        return w

    def _fill_table(self) -> None:
        sel = self._selected_id()
        self.table.fill(self.ctrl.cfg.ping.targets, sel)

    def _selected_id(self) -> str | None:
        return self.table.selected_id()

    def _table_item_changed(self, item, column: int) -> None:
        if column != 0 or self._loading or self.table.filling:
            return
        tid = item.data(0, Qt.ItemDataRole.UserRole)
        if not tid or item.data(0, ROLE_SECTION):
            return
        on = item.checkState(0) == Qt.CheckState.Checked

        def fn(c: AppConfig):
            t = c.target_by_id(tid)
            if t:
                t.enabled = on
        self._apply(fn)

    def _order_from_tree(self, ids: list) -> None:
        self._apply(lambda c: setattr(c.ping, "targets", target_order.reorder(c.ping.targets, ids)))
        self._fill_table()

    def _add_target(self) -> None:
        d = TargetDialog(None, self, ctrl=self.ctrl)
        if d.exec():
            t = d.result_target()
            anchor = self._selected_id()
            self._apply(lambda c: setattr(c.ping, "targets", target_order.insert_target(c.ping.targets, t, anchor)))
            self.table.select_id = t.id
            self._fill_table()

    def _add_section(self) -> None:
        name, ok = QInputDialog.getText(self, "Přidat sekci", "Název sekce (např. DNS, DOMÉNY, ZAŘÍZENÍ):")
        if ok and name.strip():
            sec = new_section(name)
            self._apply(lambda c: c.ping.targets.append(sec))
            self.table.select_id = sec.id
            self._fill_table()
            self.status.setText("Sekce přidána na konec – přetáhněte do ní cíle myší.")

    def _edit_target(self) -> None:
        tid = self._selected_id()
        t = self.ctrl.cfg.target_by_id(tid) if tid else None
        if not t:
            return
        if t.is_section:
            name, ok = QInputDialog.getText(self, "Přejmenovat sekci", "Název sekce:", text=t.name)
            if ok and name.strip():
                def fn(c: AppConfig):
                    x = c.target_by_id(tid)
                    if x:
                        x.name = name.strip()
                self._apply(fn)
                self._fill_table()
            return
        d = TargetDialog(t, self, ctrl=self.ctrl)
        if d.exec():
            nt = d.result_target()
            self._apply(lambda c: c.ping.targets.__setitem__(
                next(i for i, x in enumerate(c.ping.targets) if x.id == tid), nt))
            self._fill_table()

    def _del_target(self) -> None:
        tid = self._selected_id()
        t = self.ctrl.cfg.target_by_id(tid) if tid else None
        if not t:
            return
        if t.is_section:
            msg = f"Smazat sekci '{t.name}'?\nCíle v ní zůstanou zachované (připojí se k předchozí sekci)."
        else:
            msg = f"Smazat '{t.name}' ({t.host})?\nHistorie v databázi zůstane."
        if QMessageBox.question(self, "Smazat", msg) != QMessageBox.StandardButton.Yes:
            return
        self._apply(lambda c: setattr(c.ping, "targets", target_order.remove(c.ping.targets, tid)))
        self._fill_table()

    def _move(self, d: int) -> None:
        tid = self._selected_id()
        if not tid:
            return
        self._apply(lambda c: setattr(c.ping, "targets", target_order.move(c.ping.targets, tid, d)))
        self.table.select_id = tid
        self._fill_table()

    def _test_target(self) -> None:
        tid = self._selected_id()
        t = self.ctrl.cfg.target_by_id(tid) if tid else None
        if t and t.is_section:
            self.status.setText("Vyberte cíl, ne sekci.")
            return
        if tid and self.ctrl.engine.test_now(tid):
            self.status.setText("Test odeslán – výsledek se zobrazí ve widgetu.")
        elif tid:
            self.status.setText("Cíl je vypnutý – zapněte jej pro test.")

    def _graph_target(self) -> None:
        tid = self._selected_id()
        t = self.ctrl.cfg.target_by_id(tid) if tid else None
        if t and not t.is_section:
            self.history_requested.emit(tid)

    def _export(self) -> None:
        fn, _ = QFileDialog.getSaveFileName(self, "Export konfigurace",
                                            str(Path.home() / f"netmonitor-config-{time.strftime('%Y%m%d')}.json"),
                                            "JSON (*.json)")
        if fn:
            try:
                cfgmod.export_config(self.ctrl.cfg, Path(fn))
                self.status.setText(f"Exportováno: {fn}")
            except OSError as e:
                QMessageBox.warning(self, "Export", str(e))

    def _import(self) -> None:
        fn, _ = QFileDialog.getOpenFileName(self, "Import konfigurace", str(Path.home()), "JSON (*.json)")
        if not fn:
            return
        try:
            cfg = cfgmod.import_config(Path(fn), self.ctrl.cfg)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Import", f"Soubor nelze načíst:\n{e}")
            return
        cfg.appearance.x, cfg.appearance.y = self.ctrl.cfg.appearance.x, self.ctrl.cfg.appearance.y
        self.ctrl.update_config(cfg)
        self.load()
        self.status.setText(f"Importováno: {fn}")

    def _reset_targets(self) -> None:
        if QMessageBox.question(self, "Výchozí cíle", "Nahradit seznam cílů výchozími 10 položkami?") \
                == QMessageBox.StandardButton.Yes:
            self._apply(lambda c: setattr(c.ping, "targets", default_targets()))
            self._fill_table()

    # ------------------------------------------------------------------ Upozornění
    def _tab_notifications(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.addRow(self._check("Povolit lokální upozornění", "notifications", "enabled"))
        for key, label in (("target_down", "Ztráta dostupnosti cíle"), ("target_up", "Obnovení dostupnosti cíle"),
                           ("wan_change", "Změna WAN IP"), ("internet_down", "Výpadek internetu"),
                           ("internet_up", "Obnovení internetu"), ("high_latency", "Dlouhodobě vysoká latence")):
            f.addRow(self._check(label, "notifications", key))
        f.addRow("Prodleva před upozorněním na výpadek", self._spin("notifications", "delay_s", 0, 3600, " s"))
        f.addRow("Min. odstup opakovaných upozornění", self._spin("notifications", "cooldown_s", 0, 86400, " s", 30))
        f.addRow("Vysoká latence nad", self._spin("notifications", "high_latency_ms", 1, 20000, " ms", 10, 0))
        f.addRow("… trvající alespoň", self._spin("notifications", "high_latency_duration_s", 10, 86400, " s", 10))
        f.addRow(hint("Výpadek se hlásí až po uplynutí prodlevy; krátké výpadky se tak nehlásí vůbec. "
                      "Obnovení se hlásí jen tehdy, pokud byl ohlášen výpadek."))
        return w

    # ------------------------------------------------------------------ Historie
    def _tab_history(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.addRow(self._check("Ukládat historii měření", "history", "enabled"))
        f.addRow("Doba uchování", self._spin("history", "retention_days", 1, 3650, " dní"))
        b = QPushButton("Otevřít historii a grafy…")
        b.clicked.connect(lambda: self.history_requested.emit(""))
        f.addRow(b)
        f.addRow(hint(f"Databáze: {self.ctrl.history.path}\nMěření se agregují po minutách (min/průměr/max, "
                      "počty OK/chyb) a zapisují dávkově 1× za minutu – disk se zbytečně nezatěžuje. "
                      "Události výpadků se ukládají okamžitě a odděleně."))
        return w
