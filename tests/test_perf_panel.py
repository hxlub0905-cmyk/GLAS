"""F28 M2: the perf HUD window. Event → aggregate row + coloured log line, the
category filter, pause/clear, overview tiles, and the hide-on-close lifecycle.
Qt tests are skipped where PyQt6 is unavailable.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("PyQt6")
import perfmon                      # noqa: E402
import perf_panel                   # noqa: E402


@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def win(app):
    mon = perfmon.PerfMonitor(maxlen=100)
    w = perf_panel.PerfWindow(monitor=mon)
    yield w, mon
    w.shutdown()


# ── wiring ───────────────────────────────────────────────────────────────────

def test_monitor_callback_wired(win):
    w, mon = win
    assert mon.on_event is w._emit           # events route to this window


def test_event_adds_aggregate_row_and_log(win):
    w, mon = win
    ev = mon.record("roi", 12.3, label="6/0")   # also fills the monitor aggregate
    w._on_event(ev)                              # deterministic (bypass queued sig)
    assert w._table.rowCount() == 1
    assert "ROI walk" in w._log.toPlainText()
    assert "6/0" in w._log.toPlainText()


# ── category colour + filter ─────────────────────────────────────────────────

def test_category_color_known_and_default():
    assert perf_panel.category_color("roi") == perf_panel.CATEGORY_COLORS["roi"]
    assert perf_panel.category_color("nope") == perf_panel._CAT_DEFAULT


def test_filter_hides_deselected_category(win):
    w, mon = win
    w._on_event(mon.record("roi", 1.0, label="ALPHA"))
    w._on_event(mon.record("export", 1.0, label="BETA"))
    assert "ALPHA" in w._log.toPlainText() and "BETA" in w._log.toPlainText()
    w._chips["export"].setChecked(False)     # untick export → rebuild without it
    text = w._log.toPlainText()
    assert "ALPHA" in text and "BETA" not in text


# ── pause ────────────────────────────────────────────────────────────────────

def test_pause_holds_then_resume_rebuilds(win):
    w, mon = win
    w._toggle_pause()                        # paused
    w._on_event(mon.record("roi", 1.0, label="WHILE_PAUSED"))
    assert "WHILE_PAUSED" not in w._log.toPlainText()
    w._toggle_pause()                        # resume → rebuild from _events
    assert "WHILE_PAUSED" in w._log.toPlainText()


# ── overview tiles ───────────────────────────────────────────────────────────

def test_update_overview_sets_tiles(win):
    w, _mon = win
    w.update_overview({"ramp": "3→8", "throughput": "4.2 img/s"})
    assert w._tiles["ramp"].text() == "3→8"
    assert w._tiles["throughput"].text() == "4.2 img/s"


def test_warn_level_row_uses_warn_colour(win):
    w, mon = win
    w._on_event(mon.record("export", 400.0, label="img8",
                           level=perfmon.LEVEL_WARN))
    # the op-name cell should be recoloured to the warn hue
    from PyQt6.QtGui import QColor
    item = w._table.item(0, 1)
    assert item.foreground().color() == QColor(
        perf_panel.CATEGORY_COLORS["warn"])


# ── lifecycle ────────────────────────────────────────────────────────────────

def test_clear_empties_everything(win):
    w, mon = win
    w._on_event(mon.record("roi", 1.0))
    w._clear()
    assert w._table.rowCount() == 0
    assert w._log.toPlainText() == ""
    assert mon.aggregates() == []


def test_close_hides_and_emits(win):
    w, _mon = win
    fired = []
    w.closed.connect(lambda: fired.append(True))
    from PyQt6.QtGui import QCloseEvent
    w.show()
    w.closeEvent(QCloseEvent())
    assert not w.isVisible()                 # hidden, not destroyed
    assert fired == [True]


def test_shutdown_detaches_callback(app):
    mon = perfmon.PerfMonitor()
    w = perf_panel.PerfWindow(monitor=mon)
    assert mon.on_event is w._emit
    w.shutdown()
    assert mon.on_event is None              # detached so no dangling callback
