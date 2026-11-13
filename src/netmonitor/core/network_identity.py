"""Rozpoznání sítě, ve které počítač právě je – pro nabídku přepnutí profilu.

Wi-Fi se pozná podle názvu (SSID), kabelová síť podle IP a MAC adresy výchozí brány.
Samotná IP brány nestačí: 192.168.1.1 má doma i ve firmě kdekdo.
"""
from __future__ import annotations

from typing import Callable

from .network_info import NetworkSnapshot
from .wol import arp_lookup


def identify(snap: NetworkSnapshot | None, arp: Callable[[str], str | None] = arp_lookup) -> tuple[str, str] | None:
    """(klíč, popis pro uživatele), nebo None, když síť (zatím) nejde jednoznačně určit."""
    active = snap.active if snap else None
    if active is None:
        return None
    if active.kind == "wifi" and active.ssid:
        return "wifi:" + active.ssid, f"Wi-Fi „{active.ssid}“"
    if active.gateway:
        mac = arp(active.gateway)
        if mac:
            return f"lan:{active.gateway}|{mac.lower()}", f"síť s bránou {active.gateway}"
    return None
