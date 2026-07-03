"""F26 M2a-integrate: the native-accelerated per-cell decode
(``RandomAccessReader._decode_at_native``) must be byte-for-byte identical to
the pure-Python ``_decode_at_py`` it stands in for.

The native path gobbles whole RECTANGLE runs in one C call and stores them in
the columnar ``_rcol`` form; the Python path stores tuple-list ``rect_specs``.
Both are normalized by the same ``CellContent`` accessors, so every test here
loads the *same* cell twice — once with the extension forced off, once on — and
asserts the resulting geometry, bbox, placements and repetition descriptors
match exactly.

Skipped automatically when the extension isn't built (no compiler / no CI
artifact present), like ``test_fastdecode.py``.
"""
from __future__ import annotations

import sys
import zlib
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("oasis_fastdecode")
import oasis_streamer as oas      # noqa: E402
import oasis_random as orx        # noqa: E402


# ── byte builders ────────────────────────────────────────────────────────────
def _uint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _sint(v: int) -> bytes:
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


def _rect_full(layer, dt, w, h, x, y) -> bytes:
    # info 0x7b: W H X Y D L present (S, R absent).
    return (bytes([oas.RECTANGLE, 0x7b]) + _uint(layer) + _uint(dt)
            + _uint(w) + _uint(h) + _sint(x) + _sint(y))


def _rect_xy(x, y) -> bytes:                # info 0x18: X, Y only (modal reuse)
    return bytes([oas.RECTANGLE, 0x18]) + _sint(x) + _sint(y)


def _rect_rep_xy(x, y, nx, dx) -> bytes:
    # info 0x1c: R | X | Y. Repetition type 2 (a row of nx along x, step dx).
    return (bytes([oas.RECTANGLE, 0x1c]) + _sint(x) + _sint(y)
            + _uint(2) + _uint(nx - 2) + _uint(dx))


def _cblock(inner: bytes) -> bytes:
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    comp = co.compress(inner) + co.flush()
    return (bytes([oas.CBLOCK]) + _uint(0) + _uint(len(inner))
            + _uint(len(comp)) + comp)


def _one_cell_file(body: bytes) -> bytes:
    """A single cell (refnum 0) carrying ``body``, with an S_CELL_OFFSET so a
    RandomAccessReader can seek to it."""
    start = (bytes([oas.START]) + _astr("1.0") + bytes([0])
             + _uint(1000) + _uint(0) + bytes([0] * 12))
    pn = bytes([oas.PROPNAME_IMP]) + _astr("S_CELL_OFFSET")
    cn = bytes([oas.CELLNAME_IMP]) + _astr("A")

    def prop(off):           # PROPERTY: C=1 N=1 V=0 U=1; ref 0; value uint(8)
        return (bytes([oas.PROPERTY_NORMAL, 0x16]) + _uint(0)
                + _uint(8) + _ufix(off, 4))

    hdr = oas.MAGIC + start + pn + cn + prop(0)   # prop length is offset-free
    off = len(hdr)
    cell = bytes([oas.CELL_REFNUM]) + _uint(0) + body
    end = bytes([oas.END]) + _uint(0)
    return oas.MAGIC + start + pn + cn + prop(off) + cell + end


# ── comparison harness ───────────────────────────────────────────────────────
def _load(path, wanted, *, native):
    """Load cell 0 with the native extension forced on/off."""
    saved = orx._FAST
    orx._FAST = saved if native else None
    try:
        rar = orx.RandomAccessReader(path, wanted_layers=wanted)
        return rar.load_cell(0)
    finally:
        orx._FAST = saved


def _assert_identical(a, b):
    """``a`` (python path) and ``b`` (native path) describe the same cell."""
    assert a.bbox == b.bbox
    assert a.total_rects() == b.total_rects()
    assert a.total_polys() == b.total_polys()
    assert a.placement_count() == b.placement_count()
    assert set(a.rect_keys()) == set(b.rect_keys())
    assert set(a.poly_keys()) == set(b.poly_keys())
    for key in a.rect_keys():
        ra = a.rects(key)
        rb = b.rects(key)
        assert np.array_equal(ra, rb), (key, ra, rb)
        # per-spec repetition descriptors must match in stream order
        assert a.rect_count(key) == b.rect_count(key)
        for i in range(a.rect_count(key)):
            sa = a.rect_spec_at(key, i)
            sb = b.rect_spec_at(key, i)
            assert sa[:4] == sb[:4], (key, i, sa, sb)
            assert sa[4] == sb[4], (key, i, sa, sb)   # rtype
            assert sa[5] == sb[5], (key, i, sa, sb)   # raw
    for key in a.poly_keys():
        pa = a.polys(key)
        pb = b.polys(key)
        assert len(pa) == len(pb)
        for u, v in zip(pa, pb):
            assert np.array_equal(u, v)
    for i in range(a.placement_count()):
        assert a.placement_at(i) == b.placement_at(i)


def _check(body, wanted={(17, 0)}, *, tmp_path):
    p = tmp_path / "cell.oas"
    p.write_bytes(_one_cell_file(body))
    py = _load(p, wanted, native=False)
    nat = _load(p, wanted, native=True)
    _assert_identical(py, nat)
    return py, nat


