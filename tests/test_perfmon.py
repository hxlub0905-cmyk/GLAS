"""Tests for the Qt-free perf-event collector (glas/core/perfmon.py)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1] / "glas" / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import perfmon  # noqa: E402


def _fresh():
    return perfmon.PerfMonitor(maxlen=5)


class TestRecord:
    def test_record_updates_aggregate(self):
        m = _fresh()
        m.record("roi", 10.0, label="L1")
        m.record("roi", 30.0, label="L2")
        agg = dict(m.aggregates())
        a = agg["roi"]
        assert a["n"] == 2
        assert a["total"] == 40.0
        assert a["max"] == 30.0
        assert a["last"] == 30.0
        assert a["avg"] == 20.0
        assert a["label"] == "L2"

    def test_distinct_ops_kept_in_first_seen_order(self):
        m = _fresh()
        m.record("open", 1.0)
        m.record("roi", 2.0)
        m.record("boolean", 3.0)
        ops = [op for op, _ in m.aggregates()]
        assert ops == ["open", "roi", "boolean"]

    def test_meta_carried_into_event(self):
        m = _fresh()
        ev = m.record("open", 5.0, label="big.oas", cells="1,000")
        assert ev.op == "open"
        assert ev.label == "big.oas"
        assert ev.meta["cells"] == "1,000"

    def test_recent_is_bounded(self):
        m = _fresh()  # maxlen=5
        for i in range(8):
            m.record("roi", float(i))
        rec = m.recent(50)
        assert len(rec) == 5
        assert [e.ms for e in rec] == [3.0, 4.0, 5.0, 6.0, 7.0]

    def test_disabled_records_nothing(self):
        m = _fresh()
        m.enabled = False
        m.record("roi", 10.0)
        assert m.aggregates() == []

    def test_clear(self):
        m = _fresh()
        m.record("roi", 10.0)
        m.clear()
        assert m.aggregates() == []
        assert m.recent() == []


class TestCallback:
    def test_on_event_fires(self):
        m = _fresh()
        seen = []
        m.on_event = seen.append
        m.record("roi", 7.0, label="x")
        assert len(seen) == 1
        assert seen[0].op == "roi"

    def test_callback_exception_does_not_propagate(self):
        m = _fresh()

        def boom(_ev):
            raise RuntimeError("nope")

        m.on_event = boom
        # Must not raise.
        m.record("roi", 1.0)
        assert dict(m.aggregates())["roi"]["n"] == 1


class TestTimed:
    def test_timed_records_elapsed_and_meta(self):
        m = _fresh()
        with m.timed("boolean", label="A & B") as t:
            time.sleep(0.005)
            t["meta"]["out_polys"] = 3
        agg = dict(m.aggregates())
        a = agg["boolean"]
        assert a["n"] == 1
        assert a["last"] >= 4.0     # ~5 ms slept
        rec = m.recent(1)[0]
        assert rec.meta["out_polys"] == 3
        assert rec.label == "A & B"


class TestLogfile:
    def test_logfile_written_and_closed(self, tmp_path):
        m = _fresh()
        p = tmp_path / "perf.txt"
        assert m.set_logfile(p) == p
        assert m.is_logging()
        m.record("open", 12.3, label="big.oas", cells="9")
        m.record("roi", 4.5, label="2 layer(s)")
        m.close_logfile()
        assert not m.is_logging()
        text = p.read_text(encoding="utf-8")
        assert "GLAS performance log" in text
        assert "big.oas" in text
        assert "Open + index" in text   # OP_LABELS mapping applied
        assert "ROI walk" in text

    def test_set_logfile_replaces_previous(self, tmp_path):
        m = _fresh()
        p1, p2 = tmp_path / "a.txt", tmp_path / "b.txt"
        m.set_logfile(p1)
        m.record("roi", 1.0)
        m.set_logfile(p2)        # should close p1
        m.record("roi", 2.0)
        m.close_logfile()
        assert "1.0 ms" in p1.read_text() or "1.0" in p1.read_text()
        assert "2.0" in p2.read_text()

    def test_bad_logpath_returns_none(self, tmp_path):
        m = _fresh()
        bad = tmp_path / "no_such_dir" / "perf.txt"
        assert m.set_logfile(bad) is None
        assert not m.is_logging()


class TestFormat:
    def test_format_event_contains_fields(self):
        ev = perfmon.PerfEvent(op="roi", label="2 layer(s)", ms=123.4,
                               t=time.time(), meta={"rects": "10"})
        line = perfmon.format_event(ev)
        assert "ROI walk" in line
        assert "123.4 ms" in line
        assert "2 layer(s)" in line
        assert "rects=10" in line
