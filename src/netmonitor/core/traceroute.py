"""Traceroute – trasa paketů k cíli po jednotlivých skocích (hopech).

* Windows: systémový `tracert -d` v samostatném procesu (případná chyba nemůže shodit aplikaci);
  výstup se parsuje nezávisle na jazyku systému (čísla, "ms", "*", IP adresy).
* Linux / macOS: systémový `traceroute -n` (případně `tracepath -n`), výstup se průběžně parsuje.

Výsledky se doručují po jednotlivých hopech přes callback (vhodné pro živé zobrazení).
"""
from __future__ import annotations

import ipaddress
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, TextIO

from .ping_engine import Resolver


@dataclass
class Hop:
    n: int
    ip: str | None = None
    rtts: list[float | None] = field(default_factory=list)
    reached: bool = False
    note: str = ""

    @property
    def best(self) -> float | None:
        vals = [r for r in self.rtts if r is not None]
        return min(vals) if vals else None

    @property
    def avg(self) -> float | None:
        vals = [r for r in self.rtts if r is not None]
        return sum(vals) / len(vals) if vals else None

    @property
    def loss_pct(self) -> float:
        return 100.0 * sum(1 for r in self.rtts if r is None) / len(self.rtts) if self.rtts else 100.0


# ---------------------------------------------------------------------------- parsování (Linux / macOS)

_IP = r"(?:\d{1,3}\.){3}\d{1,3}|[0-9a-fA-F:]+:[0-9a-fA-F:]+"
_TR_LINE = re.compile(r"^\s*(\d+)\s+(.*)$")
_TR_TOKEN = re.compile(rf"({_IP})|(\d+(?:\.\d+)?)\s*ms|(\*)|(![A-Za-z0-9]*)")


def parse_traceroute_line(line: str, dest_ip: str | None = None) -> Hop | None:
    """' 3  10.0.0.1  5.123 ms * 5.3 ms' -> Hop(3, '10.0.0.1', [5.123, None, 5.3])"""
    m = _TR_LINE.match(line)
    if not m:
        return None
    hop = Hop(int(m.group(1)))
    for ip, ms, star, flag in _TR_TOKEN.findall(m.group(2)):
        if ip and hop.ip is None:
            try:
                ipaddress.ip_address(ip)
                hop.ip = ip
            except ValueError:
                pass
        elif ms:
            hop.rtts.append(float(ms))
        elif star:
            hop.rtts.append(None)
        elif flag:
            hop.note = (hop.note + " " + flag).strip()
    if not hop.rtts and hop.ip is None:
        return None
    hop.reached = bool(dest_ip and hop.ip == dest_ip)
    return hop


def parse_tracert_line(line: str, dest_ip: str | None = None) -> Hop | None:
    """Windows tracert (libovolný jazyk):
    '  1    <1 ms    <1 ms    <1 ms  192.168.14.1'
    '  3     *        *        *     Request timed out.' / 'Vypršel časový limit žádosti.'
    '  5  192.168.1.1  reports: Destination host unreachable.' / 'hlásí: Cílový hostitel není dostupný.'"""
    m = _TR_LINE.match(line)
    if not m:
        return None
    rest = m.group(2)
    hop = Hop(int(m.group(1)))
    for ms, star in re.findall(r"<?\s*(\d+)\s*ms|(\*)", rest):
        hop.rtts.append(float(ms) if ms else None)
    ipm = re.search(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])|\[?([0-9a-fA-F]*:[0-9a-fA-F:]+)\]?", rest)
    if ipm:
        cand = ipm.group(1) or ipm.group(2)
        try:
            ipaddress.ip_address(cand)
            hop.ip = cand
        except ValueError:
            pass
    if not hop.rtts and hop.ip is None:
        return None
    if hop.ip and not hop.rtts:            # "X reports: Destination host/net unreachable"
        hop.rtts = [None]
        hop.note = "!H"
    hop.reached = bool(dest_ip and hop.ip == dest_ip)
    return hop


