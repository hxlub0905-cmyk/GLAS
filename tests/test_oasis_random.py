"""Tests for tools/oasis_random.py (F2 M3.5b random-access cell decode)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import oasis_streamer as oas      # noqa: E402
import oasis_random as orx        # noqa: E402


def _uint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _sint(v: int) -> bytes:
    """OASIS signed-int: sign in the low bit."""
    return _uint((abs(v) << 1) | (1 if v < 0 else 0))


def _ufix(n: int, width: int) -> bytes:
    out = []
    for i in range(width):
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if i < width - 1 else b)
    return bytes(out)


def _astr(s: str) -> bytes:
    b = s.encode()
    return _uint(len(b)) + b


def _rect(layer: int, w: int, h: int, x: int, y: int) -> bytes:
    # info 0x7b: W H X Y D L present (S, R absent). layer/dt/w/h uint, x/y signed.
    return (bytes([oas.RECTANGLE, 0x7b]) + _uint(layer) + _uint(0)
            + _uint(w) + _uint(h) + _sint(x) + _sint(y))


def _build_two_cell() -> tuple[bytes, int, int]:
    """A=ref0 (rect at origin + a placement of B); B=ref1 (rect at 100,100).
    Both cells carry an S_CELL_OFFSET. Returns (bytes, offA, offB)."""
    start = (bytes([oas.START]) + _astr("1.0") + bytes([0])
             + _uint(1000) + _uint(0) + bytes([0] * 12))
    pn = bytes([oas.PROPNAME_IMP]) + _astr("S_CELL_OFFSET")
    cna = bytes([oas.CELLNAME_IMP]) + _astr("A")
    cnb = bytes([oas.CELLNAME_IMP]) + _astr("B")

    def prop(off):    # PROPERTY: C=1 N=1 V=0 U=1; ref 0; value type 8 (uint)
        return (bytes([oas.PROPERTY_NORMAL, 0x16]) + _uint(0)
                + _uint(8) + _ufix(off, 4))

    place_b = bytes([oas.PLACEMENT_NOMAG, 0xC0]) + _uint(1)   # target refnum 1
    cell_a = bytes([oas.CELL_REFNUM]) + _uint(0)
    cell_b = bytes([oas.CELL_REFNUM]) + _uint(1)
    end = bytes([oas.END]) + _uint(0)

    hdr = (oas.MAGIC + start + pn + cna + prop(0) + cnb + prop(0))
    off_a = len(hdr)
    body_a = cell_a + _rect(17, 10, 10, 0, 0) + place_b
    off_b = len(hdr) + len(body_a)
    data = (oas.MAGIC + start + pn + cna + prop(off_a) + cnb + prop(off_b)
            + body_a + cell_b + _rect(17, 20, 20, 100, 100) + end)
    return data, off_a, off_b


class TestRandomAccessReader:

    def test_load_each_cell_in_isolation(self, tmp_path):
        data, off_a, off_b = _build_two_cell()
        p = tmp_path / "two.oas"
        p.write_bytes(data)

        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        assert rar.has_offsets()
        assert rar.offset_for(0) == off_a
        assert rar.offset_for(1) == off_b
        assert rar.offset_for("A") == off_a

        a = rar.load_cell(0)
        assert a.rects((17, 0)).tolist() == [[0, 0, 10, 10]]
        assert a.bbox == (0, 0, 10, 10)
        assert len(a.placements) == 1
        assert a.placements[0].target == 1

        b = rar.load_cell(1)
        # Cell B's rect must NOT include cell A's geometry (isolation).
        assert b.rects((17, 0)).tolist() == [[100, 100, 120, 120]]
        assert b.bbox == (100, 100, 120, 120)
        assert b.placements == []

    def test_memoized(self, tmp_path):
        data, _, _ = _build_two_cell()
        p = tmp_path / "two.oas"
        p.write_bytes(data)
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        assert rar.load_cell(0) is rar.load_cell(0)

    def test_layer_filter_drops_geometry_keeps_placements(self, tmp_path):
        data, _, _ = _build_two_cell()
        p = tmp_path / "two.oas"
        p.write_bytes(data)
        rar = orx.RandomAccessReader(p, wanted_layers={(99, 0)})
        a = rar.load_cell(0)
        assert a.rects((17, 0)).shape[0] == 0 and a.bbox is None
        assert len(a.placements) == 1   # placements are never filtered

    def test_unknown_cell_returns_empty(self, tmp_path):
        data, _, _ = _build_two_cell()
        p = tmp_path / "two.oas"
        p.write_bytes(data)
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        empty = rar.load_cell(999)
        assert empty.is_empty()
        assert empty.bbox is None


def _build_hierarchy_sbbox(places, root_sbbox, child_sbbox) -> bytes:
    """root R=ref0 places child A=ref1 (a 10x10 rect at local origin) at each
    (x, y) in ``places``. Both cells carry S_CELL_OFFSET *and* an
    S_BOUNDING_BOX standard property (F16). ``root_sbbox`` / ``child_sbbox`` are
    [flag, x, y, w, h] value lists written verbatim, so a test can set them
    *different* from the true decoded extent to prove the prune reads the name
    table rather than decoding geometry."""
    start = (bytes([oas.START]) + _astr("1.0") + bytes([0])
             + _uint(1000) + _uint(0) + bytes([0] * 12))
    pn0 = bytes([oas.PROPNAME_IMP]) + _astr("S_CELL_OFFSET")    # refnum 0
    pn1 = bytes([oas.PROPNAME_IMP]) + _astr("S_BOUNDING_BOX")   # refnum 1
    cnr = bytes([oas.CELLNAME_IMP]) + _astr("R")
    cna = bytes([oas.CELLNAME_IMP]) + _astr("A")

    def prop_off(off):
        return (bytes([oas.PROPERTY_NORMAL, 0x16]) + _uint(0)
                + _uint(8) + _ufix(off, 4))

    def prop_bbox(vals):
        # info 0x56: U=5 values, C=1 (propname follows), N=1 (refnum 1).
        body = bytes([oas.PROPERTY_NORMAL, 0x56]) + _uint(1)
        for v in vals:
            body += _uint(8) + _uint(v)
        return body

    xyabs = bytes([15])
    def place(x, y):
        return bytes([oas.PLACEMENT_NOMAG, 0xF0]) + _uint(1) + _sint(x) + _sint(y)

    cell_r = bytes([oas.CELL_REFNUM]) + _uint(0)
    cell_a = bytes([oas.CELL_REFNUM]) + _uint(1)
    end = bytes([oas.END]) + _uint(0)

    # prop_off uses a fixed-width offset, so the header length is invariant —
    # measure it with placeholder offsets, then re-emit with the real ones.
    def header(off_r, off_a):
        return (oas.MAGIC + start + pn0 + pn1
                + cnr + prop_off(off_r) + prop_bbox(root_sbbox)
                + cna + prop_off(off_a) + prop_bbox(child_sbbox))

    off_r = len(header(0, 0))
    body_r = cell_r + xyabs + b"".join(place(x, y) for x, y in places)
    off_a = off_r + len(body_r)
    return (header(off_r, off_a) + body_r
            + cell_a + _rect(17, 10, 10, 0, 0) + end)


class TestSBoundingBoxPrune:
    """F16: when the name table carries S_BOUNDING_BOX, reachable_bbox returns
    it directly (the complete, placement-inclusive bbox) — pruning the ROI
    walk without decoding geometry. The fixtures set the property *larger than*
    the real geometry so the value can only come from the name table."""

    def test_map_populated(self, tmp_path):
        p = tmp_path / "sb.oas"
        p.write_bytes(_build_hierarchy_sbbox(
            [(0, 0)], root_sbbox=[0, 0, 0, 99999, 99999],
            child_sbbox=[0, 0, 0, 20, 20]))
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        assert rar._n_bbox_props == 2
        # keyed by refnum and by name, stored as (x0, y0, x1, y1)
        assert rar.sbbox_for(1) == (0, 0, 20, 20)
        assert rar.sbbox_for("R") == (0, 0, 99999, 99999)

    def test_reachable_bbox_reads_name_table(self, tmp_path):
        # child's real geometry is 10x10; S_BOUNDING_BOX says 20x20.
        p = tmp_path / "sb.oas"
        p.write_bytes(_build_hierarchy_sbbox(
            [(0, 0)], root_sbbox=[0, 0, 0, 99999, 99999],
            child_sbbox=[0, 0, 0, 20, 20]))
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        assert tuple(rar.reachable_bbox(1)) == (0, 0, 20, 20)   # not decoded 10x10
        assert tuple(rar.reachable_bbox(0)) == (0, 0, 99999, 99999)

    def test_flag_nonzero_ignored(self, tmp_path):
        # flag != 0 means "own geometry only" (not the complete bbox) — must be
        # skipped, so reachable_bbox falls back to the decoded union (10x10).
        p = tmp_path / "sb.oas"
        p.write_bytes(_build_hierarchy_sbbox(
            [(0, 0)], root_sbbox=[1, 0, 0, 99999, 99999],
            child_sbbox=[1, 0, 0, 20, 20]))
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        assert rar.sbbox_for(1) is None
        assert tuple(rar.reachable_bbox(1)) == (0, 0, 10, 10)   # decoded

    def test_walk_prunes_far_instance(self, tmp_path):
        p = tmp_path / "sb.oas"
        p.write_bytes(_build_hierarchy_sbbox(
            [(0, 0), (100_000, 100_000)],
            root_sbbox=[0, 0, 0, 100_020, 100_020],
            child_sbbox=[0, 0, 0, 20, 20]))
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        res = orx.walk_roi(rar, 0, (0, 0, 5, 5), 17, 0)
        st = res["stats"]
        assert res["rects"].tolist() == [[0, 0, 10, 10]]
        assert st.instances_pruned == 1
        # decode-free prune: load_cell_bbox (the CE/decode path) never ran.
        assert rar._bbox_memo == {}
        # telemetry: prune flagged on, and only the ROI-hit child was decoded
        # (root + child A), not the pruned far instance's subtree.
        assert st.sbbox_prune is True
        assert st.cells_decoded == 2


def _build_hierarchy(places: list[tuple[int, int]]) -> bytes:
    """root R=ref0 places child A=ref1 (a 10x10 rect at local origin) at
    each (x, y) in ``places``. Both cells carry S_CELL_OFFSET."""
    start = (bytes([oas.START]) + _astr("1.0") + bytes([0])
             + _uint(1000) + _uint(0) + bytes([0] * 12))
    pn = bytes([oas.PROPNAME_IMP]) + _astr("S_CELL_OFFSET")
    cnr = bytes([oas.CELLNAME_IMP]) + _astr("R")
    cna = bytes([oas.CELLNAME_IMP]) + _astr("A")

    def prop(off):
        return (bytes([oas.PROPERTY_NORMAL, 0x16]) + _uint(0)
                + _uint(8) + _ufix(off, 4))

    xyabs = bytes([15])
    # PLACEMENT info C=1 N=1 X=1 Y=1 -> 0xF0; refnum 1; signed x, y.
    def place(x, y):
        return bytes([oas.PLACEMENT_NOMAG, 0xF0]) + _uint(1) + _sint(x) + _sint(y)

    cell_r = bytes([oas.CELL_REFNUM]) + _uint(0)
    cell_a = bytes([oas.CELL_REFNUM]) + _uint(1)
    end = bytes([oas.END]) + _uint(0)

    hdr = oas.MAGIC + start + pn + cnr + prop(0) + cna + prop(0)
    off_r = len(hdr)
    body_r = cell_r + xyabs + b"".join(place(x, y) for x, y in places)
    off_a = len(hdr) + len(body_r)
    return (oas.MAGIC + start + pn + cnr + prop(off_r) + cna + prop(off_a)
            + body_r + cell_a + _rect(17, 10, 10, 0, 0) + end)


def _build_big_grid(nx: int, ny: int, pitch: int) -> bytes:
    """root R places child A (10x10 rect) as an nx*ny type-1 array at
    ``pitch`` spacing. Exercises analytic-extent pruning (M3.5e)."""
    start = (bytes([oas.START]) + _astr("1.0") + bytes([0])
             + _uint(1000) + _uint(0) + bytes([0] * 12))
    pn = bytes([oas.PROPNAME_IMP]) + _astr("S_CELL_OFFSET")
    cnr = bytes([oas.CELLNAME_IMP]) + _astr("R")
    cna = bytes([oas.CELLNAME_IMP]) + _astr("A")

    def prop(off):
        return (bytes([oas.PROPERTY_NORMAL, 0x16]) + _uint(0)
                + _uint(8) + _ufix(off, 4))

    rep1 = bytes([1]) + _uint(nx - 2) + _uint(ny - 2) + _uint(pitch) + _uint(pitch)
    # PLACEMENT info 0xF8 = C N X Y R; cell_ref 1; x0 y0; type-1 repetition.
    place = bytes([oas.PLACEMENT_NOMAG, 0xF8]) + _uint(1) + _sint(0) + _sint(0) + rep1
    cell_r = bytes([oas.CELL_REFNUM]) + _uint(0)
    cell_a = bytes([oas.CELL_REFNUM]) + _uint(1)
    end = bytes([oas.END]) + _uint(0)
    hdr = oas.MAGIC + start + pn + cnr + prop(0) + cna + prop(0)
    off_r = len(hdr)
    body_r = cell_r + place
    off_a = len(hdr) + len(body_r)
    return (oas.MAGIC + start + pn + cnr + prop(off_r) + cna + prop(off_a)
            + body_r + cell_a + _rect(17, 10, 10, 0, 0) + end)


class TestBigGridRepetition:
    """M3.5e: a 1M-instance array must not be materialized for bbox, must
    be pruned instantly when outside the ROI, and must NOT be eagerly
    expanded at decode time."""

    def test_placement_repetition_kept_raw(self, tmp_path):
        p = tmp_path / "big.oas"
        p.write_bytes(_build_big_grid(1000, 1000, 1000))
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        root = rar.load_cell(0)
        pl = root.placements[0]
        assert pl.repetition_type == 1
        assert pl.repetition_raw == (1000, 1000, 1000, 1000)
        assert pl.repetition_offsets == []          # never materialized

    def test_roi_inside_picks_one(self, tmp_path):
        p = tmp_path / "big.oas"
        p.write_bytes(_build_big_grid(1000, 1000, 1000))
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        res = orx.walk_roi(rar, 0, (4990, 4990, 5010, 5010), 17, 0)
        assert res["rects"].tolist() == [[5000, 5000, 5010, 5010]]
        assert res["stats"].instances_pruned == 1_000_000 - 1
        # F16 perf: the analytic sub-grid clip must keep one instance WITHOUT
        # materializing the whole 1M grid — only a small padded neighbourhood
        # (25 placement candidates + the one surviving cell's single rect).
        assert res["stats"].max_array_k <= 25
        assert res["stats"].instances_materialized <= 30

    def test_roi_outside_pruned_instantly(self, tmp_path):
        p = tmp_path / "big.oas"
        p.write_bytes(_build_big_grid(1000, 1000, 1000))
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        res = orx.walk_roi(rar, 0, (9_000_000, 9_000_000, 9_001_000, 9_001_000),
                           17, 0)
        assert res["rects"].shape[0] == 0
        assert res["stats"].instances_pruned == 1_000_000   # whole array culled


class TestReachableBbox:
    """F11: the read-only reachable_bbox accessor returns the whole-cell
    extent (own + placed children), for sizing the whole-chip export grid."""

    def test_union_of_placements_grid(self, tmp_path):
        # child A = 10x10 rect at origin, placed at 3 points -> union extent.
        p = tmp_path / "hier.oas"
        p.write_bytes(_build_hierarchy([(0, 0), (100, 0), (0, 100)]))
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        assert tuple(rar.reachable_bbox(0)) == (0, 0, 110, 110)

    def test_nm_scaled(self, tmp_path):
        p = tmp_path / "hier2.oas"
        p.write_bytes(_build_hierarchy([(0, 0)]))
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        s = getattr(rar, "_nm_per_grid", 1.0) or 1.0
        assert tuple(rar.reachable_bbox_nm(0)) == (0, 0, 10 * s, 10 * s)

    def test_unknown_cell_none(self, tmp_path):
        p = tmp_path / "hier3.oas"
        p.write_bytes(_build_hierarchy([(0, 0)]))
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        assert rar.reachable_bbox(999) is None


class TestRectRepetition:
    """M3.5e correctness: a RECTANGLE with repetition must expand into N
    rects (previously _decode_at kept only the first → lost geometry)."""

    def test_rect_type2_expands(self, tmp_path):
        start = (bytes([oas.START]) + _astr("1.0") + bytes([0])
                 + _uint(1000) + _uint(0) + bytes([0] * 12))
        pn = bytes([oas.PROPNAME_IMP]) + _astr("S_CELL_OFFSET")
        cn = bytes([oas.CELLNAME_IMP]) + _astr("A")

        def prop(off):
            return (bytes([oas.PROPERTY_NORMAL, 0x16]) + _uint(0)
                    + _uint(8) + _ufix(off, 4))

        # RECTANGLE info 0x7f = S0 W H X Y R D L; layer17 dt0 w10 h10 x0 y0,
        # then type-2 repetition: 3 along x at pitch 100.
        rect = (bytes([oas.RECTANGLE, 0x7f]) + _uint(17) + _uint(0)
                + _uint(10) + _uint(10) + _sint(0) + _sint(0)
                + bytes([2]) + _uint(3 - 2) + _uint(100))
        cell = bytes([oas.CELL_REFNUM]) + _uint(0)
        end = bytes([oas.END]) + _uint(0)
        hdr = oas.MAGIC + start + pn + cn + prop(0)
        off = len(hdr)
        p = tmp_path / "rep.oas"
        p.write_bytes(oas.MAGIC + start + pn + cn + prop(off) + cell + rect + end)
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        cc = rar.load_cell(0)
        got = sorted(cc.rects((17, 0)).tolist())
        assert got == [[0, 0, 10, 10], [100, 0, 110, 10], [200, 0, 210, 10]]

    def test_huge_rect_array_not_materialized_at_load(self, tmp_path):
        # A rect with a 1000x1000 (1M) repetition must load as ONE spec with
        # an analytic bbox — never expanded during the scan (M3.5e).
        start = (bytes([oas.START]) + _astr("1.0") + bytes([0])
                 + _uint(1000) + _uint(0) + bytes([0] * 12))
        pn = bytes([oas.PROPNAME_IMP]) + _astr("S_CELL_OFFSET")
        cn = bytes([oas.CELLNAME_IMP]) + _astr("A")

        def prop(off):
            return (bytes([oas.PROPERTY_NORMAL, 0x16]) + _uint(0)
                    + _uint(8) + _ufix(off, 4))

        rep1 = bytes([1]) + _uint(998) + _uint(998) + _uint(1000) + _uint(1000)
        rect = (bytes([oas.RECTANGLE, 0x7f]) + _uint(17) + _uint(0)
                + _uint(10) + _uint(10) + _sint(0) + _sint(0) + rep1)
        cell = bytes([oas.CELL_REFNUM]) + _uint(0)
        end = bytes([oas.END]) + _uint(0)
        hdr = oas.MAGIC + start + pn + cn + prop(0)
        off = len(hdr)
        p = tmp_path / "huge.oas"
        p.write_bytes(oas.MAGIC + start + pn + cn + prop(off) + cell + rect + end)
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        cc = rar.load_cell(0)
        # one spec, not 1M rects (rect_count works for either the tuple-list
        # backing or the native columnar _rcol backing — both stay lazy).
        assert cc.rect_count((17, 0)) == 1
        assert cc.bbox == (0, 0, 999010, 999010)         # analytic extent
        assert cc.rects((17, 0)).shape == (1_000_000, 4)  # lazy materialization

    def test_walk_clips_huge_rect_array_to_roi(self, tmp_path):
        # The walk's own-geometry emission must clip a 1M-rect repetition to the
        # ROI, NOT materialize+transform the whole array (the slow path).
        start = (bytes([oas.START]) + _astr("1.0") + bytes([0])
                 + _uint(1000) + _uint(0) + bytes([0] * 12))
        pn = bytes([oas.PROPNAME_IMP]) + _astr("S_CELL_OFFSET")
        cn = bytes([oas.CELLNAME_IMP]) + _astr("A")

        def prop(off):
            return (bytes([oas.PROPERTY_NORMAL, 0x16]) + _uint(0)
                    + _uint(8) + _ufix(off, 4))

        rep1 = bytes([1]) + _uint(998) + _uint(998) + _uint(1000) + _uint(1000)
        rect = (bytes([oas.RECTANGLE, 0x7f]) + _uint(17) + _uint(0)
                + _uint(10) + _uint(10) + _sint(0) + _sint(0) + rep1)
        cell = bytes([oas.CELL_REFNUM]) + _uint(0)
        end = bytes([oas.END]) + _uint(0)
        hdr = oas.MAGIC + start + pn + cn + prop(0)
        off = len(hdr)
        p = tmp_path / "huge.oas"
        p.write_bytes(oas.MAGIC + start + pn + cn + prop(off) + cell + rect + end)
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        # ROI around the instance at (500_000, 500_000) (grid index 500,500).
        res = orx.walk_roi(rar, 0, (499_995, 499_995, 500_015, 500_015), 17, 0)
        assert res["rects"].tolist() == [[500_000, 500_000, 500_010, 500_010]]
        assert res["stats"].max_array_k <= 25            # clipped, not 1M
        assert res["stats"].instances_materialized <= 25


def _rectdt(layer: int, dt: int, w: int, h: int, x: int, y: int) -> bytes:
    return (bytes([oas.RECTANGLE, 0x7b]) + _uint(layer) + _uint(dt)
            + _uint(w) + _uint(h) + _sint(x) + _sint(y))


def _build_ce_hierarchy() -> bytes:
    """root R(0) places M(1) at origin. M, like a Calibre D2DB geometry
    cell, emits in stream order: [PLACEMENT of L(2)] then [CE boundary rect
    108/250 == M's own bbox] then [bulk 17/0 device rects]. L(2) is a leaf
    17/0 rect. Exercises the early-stop CE read: load_cell_bbox(M) must see
    L + the CE bbox and NOT the 17/0 device rects that follow the CE rect."""
    start = (bytes([oas.START]) + _astr("1.0") + bytes([0])
             + _uint(1000) + _uint(0) + bytes([0] * 12))
    pn = bytes([oas.PROPNAME_IMP]) + _astr("S_CELL_OFFSET")

    def prop(off):
        return (bytes([oas.PROPERTY_NORMAL, 0x16]) + _uint(0)
                + _uint(8) + _ufix(off, 4))

    xyabs = bytes([15])

    def place(ref, x, y):
        return bytes([oas.PLACEMENT_NOMAG, 0xF0]) + _uint(ref) + _sint(x) + _sint(y)

    cell_r = bytes([oas.CELL_REFNUM]) + _uint(0)
    cell_m = bytes([oas.CELL_REFNUM]) + _uint(1)
    cell_l = bytes([oas.CELL_REFNUM]) + _uint(2)
    end = bytes([oas.END]) + _uint(0)

    # M owns two 17/0 device rects -> own bbox (200,200)-(310,210); the CE
    # rect carries exactly that extent.
    body_r = cell_r + xyabs + place(1, 0, 0)
    body_m = (cell_m + xyabs + place(2, 50, 50)
              + _rectdt(108, 250, 110, 10, 200, 200)     # CE == own bbox
              + _rectdt(17, 0, 10, 10, 200, 200)
              + _rectdt(17, 0, 10, 10, 300, 200))
    body_l = cell_l + xyabs + _rectdt(17, 0, 10, 10, 0, 0)

    off = [0, 0, 0]
    for _ in range(5):
        h = oas.MAGIC + start + pn
        for i, n in enumerate(["R", "M", "L"]):
            h += bytes([oas.CELLNAME_IMP]) + _astr(n) + prop(off[i])
        cur = len(h)
        for i, b in enumerate((body_r, body_m, body_l)):
            off[i] = cur
            cur += len(b)
    return h + body_r + body_m + body_l + end


class TestCeBoundaryEarlyStop:
    """M3.5e.3: a configured bbox_layer makes reachable_bbox read only up to
    the per-cell boundary rect, skipping bulk geometry — and must stay
    bit-identical to the full decode."""

    def _reader(self, tmp_path, **kw):
        p = tmp_path / "ce.oas"
        p.write_bytes(_build_ce_hierarchy())
        return orx.RandomAccessReader(p, wanted_layers={(17, 0)}, **kw)

    def test_load_cell_bbox_stops_at_ce_rect(self, tmp_path):
        rar = self._reader(tmp_path, bbox_layer=(108, 250))
        m = rar.load_cell_bbox(1)
        # Placements (decoded before the CE rect) are present...
        assert [pl.target for pl in m.placements] == [2]
        # ...own bbox comes from the CE rect...
        assert m.bbox == (200, 200, 310, 210)
        # ...and the 17/0 device rects that FOLLOW the CE rect were skipped.
        assert (17, 0) not in m.rect_specs
        assert list(m.rect_specs.keys()) == [(108, 250)]

    def test_reachable_bbox_union_with_child(self, tmp_path):
        rar = self._reader(tmp_path, bbox_layer=(108, 250))
        res = orx.walk_roi(rar, 0, (-10_000, -10_000, 10_000, 10_000), 17, 0)
        # M own (200,200),(300,200) + L placed at (50,50).
        got = sorted(res["rects"].tolist())
        assert got == [[50, 50, 60, 60], [200, 200, 210, 210],
                       [300, 200, 310, 210]]

    def test_bit_identical_to_full_decode(self, tmp_path):
        p = tmp_path / "ce.oas"
        p.write_bytes(_build_ce_hierarchy())
        roi = (-10_000, -10_000, 10_000, 10_000)
        full = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        fast = orx.RandomAccessReader(p, wanted_layers={(17, 0)},
                                      bbox_layer=(108, 250))
        rf = sorted(orx.walk_roi(full, 0, roi, 17, 0)["rects"].tolist())
        rq = sorted(orx.walk_roi(fast, 0, roi, 17, 0)["rects"].tolist())
        assert rf == rq

    def test_reach_memo_reused_across_walks(self, tmp_path):
        rar = self._reader(tmp_path, bbox_layer=(108, 250))
        wide = (-10_000, -10_000, 10_000, 10_000)
        orx.walk_roi(rar, 0, wide, 17, 0)        # first walk fills reach memo
        assert rar._reach_memo                   # populated and reader-level
        # Spy: a second walk must not touch the lightweight loader at all —
        # every reachable_bbox lookup is a cache hit.
        calls: list = []
        orig = rar.load_cell_bbox

        def spy(cid):
            calls.append(cid)
            return orig(cid)

        rar.load_cell_bbox = spy
        res = orx.walk_roi(rar, 0, wide, 17, 0)
        assert calls == []
        assert sorted(res["rects"].tolist()) == [
            [50, 50, 60, 60], [200, 200, 210, 210], [300, 200, 310, 210]]

    def test_roi_prunes_via_ce_bbox(self, tmp_path):
        # ROI far from everything -> the CE bbox lets the walk prune M's
        # whole subtree without ever emitting geometry.
        rar = self._reader(tmp_path, bbox_layer=(108, 250))
        res = orx.walk_roi(rar, 0, (10**7, 10**7, 10**7 + 100, 10**7 + 100),
                           17, 0)
        assert res["rects"].shape[0] == 0


class TestWalkRoi:

    def _reader(self, tmp_path, places):
        p = tmp_path / "h.oas"
        p.write_bytes(_build_hierarchy(places))
        return orx.RandomAccessReader(p, wanted_layers={(17, 0)})

    def test_prunes_to_single_instance(self, tmp_path):
        rar = self._reader(tmp_path, [(0, 0), (1000, 0), (2000, 0)])
        res = orx.walk_roi(rar, 0, (900, -50, 1100, 50), 17, 0)
        assert res["rects"].tolist() == [[1000, 0, 1010, 10]]
        assert res["stats"].instances_visited == 1
        assert res["stats"].instances_pruned == 2

    def test_wide_roi_selects_all(self, tmp_path):
        rar = self._reader(tmp_path, [(0, 0), (1000, 0), (2000, 0)])
        res = orx.walk_roi(rar, 0, (-100, -100, 3000, 100), 17, 0)
        got = sorted(res["rects"].tolist())
        assert got == [[0, 0, 10, 10], [1000, 0, 1010, 10], [2000, 0, 2010, 10]]
        assert res["stats"].instances_visited == 3
        assert res["stats"].instances_pruned == 0

    def test_placement_prep_cached_across_rois(self, tmp_path):
        # F16-B M6: the placement gather is built once per cell and reused for
        # later ROIs (same object), while still giving correct per-ROI results.
        rar = self._reader(tmp_path, [(0, 0), (1000, 0), (2000, 0)])
        r1 = orx.walk_roi(rar, 0, (900, -50, 1100, 50), 17, 0)
        prep = rar.load_cell(0)._place_prep
        assert prep is not None
        assert r1["rects"].tolist() == [[1000, 0, 1010, 10]]
        # A different ROI reuses the cached prep object and selects a different
        # instance correctly.
        r2 = orx.walk_roi(rar, 0, (1900, -50, 2100, 50), 17, 0)
        assert rar.load_cell(0)._place_prep is prep        # reused, not rebuilt
        assert r2["rects"].tolist() == [[2000, 0, 2010, 10]]

    def test_many_individual_placements_pruned_vectorized(self, tmp_path):
        # A cell with thousands of *individual* placements (no repetition) must
        # be pruned with a single batched apply_to_rects, not one numpy call per
        # placement (the regression that made big real chips take minutes).
        n = 20_000
        places = [(i * 100, 0) for i in range(n)]   # x = 0 .. 1,999,900
        rar = self._reader(tmp_path, places)
        res = orx.walk_roi(rar, 0, (1_499_995, -50, 1_500_015, 50), 17, 0)
        assert res["rects"].tolist() == [[1_500_000, 0, 1_500_010, 10]]
        assert res["stats"].instances_visited == 1
        assert res["stats"].placements_scanned == n      # all scanned, batched
        assert res["stats"].instances_pruned == n - 1

    def test_roi_outside_everything(self, tmp_path):
        rar = self._reader(tmp_path, [(0, 0), (1000, 0)])
        res = orx.walk_roi(rar, 0, (50_000, 50_000, 60_000, 60_000), 17, 0)
        assert res["rects"].shape[0] == 0
        assert res["stats"].instances_visited == 0
        assert res["stats"].instances_pruned == 2


class TestResolveLayerName:
    """F3 M2: OASIS LAYERNAME -> human-readable layer label."""

    LN = [("METAL1", (17, 17), (0, 0)),
          ("VIA", (20, 20), (0, -1)),       # datatype INF
          ("ALL", (0, -1), (0, -1))]        # all-layers catch-all (skipped)

    def test_exact_single_value(self):
        assert orx.resolve_layer_name(self.LN, 17, 0) == "METAL1"

    def test_datatype_inf(self):
        assert orx.resolve_layer_name(self.LN, 20, 5) == "VIA"

    def test_all_layers_catch_all_skipped(self):
        # An (0, INF) layer interval is a placeholder; it must NOT label an
        # arbitrary layer (the "every layer shows the first name" bug).
        assert orx.resolve_layer_name(self.LN, 99, 0) == ""

    def test_catch_all_does_not_mask_specific(self):
        # The catch-all coexists with a specific record; the specific wins and
        # the catch-all never bleeds onto other layers.
        assert orx.resolve_layer_name(self.LN, 17, 0) == "METAL1"
        assert orx.resolve_layer_name(self.LN, 20, 0) == "VIA"

    def test_no_match_or_empty(self):
        assert orx.resolve_layer_name([], 1, 2) == ""

    def test_narrower_record_wins(self):
        ln = [("R", (10, 30), (0, 0)), ("X", (20, 20), (0, 0))]
        assert orx.resolve_layer_name(ln, 20, 0) == "X"


class TestGridClip:
    """F16 perf: _clip_grid_offsets must be a *superset* of the true ROI
    survivors under every D4 transform (so the downstream exact mask yields
    identical geometry) while actually shrinking the materialized set."""

    def test_clip_is_superset_under_d4(self):
        from oasis_walker import Transform
        rng = np.random.default_rng(0)
        nx, ny, xs, ys = 200, 150, 37, 41
        placed = np.array([0.0, 0.0, 12.0, 9.0])          # child bbox, local
        full = oas.repetition_offsets_np(1, (nx, ny, xs, ys))
        plb = np.empty((full.shape[0], 4))
        plb[:, 0] = placed[0] + full[:, 0]; plb[:, 1] = placed[1] + full[:, 1]
        plb[:, 2] = placed[2] + full[:, 0]; plb[:, 3] = placed[3] + full[:, 1]
        for angle in (0, 90, 180, 270):
            for flip in (False, True):
                T = Transform.from_placement(123, -456, angle, flip, 1.0)
                rootb = T.apply_to_rects(plb)
                for _ in range(15):
                    i = int(rng.integers(0, nx)); j = int(rng.integers(0, ny))
                    lx, ly = i * xs, j * ys
                    rb = T.apply_to_rects(np.array(
                        [[placed[0] + lx, placed[1] + ly,
                          placed[2] + lx, placed[3] + ly]]))[0]
                    roi = (rb[0] - 3, rb[1] - 3, rb[2] + 3, rb[3] + 3)
                    truth = set(map(tuple,
                                    full[orx._roi_overlap_mask(rootb, roi)].tolist()))
                    clipped = orx._clip_grid_offsets(
                        1, (nx, ny, xs, ys), placed, T, roi)
                    cl = set(map(tuple, clipped.tolist()))
                    assert truth, "fixture should have at least one survivor"
                    assert truth <= cl                      # never drops a hit
                    assert len(cl) < full.shape[0]          # and actually clips

    def test_clip_1d_arrays(self):
        from oasis_walker import Transform
        T = Transform.identity()
        placed = np.array([0.0, 0.0, 10.0, 10.0])
        # type 2: 1000 along x at pitch 50; ROI around index 400 (x=20000)
        off2 = orx._clip_grid_offsets(2, (1000, 50), placed, T,
                                      (19995, -5, 20015, 15))
        assert off2.shape[0] < 1000 and (off2[:, 1] == 0).all()
        # type 3: 1000 along y at pitch 50; ROI around index 400 (y=20000)
        off3 = orx._clip_grid_offsets(3, (1000, 50), placed, T,
                                      (-5, 19995, 15, 20015))
        assert off3.shape[0] < 1000 and (off3[:, 0] == 0).all()

    def test_clip_type8_axis_aligned(self):
        from oasis_walker import Transform
        T = Transform.identity()
        placed = np.array([0.0, 0.0, 10.0, 10.0])
        # type 8: 500 along (40,0) x 500 along (0,40) = 250k grid; ROI near
        # index (100, 200) -> (4000, 8000). Must clip to a tiny neighbourhood.
        raw = (500, 500, (40, 0), (0, 40))
        full = oas.repetition_offsets_np(8, raw)
        roi = (3995, 7995, 4015, 8015)
        clipped = orx._clip_grid_offsets(8, raw, placed, T, roi)
        assert clipped.shape[0] <= 25 and full.shape[0] == 250_000
        # superset check: every true survivor of the full array is kept
        plb = np.column_stack((placed[0] + full[:, 0], placed[1] + full[:, 1],
                               placed[2] + full[:, 0], placed[3] + full[:, 1]))
        truth = set(map(tuple, full[orx._roi_overlap_mask(plb, roi)].tolist()))
        assert truth and truth <= set(map(tuple, clipped.tolist()))
        # a skew type-8 lattice is not clippable -> full fallback
        skew = orx._clip_grid_offsets(8, (10, 10, (40, 5), (0, 40)), placed,
                                      T, roi)
        assert skew.shape[0] == 100


class TestExtVectorized:
    """F16-B 'B': _ext_from_columnar must match a per-spec repetition_extent for
    every repetition type (the vectorized regular types + looped 10/11)."""

    def test_matches_repetition_extent(self):
        cases = [
            (None, None), (0, None),
            (1, (3, 4, 100, 200)), (1, (2, 2, -50, 30)),
            (2, (5, 50)), (3, (5, 50)),
            (8, (3, 3, (40, 0), (0, 40))), (8, (2, 4, (10, 5), (-3, 20))),
            (9, (4, (7, 3))),
            (10, ([(1, 2), (3, 4)],)), (11, (2, [(1, 1), (2, 2)])),
        ]
        M = len(cases)
        base = np.arange(M * 4, dtype=np.float64).reshape(M, 4)
        rt = np.fromiter(((-1 if c[0] is None else c[0]) for c in cases),
                         dtype=np.int16, count=M)
        rr = np.empty(M, dtype=object)
        for i, c in enumerate(cases):
            rr[i] = c[1]
        ext = orx._ext_from_columnar(base, rt, rr)
        for i, (t, raw) in enumerate(cases):
            e = np.array(oas.repetition_extent(t, raw), dtype=np.float64)
            assert np.array_equal(ext[i], base[i] + e), (t, raw, ext[i],
                                                         base[i] + e)

    def test_empty(self):
        base = np.empty((0, 4), dtype=np.float64)
        rt = np.empty(0, dtype=np.int16)
        rr = np.empty(0, dtype=object)
        assert orx._ext_from_columnar(base, rt, rr).shape == (0, 4)


class TestPrebuiltIndex:
    """F23 M1: a reader built with ``prebuilt_index=`` (the index handed over
    from another reader over the same file) must be byte/value identical to one
    that ran its own ``scan_cell_offsets`` — that equivalence is what lets the
    batch process pool hand the orchestrator's index to its workers instead of
    each worker rescanning the name table."""

    def _index_fields(self, rar):
        return (rar._by_refnum, rar._by_name, rar._unit, rar._layernames,
                rar._sbbox_by_refnum, rar._sbbox_by_name, rar._offset_flag,
                rar._offsets_via)

    def test_injected_index_matches_scanned(self, tmp_path):
        data, off_a, off_b = _build_two_cell()
        p = tmp_path / "two.oas"
        p.write_bytes(data)

        scanned = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        snap = scanned.index_snapshot()
        # No scan_cell_offsets call happens on this build — it reuses ``snap``.
        injected = orx.RandomAccessReader(
            p, wanted_layers={(17, 0)}, prebuilt_index=snap)

        assert self._index_fields(injected) == self._index_fields(scanned)
        assert injected.offset_for(0) == off_a == scanned.offset_for(0)
        assert injected.offset_for("A") == off_a
        assert injected.offset_for(1) == off_b
        # Geometry decoded through the injected-index reader is identical.
        for cid in (0, 1):
            a = scanned.load_cell(cid)
            b = injected.load_cell(cid)
            assert a.rects((17, 0)).tolist() == b.rects((17, 0)).tolist()
            assert a.bbox == b.bbox

    def test_injected_sbbox_matches(self, tmp_path):
        p = tmp_path / "sb.oas"
        p.write_bytes(_build_hierarchy_sbbox(
            [(0, 0)], root_sbbox=[0, 0, 0, 99999, 99999],
            child_sbbox=[0, 0, 0, 20, 20]))
        scanned = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        injected = orx.RandomAccessReader(
            p, wanted_layers={(17, 0)},
            prebuilt_index=scanned.index_snapshot())
        assert injected.sbbox_for(1) == scanned.sbbox_for(1) == (0, 0, 20, 20)
        assert injected.sbbox_for("R") == (0, 0, 99999, 99999)
        assert injected._n_bbox_props == scanned._n_bbox_props == 2

    def test_clone_reuses_index(self, tmp_path):
        data, _, _ = _build_two_cell()
        p = tmp_path / "two.oas"
        p.write_bytes(data)
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        clone = rar.clone()
        # clone() now forwards the parent index, so the clone shares the very
        # same index object (no rescan) yet decodes identical geometry.
        assert clone.index_snapshot() is rar.index_snapshot()
        assert (clone.load_cell(0).rects((17, 0)).tolist()
                == rar.load_cell(0).rects((17, 0)).tolist())
