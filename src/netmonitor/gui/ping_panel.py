"""Dolní část widgetu: seznam pingovaných cílů. Každý řádek se kreslí sám
a překresluje se jen při změně vlastní hodnoty."""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter
from PySide6.QtWidgets import QFrame, QScrollArea, QToolTip, QVBoxLayout, QWidget

from ..core.config import AppConfig, PingTarget
from ..core.monitoring import TargetSnapshot
from ..core.notifier import fmt_duration
from ..core.ping_engine import PingStatus
from . import theme
from .i18n import tr
from .icons import auto_icon, draw_icon
from .widgets import GlowTitle

ERR_TEXT = {
    PingStatus.TIMEOUT: "TIMEOUT",
    PingStatus.HOST_UNREACHABLE: "UNREACH",
    PingStatus.NET_UNREACHABLE: "NET UNREACH",
    PingStatus.DNS_ERROR: "DNS ERR",
    PingStatus.ERROR: "ERROR",
}
ERR_DESC = {
    PingStatus.TIMEOUT: "vypršel časový limit",
    PingStatus.HOST_UNREACHABLE: "host nedostupný",
    PingStatus.NET_UNREACHABLE: "síť nedostupná",
    PingStatus.DNS_ERROR: "doménu nelze přeložit",
    PingStatus.ERROR: "chyba",
}


def fmt_rtt(v: float | None) -> str:
    if v is None:
        return "–"
    if v < 10:
        return f"{v:.1f} ms" if v >= 0.1 else f"{v:.2f} ms"
    return f"{v:.0f} ms"


class PingRow(QWidget):
    clicked = Signal(str)

    def __init__(self, target: PingTarget, index: int, parent=None):
        super().__init__(parent)
        self.target = target
        self.index = index
        self.snap: TargetSnapshot | None = None
        self._hover = False
        self._value = ("…", "unknown")
        self._cfg: AppConfig | None = None
        self._font = QFont()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def apply_style(self, cfg: AppConfig, scale: float) -> None:
        self._cfg = cfg
        px = 20 if not cfg.appearance.compact else 16
        self._font = theme.font("body", px, QFont.Weight.Medium, scale)
        self._recompute()
        self.update()

    def height_range(self) -> tuple[int, int]:
        fh = QFontMetricsF(self._font).height()
        compact = self._cfg.appearance.compact if self._cfg else False
        return int(fh * 1.22), int(fh * (1.3 if compact else 1.62))

    def set_snapshot(self, snap: TargetSnapshot | None) -> None:
        self.snap = snap
        old = self._value
        self._recompute()
        if self._value != old:
            self.update()

    def _recompute(self) -> None:
        cfg, s, t = self._cfg, self.snap, self.target
        if cfg is None:
            return
        if not t.enabled:
            self._value = ("vypnuto", "unknown")
            return
        if s is None or s.last is None:
            self._value = ("…", "unknown")
            return
        r = s.last
        if r.ok:
            kind = theme.rtt_kind(r.rtt_ms, cfg.ping.thresholds)
            self._value = (fmt_rtt(r.rtt_ms) if t.show_rtt else "OK", kind)
        else:
            # selhání, ale ještě nevyhlášen výpadek (čeká se na N opakování) => oranžově
            self._value = (ERR_TEXT.get(r.status, "ERROR"), "bad" if s.up is False or s.up is None else "warn")

    # ------------------------------------------------------------------
    def paintEvent(self, _):
        if self._cfg is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        bg = theme.ROW_BG_HOVER if self._hover else (theme.ROW_BG if self.index % 2 == 0 else None)
        if bg is not None:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(r, 6, 6)
        h = r.height()
        icon_sz = h * 0.62
        x = r.left() + h * 0.28
        dim = not self.target.enabled
        fg = QColor(theme.WHITE) if not dim else QColor(theme.MUTED)
        draw_icon(p, auto_icon(self.target), QRectF(x, r.center().y() - icon_sz / 2, icon_sz, icon_sz), fg,
                  max(1.2, h * 0.05))
        p.setFont(self._font)
        fm = QFontMetricsF(self._font)
        name_x = x + icon_sz + h * 0.42
        host_x = r.left() + r.width() * 0.455
        val_w = fm.horizontalAdvance("NET UNREACH") + 6
        p.setPen(fg)
        p.drawText(QRectF(name_x, r.top(), host_x - name_x - 8, h), Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(self.target.name, Qt.TextElideMode.ElideRight, host_x - name_x - 8))
        host_w = r.right() - val_w - host_x - 12
        p.drawText(QRectF(host_x, r.top(), host_w, h), Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(self.target.host, Qt.TextElideMode.ElideMiddle, host_w))
        text, kind = self._value
        col = theme.status_color(self._cfg.appearance.colors, kind)
        p.setPen(col)
        p.drawText(QRectF(r.right() - val_w - h * 0.35, r.top(), val_w, h),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, text)

    def sizeHint(self):
        return QSize(400, self.height())

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def event(self, e):
        if e.type() == QEvent.Type.ToolTip:
            QToolTip.showText(e.globalPos(), self.tooltip_text(), self)
            return True
        return super().event(e)

    def tooltip_text(self) -> str:
        t, s = self.target, self.snap
        lines = [f"<b>{t.name}</b> – {t.host}"]
        if s and s.last and s.last.address and s.last.address != t.host:
            lines.append(f"IP: {s.last.address}")
        lines.append(f"Interval {t.interval_s:g} s, timeout {t.timeout_s:g} s, výpadek po {t.retries}× selhání")
        if not s or not s.last:
            lines.append(tr("Zatím neměřeno"))
            return "<br>".join(lines)
        r = s.last
        lines.append(f"Aktuálně: {fmt_rtt(r.rtt_ms) if r.ok else ERR_DESC.get(r.status, r.status.value)}"
                     + (f" ({r.error})" if r.error and not r.ok else ""))
        lines.append(f"Min / Prům / Max: {fmt_rtt(s.rtt_min)} / {fmt_rtt(s.rtt_avg)} / {fmt_rtt(s.rtt_max)}")
        if s.loss_pct is not None:
            lines.append(f"Ztrátovost: {s.loss_pct:.1f} % (posl. {min(100, s.ok_count + s.fail_count)}), "
                         f"celkem {s.loss_pct_total:.1f} %")
        lines.append(f"Úspěšných: {s.ok_count}, neúspěšných: {s.fail_count}")
        if s.last_success_ts:
            lines.append("Poslední úspěch: " + time.strftime("%H:%M:%S %d.%m.", time.localtime(s.last_success_ts)))
        if s.outage_start_ts:
            lines.append(f"<span style='color:#ff6b78'>Výpadek od {time.strftime('%H:%M:%S', time.localtime(s.outage_start_ts))}"
                         f" ({fmt_duration(s.outage_duration_s)})</span>")
        elif s.last_outage_duration_s:
            lines.append(f"Poslední výpadek trval {fmt_duration(s.last_outage_duration_s)}")
        lines.append(f"Výpadků celkem: {s.outage_count}")
        lines.append("<i>Klikněte pro graf historie</i>")
        return "<br>".join(lines)


