"""Hlavní průhledný widget na ploše."""
from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QCursor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QApplication, QMenu, QVBoxLayout, QWidget

from .. import platform as plat
from ..core.config import MODE_DESKTOP, MODE_ONTOP, AppConfig
from ..core.connection_status import ConnEvaluation
from . import theme
from .controller import Controller
from .i18n import tr
from .network_panel import NetworkPanel
from .ping_panel import PingPanel
from .widgets import GlowTitle
from .wol_dialog import WolDevicesDialog, add_wol_menu

log = logging.getLogger(__name__)
BASE_W, BASE_H = 600, 850
MIN_W, MIN_H = 260, 340


class ResizeGrip(QWidget):
    def __init__(self, owner: "MainWidget"):
        super().__init__(owner)
        self.owner = owner
        self.setFixedSize(22, 22)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._start: QPoint | None = None
        self._start_size = QSize()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(34, 211, 255, 150))
        for dx, dy in ((14, 14), (9, 14), (14, 9), (4, 14), (9, 9), (14, 4)):
            p.drawEllipse(QPointF(dx + 2, dy + 2), 1.4, 1.4)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            wh = self.owner.windowHandle()
            if wh is not None and QGuiApplication.platformName() in ("wayland",) and \
                    wh.startSystemResize(Qt.Edge.RightEdge | Qt.Edge.BottomEdge):
                return
            self._start = e.globalPosition().toPoint()
            self._start_size = self.owner.size()

    def mouseMoveEvent(self, e):
        if self._start is not None:
            d = e.globalPosition().toPoint() - self._start
            self.owner.resize(max(MIN_W, self._start_size.width() + d.x()), max(MIN_H, self._start_size.height() + d.y()))

    def mouseReleaseEvent(self, e):
        self._start = None
        self.owner.remember_geometry()


