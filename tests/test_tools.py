import os
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, call

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


def _local_trace(monkeypatch, code, kind="traceroute") -> list[subprocess.Popen]:
    """Pouze lokální Python child, nikdy systémový traceroute ani síť."""
    processes = []
    popen = subprocess.Popen

    def spawn(*args, **kwargs):
        proc = popen(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(traceroute, "system_command", lambda *args: ([sys.executable, "-c", code], kind))
    monkeypatch.setattr(traceroute.subprocess, "Popen", spawn)
    return processes


@pytest.mark.skipif(sys.platform == "win32", reason="killpg je POSIX scénář")
def test_traceroute_stop_finished_process_skips_killpg(monkeypatch):
    """Regrese L32 review 3: sklizený proces už nesmí signalizovat potenciálně znovu použité PID."""
    with subprocess.Popen([sys.executable, "-c", "pass"]) as proc:
        assert proc.wait(timeout=3) == 0
        killpg = Mock()
        wait = Mock(wraps=proc.wait)
        monkeypatch.setattr(traceroute.os, "killpg", killpg)
        monkeypatch.setattr(proc, "wait", wait)
        traceroute._stop_process(proc)
        killpg.assert_not_called()
        wait.assert_called_once_with()


@pytest.mark.skipif(sys.platform == "win32", reason="killpg je POSIX scénář")
@pytest.mark.parametrize("failed_signal", ["SIGTERM", "SIGKILL"])
def test_traceroute_stop_group_error_falls_back_to_kill(monkeypatch, failed_signal):
    """Regrese L32 review 3: chyba oprávnění killpg použije kill rodiče a vždy wait."""
    proc = Mock(spec=subprocess.Popen, returncode=None, pid=12345)

    def killpg(pid, sig):
        if sig == getattr(signal, failed_signal):
            raise PermissionError("group signal denied")

    monkeypatch.setattr(traceroute.os, "killpg", killpg)
    traceroute._stop_process(proc)
    proc.kill.assert_called_once_with()
    assert proc.wait.call_args_list == [call(timeout=1), call()]


@pytest.mark.skipif(sys.platform == "win32", reason="killpg je POSIX scénář")
def test_traceroute_stop_missing_group_still_waits(monkeypatch):
    """Regrese L32 review 3: zaniklá skupina nepotřebuje fallback kill, wait se ale provede."""
    proc = Mock(spec=subprocess.Popen, returncode=None, pid=12345)
    monkeypatch.setattr(traceroute.os, "killpg", Mock(side_effect=ProcessLookupError))
    traceroute._stop_process(proc)
    proc.kill.assert_not_called()
    assert proc.wait.call_args_list == [call(timeout=1), call()]


@pytest.mark.skipif(sys.platform == "win32", reason="killpg je POSIX scénář")
def test_traceroute_stop_failed_fallback_still_waits(monkeypatch):
    """Regrese L32 review 3: i selhání fallback kill musí projít závěrečným wait."""
    proc = Mock(spec=subprocess.Popen, returncode=None, pid=12345)
    proc.kill.side_effect = OSError("kill failed")
    monkeypatch.setattr(traceroute.os, "killpg", Mock(side_effect=PermissionError))
    with pytest.raises(OSError, match="kill failed"):
        traceroute._stop_process(proc)
    proc.wait.assert_called_once_with()


def test_traceroute_stop_error_still_joins_readers(monkeypatch):
    """Regrese L32 review 3: výjimka úklidu procesu nesmí přeskočit join obou čteček."""
    processes = _local_trace(monkeypatch, "print(' 1  192.0.2.1  1 ms')")
    stop = traceroute._stop_process
    join = threading.Thread.join
    joined = []

    def fail_after_stop(proc):
        stop(proc)
        raise OSError("cleanup failed")

    def record_join(thread, timeout=None):
        joined.append((thread.name, timeout))
        return join(thread, timeout)

    monkeypatch.setattr(traceroute, "_stop_process", fail_after_stop)
    monkeypatch.setattr(traceroute.threading.Thread, "join", record_join)
    with pytest.raises(OSError, match="cleanup failed"):
        traceroute.run("192.0.2.1", lambda h: None)
    assert processes[0].poll() is not None
    assert joined == [("traceroute-stdout", 0.2), ("traceroute-stderr", 0.2)]
    assert not [t for t in threading.enumerate() if t.name.startswith("traceroute-")]


def test_traceroute_already_cancelled(monkeypatch):
    """Regrese B05: předem zrušený běh nesmí ani spustit mlčící proces."""
    processes = _local_trace(monkeypatch, "import time; time.sleep(30)")
    start = time.monotonic()
    ip, err = traceroute.run("192.0.2.1", lambda h: None, cancelled=lambda: True)
    assert time.monotonic() - start <= 2
    assert ip == "192.0.2.1" and err == "Zrušeno" and not processes


@pytest.mark.parametrize("partial", [False, True])
def test_traceroute_cancel_silent_process(monkeypatch, tmp_path, partial):
    """Regrese B05: mlčení i neukončený řádek musí jít zrušit do 2 s a proces sklidit."""
    ready = tmp_path / "ready"
    code = ("import pathlib, sys, time; "
            + ("sys.stdout.write(' 1  192.0.2.2  1 ms'); sys.stdout.flush(); " if partial else "")
            + f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)")
    processes = _local_trace(monkeypatch, code)
    cancel = threading.Event()
    result = []
    worker = threading.Thread(target=lambda: result.append(
        traceroute.run("192.0.2.1", lambda h: None, cancelled=cancel.is_set)))
    worker.start()
    try:
        limit = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < limit:
            time.sleep(0.01)
        assert ready.exists() and processes[0].poll() is None
        start = time.monotonic()
        cancel.set()
        worker.join(2)
        assert not worker.is_alive() and time.monotonic() - start <= 2
        assert result == [("192.0.2.1", "Zrušeno")]
        assert processes[0].poll() is not None
        assert not [t for t in threading.enumerate() if t.name.startswith("traceroute-")]
    finally:
        cancel.set()
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        worker.join(3)


def test_traceroute_cancel_during_spawn(monkeypatch):
    """Regrese B05: zrušení během startu procesu se kontroluje i bez prvního řádku."""
    processes = _local_trace(monkeypatch, "import time; time.sleep(30)")
    start = time.monotonic()
    _, err = traceroute.run("192.0.2.1", lambda h: None, cancelled=lambda: bool(processes))
    assert err == "Zrušeno" and time.monotonic() - start <= 2
    assert processes and processes[0].poll() is not None


@pytest.mark.skipif(sys.platform == "win32", reason="ignorování SIGTERM je POSIX scénář")
def test_traceroute_kills_uncooperative_process(monkeypatch, tmp_path):
    """Regrese B05: po ignorovaném terminate následuje kill a wait do 2 s."""
    ready = tmp_path / "ready"
    processes = _local_trace(monkeypatch,
                            "import pathlib, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                            f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)")
    cancelled_at = []

    def cancelled():
        if ready.exists():
            if not cancelled_at:
                cancelled_at.append(time.monotonic())
            return True
        return False

    _, err = traceroute.run("192.0.2.1", lambda h: None, cancelled=cancelled)
    assert err == "Zrušeno" and time.monotonic() - cancelled_at[0] <= 2
    assert processes[0].returncode == -9


@pytest.mark.skipif(sys.platform == "win32", reason="vlastní skupina procesů je POSIX scénář")
def test_traceroute_cancel_process_group(monkeypatch):
    """Regrese L32 review: potomci shellu držící pipe nesmí prodloužit zrušení nad 2 s."""
    import psutil

    processes = _local_trace(monkeypatch, "")
    monkeypatch.setattr(traceroute, "system_command",
                        lambda *args: (["/bin/sh", "-c", "sleep 6 & sleep 30"], "traceroute"))
    cancel = threading.Event()
    result = []
    worker = threading.Thread(target=lambda: result.append(
        traceroute.run("192.0.2.1", lambda h: None, cancelled=cancel.is_set)))
    children = []
    worker.start()
    try:
        time.sleep(0.5)
        assert processes and processes[0].poll() is None
        children = psutil.Process(processes[0].pid).children(recursive=True)
        assert children
        start = time.monotonic()
        cancel.set()
        worker.join(2)
        assert not worker.is_alive() and time.monotonic() - start <= 2
        assert result == [("192.0.2.1", "Zrušeno")] and processes[0].poll() is not None
        # Zombie už neběží ani nedrží pipe; jeho sklizení patří rodiči/initu.
        assert all(not child.is_running() or child.status() == psutil.STATUS_ZOMBIE for child in children)
        assert not [t for t in threading.enumerate() if t.name.startswith("traceroute-")]
    finally:
        cancel.set()
        for child in children:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        worker.join(3)


@pytest.mark.skipif(sys.platform == "win32", reason="opuštění skupiny přes setsid je POSIX scénář")
def test_traceroute_reader_join_is_bounded(monkeypatch, tmp_path):
    """Regrese L32 review: ani potomek mimo skupinu nesmí zablokovat join čtecích vláken."""
    ready = tmp_path / "child-pid"
    child_code = "import time; time.sleep(30)"
    processes = _local_trace(monkeypatch, "import pathlib, subprocess, sys, time; "
                            f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}], "
                            "start_new_session=True); "
                            f"pathlib.Path({str(ready)!r}).write_text(str(child.pid)); time.sleep(30)")
    cancelled_at = []

    def cancelled():
        if ready.exists() and ready.read_text():
            if not cancelled_at:
                cancelled_at.append(time.monotonic())
            return True
        return False

    try:
        _, err = traceroute.run("192.0.2.1", lambda h: None, cancelled=cancelled)
        assert err == "Zrušeno" and time.monotonic() - cancelled_at[0] <= 2
        assert processes[0].poll() is not None
    finally:
        if ready.exists() and ready.read_text():
            try:
                os.kill(int(ready.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        for reader in threading.enumerate():
            if reader.name.startswith("traceroute-"):
                reader.join(1)
    assert not [t for t in threading.enumerate() if t.name.startswith("traceroute-")]


@pytest.mark.parametrize("output", ["", "print(' 1  192.0.2.1  1 ms'); "])
def test_traceroute_stderr_error(monkeypatch, output):
    """Regrese B05: chyba nástroje se nezamění za nedosažitelný cíl ani při dosaženém hopu."""
    processes = _local_trace(monkeypatch, "import sys; " + output
                            + "sys.stderr.write('x' * 10000 + 'permission denied'); sys.exit(7)")
    hops = []
    ip, err = traceroute.run("192.0.2.1", hops.append)
    assert ip == "192.0.2.1" and "selhal" in err and "7" in err and "permission denied" in err
    assert "neodpověděl" not in err and len(err) < 2200 and processes[0].returncode == 7
    assert bool(hops) == bool(output)


def test_traceroute_nonzero_without_stderr(monkeypatch):
    """Regrese B05: i nenulový exit bez stderr obsahuje skutečný důvod selhání."""
    _local_trace(monkeypatch, "raise SystemExit(2)")
    _, err = traceroute.run("192.0.2.1", lambda h: None)
    assert "selhal" in err and "2" in err and "neodpověděl" not in err


@pytest.mark.parametrize("reached", [False, True])
def test_traceroute_success_with_stderr(monkeypatch, reached):
    """Regrese L32 review: běžná hlavička a varování na stderr při exit 0 nejsou chyba."""
    code = ("import sys; sys.stderr.write('traceroute to 192.0.2.1, 30 hops max\\nwarning\\n'); "
            + ("print(' 1  192.0.2.1  1 ms')" if reached else ""))
    _local_trace(monkeypatch, code)
    _, err = traceroute.run("192.0.2.1", lambda h: None)
    assert err == ("" if reached else
                   "Cíl na sondy neodpověděl (může blokovat ICMP/UDP) – trasa je zobrazena, kam až vede")


@pytest.mark.parametrize("detail", ["permission denied", ""])
def test_traceroute_error_omits_stderr_header(monkeypatch, detail):
    """Regrese L32 review: chybový detail obsahuje diagnostiku bez běžné hlavičky traceroute."""
    stderr = "traceroute to 192.0.2.1, 30 hops max\n" + detail
    _local_trace(monkeypatch, f"import sys; sys.stderr.write({stderr!r}); sys.exit(7)")
    _, err = traceroute.run("192.0.2.1", lambda h: None)
    expected = "Příkaz traceroute selhal (návratový kód 7)" + (f": {detail}" if detail else "")
    assert err == expected


def test_traceroute_deadline_has_reserve(monkeypatch, tmp_path):
    """Regrese L32 review: tiché hopy mají rezervu nad původním těsným deadlinem 95 s."""
    ready = tmp_path / "ready"
    _local_trace(monkeypatch, "import pathlib, time; "
                 f"pathlib.Path({str(ready)!r}).touch(); time.sleep(0.2); print(' 1  192.0.2.1  1 ms')")
    base = time.monotonic()
    monkeypatch.setattr(traceroute, "time", SimpleNamespace(monotonic=lambda: base + (100 if ready.exists() else 0)))
    assert traceroute.run("192.0.2.1", lambda h: None) == ("192.0.2.1", "")


@pytest.mark.parametrize("close_stdout", [False, True])
def test_traceroute_total_deadline(monkeypatch, tmp_path, close_stdout):
    """Regrese B05: celkový deadline platí za mlčení i po EOF stále běžícího procesu."""
    ready = tmp_path / "ready"
    processes = _local_trace(monkeypatch, "import os, pathlib, time; "
                            + ("os.close(1); " if close_stdout else "")
                            + f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)")
    # Posun jen hodin core, skutečné čekání procesu zůstává nemockované.
    monkeypatch.setattr(traceroute, "time", SimpleNamespace(
        monotonic=lambda: time.monotonic() + (1000 if ready.exists() else 0)))
    start = time.monotonic()
    _, err = traceroute.run("192.0.2.1", lambda h: None)
    assert "časový limit" in err and time.monotonic() - start <= 2
    assert processes[0].poll() is not None


def test_traceroute_start_failure(monkeypatch):
    """Regrese B05: chybu spuštění doplňuje český kontext, chybějící nástroj je samostatný stav."""
    monkeypatch.setattr(traceroute, "system_command", lambda *args: None)
    _, err = traceroute.run("192.0.2.1", lambda h: None)
    assert "nenalezen" in err or "Není nainstalován" in err
    monkeypatch.setattr(traceroute, "system_command", lambda *args: (["missing-program"], "traceroute"))

    def fail(*args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(traceroute.subprocess, "Popen", fail)
    _, err = traceroute.run("192.0.2.1", lambda h: None)
    assert "Nelze spustit traceroute" in err and "permission denied" in err


@pytest.mark.parametrize("kind,line", [
    ("traceroute", " 1  192.0.2.1  1 ms"),
    ("tracert", " 1  1 ms  1 ms  1 ms  192.0.2.1"),
    ("tracepath", " 1:  192.0.2.2  1.000ms\n 1:  192.0.2.1  2.000ms reached"),
])
def test_traceroute_local_success(monkeypatch, kind, line):
    """Regrese B05: fronta doručí i poslední řádek bez newline a sloučený tracepath cíl."""
    processes = _local_trace(monkeypatch, f"import sys; sys.stdout.write({line!r})", kind)
    hops = []
    assert traceroute.run("192.0.2.1", hops.append) == ("192.0.2.1", "")
    assert hops[-1].reached and processes[0].returncode == 0


@pytest.mark.parametrize("finish", ["close", "reject", "accept"])
def test_traceroute_dialog_waits_for_worker(monkeypatch, tmp_path, finish):
    """Regrese B05: zavření i Escape drží dialog/bridge až do sklizení procesu a workeru."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    from shiboken6 import isValid
    from netmonitor.gui import tools_dialog as tools

    app = QApplication.instance() or QApplication([])
    ready = tmp_path / "ready"
    processes = _local_trace(monkeypatch, "import pathlib, time; "
                            f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)")
    dialog = tools.open_traceroute(SimpleNamespace(), PingTarget(name="Test", host="192.0.2.1"))
    finished = []
    dialog.finished.connect(lambda result: finished.append(not dialog.busy() and processes[0].poll() is not None))
    try:
        limit = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < limit:
            app.processEvents()
            time.sleep(0.01)
        assert ready.exists() and dialog.busy()
        start = time.monotonic()
        getattr(dialog, finish)()
        assert dialog in tools._trace_dialogs and not finished and isValid(dialog)
        while not finished and time.monotonic() - start < 2:
            app.processEvents()
            time.sleep(0.01)
        assert finished == [True] and time.monotonic() - start <= 2
        assert dialog not in tools._trace_dialogs
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not isValid(dialog)
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        if isValid(dialog):
            if dialog._thread is not None:
                dialog._thread.join(3)
            dialog.close()
        app.processEvents()


def test_traceroute_dialog_displays_error(monkeypatch):
    """Regrese B05: stderr chyba má v GUI přednost i před dosaženým hopem."""
    from PySide6.QtWidgets import QApplication
    from netmonitor.gui.tools_dialog import TracerouteDialog

    app = QApplication.instance() or QApplication([])
    dialog = TracerouteDialog(SimpleNamespace(), PingTarget(name="Test", host="192.0.2.1"))
    dialog.hops = [traceroute.Hop(1, "192.0.2.1", [1], reached=True)]
    dialog._finished("192.0.2.1", "Příkaz traceroute selhal: <permission denied>")
    assert dialog.status.text() == "Příkaz traceroute selhal: <permission denied>"
    dialog.close()
    app.processEvents()


def test_traceroute_completion_timer_has_dialog_context(monkeypatch):
    """Regrese L32 review: odložené dokončení se po zničení dialogu už nesmí zavolat."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    from shiboken6 import isValid
    from netmonitor.gui.tools_dialog import TracerouteDialog

    app = QApplication.instance() or QApplication([])
    dialog = TracerouteDialog(SimpleNamespace(), PingTarget(name="Test", host="192.0.2.1"))
    monkeypatch.setattr(dialog, "busy", lambda: True)
    dialog._finished("192.0.2.1", "")
    calls = []
    monkeypatch.setattr(dialog, "_finished", lambda *args: calls.append(args))
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(dialog)
    time.sleep(0.03)
    app.processEvents()
    assert not calls


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
