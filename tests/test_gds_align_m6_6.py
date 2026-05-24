"""Offscreen regression tests for the M6.6 UX polish in
``tools/gds_align_tool.py``:

* English-only UI strings (no CJK in user-facing widget text).
* SemViewer live readout: ``_view_to_world`` inverse mapping, scale-bar
  ``_nice_round`` rounding, cursor clearing on leave.
* ``zoom_by`` (keyboard zoom) around the widget centre + clamping.
* MainWindow workflow guidance step progression.
* Origin-δ keyboard nudge.
* Coordinate Setup live chip-corner preview shows µm.

Skips when PyQt6 / Qt platform libs are unavailable.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 not available")
np = pytest.importorskip("numpy")

try:
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QPixmap, QColor
    from PyQt6.QtCore import QPointF
    _APP = QApplication.instance() or QApplication([])
    import gds_align_tool as gat  # noqa: E402
except Exception as exc:  # pragma: no cover - environment-dependent
    pytest.skip(f"Qt runtime unavailable: {exc}", allow_module_level=True)


def _image_viewer(w=400, h=400, nw=200, nh=200):
    v = gat.SemViewer()
    v.resize(w, h)
    pm = QPixmap(nw, nh)
    pm.fill(QColor("#303030"))
    v._pixmap = pm
    return v


# ── Scale-bar rounding ───────────────────────────────────────────────


class TestNiceRound:

    @pytest.mark.parametrize("x,expected", [
        (3.0, 2.0), (7.0, 5.0), (12.0, 10.0), (90.0, 50.0),
        (450.0, 200.0), (1300.0, 1000.0), (0.0, 1.0),
    ])
    def test_nice_round(self, x, expected):
        assert gat._nice_round(x) == expected


# ── SemViewer live readout ───────────────────────────────────────────


class TestViewToWorld:

    def test_none_without_anchor(self):
        v = _image_viewer()
        assert v._view_to_world(100, 100) is None

    def test_centre_maps_to_anchor(self):
        v = _image_viewer()
        v._anchor = (10000.0, 20000.0)
        v._nm_per_px = 5.0
        v._img_rect = (0.0, 0.0, 400.0, 400.0)
        v._scale = 2.0
        wx, wy = v._view_to_world(200, 200)
        assert (wx, wy) == pytest.approx((10000.0, 20000.0))

    def test_roundtrip_with_world_to_view(self):
        v = _image_viewer()
        v._anchor = (5000.0, 6000.0)
        v._nm_per_px = 3.0
        v._img_rect = (10.0, 20.0, 380.0, 360.0)
        v._scale = 1.8
        sx, sy = v._world_to_view(5300.0, 6120.0)
        wx, wy = v._view_to_world(sx, sy)
        assert (wx, wy) == pytest.approx((5300.0, 6120.0))

    def test_leave_clears_cursor(self):
        v = _image_viewer()
        v._cursor_screen = QPointF(50, 50)
        v.leaveEvent(None)
        assert v._cursor_screen is None


# ── Keyboard zoom ────────────────────────────────────────────────────


class TestZoomBy:

    def test_zoom_by_changes_zoom(self):
        v = _image_viewer()
        v.zoom_by(1.2)
        assert v._view_zoom == pytest.approx(1.2)
        v.zoom_by(1.0 / 1.2)
        assert v._view_zoom == pytest.approx(1.0)

    def test_zoom_by_clamped(self):
        v = _image_viewer()
        for _ in range(60):
            v.zoom_by(1.2)
        assert v._view_zoom <= v._MAX_ZOOM
        for _ in range(120):
            v.zoom_by(1.0 / 1.2)
        assert v._view_zoom >= v._MIN_ZOOM


# ── MainWindow guidance + nudge ──────────────────────────────────────


@pytest.fixture
def mw():
    w = gat.MainWindow()
    yield w
    w.close()


class TestGuidance:

    def test_step_progression(self, mw):
        assert mw._guidance.text().startswith("Step 1")
        mw._rar = object()
        mw._update_guidance()
        assert mw._guidance.text().startswith("Step 2")
        mw._sem_images = [object()]
        mw._update_guidance()
        assert mw._guidance.text().startswith("Step 3")
        mw._fov_w = mw._fov_h = 2000.0
        mw._update_guidance()
        assert mw._guidance.text().startswith("Step 4")
        mw._current_sem = object()
        mw._update_guidance()
        assert mw._guidance.text().startswith("Step 5")

    def test_guidance_english_only(self, mw):
        # Walk every step and assert no CJK leaked into the guidance text.
        for setup in [lambda: None,
                      lambda: setattr(mw, "_rar", object()),
                      lambda: setattr(mw, "_sem_images", [object()]),
                      lambda: (setattr(mw, "_fov_w", 2000.0),
                               setattr(mw, "_fov_h", 2000.0)),
                      lambda: setattr(mw, "_current_sem", object())]:
            setup()
            mw._update_guidance()
            assert not any("一" <= c <= "鿿"
                           for c in mw._guidance.text())


class TestNudgeOrigin:

    def test_nudge_shifts_delta(self, mw):
        mw._origin_dx = mw._origin_dy = 0.0
        mw._current_sem = None
        mw._nudge_origin(10.0, 0.0)
        mw._nudge_origin(0.0, -10.0)
        assert (mw._origin_dx, mw._origin_dy) == (10.0, -10.0)

    def test_overview_toggle_via_button(self, mw):
        assert mw.canvas.isHidden() is True
        mw._set_view_mode("gds")
        assert mw.canvas.isHidden() is False
        mw._set_view_mode("sem")
        assert mw.canvas.isHidden() is True


# ── Coordinate Setup live preview ────────────────────────────────────


class TestCoordPreview:

    def test_corner_label_shows_um(self, mw):
        panel = mw.sem_panel.coord_setup
        panel._chip_x.setValue(100.0)
        txt = panel._corner_lbl.text()
        assert "nm" in txt and "µm" in txt
