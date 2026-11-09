"""Historie měření v lokální SQLite.

Efektivita zápisu: jednotlivé pingy (každé 2 s × N cílů) se NEukládají řádek po
řádku. Agregují se v paměti do minutových buněk (min/avg/max, počty OK/FAIL,
poslední stav/chyba) a zapisují se dávkově (výchozí 1× za 60 s) jedním
transakčním UPSERTem. Posledních ~60 minut surových vzorků drží paměťový
ring-buffer pro detailní graf "1 hodina".

Události (výpadek / obnovení / změna WAN / internet) se ukládají okamžitě
do samostatné tabulky – odděleně od agregovaných statistik.

Migrace schématu: PRAGMA user_version + seznam MIGRATIONS.
"""
from __future__ import annotations

import csv
import logging
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from pathlib import Path
from statistics import median

from .ping_engine import PingResult

log = logging.getLogger(__name__)

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE IF NOT EXISTS samples_1m (
        target_id TEXT NOT NULL,
        ts INTEGER NOT NULL,            -- začátek minuty (unix epoch, UTC)
        n_ok INTEGER NOT NULL DEFAULT 0,
        n_fail INTEGER NOT NULL DEFAULT 0,
        rtt_min REAL, rtt_avg REAL, rtt_max REAL,
        last_status TEXT, last_error TEXT,
        PRIMARY KEY (target_id, ts)
    ) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        target_id TEXT,
        kind TEXT NOT NULL,             -- down|up|wan_change|internet_down|internet_up|high_latency
        status TEXT, detail TEXT, duration_s REAL
    );
    """,
    # v2
    """
    CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
    CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples_1m(ts);
    CREATE TABLE IF NOT EXISTS targets (
        id TEXT PRIMARY KEY, name TEXT, host TEXT, updated_at REAL
    );
    """,
]
SCHEMA_VERSION = len(MIGRATIONS)


@dataclass
class _Bucket:
    n_ok: int = 0
    n_fail: int = 0
    rtt_min: float | None = None
    rtt_max: float | None = None
    rtt_sum: float = 0.0
    last_status: str = ""
    last_error: str = ""

    def add(self, r: PingResult) -> None:
        self.last_status = r.status.value
        self.last_error = r.error[:200]
        if r.ok and r.rtt_ms is not None:
            self.n_ok += 1
            self.rtt_sum += r.rtt_ms
            self.rtt_min = r.rtt_ms if self.rtt_min is None else min(self.rtt_min, r.rtt_ms)
            self.rtt_max = r.rtt_ms if self.rtt_max is None else max(self.rtt_max, r.rtt_ms)
        else:
            self.n_fail += 1


@dataclass(frozen=True)
class SeriesPoint:
    ts: float
    rtt_min: float | None
    rtt_avg: float | None
    rtt_max: float | None
    n_ok: int
    n_fail: int
    bucket_s: float = 60.0

    @property
    def center_ts(self) -> float:
        return self.ts + self.bucket_s / 2

    @property
    def loss_pct(self) -> float:
        n = self.n_ok + self.n_fail
        return 100.0 * self.n_fail / n if n else 0.0


def series_segments(points: list[SeriesPoint]) -> list[list[SeriesPoint]]:
    """Úseky grafu bez výpadků a mezer, podle šířek obou sousedních bodů.

    U stejného rozlišení zůstává tolerance 2,5 kroku; u DB → raw se
    porovnávají středy s 1,25násobkem součtu obou kroků.
    """
    segments: list[list[SeriesPoint]] = []
    current: list[SeriesPoint] = []
    for point in points:
        gap = (bool(current) and point.center_ts - current[-1].center_ts >
               1.25 * (current[-1].bucket_s + point.bucket_s))
        if point.rtt_avg is None or gap:
            if current:
                segments.append(current)
            current = []
        if point.rtt_avg is not None:
            current.append(point)
    if current:
        segments.append(current)
    return segments


@dataclass(frozen=True)
class Event:
    ts: float
    target_id: str | None
    kind: str
    status: str | None
    detail: str | None
    duration_s: float | None


def migrate(conn: sqlite3.Connection) -> int:
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    for i in range(ver, SCHEMA_VERSION):
        log.info("Migrace DB historie v%d -> v%d", i, i + 1)
        with conn:
            conn.executescript(MIGRATIONS[i])
            conn.execute(f"PRAGMA user_version = {i + 1}")
    return conn.execute("PRAGMA user_version").fetchone()[0]


class HistoryStore:
    def __init__(self, path: Path | str, raw_window_s: int = 3600):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self.version = migrate(self._conn)
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, int], _Bucket] = defaultdict(_Bucket)
        self._raw: dict[str, deque[tuple[float, float | None, str]]] = defaultdict(lambda: deque(maxlen=4000))
        self.raw_window_s = raw_window_s
        self.enabled = True

    # -- zápis -----------------------------------------------------------
    def record(self, target_id: str, r: PingResult) -> None:
        if not self.enabled:
            return
        minute = int(r.ts // 60 * 60)
        with self._lock:
            self._pending[(target_id, minute)].add(r)
            self._raw[target_id].append((r.ts, r.rtt_ms if r.ok else None, r.status.value))

    def flush(self) -> int:
        with self._lock:
            return self._flush_locked()

    def _flush_locked(self) -> int:
        """Zapíše pending vzorky; volající drží _lock, zámek znovu nezískává."""
        pending, self._pending = self._pending, defaultdict(_Bucket)
        if not pending:
            return 0
        rows = [(tid, ts, b.n_ok, b.n_fail, b.rtt_min, (b.rtt_sum / b.n_ok) if b.n_ok else None,
                 b.rtt_max, b.last_status, b.last_error) for (tid, ts), b in pending.items()]
        try:
            with self._conn:
                self._conn.executemany(
                    """INSERT INTO samples_1m(target_id, ts, n_ok, n_fail, rtt_min, rtt_avg, rtt_max,
                                             last_status, last_error)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(target_id, ts) DO UPDATE SET
                         rtt_avg = CASE WHEN excluded.n_ok = 0 THEN rtt_avg
                                        WHEN n_ok = 0 THEN excluded.rtt_avg
                                        ELSE (rtt_avg * n_ok + excluded.rtt_avg * excluded.n_ok)
                                             / (n_ok + excluded.n_ok) END,
                         rtt_min = CASE WHEN rtt_min IS NULL THEN excluded.rtt_min
                                        WHEN excluded.rtt_min IS NULL THEN rtt_min
                                        ELSE min(rtt_min, excluded.rtt_min) END,
                         rtt_max = CASE WHEN rtt_max IS NULL THEN excluded.rtt_max
                                        WHEN excluded.rtt_max IS NULL THEN rtt_max
                                        ELSE max(rtt_max, excluded.rtt_max) END,
                         n_ok = n_ok + excluded.n_ok,
                         n_fail = n_fail + excluded.n_fail,
                         last_status = excluded.last_status,
                         last_error = excluded.last_error""",
                    rows,
                )
        except sqlite3.Error as e:
            log.error("Zápis historie selhal: %s", e)
            for k, b in pending.items():   # vrátit zpět do bufferu, ať se data neztratí
                self._pending[k] = b
            return 0
        return len(rows)

    def add_event(self, kind: str, target_id: str | None = None, status: str | None = None,
                  detail: str | None = None, duration_s: float | None = None, ts: float | None = None) -> None:
        with self._lock:
            try:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO events(ts, target_id, kind, status, detail, duration_s) VALUES (?,?,?,?,?,?)",
                        (ts or time.time(), target_id, kind, status, detail, duration_s))
            except sqlite3.Error as e:
                log.error("Zápis události selhal: %s", e)

    def upsert_targets(self, targets) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO targets(id,name,host,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, host=excluded.host, updated_at=excluded.updated_at",
                [(t.id, t.name, t.host, time.time()) for t in targets])

    def purge(self, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        with self._lock, self._conn:
            n = self._conn.execute("DELETE FROM samples_1m WHERE ts < ?", (int(cutoff),)).rowcount
            n += self._conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
        return n

    # -- čtení -----------------------------------------------------------
    def raw_series(self, target_id: str, since: float) -> list[tuple[float, float | None, str]]:
        with self._lock:
            return [x for x in self._raw.get(target_id, ()) if x[0] >= since]

    def series(self, target_id: str, since: float, bucket_s: int = 60) -> list[SeriesPoint]:
        self.flush()
        bucket_s = max(60, int(bucket_s))
        with self._lock:
            return self._series_locked(target_id, since, bucket_s)

    def _series_locked(self, target_id: str, since: float, bucket_s: int) -> list[SeriesPoint]:
        """Čtení série; volající drží _lock."""
        cur = self._conn.execute(
            """SELECT (ts / ?) * ? AS b, min(rtt_min),
                      sum(rtt_avg * n_ok) / nullif(sum(CASE WHEN rtt_avg IS NOT NULL THEN n_ok END), 0),
                      max(rtt_max), sum(n_ok), sum(n_fail)
               FROM samples_1m WHERE target_id = ? AND ts >= ?
               GROUP BY b ORDER BY b""",
            (bucket_s, bucket_s, target_id, int(since)))
        return [SeriesPoint(float(r[0]), r[1], r[2], r[3], int(r[4] or 0), int(r[5] or 0), float(bucket_s))
                for r in cur]

    def hour_data(self, target_id: str, since: float) -> tuple[list[SeriesPoint], float, dict]:
        """Hodinový graf a souhrn ze stejného snapshotu, jediným SELECTem.

        Celou minutu nahradíme raw body jen při shodě počtů OK/FAIL. Minuta
        restartu (nebo oříznutí ring-bufferu) tak zůstane z DB, pokud RAM
        neobsahuje všechny její vzorky. Neztratí se starší data ani se nic
        nezapočítá dvakrát. Filtr období je stejný jako u summary().
        """
        with self._lock:
            self._flush_locked()
            minutes = self._series_locked(target_id, since, 60)
            raw = list(self._raw.get(target_id, ()))
        by_minute: dict[int, list[SeriesPoint]] = defaultdict(list)
        for ts, rtt, _status in raw:
            by_minute[int(ts // 60 * 60)].append(
                SeriesPoint(ts, rtt, rtt, rtt, int(rtt is not None), int(rtt is None)))
        points: list[SeriesPoint] = []
        raw_points: list[SeriesPoint] = []
        raw_minutes: set[int] = set()
        for minute in minutes:
            samples = by_minute.get(int(minute.ts), [])
            if (sum(p.n_ok for p in samples), sum(p.n_fail for p in samples)) == (minute.n_ok, minute.n_fail):
                points.extend(samples)
                raw_points.extend(samples)
                raw_minutes.add(int(minute.ts))
            else:
                points.append(minute)
        points.sort(key=lambda p: p.ts)
        raw_points.sort(key=lambda p: p.ts)
        # Medián brání tomu, aby dlouhá pauza v měření zvětšila šířku raw bodů
        # a zamaskovala skutečnou mezeru v grafu. Osamocený raw bod má šířku 1 s.
        steps = [b.ts - a.ts for a, b in zip(raw_points, raw_points[1:]) if b.ts > a.ts]
        bucket = max(1.0, float(median(steps))) if steps else (1.0 if raw_points else 60.0)
        points = [replace(p, bucket_s=bucket) if int(p.ts // 60 * 60) in raw_minutes else p for p in points]
        n_ok, n_fail = sum(p.n_ok for p in minutes), sum(p.n_fail for p in minutes)
        n = n_ok + n_fail
        rtt_count = sum(p.n_ok for p in minutes if p.rtt_avg is not None)
        summary = {
            "rtt_min": min((p.rtt_min for p in minutes if p.rtt_min is not None), default=None),
            "rtt_avg": (sum(p.rtt_avg * p.n_ok for p in minutes if p.rtt_avg is not None) / rtt_count
                        if rtt_count else None),
            "rtt_max": max((p.rtt_max for p in minutes if p.rtt_max is not None), default=None),
            "n_ok": n_ok, "n_fail": n_fail,
            "loss_pct": (100.0 * n_fail / n) if n else None,
        }
        return points, bucket, summary

    def events(self, since: float = 0, target_id: str | None = None, limit: int = 500) -> list[Event]:
        q = "SELECT ts, target_id, kind, status, detail, duration_s FROM events WHERE ts >= ?"
        args: list = [since]
        if target_id:
            q += " AND target_id = ?"
            args.append(target_id)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [Event(*r) for r in self._conn.execute(q, args)]

    def summary(self, target_id: str, since: float) -> dict:
        self.flush()
        with self._lock:
            r = self._conn.execute(
                """SELECT min(rtt_min), sum(rtt_avg*n_ok)/nullif(sum(CASE WHEN rtt_avg IS NOT NULL THEN n_ok END),0),
                          max(rtt_max), sum(n_ok), sum(n_fail)
                   FROM samples_1m WHERE target_id=? AND ts>=?""", (target_id, int(since))).fetchone()
        n_ok, n_fail = int(r[3] or 0), int(r[4] or 0)
        n = n_ok + n_fail
        return {"rtt_min": r[0], "rtt_avg": r[1], "rtt_max": r[2], "n_ok": n_ok, "n_fail": n_fail,
                "loss_pct": (100.0 * n_fail / n) if n else None}

    def export_csv(self, path: Path | str, since: float = 0, target_ids: list[str] | None = None,
                   names: dict[str, str] | None = None) -> int:
        self.flush()
        names = names or {}
        q = "SELECT target_id, ts, n_ok, n_fail, rtt_min, rtt_avg, rtt_max, last_status, last_error " \
            "FROM samples_1m WHERE ts >= ?"
        args: list = [int(since)]
        if target_ids:
            q += f" AND target_id IN ({','.join('?' * len(target_ids))})"
            args += target_ids
        q += " ORDER BY ts, target_id"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        with open(path, "w", newline="", encoding="utf-8-sig") as f:   # BOM => Excel správně zobrazí češtinu
            w = csv.writer(f, delimiter=";")
            w.writerow(["cas_utc", "cas_lokalni", "cil_id", "nazev", "ok", "chyb", "loss_pct",
                        "rtt_min_ms", "rtt_avg_ms", "rtt_max_ms", "posledni_stav", "posledni_chyba"])
            for tid, ts, ok, fail, mn, avg, mx, st, err in rows:
                n = ok + fail
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                            time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)), tid, names.get(tid, ""),
                            ok, fail, f"{100 * fail / n:.1f}" if n else "",
                            _fmt(mn), _fmt(avg), _fmt(mx), st or "", err or ""])
        return len(rows)

    def export_events_csv(self, path: Path | str, since: float = 0, names: dict[str, str] | None = None) -> int:
        names = names or {}
        evs = self.events(since, limit=1_000_000)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["cas_lokalni", "typ", "cil_id", "nazev", "stav", "detail", "delka_s"])
            for e in reversed(evs):
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.ts)), e.kind, e.target_id or "",
                            names.get(e.target_id or "", ""), e.status or "", e.detail or "",
                            f"{e.duration_s:.0f}" if e.duration_s is not None else ""])
        return len(evs)

    def close(self) -> None:
        try:
            self.flush()
        finally:
            with self._lock:
                self._conn.close()


def _fmt(v: float | None) -> str:
    return f"{v:.2f}" if v is not None else ""
