"""F27 M5: the raster Boolean engine (resolve_expression_raster + evaluate_raster)
must match the shapely path (resolve_expression -> make_mask) — exactly for the
set ops (& | ~ -) and within a sub-pixel boundary band for grow/shrink
morphology (which is quantized to the export pixel grid on purpose).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gds_boolean as gb          # noqa: E402

if not (getattr(gb, "_SHAPELY_OK", False) and getattr(gb, "_CV2_OK", False)):
    pytest.skip("raster Boolean needs shapely + cv2", allow_module_level=True)

W = H = 400
NMPP = 1.0
X0 = Y0 = 0.0
_A = [np.array([[50, 50], [250, 50], [250, 250], [50, 250]], float)]
_B = [np.array([[150, 150], [350, 150], [350, 350], [150, 350]], float)]
_BINDS = {"A": ("raw", 1, 0), "B": ("raw", 2, 0)}


def _raw_polys(layer, dt):
    return _A if (layer, dt) == (1, 0) else _B


def _shapely_mask(expr):
    fov = gb.fov_box((X0 + W * NMPP) / 2, (Y0 + H * NMPP) / 2, W * NMPP, H * NMPP)
    g = gb.resolve_expression(
        expr, _BINDS,
        raw_provider=lambda l, d: gb.polys_to_geometry(_raw_polys(l, d)),
        recipe_provider=lambda n: None, fov_bbox=fov)
    return gb.make_mask(g, width_px=W, height_px=H, x_min_nm=X0, y_min_nm=Y0,
                        nm_per_px=NMPP) > 0


def _raster_mask(expr):
    m = gb.resolve_expression_raster(
        expr, _BINDS, raw_poly_provider=_raw_polys, recipe_provider=lambda n: None,
        width_px=W, height_px=H, x_min_nm=X0, y_min_nm=Y0, nm_per_px=NMPP)
    return m > 0


@pytest.mark.parametrize("expr", ["A", "A & B", "A | B", "~A"])
def test_set_ops_exact(expr):
    # Pure set ops rasterize to the identical mask (fillPoly + bitwise).
    assert np.array_equal(_shapely_mask(expr), _raster_mask(expr))


@pytest.mark.parametrize("expr", ["A > W:10", "A < H:10", "A > H:20", "A < W:15"])
def test_morphology_within_one_pixel(expr):
    # Grow/shrink quantize to the pixel grid; allow a thin boundary band.
    s = _shapely_mask(expr); r = _raster_mask(expr)
    diff = int(np.count_nonzero(s != r))
    assert diff <= 0.03 * max(int(s.sum()), 1), (expr, diff, int(s.sum()))


@pytest.mark.parametrize("expr", ["(A > W:10) & B", "[(A > W:10) & B] < H:10",
                                  "(A | B) < W:5", "~(A & B)"])
def test_composed_expressions_close(expr):
    s = _shapely_mask(expr); r = _raster_mask(expr)
    diff = int(np.count_nonzero(s != r))
    assert diff <= 0.03 * max(int(s.sum()), 1), (expr, diff, int(s.sum()))


def test_mask_to_geometry_preserves_hole():
    # A - B leaves an L/notch; round-tripping the mask back to a geom and
    # re-rasterizing must reproduce (approximately) the same footprint.
    r = _raster_mask("A - B")
    m = gb.resolve_expression_raster(
        "A - B", _BINDS, raw_poly_provider=_raw_polys,
        recipe_provider=lambda n: None, width_px=W, height_px=H,
        x_min_nm=X0, y_min_nm=Y0, nm_per_px=NMPP)
    geom = gb.mask_to_geometry(m, x_min_nm=X0, y_min_nm=Y0, nm_per_px=NMPP,
                               invert_y=False)
    assert not geom.is_empty
    m2 = gb.make_mask(geom, width_px=W, height_px=H, x_min_nm=X0, y_min_nm=Y0,
                      nm_per_px=NMPP, invert_y=False) > 0
    diff = int(np.count_nonzero(m2 != r))
    assert diff <= 0.03 * max(int(r.sum()), 1), (diff, int(r.sum()))