class SectionRow(QWidget):
    """Nadpis skupiny cílů (např. DNS, DOMÉNY) – jen popisek s neonovou linkou."""

    def __init__(self, target: PingTarget, parent=None):
        super().__init__(parent)
        self.target = target
        self._font = QFont()
        self._neon = True
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def apply_style(self, cfg: AppConfig, scale: float) -> None:
        self._neon = cfg.appearance.neon_effects
        self._font = theme.font("body", 15 if not cfg.appearance.compact else 13, QFont.Weight.DemiBold, scale)
        self._font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 112)
        fh = QFontMetricsF(self._font).height()
        self.setFixedHeight(int(fh * 1.55))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(self._font)
        fm = QFontMetricsF(self._font)
        text = self.target.name.upper()
        r = QRectF(self.rect())
        x = r.left() + 6
        baseline_y = r.bottom() - fm.descent() - r.height() * 0.12
        p.setPen(theme.CYAN_TEXT)
        tw = fm.horizontalAdvance(text)
        avail = r.width() - x - 12
        shown = fm.elidedText(text, Qt.TextElideMode.ElideRight, avail)
        p.drawText(QPointF(x, baseline_y), shown)
        line_y = baseline_y - fm.capHeight() / 2
        x2 = x + min(tw, avail) + 12
        if x2 < r.right() - 8:
            theme.paint_neon_line(p, x2, r.right() - 4, line_y, self._neon, fade="right")


