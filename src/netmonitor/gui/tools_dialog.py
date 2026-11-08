"""Síťové nástroje: Traceroute, Test rychlosti internetu, Wake-on-LAN."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
                               QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..core import speedtest, traceroute, wol
from ..core.config import PingTarget, WolDevice
from ..core.notifier import Notification
from ..core.ping_engine import PingResult, ping_host
from . import theme
from .ping_panel import ERR_DESC, fmt_rtt

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
        self.hops: list[traceroute.Hop] = []

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
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
        self.btn_run.setEnabled(True)
        reached = any(h.reached for h in self.hops)
        if ip and reached:
            self.status.setText(f"✔ Cíl {ip} dosažen za {len(self.hops)} skoků.")
        else:
            self.status.setText(err or "Hotovo.")

    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication
        lines = [f"Traceroute {self.target.host}"]
        for h in self.hops:
            lines.append(f"{h.n:>2}  {h.ip or '*':<40} " + "  ".join(fmt_rtt(x) if x is not None else "*"
                                                                     for x in h.rtts))
        QApplication.clipboard().setText("\n".join(lines))
        self.status.setText("Zkopírováno do schránky.")

    def closeEvent(self, e):
        self._cancel = True
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
    for old in [d for d in _trace_dialogs if not d.isVisible() and not d.busy()]:
        _trace_dialogs.remove(old)
        old.deleteLater()                      # rušení v GUI vlákně a až po doběhnutí vlákna
    d = TracerouteDialog(ctrl, target)
    _trace_dialogs.append(d)
    d.show()
    d.start()
    return d


WOL_CHECK_DELAYS_MS = (15_000, 30_000, 60_000)
_wol_dialogs: list[WolDialog] = []


class WolDialog(QDialog):
    """Nemodální stav WoL; zavření okna nezruší naplánovaná ověření."""

    def __init__(self, ctrl, target: PingTarget | WolDevice):
        # Bez rodiče: ověření přežije i zavření nastavení, ze kterého uživatel budí.
        super().__init__()
        self.ctrl = ctrl
        self.target = replace(target)
        self.pending = 0
        self.detecting = False
        self._best_result: PingResult | None = None
        self.setWindowTitle(f"Wake-on-LAN – {target.name}")
        self.setStyleSheet(theme.DIALOG_QSS)
        self.resize(480, 0)
        layout = QVBoxLayout(self)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        close = QPushButton("Zavřít")
        close.clicked.connect(self.close)
        layout.addWidget(close)

    def busy(self) -> bool:
        return self.detecting or self.pending > 0

    def _status(self, text: str) -> None:
        log.info("WoL %s: %s", self.target.name, text)
        self.status.setText(text)

    def start(self) -> None:
        self._best_result = None
        self.setWindowTitle(f"Wake-on-LAN – {self.target.name}")
        if self.target.mac or isinstance(self.target, WolDevice):
            self._send(self.target.mac)
            return
        self.detecting = True
        self._status("Zjišťuji MAC adresu…")
        self.ctrl.detect_mac(f"mac-wol-{id(self)}", self.target.host, self._mac_found)

    def _mac_found(self, res) -> None:
        self.detecting = False
        mac, err = res
        if not mac:
            self._status(err)
            QMessageBox.warning(self, "Wake-on-LAN", err + "\nZadejte MAC v Nastavení → Ping cíle → Upravit.")
            return
        # Uživatel mohl během úlohy cíl přepsat nebo smazat.
        current = self.ctrl.cfg.target_by_id(self.target.id)
        if not current or current.host != self.target.host:
            self._status("Cíl se během zjišťování změnil; spusťte probuzení znovu.")
            return
        if not current.mac and QMessageBox.question(self, "Wake-on-LAN",
                                f"Nalezena MAC adresa {mac} pro {self.target.host}.\nUložit ji k cíli a odeslat "
                                "probouzecí paket?") != QMessageBox.StandardButton.Yes:
            self._status("Probuzení zrušeno.")
            return
        # Modální dotaz má vlastní event loop; konfigurace se mohla změnit i během něj.
        current = self.ctrl.cfg.target_by_id(self.target.id)
        if not current or current.host != self.target.host:
            self._status("Cíl se během zjišťování změnil; spusťte probuzení znovu.")
            return
        self.target = replace(current)
        if current.mac:
            mac = current.mac                 # ručně zadaná MAC má přednost před ARP výsledkem
        else:
            cfg = self.ctrl.cfg.copy()
            t = cfg.target_by_id(self.target.id)
            if t:
                t.mac = mac
            self.ctrl.update_config(cfg)
        self._send(mac)

    def _send(self, mac: str) -> None:
        a = self.ctrl.network.active if self.ctrl.network else None
        local_ip = a.ipv4[0] if a and a.ipv4 else None
        mask = a.ipv4_masks[0] if a and a.ipv4_masks else None
        if isinstance(self.target, WolDevice):
            device = self.target
            self.detecting = True
            self._status("Odesílám probouzecí paket…")

            def job():
                try:
                    return (wol.send(mac, local_ip, mask, device.broadcast or None, port=device.port),)
                except Exception as e:  # doručit i chybu, aby okno nezůstalo ve stavu busy
                    return (e,)

            self.ctrl._run_bg(f"wol-send-{id(self)}", job, lambda result: self._sent(mac, result[0]))
            return
        try:
            sent = wol.send(mac, local_ip, mask, self.target.wol_broadcast or None)
        except (OSError, ValueError) as e:
            self._sent(mac, e)
            return
        self._sent(mac, sent)

    def _sent(self, mac: str, sent) -> None:
        self.detecting = False
        if isinstance(sent, Exception):
            self._status(f"Odeslání se nezdařilo: {sent}")
            QMessageBox.warning(self, "Wake-on-LAN", self.status.text())
            return
        log.info("WoL %s (%s) -> %s", self.target.name, mac, ", ".join(sent))
        target_id = self.target.id if isinstance(self.target, PingTarget) else None
        detail = f"MAC {wol.format_mac(mac)}"
        if isinstance(self.target, WolDevice):
            detail = f"{self.target.name} – {detail}"
        self.ctrl._db(lambda: self.ctrl.history.add_event("wol", target_id, None, detail))
        if not self.target.host:
            self._status(f"Probouzecí paket odeslán na {wol.format_mac(mac)}. Ověření není nastaveno.")
            return
        self._status(f"Probouzecí paket odeslán na {wol.format_mac(mac)}. "
                     f"Dostupnost „{self.target.name}“ se ověří za 15, 30 a 60 s.")
        self.pending = len(WOL_CHECK_DELAYS_MS)
        for attempt, delay in enumerate(WOL_CHECK_DELAYS_MS, 1):
            QTimer.singleShot(delay, lambda n=attempt: self._check(n))

    def _check(self, attempt: int) -> None:
        # Jednorázový ping nezávisí na enabled, pauze ani přítomnosti cíle v plánovači.
        self.ctrl._run_bg(f"wol-check-{id(self)}-{attempt}",
                          lambda: ping_host(self.target.host, self.target.timeout_s if isinstance(self.target, PingTarget) else 1.0,
                                            self.ctrl.pinger, self.ctrl.resolver),
                          lambda result: self._checked(attempt, result))

    def _checked(self, attempt: int, result: PingResult) -> None:
        self.pending -= 1
        first_success = result.ok and (self._best_result is None or not self._best_result.ok)
        # Úspěch dokazuje probuzení i při pozdější ztrátě paketu. Do té doby zobrazuj poslední chybu.
        if self._best_result is None or not self._best_result.ok:
            self._best_result = result
        best = self._best_result
        detail = (f"dostupný ({fmt_rtt(best.rtt_ms)})" if best.ok else
                  f"neodpovídá ({best.error or ERR_DESC.get(best.status, 'chyba')})")
        earlier = " Cíl odpověděl při dřívějším ověření." if best.ok and not result.ok else ""
        text = f"Ověření {attempt}/3: {self.target.name} – {detail}.{earlier}"
        self._status(text)
        # Nejvýše jedna notifikace: první úspěch, jinak až dokončení všech tří kontrol.
        if not self.isVisible() and (first_success or (self.pending == 0 and not best.ok)):
            self.ctrl.notification.emit(Notification("wol", f"wol:{self.target.id}",
                                                     f"Wake-on-LAN – {self.target.name}", text))
        current = self.ctrl.cfg.target_by_id(self.target.id) if isinstance(self.target, PingTarget) else None
        if current and current.enabled and current.host == self.target.host:
            self.ctrl.test_now(current.id)


def wake(ctrl, target: PingTarget | WolDevice, parent=None) -> None:
    """Otevře stav probuzení; zjištění MAC a ověřovací pingy běží na pozadí."""
    for old in list(_wol_dialogs):
        if old.ctrl is ctrl and type(old.target) is type(target) and old.target.id == target.id and (old.busy() or old.isVisible()):
            old.show()
            old.raise_()
            if not old.busy():
                old.target = replace(target)
                old.start()
            return
        if not old.isVisible() and not old.busy():
            _wol_dialogs.remove(old)
            old.deleteLater()
    d = WolDialog(ctrl, target)
    _wol_dialogs.append(d)                     # držet Qt objekt až do doručení posledního výsledku
    d.show()
    d.start()
