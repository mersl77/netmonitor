"""Veřejná (WAN) IPv4 a ISP – bezplatné HTTPS služby bez API klíče, s fallbackem a cache.

Dokumentace použitých služeb viz docs/EXTERNAL_APIS.md. Vše lze vypnout v nastavení
(Síť → Povolit externí dotazy). Neodesílá se nic jiného než samotný HTTP GET.
"""
from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import __version__
from ..utils.validation import is_ipv4

log = logging.getLogger(__name__)

WAN_PROVIDERS = [
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://checkip.amazonaws.com",
    "https://ifconfig.me/ip",
]
# připraveno pro IPv6: "https://api6.ipify.org", "https://ipv6.icanhazip.com"

USER_AGENT = f"NetMonitor/{__version__}"
Fetcher = Callable[[str, float], str]


def http_get(url: str, timeout: float = 5.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:  # noqa: S310 – pouze https URL
        return r.read(65536).decode("utf-8", errors="replace")


@dataclass(frozen=True)
class WanResult:
    ip: str | None
    provider: str = ""
    error: str = ""
    ts: float = 0.0


def fetch_wan_ip(fetch: Fetcher = http_get, providers: list[str] | None = None, timeout: float = 5.0) -> WanResult:
    errors = []
    for url in providers or WAN_PROVIDERS:
        try:
            ip = fetch(url, timeout).strip()
            if is_ipv4(ip):
                return WanResult(ip, url, ts=time.time())
            errors.append(f"{url}: neplatná odpověď")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {e}")
    return WanResult(None, error="; ".join(errors)[:500], ts=time.time())


def isp_ipinfo(ip: str, fetch: Fetcher = http_get) -> str | None:
    data = json.loads(fetch(f"https://ipinfo.io/{ip}/json", 6))
    org = data.get("org")
    return str(org) if org else None


def isp_ripestat(ip: str, fetch: Fetcher = http_get) -> str | None:
    ni = json.loads(fetch(f"https://stat.ripe.net/data/network-info/data.json?resource={ip}&sourceapp=netmonitor", 6))
    asns = ni.get("data", {}).get("asns") or []
    if not asns:
        return None
    asn = asns[0]
    ov = json.loads(fetch(f"https://stat.ripe.net/data/as-overview/data.json?resource=AS{asn}&sourceapp=netmonitor", 6))
    holder = ov.get("data", {}).get("holder") or ""
    return f"AS{asn} {holder}".strip()


ISP_PROVIDERS = {"ipinfo": isp_ipinfo, "ripestat": isp_ripestat}


class WanState:
    """Drží aktuální/předchozí WAN IP, čas změny a cache ISP (perzistentně v JSON)."""

    def __init__(self, state_file: Path | None = None):
        self.state_file = state_file
        self.current: str | None = None
        self.previous: str | None = None
        self.changed_at: float | None = None
        self.last_check: float | None = None
        self.last_error: str = ""
        self.isp_cache: dict[str, tuple[str, float]] = {}
        self._load()

    def _load(self) -> None:
        if not self.state_file or not self.state_file.exists():
            return
        try:
            d = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.current = d.get("current")
            self.previous = d.get("previous")
            self.changed_at = d.get("changed_at")
            self.isp_cache = {k: (v[0], v[1]) for k, v in (d.get("isp_cache") or {}).items()}
        except (OSError, ValueError, TypeError, IndexError):
            pass

    def save(self) -> None:
        if not self.state_file:
            return
        from .config import atomic_write_json
        try:
            atomic_write_json(self.state_file, {
                "current": self.current, "previous": self.previous, "changed_at": self.changed_at,
                "isp_cache": {k: list(v) for k, v in list(self.isp_cache.items())[-20:]},
            })
        except OSError as e:
            log.warning("Nelze uložit stav WAN: %s", e)

    def update(self, r: WanResult, *, persist: bool = True) -> bool:
        """Vrátí True při změně známé IP; persist=False nechá uložení na volajícím."""
        self.last_check = r.ts
        self.last_error = r.error
        if not r.ip:
            return False
        changed = self.current is not None and r.ip != self.current
        if self.current != r.ip:
            self.previous = self.current
            self.current = r.ip
            self.changed_at = r.ts
            if persist:
                self.save()
        return changed

    def isp_for(self, ip: str, provider: str, max_age_s: float, fetch: Fetcher = http_get) -> str | None:
        hit = self.isp_cache.get(ip)
        if hit and time.time() - hit[1] < max_age_s:
            return hit[0]
        order = [provider] + [p for p in ISP_PROVIDERS if p != provider]
        for p in order:
            try:
                name = ISP_PROVIDERS[p](ip, fetch)
                if name:
                    self.isp_cache[ip] = (name, time.time())
                    self.save()
                    return name
            except Exception as e:  # noqa: BLE001
                log.info("ISP lookup přes %s selhal: %s", p, e)
        return hit[0] if hit else None
