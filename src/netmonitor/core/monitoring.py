"""Plánovač pingů a statistiky cílů.

* Plánování na monotónních hodinách (fixed-rate, bez driftu).
* Paralelní běh v daemon poolu; souběh omezuje plánovač živým limitem.
* Na jeden cíl nikdy neběží dva požadavky současně (in-flight set).
* Pomalý/nedostupný cíl neblokuje ostatní; DNS může přesáhnout timeout pingu.
* Detekce probuzení z uspání (skok wall-clock) => okamžité přeplánování.
* Engine je čistý Python bez Qt (testovatelný); GUI se napojuje přes callbacky.
"""
from __future__ import annotations

import heapq
import logging
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Callable, Iterable

from ..utils.workers import DaemonPool
from .config import PingTarget
from .ping_engine import Pinger, PingResult, PingStatus, Resolver, ping_host

log = logging.getLogger(__name__)

LOSS_WINDOW = 100      # klouzavé okno pro packet loss %
MAX_CONCURRENT = 128   # horní mez normalizované konfigurace; workery vznikají až podle potřeby


@dataclass(frozen=True)
class TargetSnapshot:
    target_id: str
    name: str
    host: str
    last: PingResult | None
    up: bool | None             # None = zatím neměřeno / neznámé
    rtt_min: float | None
    rtt_max: float | None
    rtt_avg: float | None
    loss_pct: float | None      # z klouzavého okna
    loss_pct_total: float | None
    ok_count: int
    fail_count: int
    last_success_ts: float | None
    outage_start_ts: float | None
    outage_count: int
    last_outage_duration_s: float | None
    consecutive_fail: int

    @property
    def outage_duration_s(self) -> float | None:
        if self.outage_start_ts is None:
            return None
        return max(0.0, time.time() - self.outage_start_ts)


@dataclass(frozen=True)
class Transition:
    target_id: str
    name: str
    host: str
    up: bool
    ts: float
    outage_start_ts: float | None = None
    duration_s: float | None = None
    status: PingStatus | None = None
    error: str = ""


class TargetStats:
    """Statistiky jednoho cíle. Není thread-safe – volající drží zámek."""

    def __init__(self, target: PingTarget):
        self.target = target
        self.reset()

    def reset(self) -> None:
        self.last: PingResult | None = None
        self.up: bool | None = None
        self.rtt_min: float | None = None
        self.rtt_max: float | None = None
        self.rtt_sum = 0.0
        self.ok_count = 0
        self.fail_count = 0
        self.window: deque[bool] = deque(maxlen=LOSS_WINDOW)
        self.consecutive_fail = 0
        self.first_fail_ts: float | None = None
        self.last_success_ts: float | None = None
        self.outage_start_ts: float | None = None
        self.outage_count = 0
        self.last_outage_duration_s: float | None = None

    def add(self, r: PingResult) -> Transition | None:
        t = self.target
        self.last = r
        self.window.append(r.ok)
        if r.ok:
            self.ok_count += 1
            rtt = r.rtt_ms or 0.0
            self.rtt_sum += rtt
            self.rtt_min = rtt if self.rtt_min is None else min(self.rtt_min, rtt)
            self.rtt_max = rtt if self.rtt_max is None else max(self.rtt_max, rtt)
            self.consecutive_fail = 0
            self.first_fail_ts = None
            self.last_success_ts = r.ts
            if self.up is False:
                start = self.outage_start_ts
                dur = (r.ts - start) if start else None
                self.last_outage_duration_s = dur
                self.outage_start_ts = None
                self.up = True
                return Transition(t.id, t.name, t.host, True, r.ts, start, dur, r.status)
            self.up = True
            return None
        # selhání
        self.fail_count += 1
        self.consecutive_fail += 1
        if self.first_fail_ts is None:
            self.first_fail_ts = r.ts
        if self.up is not False and self.consecutive_fail >= t.retries:
            self.up = False
            self.outage_start_ts = self.first_fail_ts
            self.outage_count += 1
            return Transition(t.id, t.name, t.host, False, r.ts, self.outage_start_ts, None, r.status, r.error)
        return None

    def snapshot(self) -> TargetSnapshot:
        t = self.target
        total = self.ok_count + self.fail_count
        win = len(self.window)
        return TargetSnapshot(
            target_id=t.id, name=t.name, host=t.host, last=self.last, up=self.up,
            rtt_min=self.rtt_min, rtt_max=self.rtt_max,
            rtt_avg=(self.rtt_sum / self.ok_count) if self.ok_count else None,
            loss_pct=(100.0 * self.window.count(False) / win) if win else None,
            loss_pct_total=(100.0 * self.fail_count / total) if total else None,
            ok_count=self.ok_count, fail_count=self.fail_count,
            last_success_ts=self.last_success_ts, outage_start_ts=self.outage_start_ts,
            outage_count=self.outage_count, last_outage_duration_s=self.last_outage_duration_s,
            consecutive_fail=self.consecutive_fail,
        )


