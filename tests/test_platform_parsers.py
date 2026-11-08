import json
import subprocess

import pytest

from netmonitor.platform import linux, macos, windows
from netmonitor.utils import proc


def test_proc_net_route(tmp_path):
    f = tmp_path / "route"
    f.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "wlan0\t00000000\tFECAA8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
        "eth0\t00000000\t0100000A\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
        "wlan0\t00CAA8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0\n")
    assert linux.default_gateway(path=str(f)) == ("10.0.0.1", "eth0")          # nejnižší metrika
    assert linux.default_gateway("wlan0", path=str(f)) == ("192.168.202.254", "wlan0")


def test_resolv_conf_stub():
    assert linux.parse_resolv_conf("# x\nnameserver 127.0.0.53\noptions edns0\n") == ["127.0.0.53"]
    assert linux.parse_resolv_conf("nameserver 93.93.32.32\nnameserver fe80::1%eth0\n") == ["93.93.32.32", "fe80::1"]


def test_nmcli_escape_split():
    assert linux._nm_unescape_split(r"yes:Nordic\:Telecom 5G:72:270 Mbit/s:wlan0") == \
        ["yes", "Nordic:Telecom 5G", "72", "270 Mbit/s", "wlan0"]


NETSH_CZ = """
V systému je 1 rozhraní:

    Název                  : Wi-Fi
    Popis                  : Intel(R) Wi-Fi 6E AX211 160MHz
    GUID                   : 1234
    Fyzická adresa         : aa:bb:cc:dd:ee:ff
    Stav                   : připojeno
    SSID                   : Nordic Telecom 5G
    BSSID                  : 11:22:33:44:55:66
    Typ sítě               : Infrastruktura
    Rychlost příjmu (Mb/s) : 200
    Rychlost přenosu (Mb/s): 200
    Signál                 : 84%
"""
NETSH_EN = """
There is 1 interface on the system:

    Name                   : WLAN
    SSID                   : Home
    BSSID                  : 11:22:33:44:55:66
    Receive rate (Mbps)    : 866.7
    Signal                 : 99%
"""


def test_netsh_parsing_cz_en():
    b = windows.parse_netsh_wlan(NETSH_CZ)[0]
    assert (b["name"], b["ssid"], b["signal_pct"], b["rate_mbps"]) == ("Wi-Fi", "Nordic Telecom 5G", 84, 200.0)
    b = windows.parse_netsh_wlan(NETSH_EN)[0]
    assert (b["name"], b["ssid"], b["signal_pct"], b["rate_mbps"]) == ("WLAN", "Home", 99, 866.7)


def test_windows_struct_layout_x64():
    import ctypes
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        return
    # offsety dle IP_ADAPTER_ADDRESSES_LH (iptypes.h) pro x64
    assert windows._Adapter.FriendlyName.offset == 72
    assert windows._Adapter.TransmitLinkSpeed.offset == 184
    assert windows._Adapter.FirstGatewayAddress.offset == 208
    assert windows._AddrEntry.lpSockaddr.offset == 16


def test_windows_classify():
    assert windows.classify(71, "Intel Wi-Fi") == "wifi"
    assert windows.classify(6, "Realtek PCIe GbE") == "ethernet"
    assert windows.classify(53, "WireGuard Tunnel") == "vpn"
    assert windows.classify(6, "TAP-Windows Adapter V9") == "vpn"


@pytest.mark.parametrize("multiple", [False, True], ids=["object", "array"])
def test_windows_powershell_utf8_adapter_names(monkeypatch, multiple):
    """Regrese L22: PowerShell musí poslat UTF-8 a zachovat diakritiku názvu i popisu."""
    name = "Připojení k místní síti"
    adapter = {"n": name, "d": "Síťový adaptér", "g": ["192.0.2.1"], "s": ["192.0.2.53"]}
    data = [adapter, {"n": "Záložní síť", "d": "Virtuální adaptér", "g": [], "s": []}] if multiple else adapter
    output = (json.dumps(data, ensure_ascii=False) + "\r\n").encode("utf-8")
    commands = []

    def fake_run(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr=b"")

    monkeypatch.setattr(proc.shutil, "which", lambda _: "powershell")
    monkeypatch.setattr(proc.subprocess, "run", fake_run)
    result = windows._adapter_details_powershell()

    assert result[name] == {"display_name": name, "description": "Síťový adaptér", "kind": "other",
                            "gateways": ["192.0.2.1"], "dns": ["192.0.2.53"], "up": True}
    if multiple:
        assert result["Záložní síť"]["gateways"] == []
        assert result["Záložní síť"]["dns"] == []
    assert len(commands) == 1
    assert commands[0][:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert commands[0][4].startswith("[Console]::OutputEncoding=[Text.Encoding]::UTF8; ")


SCUTIL = """DNS configuration

resolver #1
  nameserver[0] : 93.93.32.32
  nameserver[1] : 93.93.33.33
  if_index : 6 (en0)

DNS configuration (for scoped queries)

resolver #1
  nameserver[0] : 10.8.0.1
  if_index : 20 (utun3)

resolver #2
  nameserver[0] : 93.93.32.32
  if_index : 6 (en0)
"""


def test_scutil_dns():
    assert macos.parse_scutil_dns(SCUTIL, "utun3") == ["10.8.0.1"]
    assert macos.parse_scutil_dns(SCUTIL, None) == ["93.93.32.32", "93.93.33.33"]
    assert macos.parse_scutil_dns(SCUTIL, "en9") == ["93.93.32.32", "93.93.33.33"]


def test_psutil_speed_overflow_is_unknown():
    from netmonitor.core.network_info import _sane_speed
    assert _sane_speed(4294) is None        # 10GbE na Windows přes psutil
    assert _sane_speed(0) is None
    assert _sane_speed(1000) == 1000


def test_speed_format():
    from netmonitor.gui.network_panel import fmt_speed
    assert fmt_speed(10000) == "10 Gb/s"
    assert fmt_speed(2500) == "2.5 Gb/s"
    assert fmt_speed(200) == "200 Mb/s"
    assert fmt_speed(None) == "N/A"
