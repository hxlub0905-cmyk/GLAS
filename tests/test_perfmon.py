"""F28 M1: the Qt-free perf event collector (perfmon). Aggregation, ring buffer,
on_event callback, timed() context manager, category/level, console + .txt sinks,
and thread-safety (record() is called from ROI / batch worker threads).
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import perfmon         # noqa: E402


def _fresh():
    return perfmon.PerfMonitor(maxlen=5)


# ── aggregation ──────────────────────────────────────────────────────────────

def test_record_aggregates():
    m = _fresh()
    m.record("roi", 10.0)
    m.record("roi", 30.0)
    agg = dict((op, a) for op, a in m.aggregates())
    a = agg["roi"]
    assert a["n"] == 2 and a["total"] == 40.0
    assert a["last"] == 30.0 and a["max"] == 30.0 and a["avg"] == 20.0


def test_aggregates_keep_first_seen_order():
    m = _fresh()
    for op in ("open", "roi", "boolean"):
        m.record(op, 1.0)
    assert [op for op, _ in m.aggregates()] == ["open", "roi", "boolean"]


def test_ring_buffer_bounded():
    m = _fresh()                        # maxlen=5
    for i in range(20):
        m.record("roi", float(i))
    r = m.recent(100)
    assert len(r) == 5                  # only the last 5 kept
    assert [e.ms for e in r] == [15.0, 16.0, 17.0, 18.0, 19.0]


# ── on_event callback ────────────────────────────────────────────────────────

def test_on_event_callback_fires():
    m = _fresh()
    seen = []
    m.on_event = seen.append
    ev = m.record("roi", 5.0, label="6/0")
    assert seen and seen[-1] is ev and ev.label == "6/0"


def test_callback_error_never_propagates():
    m = _fresh()

    def boom(_ev):
        raise RuntimeError("ui blew up")

    m.on_event = boom
    m.record("roi", 1.0)                # must not raise


# ── timed() ──────────────────────────────────────────────────────────────────

def test_timed_records_and_merges_meta():
    m = _fresh()
    seen = []
    m.on_event = seen.append
    with m.timed("roi", label="load", cells=1) as t:
        t["meta"]["rects"] = 99
    ev = seen[-1]
    assert ev.op == "roi" and ev.label == "load"
    assert ev.meta == {"cells": 1, "rects": 99}
    assert ev.ms >= 0.0


def test_timed_level_upgrade():
    m = _fresh()
    seen = []
    m.on_event = seen.append
    with m.timed("export", label="img8") as t:
        t["level"] = perfmon.LEVEL_WARN       # discovered a thrash mid-op
    assert seen[-1].level == perfmon.LEVEL_WARN


# ── category / level ─────────────────────────────────────────────────────────

def test_category_defaults_to_op_prefix():
    m = _fresh()
    assert m.record("worker:24908", 1.0).category == "worker"
    assert m.record("roi", 1.0).category == "roi"


def test_category_explicit_override():
    m = _fresh()
    ev = m.record("batch:walk", 1.0, category="export")
    assert ev.category == "export"


def test_level_defaults_info_and_overrides():
    m = _fresh()
    assert m.record("roi", 1.0).level == perfmon.LEVEL_INFO
    assert m.record("roi", 1.0, level=perfmon.LEVEL_ERROR).level == "error"


# ── console + txt sinks ──────────────────────────────────────────────────────

def test_echo_console_prints_when_enabled(capsys):
    m = _fresh()
    m.record("roi", 1.0, label="quiet")
    assert capsys.readouterr().out == ""        # off by default
    m.echo_console = True
    m.record("roi", 1.0, label="loud")
    assert "loud" in capsys.readouterr().out


def test_logfile_roundtrip(tmp_path):
    m = _fresh()
    p = m.set_logfile(tmp_path / "perf.txt")
    assert p is not None and m.is_logging()
    m.record("roi", 12.3, label="6/0")
    m.close_logfile()
    assert not m.is_logging()
    text = (tmp_path / "perf.txt").read_text(encoding="utf-8")
    assert "ROI walk" in text and "6/0" in text


# ── lifecycle ────────────────────────────────────────────────────────────────

def test_clear_resets():
    m = _fresh()
    m.record("roi", 1.0)
    m.clear()
    assert m.aggregates() == [] and m.recent() == []


def test_disabled_monitor_records_nothing():
    m = _fresh()
    m.enabled = False
    m.record("roi", 1.0)
    assert m.aggregates() == []


def test_format_event_shape():
    ev = perfmon.PerfEvent(op="roi", label="6/0", ms=12.3, meta={"rects": 5})
    line = perfmon.format_event(ev)
    assert "ROI walk" in line and "12.3 ms" in line and "6/0" in line
    assert "rects=5" in line


# ── thread-safety (record() runs on ROI / batch worker threads) ──────────────

def test_record_is_thread_safe():
    m = perfmon.PerfMonitor(maxlen=10_000)
    n_threads, per = 8, 500

    def worker():
        for _ in range(per):
            m.record("roi", 1.0)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    agg = dict((op, a) for op, a in m.aggregates())
    assert agg["roi"]["n"] == n_threads * per
