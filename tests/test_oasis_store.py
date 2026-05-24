"""Tests for tools/oasis_store.py (F2 M1.11b).

Builds tiny OASIS files in tmp_path and verifies the store accumulates
rectangles / polygons / placements into the right per-cell, per-layer
buckets. Cross-checks the streamer roundtrip done in test_oasis_streamer
by reading the SAME file through OasisReader first, so any bug in the
underlying decoder is caught here before it propagates into store
assertions.
"""
from __future__ import annotations

import sys
import zlib
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import oasis_streamer as oas  # noqa: E402
import oasis_store as store_mod  # noqa: E402


# ── Byte-fixture helpers (mirrors test_oasis_streamer.TestCBlock helpers) ────


def _make_uint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _file_header() -> bytes:
    """MAGIC + minimal START (offset_flag=0, all-zero offsets)."""
    return (
        oas.MAGIC
        + bytes([oas.START])
        + _make_uint(3) + b"1.0"
        + bytes([0]) + _make_uint(1)   # unit = 1 (real type 0 + uint 1)
        + _make_uint(0)                 # offset_flag = 0
        + bytes([0] * 12)               # 6 (strict, offset) pairs of zeros
    )


def _file_footer() -> bytes:
    return bytes([oas.END]) + _make_uint(0)


def _explicit_cellname(refnum: int, name: bytes) -> bytes:
    return (
        bytes([oas.CELLNAME_EXP])
        + _make_uint(len(name)) + name
        + _make_uint(refnum)
    )


def _cell_header_by_refnum(refnum: int) -> bytes:
    return bytes([oas.CELL_REFNUM]) + _make_uint(refnum)


def _rect_with_all_fields(layer: int, datatype: int,
                          w: int, h: int, x: int, y: int) -> bytes:
    """Emit a RECTANGLE with W=H=X=Y=D=L bits set (no S, no R).

    The standard sign convention for signed-int is ``(magnitude << 1) | sign``;
    for positive coords used in tests just shift by 1.
    """
    info = 0x7b   # W=1 H=1 X=1 Y=1 D=1 L=1
    return bytes([oas.RECTANGLE, info]) + _make_uint(layer) + _make_uint(datatype) \
        + _make_uint(w) + _make_uint(h) \
        + _make_uint(x << 1) + _make_uint(y << 1)


def _polygon_2delta(layer: int, datatype: int,
                    deltas: list[tuple[int, int]],
                    ax: int, ay: int) -> bytes:
    """POLYGON with point-list type 2 (2-delta Manhattan).

    Each delta is one of (+x, 0), (-x, 0), (0, +y), (0, -y); we encode
    via 2-delta where direction is at bits 1-0 (0=E, 1=N, 2=W, 3=S) and
    magnitude is at bits 2+.
    """
    info = 0x3b   # P=1 X=1 Y=1 D=1 L=1
    out = bytes([oas.POLYGON, info]) + _make_uint(layer) + _make_uint(datatype)
    # Point list: type=2, n=len(deltas), then 2-deltas
    out += _make_uint(2) + _make_uint(len(deltas))
    for dx, dy in deltas:
        if dx > 0:
            raw = (dx << 2) | 0
        elif dx < 0:
            raw = ((-dx) << 2) | 2
        elif dy > 0:
            raw = (dy << 2) | 1
        else:
            raw = ((-dy) << 2) | 3
        out += _make_uint(raw)
    # x, y (signed)
    out += _make_uint(ax << 1) + _make_uint(ay << 1)
    return out


def _placement_to_refnum(refnum: int, x: int, y: int) -> bytes:
    """PLACEMENT (no mag), C=1, N=1 (refnum form), X=1, Y=1."""
    info = 0xf0   # C=1 N=1 X=1 Y=1 R=0 AA=0 F=0
    return bytes([oas.PLACEMENT_NOMAG, info]) + _make_uint(refnum) \
        + _make_uint(x << 1) + _make_uint(y << 1)


