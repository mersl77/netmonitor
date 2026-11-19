"""Test rychlosti internetu proti veřejným měřicím serverům Cloudflare (speed.cloudflare.com).

* odezva a jitter: 20× prázdný požadavek po jednom HTTPS spojení (keep-alive)
* stahování: 6 paralelních proudů po dobu ~8 s, prvních ~1,5 s (náběh TCP) se do výsledku nepočítá
* nahrávání: 4 paralelní proudy po dobu ~8 s

Odpovědi musí mít HTTP 2xx. Download ověřuje požadovanou délku; předčasný konec
zneplatní celou fázi. Ukončení čtení časovým limitem měření je záměrné a přijaté
bajty se započítají. Upload započítá jen celé přenosy potvrzené serverem, takže
jeho průběžná rychlost se mění po celých přenosech a na pomalé lince může být
potřeba delší měření.

Měří se celá cesta počítač → internet (včetně Wi-Fi). Používá se jen pokud jsou povolené
externí dotazy. Výsledek je orientační – běžný test v prohlížeči může vyjít mírně jinak.
"""
from __future__ import annotations

import http.client
import os
import ssl
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .. import __version__

HOST = "speed.cloudflare.com"
DOWN_CHUNK = 25_000_000
UP_CHUNK = 4_000_000
READ = 256 * 1024


@dataclass
class SpeedResult:
    latency_ms: float | None = None
    jitter_ms: float | None = None
    download_mbps: float | None = None
    upload_mbps: float | None = None
    colo: str = ""
    error: str = ""
    samples: dict = field(default_factory=dict)


Progress = Callable[[str, float, float | None], None]   # (fáze, 0–1, průběžná hodnota)


def _conn(timeout: float = 15) -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(HOST, timeout=timeout, context=ssl.create_default_context(),
                                       blocksize=64 * 1024)


HEADERS = {"User-Agent": f"NetMonitor/{__version__}", "Accept-Encoding": "identity"}


def mbps(nbytes: float, seconds: float) -> float:
    return nbytes * 8 / seconds / 1e6 if seconds > 0 else 0.0


def _check_response(response: http.client.HTTPResponse) -> None:
    if response.status == 429:
        raise OSError("Měřicí server odmítl příliš mnoho požadavků (HTTP 429). Zkuste test později.")
    if not 200 <= response.status < 300:
        raise OSError(f"Měřicí server vrátil HTTP {response.status}.")


def measure_latency(n: int = 20, cancelled=lambda: False) -> tuple[float, float, str]:
    c = _conn()
    times, colo = [], ""
    try:
        for _ in range(n):
            if cancelled():
                break
            t0 = time.perf_counter()
            c.request("GET", "/__down?bytes=0", headers=HEADERS)
            r = c.getresponse()
            _check_response(r)
            if r.read(1) or r.length not in (0, None):
                raise OSError("Měřicí server vrátil neočekávaná data místo prázdné odpovědi pro odezvu.")
            dt = (time.perf_counter() - t0) * 1000
            colo = r.getheader("colo") or colo
            # odečti dobu zpracování na serveru (Server-Timing: cfSpeedWorker;dur=…) – jen přenos po síti
            st = r.getheader("Server-Timing") or ""
            if "dur=" in st:
                try:
                    dt -= float(st.split("dur=")[-1].split(",")[0].split(";")[0])
                except ValueError:
                    pass
            times.append(max(0.1, dt))
    finally:
        c.close()
    if len(times) > 2:
        times = times[1:]                           # první požadavek zahrnuje TLS handshake
    lat = statistics.median(times) if times else None
    jit = statistics.mean(abs(a - b) for a, b in zip(times, times[1:])) if len(times) > 1 else None
    return lat, jit, colo


class _Meter:
    """Sčítá přenesené bajty ze všech vláken; rychlost počítá až po náběhu (warmup)."""

    def __init__(self, warmup: float):
        self.lock = threading.Lock()
        self.total = 0
        self.t0 = time.perf_counter()
        self.warmup = warmup
        self.base_bytes: int | None = None
        self.base_t: float | None = None

    def add(self, n: int) -> None:
        with self.lock:
            self.total += n
            now = time.perf_counter()
            if self.base_t is None and now - self.t0 >= self.warmup:
                self.base_t, self.base_bytes = now, self.total

    def rate(self) -> float | None:
        with self.lock:
            now = time.perf_counter()
            if self.base_t is None:
                return mbps(self.total, now - self.t0) if now > self.t0 else None
            return mbps(self.total - self.base_bytes, now - self.base_t)


