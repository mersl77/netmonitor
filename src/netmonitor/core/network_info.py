"""Zjištění aktivního síťového rozhraní, LAN IP, brány, rychlosti linky, Wi-Fi a DNS.

Aktivní rozhraní = rozhraní, přes které vede výchozí trasa. Zjišťuje se
spolehlivě a bez odeslání paketu: UDP socket ``connect()`` na veřejnou adresu
nastaví zdrojovou IP podle směrovací tabulky; tu pak dohledáme v psutil.

Pozor: "Speed" je *linková* rychlost adaptéru (vyjednaná s AP/switchem),
nikoli rychlost internetu. V UI je tak i popsána.
"""
from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass, field

import psutil

from .. import platform as plat
from .ipconfig import parse_mask

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InterfaceInfo:
    name: str
    display_name: str
    kind: str = "other"               # wifi | ethernet | vpn | loopback | other
    is_up: bool = False
    ipv4: tuple[str, ...] = ()
    ipv4_masks: tuple[str, ...] = ()
    ipv6: tuple[str, ...] = ()
    mac: str = ""
    speed_mbps: float | None = None   # linková rychlost
    ssid: str | None = None
    signal_pct: int | None = None
    signal_dbm: int | None = None
    wifi_note: str = ""
    gateway: str | None = None
    dns: tuple[str, ...] = ()
    dns_note: str = ""

    @property
    def kind_label(self) -> str:
        return {"wifi": "Wireless", "ethernet": "Ethernet", "vpn": "VPN", "loopback": "Loopback"}.get(
            self.kind, "Adapter")


@dataclass(frozen=True)
class NetworkSnapshot:
    active: InterfaceInfo | None
    interfaces: tuple[InterfaceInfo, ...] = ()
    has_default_route: bool = False
    default_iface: str | None = None
    ts: float = field(default_factory=time.time)
    error: str = ""

    def signature(self) -> tuple:
        a = self.active
        if not a:
            return (None, self.has_default_route)
        return (a.name, a.ipv4, a.gateway, a.dns, a.ssid, self.has_default_route)


@dataclass(frozen=True)
class AdapterConfiguration:
    """Stav pro editor. None = nezjištěno, prázdný řetězec/tuple = ověřená absence."""
    iface: str
    ip: str | None = None
    mask: str | None = None
    gateway: str | None = None
    dns: tuple[str, ...] | None = None
    dhcp: bool | None = None
    dns_dhcp: bool | None = None
    ipv4: tuple[str, ...] = ()

    @property
    def apply_blockers(self) -> list[str]:
        errors = []
        if len(self.ipv4) > 1:
            errors.append("karta má více IPv4 adres – změnu proveďte v nastavení systému")
        if any(":" in server for server in self.dns or ()):
            errors.append("Karta má IPv6 DNS – změnu proveďte v nastavení systému")
        return errors

    @property
    def unknown(self) -> list[str]:
        values = (("IP", self.ip), ("maska", self.mask), ("brána", self.gateway), ("DNS", self.dns),
                  ("režim IP", self.dhcp), ("režim DNS", self.dns_dhcp))
        return [label for label, value in values if value is None]

    def summary(self) -> str:
        prefix = "nezjištěno" if self.mask is None else "–"
        if self.mask:
            try:
                prefix = f"/{parse_mask(self.mask)}"
            except ValueError:
                prefix = "nezjištěno"
        mode = {True: "DHCP", False: "pevná IP", None: "nezjištěno"}[self.dhcp]
        dns_mode = {True: "automaticky", False: "ručně", None: "nezjištěno"}[self.dns_dhcp]
        dns = "nezjištěno" if self.dns is None else ", ".join(self.dns) or "žádné"
        return (f"IP: {self.ip if self.ip is not None else 'nezjištěno'} ({mode}), prefix {prefix}\n"
                f"Brána: {(self.gateway or 'žádná') if self.gateway is not None else 'nezjištěno'}\n"
                f"DNS: {dns} ({dns_mode})")


def route_source_ip(family: int = socket.AF_INET) -> str | None:
    probe = ("192.0.2.1", 9) if family == socket.AF_INET else ("2001:db8::1", 9, 0, 0)
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as s:
            s.connect(probe)   # UDP connect neposílá žádná data
            return s.getsockname()[0]
    except OSError:
        return None


PSUTIL_32BIT_OVERFLOW = 4294   # 0xFFFFFFFF b/s / 1e6 – psutil na Windows čte rychlost jako 32bit DWORD


def _sane_speed(mbps) -> int | None:
    """psutil: 0 = neznámé; 4294 = přetečení 32bit hodnoty (linky > 4,29 Gb/s, např. 10GbE) => neznámé."""
    if not mbps or mbps <= 0 or mbps == PSUTIL_32BIT_OVERFLOW:
        return None
    return mbps


