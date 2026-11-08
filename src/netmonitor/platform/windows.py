"""Windows: GetAdaptersAddresses (brána, DNS, typ, rychlost), netsh wlan (SSID), autostart HKCU\\Run,
režim widgetu na ploše (HWND_BOTTOM).

Pozn.: od Windows 11 24H2 vyžaduje čtení SSID (netsh wlan / WLAN API) povolené
služby polohy pro desktopové aplikace. Bez nich se zobrazí SSID = N/A a v tooltipu důvod.
"""
from __future__ import annotations

import ctypes
import logging
import re
import socket
import sys

from ..utils.proc import run_cmd

log = logging.getLogger(__name__)

AF_UNSPEC, AF_INET, AF_INET6_WIN = 0, 2, 23
GAA_FLAG_SKIP_ANYCAST = 0x2
GAA_FLAG_SKIP_MULTICAST = 0x4
GAA_FLAG_INCLUDE_GATEWAYS = 0x80
ERROR_BUFFER_OVERFLOW = 111
IF_OPER_UP = 1
IFTYPE_ETHERNET = 6
IFTYPE_PPP = 23
IFTYPE_SOFTWARE_LOOPBACK = 24
IFTYPE_PROP_VIRTUAL = 53
IFTYPE_IEEE80211 = 71
IFTYPE_TUNNEL = 131
VPN_HINTS = ("vpn", "wireguard", "wintun", "tap-windows", "openvpn", "tailscale", "zerotier", "fortinet",
             "cisco anyconnect", "globalprotect", "nordlynx", "proton")