ResultCallback = Callable[[TargetSnapshot, PingResult], None]
TransitionCallback = Callable[[Transition], None]


class MonitorEngine:
    RESUME_JUMP_S = 15.0

    def __init__(self, pinger: Pinger, resolver: Resolver | None = None, *,
                 max_concurrent: int = 16,
                 on_result: ResultCallback | None = None,
                 on_transition: TransitionCallback | None = None,
                 on_resume: Callable[[], None] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.pinger = pinger
        self.resolver = resolver or Resolver()
        self.max_concurrent = max(1, min(MAX_CONCURRENT, int(max_concurrent)))
        self.on_result = on_result
        self.on_transition = on_transition
        self.on_resume = on_resume
        self._clock = clock
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._targets: dict[str, PingTarget] = {}
        self._stats: dict[str, TargetStats] = {}
        self._heap: list[tuple[float, int, str]] = []
        self._due: dict[str, float] = {}
        self._seq = 0
        self._inflight: set[str] = set()
        self._executor: DaemonPool | None = None
        self._retired_executors: list[DaemonPool] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = False

    # -- konfigurace --------------------------------------------------------
    def set_max_concurrent(self, limit: int) -> None:
        """Změní limit bez restartu; rozběhnuté sondy vždy doběhnou."""
        with self._cond:
            limit = max(1, min(MAX_CONCURRENT, int(limit)))
            if limit == self.max_concurrent:
                return
            self.max_concurrent = limit
            if self._executor is not None and not self._stop.is_set():
                old = self._executor
                self._executor = DaemonPool(max_workers=limit, thread_name_prefix="ping")
                self._retired_executors.append(old)
                old.shutdown(wait=False)  # bez rušení sond; stop() počká i na staré pooly
            self._cond.notify_all()

    def set_targets(self, targets: Iterable[PingTarget]) -> None:
        with self._lock:
            new = {t.id: t for t in targets if t.enabled and not getattr(t, 'is_section', False)}
            for tid in list(self._targets):
                if tid not in new:
                    self._targets.pop(tid)
                    self._stats.pop(tid, None)
                    self._due.pop(tid, None)
            now = self._clock()
            for i, (tid, t) in enumerate(new.items()):
                old = self._targets.get(tid)
                if old is None or old.host != t.host:
                    self._stats[tid] = TargetStats(t)
                    self._schedule(tid, now + 0.05 * i)   # mírné rozprostření startu
                else:
                    self._stats[tid].target = t
                    if old.interval_s != t.interval_s:
                        self._schedule(tid, now + min(t.interval_s, 0.2))
                self._targets[tid] = t
            self._cond.notify_all()

    def set_pinger(self, pinger: Pinger) -> None:
        with self._lock:
            self.pinger = pinger

    def _schedule(self, tid: str, due: float) -> None:
        self._seq += 1
        self._due[tid] = due
        heapq.heappush(self._heap, (due, self._seq, tid))

    def test_now(self, tid: str) -> bool:
        with self._lock:
            if tid not in self._targets:
                return False
            self._schedule(tid, self._clock())
            self._cond.notify_all()
            return True

    def reschedule_all(self) -> None:
        with self._lock:
            now = self._clock()
            for i, tid in enumerate(self._targets):
                self._schedule(tid, now + 0.05 * i)
            self._cond.notify_all()

    def reset_stats(self, tid: str | None = None) -> None:
        with self._lock:
            for k, s in self._stats.items():
                if tid is None or k == tid:
                    s.reset()

    def snapshot(self, tid: str) -> TargetSnapshot | None:
        with self._lock:
            s = self._stats.get(tid)
            return s.snapshot() if s else None

    def snapshots(self) -> dict[str, TargetSnapshot]:
        with self._lock:
            return {k: s.snapshot() for k, s in self._stats.items()}

    def pause(self, paused: bool) -> None:
        with self._lock:
            self._paused = paused
            if not paused:
                self.reschedule_all()

    # -- běh ---------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop = threading.Event()  # staré sondy zůstávají zastavené i po restartu
        self.reschedule_all()      # po (re)startu měřit hned, ne až po intervalu
        # Limit hlídá plánovač i pool, aby rychlé předávání kapacity nevytvářelo
        # další vlákna ještě před návratem předchozího workeru do poolu.
        self._executor = DaemonPool(max_workers=self.max_concurrent, thread_name_prefix="ping")
        self._thread = threading.Thread(target=self._loop, args=(self._stop,),
                                        name="ping-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        """Počká nejvýše timeout sekund včetně workerů s blokujícím resolverem."""
        deadline = time.monotonic() + max(0, timeout)
        self._stop.set()
        with self._lock:
            self._cond.notify_all()
        if self._thread:
            self._thread.join(max(0, deadline - time.monotonic()))
            if not self._thread.is_alive():
                self._thread = None
        with self._lock:
            executors = self._retired_executors
            self._retired_executors = []
            if self._executor is not None:
                executors.append(self._executor)
            self._executor = None
        for executor in executors:
            executor.shutdown(cancel_futures=True, timeout=max(0, deadline - time.monotonic()))
        # Nedokončené sondy zůstávají in-flight i při restartu, aby se nepřekryly.

    def _loop(self, stop: threading.Event) -> None:
        last_wall = time.time()
        last_mono = self._clock()
        while not stop.is_set():
            with self._lock:
                if stop.is_set():
                    return
                # detekce uspání/probuzení: wall clock poskočil víc než monotonic
                wall, mono = time.time(), self._clock()
                if (wall - last_wall) - (mono - last_mono) > self.RESUME_JUMP_S or (wall - last_wall) > 60:
                    log.info("Detekováno probuzení ze spánku – obnovuji monitoring")
                    self.resolver.clear()
                    self.reschedule_all()
                    if self.on_resume:
                        threading.Thread(target=self.on_resume, daemon=True).start()
                last_wall, last_mono = wall, mono

                now = self._clock()
                wait = 1.0
                while self._heap:
                    due, _, tid = self._heap[0]
                    if tid not in self._targets or self._due.get(tid) != due:
                        heapq.heappop(self._heap)   # zastaralý záznam
                        continue
                    if due > now:
                        wait = min(wait, due - now)
                        break
                    if not self._paused and tid not in self._inflight and len(self._inflight) >= self.max_concurrent:
                        break  # splatný cíl zůstane ve frontě do uvolnění kapacity
                    heapq.heappop(self._heap)
                    t = self._targets[tid]
                    nxt = due + t.interval_s
                    if nxt <= now:            # zpoždění (např. po spánku) – žádná dávka dohánění
                        nxt = now + t.interval_s
                    self._schedule(tid, nxt)
                    if self._paused or tid in self._inflight:
                        continue              # nepřekrývat požadavky na stejný cíl
                    self._inflight.add(tid)
                    try:
                        # Živá změna limitu vyměňuje pool pod stejným zámkem.
                        assert self._executor is not None
                        future = self._executor.submit(lambda target=t: self._probe(target, stop))
                        future.add_done_callback(lambda f, target_id=tid: self._probe_cancelled(target_id, f))
                    except RuntimeError:      # executor už se vypíná
                        self._inflight.discard(tid)
                        return
                self._cond.wait(timeout=max(0.01, wait))

    def _probe_cancelled(self, tid: str, future: Future) -> None:
        if future.cancelled():
            with self._lock:
                self._inflight.discard(tid)

    def _probe(self, t: PingTarget, stop: threading.Event) -> None:
        try:
            if stop.is_set():
                return
            r = ping_host(t.host, t.timeout_s, self.pinger, self.resolver)
            with self._lock:
                if stop.is_set():
                    return
                st = self._stats.get(t.id)
                if st is None or st.target.host != t.host:
                    return          # cíl mezitím odebrán/změněn
                tr = st.add(r)
                snap = st.snapshot()
            if stop.is_set():
                return
            if self.on_result:
                self.on_result(snap, r)
            if tr and self.on_transition:
                self.on_transition(tr)
        except Exception:  # noqa: BLE001
            log.exception("Chyba v ping workeru pro %s", t.host)
        finally:
            with self._lock:
                self._inflight.discard(t.id)
                self._cond.notify_all()
