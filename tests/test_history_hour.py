"""Regrese B08: hodinový graf kombinuje trvalou historii a detailní vzorky."""
import pytest

from netmonitor.core.history import HistoryStore, SeriesPoint, series_segments
from netmonitor.core.ping_engine import PingResult, PingStatus


BASE = 1_800_000_000  # začátek minuty, syntetický čas


def record(store, ts, ms):
    store.record("t", PingResult(PingStatus.OK if ms is not None else PingStatus.TIMEOUT,
                                rtt_ms=ms, ts=ts))


def assert_summary(points, summary):
    assert sum(p.n_ok for p in points) == summary["n_ok"]
    assert sum(p.n_fail for p in points) == summary["n_fail"]
    values = [p for p in points if p.rtt_avg is not None]
    if values:
        assert min(p.rtt_min for p in values) == pytest.approx(summary["rtt_min"])
        assert max(p.rtt_max for p in values) == pytest.approx(summary["rtt_max"])
        assert sum(p.rtt_avg * p.n_ok for p in values) / summary["n_ok"] == pytest.approx(summary["rtt_avg"])


def test_hour_data_keeps_history_after_restart(tmp_path):
    """Regrese B08: bod před 30 minutami nezmizí při příchodu dvou raw bodů."""
    path = tmp_path / "history.db"
    old = HistoryStore(path)
    record(old, BASE - 1800 + 10, 40.0)
    old.close()
    store = HistoryStore(path)
    try:
        record(store, BASE + 10, 10.0)
        record(store, BASE + 12, 20.0)
        points, bucket, summary = store.hour_data("t", BASE - 3600)
        assert [p.ts for p in points] == [BASE - 1800, BASE + 10, BASE + 12]
        assert [p.rtt_avg for p in points] == [40.0, 10.0, 20.0]
        assert bucket == 2.0
        assert summary == pytest.approx(store.summary("t", BASE - 3600))
        assert summary["n_ok"] == 3
        assert_summary(points, summary)
    finally:
        store.close()


def test_hour_data_preserves_partial_boundary_minute(tmp_path):
    """Regrese B08: DB zahrne celou minutu restartu, raw ji nesmí započíst podruhé."""
    path = tmp_path / "history.db"
    old = HistoryStore(path)
    record(old, BASE + 5, 50.0)
    old.close()
    store = HistoryStore(path)
    try:
        record(store, BASE + 10, 10.0)
        record(store, BASE + 12, None)
        record(store, BASE + 61, 20.0)
        record(store, BASE + 63, 30.0)
        points, bucket, summary = store.hour_data("t", BASE - 3600)
        assert points == [SeriesPoint(BASE, 10.0, 30.0, 50.0, 2, 1),
                          SeriesPoint(BASE + 61, 20.0, 20.0, 20.0, 1, 0, 2.0),
                          SeriesPoint(BASE + 63, 30.0, 30.0, 30.0, 1, 0, 2.0)]
        assert bucket == 2.0
        assert summary == pytest.approx(store.summary("t", BASE - 3600))
        assert_summary(points, summary)
    finally:
        store.close()


@pytest.mark.parametrize("count", [0, 1, 2])
def test_hour_data_empty_or_complete_raw_minute(tmp_path, count):
    """Regrese B08: úplná minuta z RAM zachová i samostatný raw bod a chyby."""
    store = HistoryStore(tmp_path / "history.db")
    try:
        for i in range(count):
            record(store, BASE + 10 + i * 2, None if i else 10.0)
        points, bucket, summary = store.hour_data("t", BASE - 3600)
        assert [p.ts for p in points] == [BASE + 10 + i * 2 for i in range(count)]
        assert bucket == (2.0 if count == 2 else 1.0 if count == 1 else 60.0)
        assert summary == pytest.approx(store.summary("t", BASE - 3600))
        assert_summary(points, summary)
    finally:
        store.close()


