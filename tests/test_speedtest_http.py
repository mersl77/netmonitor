"""Regrese B10: měření přijímá jen úspěšné a úplné HTTP přenosy."""
import http.client
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from netmonitor.core import speedtest


@pytest.fixture
def http_server(monkeypatch):
    """Skutečný HTTP transport pouze na loopbacku, bez veřejného speedtestu."""
    modes = {"latency": "ok", "download": "ok", "upload": "ok"}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            pass

        def do_GET(self):
            size = int(parse_qs(urlsplit(self.path).query)["bytes"][0])
            self.respond("download" if size else "latency", size)

        def do_POST(self):
            self.connection.settimeout(1)
            try:
                body = self.rfile.read(int(self.headers["Content-Length"]))
            except TimeoutError:
                self.close_connection = True
                return
            if len(body) != int(self.headers["Content-Length"]):
                self.close_connection = True
                return
            self.respond("upload", 0)

        def respond(self, phase, size):
            mode = modes[phase]
            if mode in ("disconnect", "timeout"):
                if mode == "timeout":
                    threading.Event().wait(0.2)
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            status = mode if isinstance(mode, int) else 200
            body = b"x" * size if mode == "ok" else b"<html>Chyba</html>"
            self.send_response(status)
            if mode != "short-no-length":
                declared = size if mode == "truncated" else len(body)
                if mode == "truncated-upload":
                    declared += 10
                self.send_header("Content-Length", str(declared))
            self.send_header("colo", "TEST")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # Klient odmítl chybnou odpověď a zavřel spojení.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    monkeypatch.setattr(speedtest, "_conn", lambda timeout=15: http.client.HTTPConnection(
        "127.0.0.1", server.server_port, timeout=timeout))
    monkeypatch.setattr(speedtest, "DOWN_CHUNK", 64 * 1024)
    monkeypatch.setattr(speedtest, "UP_CHUNK", 32 * 1024)
    try:
        yield modes
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_http_200_full_run(http_server):
    """Regrese: úplné 200 odpovědi stále dávají RTT i obě rychlosti."""
    result = speedtest.run(duration=0.3)
    assert not result.error
    assert result.latency_ms is not None and result.jitter_ms is not None
    assert result.download_mbps is not None and result.download_mbps > 0
    assert result.upload_mbps is not None and result.upload_mbps > 0
    assert result.colo == "TEST"


def test_latency_rejects_http_503(http_server):
    """Regrese: samotná latence musí odmítnout HTTP 503 ještě před vzorkem RTT."""
    http_server["latency"] = 503
    with pytest.raises(OSError, match="HTTP 503"):
        speedtest.measure_latency(n=1)


@pytest.mark.parametrize("phase", ["latency", "download", "upload"])
@pytest.mark.parametrize("status", [302, 403, 429, 503])
def test_http_error_is_not_a_measurement(http_server, phase, status):
    """Regrese: HTTP chybová HTML stránka nedává RTT ani rychlost dané fáze."""
    http_server[phase] = status
    result = speedtest.run(duration=0.25)
    assert f"HTTP {status}" in result.error
    if status == 429:
        assert "příliš mnoho požadavků" in result.error
    field = {"latency": "latency_ms", "download": "download_mbps", "upload": "upload_mbps"}[phase]
    assert getattr(result, field) is None
    if phase != "latency":
        assert result.latency_ms is not None and "Částečný výsledek" in result.error


@pytest.mark.parametrize("phase", ["latency", "download", "upload"])
def test_connection_interrupted(http_server, phase):
    """Regrese: spojení uzavřené bez odpovědi není úspěšná fáze."""
    http_server[phase] = "disconnect"
    result = speedtest.run(duration=0.25)
    assert result.error
    field = {"latency": "latency_ms", "download": "download_mbps", "upload": "upload_mbps"}[phase]
    assert getattr(result, field) is None


@pytest.mark.parametrize("mode", ["short", "short-no-length", "truncated"])
def test_short_download_does_not_count_bytes(http_server, mode):
    """Regrese: krátká 200 odpověď ani předčasné EOF nezvýší čítač."""
    http_server["download"] = mode
    meter = speedtest._Meter(warmup=0)
    with pytest.raises(OSError, match="neúplná data"):
        speedtest._download_worker(meter, threading.Event())
    assert meter.total == 0


@pytest.mark.parametrize("mode", [403, 429, 503, "disconnect", "truncated-upload"])
def test_rejected_upload_does_not_count_bytes(http_server, mode):
    """Regrese: načtené tělo uploadu není potvrzení přijetí serverem."""
    http_server["upload"] = mode
    meter = speedtest._Meter(warmup=0)
    with pytest.raises((OSError, http.client.HTTPException)):
        speedtest._upload_worker(meter, threading.Event())
    assert meter.total == 0


def test_timed_download_counts_valid_partial_transfer(http_server, monkeypatch):
    """Regrese: konec časového okna dovolí změřit i pomalý rozběhnutý download."""
    monkeypatch.setattr(speedtest, "READ", 4096)
    meter = speedtest._Meter(warmup=0)
    stop = threading.Event()
    add = meter.add

    def record(n):
        add(n)
        stop.set()

    monkeypatch.setattr(meter, "add", record)
    speedtest._download_worker(meter, stop)
    assert meter.total == 4096


def test_latency_rejects_unexpected_body(http_server):
    """Regrese: HTML místo prázdné 200 odpovědi není platný vzorek odezvy."""
    http_server["latency"] = "short"
    with pytest.raises(OSError, match="neočekávaná data"):
        speedtest.measure_latency(n=1)


@pytest.mark.parametrize("phase", ["latency", "download", "upload"])
def test_http_timeout(http_server, monkeypatch, phase):
    """Regrese: čekání bez odpovědi skončí chybou, nikoli naměřenou hodnotou."""
    http_server[phase] = "timeout"
    connection = speedtest._conn
    monkeypatch.setattr(speedtest, "_conn", lambda: connection(timeout=0.05))
    result = speedtest.run(duration=0.25)
    assert "časový limit" in result.error
    field = {"latency": "latency_ms", "download": "download_mbps", "upload": "upload_mbps"}[phase]
    assert getattr(result, field) is None


def test_one_failed_stream_invalidates_phase():
    """Regrese: úspěšný proud nesmí maskovat chybu jiného proudu."""
    lock = threading.Lock()
    calls = 0

    def worker(meter, stop):
        nonlocal calls
        with lock:
            calls += 1
            first = calls == 1
        if first:
            meter.add(1024)
        else:
            raise OSError("Měřicí server vrátil HTTP 503.")

    with pytest.raises(OSError, match="HTTP 503"):
        speedtest._run_streams(worker, 2, 0.25, "download", None, lambda: False)


def test_cancelled_run(http_server):
    """Regrese: zrušení měření se dál hlásí česky jako zrušení."""
    result = speedtest.run(cancelled=lambda: True, duration=0.25)
    assert result.error == "Zrušeno"
    assert result.download_mbps is None and result.upload_mbps is None
