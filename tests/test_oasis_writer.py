"""Tests for glas/core/oasis_writer.py (F9 M1/M4).

The writer is validated by round-tripping through GLAS's own
``oasis_streamer`` reader: write geometry out, read it back, assert the
records and coordinates survive. This makes the reader the oracle for the
writer's byte layout. (KLayout open is the separate manual acceptance gate
in M3.)
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _sub in ("glas/core",):
    _p = REPO_ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import oasis_streamer as oas  # noqa: E402
import oasis_writer as w  # noqa: E402


# ── Read-back helper ─────────────────────────────────────────────────────────


def _read_back(path):
    """Iterate records, returning (record_ids, rectangles, polygons).

    rectangles: list of (layer, datatype, x1, y1, x2, y2)
    polygons:   list of (layer, datatype, [abs (x, y) verts])
    """
    rids, rects, polys = [], [], []
    for rid, p in oas.OasisReader(path).iter_records():
        rids.append(rid)
        if rid == oas.RECTANGLE:
            x, y = p["x"], p["y"]
            rects.append((p["layer"], p["datatype"],
                          x, y, x + p["width"], y + p["height"]))
        elif rid == oas.POLYGON:
            x, y = p["x"], p["y"]
            verts = [(x + px, y + py) for px, py in p["points"]]
            polys.append((p["layer"], p["datatype"], verts))
    return rids, rects, polys


# ── Encode primitives ────────────────────────────────────────────────────────


@pytest.mark.parametrize("n", [0, 1, 127, 128, 16383, 16384, 5_000_000])
def test_encode_unsigned_roundtrip(n):
    assert oas.decode_unsigned_int(io.BytesIO(w.encode_unsigned_int(n))) == n


@pytest.mark.parametrize("n", [0, 1, -1, 5, -5, 123456, -123456])
def test_encode_signed_roundtrip(n):
    assert oas.decode_signed_int(io.BytesIO(w.encode_signed_int(n))) == n


@pytest.mark.parametrize("x", [1.0, 1000.0, 0.0, 0.001, 1.5, -2.0, -0.25])
def test_encode_real_roundtrip(x):
    assert oas.decode_real(io.BytesIO(w.encode_real(x))) == pytest.approx(x)


def test_encode_negative_unsigned_raises():
    with pytest.raises(ValueError):
        w.encode_unsigned_int(-1)


# ── Known-good byte layout (golden fixture from test_oasis_streamer) ─────────


def test_rectangle_bytes_match_fixture():
    ring = [(0, 0), (10, 0), (10, 10), (0, 10)]
    assert w._emit_geometry(17, 0, [ring]) == bytes([20, 0x7B, 17, 0, 10, 10, 0, 0])


# ── Round-trip through the reader ────────────────────────────────────────────


def test_rectangle_roundtrip(tmp_path):
    p = tmp_path / "rect.oas"
    w.write_oasis(p, [(17, 0, [[(0, 0), (40, 0), (40, 30), (0, 30)]])], unit=1000)
    rids, rects, polys = _read_back(p)
    assert oas.RECTANGLE in rids and oas.POLYGON not in rids
    assert rects == [(17, 0, 0, 0, 40, 30)]
    assert polys == []


def test_closed_ring_detected_as_rectangle(tmp_path):
    # A repeated closing vertex must not defeat rect detection.
    p = tmp_path / "closed.oas"
    ring = [(5, 5), (15, 5), (15, 25), (5, 25), (5, 5)]
    w.write_oasis(p, [(3, 0, [ring])])
    rids, rects, _ = _read_back(p)
    assert oas.RECTANGLE in rids
    assert rects == [(3, 0, 5, 5, 15, 25)]


def test_polygon_roundtrip(tmp_path):
    p = tmp_path / "tri.oas"
    tri = [(0, 0), (20, 0), (0, 30)]
    w.write_oasis(p, [(7, 2, [tri])])
    rids, rects, polys = _read_back(p)
    assert oas.POLYGON in rids and oas.RECTANGLE not in rids
    assert rects == []
    assert len(polys) == 1
    layer, dt, verts = polys[0]
    assert (layer, dt) == (7, 2)
    assert verts[:3] == [(0, 0), (20, 0), (0, 30)]


def test_non_axis_polygon_roundtrip(tmp_path):
    # 45-degree edges -> arbitrary g-delta form must survive.
    p = tmp_path / "diag.oas"
    shape = [(0, 0), (100, 50), (50, 100), (-30, 40)]
    w.write_oasis(p, [(9, 0, [shape])])
    _, _, polys = _read_back(p)
    assert polys[0][2][:4] == shape


def test_multi_layer(tmp_path):
    p = tmp_path / "multi.oas"
    w.write_oasis(p, [
        (17, 0, [[(0, 0), (10, 0), (10, 10), (0, 10)]]),
        (25, 1, [[(0, 0), (5, 0), (0, 5)]]),
    ])
    _, rects, polys = _read_back(p)
    assert rects == [(17, 0, 0, 0, 10, 10)]
    assert len(polys) == 1 and polys[0][:2] == (25, 1)


def test_empty_and_degenerate_skipped(tmp_path):
    p = tmp_path / "empty.oas"
    w.write_oasis(p, [
        (17, 0, []),                       # no polygons
        (18, 0, [[(0, 0), (1, 1)]]),       # degenerate (< 3 verts)
    ])
    rids, rects, polys = _read_back(p)
    assert rects == [] and polys == []
    # File still well-formed: reader reaches END.
    assert oas.END in rids


def test_serialize_is_deterministic():
    layers = [(17, 0, [[(0, 0), (10, 0), (10, 10), (0, 10)]])]
    assert w.serialize_oasis(layers, unit=1000) == w.serialize_oasis(layers, unit=1000)


def test_end_record_padded_to_256():
    # KLayout requires the END record to occupy exactly 256 bytes; verify the
    # serialized stream ends with a 256-byte END (id 2 + scheme 0 + pad).
    data = w.serialize_oasis([(17, 0, [[(0, 0), (10, 0), (10, 10), (0, 10)]])],
                             unit=1000)
    end_id = data.rfind(bytes([w._END]))
    # the END record (from its id byte to EOF) must be exactly 256 bytes
    assert len(data) - end_id == 256
    assert data[end_id:end_id + 2] == bytes([w._END, 0])   # id + scheme 0


def test_padded_end_still_roundtrips(tmp_path):
    p = tmp_path / "padded.oas"
    w.write_oasis(p, [(17, 0, [[(0, 0), (40, 0), (40, 30), (0, 30)]])], unit=1000)
    rids, rects, _ = _read_back(p)
    assert oas.END in rids
    assert rects == [(17, 0, 0, 0, 40, 30)]


def test_stream_writer_matches_serialize(tmp_path):
    layers = [
        (17, 0, [[(0, 0), (40, 0), (40, 30), (0, 30)]]),
        (25, 0, [[(0, 0), (20, 0), (0, 15)]]),
    ]
    p = tmp_path / "stream.oas"
    with w.OasisStreamWriter(p, unit=1000, cellname="SAMPLE") as sw:
        for layer, dt, polys in layers:
            sw.add_polygons(layer, dt, polys)
    assert p.read_bytes() == w.serialize_oasis(layers, unit=1000, cellname="SAMPLE")


def test_stream_writer_roundtrips(tmp_path):
    p = tmp_path / "stream_rt.oas"
    with w.OasisStreamWriter(p, unit=1000) as sw:
        sw.add_polygons(17, 0, [[(0, 0), (40, 0), (40, 30), (0, 30)]])
        sw.add_polygons(25, 0, [[(0, 0), (20, 0), (0, 15)]])
    rids, rects, polys = _read_back(p)
    assert oas.END in rids
    assert rects == [(17, 0, 0, 0, 40, 30)]
    assert len(polys) == 1 and polys[0][0] == 25
