"""F26 M0: the optional native decode extension must be byte-for-byte identical
to the pure-Python varint readers it replaces.

Skipped automatically when the extension isn't built (no compiler / no CI
artifact), so the suite still passes everywhere; on a machine where
``oasis_fastdecode`` is present (CI build, or a local ``build_ext``) it pins the
native decoder against ``OasisStream.read_uvarint`` / ``read_svarint``.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

fast = pytest.importorskip("oasis_fastdecode")
import oasis_streamer as oas  # noqa: E402


def _encode_uvarint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | 0x80 if v else b)
        if not v:
            return bytes(out)


def _encode_svarint(v: int) -> bytes:
    return _encode_uvarint((abs(v) << 1) | (1 if v < 0 else 0))


def _py_uvarint(b: bytes) -> int:
    return oas.OasisStream(io.BytesIO(b)).read_uvarint()


def _py_svarint(b: bytes) -> int:
    return oas.OasisStream(io.BytesIO(b)).read_svarint()


_UVALS = [0, 1, 2, 127, 128, 129, 255, 256, 16383, 16384,
          2 ** 16, 2 ** 31, 2 ** 32, 2 ** 53 - 1, 123_456_789, 2 ** 63 - 1]
_SVALS = [0, 1, -1, 2, -2, 5, -5, 1000, -1000, 2 ** 31, -(2 ** 31),
          2 ** 40, -(2 ** 40)]


def test_native_version_and_selftest():
    assert fast.selftest() == fast.VERSION


@pytest.mark.parametrize("v", _UVALS)
def test_uvarint_matches_python(v):
    enc = _encode_uvarint(v)
    val, pos = fast.decode_uvarint(enc, 0)
    assert val == v == _py_uvarint(enc)
    assert pos == len(enc)


@pytest.mark.parametrize("v", _SVALS)
def test_svarint_matches_python(v):
    enc = _encode_svarint(v)
    val, pos = fast.decode_svarint(enc, 0)
    assert val == v == _py_svarint(enc)
    assert pos == len(enc)


def test_uvarint_resumes_at_offset():
    # Decoding mid-buffer returns the right new position (used to walk a record).
    blob = _encode_uvarint(300) + _encode_uvarint(7) + _encode_uvarint(2 ** 20)
    v1, p1 = fast.decode_uvarint(blob, 0)
    v2, p2 = fast.decode_uvarint(blob, p1)
    v3, p3 = fast.decode_uvarint(blob, p2)
    assert (v1, v2, v3) == (300, 7, 2 ** 20)
    assert p3 == len(blob)


def test_uvarint_eof_raises():
    with pytest.raises(Exception):
        fast.decode_uvarint(b"\x80\x80", 0)   # continuation set, then EOF


# ── decode_rect_run (M2a): native rectangle-run decoder ──────────────────────
pytest.importorskip("numpy")
import numpy as np  # noqa: E402


def _uint(n):
    o = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        o.append(b | 0x80 if n else b)
        if not n:
            return bytes(o)


def _sint(v):
    return _uint((abs(v) << 1) | (1 if v < 0 else 0))


def _astr(s):
    b = s.encode()
    return _uint(len(b)) + b


def _file_with_rects(body: bytes):
    """MAGIC + START + CELL(refnum 0) + body + END, and the offset of body."""
    start = (bytes([oas.START]) + _astr("1.0") + bytes([0]) + _uint(1000)
             + _uint(0) + bytes([0] * 12))
    cell = bytes([oas.CELL_REFNUM]) + _uint(0)
    head = oas.MAGIC + start + cell
    end = bytes([oas.END]) + _uint(0)
    return head + body + end, len(head)


def _py_rects(data, tmp_path, layer=17, dt=0):
    from oasis_store import OasisGeometryStore
    p = tmp_path / "rects.oas"
    p.write_bytes(data)
    s = OasisGeometryStore(p, allow_unfiltered=True)
    s.run()
    return np.asarray(s.rectangles_for(0, layer, dt)).astype(np.int64)


def _rect_full(layer, dt, w, h, x, y):
    return (bytes([oas.RECTANGLE, 0x7b]) + _uint(layer) + _uint(dt)
            + _uint(w) + _uint(h) + _sint(x) + _sint(y))


def _rect_xy(x, y):                                  # info 0x18: X,Y only (modal)
    return bytes([oas.RECTANGLE, 0x18]) + _sint(x) + _sint(y)


def test_rect_run_matches_python(tmp_path):
    body = bytearray(_rect_full(17, 0, 10, 10, 0, 0))
    for i in range(1, 500):
        body += _rect_xy((i * 13) % 2000, (i * 7) % 9000)
    data, pos = _file_with_rects(bytes(body))
    new_pos, rects, lay, dtp, w, h, x, y, xyrel, stop = \
        fast.decode_rect_run(memoryview(data), pos, 0, 0, 0, 0, 0, 0, 0)
    assert stop == 2                                 # stopped at END
    assert np.array_equal(np.asarray(rects), _py_rects(data, tmp_path))


def test_rect_run_xyrelative_toggle(tmp_path):
    # XYRELATIVE makes x/y deltas accumulate; the native run must honour it.
    body = bytearray(_rect_full(17, 0, 4, 4, 100, 100))
    body += bytes([oas.XYRELATIVE])
    for _ in range(50):
        body += _rect_xy(5, 7)                       # each adds (5,7)
    data, pos = _file_with_rects(bytes(body))
    _np, rects, *_rest = fast.decode_rect_run(
        memoryview(data), pos, 0, 0, 0, 0, 0, 0, 0)
    assert np.array_equal(np.asarray(rects), _py_rects(data, tmp_path))


def test_rect_run_stops_on_layer_change_and_resumes(tmp_path):
    # Two layers: native decodes layer-A run, hands back at the layer-B rect;
    # a second call (with the carried modal) decodes layer B.
    body = bytearray()
    for i in range(10):
        body += (_rect_full(17, 0, 10, 10, i, i) if i == 0 else _rect_xy(i, i))
    body += _rect_full(20, 0, 6, 6, 500, 500)        # layer change → stop
    body += _rect_xy(501, 501)
    data, pos = _file_with_rects(bytes(body))
    p1, r1, lay, dtp, w, h, x, y, xyrel, stop1 = fast.decode_rect_run(
        memoryview(data), pos, 0, 0, 0, 0, 0, 0, 0)
    assert stop1 == 20 and len(r1) == 10             # first run = layer 17
    assert np.array_equal(np.asarray(r1), _py_rects(data, tmp_path, 17, 0))
    p2, r2, *_rest, stop2 = fast.decode_rect_run(
        memoryview(data), p1, lay, dtp, w, h, x, y, xyrel)
    assert stop2 == 2 and len(r2) == 2               # second run = layer 20
    assert np.array_equal(np.asarray(r2), _py_rects(data, tmp_path, 20, 0))


def test_rect_run_stops_on_repetition(tmp_path):
    # A rect with a repetition (info bit 0x04) is handed back to Python. info
    # 0x1c = R | X | Y (modal layer/dt/w/h); native reads X,Y then rewinds to
    # the start of this record (it never reads the repetition payload).
    rep_rect = bytes([oas.RECTANGLE, 0x1c]) + _sint(5) + _sint(5)
    body = _rect_full(17, 0, 10, 10, 0, 0) + rep_rect
    data, pos = _file_with_rects(body)
    _np, rects, *_rest, stop = fast.decode_rect_run(
        memoryview(data), pos, 0, 0, 0, 0, 0, 0, 0)
    assert stop == 20 and len(rects) == 1            # only the first, no-rep rect
