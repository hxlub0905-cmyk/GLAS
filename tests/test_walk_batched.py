"""F27 M4: the pure-Python batched walk (``walk_roi_batched``) must produce the
same emitted rect / polygon SET as the recursive ``walk_roi`` (order differs —
rasterization is order-independent — so compare sorted). No native extension is
needed, so this file never skips.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app", "tests"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import oasis_streamer as oas          # noqa: E402
import oasis_random as orx            # noqa: E402
import test_oasis_random as T         # noqa: E402
import test_oasis_store as S          # noqa: E402


def _sorted_rows(a):
    return sorted(map(tuple, a.tolist()))


def _cmp(path, wanted, layer, dt, rois):
    for roi in rois:
        r1 = orx.RandomAccessReader(path, wanted_layers=wanted)
        a = orx.walk_roi(r1, 0, roi, layer, dt)
        r2 = orx.RandomAccessReader(path, wanted_layers=wanted)
        b = orx.walk_roi_batched(r2, 0, roi, layer, dt)
        assert _sorted_rows(a["rects"]) == _sorted_rows(b["rects"]), \
            f"rects differ at roi={roi}"
        pa = sorted(_sorted_rows(p) for p in a["polys"])
        pb = sorted(_sorted_rows(p) for p in b["polys"])
        assert pa == pb, f"polys differ at roi={roi}"


def test_batched_matches_walk_roi_flat_hierarchy(tmp_path):
    # 1200 distinct placements of a leaf rect — exercises the no-rep grouped path.
    places = [(x * 40, y * 40) for y in range(30) for x in range(40)]
    p = tmp_path / "h.oas"
    p.write_bytes(T._build_hierarchy(places))
    _cmp(p, {(17, 0)}, 17, 0, [
        (-50, -50, 40 * 40 + 50, 30 * 40 + 50),   # all
        (300, 300, 620, 620),                      # tight (pruning)
        (10_000_000, 10_000_000, 10_000_100, 10_000_100),  # empty
    ])


def test_batched_matches_walk_roi_big_grid(tmp_path):
    # A 1M-instance placement array (type-1 repetition) — the in-descent analytic
    # clip must give the same survivors as walk_roi.
    p = tmp_path / "g.oas"
    p.write_bytes(T._build_big_grid(1000, 1000, 1000))
    _cmp(p, {(17, 0)}, 17, 0, [
        (1900, 1900, 2100, 2100),      # around one instance
        (-100, -100, 5000, 5000),      # a block of instances
        (10_000_000, 0, 10_000_100, 100),  # empty
    ])


def test_batched_matches_walk_roi_small_grid(tmp_path):
    p = tmp_path / "g5.oas"
    p.write_bytes(T._build_big_grid(5, 5, 1000))
    _cmp(p, {(17, 0)}, 17, 0, [(-100, -100, 6000, 6000), (1900, 1900, 2100, 2100)])


def test_batched_matches_walk_roi_sbbox_hierarchy(tmp_path):
    # F30 M4 guard: the batched machinery must be byte-identical to walk_roi on an
    # S_BOUNDING_BOX chip too (the LTV shape) — the existing batched tests only use
    # S_CELL_OFFSET fixtures, so the sbbox-prune descent path was untested. A leaf
    # (10x10 rect) placed on a 20x20 grid; sbbox set to the true extents so the
    # prune actually fires on the tight/empty ROIs and both walks must prune the
    # same instances.
    places = [(x * 40, y * 40) for y in range(20) for x in range(20)]
    p = tmp_path / "sbh.oas"
    p.write_bytes(T._build_hierarchy_sbbox(
        places, root_sbbox=[0, 0, 0, 20 * 40 + 10, 20 * 40 + 10],
        child_sbbox=[0, 0, 0, 10, 10]))
    _cmp(p, {(17, 0)}, 17, 0, [
        (-50, -50, 20 * 40 + 50, 20 * 40 + 50),   # all placements
        (300, 300, 340, 340),                      # tight — sbbox prunes far ones
        (-10, 300, 850, 340),                      # a horizontal strip (one row)
        (10_000_000, 10_000_000, 10_000_100, 10_000_100),  # empty
    ])


def test_batched_matches_walk_roi_rect_repetition(tmp_path):
    # A RECTANGLE with a type-2 repetition inside the cell (own-geometry clip).
    start = (bytes([oas.START]) + T._astr("1.0") + bytes([0])
             + T._uint(1000) + T._uint(0) + bytes([0] * 12))
    pn = bytes([oas.PROPNAME_IMP]) + T._astr("S_CELL_OFFSET")
    cn = bytes([oas.CELLNAME_IMP]) + T._astr("R")

    def prop(off):
        return (bytes([oas.PROPERTY_NORMAL, 0x16]) + T._uint(0)
                + T._uint(8) + T._ufix(off, 4))

    rect = (bytes([oas.RECTANGLE, 0x7f]) + T._uint(17) + T._uint(0)
            + T._uint(10) + T._uint(10) + T._sint(0) + T._sint(0)
            + bytes([2]) + T._uint(998) + T._uint(100))   # 1000 @ pitch 100
    cell = bytes([oas.CELL_REFNUM]) + T._uint(0)
    end = bytes([oas.END]) + T._uint(0)
    hdr = oas.MAGIC + start + pn + cn + prop(0)
    off = len(hdr)
    p = tmp_path / "rr.oas"
    p.write_bytes(oas.MAGIC + start + pn + cn + prop(off) + cell + rect + end)
    _cmp(p, {(17, 0)}, 17, 0, [
        (-50, -50, 99910, 50),         # all 1000
        (49900, -50, 50100, 50),       # a couple in the middle
        (-50, -50, 250, 50),           # first few
    ])


def test_batched_matches_walk_roi_polygon(tmp_path):
    start = (bytes([oas.START]) + T._astr("1.0") + bytes([0])
             + T._uint(1000) + T._uint(0) + bytes([0] * 12))
    pn = bytes([oas.PROPNAME_IMP]) + T._astr("S_CELL_OFFSET")
    cn = bytes([oas.CELLNAME_IMP]) + T._astr("R")

    def prop(off):
        return (bytes([oas.PROPERTY_NORMAL, 0x16]) + T._uint(0)
                + T._uint(8) + T._ufix(off, 4))

    poly = S._polygon_2delta(17, 0, [(100, 0), (0, 100), (-100, 0), (0, -100)], 0, 0)
    cell = bytes([oas.CELL_REFNUM]) + T._uint(0)
    end = bytes([oas.END]) + T._uint(0)
    hdr = oas.MAGIC + start + pn + cn + prop(0)
    off = len(hdr)
    p = tmp_path / "poly.oas"
    p.write_bytes(oas.MAGIC + start + pn + cn + prop(off) + cell + poly + end)
    _cmp(p, {(17, 0)}, 17, 0, [(-200, -200, 400, 400),
                               (10_000, 10_000, 10_100, 10_100)])
