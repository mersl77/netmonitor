"""Detail cíle: graf odezvy (1 h / 24 h / 7 d / 30 d), statistiky, výpadky, export CSV."""
from __future__ import annotations

import math
import time
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
                               QHeaderView, QAbstractItemView, QToolTip)

from ..core.history import SeriesPoint
from ..core.notifier import fmt_duration
from . import theme
from .controller import Controller
from .ping_panel import fmt_rtt
from .widgets import show_dialog

RANGES = [("1 hodina", 3600, 0), ("24 hodin", 86400, 300), ("7 dní", 7 * 86400, 3600), ("30 dní", 30 * 86400, 4 * 3600)]
EVENT_LABELS = {"down": "Výpadek", "up": "Obnovení", "wan_change": "Změna WAN IP", "internet_down": "Výpadek internetu",
                "internet_up": "Internet obnoven", "high_latency": "Vysoká latence",
                "ip_config": "Změna nastavení IP", "speedtest": "Test rychlosti", "wol": "Wake-on-LAN"}


class Chart(QWidget):
    """Lehký graf: pásmo min–max, čára průměru, červené sloupce ztrátovosti."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points: list[SeriesPoint] = []
        self.t0 = self.t1 = 0.0
        self.thresholds = (50.0, 150.0)
        self.bucket = 60.0
        self.setMinimumHeight(260)
        self.setMouseTracking(True)

    def set_data(self, pts: list[SeriesPoint], t0: float, t1: float, bucket: float, thresholds) -> None:
        self.points, self.t0, self.t1, self.bucket = pts, t0, t1, bucket
        self.thresholds = (thresholds.green_max_ms, thresholds.orange_max_ms)
        self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(58, 14, -16, -34)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#081426"))
        r = self._plot_rect()
        vals = [x.rtt_max for x in self.points if x.rtt_max is not None] + \
               [x.rtt_avg for x in self.points if x.rtt_avg is not None]
        ymax = max(vals) if vals else 10.0
        ymax = _nice(max(ymax * 1.12, 1.0))
        f = theme.font("body", 12, QFont.Weight.Normal)
        p.setFont(f)
        # osa Y + mřížka + prahy
        for i in range(5):
            y = r.bottom() - r.height() * i / 4
            p.setPen(QPen(QColor(34, 211, 255, 30), 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor("#8fb3d1"))
            p.drawText(QRectF(0, y - 9, r.left() - 6, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"{ymax * i / 4:.0f} ms" if ymax >= 4 else f"{ymax * i / 4:.1f} ms")
        for th, col in ((self.thresholds[0], QColor(61, 255, 110, 80)), (self.thresholds[1], QColor(255, 170, 43, 80))):
            if th < ymax:
                y = r.bottom() - r.height() * th / ymax
                p.setPen(QPen(col, 1, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        # osa X
        span = max(1.0, self.t1 - self.t0)
        fmt = "%H:%M" if span <= 2 * 86400 else "%d.%m."
        p.setPen(QColor("#8fb3d1"))
        for i in range(6):
            t = self.t0 + span * i / 5
            x = r.left() + r.width() * i / 5
            p.drawText(QRectF(x - 40, r.bottom() + 6, 80, 18), Qt.AlignmentFlag.AlignCenter,
                       time.strftime(fmt, time.localtime(t)))
        p.setPen(QPen(QColor(34, 211, 255, 120), 1))
        p.drawRect(r)
        if not self.points:
            p.setPen(QColor("#8fb3d1"))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "Zatím žádná data pro zvolené období")
            return

        def X(t):
            return r.left() + r.width() * (t - self.t0) / span

        def Y(v):
            return r.bottom() - r.height() * min(v, ymax) / ymax

        bw = max(1.5, r.width() * self.bucket / span)
        # ztrátovost (červeně, výška = % z výšky grafu)
        for pt in self.points:
            if pt.n_fail:
                h = r.height() * pt.loss_pct / 100
                p.fillRect(QRectF(X(pt.ts), r.bottom() - h, bw, h), QColor(255, 77, 94, 110 if pt.loss_pct < 100 else 170))
        # pásmo min–max a průměr; mezery tam, kde nejsou data
        segments: list[list[SeriesPoint]] = []
        cur: list[SeriesPoint] = []
        last_t = None
        for pt in self.points:
            if pt.rtt_avg is None or (last_t is not None and pt.ts - last_t > self.bucket * 2.5):
                if cur:
                    segments.append(cur)
                cur = []
            if pt.rtt_avg is not None:
                cur.append(pt)
                last_t = pt.ts
        if cur:
            segments.append(cur)
        for seg in segments:
            if len(seg) > 1 and self.bucket > 30:
                band = QPainterPath()
                band.moveTo(X(seg[0].ts + self.bucket / 2), Y(seg[0].rtt_max))
                for pt in seg[1:]:
                    band.lineTo(X(pt.ts + self.bucket / 2), Y(pt.rtt_max))
                for pt in reversed(seg):
                    band.lineTo(X(pt.ts + self.bucket / 2), Y(pt.rtt_min))
                band.closeSubpath()
                p.fillPath(band, QColor(34, 211, 255, 45))
            line = QPainterPath()
            line.moveTo(X(seg[0].ts + self.bucket / 2), Y(seg[0].rtt_avg))
            for pt in seg[1:]:
                line.lineTo(X(pt.ts + self.bucket / 2), Y(pt.rtt_avg))
            p.setPen(QPen(QColor(64, 224, 255), 1.8))
            p.drawPath(line)
            if len(seg) == 1:
                p.setBrush(QColor(64, 224, 255))
                p.drawEllipse(QPointF(X(seg[0].ts + self.bucket / 2), Y(seg[0].rtt_avg)), 2.5, 2.5)

    def mouseMoveEvent(self, e):
        r = self._plot_rect()
        if not self.points or not r.contains(e.position()):
            return
        span = max(1.0, self.t1 - self.t0)
        t = self.t0 + (e.position().x() - r.left()) / r.width() * span
        pt = min(self.points, key=lambda x: abs(x.ts + self.bucket / 2 - t))
        txt = (f"{time.strftime('%d.%m. %H:%M:%S' if self.bucket < 60 else '%d.%m. %H:%M', time.localtime(pt.ts))}\n"
               f"průměr {fmt_rtt(pt.rtt_avg)}  (min {fmt_rtt(pt.rtt_min)}, max {fmt_rtt(pt.rtt_max)})\n"
               f"OK {pt.n_ok}, chyb {pt.n_fail}, ztrátovost {pt.loss_pct:.0f} %")
        QToolTip.showText(e.globalPosition().toPoint(), txt, self)


def _nice(v: float) -> float:
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * exp:
            return m * exp
    return 10 * exp


class HistoryDialog(QDialog):
    tool_requested = Signal(str, str)     # ("traceroute"|"wake"|"speedtest", id cíle)

    def __init__(self, ctrl: Controller, parent=None):
        super().__init__(parent)
        self.ctrl = ctrl
        self.setWindowTitle("NetMonitor – Historie a diagnostika")
        self.setStyleSheet(theme.DIALOG_QSS)
        self.resize(900, 700)
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.combo = QComboBox()
        self.combo.currentIndexChanged.connect(lambda _: self.refresh())
        top.addWidget(QLabel("Cíl:"))
        top.addWidget(self.combo, 1)
        self.range_group = QButtonGroup(self)
        for i, (label, _s, _b) in enumerate(RANGES):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(i == 0)
            self.range_group.addButton(b, i)
            top.addWidget(b)
        self.range_group.idClicked.connect(lambda _: self.refresh())
        v.addLayout(top)
        self.chart = Chart()
        v.addWidget(self.chart, 3)

        stats = QGridLayout()
        self.stat_labels: dict[str, QLabel] = {}
        keys = [("cur", "Aktuální"), ("min", "Minimum"), ("avg", "Průměr"), ("max", "Maximum"), ("loss", "Packet loss"),
                ("ok", "Úspěšných"), ("fail", "Neúspěšných"), ("last_ok", "Poslední úspěch"),
                ("outage", "Aktuální výpadek"), ("outages", "Výpadků celkem")]
        for i, (k, label) in enumerate(keys):
            l = QLabel(label + ":")
            l.setProperty("hint", True)
            val = QLabel("–")
            val.setStyleSheet("font-weight:600")
            stats.addWidget(l, i // 5 * 2, i % 5)
            stats.addWidget(val, i // 5 * 2 + 1, i % 5)
            self.stat_labels[k] = val
        v.addLayout(stats)
        self.period_note = QLabel()
        self.period_note.setProperty("hint", True)
        v.addWidget(self.period_note)

        v.addWidget(QLabel("Události (výpadky, obnovení, změny WAN):"))
        self.events = QTableWidget(0, 4)
        self.events.setHorizontalHeaderLabels(["Čas", "Událost", "Detail", "Délka"])
        self.events.verticalHeader().setVisible(False)
        self.events.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.events.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.events.setAlternatingRowColors(True)
        v.addWidget(self.events, 2)
        tools = QHBoxLayout()
        for label, sig in (("Traceroute…", "traceroute"), ("Probudit (Wake-on-LAN)", "wake")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, s=sig: self.tool_requested.emit(s, self.combo.currentData() or ""))
            tools.addWidget(b)
        b = QPushButton("Test rychlosti internetu…")
        b.clicked.connect(lambda: self.tool_requested.emit("speedtest", ""))
        tools.addWidget(b)
        tools.addStretch(1)
        v.addLayout(tools)
        bot = QHBoxLayout()
        for label, fn in (("Export měření (CSV)…", self._export_samples), ("Export událostí (CSV)…", self._export_events)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            bot.addWidget(b)
        bot.addStretch(1)
        close = QPushButton("Zavřít")
        close.clicked.connect(self.close)
        bot.addWidget(close)
        v.addLayout(bot)

        self._timer = QTimer(self, interval=5000, timeout=self.refresh)
        ctrl.targets_changed.connect(self._fill_targets)
        self._fill_targets()

    def _fill_targets(self) -> None:
        cur = self.combo.currentData()
        self.combo.blockSignals(True)
        self.combo.clear()
        for t in self.ctrl.cfg.real_targets():
            self.combo.addItem(f"{t.name}  ({t.host})", t.id)
        if cur:
            i = self.combo.findData(cur)
            if i >= 0:
                self.combo.setCurrentIndex(i)
        self.combo.blockSignals(False)

    def show_target(self, tid: str = "") -> None:
        if tid:
            i = self.combo.findData(tid)
            if i >= 0:
                self.combo.setCurrentIndex(i)
        self.refresh()
        show_dialog(self)
        self._timer.start()

    def closeEvent(self, e):
        self._timer.stop()
        super().closeEvent(e)

    def refresh(self) -> None:
        tid = self.combo.currentData()
        if not tid:
            return
        idx = max(0, self.range_group.checkedId())
        _label, span, bucket = RANGES[idx]
        now = time.time()
        t0 = now - span
        h = self.ctrl.history
        if bucket == 0:   # 1 h: surové vzorky z paměti (+ doplnění z DB po restartu)
            raw = h.raw_series(tid, t0)
            if len(raw) >= 2:
                pts = [SeriesPoint(ts, rtt, rtt, rtt, 1 if rtt is not None else 0, 0 if rtt is not None else 1)
                       for ts, rtt, _st in raw]
                b = max(1.0, (raw[-1][0] - raw[0][0]) / max(1, len(raw) - 1))
            else:
                pts, b = h.series(tid, t0, 60), 60.0
        else:
            pts, b = h.series(tid, t0, bucket), float(bucket)
        self.chart.set_data(pts, t0, now, b, self.ctrl.cfg.ping.thresholds)

        s = self.ctrl.snapshots.get(tid)
        L = self.stat_labels
        summ = h.summary(tid, t0)
        L["cur"].setText(fmt_rtt(s.last.rtt_ms) if s and s.last and s.last.ok else
                         (s.last.status.value.upper() if s and s.last else "–"))
        L["min"].setText(fmt_rtt(summ["rtt_min"]))
        L["avg"].setText(fmt_rtt(summ["rtt_avg"]))
        L["max"].setText(fmt_rtt(summ["rtt_max"]))
        L["loss"].setText(f"{summ['loss_pct']:.2f} %" if summ["loss_pct"] is not None else "–")
        L["ok"].setText(str(summ["n_ok"]))
        L["fail"].setText(str(summ["n_fail"]))
        L["last_ok"].setText(time.strftime("%d.%m. %H:%M:%S", time.localtime(s.last_success_ts))
                             if s and s.last_success_ts else "–")
        if s and s.outage_start_ts:
            L["outage"].setText(f"od {time.strftime('%H:%M:%S', time.localtime(s.outage_start_ts))} "
                                f"({fmt_duration(s.outage_duration_s)})")
            L["outage"].setStyleSheet("color:#ff6b78;font-weight:600")
        else:
            L["outage"].setText("žádný")
            L["outage"].setStyleSheet("color:#3dff6e;font-weight:600")
        evs = h.events(t0, tid)
        L["outages"].setText(f"{sum(1 for e in evs if e.kind == 'down')} (v období)")
        self.period_note.setText(f"Statistiky za zvolené období (agregace z DB, zpoždění zápisu max. 60 s). "
                                 f"Od spuštění: výpadků {s.outage_count if s else 0}, ztrátovost "
                                 f"{s.loss_pct_total:.1f} %" if s and s.loss_pct_total is not None else
                                 "Statistiky za zvolené období.")
        all_evs = sorted(evs + [e for e in h.events(t0) if e.target_id is None], key=lambda e: -e.ts)[:300]
        self.events.setRowCount(len(all_evs))
        for i, e in enumerate(all_evs):
            col = QColor("#ff6b78") if e.kind in ("down", "internet_down") else \
                QColor("#3dff6e") if e.kind in ("up", "internet_up") else QColor("#38d9ff")
            vals = [time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(e.ts)), EVENT_LABELS.get(e.kind, e.kind),
                    e.detail or e.status or "", fmt_duration(e.duration_s) if e.duration_s else ""]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if c == 1:
                    it.setForeground(col)
                self.events.setItem(i, c, it)
        self.events.resizeColumnToContents(0)
        self.events.resizeColumnToContents(1)

    def _names(self) -> dict[str, str]:
        return {t.id: t.name for t in self.ctrl.cfg.real_targets()}

    def _export_samples(self) -> None:
        fn, _ = QFileDialog.getSaveFileName(self, "Export měření",
                                            str(Path.home() / f"netmonitor-history-{time.strftime('%Y%m%d')}.csv"),
                                            "CSV (*.csv)")
        if not fn:
            return
        span = RANGES[max(0, self.range_group.checkedId())][1]
        n = self.ctrl.history.export_csv(fn, time.time() - span, None, self._names())
        QMessageBox.information(self, "Export", f"Uloženo {n} řádků (minutové agregace, všechny cíle) do\n{fn}")

    def _export_events(self) -> None:
        fn, _ = QFileDialog.getSaveFileName(self, "Export událostí",
                                            str(Path.home() / f"netmonitor-events-{time.strftime('%Y%m%d')}.csv"),
                                            "CSV (*.csv)")
        if fn:
            n = self.ctrl.history.export_events_csv(fn, 0, self._names())
            QMessageBox.information(self, "Export", f"Uloženo {n} událostí do\n{fn}")
