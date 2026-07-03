"""F27 M7f: the native type-0/1 polygon point-list decoder
(``oasis_fastdecode.decode_pointlist_01``) must be byte-for-byte identical to the
pure-Python ``oasis_streamer.decode_point_list`` type 0/1 branch it stands in for
— same points, same implicit (0,0) anchor, same polygon auto-closure.

Skipped when the extension isn't built (like test_fastdecode).
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app", "tests"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_FAST = pytest.importorskip("oasis_fastdecode")
if getattr(_FAST, "VERSION", 0) < 8:
    pytest.skip("oasis_fastdecode VERSION < 8 (no decode_pointlist_01)",
                allow_module_level=True)
import oasis_streamer as oas          # noqa: E402


def _uint(x: int) -> bytes:
    out = bytearray()
    while True:
        b = x & 0x7F
        x >>= 7
        out.append(b | 0x80 if x else b)
        if not x:
            return bytes(out)


def _sint(v: int) -> bytes:
    return _uint((abs(v) << 1) | (1 if v < 0 else 0))


def _both(ptype: int, deltas: list, for_polygon: bool):
    """Decode the same point-list via the Python path (BytesIO, no _buf) and the
    native kernel; return (python_points_list, native_array, native_new_pos)."""
    delta_bytes = b"".join(_sint(d) for d in deltas)
    full = _uint(ptype) + _uint(len(deltas)) + delta_bytes
    py = oas.decode_point_list(io.BytesIO(full), for_polygon=for_polygon)
    npos, nat = _FAST.decode_pointlist_01(delta_bytes, 0, ptype,
                                          len(deltas), 1 if for_polygon else 0)
    return py, nat, npos, len(delta_bytes)


@pytest.mark.parametrize("ptype", [0, 1])
@pytest.mark.parametrize("deltas", [
    [5, 3],                 # even n → clean rectangle
    [5, 3, 7],              # odd n
    [10, -4, 6, -8, 2],     # negatives, longer
    [1],                    # single step
    [1000000, -999999],     # large-ish coords
])
def test_native_matches_python_polygon(ptype, deltas):
    py, nat, npos, consumed = _both(ptype, deltas, for_polygon=True)
    assert npos == consumed                     # cursor advanced exactly
    assert np.array_equal(np.asarray(py, dtype=np.int64), nat)


@pytest.mark.parametrize("ptype", [0, 1])
def test_native_matches_python_path_no_closure(ptype):
    # for_polygon=False → no implicit closure point (PATH semantics).
    py, nat, npos, consumed = _both(ptype, [5, 3, 7], for_polygon=False)
    assert npos == consumed
    assert np.array_equal(np.asarray(py, dtype=np.int64), nat)
    assert nat.shape[0] == 4                     # (0,0) + 3 steps, no closure


# ── end-to-end: the streamer's _read_polygon through a real OasisStream ──────
# This exercises the actual _buf/_pos handoff (the direct-kernel test above uses
# a bare bytes buffer). A POLYGON record: info 0x3b (P X Y D L), layer 1, dt 0,
# then the point-list, then x=0 y=0.
import oasis_streamer as _oas          # noqa: E402
from test_oasis_streamer import _geom_reader  # noqa: E402


def _read_poly_points(ptype, deltas, native):
    saved = _oas._POLY_NATIVE
    _oas._POLY_NATIVE = saved if native else None
    try:
        pl = _uint(ptype) + _uint(len(deltas)) + b"".join(_sint(d) for d in deltas)
        bs = bytes([0x3b, 1, 0]) + pl + bytes([0, 0])
        p = _geom_reader(bs)._read_polygon()
        return np.asarray(p["points"], dtype=np.int64), p["point_count"]
    finally:
        _oas._POLY_NATIVE = saved


@pytest.mark.parametrize("ptype", [0, 1])
@pytest.mark.parametrize("deltas", [[5, 3], [5, 3, 7], [10, -4, 6, -8]])
def test_streamer_polygon_native_vs_python(ptype, deltas):
    pts_on, n_on = _read_poly_points(ptype, deltas, native=True)
    pts_off, n_off = _read_poly_points(ptype, deltas, native=False)
    assert n_on == n_off
    assert np.array_equal(pts_on, pts_off)