class _AddrEntry(ctypes.Structure):
    # společná hlavička IP_ADAPTER_UNICAST/DNS_SERVER/GATEWAY_ADDRESS (x64 i x86 díky c_void_p)
    _fields_ = [("Length", ctypes.c_uint32), ("Flags", ctypes.c_uint32), ("Next", ctypes.c_void_p),
                ("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int32)]


class _Adapter(ctypes.Structure):
    # IP_ADAPTER_ADDRESSES_LH – zkrácená definice do Ipv6Metric (zbytek nepotřebujeme,
    # čteme přímo z bufferu, který vrátí API, takže zkrácení je bezpečné)
    _fields_ = [
        ("Length", ctypes.c_uint32), ("IfIndex", ctypes.c_uint32),
        ("Next", ctypes.c_void_p),
        ("AdapterName", ctypes.c_void_p),
        ("FirstUnicastAddress", ctypes.c_void_p),
        ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.c_void_p),
        ("DnsSuffix", ctypes.c_void_p),
        ("Description", ctypes.c_void_p),
        ("FriendlyName", ctypes.c_void_p),
        ("PhysicalAddress", ctypes.c_ubyte * 8),
        ("PhysicalAddressLength", ctypes.c_uint32),
        ("Flags", ctypes.c_uint32),
        ("Mtu", ctypes.c_uint32),
        ("IfType", ctypes.c_uint32),
        ("OperStatus", ctypes.c_int32),
        ("Ipv6IfIndex", ctypes.c_uint32),
        ("ZoneIndices", ctypes.c_uint32 * 16),
        ("FirstPrefix", ctypes.c_void_p),
        ("TransmitLinkSpeed", ctypes.c_uint64),
        ("ReceiveLinkSpeed", ctypes.c_uint64),
        ("FirstWinsServerAddress", ctypes.c_void_p),
        ("FirstGatewayAddress", ctypes.c_void_p),
        ("Ipv4Metric", ctypes.c_uint32),
        ("Ipv6Metric", ctypes.c_uint32),
    ]


def _sockaddr_to_ip(ptr: int, length: int) -> str | None:
    if not ptr or length < 8:
        return None
    raw = ctypes.string_at(ptr, length)
    family = int.from_bytes(raw[0:2], "little")
    if family == AF_INET and length >= 8:
        return socket.inet_ntop(socket.AF_INET, raw[4:8])
    if family == AF_INET6_WIN and length >= 24:
        return socket.inet_ntop(socket.AF_INET6, raw[8:24])
    return None


def _walk(first: int) -> list[str]:
    out, p, guard = [], first, 0
    while p and guard < 64:
        e = _AddrEntry.from_address(p)
        ip = _sockaddr_to_ip(e.lpSockaddr, e.iSockaddrLength)
        if ip:
            out.append(ip)
        p = e.Next
        guard += 1
    return out


def _wstr(ptr: int) -> str:
    return ctypes.wstring_at(ptr) if ptr else ""


def classify(if_type: int, description: str) -> str:
    d = description.lower()
    if if_type == IFTYPE_SOFTWARE_LOOPBACK:
        return "loopback"
    if any(h in d for h in VPN_HINTS) or if_type in (IFTYPE_PPP, IFTYPE_TUNNEL, IFTYPE_PROP_VIRTUAL):
        return "vpn"
    if if_type == IFTYPE_IEEE80211:
        return "wifi"
    if if_type == IFTYPE_ETHERNET:
        return "ethernet"
    return "other"


def adapter_details() -> dict[str, dict]:
    """Klíč = FriendlyName (shodné s názvy rozhraní v psutil na Windows)."""
    if sys.platform != "win32":
        return {}
    try:
        fn = ctypes.windll.iphlpapi.GetAdaptersAddresses
        fn.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.POINTER(ctypes.c_uint32)]
        fn.restype = ctypes.c_uint32
        size = ctypes.c_uint32(32 * 1024)
        flags = GAA_FLAG_INCLUDE_GATEWAYS | GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST
        for _ in range(4):
            buf = ctypes.create_string_buffer(size.value)
            rc = fn(AF_UNSPEC, flags, None, buf, ctypes.byref(size))
            if rc == ERROR_BUFFER_OVERFLOW:
                continue
            if rc != 0:
                log.warning("GetAdaptersAddresses vrátilo %s", rc)
                return {}
            break
        else:
            return {}
        res: dict[str, dict] = {}
        p, guard = ctypes.addressof(buf), 0
        while p and guard < 256:
            a = _Adapter.from_address(p)
            name = _wstr(a.FriendlyName)
            desc = _wstr(a.Description)
            speed = int(a.ReceiveLinkSpeed or a.TransmitLinkSpeed or 0)
            if speed == 0xFFFFFFFFFFFFFFFF:
                speed = 0
            res[name] = {
                "display_name": name,
                "description": desc,
                "kind": classify(a.IfType, desc),
                "up": a.OperStatus == IF_OPER_UP,
                "ips": _walk(a.FirstUnicastAddress),
                "dns": _walk(a.FirstDnsServerAddress),
                "gateways": _walk(a.FirstGatewayAddress),
                "speed_mbps": (speed // 1_000_000) or None,
                "metric": a.Ipv4Metric,
                "dhcp": bool(a.Flags & IP_ADAPTER_DHCP_ENABLED),
                "if_index": a.IfIndex,
                "guid": ctypes.string_at(a.AdapterName).decode("ascii") if a.AdapterName else "",
            }
            p = a.Next
            guard += 1
        return res
    except Exception:  # noqa: BLE001
        log.exception("GetAdaptersAddresses selhalo – použiji fallback")
        return _adapter_details_powershell()


def _adapter_details_powershell() -> dict[str, dict]:
    import json
    out = run_cmd(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                   "Get-NetIPConfiguration | ForEach-Object { [pscustomobject]@{"
                   "n=$_.InterfaceAlias; d=$_.InterfaceDescription;"
                   "g=@($_.IPv4DefaultGateway | ForEach-Object NextHop);"
                   "s=@($_.DNSServer | Where-Object AddressFamily -eq 2 | ForEach-Object ServerAddresses)} }"
                   " | ConvertTo-Json -Compress"], timeout=8, c_locale=False, encoding="utf-8")
    res: dict[str, dict] = {}
    if not out:
        return res
    try:
        data = json.loads(out)
        for it in data if isinstance(data, list) else [data]:
            res[it.get("n", "")] = {"display_name": it.get("n", ""), "description": it.get("d", ""),
                                    "kind": classify(0, it.get("d", "")), "dns": it.get("s") or [],
                                    "gateways": it.get("g") or [], "up": True}
    except (ValueError, AttributeError):
        pass
    return res


