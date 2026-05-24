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

    def test_roi_outside_pruned_instantly(self, tmp_path):
        p = tmp_path / "big.oas"
        p.write_bytes(_build_big_grid(1000, 1000, 1000))
        rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        res = orx.walk_roi(rar, 0, (9_000_000, 9_000_000, 9_001_000, 9_001_000),
                           17, 0)
        assert res["rects"].shape[0] == 0
        assert res["stats"].instances_pruned == 1_000_000   # whole array culled


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
        assert len(cc.rect_specs[(17, 0)]) == 1          # one spec, not 1M rects
        assert cc.bbox == (0, 0, 999010, 999010)         # analytic extent
        assert cc.rects((17, 0)).shape == (1_000_000, 4)  # lazy materialization


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

    def test_roi_outside_everything(self, tmp_path):
        rar = self._reader(tmp_path, [(0, 0), (1000, 0)])
        res = orx.walk_roi(rar, 0, (50_000, 50_000, 60_000, 60_000), 17, 0)
        assert res["rects"].shape[0] == 0
        assert res["stats"].instances_visited == 0
        assert res["stats"].instances_pruned == 2