def test_hour_data_uses_same_period_as_summary(tmp_path):
    """Regrese B08: neúplná počáteční minuta období má stejný filtr jako souhrn."""
    store = HistoryStore(tmp_path / "history.db")
    try:
        record(store, BASE + 12, 99.0)
        record(store, BASE + 61, 10.0)
        record(store, BASE + 63, 20.0)
        points, _, summary = store.hour_data("t", BASE + 10.5)
        assert [p.ts for p in points] == [BASE + 61, BASE + 63]
        assert summary == pytest.approx(store.summary("t", BASE + 10.5))
        assert_summary(points, summary)
    finally:
        store.close()


def test_hour_data_needs_one_select(tmp_path):
    """Regrese B16: série i souhrn se získají jediným čtecím SQL dotazem."""
    store = HistoryStore(tmp_path / "history.db")
    try:
        record(store, BASE + 10, 10.0)
        record(store, BASE + 12, 20.0)
        queries = []
        store._conn.set_trace_callback(queries.append)
        store.hour_data("t", BASE - 3600)
        assert len([q for q in queries if q.lstrip().upper().startswith("SELECT")]) == 1
    finally:
        store.close()


@pytest.mark.parametrize("old_offset", [-1800, 0])
def test_hour_dialog_keeps_persisted_data(tmp_path, monkeypatch, old_offset):
    """Regrese B08: skutečný dialog použije spojenou sérii i odpovídající souhrn."""
    from types import SimpleNamespace
    from typing import cast

    pytest.importorskip("PySide6")
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication

    from netmonitor.core.config import AppConfig, PingTarget
    from netmonitor.gui.controller import Controller
    from netmonitor.gui.history_dialog import HistoryDialog

    class Signals(QObject):
        targets_changed = Signal()

    app = QApplication.instance() or QApplication([])
    path = tmp_path / "history.db"
    old = HistoryStore(path)
    record(old, BASE + old_offset + 5, 40.0)
    old.close()
    store = HistoryStore(path)
    dialog = None
    try:
        record(store, BASE + 10, 10.0)
        record(store, BASE + 12, 20.0)
        cfg = AppConfig()
        cfg.ping.targets = [PingTarget(id="t", name="Test", host="192.0.2.1")]
        signals = Signals()
        ctrl = cast(Controller, SimpleNamespace(cfg=cfg, history=store, snapshots={},
                                                targets_changed=signals.targets_changed))
        monkeypatch.setattr("netmonitor.gui.history_dialog.time.time", lambda: BASE + 20)
        dialog = HistoryDialog(ctrl)
        queries = []
        store._conn.set_trace_callback(queries.append)
        dialog.refresh()
        assert dialog.chart.points[0].ts == BASE + old_offset
        assert sum(p.n_ok for p in dialog.chart.points) == 3
        assert dialog.stat_labels["ok"].text() == "3"
        # Původně souhrn + cílové a globální události (při ≥2 raw bodech).
        assert len([q for q in queries if q.lstrip().upper().startswith("SELECT")]) <= 3
        assert app is not None
    finally:
        if dialog is not None:
            dialog.close()
        store.close()


def test_mixed_history_is_one_continuous_segment(tmp_path):
    """Regrese review L33: 50 DB minut + 190 s raw nevytvoří 50 izolovaných teček."""
    path = tmp_path / "history.db"
    old = HistoryStore(path)
    for offset in range(0, 3000, 2):
        record(old, BASE + offset, 10.0 + offset % 10)
    old.close()
    store = HistoryStore(path)
    try:
        for offset in range(0, 190, 2):
            record(store, BASE + 3000 + offset, 20.0)
        points, _, _ = store.hour_data("t", BASE)
        segments = series_segments(points)
        assert len(points) == 145
        assert len(segments) == 1
        assert len(segments[0]) == len(points)
        assert [p.bucket_s for p in points[:50]] == [60.0] * 50
        assert [p.bucket_s for p in points[50:]] == [2.0] * 95
        assert points[0].center_ts == BASE + 30
        assert points[49].center_ts == BASE + 2970
        assert points[50].center_ts == BASE + 3001
    finally:
        store.close()