def _psutil_interfaces() -> dict[str, dict]:
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    res = {}
    for name, lst in addrs.items():
        v4 = tuple(a.address for a in lst if a.family == socket.AF_INET)
        v4m = tuple((a.netmask or "") for a in lst if a.family == socket.AF_INET)
        v6 = tuple(a.address.split("%")[0] for a in lst if a.family == socket.AF_INET6)
        mac = next((a.address for a in lst if a.family == psutil.AF_LINK), "")
        st = stats.get(name)
        res[name] = {"ipv4": v4, "ipv4_masks": v4m, "ipv6": v6, "mac": mac, "is_up": bool(st and st.isup),
                     "speed": _sane_speed(st.speed if st else None)}
    return res


class NetworkInfoProvider:
    def __init__(self, platform_mod=None):
        self.p = platform_mod or plat.current()

    def list_interfaces(self) -> list[str]:
        return sorted(_psutil_interfaces())

    def ip_configuration(self, iface: str) -> AdapterConfiguration:
        """Čerstvé údaje vybrané karty bez globálních fallbacků DNS/brány; volat na pozadí."""
        try:
            addresses = _psutil_interfaces().get(iface)
            if addresses is None:
                return AdapterConfiguration(iface)
            state = self.p.ip_config_details(iface)
            ips = addresses["ipv4"]
            masks = addresses.get("ipv4_masks", ())
            mask = (masks[0] or None) if ips and masks else (None if ips else "")
            if mask:
                parse_mask(mask)
            return AdapterConfiguration(iface, ips[0] if ips else "", mask,
                                        state.get("gateway"), state.get("dns"), state.get("dhcp"),
                                        state.get("dns_dhcp"), ipv4=tuple(ips))
        except Exception:  # noqa: BLE001
            log.debug("Detaily IP karty nelze načíst", exc_info=True)
            return AdapterConfiguration(iface)

    def snapshot(self, preferred_iface: str | None = None, with_wifi: bool = True) -> NetworkSnapshot:
        try:
            return self._snapshot(preferred_iface, with_wifi)
        except Exception as e:  # noqa: BLE001
            log.exception("Chyba při zjišťování sítě")
            return NetworkSnapshot(active=None, error=str(e))

    def _snapshot(self, preferred_iface: str | None, with_wifi: bool) -> NetworkSnapshot:
        ifs = _psutil_interfaces()
        details = self.p.adapter_details() or {}
        src = route_source_ip()
        default_iface = None
        if src:
            default_iface = next((n for n, d in ifs.items() if src in d["ipv4"]), None)
        gw_default, gw_iface = self.p.default_gateway()
        if not default_iface and gw_iface in ifs:
            default_iface = gw_iface
        has_route = bool(src) or bool(gw_default)

        name = preferred_iface if preferred_iface in ifs else default_iface
        infos = []
        for n, d in ifs.items():
            det = details.get(n, {})
            infos.append(InterfaceInfo(name=n, display_name=det.get("display_name", n),
                                       kind=det.get("kind", "other"), is_up=d["is_up"], ipv4=d["ipv4"],
                                       ipv4_masks=d.get("ipv4_masks", ()),
                                       ipv6=d["ipv6"], mac=d["mac"], speed_mbps=d["speed"]))
        if not name:
            return NetworkSnapshot(active=None, interfaces=tuple(infos), has_default_route=has_route,
                                   default_iface=default_iface)

        d, det = ifs[name], details.get(name, {})
        kind = det.get("kind") or "other"
        if kind == "other" and hasattr(self.p, "interface_kind"):
            kind = self.p.interface_kind(name)
        gateway = None
        if det.get("gateways"):
            gateway = next((g for g in det["gateways"] if ":" not in g), det["gateways"][0])
        if gateway is None:
            gateway, _ = self.p.default_gateway(name)
        if "dns" in det:   # Windows: už máme z GetAdaptersAddresses
            dns = [x for x in det["dns"] if not x.lower().startswith("fec0:")]
            dns_note = "zdroj: GetAdaptersAddresses" if dns else "DNS nezjištěno"
        else:
            dns, dns_note = self.p.dns_servers(name)
        # Windows: 64bit ReceiveLinkSpeed z GetAdaptersAddresses má přednost (psutil přetéká u 10GbE)
        speed = det.get("speed_mbps") or d["speed"]
        if speed is None and hasattr(self.p, "link_speed_sysfs"):
            speed = self.p.link_speed_sysfs(name)
        wifi = {}
        if kind == "wifi" and with_wifi:
            wifi = self.p.wifi_info(name) or {}
            if not speed and wifi.get("rate_mbps"):
                speed = wifi["rate_mbps"]
        active = InterfaceInfo(
            name=name, display_name=det.get("display_name", name), kind=kind, is_up=d["is_up"],
            ipv4=d["ipv4"], ipv4_masks=d.get("ipv4_masks", ()), ipv6=tuple(a for a in d["ipv6"] if not a.lower().startswith("fe80")) or d["ipv6"],
            mac=d["mac"], speed_mbps=speed, ssid=wifi.get("ssid"), signal_pct=wifi.get("signal_pct"),
            signal_dbm=wifi.get("signal_dbm"), wifi_note=wifi.get("note", ""), gateway=gateway,
            dns=tuple(dns), dns_note=dns_note,
        )
        return NetworkSnapshot(active=active, interfaces=tuple(infos), has_default_route=has_route,
                               default_iface=default_iface)