def _run_streams(worker, streams: int, duration: float, phase: str, progress: Progress | None,
                 cancelled) -> float | None:
    meter = _Meter(warmup=min(1.5, duration / 4))
    stop = threading.Event()
    errors: list[OSError | http.client.HTTPException] = []

    def wrap():
        try:
            worker(meter, stop)
        except (OSError, http.client.HTTPException) as e:
            errors.append(e)
            stop.set()

    threads = [threading.Thread(target=wrap, daemon=True, name=f"speed-{phase}-{i}") for i in range(streams)]
    for t in threads:
        t.start()
    start = time.perf_counter()
    while time.perf_counter() - start < duration and not cancelled() and not stop.is_set():
        time.sleep(0.2)
        if errors:
            break
        if progress:
            progress(phase, min(1.0, (time.perf_counter() - start) / duration), meter.rate())
    stop.set()
    for t in threads:
        t.join(timeout=5)
    if errors:
        raise errors[0]
    if meter.total == 0 and not cancelled():
        raise OSError("Během měření se nepodařilo dokončit žádný přenos.")
    return meter.rate()


def _download_worker(meter: _Meter, stop: threading.Event) -> None:
    c = _conn()
    try:
        while not stop.is_set():
            c.request("GET", f"/__down?bytes={DOWN_CHUNK}", headers=HEADERS)
            r = c.getresponse()
            _check_response(r)
            if r.length is not None and r.length != DOWN_CHUNK:
                raise OSError("Měřicí server vrátil neúplná data: "
                              f"očekáváno {DOWN_CHUNK} bajtů, ohlášeno {r.length}.")
            received = 0
            try:
                while not stop.is_set():
                    b = r.read(READ)
                    if not b:
                        break
                    if received + len(b) > DOWN_CHUNK:
                        raise OSError("Měřicí server vrátil více dat, než bylo požadováno.")
                    received += len(b)
                    meter.add(len(b))
                if stop.is_set():
                    break
                if received != DOWN_CHUNK or r.length not in (0, None):
                    raise OSError("Měřicí server vrátil neúplná data při stahování "
                                  f"({received} z {DOWN_CHUNK} bajtů).")
            except (OSError, http.client.HTTPException):
                # Již zobrazený průběžný odhad není platný výsledek chybné odpovědi.
                meter.add(-received)
                raise
    finally:
        c.close()


class _CountingBody:
    def __init__(self, size: int, stop: threading.Event):
        self.left = size
        self.stop = stop
        self.block = os.urandom(64 * 1024)

    def read(self, n: int = -1) -> bytes:
        if self.left <= 0 or self.stop.is_set():
            return b""
        k = min(len(self.block), self.left, n if n and n > 0 else len(self.block))
        self.left -= k
        return self.block[:k]


def _upload_worker(meter: _Meter, stop: threading.Event) -> None:
    c = _conn()
    try:
        while not stop.is_set():
            body = _CountingBody(UP_CHUNK, stop)
            h = dict(HEADERS, **{"Content-Type": "application/octet-stream", "Content-Length": str(UP_CHUNK)})
            try:
                c.request("POST", "/__up", body=body, headers=h)
                r = c.getresponse()
            except (OSError, http.client.HTTPException):
                if stop.is_set():
                    break
                raise
            _check_response(r)
            r.read()
            if body.left:
                if stop.is_set():
                    break
                raise OSError("Nahrávání nebylo dokončeno.")
            meter.add(UP_CHUNK)
    finally:
        c.close()


def run(progress: Progress | None = None, cancelled: Callable[[], bool] = lambda: False,
        duration: float = 8.0) -> SpeedResult:
    res = SpeedResult()
    try:
        if progress:
            progress("latency", 0.0, None)
        res.latency_ms, res.jitter_ms, res.colo = measure_latency(cancelled=cancelled)
        if cancelled():
            res.error = "Zrušeno"
            return res
        res.download_mbps = _run_streams(_download_worker, 6, duration, "download", progress, cancelled)
        if cancelled():
            res.error = "Zrušeno"
            return res
        res.upload_mbps = _run_streams(_upload_worker, 4, duration, "upload", progress, cancelled)
        if cancelled():
            res.error = "Zrušeno"
    except (OSError, http.client.HTTPException, ssl.SSLError) as e:
        if cancelled():
            res.error = "Zrušeno"
        else:
            if isinstance(e, TimeoutError):
                detail = "Vypršel časový limit spojení s měřicím serverem."
            elif isinstance(e, (http.client.HTTPException, ConnectionError)):
                detail = "Spojení s měřicím serverem bylo přerušeno."
            else:
                detail = str(e)
            partial = "Částečný výsledek. " if res.latency_ms is not None else ""
            res.error = f"{partial}Test se nezdařil: {detail}"
    return res