@pytest.mark.parametrize("times,widths,expected", [
    ([], [], []),
    ([0, 60, 120, 122], [60, 60, 2, 2], [4]),
    ([0, 60, 240, 242], [60, 60, 2, 2], [2, 2]),
    ([0, 300], [60, 60], [1, 1]),
    ([0, 2, 20, 22], [2, 2, 2, 2], [2, 2]),
    ([0, 300, 600], [300, 300, 300], [3]),
])
def test_series_segments_respect_each_points_width(times, widths, expected):
    """Regrese review L33: DB/raw navazují, skutečné mezery zůstávají oddělené."""
    points = [SeriesPoint(ts, 10.0, 10.0, 10.0, 1, 0, width) for ts, width in zip(times, widths)]
    assert [len(segment) for segment in series_segments(points)] == expected


def test_failed_minute_breaks_line_and_keeps_full_width():
    """Regrese review L33: 100% výpadek z DB zabere celou minutu a přeruší čáru."""
    before = SeriesPoint(0, 10.0, 10.0, 10.0, 30, 0)
    failure = SeriesPoint(60, None, None, None, 0, 30)
    after = SeriesPoint(120, 10.0, 10.0, 10.0, 1, 0, 2)
    assert series_segments([before, failure, after]) == [[before], [after]]
    assert failure.loss_pct == 100
    assert failure.bucket_s == 60
    assert failure.center_ts == 90


def test_raw_pause_does_not_inflate_point_width(tmp_path):
    """Regrese review L33: pauza mezi raw vzorky nesmí propojit skutečnou mezeru."""
    store = HistoryStore(tmp_path / "history.db")
    try:
        for offset in [0, 2, 4, 100, 102, 104]:
            record(store, BASE + offset, 10.0)
        points, bucket, _ = store.hour_data("t", BASE)
        assert bucket == 2
        assert all(p.bucket_s == 2 for p in points)
        assert [len(s) for s in series_segments(points)] == [3, 3]
    finally:
        store.close()


def test_hour_snapshot_blocks_record_between_flush_and_read(tmp_path, monkeypatch):
    """Regrese review L33: souběžný record nevstoupí mezi flush a čtecí snapshot."""
    import threading

    store = HistoryStore(tmp_path / "history.db")
    record(store, BASE + 10, 10.0)
    record(store, BASE + 12, 20.0)
    flushed, attempting, recorded, read = (threading.Event() for _ in range(4))
    lock = store._lock
    reader = threading.current_thread()
    flush = store._flush_locked
    series = store._series_locked

    class SnapshotLock:
        def __enter__(self):
            lock.acquire()

        def __exit__(self, *_args):
            lock.release()
            # Pokud čtenář odemkne před SELECTem, deterministicky pustit record.
            if threading.current_thread() is reader and flushed.is_set() and not read.is_set():
                assert recorded.wait(2)

    def flush_and_signal():
        result = flush()
        flushed.set()
        assert attempting.wait(2)
        return result

    def read_snapshot(*args):
        read.set()
        assert not recorded.is_set(), "record vstoupil mezi flush a SELECT"
        return series(*args)

    def writer():
        if flushed.wait(2):
            attempting.set()
            record(store, BASE + 14, 30.0)
            recorded.set()

    monkeypatch.setattr(store, "_lock", SnapshotLock())
    monkeypatch.setattr(store, "_flush_locked", flush_and_signal)
    monkeypatch.setattr(store, "_series_locked", read_snapshot)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        points, _, summary = store.hour_data("t", BASE)
        assert [p.ts for p in points] == [BASE + 10, BASE + 12]
        assert summary["n_ok"] == 2
        assert recorded.wait(2)
    finally:
        thread.join(2)
        monkeypatch.undo()
        store.close()
    assert not thread.is_alive()
