"""F27 M1: the native walk helpers (transform_rects_d4 / roi_overlap_mask) must
be byte-for-byte identical to the pure-numpy path, and a full walk_roi with the
native helpers on must match the walk with them off.

Skipped when the extension isn't built (VERSION < 5).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

fast = pytest.importorskip("oasis_fastdecode")
if getattr(fast, "VERSION", 0) < 5:
    pytest.skip("native walk helpers need VERSION >= 5", allow_module_level=True)

sys.path.insert(0, str(_ROOT / "tests"))
import oasis_walker as owk        # noqa: E402
import oasis_random as orx        # noqa: E402
from oasis_walker import Transform  # noqa: E402
import test_oasis_random as T      # noqa: E402


# All D4 transforms the walk can build: 0/90/180/270 × flip × a couple mags.
def _all_d4():
    out = []
    for ang in (0, 90, 180, 270):
        for flip in (False, True):
            for mag in (1.0, 2.0, 0.5):
                tf = Transform.from_placement(37, -19, ang, flip, mag)
                assert tf is not None
                out.append(tf)
    return out


def _rng_rects(seed):
    rng = np.random.RandomState(seed)
    r = rng.randint(-5000, 5000, size=(200, 4)).astype(np.float64)
    # mix normalized and un-normalized corners (x1>x2 etc.) — the mask + bbox
    # must handle both.
    return r


def test_transform_rects_d4_matches_numpy():
    for tf in _all_d4():
        rects = _rng_rects(hash((tf.M.tobytes(), tf.t.tobytes())) & 0xFFFF)
        m = tf.M
        native = fast.transform_rects_d4(
            np.ascontiguousarray(rects), m[0, 0], m[0, 1], m[1, 0], m[1, 1],
            tf.t[0], tf.t[1])
        # pure numpy 2-corner reference (force the fallback)
        saved = owk._FASTW
        owk._FASTW = None
        try:
            ref = tf.apply_to_rects(rects)
        finally:
            owk._FASTW = saved
        assert np.array_equal(native, ref), tf.M


def test_roi_overlap_mask_matches_numpy():
    for seed in range(6):
        boxes = _rng_rects(seed)
        roi = (-1000.0, -500.0, 1500.0, 2000.0)
        native = fast.roi_overlap_mask(np.ascontiguousarray(boxes), *roi)
        saved = orx._FASTW
        orx._FASTW = None
        try:
            ref = orx._roi_overlap_mask(boxes, roi)
        finally:
            orx._FASTW = saved
        assert native.dtype == np.bool_
        assert np.array_equal(native, ref), seed


def _walk_both(rar_path, wanted, roi, layer, dt):
    """walk_roi with native helpers ON then OFF; return both results."""
    def run(native):
        w_saved, r_saved = owk._FASTW, orx._FASTW
        owk._FASTW = (fast if native else None)
        orx._FASTW = (fast if native else None)
        try:
            rar = orx.RandomAccessReader(rar_path, wanted_layers=wanted)
            return orx.walk_roi(rar, 0, roi, layer, dt)
        finally:
            owk._FASTW, orx._FASTW = w_saved, r_saved
    return run(True), run(False)


def test_walk_roi_native_matches_pure(tmp_path):
    # A wide-repeat tree (one leaf cell placed many times) — the exact shape the
    # walk helpers are hot on.
    places = [(x * 40, y * 40) for y in range(30) for x in range(40)]  # 1200
    p = tmp_path / "w.oas"
    p.write_bytes(T._build_hierarchy(places))
    roi = (-50, -50, 40 * 40 + 50, 30 * 40 + 50)
    on, off = _walk_both(p, {(17, 0)}, roi, 17, 0)
    assert np.array_equal(on["rects"], off["rects"])
    assert on["stats"].rects_emitted == off["stats"].rects_emitted
    assert len(on["polys"]) == len(off["polys"])
    for a, b in zip(on["polys"], off["polys"]):
        assert np.array_equal(a, b)


def test_walk_roi_native_matches_pure_partial_roi(tmp_path):
    # A tight ROI so pruning is exercised (most instances clipped away).
    places = [(x * 40, y * 40) for y in range(30) for x in range(40)]
    p = tmp_path / "w2.oas"
    p.write_bytes(T._build_hierarchy(places))
    roi = (300, 300, 460, 460)
    on, off = _walk_both(p, {(17, 0)}, roi, 17, 0)
    assert np.array_equal(on["rects"], off["rects"])


# ── M3: full native subtree walk (flatten + walk_rects_native, gated on
#        walk_roi) must match the pure-Python walk (rect SET; order differs) ────
if getattr(fast, "VERSION", 0) < 6:
    pytest.skip("native subtree walk needs VERSION >= 6",
                allow_module_level=True)

import oasis_streamer as oas  # noqa: E402


def _walk_m3(path, wanted, roi, layer, dt, native):
    saved = orx._FASTWALK
    orx._FASTWALK = (fast if native else None)
    try:
        rar = orx.RandomAccessReader(path, wanted_layers=wanted)
        return orx.walk_roi_fast(rar, 0, roi, layer, dt)
    finally:
        orx._FASTWALK = saved


def _sorted_rows(a):
    return sorted(map(tuple, a.tolist()))


@pytest.mark.parametrize("roi", [
    (-50, -50, 40 * 40 + 50, 30 * 40 + 50),   # all
    (300, 300, 620, 620),                      # tight (pruning)
    (10_000_000, 10_000_000, 10_000_100, 10_000_100),  # empty
])
def test_walk_roi_m3_native_matches_python(tmp_path, roi):
    places = [(x * 40, y * 40) for y in range(30) for x in range(40)]
    p = tmp_path / "m3.oas"
    p.write_bytes(T._build_hierarchy(places))
    on = _walk_m3(p, {(17, 0)}, roi, 17, 0, True)
    off = _walk_m3(p, {(17, 0)}, roi, 17, 0, False)
    assert on["stats"].rects_emitted == off["stats"].rects_emitted
    assert _sorted_rows(on["rects"]) == _sorted_rows(off["rects"])
    assert on["polys"] == [] and off["polys"] == []


def test_flatten_native_able_on_plain_rect(tmp_path):
    places = [(x * 40, y * 40) for y in range(5) for x in range(5)]
    p = tmp_path / "ok.oas"
    p.write_bytes(T._build_hierarchy(places))
    rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
    assert orx.flatten_cell_graph(rar, 0, 17, 0) is not None


def test_prewarm_persists_and_enables_native_over_cap(tmp_path, monkeypatch):
    # M3c: a graph over the interactive cap stays Python UNTIL a prewarm builds
    # + persists the whole-chip sidecar; a fresh reader then loads it and goes
    # native, byte-identical.
    monkeypatch.setenv("GLAS_CELLCACHE_DIR", str(tmp_path / "wf"))
    places = [(x * 40, y * 40) for y in range(6) for x in range(6)]
    p = tmp_path / "pw.oas"
    p.write_bytes(T._build_hierarchy(places))
    roi = (-50, -50, 40 * 6 + 50, 40 * 6 + 50)
    monkeypatch.setattr(orx, "_NATIVE_WALK_MAX_CELLS", 1)   # simulate a big chip
    saved = orx._FASTWALK
    orx._FASTWALK = fast
    try:
        r0 = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        assert orx._flatten_cached(r0, 0, 17, 0) is None    # over-cap -> Python
        flat = orx.flatten_prewarm(r0, 0, 17, 0)            # ignores cap, persists
        assert flat is not None
        r1 = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
        assert orx._flatten_cached(r1, 0, 17, 0) is not None   # sidecar hit
        on = orx.walk_roi_fast(r1, 0, roi, 17, 0)
        orx._FASTWALK = None
        off = orx.walk_roi_fast(orx.RandomAccessReader(
            p, wanted_layers={(17, 0)}), 0, roi, 17, 0)
        assert _sorted_rows(on["rects"]) == _sorted_rows(off["rects"])
    finally:
        orx._FASTWALK = saved


def test_flatten_cap_falls_back_without_stall(tmp_path, monkeypatch):
    # A graph over the cell cap must return None *without decoding* (the
    # regression: a 13k-cell chip flattened the whole graph and stalled).
    places = [(x * 40, y * 40) for y in range(5) for x in range(5)]
    p = tmp_path / "cap.oas"
    p.write_bytes(T._build_hierarchy(places))
    rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
    monkeypatch.setattr(orx, "_NATIVE_WALK_MAX_CELLS", 1)
    assert orx.flatten_cell_graph(rar, 0, 17, 0) is None
    assert rar._n_loaded == 0        # bailed before touching any geometry
    # walk_roi_fast still returns the correct rects (via Python fallback)
    roi = (-50, -50, 40 * 5 + 50, 40 * 5 + 50)
    on = _walk_m3(p, {(17, 0)}, roi, 17, 0, True)
    off = _walk_m3(p, {(17, 0)}, roi, 17, 0, False)
    assert _sorted_rows(on["rects"]) == _sorted_rows(off["rects"])


def test_flatten_native_able_with_rect_repetition(tmp_path):
    # M3d: a rect with a type-2 repetition is native-able — it expands (via
    # c.rects) into plain rects the kernel handles; native must equal Python.
    start = (bytes([oas.START]) + T._astr("1.0") + bytes([0])
             + T._uint(1000) + T._uint(0) + bytes([0] * 12))
    pn = bytes([oas.PROPNAME_IMP]) + T._astr("S_CELL_OFFSET")
    cn = bytes([oas.CELLNAME_IMP]) + T._astr("R")

    def prop(off):
        return (bytes([oas.PROPERTY_NORMAL, 0x16]) + T._uint(0)
                + T._uint(8) + T._ufix(off, 4))

    rect = (bytes([oas.RECTANGLE, 0x7f]) + T._uint(17) + T._uint(0)
            + T._uint(10) + T._uint(10) + T._sint(0) + T._sint(0)
            + bytes([2]) + T._uint(1) + T._uint(100))   # type-2 rep: 3 @ pitch 100
    cell = bytes([oas.CELL_REFNUM]) + T._uint(0)
    end = bytes([oas.END]) + T._uint(0)
    hdr = oas.MAGIC + start + pn + cn + prop(0)
    off = len(hdr)
    p = tmp_path / "rep.oas"
    p.write_bytes(oas.MAGIC + start + pn + cn + prop(off) + cell + rect + end)
    rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
    assert orx.flatten_cell_graph(rar, 0, 17, 0) is not None   # rep expands -> native
    roi = (-100, -100, 1000, 1000)
    on = _walk_m3(p, {(17, 0)}, roi, 17, 0, True)
    off = _walk_m3(p, {(17, 0)}, roi, 17, 0, False)
    assert _sorted_rows(on["rects"]) == _sorted_rows(off["rects"])
    assert on["rects"].shape[0] == 3   # the 3-instance repetition


def test_flatten_over_expanded_rect_cap_falls_back(tmp_path, monkeypatch):
    # A huge repetition over the expanded-rect cap must bail (Python), not OOM.
    start = (bytes([oas.START]) + T._astr("1.0") + bytes([0])
             + T._uint(1000) + T._uint(0) + bytes([0] * 12))
    pn = bytes([oas.PROPNAME_IMP]) + T._astr("S_CELL_OFFSET")
    cn = bytes([oas.CELLNAME_IMP]) + T._astr("R")

    def prop(off):
        return (bytes([oas.PROPERTY_NORMAL, 0x16]) + T._uint(0)
                + T._uint(8) + T._ufix(off, 4))

    rect = (bytes([oas.RECTANGLE, 0x7f]) + T._uint(17) + T._uint(0)
            + T._uint(10) + T._uint(10) + T._sint(0) + T._sint(0)
            + bytes([2]) + T._uint(998) + T._uint(100))  # 1000 instances
    cell = bytes([oas.CELL_REFNUM]) + T._uint(0)
    end = bytes([oas.END]) + T._uint(0)
    hdr = oas.MAGIC + start + pn + cn + prop(0)
    off = len(hdr)
    p = tmp_path / "bigrep.oas"
    p.write_bytes(oas.MAGIC + start + pn + cn + prop(off) + cell + rect + end)
    rar = orx.RandomAccessReader(p, wanted_layers={(17, 0)})
    monkeypatch.setattr(orx, "_NATIVE_WALK_MAX_RECTS", 100)
    assert orx.flatten_cell_graph(rar, 0, 17, 0) is None
    assert "expanded rects" in (rar._flatten_reject or "")