# ── Small-file roundtrip ─────────────────────────────────────────────────────


class TestStoreRoundtrip:
    """End-to-end: write a tiny OASIS with 2 cells, RECTANGLE + POLYGON
    + PLACEMENT, and verify the store buckets everything correctly."""

    def _build(self) -> bytes:
        # Two cells:
        #   #1 'A': has 2 rectangles on (5, 0) and 1 polygon on (5, 0)
        #   #0 'TOP': has 1 placement of #1 at (100, 200)
        body = b""
        body += _explicit_cellname(1, b"A")
        body += _explicit_cellname(0, b"TOP")

        # Cell A geometry
        body += _cell_header_by_refnum(1)
        body += _rect_with_all_fields(5, 0, w=10, h=20, x=1, y=2)
        body += _rect_with_all_fields(5, 0, w=30, h=40, x=3, y=4)
        body += _polygon_2delta(5, 0,
                                deltas=[(5, 0), (0, 3), (-5, 0)],
                                ax=0, ay=0)

        # Cell TOP placements
        body += _cell_header_by_refnum(0)
        body += _placement_to_refnum(1, x=100, y=200)

        return _file_header() + body + _file_footer()

    def test_full_walk(self, tmp_path: Path):
        path = tmp_path / "roundtrip.oas"
        path.write_bytes(self._build())

        store = store_mod.OasisGeometryStore(path)
        store.run()

        # Cellnames table populated.
        assert store.cells[0] == "TOP"
        assert store.cells[1] == "A"

        # Cell #1 ('A') has two rectangles stored at the right coordinates.
        rects = store.rectangles_for(1, layer=5, datatype=0)
        assert rects.shape == (2, 4)
        # Row 0: (1, 2, 1+10, 2+20) = (1, 2, 11, 22)
        # Row 1: (3, 4, 33, 44)
        np.testing.assert_array_equal(
            rects, np.array([[1, 2, 11, 22], [3, 4, 33, 44]], dtype=np.int32))

        # One polygon in cell #1 layer 5/0, anchored at (0, 0) with 4 pts
        # (3 deltas + the implicit origin).
        polys = store.polygons_for(1, layer=5, datatype=0)
        assert len(polys) == 1
        # Points: (0,0), (5,0), (5,3), (0,3)
        np.testing.assert_array_equal(
            polys[0], np.array([[0, 0], [5, 0], [5, 3], [0, 3]], dtype=np.int32))

        # Cell TOP has the placement.
        placements = store.placements_for(0)
        assert len(placements) == 1
        p = placements[0]
        assert p.target == 1
        assert p.target_kind == "refnum"
        assert p.x == 100
        assert p.y == 200
        assert p.angle == 0.0
        assert p.flip is False

        # Summary matches the wire.
        summary = store.summary()
        assert summary["total_rectangles"] == 2
        assert summary["total_polygons"] == 1
        assert summary["total_placements"] == 1
        assert summary["cells_with_rectangles"] == 1
        assert summary["cells_with_placements"] == 1


# ── Layer filter ─────────────────────────────────────────────────────────────


