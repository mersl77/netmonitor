"""Wake-on-LAN (magic packet) a zjištění MAC adresy z ARP tabulky systému.

Magic packet = 6× 0xFF + 16× MAC adresa, posláno UDP broadcastem na zvolený port.
Bez explicitního portu zůstává původní odesílání na porty 9 a 7.
Posílá se na 255.255.255.255 i na broadcast podsítě aktivní karty a socket se váže na IP
této karty – na počítači s více síťovkami tak paket odejde správným rozhraním.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import sys
from pathlib import Path
from dataclasses import dataclass

from ..utils.proc import run_cmd

_MAC_ANY = re.compile(r"^[0-9a-fA-F]{12}$")
_MAC_IN_TEXT = re.compile(r"(?<![0-9a-fA-F:-])((?:[0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2})(?![0-9a-fA-F:-])")
WOL_PORTS = (9, 7)


def parse_mac(text: str) -> bytes:
    """Přijímá aa:bb:cc:dd:ee:ff, AA-BB-…, aabb.ccdd.eeff i aabbccddeeff. Neplatná => ValueError."""
    s = str(text).strip()
    parts = re.split(r"[:-]", s)
    if len(parts) == 6 and all(1 <= len(p) <= 2 for p in parts):
        s = "".join(p.zfill(2) for p in parts)
    else:
        s = s.replace(".", "")
    if not _MAC_ANY.match(s):
        raise ValueError(f"Neplatná MAC adresa: {text!r}")
    mac = bytes.fromhex(s)
    if mac == b"\x00" * 6 or mac == b"\xff" * 6:
        raise ValueError("MAC adresa nesmí být samé nuly ani broadcast")
    return mac


def format_mac(text_or_bytes) -> str:
    mac = text_or_bytes if isinstance(text_or_bytes, bytes) else parse_mac(text_or_bytes)
    return ":".join(f"{b:02X}" for b in mac)


def magic_packet(mac: bytes) -> bytes:
    return b"\xff" * 6 + mac * 16


def directed_broadcast(ip: str, mask: str) -> str | None:
    try:
        net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
    except ValueError:
        return None
    return str(net.broadcast_address) if net.prefixlen < 31 else None


def validate_destination(mac: str, broadcast: str | None, port: int | None) -> None:
    parse_mac(mac)
    if broadcast:
        try:
            ipaddress.IPv4Address(broadcast)
        except ValueError:
            raise ValueError("Neplatná broadcast adresa pro WoL; zadejte IPv4 adresu") from None
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("Port musí být celé číslo 1–65535")


def send(mac_text: str, local_ip: str | None = None, netmask: str | None = None,
         extra_broadcast: str | None = None, port: int | None = None) -> list[str]:
    """Odešle magic packet. Vrací seznam cílů (adresa:port), kam odešel. Chyba => OSError/ValueError."""
    validate_destination(mac_text, extra_broadcast, port)
    pkt = magic_packet(parse_mac(mac_text))
    ports = WOL_PORTS if port is None else (port,)
    targets: list[str] = ["255.255.255.255"]
    if local_ip and netmask:
        b = directed_broadcast(local_ip, netmask)
        if b and b not in targets:
            targets.append(b)
    if extra_broadcast:
        if extra_broadcast not in targets:
            targets.append(extra_broadcast)
    sent = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if local_ip:
            try:
                s.bind((local_ip, 0))
            except OSError:
                pass
        for addr in targets:
            for destination_port in ports:
                s.sendto(pkt, (addr, destination_port))
                sent.append(f"{addr}:{destination_port}")
    return sent


# ---------------------------------------------------------------------------- ARP

@dataclass(frozen=True)
class Neighbor:
    ip: str
    mac: str


def parse_neighbors(text: str) -> list[Neighbor]:
    """IPv4 sousedé z ip neigh, /proc/net/arp, arp -a (i CZ) nebo arp -an; bez DNS."""
    found: list[Neighbor] = []
    for line in text.splitlines():
        if re.search(r"\b(INCOMPLETE|FAILED)\b", line, re.IGNORECASE):
            continue
        ip = re.search(r"(?<![\w.:])(?:\d{1,3}\.){3}\d{1,3}(?![\w.:])", line)
        mac = _MAC_IN_TEXT.search(line)
        if not ip or not mac:
            continue
        try:
            address = ipaddress.IPv4Address(ip.group())
            hardware = parse_mac(mac.group(1))
        except ValueError:
            continue
        if address.is_multicast or address.is_unspecified or hardware[0] & 1:
            continue
        neighbor = Neighbor(str(address), format_mac(hardware))
        if neighbor not in found:
            found.append(neighbor)
    return found


def list_neighbors() -> list[Neighbor]:
    """Čte pouze již známé sousedy; neosvěžuje ARP ani neskenuje podsíť."""
    if sys.platform.startswith("linux"):
        out = run_cmd(["ip", "neigh", "show"], timeout=3) or ""
        neighbors = parse_neighbors(out)
        if neighbors:
            return neighbors
        return parse_neighbors(Path("/proc/net/arp").read_text())
    args = ["arp", "-an"] if sys.platform == "darwin" else ["arp", "-a"]
    return parse_neighbors(run_cmd(args, timeout=3, c_locale=False) or "")

def parse_arp_output(text: str, ip: str) -> str | None:
    """Najde MAC u dané IP ve výstupu `arp -a` (Windows, libovolný jazyk) / `arp -n` (macOS) / /proc/net/arp."""
    for line in text.splitlines():
        if not re.search(rf"(?<![\d.]){re.escape(ip)}(?![\d.])", line):
            continue
        m = _MAC_IN_TEXT.search(line)
        if m:
            try:
                mac = parse_mac(m.group(1))
            except ValueError:
                continue
            return format_mac(mac)
    return None


def arp_lookup(ip: str) -> str | None:
    """MAC adresa IPv4 z ARP tabulky (zařízení musí být nedávno dostupné – nejdřív ho pingněte)."""
    try:
        ipaddress.IPv4Address(ip)
    except ValueError:
        return None
    if sys.platform.startswith("linux"):
        try:
            return parse_arp_output(Path("/proc/net/arp").read_text(), ip)
        except OSError:
            pass
        out = run_cmd(["ip", "neigh", "show", ip], timeout=3) or ""
        return parse_arp_output(out, ip)
    if sys.platform == "darwin":
        return parse_arp_output(run_cmd(["arp", "-n", ip], timeout=3) or "", ip)
    return parse_arp_output(run_cmd(["arp", "-a", ip], timeout=3, c_locale=False) or "", ip)
