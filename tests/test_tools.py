import socket
import threading

import pytest

from netmonitor.core import speedtest, traceroute, wol
from netmonitor.core.config import PingTarget


# ---------------------------------------------------------------- Wake-on-LAN
@pytest.mark.parametrize("txt", ["04:D9:F5:15:34:86", "04-d9-f5-15-34-86", "04d9.f515.3486", "04d9f5153486",
                                 "4:d9:f5:15:34:86"])
def test_parse_mac_formats(txt):
    assert wol.format_mac(txt) == ("04:D9:F5:15:34:86" if txt != "4:d9:f5:15:34:86" else "04:D9:F5:15:34:86")


@pytest.mark.parametrize("txt", ["", "zz:zz:zz:zz:zz:zz", "00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "04:d9:f5:15:34"])
def test_parse_mac_invalid(txt):
    with pytest.raises(ValueError):
        wol.parse_mac(txt)


def test_magic_packet_and_broadcast():
    mac = wol.parse_mac("04:D9:F5:15:34:86")
    p = wol.magic_packet(mac)
    assert len(p) == 102 and p[:6] == b"\xff" * 6 and p[6:12] == mac and p[-6:] == mac
    assert wol.directed_broadcast("192.168.14.112", "255.255.255.0") == "192.168.14.255"
    assert wol.directed_broadcast("10.0.1.5", "255.255.0.0") == "10.0.255.255"


@pytest.mark.integration
def test_send_reaches_udp_listener():
    """Odeslání na loopback-broadcast (přes extra broadcast 127.0.0.1) – ověří skutečný UDP paket."""
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        rx.bind(("127.0.0.1", 9))
    except (PermissionError, OSError):
        pytest.skip("port 9 nelze otevřít")
    rx.settimeout(2)
    try:
        sent = wol.send("04:D9:F5:15:34:86", extra_broadcast="127.0.0.1")
        assert "127.0.0.1:9" in sent
        data, _ = rx.recvfrom(200)
        assert data == wol.magic_packet(wol.parse_mac("04:D9:F5:15:34:86"))
    finally:
        rx.close()


ARP_WIN_CZ = """
Rozhraní: 192.168.14.112 --- 0x7
  Internetová adresa    Fyzická adresa        Typ
  192.168.14.1          a0-b5-49-12-34-56     dynamická
  192.168.14.10         00-11-32-aa-bb-cc     dynamická
  192.168.14.255        ff-ff-ff-ff-ff-ff     statická
"""
ARP_MAC = "? (192.168.1.20) at 4:d9:f5:15:34:86 on en0 ifscope [ethernet]"
PROC_ARP = """IP address       HW type     Flags       HW address            Mask     Device
192.168.1.1      0x1         0x2         a0:b5:49:12:34:56     *        wlan0
192.168.1.17     0x1         0x0         00:00:00:00:00:00     *        wlan0
"""


def test_arp_parsers():
    assert wol.parse_arp_output(ARP_WIN_CZ, "192.168.14.1") == "A0:B5:49:12:34:56"
    assert wol.parse_arp_output(ARP_WIN_CZ, "192.168.14.10") == "00:11:32:AA:BB:CC"
    assert wol.parse_arp_output(ARP_WIN_CZ, "192.168.14.255") is None       # broadcast se nevrací
    assert wol.parse_arp_output(ARP_WIN_CZ, "192.168.14.11") is None        # nesmí chytit .1 / .10
    assert wol.parse_arp_output(ARP_MAC, "192.168.1.20") == "04:D9:F5:15:34:86"
    assert wol.parse_arp_output(PROC_ARP, "192.168.1.1") == "A0:B5:49:12:34:56"
    assert wol.parse_arp_output(PROC_ARP, "192.168.1.17") is None            # neúplný záznam


def test_target_mac_validation_and_roundtrip():
    from netmonitor.core.config import AppConfig
    assert PingTarget(name="PC", host="192.168.1.20", mac="04:D9:F5:15:34:86").validate() == []
    assert PingTarget(name="PC", host="192.168.1.20", mac="xx").validate()
    assert PingTarget(name="PC", host="192.168.1.20", wol_broadcast="nope").validate()
    cfg = AppConfig.from_dict({"ping": {"targets": [{"name": "PC", "host": "192.168.1.20", "mac": "04:D9:F5:15:34:86"}]}})
    assert AppConfig.from_dict(cfg.to_dict()).ping.targets[0].mac == "04:D9:F5:15:34:86"


# ---------------------------------------------------------------- Traceroute
TR = """traceroute to 8.8.8.8 (8.8.8.8), 30 hops max, 60 byte packets
 1  192.168.1.1  0.512 ms  0.433 ms  0.401 ms
 2  * * *
 3  10.0.0.1  5.123 ms *  5.300 ms
 4  72.14.1.1  6.0 ms 72.14.1.2  6.2 ms  6.1 ms
 5  8.8.8.8  7.1 ms  7.0 ms  7.2 ms
 6  9.9.9.9  1.0 ms !H  * *
"""


def test_parse_traceroute():
    hops = [traceroute.parse_traceroute_line(x, "8.8.8.8") for x in TR.splitlines()]
    hops = [h for h in hops if h]
    assert [h.n for h in hops] == [1, 2, 3, 4, 5, 6]
    assert hops[0].ip == "192.168.1.1" and hops[0].rtts == [0.512, 0.433, 0.401] and hops[0].best == 0.401
    assert hops[1].ip is None and hops[1].rtts == [None, None, None] and hops[1].loss_pct == 100
    assert hops[2].rtts == [5.123, None, 5.3]
    assert hops[3].ip == "72.14.1.1" and len(hops[3].rtts) == 3
    assert hops[4].reached and not hops[3].reached
    assert hops[5].note == "!H"


TP = """ 1?: [LOCALHOST]                      pmtu 1500
 1:  192.168.1.1                                           0.512ms
 1:  192.168.1.1                                           0.411ms
 2:  no reply
 3:  8.8.8.8                                               7.300ms reached
     Resume: pmtu 1500 hops 3 back 3
"""


def test_parse_tracepath():
    hops = [h for h in (traceroute.parse_tracepath_line(x, "8.8.8.8") for x in TP.splitlines()) if h]
    assert [(h.n, h.ip) for h in hops] == [(1, "192.168.1.1"), (1, "192.168.1.1"), (2, None), (3, "8.8.8.8")]
    assert hops[-1].reached and hops[2].rtts == [None]


TRACERT_EN = """
Tracing route to 8.8.8.8 over a maximum of 30 hops

  1    <1 ms    <1 ms    <1 ms  192.168.14.1
  2     3 ms     2 ms     3 ms  100.64.0.1
  3     *        *        *     Request timed out.
  4     *       12 ms    11 ms  194.228.190.1
  5  10.20.30.40  reports: Destination host unreachable.
  6     6 ms     6 ms     6 ms  8.8.8.8

Trace complete.
"""

TRACERT_CZ = """
Výpis trasy k 8.8.8.8 s nejvýše 30 směrováními

  1    <1 ms    <1 ms    <1 ms  192.168.14.1
  2     *        *        *     Vypršel časový limit žádosti.
  3  10.20.30.40  hlásí: Cílový hostitel není dostupný.
  4     6 ms     5 ms     7 ms  8.8.8.8

Trasování bylo dokončeno.
"""


def test_parse_tracert_en():
    hops = [h for h in (traceroute.parse_tracert_line(x, "8.8.8.8") for x in TRACERT_EN.splitlines()) if h]
    assert [h.n for h in hops] == [1, 2, 3, 4, 5, 6]
    assert hops[0].ip == "192.168.14.1" and hops[0].rtts == [1.0, 1.0, 1.0]
    assert hops[2].ip is None and hops[2].rtts == [None, None, None] and hops[2].loss_pct == 100
    assert hops[3].ip == "194.228.190.1" and hops[3].rtts == [None, 12.0, 11.0]
    assert hops[4].ip == "10.20.30.40" and hops[4].note == "!H"
    assert hops[5].reached and not any(h.reached for h in hops[:5])


def test_parse_tracert_cz():
    hops = [h for h in (traceroute.parse_tracert_line(x, "8.8.8.8") for x in TRACERT_CZ.splitlines()) if h]
    assert [h.n for h in hops] == [1, 2, 3, 4]
    assert hops[1].ip is None and hops[2].note == "!H" and hops[3].reached and hops[3].rtts == [6.0, 5.0, 7.0]


def test_tracert_command(monkeypatch, tmp_path):
    exe = tmp_path / "tracert.exe"
    exe.write_text("")
    monkeypatch.setattr(traceroute.sys, "platform", "win32")
    monkeypatch.setattr(traceroute.shutil, "which", lambda n: str(exe) if n == "tracert" else None)
    args, kind = traceroute.system_command("8.8.8.8", 30, 3, 1.0)
    assert kind == "tracert" and args[1:] == ["-d", "-4", "-h", "30", "-w", "1000", "8.8.8.8"]


@pytest.mark.integration
def test_traceroute_live_localhost():
    got = []
    ip, err = traceroute.run("127.0.0.1", got.append, max_hops=3, probes=1, timeout=1)
    if not got and "Není nainstalován" in err:
        pytest.skip(err)
    assert ip == "127.0.0.1" and got and got[0].ip == "127.0.0.1"


def test_traceroute_unresolvable():
    ip, err = traceroute.run("does-not-exist.invalid", lambda h: None)
    assert ip is None and err


# ---------------------------------------------------------------- Test rychlosti
def test_meter_excludes_warmup():
    import time
    m = speedtest._Meter(warmup=0.05)
    m.add(10_000_000)                      # náběh – nepočítá se
    time.sleep(0.06)
    m.add(1)
    base = m.total
    time.sleep(0.1)
    m.add(1_250_000)                       # 10 Mbit za ~0.1 s => ~100 Mb/s
    r = m.rate()
    assert 40 < r < 130, r
    assert m.base_bytes == base


def test_speed_format_and_mbps():
    from netmonitor.gui.tools_dialog import _fmt_speed
    assert speedtest.mbps(125_000_000, 1.0) == 1000
    assert _fmt_speed(2610.8) == "2.61 Gb/s" and _fmt_speed(478.5) == "478 Mb/s" and _fmt_speed(5.25) == "5.2 Mb/s"
    assert _fmt_speed(None) == "–"


def test_run_streams_aggregates_threads():
    import time

    def worker(meter, stop):
        while not stop.is_set():
            meter.add(100_000)
            time.sleep(0.01)
    seen = []
    rate = speedtest._run_streams(worker, 3, 0.6, "download", lambda ph, f, v: seen.append(ph), lambda: False)
    assert rate and rate > 1 and "download" in seen   # jen součet proudů; časování runnerů kolísá
    assert not [t for t in threading.enumerate() if t.name.startswith("speed-")]
