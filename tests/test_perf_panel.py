"""Smoke tests for the PerfPanel HUD (glas/app/perf_panel.py).

Qt-gated: skipped where PyQt6 / a Qt platform plugin is unavailable.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1] / "glas" / "core"
_APP = Path(__file__).resolve().parents[1] / "glas" / "app"
for _p in (_CORE, _APP):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 not available")

from PyQt6.QtWidgets import QApplication  # noqa: E402

import perfmon          # noqa: E402
from perf_panel import PerfPanel  # noqa: E402


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    yield a


def _drain(app):
    # Flush the queued cross-thread signal so _on_event runs.
    app.processEvents()


def test_panel_shows_recorded_event(app):
    mon = perfmon.PerfMonitor()
    panel = PerfPanel(mon)
    mon.record("roi", 12.5, label="2 layer(s)", rects="10")
    _drain(app)
    # one aggregate row for "roi"
    assert panel._table.rowCount() == 1
    assert panel._table.item(0, 0).text() == "ROI walk"
    assert "12.5" in panel._table.item(0, 1).text()
    assert "ROI walk" in panel._log.toPlainText()
    panel.detach()


def test_panel_aggregates_multiple_ops(app):
    mon = perfmon.PerfMonitor()
    panel = PerfPanel(mon)
    mon.record("open", 100.0)
    mon.record("roi", 10.0)
    mon.record("roi", 20.0)
    _drain(app)
    assert panel._table.rowCount() == 2          # open + roi
    # find the roi row and check count column == 2
    rows = {panel._table.item(r, 0).text(): r
            for r in range(panel._table.rowCount())}
    roi_row = rows["ROI walk"]
    assert panel._table.item(roi_row, 2).text() == "2"
    panel.detach()


def test_clear_empties_table(app):
    mon = perfmon.PerfMonitor()
    panel = PerfPanel(mon)
    mon.record("roi", 1.0)
    _drain(app)
    assert panel._table.rowCount() == 1
    panel._clear()
    assert panel._table.rowCount() == 0
    assert panel._log.toPlainText() == ""
    panel.detach()


def test_primes_from_existing_monitor_state(app):
    mon = perfmon.PerfMonitor()
    mon.record("boolean", 5.0, label="A & B")   # before panel exists
    panel = PerfPanel(mon)                       # should prime
    assert panel._table.rowCount() == 1
    assert panel._table.item(0, 0).text() == "Boolean eval"
    panel.detach()


def test_detach_stops_callback(app):
    mon = perfmon.PerfMonitor()
    panel = PerfPanel(mon)
    panel.detach()
    assert mon.on_event is None
    # recording after detach must not raise / must not touch the panel
    mon.record("roi", 1.0)
    _drain(app)
