"""Síťové nástroje: Traceroute, Test rychlosti internetu, Wake-on-LAN."""
from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
                               QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..core import speedtest, traceroute, wol
from ..core.config import PingTarget
from ..core.ping_engine import Resolver, ping_host
from . import theme
from .ping_panel import fmt_rtt

log = logging.getLogger(__name__)


class _Bridge(QObject):
    hop = Signal(object)
    done = Signal(object, str)
    progress = Signal(str, float, object)
    result = Signal(object)


# ---------------------------------------------------------------------------- Traceroute

class TracerouteDialog(QDialog):
    COLS = ["#", "IP adresa", "Odezva 1", "Odezva 2", "Odezva 3", "Ztráta"]

    def __init__(self, ctrl, target: PingTarget, parent=None):
        super().__init__(parent)
        self.ctrl = ctrl
        self.target = target
        self.setWindowTitle(f"Traceroute – {target.name} ({target.host})")
        self.setStyleSheet(theme.DIALOG_QSS)
        self.resize(640, 520)
        v = QVBoxLayout(self)
        self.head = QLabel()
        self.head.setWordWrap(True)
        v.addWidget(self.head)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.table, 1)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setProperty("hint", True)
        v.addWidget(self.status)
        row = QHBoxLayout()
        self.btn_run = QPushButton("Spustit znovu")
        self.btn_run.clicked.connect(self.start)
        self.btn_copy = QPushButton("Kopírovat")
        self.btn_copy.clicked.connect(self._copy)
        close = QPushButton("Zavřít")
        close.clicked.connect(self.close)
        row.addWidget(self.btn_run)
        row.addWidget(self.btn_copy)
        row.addStretch(1)
        row.addWidget(close)
        v.addLayout(row)
        self.bridge = _Bridge()
        self.bridge.hop.connect(self._add_hop)
        self.bridge.done.connect(self._finished)
        self._cancel = False
        self._thread: threading.Thread | None = None
        self._closing_result: int | None = None
        self.hops: list[traceroute.Hop] = []

    def start(self) -> None:
        if self._closing_result is not None or (self._thread and self._thread.is_alive()):
            return
        self._cancel = False
        self.hops = []
        self.table.setRowCount(0)
        self.btn_run.setEnabled(False)
        self.head.setText(f"<b>Trasa k {self.target.host}</b> – max. 30 skoků, 3 sondy na skok")
        self.status.setText("Probíhá… (každý neodpovídající skok trvá ~3 s)")

        def job():
            try:
                ip, err = traceroute.run(self.target.host, self.bridge.hop.emit, cancelled=lambda: self._cancel)
            except Exception as e:  # noqa: BLE001
                log.exception("traceroute")
                ip, err = None, str(e)
            self.bridge.done.emit(ip, err)
        self._thread = threading.Thread(target=job, name="traceroute", daemon=True)
        self._thread.start()

    def busy(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _add_hop(self, hop: traceroute.Hop) -> None:
        self.hops.append(hop)
        r = self.table.rowCount()
        self.table.insertRow(r)
        rtts = (hop.rtts + [None, None, None])[:3]
        vals = [str(hop.n), hop.ip or "* (neodpovídá)"] + [fmt_rtt(x) if x is not None else "*" for x in rtts] + \
               [f"{hop.loss_pct:.0f} %"]
        for c, val in enumerate(vals):
            it = QTableWidgetItem(val)
            if c == 1 and hop.reached:
                it.setForeground(QColor("#3dff6e"))
                f = it.font()
                f.setBold(True)
                it.setFont(f)
            elif c == 1 and not hop.ip:
                it.setForeground(QColor("#8a9bb0"))
            elif c >= 2 and c <= 4 and rtts[c - 2] is not None:
                kind = theme.rtt_kind(rtts[c - 2], self.ctrl.cfg.ping.thresholds)
                it.setForeground(theme.status_color(self.ctrl.cfg.appearance.colors, kind))
            self.table.setItem(r, c, it)
        self.table.scrollToBottom()

    def _finished(self, ip, err: str) -> None:
        # Signál může GUI převzít ještě před návratem workeru z emit().
        if self.busy():
            QTimer.singleShot(10, self, lambda: self._finished(ip, err))
            return
        self.btn_run.setEnabled(True)
        reached = any(h.reached for h in self.hops)
        if err:
            self.status.setText(err)
        elif ip and reached:
            self.status.setText(f"✔ Cíl {ip} dosažen za {len(self.hops)} skoků.")
        else:
            self.status.setText("Hotovo.")
        if self._closing_result is not None:
            self.done(self._closing_result)

    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication
        lines = [f"Traceroute {self.target.host}"]
        for h in self.hops:
            lines.append(f"{h.n:>2}  {h.ip or '*':<40} " + "  ".join(fmt_rtt(x) if x is not None else "*"
                                                                     for x in h.rtts))
        QApplication.clipboard().setText("\n".join(lines))
        self.status.setText("Zkopírováno do schránky.")

    def _request_close(self, result: int) -> bool:
        """Nechat Qt/bridge žít až do ukončení procesu i workeru, bez blokování GUI."""
        self._cancel = True
        if self.busy():
            self._closing_result = result
            self.btn_run.setEnabled(False)
            self.status.setText("Ukončuji trasování…")
            return False
        return True

    def done(self, result: int) -> None:
        # Sem vede také Escape/reject(), které nemusí vyvolat closeEvent.
        if self._request_close(result):
            super().done(result)

    def closeEvent(self, e):
        if not self._request_close(QDialog.DialogCode.Rejected):
            e.ignore()
            return
        super().closeEvent(e)


# ---------------------------------------------------------------------------- Test rychlosti

class SpeedTestDialog(QDialog):
    def __init__(self, ctrl, parent=None):
        super().__init__(parent)
        self.ctrl = ctrl
        self.setWindowTitle("NetMonitor – Test rychlosti internetu")
        self.setStyleSheet(theme.DIALOG_QSS)
        self.resize(520, 0)
        v = QVBoxLayout(self)
        grid = QHBoxLayout()
        self.vals = {}
        for key, title in (("latency", "Odezva"), ("jitter", "Jitter"), ("download", "Stahování"),
                           ("upload", "Nahrávání")):
            box = QVBoxLayout()
            t = QLabel(title)
            t.setProperty("hint", True)
            t.setAlignment(Qt.AlignmentFlag.AlignCenter)
            val = QLabel("–")
            val.setAlignment(Qt.AlignmentFlag.AlignCenter)
            f = theme.font("body", 26, QFont.Weight.DemiBold)
            val.setFont(f)
            val.setStyleSheet("color:#38d9ff")
            box.addWidget(t)
            box.addWidget(val)
            grid.addLayout(box)
            self.vals[key] = val
        v.addLayout(grid)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        v.addWidget(self.bar)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setProperty("hint", True)
        v.addWidget(self.status)
        note = QLabel("Měří se proti serverům Cloudflare (speed.cloudflare.com) – celá cesta tento počítač → "
                      "internet. Test přenese řádově stovky MB dat.")
        note.setWordWrap(True)
        note.setProperty("hint", True)
        v.addWidget(note)
        row = QHBoxLayout()
        self.btn = QPushButton("Spustit test")
        self.btn.setProperty("accent", True)
        self.btn.clicked.connect(self.start)
        close = QPushButton("Zavřít")
        close.clicked.connect(self.close)
        row.addStretch(1)
        row.addWidget(self.btn)
        row.addWidget(close)
        v.addLayout(row)
        self.bridge = _Bridge()
        self.bridge.progress.connect(self._progress)
        self.bridge.result.connect(self._result)
        self._cancel = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.ctrl.cfg.network.external_api:
            self.status.setText("Externí dotazy jsou v nastavení vypnuté (Nastavení → Síť).")
            return
        if self._thread and self._thread.is_alive():
            return
        self._cancel = False
        for val in self.vals.values():
            val.setText("–")
        self.btn.setEnabled(False)
        self.bar.setValue(0)
        self.status.setText("Měřím odezvu…")

        def job():
            r = speedtest.run(progress=lambda ph, f, val: self.bridge.progress.emit(ph, f, val),
                              cancelled=lambda: self._cancel)
            self.bridge.result.emit(r)
        self._thread = threading.Thread(target=job, name="speedtest", daemon=True)
        self._thread.start()

    def _progress(self, phase: str, frac: float, value) -> None:
        base = {"latency": 0, "download": 100, "upload": 550}[phase]
        span = {"latency": 100, "download": 450, "upload": 450}[phase]
        self.bar.setValue(int(base + span * frac))
        label = {"latency": "Měřím odezvu…", "download": "Měřím stahování…", "upload": "Měřím nahrávání…"}[phase]
        self.status.setText(label)
        if value is not None and phase in ("download", "upload"):
            self.vals[phase].setText(_fmt_speed(value))

    def _result(self, r: speedtest.SpeedResult) -> None:
        self.btn.setEnabled(True)
        self.btn.setText("Spustit znovu")
        self.bar.setValue(1000 if not r.error else self.bar.value())
        self.vals["latency"].setText(fmt_rtt(r.latency_ms))
        self.vals["jitter"].setText(fmt_rtt(r.jitter_ms))
        self.vals["download"].setText(_fmt_speed(r.download_mbps))
        self.vals["upload"].setText(_fmt_speed(r.upload_mbps))
        if r.error:
            self.status.setText(f"<span style='color:#ff6b78'>✖ {r.error}</span>")
            return
        self.status.setText(f"✔ Hotovo{' – server ' + r.colo if r.colo else ''} · "
                            f"{time.strftime('%d.%m.%Y %H:%M')}")
        detail = (f"↓ {_fmt_speed(r.download_mbps)}, ↑ {_fmt_speed(r.upload_mbps)}, "
                  f"odezva {fmt_rtt(r.latency_ms)}, jitter {fmt_rtt(r.jitter_ms)}")
        self.ctrl._db(lambda: self.ctrl.history.add_event("speedtest", None, "ok", detail))

    def closeEvent(self, e):
        self._cancel = True
        super().closeEvent(e)


def _fmt_speed(v) -> str:
    if v is None:
        return "–"
    return f"{v / 1000:.2f} Gb/s" if v >= 1000 else f"{v:.0f} Mb/s" if v >= 10 else f"{v:.1f} Mb/s"


# ---------------------------------------------------------------------------- Wake-on-LAN

# Otevřená okna traceroute. Okno musí mít trvalý odkaz: bez něj by ho Python zrušil ve chvíli, kdy doběhne
# pracovní vlákno (poslední odkaz drží jeho closure) – tedy mimo GUI vlákno, což shodí celou aplikaci.
_trace_dialogs: list[TracerouteDialog] = []


def open_traceroute(ctrl, target: PingTarget) -> TracerouteDialog:
    d = TracerouteDialog(ctrl, target)
    _trace_dialogs.append(d)

    def release(_result):
        _trace_dialogs.remove(d)
        d.deleteLater()                        # GUI vlákno, proces i worker už skončily

    d.finished.connect(release)
    d.show()
    d.start()
    return d


def wake(ctrl, target: PingTarget, parent=None) -> None:
    """Probudí zařízení. Bez MAC zkusí najít adresu v ARP tabulce a nabídne její uložení."""
    mac = target.mac
    if not mac:
        ip, _ = Resolver().resolve(target.host)
        found = None
        if ip:
            ping_host(ip, 0.6, ctrl.pinger, Resolver())     # osvěží ARP záznam, pokud zařízení běží
            found = wol.arp_lookup(ip)
        if not found:
            QMessageBox.information(parent, "Wake-on-LAN",
                                    f"Cíl „{target.name}“ nemá zadanou MAC adresu a v ARP tabulce ji nelze najít "
                                    "(zařízení je vypnuté nebo v jiné síti).\n\nZadejte ji v Nastavení → Ping cíle → "
                                    "Upravit. Tip: když zařízení běží, tlačítko „Zjistit“ ji doplní samo.")
            return
        if QMessageBox.question(parent, "Wake-on-LAN",
                                f"Nalezena MAC adresa {found} pro {target.host}.\nUložit ji k cíli a odeslat "
                                "probouzecí paket?") != QMessageBox.StandardButton.Yes:
            return
        cfg = ctrl.cfg.copy()
        t = cfg.target_by_id(target.id)
        if t:
            t.mac = found
            ctrl.update_config(cfg)
        mac = found
    a = ctrl.network.active if ctrl.network else None
    local_ip = a.ipv4[0] if a and a.ipv4 else None
    mask = a.ipv4_masks[0] if a and a.ipv4_masks else None
    try:
        sent = wol.send(mac, local_ip, mask, target.wol_broadcast or None)
    except (OSError, ValueError) as e:
        QMessageBox.warning(parent, "Wake-on-LAN", f"Odeslání se nezdařilo:\n{e}")
        return
    log.info("WoL %s (%s) -> %s", target.name, mac, ", ".join(sent))
    ctrl._db(lambda: ctrl.history.add_event("wol", target.id, None, f"MAC {wol.format_mac(mac)}"))
    for delay in (15_000, 30_000, 60_000):              # ověř, jestli se zařízení probudilo
        QTimer.singleShot(delay, lambda: ctrl.test_now(target.id))
    QMessageBox.information(parent, "Wake-on-LAN",
                            f"Probouzecí paket odeslán na {wol.format_mac(mac)}.\n"
                            f"Stav „{target.name}“ ve widgetu se ověří za 15, 30 a 60 s.")
