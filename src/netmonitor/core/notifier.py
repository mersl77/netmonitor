"""Logika lokálních upozornění – debounce (prodleva), cooldown, žádné zahlcení.

Čistá logika bez Qt; GUI volá `tick()` (1× za sekundu) a doručené
`Notification` zobrazí přes systémovou oblast (tray), bez ní ve widgetu a v logu.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .config import NotificationConfig
from .connection_status import ConnState
from .monitoring import Transition


@dataclass(frozen=True)
class Notification:
    kind: str          # down|up|wan_change|internet_down|internet_up|high_latency
    key: str
    title: str
    message: str
    critical: bool = False


class Notifier:
    def __init__(self, cfg: NotificationConfig, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.clock = clock
        self._pending: dict[tuple[str, str], tuple[float, Notification]] = {}
        self._sent_down: set[str] = set()         # klíče, u nichž bylo odesláno "down"
        self._last_sent: dict[tuple[str, str], float] = {}
        self._latency_since: dict[str, float] = {}
        self._latency_notified: set[str] = set()
        self._internet_state: ConnState | None = None

    def update_config(self, cfg: NotificationConfig) -> None:
        self.cfg = cfg

    # -- vstupy -----------------------------------------------------------
    def on_target_transition(self, tr: Transition) -> None:
        key = f"t:{tr.target_id}"
        if not tr.up:
            if self.cfg.target_down:
                n = Notification("down", key, f"{tr.name} nedostupný",
                                 f"{tr.host} neodpovídá ({_status_text(tr)})", critical=True)
                self._defer(n)
        else:
            had = self._pending.pop((key, "down"), None)
            if had is None and key in self._sent_down and self.cfg.target_up:
                dur = f" po {fmt_duration(tr.duration_s)}" if tr.duration_s else ""
                self._emit_now(Notification("up", key, f"{tr.name} opět dostupný", f"{tr.host} odpovídá{dur}"))
            self._sent_down.discard(key)

    def on_internet_state(self, state: ConnState) -> None:
        prev, self._internet_state = self._internet_state, state
        key = "internet"
        if state is ConnState.OFFLINE and prev is not ConnState.OFFLINE:
            if self.cfg.internet_down:
                self._defer(Notification("internet_down", key, "Výpadek internetu",
                                         "Žádný internetový cíl neodpovídá", critical=True))
        elif state in (ConnState.ONLINE, ConnState.DEGRADED) and prev is ConnState.OFFLINE:
            had = self._pending.pop((key, "internet_down"), None)
            if had is None and key in self._sent_down and self.cfg.internet_up:
                self._emit_now(Notification("internet_up", key, "Internet obnoven", "Připojení k internetu funguje"))
            self._sent_down.discard(key)

    def on_wan_change(self, old: str | None, new: str) -> None:
        if self.cfg.wan_change:
            self._emit_now(Notification("wan_change", "wan", "Změna veřejné IP", f"{old or '?'} → {new}"))

    def on_latency(self, target_id: str, name: str, rtt_ms: float | None) -> None:
        now = self.clock()
        if rtt_ms is None or rtt_ms <= self.cfg.high_latency_ms:
            self._latency_since.pop(target_id, None)
            self._latency_notified.discard(target_id)
            return
        since = self._latency_since.setdefault(target_id, now)
        if (self.cfg.high_latency and target_id not in self._latency_notified
                and now - since >= self.cfg.high_latency_duration_s):
            self._latency_notified.add(target_id)
            self._emit_now(Notification("high_latency", f"t:{target_id}", f"{name}: vysoká latence",
                                        f"Odezva přes {self.cfg.high_latency_ms:.0f} ms déle než "
                                        f"{fmt_duration(now - since)} (nyní {rtt_ms:.0f} ms)"))

    # -- výstup -----------------------------------------------------------
    def _defer(self, n: Notification) -> None:
        self._pending.setdefault((n.key, n.kind), (self.clock() + self.cfg.delay_s, n))

    def _emit_now(self, n: Notification) -> None:
        self._pending[(n.key, n.kind)] = (self.clock(), n)

    def tick(self) -> list[Notification]:
        if not self._pending:
            return []
        now = self.clock()
        out = []
        for k, (due, n) in list(self._pending.items()):
            if due > now:
                continue
            del self._pending[k]
            if not self.cfg.enabled:
                continue
            last = self._last_sent.get(k)
            if last is not None and now - last < self.cfg.cooldown_s and n.kind not in ("up", "internet_up"):
                continue
            self._last_sent[k] = now
            if n.kind in ("down", "internet_down"):
                self._sent_down.add(n.key)
            out.append(n)
        return out


def _status_text(tr: Transition) -> str:
    if tr.status is None:
        return "chyba"
    return {"timeout": "TIMEOUT", "host_unreachable": "host nedostupný", "net_unreachable": "síť nedostupná",
            "dns_error": "chyba DNS", "error": tr.error or "chyba"}.get(tr.status.value, tr.status.value)


def fmt_duration(s: float | None) -> str:
    if s is None:
        return "–"
    s = int(s)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60} s"
    if s < 86400:
        return f"{s // 3600} h {(s % 3600) // 60} min"
    return f"{s // 86400} d {(s % 86400) // 3600} h"