class PingPanel(QWidget):
    target_clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.header = GlowTitle("PING", px=30, underline="sides")
        lay.addWidget(self.header)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea{background:transparent;border:none}"
                                  "QScrollBar:vertical{background:transparent;width:6px}"
                                  "QScrollBar::handle:vertical{background:rgba(34,211,255,90);border-radius:3px}"
                                  "QScrollBar::add-line,QScrollBar::sub-line{height:0}")
        self.scroll.viewport().setAutoFillBackground(False)
        self.container = QWidget()
        self.container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.container.setStyleSheet("background:transparent")
        self.rows_layout = QVBoxLayout(self.container)
        self.rows_layout.setContentsMargins(0, 0, 4, 0)
        self.rows_layout.setSpacing(2)
        self.rows_layout.addStretch(1)
        self.scroll.setWidget(self.container)
        lay.addWidget(self.scroll, 1)
        self.rows: dict[str, PingRow] = {}
        self.sections: dict[str, SectionRow] = {}
        self._order: list[str] = []
        self._cfg: AppConfig | None = None
        self._scale = 1.0

    def apply_style(self, cfg: AppConfig, scale: float) -> None:
        self._cfg, self._scale = cfg, scale
        self.header.set_style(scale, cfg.appearance.neon_effects)
        for r in list(self.rows.values()) + list(self.sections.values()):
            r.apply_style(cfg, scale)
        self._fit_rows()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # rozměry potomků (viewport) se aplikují až po tomto eventu => přepočet v další iteraci smyčky
        QTimer.singleShot(0, self._fit_rows)

    def _fit_rows(self) -> None:
        """Výšku řádků přizpůsobí tak, aby se všechny cíle vešly (v mezích); jinak se posouvá."""
        if not self.rows:
            return
        rows = list(self.rows.values())
        lo, hi = rows[0].height_range()
        n_all = len(rows) + len(self.sections)
        sec_h = sum(sr.height() for sr in self.sections.values())
        avail = self.scroll.viewport().height() - self.rows_layout.spacing() * (n_all - 1) - sec_h - 2
        h = max(lo, min(hi, int(avail / len(rows))))
        for r in rows:
            if r.height() != h:
                r.setFixedHeight(h)

    def set_targets(self, cfg: AppConfig, snapshots: dict[str, TargetSnapshot]) -> None:
        """Přestaví řádky jen pokud se změnil seznam/pořadí; jinak jen aktualizuje cíle a nadpisy."""
        self._cfg = cfg
        items = list(cfg.ping.targets)
        ids = [t.id for t in items]
        kinds_changed = any((t.id in self.sections) != t.is_section for t in items
                            if t.id in self.sections or t.id in self.rows)
        if ids != self._order or kinds_changed:
            for w in list(self.rows.values()) + list(self.sections.values()):
                w.setParent(None)
                w.deleteLater()
            self.rows.clear()
            self.sections.clear()
            for pos, t in enumerate(items):
                if t.is_section:
                    w = SectionRow(t)
                    self.sections[t.id] = w
                else:
                    w = PingRow(t, 0)
                    w.clicked.connect(self.target_clicked)
                    self.rows[t.id] = w
                w.apply_style(cfg, self._scale)
                self.rows_layout.insertWidget(pos, w)
            self._order = ids
            self._fit_rows()
        stripe = 0
        for t in items:
            if t.is_section:
                sw = self.sections[t.id]
                if sw.target.name != t.name:
                    sw.target = t
                    sw.update()
                else:
                    sw.target = t
                stripe = 0          # pruhování začíná v každé sekci znovu
                continue
            row = self.rows[t.id]
            changed = (row.target.host != t.host or row.target.name != t.name or row.target.enabled != t.enabled
                       or row.target.icon != t.icon or row.target.show_rtt != t.show_rtt or row.index != stripe)
            row.target, row.index = t, stripe
            stripe += 1
            snap = snapshots.get(t.id)
            if snap is not None and (not t.enabled or snap.host != t.host):
                snap = None
            row.set_snapshot(snap)
            if changed:
                row.update()

    def update_snapshot(self, snap: TargetSnapshot) -> None:
        row = self.rows.get(snap.target_id)
        if row and row.target.enabled and row.target.host == snap.host:
            row.set_snapshot(snap)

    def row_at(self, global_pos) -> PingRow | None:
        w = self.container.childAt(self.container.mapFromGlobal(global_pos))
        while w is not None and not isinstance(w, PingRow):
            w = w.parentWidget()
        return w