# ── tests ────────────────────────────────────────────────────────────────────
def test_native_path_actually_engaged():
    # Guard: the extension must be present and >= the rewind-invariant version,
    # otherwise these "native" loads silently fall back to Python and prove
    # nothing.
    assert orx._FAST is not None
    assert orx._FAST.VERSION >= 3


def test_single_run_modal_reuse(tmp_path):
    body = bytearray(_rect_full(17, 0, 10, 10, 0, 0))
    for i in range(1, 400):
        body += _rect_xy((i * 13) % 5000, (i * 7) % 9000)
    py, nat = _check(bytes(body), tmp_path=tmp_path)
    assert nat.total_rects() == 400
    assert nat._rcol and not nat.rect_specs        # native => columnar backing


def test_xyrelative_within_run(tmp_path):
    body = bytearray(_rect_full(17, 0, 4, 4, 100, 100))
    body += bytes([oas.XYRELATIVE])
    for _ in range(60):
        body += _rect_xy(5, 7)
    body += bytes([oas.XYABSOLUTE])
    body += _rect_xy(2000, 3000)
    _check(bytes(body), tmp_path=tmp_path)


def test_multi_layer_runs(tmp_path):
    body = bytearray()
    for i in range(20):
        body += (_rect_full(17, 0, 10, 10, i, i) if i == 0 else _rect_xy(i, i))
    for i in range(15):
        body += (_rect_full(20, 0, 6, 6, 500, 500) if i == 0
                 else _rect_xy(500 + i, 500 + i))
    for i in range(8):                       # back to layer 17 (new run)
        body += (_rect_full(17, 0, 3, 3, 900, 900) if i == 0
                 else _rect_xy(900 + i, 900 + i))
    _check(bytes(body), wanted={(17, 0), (20, 0)}, tmp_path=tmp_path)


def test_repetition_rect_interleaved(tmp_path):
    body = bytearray(_rect_full(17, 0, 10, 10, 0, 0))
    body += _rect_xy(50, 50)
    body += _rect_rep_xy(100, 100, 4, 25)    # a repeated rect mid-run
    body += _rect_xy(300, 300)               # run resumes after it
    body += _rect_xy(350, 350)
    py, nat = _check(bytes(body), tmp_path=tmp_path)
    # the repetition descriptor survived on exactly one spec
    rtypes = {nat.rect_spec_at((17, 0), i)[4]
              for i in range(nat.rect_count((17, 0)))}
    assert 2 in rtypes


def test_layer_filter_drops_unwanted_runs(tmp_path):
    body = bytearray()
    for i in range(12):
        body += (_rect_full(17, 0, 10, 10, i, i) if i == 0 else _rect_xy(i, i))
    for i in range(30):                      # layer 99 — filtered out
        body += (_rect_full(99, 0, 6, 6, i, i) if i == 0 else _rect_xy(i, i))
    for i in range(5):
        body += (_rect_full(17, 0, 3, 3, 800, 0) if i == 0
                 else _rect_xy(800 + i, 0))
    py, nat = _check(bytes(body), wanted={(17, 0)}, tmp_path=tmp_path)
    assert (99, 0) not in set(nat.rect_keys())
    assert nat.total_rects() == 17


def test_cblock_wrapped_run(tmp_path):
    inner = bytearray(_rect_full(17, 0, 10, 10, 0, 0))
    for i in range(1, 250):
        inner += _rect_xy((i * 11) % 4000, (i * 5) % 7000)
    body = _cblock(bytes(inner))
    py, nat = _check(body, tmp_path=tmp_path)
    assert nat.total_rects() == 250


def test_two_cblocks_then_rects(tmp_path):
    inner1 = bytearray(_rect_full(17, 0, 10, 10, 0, 0))
    for i in range(1, 100):
        inner1 += _rect_xy(i, i)
    inner2 = bytearray(_rect_full(20, 0, 5, 5, 0, 0))
    for i in range(1, 80):
        inner2 += _rect_xy(1000 + i, 1000 + i)
    body = _cblock(bytes(inner1)) + _cblock(bytes(inner2)) \
        + _rect_full(17, 0, 2, 2, 9000, 9000)
    _check(body, wanted={(17, 0), (20, 0)}, tmp_path=tmp_path)


def test_rects_with_placement_splitting_runs(tmp_path):
    body = bytearray(_rect_full(17, 0, 10, 10, 0, 0))
    for i in range(1, 50):
        body += _rect_xy(i * 4, i * 3)
    body += bytes([oas.PLACEMENT_NOMAG, 0xC0]) + _uint(1)   # placement ref 1
    body += _rect_full(17, 0, 8, 8, 7000, 7000)            # run resumes
    body += _rect_xy(7100, 7100)
    py, nat = _check(bytes(body), tmp_path=tmp_path)
    assert nat.placement_count() == 1
    assert nat.placement_at(0).target == 1


def test_empty_cell(tmp_path):
    body = bytes([oas.PLACEMENT_NOMAG, 0xC0]) + _uint(1)   # only a placement
    py, nat = _check(body, tmp_path=tmp_path)
    assert nat.total_rects() == 0
    assert nat.bbox is None
    assert nat.placement_count() == 1