_TP_LINE = re.compile(r"^\s*(\d+)\??:\s+(.*)$")


def parse_tracepath_line(line: str, dest_ip: str | None = None) -> Hop | None:
    """tracepath: ' 2:  192.168.1.1   0.512ms' / ' 3:  no reply' / ' 1?: [LOCALHOST] pmtu 1500'"""
    m = _TP_LINE.match(line)
    if not m:
        return None
    rest = m.group(2)
    if "LOCALHOST" in rest:
        return None
    hop = Hop(int(m.group(1)))
    if "no reply" in rest:
        hop.rtts.append(None)
        return hop
    ipm = re.match(rf"\s*({_IP})", rest)
    if ipm:
        hop.ip = ipm.group(1)
    ms = re.search(r"(\d+(?:\.\d+)?)ms", rest)
    hop.rtts.append(float(ms.group(1)) if ms else None)
    hop.reached = bool(dest_ip and hop.ip == dest_ip) or "reached" in rest
    return hop


def system_command(dest_ip: str, max_hops: int, probes: int, timeout: float) -> tuple[list[str], str] | None:
    v6 = ":" in dest_ip
    if sys.platform == "win32":
        tc = shutil.which("tracert") or os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                                                     "tracert.exe")
        if not os.path.exists(tc):
            return None
        return [tc, "-d", "-6" if v6 else "-4", "-h", str(max_hops), "-w", str(max(500, int(timeout * 1000))),
                dest_ip], "tracert"
    wait = str(max(1, int(round(timeout))))
    tr = shutil.which("traceroute6" if v6 and sys.platform == "darwin" else "traceroute")
    if tr:
        args = [tr, "-n", "-q", str(probes), "-w", wait, "-m", str(max_hops)]
        if v6 and sys.platform != "darwin":
            args.append("-6")
        return args + [dest_ip], "traceroute"
    tp = shutil.which("tracepath")
    if tp:
        return [tp, "-n", "-m", str(max_hops)] + (["-6"] if v6 else []) + [dest_ip], "tracepath"
    return None


# ---------------------------------------------------------------------------- běh

def run(host: str, on_hop: Callable[[Hop], None], *, max_hops: int = 30, probes: int = 3, timeout: float = 1.0,
        cancelled: Callable[[], bool] = lambda: False, resolver: Resolver | None = None) -> tuple[str | None, str]:
    """Spustí traceroute. Vrací (cílová IP | None, chyba). Hopy průběžně posílá do on_hop."""
    ip, err = (resolver or Resolver()).resolve(host)
    if not ip:
        return None, err or "Doménu nelze přeložit"
    return ip, _run_system(ip, on_hop, max_hops, probes, timeout, cancelled)


def _stop_process(proc: subprocess.Popen) -> None:
    """Vždy sklidí proces; ani ignorovaný terminate nesmí zablokovat zrušení."""
    running = proc.returncode is None
    try:
        # Už sklizenému procesu mohl OS znovu přidělit PID; jeho skupinu
        # nesignalizovat. Stav zachytit před případným wait/poll v úklidu.
        if not running:
            return
        if sys.platform != "win32":
            # Skupina patří jen tomuto běhu (start_new_session). Potomci
            # mohou držet pipe, proto signály neposíláme jen rodiči.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError as e:
                if not isinstance(e, ProcessLookupError):
                    proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            # Rodič mohl skončit dřív než potomek, který SIGTERM ignoruje.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError as e:
                if not isinstance(e, ProcessLookupError):
                    proc.kill()
        elif proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
    finally:
        proc.wait()