class MainWidget(QWidget):
    open_settings = Signal()
    open_history = Signal(str)
    open_ipconfig = Signal(str)
    open_update = Signal()
    open_traceroute = Signal(str)
    wake_target = Signal(str)
    open_speedtest = Signal()
    open_about = Signal()
    quit_requested = Signal()
    visibility_toggled = Signal(bool)

    def __init__(self, ctrl: Controller):
        super().__init__(None)
        self.ctrl = ctrl
        self._wol_devices_dialog: WolDevicesDialog | None = None
        self.setWindowTitle("NetMonitor")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setMouseTracking(True)          # kurzor "ruka" nad tabem s verzí
        self._scale = 1.0
        self._press_pos: QPoint | None = None
        self._dragging = False
        self._win_offset = QPoint()
        self.platform_notice = ""
        self.update_version: str | None = None

        self.title = GlowTitle(tr("SÍŤOVÉ INFORMACE"), px=32, underline="under")
        self.net = NetworkPanel()
        self.ping = PingPanel()
        lay = QVBoxLayout(self)
        lay.setSpacing(2)
        lay.addWidget(self.title)
        lay.addWidget(self.net)
        lay.addWidget(self.ping, 1)
        self._layout = lay
        self.grip = ResizeGrip(self)
        self.setMinimumSize(MIN_W, MIN_H)

        self._geom_timer = QTimer(self, singleShot=True, interval=700, timeout=self.remember_geometry)
        self._bottom_timer = QTimer(self, singleShot=True, interval=50, timeout=self._push_to_bottom)

        ctrl.target_result.connect(lambda s, r: self.ping.update_snapshot(s))
        ctrl.targets_changed.connect(lambda: self.ping.set_targets(self.ctrl.cfg, self.ctrl.snapshots))
        ctrl.network_updated.connect(lambda s: self.net.update_network(s, self.ctrl.cfg.network.show_ipv6))
        ctrl.wan_updated.connect(self.net.update_wan)
        ctrl.connection_changed.connect(self._on_connection)
        ctrl.config_changed.connect(self.apply_config)

        self.apply_config(ctrl.cfg, initial=True)
        self.ping.set_targets(ctrl.cfg, ctrl.snapshots)
        self._install_filters(self)

    # ------------------------------------------------------------------ vzhled
    def _compute_scale(self) -> float:
        a = self.ctrl.cfg.appearance
        return max(0.4, min(2.5, min(self.width() / BASE_W, self.height() / BASE_H * 1.15))) * a.font_scale

    def apply_config(self, cfg: AppConfig, initial: bool = False) -> None:
        a = cfg.appearance
        if initial:
            self._restore_geometry(cfg)
        self._scale = self._compute_scale()
        s = self._scale
        self._layout.setContentsMargins(int(34 * s), int(38 * s), int(32 * s), int(26 * s))
        self.title.set_style(s, a.neon_effects)
        self.net.apply_style(cfg, s)
        self.ping.apply_style(cfg, s)
        self.ping.set_targets(cfg, self.ctrl.snapshots)
        self.grip.setVisible(not a.locked)
        self.setWindowOpacity(a.opacity)
        self._apply_window_flags(cfg)
        if self.ctrl.network:
            self.net.update_network(self.ctrl.network, cfg.network.show_ipv6)
        self.net.update_connection(self.ctrl.evaluation, a.colors)
        self.update()

    def _on_connection(self, ev: ConnEvaluation) -> None:
        self.net.update_connection(ev, self.ctrl.cfg.appearance.colors)

    def _apply_window_flags(self, cfg: AppConfig) -> None:
        a = cfg.appearance
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.NoDropShadowWindowHint
        if a.mode == MODE_ONTOP:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags |= Qt.WindowType.WindowStaysOnBottomHint
        if a.click_through:
            flags |= Qt.WindowType.WindowTransparentForInput
        if flags != self.windowFlags():
            visible = self.isVisible()
            self.setWindowFlags(flags)
            if visible:
                self.show()
        self.platform_notice = self._platform_notice(cfg)
        self._bottom_timer.start()

    def _platform_notice(self, cfg: AppConfig) -> str:
        qpa = QGuiApplication.platformName()
        if qpa == "wayland":
            app = QApplication.instance()
            fallback = app.property("xcb_fallback") if app is not None else None
            if fallback in ("cursor", "other"):
                reason = ("chybí knihovna libxcb-cursor0 (Debian/Ubuntu: sudo apt install libxcb-cursor0)"
                          if fallback == "cursor" else
                          "chybí knihovny xcb (Debian/Ubuntu: sudo apt install libxcb-cursor0 libxkbcommon-x11-0)")
                return (f"Wayland: XWayland (xcb) nešel použít – {reason}. Po instalaci aplikaci restartujte. "
                        "Widget běží jako běžné okno bez možnosti určit pozici a vrstvu (pod okny / navrchu).")
            return ("Wayland: kompozitor nedovoluje aplikacím určovat pozici okna ani jeho vrstvu "
                    "(pod okny / navrchu). Widget běží jako běžné okno. Pro plnou funkčnost zapněte "
                    "v nastavení 'Na Waylandu použít XWayland' a restartujte aplikaci.")
        if sys.platform == "win32" and cfg.appearance.mode == MODE_DESKTOP:
            return ("Windows: režim 'Na ploše' drží widget pod všemi okny. Klávesová zkratka Win+D "
                    "(Zobrazit plochu) widget dočasně skryje – je to omezení Windows.")
        if sys.platform == "darwin" and cfg.appearance.mode == MODE_DESKTOP:
            return "macOS: režim 'Na ploše' je realizován nejnižší úrovní okna, kterou Qt poskytuje."
        if plat.session_type() == "x11" and cfg.appearance.mode == MODE_DESKTOP:
            return "X11: režim 'Na ploše' používá _NET_WM_STATE_BELOW (závisí na správci oken)."
        return ""

    def _push_to_bottom(self) -> None:
        if sys.platform == "win32" and self.ctrl.cfg.appearance.mode == MODE_DESKTOP and self.isVisible():
            from ..platform import windows
            windows.send_to_bottom(int(self.winId()))

    def changeEvent(self, e):
        if e.type() == QEvent.Type.ActivationChange and self.ctrl.cfg.appearance.mode == MODE_DESKTOP:
            self._bottom_timer.start()
        super().changeEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        self._bottom_timer.start()
        self.visibility_toggled.emit(True)

    def hideEvent(self, e):
        super().hideEvent(e)
        self.visibility_toggled.emit(False)

    def paintEvent(self, _):
        p = QPainter(self)
        a = self.ctrl.cfg.appearance
        theme.paint_glass_panel(p, QRectF(self.rect()), a.glass_alpha, a.neon_effects, self._scale,
                                self._brand_text_w())
        self._paint_brand(p)
        if self.update_version:
            from .icons import draw_update_arrow
            r = self._badge_rect()
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            if a.neon_effects:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(34, 211, 255, 60))
                p.drawEllipse(r.adjusted(-4, -4, 4, 4))
            p.setPen(QPen(QColor(8, 30, 58), 1.5))
            p.setBrush(QColor("#22d3ff"))
            p.drawEllipse(r)
            draw_update_arrow(p, r, QColor(8, 30, 58), max(1.6, 2.2 * self._scale))

    # ------------------------------------------------------------------ výklenek s verzí → zelina.eu/projekty
    PROJECTS_URL = "https://zelina.eu/projekty.html"

    def _brand_text(self) -> str:
        from .. import __version__
        return f"v{__version__}  ·  zelina.eu"

    def _brand_font(self):
        f = theme.font("body", 12.5, theme.QFont.Weight.Medium, self._scale)
        f.setLetterSpacing(theme.QFont.SpacingType.PercentageSpacing, 106)
        return f

    def _brand_text_w(self) -> float:
        from PySide6.QtGui import QFontMetricsF
        return QFontMetricsF(self._brand_font()).horizontalAdvance(self._brand_text())

    def _tab_rect(self) -> QRectF:
        """Celý zvýšený tab vpravo nahoře (klikací plocha)."""
        outer = QRectF(self.rect()).adjusted(6, 6, -6, -6)
        w, h = theme.tab_size(outer, self._scale, self._brand_text_w())
        return QRectF(outer.right() - w, outer.top(), w, h + 2)

    def _brand_rect(self) -> QRectF:
        """Oblast textu verze v tabu (za proužky ///)."""
        s = self._scale
        t = self._tab_rect()
        left = t.left() + 18 * s + 3 * 13 * s + 10 * s
        return QRectF(left, t.top(), max(0.0, t.right() - 14 * s - left), t.height() - 2)

    def _paint_brand(self, p: QPainter) -> None:
        r = self._brand_rect()
        s = self._scale
        hover = getattr(self, "_brand_hover", False)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(self._brand_font())
        p.setPen(QColor("#ffffff") if hover else QColor(150, 236, 255))
        p.drawText(r.adjusted(0, 1 * s, 0, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   self._brand_text())
        if hover:                                   # podtržení při najetí = odkaz
            fm_w = self._brand_text_w()
            y = r.bottom() - 2 * s
            p.setPen(QPen(QColor(64, 220, 255, 200), 1.0 * s))
            p.drawLine(QPointF(r.left(), y), QPointF(r.left() + fm_w, y))

    def _open_projects(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(self.PROJECTS_URL))

    def mouseMoveEvent(self, e):
        inside = self._tab_rect().contains(e.position())
        if inside != getattr(self, "_brand_hover", False):
            self._brand_hover = inside
            self.setCursor(Qt.CursorShape.PointingHandCursor if inside else Qt.CursorShape.ArrowCursor)
            self.update(self._tab_rect().toAlignedRect().adjusted(-3, -3, 3, 3))
        super().mouseMoveEvent(e)

    def event(self, e):
        if e.type() == QEvent.Type.ToolTip and self._tab_rect().contains(e.position() if hasattr(e, "position")
                                                                          else QPointF(e.pos())):
            from PySide6.QtWidgets import QToolTip
            QToolTip.showText(e.globalPos(), "Další projekty – zelina.eu/projekty", self)
            return True
        return super().event(e)

    def _badge_rect(self) -> QRectF:
        s = self._scale
        d = 22 * s
        return QRectF(self.width() - 6 - 20 * s - d, 6 + 26 * s, d, d)

    def set_update(self, info) -> None:
        self.update_version = info.version if info else None
        self.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.grip.move(self.width() - self.grip.width() - int(14 * self._scale),
                       self.height() - self.grip.height() - int(12 * self._scale))
        new_scale = self._compute_scale()
        if abs(new_scale - self._scale) > 0.01:
            self._scale = new_scale
            s = new_scale
            cfg = self.ctrl.cfg
            self._layout.setContentsMargins(int(34 * s), int(38 * s), int(32 * s), int(26 * s))
            self.title.set_style(s, cfg.appearance.neon_effects)
            self.net.apply_style(cfg, s)
            self.ping.apply_style(cfg, s)
        self._geom_timer.start()

    def moveEvent(self, e):
        super().moveEvent(e)
        self._geom_timer.start()

    # ------------------------------------------------------------------ geometrie
    def _restore_geometry(self, cfg: AppConfig) -> None:
        a = cfg.appearance
        screen = None
        for sc in QGuiApplication.screens():
            if a.screen and sc.name() == a.screen:
                screen = sc
        screen = screen or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        w = min(a.width, avail.width())
        h = min(a.height, avail.height())
        self.resize(w, h)
        if a.x is None or a.y is None:
            x = avail.right() - w - 40
            y = avail.top() + 40
        else:
            x, y = a.x, a.y
            if not any(sc.availableGeometry().intersects(QRect(x, y, w, h)) for sc in QGuiApplication.screens()):
                x, y = avail.right() - w - 40, avail.top() + 40      # monitor odpojen => zpět na hlavní
        self.move(x, y)

    def remember_geometry(self) -> None:
        a = self.ctrl.cfg.appearance
        g = self.geometry()
        sc = self.screen()
        new = (g.width(), g.height(), g.x(), g.y(), sc.name() if sc else "")
        if new != (a.width, a.height, a.x, a.y, a.screen):
            a.width, a.height, a.x, a.y, a.screen = new
            self.ctrl.schedule_save()

    def set_size(self, w: int, h: int) -> None:
        self.resize(w, h)
        self.remember_geometry()

    # ------------------------------------------------------------------ myš: přesun / klik / menu
    def _install_filters(self, w: QWidget) -> None:
        w.installEventFilter(self)
        for ch in w.findChildren(QWidget):
            if ch is not self.grip:
                ch.installEventFilter(self)

    def childEvent(self, e):
        super().childEvent(e)

    def eventFilter(self, obj, e):
        t = e.type()
        if t == QEvent.Type.ChildAdded and isinstance(e.child(), QWidget):
            e.child().installEventFilter(self)
        if obj is self.grip:
            return False
        if t == QEvent.Type.MouseButtonPress and e.button() == Qt.MouseButton.LeftButton:
            self._press_pos = e.globalPosition().toPoint()
            self._win_offset = self._press_pos - self.frameGeometry().topLeft()
            self._dragging = False
            return False
        if t == QEvent.Type.MouseMove and self._press_pos is not None and e.buttons() & Qt.MouseButton.LeftButton:
            if self.ctrl.cfg.appearance.locked:
                return False
            gp = e.globalPosition().toPoint()
            if not self._dragging and (gp - self._press_pos).manhattanLength() > 6:
                self._dragging = True
                wh = self.windowHandle()
                if wh is not None and QGuiApplication.platformName() == "wayland" and wh.startSystemMove():
                    self._press_pos = None
                    return True
            if self._dragging:
                self.move(gp - self._win_offset)
                return True
            return False
        if t == QEvent.Type.MouseButtonRelease and e.button() == Qt.MouseButton.LeftButton:
            was_drag = self._dragging
            gp = e.globalPosition().toPoint()
            self._press_pos = None
            self._dragging = False
            if was_drag:
                self.remember_geometry()
                return True
            if self._tab_rect().contains(self.mapFromGlobal(gp).toPointF()):
                self._open_projects()
                return True
            if self.update_version and self._badge_rect().contains(self.mapFromGlobal(gp).toPointF()):
                self.open_update.emit()
                return True
            row = self.ping.row_at(gp)
            if row is not None:
                self.open_history.emit(row.target.id)
                return True
            if self.net.adapter_area_contains(gp):
                a = self.ctrl.network.active if self.ctrl.network else None
                self.open_ipconfig.emit(a.name if a else "")
                return True
        if t == QEvent.Type.MouseButtonDblClick and obj is self.title:
            self.open_settings.emit()
            return True
        return False

    def contextMenuEvent(self, e):
        m = self.build_menu()
        row = self.ping.row_at(e.globalPos())
        if row is not None:
            t = row.target
            first = m.actions()[0] if m.actions() else None
            title = QAction(f"{t.name}  ({t.host})", m)
            title.setEnabled(False)
            items = [title]
            for label, fn in (("Detail a graf…", lambda: self.open_history.emit(t.id)),
                              ("Testovat nyní", lambda: self.ctrl.test_now(t.id)),
                              ("Traceroute…", lambda: self.open_traceroute.emit(t.id)),
                              ("Probudit (Wake-on-LAN)" + ("" if t.mac else " …"),
                               lambda: self.wake_target.emit(t.id))):
                a = QAction(label, m)
                a.triggered.connect(fn)
                items.append(a)
            m.insertActions(first, items)
            m.insertSeparator(first)
        m.exec(e.globalPos())

    # ------------------------------------------------------------------ menu (sdílené s tray)
    def build_menu(self, parent=None, for_tray: bool = False) -> QMenu:
        cfg = self.ctrl.cfg
        a = cfg.appearance
        m = QMenu(parent)
        m.setStyleSheet(theme.DIALOG_QSS)
        if for_tray:
            act = m.addAction(tr("Zobrazit widget") if not self.isVisible() else tr("Skrýt do oznamovací oblasti"))
            act.triggered.connect(self.toggle_visible)
            m.addSeparator()
        if self.update_version:
            up = m.addAction(f"⬆  Aktualizovat na verzi {self.update_version}…")
            up.triggered.connect(self.open_update.emit)
            f = up.font()
            f.setBold(True)
            up.setFont(f)
            m.addSeparator()
        m.addAction(tr("Nastavení…"), self.open_settings.emit)
        m.addAction(tr("Historie…"), lambda: self.open_history.emit(""))
        m.addAction(tr("Test rychlosti internetu…"), self.open_speedtest.emit)
        add_wol_menu(m, self.ctrl, self.open_wol_devices)
        act_if = self.ctrl.network.active if self.ctrl.network else None
        m.addAction(tr("Nastavit IP adresu…"), lambda: self.open_ipconfig.emit(act_if.name if act_if else ""))
        m.addSeparator()
        mode = m.addMenu(tr("Režim zobrazení"))
        grp = QActionGroup(mode)
        for key, label in ((MODE_DESKTOP, tr("Na ploše (pod okny)")), (MODE_ONTOP, tr("Vždy navrchu"))):
            act = QAction(label, mode, checkable=True, checked=a.mode == key)
            act.triggered.connect(lambda _=False, k=key: self._set_appearance(mode=k))
            grp.addAction(act)
            mode.addAction(act)
        lock = m.addAction(tr("Uzamknout pozici"))
        lock.setCheckable(True)
        lock.setChecked(a.locked)
        lock.triggered.connect(lambda v: self._set_appearance(locked=v))
        ct = m.addAction(tr("Ignorovat myš (click-through)"))
        ct.setCheckable(True)
        ct.setChecked(a.click_through)
        ct.setEnabled(for_tray or not a.click_through)
        ct.triggered.connect(lambda v: self._set_click_through(v))
        op = m.addMenu(tr("Průhlednost pozadí"))
        for pct in (0, 20, 40, 60, 80, 100):
            label = f"{pct} %" + ("  (neprůhledné)" if pct == 0 else "  (jen text)" if pct == 100 else "")
            act = op.addAction(label)
            act.setCheckable(True)
            act.setChecked(abs((1 - a.glass_alpha) - pct / 100) < 0.01)
            act.triggered.connect(lambda _=False, g=1 - pct / 100: self._set_appearance(glass_alpha=g))
        sz = m.addMenu(tr("Velikost"))
        for label, (w, h) in ((tr("Mini (300 × 430)"), (300, 430)), (tr("Malý (380 × 540)"), (380, 540)),
                              (tr("Kompaktní (460 × 650)"), (460, 650)), (tr("Výchozí (600 × 850)"), (600, 850)),
                              (tr("Velký (760 × 1076)"), (760, 1076))):
            sz.addAction(label, lambda w=w, h=h: self.set_size(w, h))
        m.addSeparator()
        m.addAction(tr("Obnovit nyní"), self.refresh_now)
        if not for_tray:
            m.addAction(tr("Skrýt do oznamovací oblasti"), self.hide)
        m.addSeparator()
        m.addAction(tr("O aplikaci…"), self.open_about.emit)
        m.addAction(tr("Ukončit"), self.quit_requested.emit)
        return m

    def open_wol_devices(self) -> None:
        if self._wol_devices_dialog is None:
            self._wol_devices_dialog = WolDevicesDialog(self.ctrl, self)
        self._wol_devices_dialog.show()
        self._wol_devices_dialog.raise_()
        self._wol_devices_dialog.activateWindow()

    def _set_appearance(self, **kw) -> None:
        cfg = self.ctrl.cfg.copy()
        for k, v in kw.items():
            setattr(cfg.appearance, k, v)
        self.ctrl.update_config(cfg)

    def _set_click_through(self, v: bool) -> None:
        if v and not QApplication.instance().property("tray_available"):
            log.warning("Click-through bez tray ikony nelze bezpečně vypnout – ignoruji")
            return
        self._set_appearance(click_through=v)

    def refresh_now(self) -> None:
        self.ctrl.refresh_network()
        self.ctrl.refresh_wan()
        self.ctrl.engine.reschedule_all()

    def toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_() if self.ctrl.cfg.appearance.mode == MODE_ONTOP else None


__all__ = ["MainWidget", "QCursor", "QPen"]
