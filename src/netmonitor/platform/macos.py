"""macOS: brána přes `route -n get default`, DNS přes `scutil --dns`, Wi-Fi přes `ipconfig getsummary`,
autostart přes LaunchAgent v ~/Library/LaunchAgents.

Pozn.: macOS 14.4+ skrývá SSID ("<redacted>") aplikacím bez oprávnění k poloze –
v takovém případě se zobrazí N/A.
"""
from __future__ import annotations

import plistlib
import re
from pathlib import Path

from ..utils.proc import run_cmd

LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "eu.zelina.netmonitor.plist"


def default_gateway(iface: str | None = None) -> tuple[str | None, str | None]:
    out = run_cmd(["route", "-n", "get", "default"], timeout=2) or ""
    gw = re.search(r"gateway:\s*(\S+)", out)
    itf = re.search(r"interface:\s*(\S+)", out)
    g, i = (gw.group(1) if gw else None), (itf.group(1) if itf else None)
    if iface and i and iface != i:
        return None, i
    return g, i


def parse_scutil_dns(text: str, iface: str | None) -> list[str]:
    """Preferuje 'scoped' resolver pro dané rozhraní, jinak resolver #1."""
    main, _, scoped_part = text.partition("DNS configuration (for scoped queries)")
    if iface:
        for sec in re.split(r"\n(?=resolver #\d+)", scoped_part):
            if re.search(rf"if_index\s*:\s*\d+\s*\({re.escape(iface)}\)", sec):
                ns = re.findall(r"nameserver\[\d+\]\s*:\s*(\S+)", sec)
                if ns:
                    return ns
    for sec in re.split(r"\n(?=resolver #\d+)", main):
        ns = re.findall(r"nameserver\[\d+\]\s*:\s*(\S+)", sec)
        if ns:
            return ns
    return []


def dns_servers(iface: str | None) -> tuple[list[str], str]:
    out = run_cmd(["scutil", "--dns"], timeout=3) or ""
    dns = parse_scutil_dns(out, iface)
    return dns, "zdroj: scutil --dns" if dns else "DNS nezjištěno"


def wifi_info(iface: str) -> dict:
    info: dict = {"ssid": None, "signal_pct": None, "signal_dbm": None, "rate_mbps": None, "note": ""}
    out = run_cmd(["ipconfig", "getsummary", iface], timeout=3) or ""
    m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
    if m:
        ssid = m.group(1).strip()
        if "redacted" in ssid.lower():
            info["note"] = "macOS skrývá SSID – udělte aplikaci oprávnění k poloze"
        else:
            info["ssid"] = ssid
    return info


def adapter_details() -> dict[str, dict]:
    res = {}
    out = run_cmd(["networksetup", "-listallhardwareports"], timeout=3) or ""
    for port, dev in re.findall(r"Hardware Port:\s*(.+)\nDevice:\s*(\S+)", out):
        pl = port.lower()
        kind = "wifi" if "wi-fi" in pl or "airport" in pl else "ethernet" if "ethernet" in pl or "lan" in pl \
            else "other"
        res[dev] = {"kind": kind, "display_name": f"{dev} ({port})"}
    return res


def autostart_enabled() -> bool:
    return LAUNCH_AGENT.exists()


def set_autostart(enabled: bool, command: list[str]) -> None:
    if not enabled:
        LAUNCH_AGENT.unlink(missing_ok=True)
        return
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    with open(LAUNCH_AGENT, "wb") as f:
        plistlib.dump({"Label": "eu.zelina.netmonitor", "ProgramArguments": command,
                       "RunAtLoad": True, "ProcessType": "Interactive"}, f)


# --- nastavení IP adresy (networksetup, práva správce přes osascript) ---------

def _service_for(iface: str) -> str | None:
    out = run_cmd(["networksetup", "-listallhardwareports"], timeout=3) or ""
    for port, dev in re.findall(r"Hardware Port:\s*(.+)\nDevice:\s*(\S+)", out):
        if dev == iface:
            return port.strip()
    return None


def dhcp_enabled(iface: str) -> bool | None:
    svc = _service_for(iface)
    if not svc:
        return None
    out = run_cmd(["networksetup", "-getinfo", svc], timeout=3) or ""
    if "DHCP Configuration" in out:
        return True
    if "Manual Configuration" in out:
        return False
    return None


def ip_config_details(iface: str) -> dict:
    """Pouze údaje vybrané služby; globální resolver není DNS jiné karty."""
    svc = _service_for(iface)
    if not svc:
        return {}
    out = run_cmd(["networksetup", "-getinfo", svc], timeout=3) or ""
    state: dict = {"dhcp": True if "DHCP Configuration" in out else
                   False if "Manual Configuration" in out else None}
    gateway = re.search(r"^Router:\s*(.*?)$", out, re.M)
    if gateway:
        value = gateway.group(1).strip()
        state["gateway"] = "" if value in ("none", "0.0.0.0", "(null)") else value
    dns_out = run_cmd(["networksetup", "-getdnsservers", svc], timeout=3)
    if dns_out and "There aren't any DNS Servers set on" in dns_out:
        state["dns_dhcp"] = True
        scoped = (run_cmd(["scutil", "--dns"], timeout=3) or "").partition(
            "DNS configuration (for scoped queries)")[2]
        for sec in re.split(r"\n(?=resolver #\d+)", scoped):
            if re.search(rf"if_index\s*:\s*\d+\s*\({re.escape(iface)}\)", sec):
                state["dns"] = tuple(re.findall(r"nameserver\[\d+\]\s*:\s*(\S+)", sec))
                break
    elif dns_out:
        from ipaddress import ip_address
        try:
            servers = [ip_address(a.strip()) for a in dns_out.splitlines() if a.strip()]
            state["dns"] = tuple(str(a) for a in servers)
            state["dns_dhcp"] = False
        except ValueError:
            pass
    return state


def apply_ip(s) -> tuple[bool, str]:
    import subprocess
    from ..core.ipconfig import applescript_admin, networksetup_args
    svc = _service_for(s.iface)
    if not svc:
        return False, f"Pro rozhraní {s.iface} nebyla nalezena síťová služba (networksetup)."
    script = applescript_admin(networksetup_args(s, svc))
    try:
        p = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    out = (p.stdout + p.stderr).decode("utf-8", "replace").strip()
    if p.returncode != 0 and "-128" in out:
        return False, "Zrušeno – změna vyžaduje heslo správce."
    return p.returncode == 0, out or "Hotovo"