def _run_system(ip, on_hop, max_hops, probes, timeout, cancelled) -> str:
    if cancelled():
        return "Zrušeno"
    cmd = system_command(ip, max_hops, probes, timeout)
    if not cmd:
        if sys.platform == "win32":
            return "Systémový příkaz tracert nebyl nalezen"
        return "Není nainstalován traceroute ani tracepath (např. sudo apt install traceroute)"
    args, kind = cmd
    parse = {"traceroute": parse_traceroute_line, "tracepath": parse_tracepath_line,
             "tracert": parse_tracert_line}[kind]
    env = dict(os.environ, LC_ALL="C", LANG="C")
    extra: dict = {"errors": "replace", "start_new_session": True}
    if sys.platform == "win32":
        extra = {"creationflags": 0x08000000, "encoding": "oem", "errors": "replace"}   # CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                env=env, text=True, bufsize=1, **extra)
    except OSError as e:
        return f"Nelze spustit {kind}: {e}"
    # Čtení pipe ve vláknech funguje i na Windows. Blokující readline (včetně
    # neúplného řádku) nikdy neblokuje řídicí smyčku s kontrolou zrušení/deadlinu.
    lines: queue.Queue[str | None] = queue.Queue(maxsize=256)
    stopping = threading.Event()
    stderr_tail = ""

    def put_line(line: str | None) -> None:
        while not stopping.is_set():
            try:
                lines.put(line, timeout=0.05)
                return
            except queue.Full:
                pass

    def read_stdout(stream: TextIO) -> None:
        try:
            for line in stream:
                put_line(line)
                if stopping.is_set():
                    break
        finally:
            stream.close()
            put_line(None)

    def read_stderr(stream: TextIO) -> None:
        nonlocal stderr_tail
        try:
            while chunk := stream.read(1024):
                stderr_tail = (stderr_tail + chunk)[-2048:]
        finally:
            stream.close()

    assert proc.stdout is not None and proc.stderr is not None
    readers = [threading.Thread(target=read_stdout, args=(proc.stdout,), name="traceroute-stdout", daemon=True),
               threading.Thread(target=read_stderr, args=(proc.stderr,), name="traceroute-stderr", daemon=True)]
    reached = False
    pending: Hop | None = None       # tracepath vypisuje hop i víckrát – sloučit
    # Rozpočet sond s 50% rezervou na interní čekání tracepath/tracert a režii
    # plánování na tichých hopech; dalších 5 s na dokončení výstupu.
    deadline = time.monotonic() + max_hops * probes * max(1.0, timeout) * 1.5 + 5
    error = ""
    eof = False
    try:
        for reader in readers:
            reader.start()
        while not (eof and proc.poll() is not None):
            if cancelled():
                error = "Zrušeno"
                break
            if time.monotonic() >= deadline:
                error = "Vypršel časový limit trasování"
                break
            try:
                line = lines.get(timeout=0.05)
            except queue.Empty:
                continue
            if line is None:
                eof = True
                continue
            hop = parse(line, ip)
            if hop is None:
                continue
            reached = reached or hop.reached
            if kind == "tracepath" and pending and pending.n == hop.n:
                pending.rtts += hop.rtts
                pending.ip = pending.ip or hop.ip
                pending.reached = pending.reached or hop.reached
                continue
            if pending:
                on_hop(pending)
            pending = hop
        if pending:
            on_hop(pending)
    finally:
        stopping.set()
        try:
            _stop_process(proc)
        finally:
            for reader in readers:
                if reader.ident is not None:
                    # Pojistka i pro Windows nebo potomka, který opustil POSIX
                    # skupinu: cizí otevřená pipe nesmí zadržet worker/dialog.
                    reader.join(timeout=0.2)
    if error:
        return error
    if proc.returncode:
        detail = "\n".join(line for line in stderr_tail.splitlines()
                           if not line.lstrip().startswith("traceroute to ")).strip()
        return f"Příkaz {kind} selhal (návratový kód {proc.returncode})" + (f": {detail}" if detail else "")
    return "" if reached else "Cíl na sondy neodpověděl (může blokovat ICMP/UDP) – trasa je zobrazena, kam až vede"
