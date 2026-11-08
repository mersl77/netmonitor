import shutil
import subprocess
import sys
from unittest.mock import Mock

import pytest

from netmonitor.core.config import AppConfig
from netmonitor.gui.controller import Controller
from netmonitor.platform import linux, macos, windows


@pytest.fixture
def appimage_runtime(tmp_path, monkeypatch):
    appimage = tmp_path / "Net Monitor.AppImage"
    appimage.touch()
    appdir = tmp_path / "mount"
    executable = appdir / "usr" / "bin" / "NetMonitor"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setenv("APPIMAGE", str(appimage))
    monkeypatch.setenv("APPDIR", str(appdir))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    return appimage, appdir, executable


@pytest.mark.parametrize("minimized", [False, True])
def test_autostart_appimage_path(tmp_path, monkeypatch, appimage_runtime, minimized):
    """Regrese L23: skutečný soubor AppImage s mezerou funguje i ve Windows test runneru."""
    appimage, _, _ = appimage_runtime
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = AppConfig()
    cfg.general.start_minimized = minimized
    command = Controller.autostart_command(Mock(cfg=cfg))
    assert command == [str(appimage)] + (["--minimized"] if minimized else [])
    linux.set_autostart(True, command)
    content = (tmp_path / "autostart" / "netmonitor.desktop").read_text(encoding="utf-8")
    escaped_path = str(appimage).replace("\\", r"\\\\").replace("%", "%%")
    expected = f'Exec="{escaped_path}"' + (" --minimized" if minimized else "")
    assert expected in content.splitlines()


@pytest.mark.parametrize("appimage", [None, "", "missing", "directory"])
@pytest.mark.parametrize("frozen", [False, True])
def test_autostart_appimage_fallback(tmp_path, monkeypatch, appimage_runtime, appimage, frozen):
    """Regrese L23: chybějící AppImage ani adresář nesmí přepsat původní příkaz."""
    _, _, executable = appimage_runtime
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    if appimage is None:
        monkeypatch.delenv("APPIMAGE", raising=False)
    else:
        monkeypatch.setenv("APPIMAGE", str(tmp_path / appimage) if appimage else "")
    (tmp_path / "directory").mkdir()
    cfg = AppConfig()
    cfg.general.start_minimized = True
    assert Controller.autostart_command(Mock(cfg=cfg)) == [str(executable)] + (
        ["--minimized"] if frozen else ["-m", "netmonitor"])


@pytest.mark.parametrize("invalid", ["not_frozen", "relative_image", "unset_appdir", "empty_appdir",
                                     "outside_appdir", "sibling_appdir", "traversal"])
@pytest.mark.parametrize("minimized", [False, True])
def test_autostart_inherited_appimage(tmp_path, monkeypatch, appimage_runtime, invalid, minimized):
    """Regrese L23/B2/M1: zděděná APPIMAGE smí být použita jen pro vlastní frozen proces uvnitř APPDIR."""
    appimage, appdir, executable = appimage_runtime
    if invalid == "not_frozen":
        monkeypatch.setattr(sys, "frozen", False)
    elif invalid == "relative_image":
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("APPIMAGE", appimage.name)
    elif invalid == "unset_appdir":
        monkeypatch.delenv("APPDIR")
    elif invalid == "empty_appdir":
        monkeypatch.setenv("APPDIR", "")
    else:
        executable = {
            "outside_appdir": tmp_path / "NetMonitor",
            "sibling_appdir": tmp_path / "mount-other" / "NetMonitor",
            "traversal": appdir / ".." / "NetMonitor",
        }[invalid]
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.touch()
        monkeypatch.setattr(sys, "executable", str(executable))
    cfg = AppConfig()
    cfg.general.start_minimized = minimized
    if invalid == "not_frozen":
        expected = [str(executable), "-m", "netmonitor"]
    else:
        expected = [str(executable)] + (["--minimized"] if minimized else [])
    assert Controller.autostart_command(Mock(cfg=cfg)) == expected


@pytest.mark.parametrize("platform", ["win32", "darwin"])
@pytest.mark.parametrize("frozen", [False, True])
def test_autostart_appimage_ignored_on_other_platforms(tmp_path, monkeypatch, platform, frozen):
    """Regrese L23: APPIMAGE nesmí ovlivnit autostart Windows/macOS."""
    appimage = tmp_path / "NetMonitor.AppImage"
    appimage.touch()
    monkeypatch.setenv("APPIMAGE", str(appimage))
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/python.exe")
    cfg = AppConfig()
    cfg.general.start_minimized = False
    expected = ["/opt/python.exe"] if frozen else [
        "/opt/pythonw.exe" if platform == "win32" else "/opt/python.exe", "-m", "netmonitor"]
    assert Controller.autostart_command(Mock(cfg=cfg)) == expected


@pytest.mark.parametrize(("argument", "expected"), [
    ("--minimized", "--minimized"),
    ("", '""'),
    ("/opt/apps/Net Monitor.AppImage", '"/opt/apps/Net Monitor.AppImage"'),
    ("a$b", r'"a\\$b"'),
    ('a"b', r'"a\\"b"'),
    ("a`b", r'"a\\`b"'),
    (r"a\b", r'"a\\\\b"'),
    ("100%", "100%%"),
    ("%f %U", '"%%f %%U"'),
    ("a\tb", r'"a\tb"'),
    ("a\nb", r'"a\nb"'),
    ("a\rb", r'"a\rb"'),
])
def test_desktop_exec_quote(argument, expected):
    """Regrese L23: Exec má vlastní quoting i escapování hodnoty, nikoli syntaxi shellu."""
    assert linux._quote_exec_arg(argument) == expected


@pytest.mark.parametrize("reserved", "'><~|&;*?#()")
def test_desktop_exec_quote_reserved(reserved):
    """Regrese L23: všechny další rezervované znaky vyžadují dvojité uvozovky."""
    assert linux._quote_exec_arg(f"a{reserved}b") == f'"a{reserved}b"'


def test_autostart_desktop_validation(tmp_path, monkeypatch):
    """Regrese L23: vygenerovaný autostart je platný Desktop Entry i se speciálními znaky."""
    validator = shutil.which("desktop-file-validate")
    if not validator:
        pytest.skip("desktop-file-validate není dostupný")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    linux.set_autostart(True, ["/opt/apps/Net Monitor.AppImage", "--minimized",
                               'mezera $ " ` \\ %f', "", "tab\tnewline\nreturn\r"])
    desktop = tmp_path / "autostart" / "netmonitor.desktop"
    result = subprocess.run([validator, str(desktop)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stdout and not result.stderr


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
