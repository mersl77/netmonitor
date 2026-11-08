"""Ikona v oznamovací oblasti – stav připojení barvou, menu, lokální upozornění."""
from __future__ import annotations

from PySide6.QtWidgets import QSystemTrayIcon

from ..core.connection_status import ConnEvaluation, ConnState
from ..core.notifier import Notification
from .icons import app_icon


class Tray(QSystemTrayIcon):
    def __init__(self, widget):
        super().__init__(app_icon("unknown"))
        self.widget = widget
        self._state = None
        self._update = False
        self.setToolTip("NetMonitor")
        # QMenu i jeho akce musí žít po celou dobu exportu přes dbusmenu.
        self._menu = widget.build_menu(for_tray=True)
        self.setContextMenu(self._menu)
        self.activated.connect(self._on_activated)
        widget.ctrl.connection_changed.connect(self.on_connection)
        widget.ctrl.notification.connect(self.on_notification)
        widget.visibility_toggled.connect(lambda _v: self._refresh_menu())
        widget.ctrl.config_changed.connect(lambda _c: self._refresh_menu())

    def _refresh_menu(self) -> None:
        self.widget.update_menu(self._menu)

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.widget.toggle_visible()

    def on_connection(self, ev: ConnEvaluation) -> None:
        state = {ConnState.ONLINE: "ok", ConnState.DEGRADED: "warn", ConnState.OFFLINE: "bad"}.get(ev.state, "unk")
        if state != self._state:
            self._state = state
            self.setIcon(app_icon(state, self._update))
        wan = self.widget.ctrl.wan.current or "N/A"
        self.setToolTip(f"NetMonitor – {ev.state.value}\nWAN: {wan}\n{ev.reason}"[:120])

    def set_update(self, info) -> None:
        self._update = info is not None
        self.setIcon(app_icon(self._state or "unk", self._update))
        self._refresh_menu()
        if info is not None:
            self.showMessage("NetMonitor – nová verze", f"Je dostupná verze {info.version}. "
                             "Klikněte pravým tlačítkem na ikonu → Aktualizovat.",
                             QSystemTrayIcon.MessageIcon.Information, 10000)

    def on_notification(self, n: Notification) -> None:
        icon = QSystemTrayIcon.MessageIcon.Warning if n.critical else QSystemTrayIcon.MessageIcon.Information
        self.showMessage(n.title, n.message, icon, 8000)