def default_gateway(iface: str | None = None) -> tuple[str | None, str | None]:
    best = None
    for name, a in adapter_details().items():
        gws = [g for g in a.get("gateways", []) if ":" not in g]
        if not gws or not a.get("up", True) or (iface and name != iface):
            continue
        if best is None or a.get("metric", 0) < best[2]:
            best = (gws[0], name, a.get("metric", 0))
    return (best[0], best[1]) if best else (None, None)


def dns_servers(iface: str | None) -> tuple[list[str], str]:
    d = adapter_details().get(iface or "", {})
    dns = [x for x in d.get("dns", []) if not x.lower().startswith("fec0:")]  # site-local placeholdery
    return dns, f"zdroj: GetAdaptersAddresses ({iface})" if dns else "DNS nezjištěno"


_KV = re.compile(r"^\s*([^:]+?)\s*:\s*(.*?)\s*$")


def parse_netsh_wlan(text: str) -> list[dict]:
    """Parsuje 'netsh wlan show interfaces' – klíče jsou lokalizované, proto heuristika."""
    blocks: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        m = _KV.match(line)
        if not m:
            continue
        k, v = m.group(1).strip().lower(), m.group(2)
        if k in ("name", "název", "jméno", "nom"):
            cur = {"name": v}
            blocks.append(cur)
            continue
        if cur is None:
            continue
        if k == "ssid":
            cur["ssid"] = v
        elif k.startswith(("signal", "signál", "signal-")) and "%" in v:
            mm = re.search(r"(\d+)\s*%", v)
            cur["signal_pct"] = int(mm.group(1)) if mm else None
        elif ("receive rate" in k or "rychlost příjmu" in k or "empfangsrate" in k):
            mm = re.search(r"([\d.,]+)", v)
            if mm:
                cur["rate_mbps"] = float(mm.group(1).replace(",", "."))
        elif k in ("state", "stav", "status", "zustand"):
            cur["state"] = v
    return blocks


def wifi_info(iface: str) -> dict:
    info: dict = {"ssid": None, "signal_pct": None, "signal_dbm": None, "rate_mbps": None, "note": ""}
    out = run_cmd(["netsh", "wlan", "show", "interfaces"], timeout=4, c_locale=False)
    if not out:
        info["note"] = "netsh wlan nedostupné"
        return info
    low = out.lower()
    if "location" in low and ("permission" in low or "access" in low) or "polohy" in low or "umístění" in low:
        info["note"] = ("Windows 11 24H2+: pro zjištění SSID povolte Nastavení → Ochrana osobních údajů → "
                        "Poloha → 'Povolit desktopovým aplikacím přístup k poloze'")
    for b in parse_netsh_wlan(out):
        if b.get("name", "").lower() == iface.lower() or len(parse_netsh_wlan(out)) == 1:
            info.update({k: b.get(k) for k in ("ssid", "signal_pct", "rate_mbps") if b.get(k) is not None})
            break
    return info


# --- autostart: HKCU\Software\Microsoft\Windows\CurrentVersion\Run -----------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "NetMonitor"


def autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool, command: list[str]) -> None:
    import subprocess
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, subprocess.list2cmdline(command))
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass


# --- okno -----------------------------------------------------------------

HWND_BOTTOM = 1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10


def send_to_bottom(hwnd: int) -> None:
    """Desktop režim: okno pod všemi běžnými okny (jako Rainmeter 'Stay bottommost')."""
    try:
        ctypes.windll.user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_BOTTOM), 0, 0, 0, 0,
                                          SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
    except Exception:  # noqa: BLE001
        log.debug("SetWindowPos selhalo", exc_info=True)


