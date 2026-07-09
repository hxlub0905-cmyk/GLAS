"""F30 M4: the recursive walk's leaf fast-path (``_WALK_LEAF_BATCH``) — a
pure-geometry leaf target does not recurse; its surviving instances accumulate
into a per-(target, matrix) group that is emitted ONCE (vectorized over every
instance) after the parent's placement loop — must be **byte-identical** to the
per-instance recursion. Compare ``walk_roi`` with the fast-path ON vs OFF.

The group collapses BOTH a dense repetition array's ROI survivors AND multiple
distinct placements of the same leaf in one parent (both land in the same group).
A leaf whose own rects repeat, that carries polygons, or that exceeds
``_LEAF_PLAIN_MAX_RECTS`` (the F29 giant-coords memory guard) declines the
vectorized emit and falls to the exact per-instance emit; those fallbacks are
guarded too.
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


def _rows(a):
    return sorted(map(tuple, a.tolist()))


def _walk(path, wanted, layer, dt, roi, flag, monkeypatch):
    monkeypatch.setattr(orx, "_WALK_LEAF_BATCH", flag)
    r = orx.RandomAccessReader(path, wanted_layers=wanted)
    try:
        w = orx.walk_roi(r, 0, roi, layer, dt)
        return (_rows(w["rects"]), sorted(_rows(p) for p in w["polys"]))
    finally:
        r.close()


def _cmp_on_off(path, wanted, layer, dt, rois, monkeypatch):
    for roi in rois:
        on = _walk(path, wanted, layer, dt, roi, True, monkeypatch)
        off = _walk(path, wanted, layer, dt, roi, False, monkeypatch)
        assert on == off, f"leaf-batch differs at roi={roi}"


def test_leaf_batch_dense_repetition_array(tmp_path, monkeypatch):
    # A 1M-instance placement array of a leaf rect — the group collapses the ROI
    # survivors (whether a block keeps many or a tight ROI keeps one) into one
    # vectorized emit; must match the per-instance walk over all ROI sizes.
    p = tmp_path / "g.oas"
    p.write_bytes(T._build_big_grid(1000, 1000, 1000))
    _cmp_on_off(p, {(17, 0)}, 17, 0, [
        (1900, 1900, 2100, 2100),      # one instance
        (-100, -100, 5000, 5000),      # a block of instances
        (10_000_000, 0, 10_000_100, 100),  # empty
    ], monkeypatch)


def test_leaf_batch_many_distinct_placements(tmp_path, monkeypatch):
    # 1200 DISTINCT single-instance placements of one leaf, same parent + same
    # orientation -> they all land in ONE per-(target, matrix) group and emit
    # once. This is the cut-2 cross-placement collapse (not just a repetition
    # array); must be byte-identical to the per-instance recursion.
    places = [(x * 40, y * 40) for y in range(30) for x in range(40)]
    p = tmp_path / "h.oas"
    p.write_bytes(T._build_hierarchy(places))
    _cmp_on_off(p, {(17, 0)}, 17, 0, [
        (-50, -50, 40 * 40 + 50, 30 * 40 + 50),
        (300, 300, 620, 620),
        (10_000_000, 10_000_000, 10_000_100, 10_000_100),
    ], monkeypatch)


def test_leaf_batch_sbbox_prune(tmp_path, monkeypatch):
    # Distinct single-instance placements of one leaf on an S_BOUNDING_BOX chip
    # (the LTV shape): the survivors that pass the sbbox prune are grouped and
    # emitted once — guards the group emit is byte-identical under sbbox pruning.
    places = [(x * 40, y * 40) for y in range(20) for x in range(20)]
    p = tmp_path / "sbh.oas"
    p.write_bytes(T._build_hierarchy_sbbox(
        places, root_sbbox=[0, 0, 0, 20 * 40 + 10, 20 * 40 + 10],
        child_sbbox=[0, 0, 0, 10, 10]))
    _cmp_on_off(p, {(17, 0)}, 17, 0, [
        (-50, -50, 20 * 40 + 50, 20 * 40 + 50),
        (300, 300, 340, 340),
        (-10, 300, 850, 340),
        (10_000_000, 10_000_000, 10_000_100, 10_000_100),
    ], monkeypatch)


def _build_array_of_repeated_rect_leaf(rep1: bytes) -> bytes:
    """root R places leaf A via a single placement carrying ``rep1`` (a type-1
    repetition, so len(sel) > 1 -> fast path); A's own rect carries a type-2
    repetition (10 @ pitch 100) so ``rect_plain_coords`` returns None and the fast
    path must decline to the exact per-instance emit."""
    start = (bytes([oas.START]) + T._astr("1.0") + bytes([0])
             + T._uint(1000) + T._uint(0) + bytes([0] * 12))
    pn = bytes([oas.PROPNAME_IMP]) + T._astr("S_CELL_OFFSET")
    cnr = bytes([oas.CELLNAME_IMP]) + T._astr("R")
    cna = bytes([oas.CELLNAME_IMP]) + T._astr("A")

    def prop(off):
        return (bytes([oas.PROPERTY_NORMAL, 0x16]) + T._uint(0)
                + T._uint(8) + T._ufix(off, 4))

    # PLACEMENT info 0xF8 = C N X Y R; cell_ref 1; x0 y0; type-1 repetition.
    place = (bytes([oas.PLACEMENT_NOMAG, 0xF8]) + T._uint(1)
             + T._sint(0) + T._sint(0) + rep1)
    rect = (bytes([oas.RECTANGLE, 0x7f]) + T._uint(17) + T._uint(0)
            + T._uint(10) + T._uint(10) + T._sint(0) + T._sint(0)
            + bytes([2]) + T._uint(8) + T._uint(100))   # 10 @ pitch 100
    cell_r = bytes([oas.CELL_REFNUM]) + T._uint(0)
    cell_a = bytes([oas.CELL_REFNUM]) + T._uint(1)
    end = bytes([oas.END]) + T._uint(0)

    def header(off_r, off_a):
        return (oas.MAGIC + start + pn + cnr + prop(off_r) + cna + prop(off_a))

    off_r = len(header(0, 0))
    body_r = cell_r + place
    off_a = off_r + len(body_r)
    return header(off_r, off_a) + body_r + cell_a + rect + end


def test_leaf_batch_repeated_rects_fall_back(tmp_path, monkeypatch):
    # R places A as a 2x2 type-1 array (len(sel) > 1 -> fast path fires); A's own
    # rect repeats -> rect_plain_coords returns None -> the vectorized path
    # declines and falls to the exact per-instance emit. Guard that fallback is
    # still byte-identical.
    rep1 = bytes([1]) + T._uint(0) + T._uint(0) + T._uint(1000) + T._uint(1000)
    p = tmp_path / "rr.oas"
    p.write_bytes(_build_array_of_repeated_rect_leaf(rep1))
    _cmp_on_off(p, {(17, 0)}, 17, 0, [
        (-50, -50, 1100, 1100),        # all 4 instances -> sel=4 -> fallback loop
        (-50, -50, 2000, 50),          # bottom row -> sel=2 -> fallback loop
        (10_000, 10_000, 10_100, 10_100),  # empty
    ], monkeypatch)


def test_leaf_batch_size_cap_falls_back(tmp_path, monkeypatch):
    # A plain leaf placed as a dense array, but with _LEAF_PLAIN_MAX_RECTS lowered
    # below the leaf's rect count so rect_plain_coords declines (the F29 memory
    # guard: a giant flat cell must never cache its coords). The fast path then
    # falls to the per-instance emit, which must stay byte-identical to recursion.
    monkeypatch.setattr(orx, "_LEAF_PLAIN_MAX_RECTS", 0)
    p = tmp_path / "cap.oas"
    p.write_bytes(T._build_big_grid(5, 5, 1000))
    _cmp_on_off(p, {(17, 0)}, 17, 0, [
        (-100, -100, 6000, 6000),      # all 25 -> len(sel) > 1 -> capped fallback
        (1900, 1900, 2100, 2100),      # one instance
    ], monkeypatch)