class TestLayerFilter:
    def _build_two_layer_rects(self) -> bytes:
        """Two rectangles in cell #0, one on (1, 0) and one on (2, 0)."""
        body = _cell_header_by_refnum(0)
        body += _rect_with_all_fields(1, 0, w=10, h=10, x=0, y=0)
        body += _rect_with_all_fields(2, 0, w=20, h=20, x=5, y=5)
        return _file_header() + body + _file_footer()

    def test_filter_keeps_only_wanted_layer(self, tmp_path: Path):
        path = tmp_path / "two_layer.oas"
        path.write_bytes(self._build_two_layer_rects())

        store = store_mod.OasisGeometryStore(
            path, wanted_layers={(1, 0)})
        store.run()

        # Only the (1, 0) rectangle ended up stored.
        kept = store.rectangles_for(0, layer=1, datatype=0)
        assert kept.shape == (1, 4)
        np.testing.assert_array_equal(
            kept, np.array([[0, 0, 10, 10]], dtype=np.int32))

        # The (2, 0) rectangle was filtered out at the decoder level and
        # never stored.
        dropped = store.rectangles_for(0, layer=2, datatype=0)
        assert dropped.shape == (0, 4)

    def test_no_filter_keeps_everything(self, tmp_path: Path):
        path = tmp_path / "two_layer.oas"
        path.write_bytes(self._build_two_layer_rects())

        store = store_mod.OasisGeometryStore(path)
        store.run()
        assert store.rectangles_for(0, 1, 0).shape == (1, 4)
        assert store.rectangles_for(0, 2, 0).shape == (1, 4)


# ── Large-file guard ─────────────────────────────────────────────────────────


class TestLargeFileGuard:
    def test_large_file_without_filter_raises(self, tmp_path: Path):
        # Build a > 50 MB file by padding the body. We don't actually
        # need the bytes to be valid OASIS past the magic + START -- the
        # guard fires in __init__ on file size alone, before run().
        big = tmp_path / "big.oas"
        big.write_bytes(_file_header() + b"\x00" * (60 * 1024 * 1024)
                        + _file_footer())
        with pytest.raises(ValueError, match="refusing to walk"):
            store_mod.OasisGeometryStore(big)

    def test_large_file_with_filter_passes_guard(self, tmp_path: Path):
        big = tmp_path / "big.oas"
        big.write_bytes(_file_header() + b"\x00" * (60 * 1024 * 1024)
                        + _file_footer())
        # Constructor returns without raising -- we don't run() because
        # the padding bytes aren't real records.
        store_mod.OasisGeometryStore(big, wanted_layers={(1, 0)})

    def test_large_file_with_allow_unfiltered_passes(self, tmp_path: Path):
        big = tmp_path / "big.oas"
        big.write_bytes(_file_header() + b"\x00" * (60 * 1024 * 1024)
                        + _file_footer())
        store_mod.OasisGeometryStore(big, allow_unfiltered=True)


# ── _RectBuffer chunked growth ───────────────────────────────────────────────


class TestRectBufferGrowth:
    def test_growth_across_chunks(self):
        buf = store_mod._RectBuffer(dtype=np.int32)
        # Cross the initial 1024-capacity boundary so a chunk handoff
        # actually happens.
        for i in range(2500):
            buf.add(i, i + 1, i + 2, i + 3)
        out = buf.to_ndarray()
        assert out.shape == (2500, 4)
        np.testing.assert_array_equal(out[0], [0, 1, 2, 3])
        np.testing.assert_array_equal(out[2499], [2499, 2500, 2501, 2502])
        assert out.dtype == np.int32

    def test_empty_buffer(self):
        buf = store_mod._RectBuffer()
        out = buf.to_ndarray()
        assert out.shape == (0, 4)
        assert out.dtype == np.int32


# ── Run-twice guard ──────────────────────────────────────────────────────────


class TestRunGuard:
    def test_double_run_raises(self, tmp_path: Path):
        path = tmp_path / "tiny.oas"
        path.write_bytes(_file_header() + _file_footer())
        store = store_mod.OasisGeometryStore(path)
        store.run()
        with pytest.raises(RuntimeError, match="can only be called once"):
            store.run()


# ── M1.13.3a: consume() callback API equivalence ─────────────────────────────


