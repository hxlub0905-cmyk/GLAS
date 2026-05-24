"""Offscreen regression tests for the M4a SEM↔GDS overlay drag + origin
offset δ machinery in ``tools/gds_align_tool.py``.

These lock the invariants the M4a plan only validated by ad-hoc offscreen
smoke (SESSION_LOG 2026-05-21):

* SemViewer fold invariant ``render(anchor, drag) == render(anchor−drag, 0)``
  — what makes "Set Offset" leave the geometry visually put.
* Set Offset folds the overlay drag into the global origin δ (``origin -= drag``)
  and resets the drag.
* Clear Offset zeros both; a zero drag is a no-op for Set Offset.
* The live drag preview reports ``origin − drag``.

The whole module skips when PyQt6 (or its Qt platform libs) can't be
imported, matching the project's "no GUI environment" test pattern, so
``pytest`` stays green on bare backends.
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

try:  # Qt platform libs (libEGL etc.) may still be missing on a bare box.
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QPixmap, QColor, QMouseEvent, QWheelEvent
    from PyQt6.QtCore import QPointF, QPoint, Qt
    _APP = QApplication.instance() or QApplication([])
    import gds_align_tool as gat  # noqa: E402
except Exception as exc:  # pragma: no cover - environment-dependent
    pytest.skip(f"Qt runtime unavailable: {exc}", allow_module_level=True)


# ── SemViewer fold invariant ─────────────────────────────────────────


def _viewer(anchor=(1000.0, 2000.0), nm_per_px=2.0):
    v = gat.SemViewer()
    v._img_rect = (0.0, 0.0, 200.0, 200.0)
    v._scale = 1.0
    v._nm_per_px = nm_per_px
    v._anchor = anchor
    return v


def _image_viewer(w=400, h=400, nw=200, nh=200):
    v = gat.SemViewer()
    v.resize(w, h)
    pm = QPixmap(nw, nh)
    pm.fill(QColor("#303030"))
    v._pixmap = pm
    return v


def _wheel(v, x, y, delta):
    ev = QWheelEvent(QPointF(x, y), QPointF(x, y), QPoint(0, delta),
                     QPoint(0, delta), Qt.MouseButton.NoButton,
                     Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase,
                     False)
    v.wheelEvent(ev)


def _press(v, btn, x, y):
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(x, y),
                     QPointF(x, y), btn, btn, Qt.KeyboardModifier.NoModifier)
    v.mousePressEvent(ev)


def _move(v, x, y, buttons):
    ev = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(x, y), QPointF(x, y),
                     Qt.MouseButton.NoButton, buttons,
                     Qt.KeyboardModifier.NoModifier)
    v.mouseMoveEvent(ev)


class TestSemViewerZoomPan:

    def test_default_view_is_fit(self):
        v = _image_viewer()
        assert v._view_zoom == 1.0
        assert (v._pan_x, v._pan_y) == (0.0, 0.0)

    def test_wheel_in_zooms_and_keeps_point_under_cursor(self):
        v = _image_viewer()
        ox, oy, dw, dh, s = v._compute_geometry()
        ix, iy = (100 - ox) / s, (100 - oy) / s
        _wheel(v, 100, 100, 120)
        assert v._view_zoom == pytest.approx(1.2)
        ox2, oy2, dw2, dh2, s2 = v._compute_geometry()
        assert (100 - ox2) / s2 == pytest.approx(ix)
        assert (100 - oy2) / s2 == pytest.approx(iy)

    def test_wheel_out_zooms_out(self):
        v = _image_viewer()
        _wheel(v, 200, 200, -120)
        assert v._view_zoom == pytest.approx(1.0 / 1.2)

    def test_zoom_clamped(self):
        v = _image_viewer()
        for _ in range(60):
            _wheel(v, 200, 200, 120)
        assert v._view_zoom <= v._MAX_ZOOM
        for _ in range(120):
            _wheel(v, 200, 200, -120)
        assert v._view_zoom >= v._MIN_ZOOM

    def test_reset_view(self):
        v = _image_viewer()
        v._view_zoom, v._pan_x, v._pan_y = 4.0, 30.0, -20.0
        v.reset_view()
        assert v._view_zoom == 1.0 and (v._pan_x, v._pan_y) == (0.0, 0.0)

    def test_set_image_resets_view(self):
        v = _image_viewer()
        v._view_zoom, v._pan_x = 4.0, 50.0
        v.set_image(None)
        assert v._view_zoom == 1.0 and v._pan_x == 0.0

    def test_right_drag_pans(self):
        v = _image_viewer()
        _press(v, Qt.MouseButton.RightButton, 100, 100)
        _move(v, 130, 120, Qt.MouseButton.RightButton)
        assert (v._pan_x, v._pan_y) == (30.0, 20.0)

    def test_left_drag_aligns_not_pans(self):
        v = _image_viewer()
        v._anchor = (1000.0, 2000.0)
        v._nm_per_px = 2.0
        v._img_rect = (0.0, 0.0, 400.0, 400.0)   # normally set at paint
        v._scale = 2.0
        _press(v, Qt.MouseButton.LeftButton, 100, 100)
        _move(v, 130, 100, Qt.MouseButton.LeftButton)
        assert v._panning is False
        assert (v._pan_x, v._pan_y) == (0.0, 0.0)   # left-drag never pans
        assert v._drag_x != 0.0                      # it moved the overlay δ


class TestSemViewerFold:

    @pytest.mark.parametrize("drag", [(0.0, 0.0), (300.0, -150.0),
                                      (-80.0, 220.0)])
    @pytest.mark.parametrize("pt", [(1000.0, 2000.0), (1300.0, 2100.0),
                                    (650.0, 2480.0)])
    def test_render_with_drag_equals_folded_anchor(self, drag, pt):
        """screen(anchor, drag) == screen(anchor − drag, drag=0)."""
        ax, ay = 1000.0, 2000.0
        v = _viewer(anchor=(ax, ay))
        v._drag_x, v._drag_y = drag
        with_drag = v._world_to_view(*pt)

        v2 = _viewer(anchor=(ax - drag[0], ay - drag[1]))
        v2._drag_x = v2._drag_y = 0.0
        folded = v2._world_to_view(*pt)

        assert with_drag == pytest.approx(folded)

    def test_reset_drag(self):
        v = _viewer()
        v._drag_x, v._drag_y = 42.0, -17.0
        v.reset_drag()
        assert v.drag_offset_nm() == (0.0, 0.0)

    def test_set_image_clears_drag(self):
        v = _viewer()
        v._drag_x, v._drag_y = 42.0, -17.0
        v.set_image(None)              # new image never inherits a temp drag
        assert v.drag_offset_nm() == (0.0, 0.0)

    def test_set_overlay_keeps_anchor_and_scale(self):
        v = gat.SemViewer()
        polys = []
        v.set_overlay([(polys, "#ff0000")], (5.0, 6.0), 3.0)
        assert v._anchor == (5.0, 6.0)
        assert v._nm_per_px == 3.0
        v.clear_overlay()
        assert v._anchor is None


# ── MainWindow origin-δ folding ──────────────────────────────────────


@pytest.fixture
def mw():
    w = gat.MainWindow()
    yield w
    w.close()


class TestOriginOffsetFold:

    def test_set_offset_folds_drag_into_origin(self, mw):
        mw._origin_dx, mw._origin_dy = 100.0, -50.0
        mw.sem_viewer._anchor = (1000.0, 2000.0)
        mw.sem_viewer._nm_per_px = 2.0
        mw.sem_viewer._drag_x, mw.sem_viewer._drag_y = 300.0, -150.0
        mw._current_sem = None
        mw._on_set_offset()
        # origin -= drag, drag reset.
        assert mw._origin_dx == pytest.approx(-200.0)
        assert mw._origin_dy == pytest.approx(100.0)
        assert mw.sem_viewer.drag_offset_nm() == (0.0, 0.0)

    def test_set_offset_zero_drag_is_noop(self, mw):
        mw._origin_dx, mw._origin_dy = 7.0, 8.0
        mw.sem_viewer._drag_x = mw.sem_viewer._drag_y = 0.0
        mw._on_set_offset()
        assert (mw._origin_dx, mw._origin_dy) == (7.0, 8.0)

    def test_clear_offset_zeros_origin_and_drag(self, mw):
        mw._origin_dx, mw._origin_dy = 123.0, -456.0
        mw.sem_viewer._drag_x, mw.sem_viewer._drag_y = 5.0, 6.0
        mw._current_sem = None
        mw._on_clear_offset()
        assert (mw._origin_dx, mw._origin_dy) == (0.0, 0.0)
        assert mw.sem_viewer.drag_offset_nm() == (0.0, 0.0)

    def test_overlay_drag_preview_is_origin_minus_drag(self, mw):
        mw._origin_dx, mw._origin_dy = 100.0, -50.0
        mw.sem_viewer._drag_x, mw.sem_viewer._drag_y = 30.0, 40.0
        mw._on_overlay_drag()
        # The Coordinate Setup panel previews the effective δ (origin − drag)
        # live via its read-only label.
        label = mw.sem_panel.coord_setup._origin_lbl.text()
        assert "70" in label and "-90" in label   # 100−30, −50−40

    def test_set_offset_preserves_visual_anchor(self, mw):
        """Folding drag into origin must reproduce the dragged eff_anchor
        so the overlay doesn't visibly jump on Set Offset."""
        mw._origin_dx, mw._origin_dy = 0.0, 0.0
        anchor = (1000.0, 2000.0)
        mw.sem_viewer._anchor = anchor
        mw.sem_viewer._nm_per_px = 2.0
        drag = (300.0, -150.0)
        mw.sem_viewer._drag_x, mw.sem_viewer._drag_y = drag
        eff_before = (anchor[0] - drag[0], anchor[1] - drag[1])
        mw._current_sem = None
        mw._on_set_offset()
        # New eff_anchor = (anchor + Δorigin) − 0; Δorigin = −drag.
        eff_after = (anchor[0] + mw._origin_dx, anchor[1] + mw._origin_dy)
        assert eff_after == pytest.approx(eff_before)
