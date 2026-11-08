"""Samostatný seznam zařízení WoL a výběr z již známých sousedů (bez skenování)."""
from __future__ import annotations

from PySide6.QtCore import QModelIndex, Qt, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMenu, QMessageBox, QPushButton, QSpinBox, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout)
from shiboken6 import isValid

from ..core import wol
from ..core.config import WolDevice, new_id
from . import theme
from .i18n import tr
from .tools_dialog import wake


class NeighborsDialog(QDialog):
    def __init__(self, ctrl, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Vybrat ze sítě")
        self.setStyleSheet(theme.DIALOG_QSS)
        self.resize(480, 300)
        layout = QVBoxLayout(self)
        self.status = QLabel("Načítám tabulku sousedů…")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTreeWidget()
        self.table.setHeaderLabels(["IP adresa", "MAC adresa"])
        self.table.setRootIsDecorated(False)
        layout.addWidget(self.table)
        self.choose = QPushButton("Vybrat")
        self.choose.setEnabled(False)
        self.choose.clicked.connect(self.accept)
        self.table.itemSelectionChanged.connect(lambda: self.choose.setEnabled(self.table.currentItem() is not None))
        self.table.itemDoubleClicked.connect(lambda *_: self.accept())
        layout.addWidget(self.choose)

        def job():
            try:
                return wol.list_neighbors(), ""
            except Exception as e:
                return [], str(e)

        def finished(result):
            if not isValid(self) or self._closed:
                return
            neighbors, error = result
            if error:
                self.status.setText(f"Tabulku sousedů nelze načíst: {error}")
                return
            self.status.setText("Vyberte známého souseda. Vypnuté zařízení v tabulce nemusí být; MAC lze zadat ručně."
                                if neighbors else "Tabulka sousedů je prázdná. MAC lze zadat ručně.")
            for n in neighbors:
                QTreeWidgetItem(self.table, [n.ip, n.mac])
            self.table.resizeColumnToContents(0)

        self._closed = False
        self.finished.connect(lambda _: setattr(self, "_closed", True))
        ctrl._run_bg(f"wol-neighbors-{id(self)}", job, finished)

    def selected(self) -> wol.Neighbor | None:
        item = self.table.currentItem()
        return wol.Neighbor(item.text(0), item.text(1)) if item else None


class WolDevicesDialog(QDialog):
    def __init__(self, ctrl, parent=None):
        super().__init__(parent)
        self.ctrl = ctrl
        self.edit_id: str | None = None
        self.setWindowTitle("Probudit zařízení…")
        self.setStyleSheet(theme.DIALOG_QSS)
        self.resize(620, 560)
        layout = QVBoxLayout(self)
        self.table = QTreeWidget()
        self.table.setHeaderLabels(["Zařízení", "MAC adresa", "Ověření"])
        self.table.setRootIsDecorated(False)
        self.table.currentItemChanged.connect(self._selected)
        layout.addWidget(self.table)
        form = QFormLayout()
        self.name = QLineEdit()
        self.mac = QLineEdit()
        self.broadcast = QLineEdit()
        self.broadcast.setPlaceholderText("automaticky podle aktivního adaptéru")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.host = QLineEdit()
        self.host.setPlaceholderText("volitelné – IP nebo hostname pro ověření pingem")
        for label, field in (("Název", self.name), ("MAC", self.mac), ("Broadcast", self.broadcast),
                             ("UDP port", self.port), ("Ověřit dostupnost", self.host)):
            form.addRow(label, field)
        layout.addLayout(form)
        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QHBoxLayout()
        for text, slot in (("Přidat", self.new), ("Vybrat ze sítě", self._neighbors),
                           ("Uložit", self.save), ("Smazat", self.remove), ("Probudit", self.send)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        close = QPushButton("Zavřít")
        close.clicked.connect(self.close)
        layout.addWidget(close)
        ctrl.config_changed.connect(self._refresh)
        self._refresh()
        self.new()

    def _refresh(self, *_):
        self.table.blockSignals(True)
        self.table.clear()
        for device in self.ctrl.cfg.wol.devices:
            item = QTreeWidgetItem(self.table, [device.name, device.mac, device.host])
            item.setData(0, Qt.ItemDataRole.UserRole, device.id)
            if device.id == self.edit_id:
                self.table.setCurrentItem(item)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(1)
        self.table.blockSignals(False)
        if self.edit_id is not None and not any(d.id == self.edit_id for d in self.ctrl.cfg.wol.devices):
            self.new()

    def new(self):
        self.edit_id = None
        self.table.clearSelection()
        self.table.setCurrentIndex(QModelIndex())
        self.name.clear()
        self.mac.clear()
        self.host.clear()
        self.port.setValue(9)
        active = self.ctrl.network.active if self.ctrl.network else None
        broadcast = (wol.directed_broadcast(active.ipv4[0], active.ipv4_masks[0])
                     if active and active.ipv4 and active.ipv4_masks else None)
        self.broadcast.clear()
        self.broadcast.setPlaceholderText(broadcast or "automaticky podle aktivního adaptéru")
        self.error.clear()

    def _selected(self, item, _previous):
        if item is None:
            return
        device = next((d for d in self.ctrl.cfg.wol.devices
                       if d.id == item.data(0, Qt.ItemDataRole.UserRole)), None)
        if device:
            self.edit_id = device.id
            for key in ("name", "mac", "host", "broadcast"):
                getattr(self, key).setText(getattr(device, key))
            self.port.setValue(device.port)
            self.error.clear()

    def save(self) -> WolDevice | None:
        device = WolDevice(name=self.name.text(), mac=self.mac.text(), broadcast=self.broadcast.text(),
                           port=self.port.value(), host=self.host.text(), id=self.edit_id or new_id()).normalized()
        errors = device.validate()
        if errors:
            self.error.setText("\n".join(errors))
            return None
        device.mac = wol.format_mac(device.mac)
        cfg = self.ctrl.cfg.copy()
        cfg.wol.devices = [device if d.id == device.id else d for d in cfg.wol.devices]
        if not any(d.id == device.id for d in cfg.wol.devices):
            cfg.wol.devices.append(device)
        self.edit_id = device.id
        self.ctrl.update_config(cfg)
        self.error.clear()
        return device

    def remove(self):
        if self.edit_id is None:
            return
        if QMessageBox.question(self, "Smazat zařízení", "Smazat vybrané zařízení ze seznamu WoL?") != QMessageBox.StandardButton.Yes:
            return
        cfg = self.ctrl.cfg.copy()
        cfg.wol.devices = [d for d in cfg.wol.devices if d.id != self.edit_id]
        self.ctrl.update_config(cfg)
        self.new()

    def send(self):
        device = self.save()
        if device:
            wake(self.ctrl, device)

    def _neighbors(self):
        dialog = NeighborsDialog(self.ctrl, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            neighbor = dialog.selected()
            if neighbor:
                self.mac.setText(neighbor.mac)
                self.host.setText(neighbor.ip)
                if not self.name.text().strip():
                    self.name.setText(neighbor.ip)
        dialog.deleteLater()


class _WolQuickMenu(QMenu):
    """Samostatně aktualizované podmenu, i když tray zachovává původní QMenu."""

    def __init__(self, ctrl, parent=None):
        super().__init__(tr("Uložená zařízení WoL"), parent)
        self.ctrl = ctrl
        self._signature: list[tuple[str, str]] | None = None
        ctrl.config_changed.connect(self.refresh)
        self.refresh(ctrl.cfg)

    @Slot(object)
    def refresh(self, _cfg):
        signature = [(d.id, d.name) for d in self.ctrl.cfg.wol.devices]
        if signature == self._signature:
            return
        self._signature = signature
        self.clear()
        if not signature:
            self.addAction(tr("(žádná zařízení)")).setEnabled(False)
        for device_id, name in signature:
            action = self.addAction(name.replace("&", "&&"))
            action.setData(("wol_device", device_id))
            action.triggered.connect(self._wake)

    @Slot()
    def _wake(self):
        # ID se řeší až při kliknutí, aby se neposlalo na zastaralou MAC po úpravě seznamu.
        action = self.sender()
        if isinstance(action, QAction):
            _, device_id = action.data()
            current = next((d for d in self.ctrl.cfg.wol.devices if d.id == device_id), None)
            if current:
                wake(self.ctrl, current)


def add_wol_menu(menu, ctrl, open_dialog):
    """Jediný vstup pro widget/tray; podmenu si obnovu konfigurace zajišťuje samo."""
    menu.addAction(tr("Probudit zařízení…"), open_dialog)
    menu.addMenu(_WolQuickMenu(ctrl, menu))
