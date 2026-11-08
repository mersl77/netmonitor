"""Regrese B04/B09: výsledky patří pouze aktuální generaci konfigurace."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from netmonitor.core.config import AppConfig, PingTarget
from netmonitor.core.monitoring import MonitorEngine
from netmonitor.core.network_info import InterfaceInfo, NetworkSnapshot
from netmonitor.core.wan_info import WanResult
from netmonitor.gui import controller as cmod
from netmonitor.gui.ping_panel import PingPanel

from .fakes import FakePinger


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def ctrl(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(cmod, "select_pinger", lambda: FakePinger(default="timeout"))
    cfg = AppConfig.from_dict({"network": {"external_api": False}, "ping": {"targets": []}})
    cfg.ping.targets = [PingTarget(name="Cíl", host="192.0.2.1", retries=1, id="a")]
    c = cmod.Controller(cfg, tmp_path / "config.json", tmp_path / "history.sqlite3")
    monkeypatch.setattr(c, "refresh_network", lambda: None)
    c._apply_targets()
    yield c
    c.shutdown()
    QCoreApplication.processEvents()


def probe(ctrl, target=None):
    """Skutečný worker callback se zařadí do Qt fronty, GUI zatím nedoručuje."""
    target = target or ctrl.cfg.ping.targets[0]
    delivered = threading.Event()
    original = ctrl.engine.on_result

    def on_result(snap, r):
        original(snap, r)
        if snap.target_id == target.id:
            delivered.set()

    # Worker spouštíme přímo přes plánovač až po instalaci callbacku.
    ctrl.engine.on_result = on_result
    ctrl.engine.start()
    try:
        assert delivered.wait(3), "worker nedokončil měření"
    finally:
        ctrl.engine.stop()
        ctrl.engine.on_result = original
    return ctrl.engine.snapshot(target.id)


def change(ctrl, mode):
    cfg = ctrl.cfg.copy()
    if mode == "remove":
        cfg.ping.targets = []
    elif mode == "host":
        cfg.ping.targets[0].host = "198.51.100.1"
    else:
        cfg.ping.targets[0].enabled = False
    ctrl.update_config(cfg, save=False)


def wait_background(ctrl):
    """Vyčká i na queued completion a případné navazující úlohy, nejen na worker."""
    deadline = time.monotonic() + 3
    while ctrl._busy and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        threading.Event().wait(0.01)
    assert not ctrl._busy, f"Nedokončené úlohy: {ctrl._busy}"


@pytest.mark.parametrize("mode", ["remove", "host", "disable"])
def test_queued_result_and_transition_are_discarded(ctrl, monkeypatch, mode):
    """Regrese B04: queued výsledek ani přechod nesmí oživit starou konfiguraci."""
    emitted, events, transitions, latencies = [], [], [], []
    ctrl.target_result.connect(lambda *args: emitted.append(args))
    monkeypatch.setattr(ctrl.history, "add_event", lambda *args: events.append(args))
    monkeypatch.setattr(ctrl.notifier, "on_target_transition", transitions.append)
    monkeypatch.setattr(ctrl.notifier, "on_latency", lambda *args: latencies.append(args))
    probe(ctrl)
    change(ctrl, mode)
    QCoreApplication.processEvents()
    wait_background(ctrl)
    assert "a" not in ctrl.snapshots
    assert emitted == transitions == latencies == events == []


@pytest.mark.parametrize("mode", ["remove", "host", "disable"])
def test_config_change_clears_snapshot_and_pending_notifications(ctrl, mode):
    """Regrese B04: již přijaté měření a odložený alert se po změně zahodí."""
    probe(ctrl)
    QCoreApplication.processEvents()
    assert ctrl.snapshots["a"].up is False
    change(ctrl, mode)
    assert "a" not in ctrl.snapshots
    ctrl.notifier.clock = lambda: time.monotonic() + 3600
    assert ctrl.notifier.tick() == []
    ctrl._evaluate()
    assert ctrl.evaluation.layers["internet_targets"] == "0/0"


@pytest.mark.parametrize("mode", ["remove", "host", "disable"])
def test_inflight_probe_cannot_cross_target_generation(mode):
    """Regrese B04: remove/readd (ABA), host ABA i disable/enable během sondy."""
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        assert release.wait(3)
        return "timeout"

    pinger = FakePinger({"192.0.2.1": blocked})
    results, transitions = [], []
    eng = MonitorEngine(pinger, max_concurrent=1, on_result=lambda *args: results.append(args),
                        on_transition=transitions.append)
    t = PingTarget(name="Cíl", host="192.0.2.1", interval_s=60, retries=1, id="a")
    eng.set_targets([t])
    eng.start()
    try:
        assert entered.wait(3)
        eng.pause(True)
        replacement = [] if mode == "remove" else [PingTarget(
            name=t.name, host="198.51.100.1" if mode == "host" else t.host,
            enabled=mode != "disable", id=t.id)]
        eng.set_targets(replacement)
        eng.set_targets([t])
        # Marker za jediným workerem dokazuje dokončení původní sondy bez sleep.
        assert eng._executor is not None
        release.set()
        eng._executor.submit(lambda: None).result(timeout=3)
    finally:
        release.set()
        eng.stop()
    snap = eng.snapshot("a")
    assert snap is not None and snap.ok_count == snap.fail_count == 0
    assert snap.last is None and snap.up is None
    assert results == transitions == []
    # Nová generace přitom dál přijímá vlastní výsledky.
    current = threading.Event()

    def on_current(snap, r):
        results.append((snap, r))
        current.set()

    eng.on_result = on_current
    eng.set_pinger(FakePinger(default="ok:4"))
    eng.pause(False)
    eng.start()
    try:
        assert current.wait(3)
    finally:
        eng.stop()
    assert len(results) == 1 and results[0][0].ok_count == 1
    assert results[0][0].fail_count == 0


def test_gateway_disable_clears_snapshot_and_queued_result(ctrl):
    """Regrese B04: vypnutý gateway_probe nesmí ponechat ani obnovit snapshot brány."""
    gateway = "192.0.2.254"
    a = InterfaceInfo(name="Test", display_name="Test", gateway=gateway)
    ctrl.network = NetworkSnapshot(active=a)
    ctrl._apply_targets()
    probe(ctrl, PingTarget(name="Brána", host=gateway, id=cmod.GATEWAY_ID))
    QCoreApplication.processEvents()
    assert ctrl.gateway_snapshot is not None
    stale = ctrl.gateway_snapshot
    cfg = ctrl.cfg.copy()
    cfg.network.gateway_probe = False
    ctrl.update_config(cfg, save=False)
    ctrl._on_deliver(("__result__", None), (stale, stale.last))
    assert ctrl.gateway_snapshot is None


def test_wan_completion_after_disable_is_discarded(ctrl, monkeypatch):
    """Regrese B09: skutečný rozběhnutý WAN job nepřepíše vypnuté externí dotazy."""
    entered, release = threading.Event(), threading.Event()
    updates, isp_calls = [], []
    ctrl.wan_updated.connect(updates.append)

    def fetch():
        entered.set()
        assert release.wait(3)
        return WanResult("192.0.2.10", ts=time.time())

    monkeypatch.setattr(cmod, "fetch_wan_ip", fetch)
    monkeypatch.setattr(ctrl.wan, "isp_for", lambda *args: isp_calls.append(args))
    # Jediný background worker umožní markerem přesně počkat na zařazení completion.
    ctrl._bg.shutdown(wait=True)
    ctrl._bg = ThreadPoolExecutor(max_workers=1)
    cfg = ctrl.cfg.copy()
    cfg.network.external_api = True
    ctrl.update_config(cfg, save=False)
    try:
        assert entered.wait(3)
        cfg = ctrl.cfg.copy()
        cfg.network.external_api = False
        ctrl.update_config(cfg, save=False)
        release.set()
        ctrl._bg.submit(lambda: None).result(timeout=3)
        QCoreApplication.processEvents()
        assert updates and all(u["disabled"] and u["ip"] is None for u in updates)
        assert ctrl.wan.current is None and ctrl.wan.last_check is None
        assert ctrl.isp is None and isp_calls == []
    finally:
        release.set()


def test_ping_panel_rejects_disabled_or_different_host(ctrl):
    """Regrese B04: ani přímé doručení do panelu nesmí ukázat cizí/vypnutý cíl."""
    stale = probe(ctrl)
    panel = PingPanel()
    try:
        change(ctrl, "host")
        panel.set_targets(ctrl.cfg, {})
        panel.update_snapshot(stale)
        assert panel.rows["a"].snap is None
        change(ctrl, "disable")
        panel.set_targets(ctrl.cfg, {"a": replace(stale, host=ctrl.cfg.ping.targets[0].host)})
        assert panel.rows["a"].snap is None
    finally:
        panel.close()


def test_queued_result_cannot_cross_remove_readd(ctrl):
    """Regrese B04: GUI odmítne i queued ABA se stejným ID a hostem."""
    probe(ctrl)
    original = ctrl.cfg.copy()
    change(ctrl, "remove")
    ctrl.update_config(original, save=False)
    QCoreApplication.processEvents()
    assert ctrl.snapshots == {}
    ctrl.notifier.clock = lambda: time.monotonic() + 3600
    assert ctrl.notifier.tick() == []
    probe(ctrl)
    QCoreApplication.processEvents()
    assert ctrl.snapshots["a"].fail_count == 1


def test_rename_keeps_measurement_and_generation(ctrl):
    """Regrese B04: pouhá změna názvu/pořadí nemá zneplatnit měření."""
    snap = probe(ctrl)
    cfg = ctrl.cfg.copy()
    cfg.ping.targets[0].name = "Nový název"
    ctrl.update_config(cfg, save=False)
    QCoreApplication.processEvents()
    assert ctrl.snapshots["a"].generation == snap.generation
    assert ctrl.engine.snapshot("a").fail_count == 1


def test_inplace_target_host_change_resets_stats():
    """Regrese B04: změnu mutable cíle pozná engine i při opakovaném předání objektu."""
    eng = MonitorEngine(FakePinger())
    t = PingTarget(name="Cíl", host="192.0.2.1", id="a")
    eng.set_targets([t])
    before = eng.snapshot(t.id)
    t.host = "198.51.100.1"
    eng.set_targets([t])
    after = eng.snapshot(t.id)
    assert before is not None and after is not None
    assert before.generation != after.generation and after.host == t.host


@pytest.mark.parametrize("fail_first", [False, True])
@pytest.mark.parametrize("setting", ["external_api", "adapter_name", "isp_provider"])
def test_wan_config_aba_requeues_latest_request(ctrl, monkeypatch, fail_first, setting):
    """Regrese B09: po busy WAN úloze (i chybě) se změna konfigurace neztratí."""
    entered, release, fresh = threading.Event(), threading.Event(), threading.Event()
    calls, updates = [], []
    ctrl.wan_updated.connect(updates.append)
    ctrl.cfg.network.isp_detection = False

    def fetch():
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
            if fail_first:
                raise RuntimeError("syntetická chyba WAN")
            return WanResult("192.0.2.10", ts=time.time())
        fresh.set()
        return WanResult("198.51.100.10", ts=time.time())

    monkeypatch.setattr(cmod, "fetch_wan_ip", fetch)
    cfg = ctrl.cfg.copy()
    cfg.network.external_api = True
    ctrl.update_config(cfg, save=False)
    try:
        assert entered.wait(3)
        cfg = ctrl.cfg.copy()
        old = getattr(cfg.network, setting)
        setattr(cfg.network, setting, {"external_api": False, "adapter_name": "Test",
                                      "isp_provider": "ripestat"}[setting])
        ctrl.update_config(cfg, save=False)
        cfg = ctrl.cfg.copy()
        setattr(cfg.network, setting, old)
        ctrl.update_config(cfg, save=False)
        release.set()
        deadline = time.monotonic() + 3
        while ctrl.wan.current is None and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            fresh.wait(0.01)
        assert len(calls) == 2
        assert ctrl.wan.current == "198.51.100.10"
        assert all(u["ip"] != "192.0.2.10" for u in updates)
    finally:
        release.set()


def test_stale_isp_job_cannot_save_cache(ctrl, monkeypatch):
    """Regrese B09: ISP doběhlé po vypnutí nesmí uložit cache ani stav WAN."""
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(cmod, "fetch_wan_ip", lambda: WanResult("192.0.2.10", ts=time.time()))

    def isp(*args):
        entered.set()
        assert release.wait(3)
        return "AS64500 Test"

    monkeypatch.setattr("netmonitor.core.wan_info.ISP_PROVIDERS", {"ipinfo": isp})
    ctrl._bg.shutdown(wait=True)
    ctrl._bg = ThreadPoolExecutor(max_workers=1)
    cfg = ctrl.cfg.copy()
    cfg.network.external_api = True
    ctrl.update_config(cfg, save=False)
    try:
        assert entered.wait(3)
        cfg = ctrl.cfg.copy()
        cfg.network.external_api = False
        ctrl.update_config(cfg, save=False)
        release.set()
        ctrl._bg.submit(lambda: None).result(timeout=3)
        QCoreApplication.processEvents()
        assert ctrl.wan.isp_cache == {} and ctrl.wan.current is None
        assert not ctrl.wan.state_file.exists()
    finally:
        release.set()


def test_current_wan_and_isp_result_is_saved(ctrl, monkeypatch):
    """Regrese B09: ochrana generace zachová úspěšné WAN/ISP, cache i oznámení změny."""
    ctrl.wan.current = "192.0.2.10"
    monkeypatch.setattr(cmod, "fetch_wan_ip", lambda: WanResult("198.51.100.10", ts=time.time()))
    monkeypatch.setattr("netmonitor.core.wan_info.ISP_PROVIDERS",
                        {"ipinfo": lambda *args: "AS64500 Test"})
    updates = []
    ctrl.wan_updated.connect(updates.append)
    cfg = ctrl.cfg.copy()
    cfg.network.external_api = True
    ctrl.update_config(cfg, save=False)
    deadline = time.monotonic() + 3
    while not updates and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        threading.Event().wait(0.01)
    assert updates and updates[-1]["ip"] == "198.51.100.10"
    assert updates[-1]["isp"] == "AS64500 Test"
    assert ctrl.wan.previous == "192.0.2.10" and ctrl._last_wan_ok
    wait_background(ctrl)
    saved = json.loads(ctrl.wan.state_file.read_text(encoding="utf-8"))
    assert saved["current"] == "198.51.100.10"
    assert saved["isp_cache"]["198.51.100.10"][0] == "AS64500 Test"
    assert [n.kind for n in ctrl.notifier.tick()] == ["wan_change"]


@pytest.mark.parametrize("force_isp,network_change", [(False, False), (True, False), (False, True)])
def test_busy_wan_requeues_only_for_forced_refresh_or_network_change(ctrl, monkeypatch, force_isp, network_change):
    """Regrese review L27: běžný busy refresh nesmí zdvojit externí dotazy."""
    entered, release = threading.Event(), threading.Event()
    calls = []

    def fetch():
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        return WanResult(None, error="syntetický offline stav", ts=time.time())

    monkeypatch.setattr(cmod, "fetch_wan_ip", fetch)
    cfg = ctrl.cfg.copy()
    cfg.network.external_api = True
    ctrl.update_config(cfg, save=False)
    try:
        assert entered.wait(3)
        if network_change:
            ctrl.network = NetworkSnapshot(active=None)
            ctrl._on_network(NetworkSnapshot(active=InterfaceInfo(name="Test", display_name="Test")))
        for _ in range(5):
            ctrl.refresh_wan(force_isp=force_isp)
        release.set()
        wait_background(ctrl)
        assert len(calls) == (2 if force_isp or network_change else 1)
    finally:
        release.set()


def test_isp_cache_hours_change_does_not_refresh_wan(ctrl, monkeypatch):
    """Regrese review L27: spinbox stáří cache nevyvolává síťový refresh ani ISP lookup."""
    calls = []
    ctrl.cfg.network.external_api = True
    monkeypatch.setattr(ctrl, "refresh_network", lambda: calls.append("network"))
    monkeypatch.setattr(ctrl, "refresh_wan", lambda **kwargs: calls.append("wan"))
    for hours in (25, 26, 27):
        cfg = ctrl.cfg.copy()
        cfg.network.isp_cache_hours = hours
        ctrl.update_config(cfg, save=False)
    assert calls == []


def test_stale_engine_callback_does_not_record_history(ctrl, monkeypatch):
    """Regrese review L27: callback staré generace nesmí zapsat měření do historie."""
    stale = probe(ctrl)
    QCoreApplication.processEvents()
    calls = []
    monkeypatch.setattr(ctrl.history, "record", lambda *args: calls.append(args))
    original = ctrl.cfg.copy()
    change(ctrl, "remove")
    ctrl.update_config(original, save=False)
    ctrl._engine_result(stale, stale.last)
    assert calls == []
    probe(ctrl)
    assert len(calls) == 1, "aktuální generace se musí dál ukládat"


def test_wan_saves_copies_in_background_and_keeps_latest_state(ctrl, monkeypatch):
    """Regrese review L27: ani změna IP, ani ISP cache nesmí ukládat na GUI vlákně."""
    entered, release = threading.Event(), threading.Event()
    saved, threads = [], []
    gui_thread = threading.get_ident()
    original = cmod.WanState.save

    def save(state):
        if state.state_file is None:
            return original(state)
        threads.append(threading.get_ident())
        if len(threads) == 1:
            entered.set()
            assert release.wait(3)
        saved.append((state.current, state.isp_cache.copy()))
        original(state)

    monkeypatch.setattr(cmod.WanState, "save", save)
    ctrl.cfg.network.external_api = True
    generation = ctrl._wan_generation

    def complete(ip, name):
        ctrl._on_wan((WanResult(ip, ts=time.time()), name, {ip: (name, time.time())}), generation)

    # Nová IP (save uvnitř update), pak dvě změny cache při stejném IP.
    complete("192.0.2.10", "AS64500 První")
    try:
        assert entered.wait(3)
        assert threads == [threads[0]] and threads[0] != gui_thread
        complete("192.0.2.10", "AS64500 Druhý")
        complete("192.0.2.10", "AS64500 Poslední")
        release.set()
        wait_background(ctrl)
        assert all(t != gui_thread for t in threads)
        assert saved[0][1]["192.0.2.10"][0] == "AS64500 První"
        assert saved[-1][1]["192.0.2.10"][0] == "AS64500 Poslední"
        state = json.loads(ctrl.wan.state_file.read_text(encoding="utf-8"))
        assert state["isp_cache"]["192.0.2.10"][0] == "AS64500 Poslední"
    finally:
        release.set()
