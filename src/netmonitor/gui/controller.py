"""Propojení jádra s Qt: plánování úloh na pozadí, signály pro UI, ukládání konfigurace.

Hlavní Qt vlákno nikdy neprovádí síťové ani diskové operace – vše běží
v pozadí a výsledky se do UI doručují queued signály.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ..core import config as cfgmod
from ..core.config import AppConfig, PingTarget
from ..core.connection_status import ConnEvaluation, ConnInputs, ConnState, evaluate
from ..core.history import HistoryStore
from ..core.monitoring import MonitorEngine, TargetSnapshot, Transition
from ..core.network_info import NetworkInfoProvider, NetworkSnapshot
from ..core.notifier import Notification, Notifier
from ..core.ping_engine import PingResult, Resolver, select_pinger
from ..core.wan_info import WanState, fetch_wan_ip
from ..utils.paths import data_dir
from ..utils.validation import host_kind, is_private_address

log = logging.getLogger(__name__)

GATEWAY_ID = "__gateway__"


class Controller(QObject):
    target_result = Signal(object, object)      # TargetSnapshot, PingResult
    targets_changed = Signal()
    network_updated = Signal(object)            # NetworkSnapshot
    wan_updated = Signal(object)                # dict
    connection_changed = Signal(object)         # ConnEvaluation
    config_changed = Signal(object)             # AppConfig
    notification = Signal(object)               # Notification
    _deliver = Signal(object, object)

    def __init__(self, cfg: AppConfig, config_path=None, history_path=None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.config_path = config_path
        self._bg = ThreadPoolExecutor(max_workers=3, thread_name_prefix="bg")
        self._deliver.connect(self._on_deliver)
        self._busy: set[str] = set()
        self._stopping = False

        self.resolver = Resolver()
        self.pinger = select_pinger()
        log.info("ICMP implementace: %s", self.pinger.name)
        self.engine = MonitorEngine(self.pinger, self.resolver, max_concurrent=cfg.ping.max_concurrent,
                                    on_result=self._engine_result, on_transition=self._engine_transition,
                                    on_resume=self._engine_resume)
        self.history = HistoryStore(history_path or (data_dir() / "history.sqlite3"))
        self.history.enabled = cfg.history.enabled
        self.wan = WanState(data_dir() / "wan_state.json")
        self.notifier = Notifier(cfg.notifications)
        self.net_provider = NetworkInfoProvider()
        self.network: NetworkSnapshot | None = None
        self.isp: str | None = None
        self.evaluation = ConnEvaluation(ConnState.UNKNOWN, "Startuji…", {})
        self.gateway_snapshot: TargetSnapshot | None = None
        self.snapshots: dict[str, TargetSnapshot] = {}
        self._last_wan_ok = False

        self._save_timer = QTimer(self, singleShot=True, interval=600, timeout=self._save_now)
        self._eval_timer = QTimer(self, singleShot=True, interval=400, timeout=self._evaluate)
        self._net_timer = QTimer(self, timeout=self.refresh_network)
        self._wan_timer = QTimer(self, timeout=self.refresh_wan)
        self._flush_timer = QTimer(self, interval=60_000, timeout=self._flush_history)
        self._notify_timer = QTimer(self, interval=1000, timeout=self._notify_tick)
        self._purge_timer = QTimer(self, interval=6 * 3600 * 1000, timeout=self._purge)

    # ------------------------------------------------------------------ životní cyklus
    def start(self) -> None:
        self._apply_targets()
        self.engine.start()
        self._net_timer.start(self.cfg.network.refresh_s * 1000)
        self._wan_timer.start(self.cfg.network.wan_interval_s * 1000)
        self._flush_timer.start()
        self._notify_timer.start()
        self._purge_timer.start()
        self.refresh_network()
        self.refresh_wan()
        self._run_bg("purge", lambda: self.history.purge(self.cfg.history.retention_days))

    def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        for t in (self._net_timer, self._wan_timer, self._flush_timer, self._notify_timer, self._purge_timer,
                  self._eval_timer):
            t.stop()
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save_now()
        self.engine.stop()
        self._bg.shutdown(wait=True, cancel_futures=True)
        self.history.close()
        log.info("Monitoring korektně ukončen")

    # ------------------------------------------------------------------ pozadí
    def _run_bg(self, key: str, fn: Callable[[], Any], done: Callable[[Any], None] | None = None) -> None:
        """Spustí fn na pozadí; stejný klíč nikdy neběží dvakrát současně."""
        if self._stopping or key in self._busy:
            return
        self._busy.add(key)

        def job():
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001
                log.exception("Úloha %s selhala", key)
                res = e
            self._deliver.emit((key, done), res)
        try:
            self._bg.submit(job)
        except RuntimeError:
            self._busy.discard(key)

    @Slot(object, object)
    def _on_deliver(self, meta, res) -> None:
        key, done = meta
        if key.startswith("__"):
            if not self._stopping:
                self._handle_engine(key, res)
            return
        self._busy.discard(key)
        if done and not self._stopping and not isinstance(res, Exception):
            done(res)

    def _db(self, fn: Callable[[], Any]) -> None:
        """Jednorázový zápis do DB na pozadí (unikátní klíč – nikdy se nezahodí)."""
        self._seq = getattr(self, "_seq", 0) + 1
        self._run_bg(f"db-{self._seq}", fn)

    # ------------------------------------------------------------------ konfigurace
    def update_config(self, cfg: AppConfig, save: bool = True) -> None:
        old = self.cfg
        self.cfg = cfg.normalized()
        self.notifier.update_config(self.cfg.notifications)
        self.history.enabled = self.cfg.history.enabled
        if old.network.refresh_s != self.cfg.network.refresh_s:
            self._net_timer.setInterval(self.cfg.network.refresh_s * 1000)
        if old.network.wan_interval_s != self.cfg.network.wan_interval_s:
            self._wan_timer.setInterval(self.cfg.network.wan_interval_s * 1000)
        net_changed = (old.network.adapter_auto, old.network.adapter_name, old.network.external_api,
                       old.network.isp_detection, old.network.isp_provider) != \
                      (self.cfg.network.adapter_auto, self.cfg.network.adapter_name, self.cfg.network.external_api,
                       self.cfg.network.isp_detection, self.cfg.network.isp_provider)
        self._apply_targets()
        if net_changed:
            self.refresh_network()
            self.refresh_wan(force_isp=True)
        self.config_changed.emit(self.cfg)
        if save:
            self._save_timer.start()

    def schedule_save(self) -> None:
        """Uloží konfiguraci (debounce) bez přestavby UI – např. po přesunu okna."""
        if not self._stopping:
            self._save_timer.start()

    def _save_now(self) -> None:
        cfg = self.cfg.copy()
        self._run_bg("save-config", lambda: cfgmod.save_config(cfg, self.config_path))

    def _apply_targets(self) -> None:
        targets = self.cfg.real_targets()
        gw = self.network.active.gateway if self.network and self.network.active else None
        if self.cfg.network.gateway_probe and gw:
            targets.append(PingTarget(name="Brána", host=gw, interval_s=5, timeout_s=1, retries=3, id=GATEWAY_ID))
        self.engine.set_targets(targets)
        ids = {t.id for t in self.cfg.real_targets()}
        self.snapshots = {k: v for k, v in self.snapshots.items() if k in ids}
        self._run_bg("targets-db", lambda: self.history.upsert_targets(self.cfg.real_targets()))
        self.targets_changed.emit()
        self._eval_timer.start()

    def apply_ip(self, settings, done: Callable[[tuple], None]) -> None:
        """Použije nastavení IP na pozadí (UAC / polkit / heslo správce), pak obnoví síťové údaje."""
        from .. import platform as plat

        def job():
            try:
                return plat.current().apply_ip(settings)
            except Exception as e:  # noqa: BLE001
                log.exception("Nastavení IP selhalo")
                return False, str(e)

        def finished(res):
            ok, out = res
            log.info("Nastavení IP (%s): %s", "OK" if ok else "CHYBA", settings.summary().replace("\n", "; "))
            if ok:
                self._db(lambda: self.history.add_event("ip_config", None, None,
                                                        settings.summary().replace("\n", "; ")))
                self.resolver.clear()
                for delay in (1500, 5000, 12000):      # DHCP / změna adresy chvíli trvá
                    QTimer.singleShot(delay, self.refresh_network)
                QTimer.singleShot(6000, self.refresh_wan)
                QTimer.singleShot(2000, self.engine.reschedule_all)
            done(res)
        self._run_bg("ip-apply", job, finished)

    def test_now(self, target_id: str) -> None:
        self.engine.test_now(target_id)

    # ------------------------------------------------------------------ engine callbacks (worker vlákna!)
    def _engine_result(self, snap: TargetSnapshot, r: PingResult) -> None:
        if snap.target_id != GATEWAY_ID:
            self.history.record(snap.target_id, r)
        self._deliver.emit(("__result__", None), (snap, r))

    def _engine_transition(self, tr: Transition) -> None:
        self._deliver.emit(("__transition__", None), tr)

    def _engine_resume(self) -> None:
        self._deliver.emit(("__resume__", None), None)

    def _handle_engine(self, key: str, payload) -> bool:
        if key == "__result__":
            snap, r = payload
            if snap.target_id == GATEWAY_ID:
                self.gateway_snapshot = snap
            else:
                self.snapshots[snap.target_id] = snap
                self.notifier.on_latency(snap.target_id, snap.name, r.rtt_ms if r.ok else None)
                self.target_result.emit(snap, r)
            if not self._eval_timer.isActive():
                self._eval_timer.start()
            return True
        if key == "__transition__":
            tr: Transition = payload
            if tr.target_id != GATEWAY_ID:
                self.notifier.on_target_transition(tr)
                self._db(lambda: self.history.add_event(
                    "up" if tr.up else "down", tr.target_id, tr.status.value if tr.status else None,
                    tr.error or None, tr.duration_s, tr.ts))
            return True
        if key == "__resume__":
            log.info("Probuzení: obnovuji síťové informace a WAN")
            self.refresh_network()
            self.refresh_wan()
            return True
        return False

    # ------------------------------------------------------------------ síť
    def refresh_network(self) -> None:
        n = self.cfg.network
        pref = None if n.adapter_auto or not n.adapter_name else n.adapter_name
        self._run_bg("network", lambda: self.net_provider.snapshot(pref), self._on_network)

    def _on_network(self, snap: NetworkSnapshot) -> None:
        old = self.network
        self.network = snap
        if old is None or old.signature() != snap.signature():
            if old is not None:
                log.info("Změna sítě: %s -> %s", old.signature(), snap.signature())
                self.resolver.clear()
                self.engine.reschedule_all()
                self.refresh_wan()
            old_gw = old.active.gateway if old and old.active else None
            new_gw = snap.active.gateway if snap.active else None
            if old_gw != new_gw:
                self.gateway_snapshot = None
                self._apply_targets()
        self.network_updated.emit(snap)
        self._eval_timer.start()

    def refresh_wan(self, force_isp: bool = False) -> None:
        n = self.cfg.network
        if not n.external_api:
            self.wan_updated.emit(self.wan_info_dict(disabled=True))
            return

        def job():
            r = fetch_wan_ip()
            isp = None
            if r.ip and n.isp_detection:
                if force_isp:
                    self.wan.isp_cache.pop(r.ip, None)
                isp = self.wan.isp_for(r.ip, n.isp_provider, n.isp_cache_hours * 3600)
            return r, isp
        self._run_bg("wan", job, self._on_wan)

    def _on_wan(self, res) -> None:
        r, isp = res
        self._last_wan_ok = bool(r.ip)
        old = self.wan.current
        changed = self.wan.update(r)
        if r.ip:
            self.isp = isp if self.cfg.network.isp_detection else None
        if changed and r.ip:
            self.notifier.on_wan_change(old, r.ip)
            self._db(lambda: self.history.add_event("wan_change", None, None, f"{old} -> {r.ip}"))
        self.wan_updated.emit(self.wan_info_dict())
        self._eval_timer.start()

    def wan_info_dict(self, disabled: bool = False) -> dict:
        return {"disabled": disabled, "ip": None if disabled else self.wan.current,
                "previous": self.wan.previous, "changed_at": self.wan.changed_at,
                "last_check": self.wan.last_check, "error": self.wan.last_error,
                "isp": None if disabled or not self.cfg.network.isp_detection else self.isp}

    # ------------------------------------------------------------------ stav připojení
    def is_internet_target(self, t: PingTarget, snap: TargetSnapshot | None) -> bool:
        """Cíl se počítá do stavu internetu, pokud je veřejný (doména s veřejnou IP nebo veřejná IP)."""
        if t.internet_probe != "auto":
            return t.internet_probe == "yes"
        if host_kind(t.host) == "domain":
            addr = snap.last.address if snap and snap.last and snap.last.address else None
            return addr is None or not is_private_address(addr)
        return not is_private_address(t.host)

    def _evaluate(self) -> None:
        net = self.network
        a = net.active if net else None
        ups, rtts, losses = [], [], []
        for t in self.cfg.real_targets():
            if not t.enabled:
                continue
            s = self.snapshots.get(t.id)
            if not self.is_internet_target(t, s):
                continue
            ups.append(s.up if s else None)
            if s and s.up and s.last and s.last.ok and s.last.rtt_ms is not None:
                rtts.append(s.last.rtt_ms)
            if s and s.loss_pct is not None and s.ok_count + s.fail_count >= 5:
                losses.append(s.loss_pct)
        gw = self.gateway_snapshot.up if self.gateway_snapshot else None
        http_ok = None
        if self.cfg.network.external_api and self.wan.last_check and time.time() - self.wan.last_check < 90:
            http_ok = self._last_wan_ok
        ev = evaluate(ConnInputs(
            iface_up=None if net is None else bool(a and a.is_up and a.ipv4),
            has_default_route=None if net is None else net.has_default_route,
            gateway_up=gw, internet_up=ups, internet_rtts=rtts, internet_loss=losses,
            high_latency_ms=self.cfg.ping.thresholds.orange_max_ms, http_ok=http_ok))
        # všechny ICMP cíle mlčí => ověř internet i přes HTTPS (max. 1× za minutu)
        if (ups and not any(ups) and any(x is False for x in ups) and self.cfg.network.external_api
                and time.time() - (self.wan.last_check or 0) > 60):
            self.refresh_wan()
        prev = self.evaluation.state
        self.evaluation = ev
        if ev.state != prev:
            log.info("Stav připojení: %s -> %s (%s)", prev.value, ev.state.value, ev.reason)
            self.notifier.on_internet_state(ev.state)
            kind = None
            if ev.state is ConnState.OFFLINE:
                kind = "internet_down"
            elif prev is ConnState.OFFLINE and ev.state in (ConnState.ONLINE, ConnState.DEGRADED):
                kind = "internet_up"
            if kind:
                self._db(lambda: self.history.add_event(kind, None, ev.state.value, ev.reason))
        self.connection_changed.emit(ev)

    # ------------------------------------------------------------------ periodické
    def _flush_history(self) -> None:
        self._run_bg("flush", self.history.flush)

    def _purge(self) -> None:
        self._run_bg("purge", lambda: self.history.purge(self.cfg.history.retention_days))

    def _notify_tick(self) -> None:
        for n in self.notifier.tick():
            self.notification.emit(n)

    # ------------------------------------------------------------------ autostart
    def autostart_command(self) -> list[str]:
        # AppImage mount i jeho sys.executable jsou dočasné; po přihlášení spouštíme původní soubor.
        frozen = getattr(sys, "frozen", False)
        if sys.platform == "linux" and frozen:
            appimage = os.environ.get("APPIMAGE")
            appdir = os.environ.get("APPDIR")
            # Proměnné mohou být zděděné z cizí AppImage; ověříme příslušnost procesu.
            if (appimage and appdir and os.path.isabs(appimage) and Path(appimage).is_file()
                    and Path(sys.executable).resolve().is_relative_to(Path(appdir).resolve())):
                return [appimage, "--minimized"] if self.cfg.general.start_minimized else [appimage]
        if frozen:
            return [sys.executable, "--minimized"] if self.cfg.general.start_minimized else [sys.executable]
        exe = sys.executable
        if sys.platform == "win32" and exe.lower().endswith("python.exe"):
            exe = exe[:-10] + "pythonw.exe"
        return [exe, "-m", "netmonitor"]