class TestConsumeEquivalence:
    """The new consume() callback path must produce a store output
    bit-identical to the legacy iter_records dispatch path.

    Run the same OASIS through both paths and deep-compare every
    populated bucket (cellnames / rectangles / polygons / placements /
    record counts).
    """

    def _build_realistic(self) -> bytes:
        body = b""
        body += _explicit_cellname(1, b"A")
        body += _explicit_cellname(0, b"TOP")
        body += _cell_header_by_refnum(1)
        body += _rect_with_all_fields(5, 0, w=10, h=20, x=1, y=2)
        body += _rect_with_all_fields(5, 0, w=30, h=40, x=3, y=4)
        body += _rect_with_all_fields(7, 1, w=5, h=5, x=11, y=12)
        body += _polygon_2delta(5, 0,
                                deltas=[(5, 0), (0, 3), (-5, 0)],
                                ax=0, ay=0)
        body += _cell_header_by_refnum(0)
        body += _placement_to_refnum(1, x=100, y=200)
        body += _placement_to_refnum(1, x=500, y=600)
        return _file_header() + body + _file_footer()

    def _run_via_iter_records(self, path: Path) -> store_mod.OasisGeometryStore:
        """Build a store using the legacy iter_records dispatch path.

        Mirrors the pre-M1.13.3a run() implementation byte-for-byte so
        we can diff its output against the new consume() path."""
        s = store_mod.OasisGeometryStore(path)
        s._has_run = True
        with oas.OasisReader(path) as reader:
            for rid, payload in reader.iter_records():
                s._record_counts[rid] = s._record_counts.get(rid, 0) + 1
                s._consume(rid, payload)
        return s

    def test_outputs_bit_identical(self, tmp_path: Path):
        path = tmp_path / "consume_eq.oas"
        path.write_bytes(self._build_realistic())

        legacy = self._run_via_iter_records(path)
        modern = store_mod.OasisGeometryStore(path)
        modern.run()

        # Cellnames table
        assert legacy.cells == modern.cells
        assert legacy._cellnames == modern._cellnames

        # Rectangles per (cell, layer/datatype)
        assert set(legacy._rect_buffers.keys()) == set(modern._rect_buffers.keys())
        for cell, bufs in legacy._rect_buffers.items():
            assert set(bufs.keys()) == set(modern._rect_buffers[cell].keys())
            for key, lbuf in bufs.items():
                mbuf = modern._rect_buffers[cell][key]
                la = lbuf.to_ndarray()
                ma = mbuf.to_ndarray()
                np.testing.assert_array_equal(la, ma)

        # Polygons per (cell, layer/datatype)
        assert set(legacy._polys.keys()) == set(modern._polys.keys())
        for cell, polys in legacy._polys.items():
            assert set(polys.keys()) == set(modern._polys[cell].keys())
            for key, llist in polys.items():
                mlist = modern._polys[cell][key]
                assert len(llist) == len(mlist)
                for la, ma in zip(llist, mlist):
                    np.testing.assert_array_equal(la, ma)

        # Placements per cell (Placement dataclass equality)
        assert set(legacy._placements.keys()) == set(modern._placements.keys())
        for cell, lplc in legacy._placements.items():
            mplc = modern._placements[cell]
            assert lplc == mplc

        # Record counts
        assert legacy._record_counts == modern._record_counts

    def test_max_records_stops_consume(self, tmp_path: Path):
        path = tmp_path / "consume_max.oas"
        path.write_bytes(self._build_realistic())

        s = store_mod.OasisGeometryStore(path)
        s.run(max_records=4)
        # Bounded by max_records — total decoded records must be <= 4.
        assert sum(s._record_counts.values()) == 4

    def test_progress_callback_fires(self, tmp_path: Path):
        path = tmp_path / "consume_prog.oas"
        path.write_bytes(self._build_realistic())

        events: list[tuple[int, dict]] = []

        def cb(count, stats):
            events.append((count, stats))

        s = store_mod.OasisGeometryStore(path)
        s.run(progress_every=2, progress_callback=cb)
        # At least one progress event should fire.
        assert len(events) >= 1
        for count, stats in events:
            assert count > 0
            assert "cells" in stats and "rectangles" in stats