# --- nastavení IP adresy (vyžaduje práva správce – UAC) ----------------------

IP_ADAPTER_DHCP_ENABLED = 0x4
ERROR_CANCELLED = 1223


def dhcp_enabled(iface: str) -> bool | None:
    d = adapter_details().get(iface)
    return None if d is None else d.get("dhcp")


def ip_config_details(iface: str) -> dict:
    """GAA poskytne údaje karty, režim DNS pochází z jejího TCP/IP klíče registru."""
    d = adapter_details().get(iface, {})
    state: dict = {"dhcp": d.get("dhcp"), "gateway": None, "dns": None}
    has_ipv4 = any(":" not in ip for ip in d.get("ips", []))
    if d.get("up") and has_ipv4 and "gateways" in d:
        state["gateway"] = next((g for g in d["gateways"] if ":" not in g), "")
    if d.get("up") and has_ipv4 and "dns" in d:
        state["dns"] = tuple(g for g in d["dns"] if ":" not in g)
    if d.get("guid"):
        try:
            import importlib
            winreg = importlib.import_module("winreg")
            path = "SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces\\" + d["guid"]
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                try:
                    servers, _ = winreg.QueryValueEx(key, "NameServer")
                except FileNotFoundError:
                    servers = ""
                state["dns_dhcp"] = not bool(servers.strip())
        except OSError:
            pass
    return state


def _shellexecute_struct():
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        # DWORD/ULONG jsou na Windows 32bitové – c_uint32 zaručí správné rozložení (sizeof = 112 na x64)
        _fields_ = [("cbSize", ctypes.c_uint32), ("fMask", ctypes.c_uint32), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", ctypes.c_uint32), ("hIconOrMonitor", wintypes.HANDLE),
                    ("hProcess", wintypes.HANDLE)]
    return SHELLEXECUTEINFOW


def run_elevated_powershell(script: str, timeout_s: float = 90.0) -> tuple[int | None, str]:
    """Spustí PowerShell se zvýšenými právy (UAC dotaz). Vrací (exit kód | None při zrušení, chyba).

    Skript se předává jako -EncodedCommand (UTF-16LE base64) – nic spustitelného se neukládá na disk,
    takže ho nelze mezi zápisem a spuštěním podvrhnout.
    """
    import base64
    from ctypes import wintypes
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    SEI = _shellexecute_struct()
    info = SEI()
    info.cbSize = ctypes.sizeof(SEI)
    info.fMask = 0x40 | 0x100 | 0x400        # NOCLOSEPROCESS | NOASYNC | FLAG_NO_UI
    info.lpVerb = "runas"
    info.lpFile = "powershell.exe"
    info.lpParameters = f"-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand {enc}"
    info.nShow = 0
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SEI)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            return None, "Zrušeno – změna vyžaduje potvrzení správce (UAC)."
        return None, f"Spuštění se zvýšenými právy selhalo (chyba {err})."
    try:
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        if kernel32.WaitForSingleObject(info.hProcess, int(timeout_s * 1000)) != 0:
            return None, "Vypršel časový limit nastavení sítě."
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value), ""
    finally:
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(info.hProcess)


def apply_ip(s) -> tuple[bool, str]:
    from ..core.ipconfig import windows_script
    from ..utils.paths import data_dir
    d = data_dir() / "ipconfig"
    d.mkdir(parents=True, exist_ok=True)
    log_path = d / "last-apply.log"
    log_path.unlink(missing_ok=True)
    code, err = run_elevated_powershell(windows_script(s, str(log_path)))
    try:
        out = log_path.read_text(encoding="utf-8-sig", errors="replace").strip()
    except OSError:
        out = ""
    if code is None:
        return False, err
    return code == 0, out or f"netsh skončil s kódem {code}"
