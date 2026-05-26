"""Tests for glas/core/layout_export.py (F9 M2).

Covers ROI clipping (whole vs cropped), drop-empty behaviour, hole
dropping (O-holes), and an end-to-end clip -> write -> read-back through
oasis_streamer. Requires shapely (+ numpy for the read-back oracle).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _sub in ("glas/core",):
    _p = REPO_ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

shapely = pytest.importorskip("shapely")

import oasis_streamer as oas  # noqa: E402
import layout_export as lx  # noqa: E402


def _square(x0, y0, x1, y1):
    return np.array([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], dtype=np.float64)


def _read_rect_bboxes(path):
    out = []
    for rid, p in oas.OasisReader(path).iter_records():
        if rid == oas.RECTANGLE:
            out.append((p["x"], p["y"], p["x"] + p["width"], p["y"] + p["height"]))
        elif rid == oas.POLYGON:
            xs = [p["x"] + px for px, _ in p["points"]]
            ys = [p["y"] + py for _, py in p["points"]]
            out.append((min(xs), min(ys), max(xs), max(ys)))
    return out


# ── clip_polygons ────────────────────────────────────────────────────────────


def test_no_crop_passthrough():
    polys = [_square(0, 0, 10, 10)]
    out = lx.clip_polygons(polys, None)
    assert len(out) == 1
    assert np.allclose(out[0], polys[0])


def test_crop_interior_box():
    # A 100x100 square clipped to its centre 20..60 box -> that 40x40 box.
    out = lx.clip_polygons([_square(0, 0, 100, 100)], (20, 20, 60, 60))
    assert len(out) == 1
    xs, ys = out[0][:, 0], out[0][:, 1]
    assert (xs.min(), ys.min(), xs.max(), ys.max()) == (20, 20, 60, 60)


def test_crop_fully_outside_drops():
    out = lx.clip_polygons([_square(0, 0, 10, 10)], (100, 100, 200, 200))
    assert out == []


def test_crop_corners_any_order():
    # upper-right given before lower-left still works
    out = lx.clip_polygons([_square(0, 0, 100, 100)], (60, 60, 20, 20))
    xs, ys = out[0][:, 0], out[0][:, 1]
    assert (xs.min(), ys.min(), xs.max(), ys.max()) == (20, 20, 60, 60)


def test_degenerate_skipped():
    assert lx.clip_polygons([np.array([(0, 0), (1, 1)])], (0, 0, 10, 10)) == []


# ── shapely_to_rings (holes dropped) ─────────────────────────────────────────


def test_shapely_to_rings_drops_holes():
    from shapely import Polygon
    donut = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)],
                    holes=[[(3, 3), (7, 3), (7, 7), (3, 7)]])
    rings = lx.shapely_to_rings(donut)
    assert len(rings) == 1  # only the exterior
    xs, ys = rings[0][:, 0], rings[0][:, 1]
    assert (xs.min(), ys.min(), xs.max(), ys.max()) == (0, 0, 10, 10)


def test_shapely_to_rings_multipolygon():
    from shapely import MultiPolygon, Polygon
    mp = MultiPolygon([Polygon(_square(0, 0, 5, 5)), Polygon(_square(20, 20, 25, 25))])
    assert len(lx.shapely_to_rings(mp)) == 2


# ── clip_layers / export_layers ──────────────────────────────────────────────


def test_clip_layers_drops_empty():
    layers = [
        (17, 0, [_square(0, 0, 10, 10)]),
        (25, 0, [_square(500, 500, 510, 510)]),   # outside crop
    ]
    out = lx.clip_layers(layers, (0, 0, 100, 100))
    assert [(l, d) for l, d, _ in out] == [(17, 0)]


def test_export_whole_roundtrip(tmp_path):
    p = tmp_path / "whole.oas"
    n, report = lx.export_layers(p, [(17, 0, [_square(0, 0, 40, 30)])], unit=1000)
    assert n == 1 and report is None
    assert _read_rect_bboxes(p) == [(0, 0, 40, 30)]


def test_export_cropped_roundtrip(tmp_path):
    p = tmp_path / "crop.oas"
    n, _ = lx.export_layers(p, [(17, 0, [_square(0, 0, 100, 100)])],
                            crop_bbox=(20, 20, 60, 60), unit=1000)
    assert n == 1
    assert _read_rect_bboxes(p) == [(20, 20, 60, 60)]


def test_export_debug_report(tmp_path):
    p = tmp_path / "dbg.oas"
    n, report = lx.export_layers(
        p, [(17, 0, [_square(0, 0, 40, 30)])], unit=1000, debug=True)
    assert n == 1
    assert report is not None
    assert "round-trip check" in report
    assert "OK" in report
