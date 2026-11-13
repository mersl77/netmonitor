"""Dotazy spojené s profily: použití IP/DNS předvolby po přepnutí a nabídka profilu podle sítě."""
from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtWidgets import QMessageBox

from . import theme

log = logging.getLogger(__name__)
_open: list[QMessageBox] = []        # nemodální dotazy potřebují trvalý odkaz


def offer_ip_preset(ctrl, preset: str, open_ipconfig: Callable[[str, str], None], parent=None) -> None:
    """Po přepnutí profilu: zeptá se a otevře dialog Nastavit IP adresu s načtenou předvolbou.

    Samo se nic nenastaví – změnu IP potvrzuje uživatel v dialogu (a systém dotazem správce).
    """
    if not any(p.get("name") == preset for p in ctrl.cfg.network.ip_profiles):
        return
    active = ctrl.network.active if ctrl.network else None
    if QMessageBox.question(parent, "NetMonitor – IP adresa",
                            f"Tento profil má IP/DNS předvolbu „{preset}“.\nChcete ji teď použít"
                            + (f" na kartě {active.display_name}?" if active else "?")) \
            == QMessageBox.StandardButton.Yes:
        open_ipconfig(active.name if active else "", preset)


def suggest_profile(ctrl, profile_id: str, name: str, network: str) -> None:
    """Nemodální nabídka: počítač je v síti spojené s jiným profilem."""
    box = QMessageBox()
    box.setStyleSheet(theme.DIALOG_QSS)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("NetMonitor – profil")
    box.setText(f"Připojeno: {network}.\nPřepnout na profil „{name}“?")
    switch = box.addButton("Přepnout", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Teď ne", QMessageBox.ButtonRole.RejectRole)
    box.setModal(False)

    def finished(_result) -> None:
        if box in _open:
            _open.remove(box)
        if box.clickedButton() == switch:
            try:
                ctrl.switch_profile(profile_id)
            except (OSError, ValueError, KeyError) as exc:
                log.error("Profil nelze přepnout: %s", exc)
        box.deleteLater()

    box.finished.connect(finished)
    _open.append(box)
    box.show()
    box.raise_()
    box.activateWindow()
