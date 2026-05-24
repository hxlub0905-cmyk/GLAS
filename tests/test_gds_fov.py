"""Tests for tools/gds_fov.py (F2 M2.2 FOV query + M2.3 coord conversion)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import gds_fov  # noqa: E402
from gds_fov import (  # noqa: E402
    klarf_to_gds,
    rfl_to_chip_corner,
    fov_bounds,
    query_fov,
    fov_overlap_indices,
    query_fov_multi,
    query_fov_klarf,
)


# ── M2.3 RFL -> chip-corner ──────────────────────────────────────────


class TestRflToChipCorner:

    def test_chip_fills_die_centered(self):
        # Chip same size as die, centred -> chip corner at die corner.
        cx, cy = rfl_to_chip_corner(10, 20, 0, 0, 10, 20)
        assert cx == 0 and cy == 0

    def test_mm_to_nm_and_formula(self):
        # die 10x20 mm, chip centre at (+1,-2) mm, chip 4x6 mm.
        # corner = die/2 + centre - chip/2
        #   x = 5 + 1 - 2 = 4 mm  -> 4e6 nm
        #   y = 10 - 2 - 3 = 5 mm -> 5e6 nm
        cx, cy = rfl_to_chip_corner(10, 20, 1, -2, 4, 6)
        assert cx == 4_000_000.0
        assert cy == 5_000_000.0

    def test_feeds_klarf_to_gds(self):
        # End-to-end: a defect at the chip centre should map to the chip
        # centre in GDS coords (= chip_size/2 nm).
        die_w, die_h = 10, 20
        ccx, ccy = 1.0, -2.0          # chip centre rel die centre (mm)
        chip_w, chip_h = 4, 6
        cc_x, cc_y = rfl_to_chip_corner(die_w, die_h, ccx, ccy, chip_w, chip_h)
        # die-corner coord of the chip centre (mm -> nm):
        #   die_centre + chip_centre_offset = (5+1, 10-2) = (6, 8) mm
        xrel, yrel = 6_000_000.0, 8_000_000.0
        gx, gy = klarf_to_gds(xrel, yrel, cc_x, cc_y)
        assert gx == chip_w / 2 * 1e6   # 2e6
        assert gy == chip_h / 2 * 1e6   # 3e6

    def test_real_diepitch_units(self):
        # DiePitch from the real KLARF fixture is nm; as mm it's
        # 23.376636 x 32.874750. Chip centred & chip = die -> corner 0.
        dw, dh = 23.376636, 32.874750
        cx, cy = rfl_to_chip_corner(dw, dh, 0, 0, dw, dh)
        assert abs(cx) < 1e-6 and abs(cy) < 1e-6


# ── M2.3 coordinate conversion ───────────────────────────────────────


class TestKlarfToGds:

    def test_scalar(self):
        gx, gy = klarf_to_gds(1000, 2000, 100, 50)
        assert gx == 900
        assert gy == 1950

    def test_zero_corner_is_identity(self):
        gx, gy = klarf_to_gds(123.5, -42.0, 0, 0)
        assert gx == 123.5
        assert gy == -42.0

    def test_array_inputs(self):
        xr = np.array([1000, 2000, 3000], dtype=float)
        yr = np.array([0, 500, 1000], dtype=float)
        gx, gy = klarf_to_gds(xr, yr, 100, 100)
        np.testing.assert_array_equal(gx, [900, 1900, 2900])
        np.testing.assert_array_equal(gy, [-100, 400, 900])

    def test_flip_y(self):
        gx, gy = klarf_to_gds(1000, 2000, 100, 50, flip_y=True)
        assert gx == 900
        assert gy == -1950


# ── FOV bounds ───────────────────────────────────────────────────────


class TestFovBounds:

    def test_centered(self):
        assert fov_bounds(0, 0, 100, 200) == (-50, -100, 50, 100)

    def test_offset(self):
        assert fov_bounds(1000, 2000, 100, 100) == (950, 1950, 1050, 2050)


# ── M2.2 single-layer query ──────────────────────────────────────────


def _rects():
    # Four rectangles spread across the plane.
    return np.array([
        [0, 0, 10, 10],         # near origin
        [100, 100, 110, 110],   # far away
        [45, 45, 55, 55],       # straddles a FOV centered at (50,50)
        [-200, -200, -190, -190],
    ], dtype=np.float64)


class TestQueryFov:

    def test_overlap_only(self):
        out = query_fov(50, 50, 20, 20, _rects())
        # FOV bounds = (40,40,60,60); only the (45,45,55,55) rect overlaps.
        assert out.shape == (1, 4)
        np.testing.assert_array_equal(out[0], [45, 45, 55, 55])

    def test_partial_overlap_counts(self):
        # FOV (0..10) just touches the origin rect and the (45..55) one
        # only if it reaches; pick a FOV that grabs origin rect only.
        out = query_fov(5, 5, 10, 10, _rects())
        assert out.shape == (1, 4)
        np.testing.assert_array_equal(out[0], [0, 0, 10, 10])

    def test_none_overlap(self):
        out = query_fov(5000, 5000, 10, 10, _rects())
        assert out.shape == (0, 4)

    def test_empty_input(self):
        out = query_fov(0, 0, 10, 10, np.empty((0, 4)))
        assert out.shape == (0, 4)

    def test_reversed_corners_normalized(self):
        # Row stored as (x2,y2,x1,y1) should still be found.
        rects = np.array([[55, 55, 45, 45]], dtype=np.float64)
        out = query_fov(50, 50, 20, 20, rects)
        assert out.shape == (1, 4)

    def test_touching_edge_counts(self):
        # Rect right edge exactly on FOV left edge -> overlap (inclusive).
        rects = np.array([[30, 45, 40, 55]], dtype=np.float64)
        out = query_fov(50, 50, 20, 20, rects)  # FOV x in [40,60]
        assert out.shape == (1, 4)

    def test_bad_shape_raises(self):
        with pytest.raises(ValueError):
            query_fov(0, 0, 10, 10, np.zeros((3, 2)))


class TestFovOverlapIndices:

    def test_indices_select_parallel_data(self):
        out = fov_overlap_indices(50, 50, 20, 20, _rects())
        # Only the (45,45,55,55) rect at index 2 overlaps.
        np.testing.assert_array_equal(out, [2])

    def test_empty(self):
        out = fov_overlap_indices(0, 0, 10, 10, np.empty((0, 4)))
        assert out.shape == (0,)

    def test_none_overlap(self):
        out = fov_overlap_indices(5000, 5000, 10, 10, _rects())
        assert out.shape == (0,)


# ── Multi-layer + KLARF-driven query ─────────────────────────────────


class TestQueryFovMulti:

    def _layers(self):
        return {
            (17, 101): np.array([[45, 45, 55, 55]], dtype=np.float64),
            (6, 0): np.array([[48, 48, 52, 52], [900, 900, 910, 910]],
                             dtype=np.float64),
        }

    def test_all_layers(self):
        out = query_fov_multi(50, 50, 20, 20, self._layers())
        assert set(out.keys()) == {(17, 101), (6, 0)}
        assert out[(17, 101)].shape == (1, 4)
        assert out[(6, 0)].shape == (1, 4)  # far rect filtered out

    def test_subset_keys(self):
        out = query_fov_multi(50, 50, 20, 20, self._layers(),
                              keys=[(17, 101)])
        assert set(out.keys()) == {(17, 101)}

    def test_missing_key_skipped(self):
        out = query_fov_multi(50, 50, 20, 20, self._layers(),
                              keys=[(17, 101), (99, 99)])
        assert set(out.keys()) == {(17, 101)}


class TestQueryFovKlarf:

    def test_klarf_centered(self):
        # Image at XREL/YREL (1050,1050); chip corner at (1000,1000) ->
        # GDS center (50,50). Layer rect at (45..55) should be returned.
        layers = {(17, 101): np.array([[45, 45, 55, 55]], dtype=np.float64)}
        out = query_fov_klarf(1050, 1050, 1000, 1000, 20, 20, layers)
        assert out[(17, 101)].shape == (1, 4)
