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
