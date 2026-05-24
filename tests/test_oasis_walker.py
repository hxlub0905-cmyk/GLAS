"""Tests for tools/oasis_walker.py (F2 M1.11c).

Two layers of coverage:

1. ``Transform`` math in isolation: identity, the eight D4 elements
   (4 rotations x {identity, flip}), composition associativity, mag,
   and rectangle-bbox correctness under each.
2. End-to-end via ``CellGraphWalker``: drive ``OasisGeometryStore`` with
   tmp_path-built OASIS files and confirm the rectangles come back in
   the expected root-cell coordinates after placement expansion. This
   includes repetition arrays and the warn-on-arbitrary-angle path.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import oasis_streamer as oas  # noqa: E402
import oasis_store as store_mod  # noqa: E402
import oasis_walker as walker_mod  # noqa: E402


# ── Byte-fixture helpers (shared with test_oasis_store.py) ───────────────────


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
    return (
        oas.MAGIC
        + bytes([oas.START])
        + _make_uint(3) + b"1.0"
        + bytes([0]) + _make_uint(1)
        + _make_uint(0)
        + bytes([0] * 12)
    )


def _file_footer() -> bytes:
    return bytes([oas.END]) + _make_uint(0)


def _cellname(refnum: int, name: bytes) -> bytes:
    return (bytes([oas.CELLNAME_EXP])
            + _make_uint(len(name)) + name
            + _make_uint(refnum))


def _cell_header(refnum: int) -> bytes:
    return bytes([oas.CELL_REFNUM]) + _make_uint(refnum)


def _rect(layer: int, datatype: int,
          w: int, h: int, x: int, y: int) -> bytes:
    info = 0x7b   # W H X Y D L
    return (bytes([oas.RECTANGLE, info])
            + _make_uint(layer) + _make_uint(datatype)
            + _make_uint(w) + _make_uint(h)
            + _make_uint(_signed(x)) + _make_uint(_signed(y)))


def _placement(target_refnum: int, x: int, y: int,
               *, angle_quarter: int = 0, flip: bool = False) -> bytes:
    """PLACEMENT (no mag), C=1 N=1 (refnum), X=1 Y=1, R=0, AA=angle_quarter, F=flip.

    info bit layout (high to low): C N X Y R A A F
        C = 0x80, N = 0x40, X = 0x20, Y = 0x10, R = 0x08, F = 0x01
    Earlier draft of this helper had X and Y at the wrong bit
    positions, which made the streamer read R=1 and try to decode a
    repetition; ``unknown repetition type 40`` was the symptom.
    """
    info = 0x80 | 0x40 | 0x20 | 0x10   # C N X Y
    info |= (angle_quarter & 0x03) << 1
    if flip:
        info |= 0x01
    return (bytes([oas.PLACEMENT_NOMAG, info])
            + _make_uint(target_refnum)
            + _make_uint(_signed(x)) + _make_uint(_signed(y)))


def _signed(v: int) -> int:
    """Encode a Python int as the unsigned-int OASIS uses for signed-int."""
    if v >= 0:
        return v << 1
    return ((-v) << 1) | 1


# ── Transform math ───────────────────────────────────────────────────────────


class TestTransformBasics:
    def test_identity_leaves_rects_alone(self):
        t = walker_mod.Transform.identity()
        rects = np.array([[1, 2, 3, 4], [10, 20, 30, 40]], dtype=np.int32)
        out = t.apply_to_rects(rects)
        np.testing.assert_array_equal(out, rects)

    def test_translation_only(self):
        t = walker_mod.Transform.from_placement(
            x=100, y=200, angle_deg=0, flip=False, mag=1.0)
        rects = np.array([[1, 2, 3, 4]], dtype=np.int32)
        out = t.apply_to_rects(rects)
        np.testing.assert_array_equal(out, np.array([[101, 202, 103, 204]]))

    def test_90deg_rotation(self):
        t = walker_mod.Transform.from_placement(
            x=0, y=0, angle_deg=90, flip=False, mag=1.0)
        # (1,2) -> (-2,1); (3,4) -> (-4,3). For bbox (1,2,3,4):
        # corners (1,2)(3,2)(3,4)(1,4) -> (-2,1)(-2,3)(-4,3)(-4,1)
        # new bbox = (-4, 1, -2, 3)
        rects = np.array([[1, 2, 3, 4]], dtype=np.int32)
        out = t.apply_to_rects(rects)
        np.testing.assert_array_equal(out, np.array([[-4, 1, -2, 3]]))

    def test_180deg_rotation(self):
        t = walker_mod.Transform.from_placement(
            x=0, y=0, angle_deg=180, flip=False, mag=1.0)
        # bbox (1,2,3,4) -> (-3,-4,-1,-2)
        rects = np.array([[1, 2, 3, 4]], dtype=np.int32)
        out = t.apply_to_rects(rects)
        np.testing.assert_array_equal(out, np.array([[-3, -4, -1, -2]]))

    def test_270deg_rotation(self):
        t = walker_mod.Transform.from_placement(
            x=0, y=0, angle_deg=270, flip=False, mag=1.0)
        # corners (1,2)(3,2)(3,4)(1,4) under (x,y) -> (y,-x):
        # (2,-1)(2,-3)(4,-3)(4,-1). New bbox = (2,-3,4,-1).
        rects = np.array([[1, 2, 3, 4]], dtype=np.int32)
        out = t.apply_to_rects(rects)
        np.testing.assert_array_equal(out, np.array([[2, -3, 4, -1]]))

    def test_flip_about_x_axis(self):
        t = walker_mod.Transform.from_placement(
            x=0, y=0, angle_deg=0, flip=True, mag=1.0)
        # Flip-x: y -> -y. (1,2,3,4) -> (1,-4,3,-2).
        rects = np.array([[1, 2, 3, 4]], dtype=np.int32)
        out = t.apply_to_rects(rects)
        np.testing.assert_array_equal(out, np.array([[1, -4, 3, -2]]))

    def test_rotation_with_translation(self):
        # Rotate 90deg around origin, then translate to (100, 200).
        t = walker_mod.Transform.from_placement(
            x=100, y=200, angle_deg=90, flip=False, mag=1.0)
        rects = np.array([[1, 2, 3, 4]], dtype=np.int32)
        out = t.apply_to_rects(rects)
        # From the 90deg test: (-4, 1, -2, 3). Add (100, 200, 100, 200):
        np.testing.assert_array_equal(out, np.array([[96, 201, 98, 203]]))

    def test_magnification(self):
        # mag=2 doubles every coordinate (no translation, no rotation).
        t = walker_mod.Transform.from_placement(
            x=0, y=0, angle_deg=0, flip=False, mag=2.0)
        rects = np.array([[1, 2, 3, 4]], dtype=np.int32)
        out = t.apply_to_rects(rects)
        np.testing.assert_array_equal(out, np.array([[2, 4, 6, 8]]))

    def test_arbitrary_angle_returns_none(self):
        assert walker_mod.Transform.from_placement(
            x=0, y=0, angle_deg=45, flip=False, mag=1.0) is None
        assert walker_mod.Transform.from_placement(
            x=0, y=0, angle_deg=30, flip=False, mag=1.0) is None

    def test_tolerance_accepts_near_quarter_turn(self):
        # 90.005 deg is well within the 0.01 deg tolerance.
        t = walker_mod.Transform.from_placement(
            x=0, y=0, angle_deg=90.005, flip=False, mag=1.0)
        assert t is not None


class TestTransformCompose:
    def test_compose_translations(self):
        a = walker_mod.Transform.from_placement(10, 20, 0, False, 1.0)
        b = walker_mod.Transform.from_placement(5, 6, 0, False, 1.0)
        # a(b(v)) = a(v + (5,6)) = v + (5,6) + (10,20) = v + (15, 26)
        composed = a.compose(b)
        rects = np.array([[0, 0, 1, 1]], dtype=np.int32)
        out = composed.apply_to_rects(rects)
        np.testing.assert_array_equal(out, np.array([[15, 26, 16, 27]]))

    def test_compose_rotation_then_translation(self):
        # child: rotate 90deg around origin
        # parent: translate by (100, 200)
        # composed: rotate first then translate = "rotate child around (100,200)"
        child = walker_mod.Transform.from_placement(0, 0, 90, False, 1.0)
        parent = walker_mod.Transform.from_placement(100, 200, 0, False, 1.0)
        composed = parent.compose(child)
        rects = np.array([[1, 2, 3, 4]], dtype=np.int32)
        # Apply child first: (1,2,3,4) -> (-4,1,-2,3) (see TestTransformBasics)
        # Apply parent: + (100, 200, 100, 200) = (96, 201, 98, 203)
        out = composed.apply_to_rects(rects)
        np.testing.assert_array_equal(out, np.array([[96, 201, 98, 203]]))

    def test_compose_two_rotations(self):
        # 90deg then 90deg = 180deg
        a = walker_mod.Transform.from_placement(0, 0, 90, False, 1.0)
        b = walker_mod.Transform.from_placement(0, 0, 90, False, 1.0)
        composed = a.compose(b)
        rects = np.array([[1, 2, 3, 4]], dtype=np.int32)
        out = composed.apply_to_rects(rects)
        # 180deg result: (-3, -4, -1, -2)
        np.testing.assert_array_equal(out, np.array([[-3, -4, -1, -2]]))


# ── End-to-end walker tests via tmp_path OASIS files ─────────────────────────


class TestWalker:
    """Build small OASIS files and verify the walker produces the
    expected rectangles in the root cell's coordinate frame."""

    def _build(self, body: bytes) -> bytes:
        return _file_header() + body + _file_footer()

    def test_root_with_no_placements(self, tmp_path: Path):
        # Cell 0: 2 rectangles directly. No descendants.
        body = (
            _cellname(0, b"ROOT")
            + _cell_header(0)
            + _rect(layer=1, datatype=0, w=10, h=20, x=0, y=0)
            + _rect(layer=1, datatype=0, w=5, h=5, x=100, y=100)
        )
        path = tmp_path / "simple.oas"
        path.write_bytes(self._build(body))

        store = store_mod.OasisGeometryStore(path)
        store.run()
        walker = walker_mod.CellGraphWalker(store)
        rects = walker.walk_to_root(root=0, layer=1, datatype=0)
        # Two rectangles, in root coords (which == cell-local since no placement).
        np.testing.assert_array_equal(
            rects, np.array([[0, 0, 10, 20], [100, 100, 105, 105]],
                            dtype=np.int32))
        assert walker.stats.rectangles_emitted == 2
        assert walker.stats.placements_expanded == 0

    def test_single_placement_translation(self, tmp_path: Path):
        # Cell 1 has one rect at origin; cell 0 places cell 1 at (100, 200).
        body = (
            _cellname(0, b"ROOT")
            + _cellname(1, b"A")
            + _cell_header(1)
            + _rect(layer=1, datatype=0, w=10, h=20, x=0, y=0)
            + _cell_header(0)
            + _placement(target_refnum=1, x=100, y=200)
        )
        path = tmp_path / "nested.oas"
        path.write_bytes(self._build(body))

        store = store_mod.OasisGeometryStore(path)
        store.run()
        walker = walker_mod.CellGraphWalker(store)
        rects = walker.walk_to_root(root=0, layer=1, datatype=0)
        # The rect at cell-local (0,0)-(10,20) moves to (100,200)-(110,220).
        np.testing.assert_array_equal(
            rects, np.array([[100, 200, 110, 220]], dtype=np.int32))
        assert walker.stats.placements_expanded == 1

    def test_placement_with_90deg_rotation(self, tmp_path: Path):
        # Cell 1: rect (1,2)-(3,4).
        # Cell 0: places cell 1 at (100, 200) rotated 90deg.
        body = (
            _cellname(0, b"ROOT")
            + _cellname(1, b"A")
            + _cell_header(1)
            + _rect(layer=1, datatype=0, w=2, h=2, x=1, y=2)   # corners 1..3, 2..4
            + _cell_header(0)
            + _placement(target_refnum=1, x=100, y=200, angle_quarter=1)
        )
        path = tmp_path / "rot90.oas"
        path.write_bytes(self._build(body))

        store = store_mod.OasisGeometryStore(path)
        store.run()
        walker = walker_mod.CellGraphWalker(store)
        rects = walker.walk_to_root(root=0, layer=1, datatype=0)
        # cell-local (1,2,3,4) under 90deg rot then +(100,200):
        # 90deg bbox = (-4, 1, -2, 3) (from Transform tests).
        # + (100, 200) = (96, 201, 98, 203)
        np.testing.assert_array_equal(
            rects, np.array([[96, 201, 98, 203]], dtype=np.int32))

    def test_two_level_hierarchy(self, tmp_path: Path):
        # Cell 2: leaf with 1 rect at origin.
        # Cell 1: places cell 2 at (10, 20).
        # Cell 0 (root): places cell 1 at (100, 200).
        # Expected root coord = (10,20) + (100,200) = (110, 220).
        body = (
            _cellname(0, b"ROOT")
            + _cellname(1, b"MID")
            + _cellname(2, b"LEAF")
            + _cell_header(2)
            + _rect(layer=1, datatype=0, w=5, h=5, x=0, y=0)
            + _cell_header(1)
            + _placement(target_refnum=2, x=10, y=20)
            + _cell_header(0)
            + _placement(target_refnum=1, x=100, y=200)
        )
        path = tmp_path / "two_level.oas"
        path.write_bytes(self._build(body))

        store = store_mod.OasisGeometryStore(path)
        store.run()
        walker = walker_mod.CellGraphWalker(store)
        rects = walker.walk_to_root(root=0, layer=1, datatype=0)
        np.testing.assert_array_equal(
            rects, np.array([[110, 220, 115, 225]], dtype=np.int32))
        assert walker.stats.cells_visited == 3
        assert walker.stats.placements_expanded == 2

    def test_root_by_name(self, tmp_path: Path):
        body = (
            _cellname(0, b"ROOT")
            + _cell_header(0)
            + _rect(layer=1, datatype=0, w=10, h=20, x=0, y=0)
        )
        path = tmp_path / "by_name.oas"
        path.write_bytes(self._build(body))

        store = store_mod.OasisGeometryStore(path)
        store.run()
        walker = walker_mod.CellGraphWalker(store)
        rects = walker.walk_to_root(root="ROOT", layer=1, datatype=0)
        assert rects.shape == (1, 4)

    def test_unknown_root_raises(self, tmp_path: Path):
        body = (
            _cellname(0, b"ROOT")
            + _cell_header(0)
            + _rect(layer=1, datatype=0, w=10, h=20, x=0, y=0)
        )
        path = tmp_path / "x.oas"
        path.write_bytes(self._build(body))
        store = store_mod.OasisGeometryStore(path)
        store.run()
        walker = walker_mod.CellGraphWalker(store)
        with pytest.raises(KeyError):
            walker.walk_to_root(root="NOPE", layer=1, datatype=0)

    def test_layer_mismatch_returns_empty(self, tmp_path: Path):
        body = (
            _cellname(0, b"ROOT")
            + _cell_header(0)
            + _rect(layer=1, datatype=0, w=10, h=20, x=0, y=0)
        )
        path = tmp_path / "x.oas"
        path.write_bytes(self._build(body))
        store = store_mod.OasisGeometryStore(path)
        store.run()
        walker = walker_mod.CellGraphWalker(store)
        rects = walker.walk_to_root(root=0, layer=99, datatype=0)
        assert rects.shape == (0, 4)


# ── Warn-on-arbitrary-angle path ─────────────────────────────────────────────


class TestArbitraryAngleSkip:
    def test_45deg_placement_warns_and_skips(self, tmp_path: Path):
        # We can't easily emit a 45deg PLACEMENT through the byte
        # encoder (PLACEMENT-no-mag only encodes quarter-turns via
        # AA bits), so we monkeypatch the store after run() to insert
        # a 45deg placement directly into the placements_for() result.
        body = (
            _cellname(0, b"ROOT")
            + _cellname(1, b"A")
            + _cell_header(1)
            + _rect(layer=1, datatype=0, w=10, h=10, x=0, y=0)
            + _cell_header(0)   # ROOT has no placements yet
        )
        path = tmp_path / "x.oas"
        path.write_bytes(_file_header() + body + _file_footer())

        store = store_mod.OasisGeometryStore(path)
        store.run()
        # Inject a non-quarter-turn placement targeting cell 1 from cell 0.
        store._placements.setdefault(0, []).append(
            store_mod.Placement(
                target=1, target_kind="refnum",
                x=0, y=0, angle=45.0, magnification=1.0, flip=False,
                repetition_type=None, repetition_offsets=[]))

        walker = walker_mod.CellGraphWalker(store)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            rects = walker.walk_to_root(root=0, layer=1, datatype=0)
        # Skipped placement means cell-1's rect never reaches root output.
        assert rects.shape == (0, 4)
        assert walker.stats.arbitrary_angle_skipped == 1
        assert any("non-quarter-turn" in str(w.message) for w in caught)


# ── Repetition expansion (verified via monkeypatch since the byte encoder
# only emits placements without repetition_offsets) ──────────────────────────


class TestRepetitionExpansion:
    """Direct verification that the walker fans a single placement into
    N independent root-coord instances, one per repetition_offsets
    entry. Crucial because production OASIS often packs large cell
    arrays into a single PLACEMENT with thousands of offsets."""

    def test_each_offset_emits_independent_rect(self, tmp_path: Path):
        body = (
            _cellname(0, b"ROOT")
            + _cellname(1, b"LEAF")
            + _cell_header(1)
            + _rect(layer=1, datatype=0, w=10, h=10, x=0, y=0)
            + _cell_header(0)   # ROOT empty for now; we'll inject below.
        )
        path = tmp_path / "rep.oas"
        path.write_bytes(_file_header() + body + _file_footer())

        store = store_mod.OasisGeometryStore(path)
        store.run()
        # Inject a placement of LEAF with 3 offsets simulating a row
        # of 3 instances starting at (100, 0) with pitch 50.
        store._placements.setdefault(0, []).append(
            store_mod.Placement(
                target=1, target_kind="refnum",
                x=100, y=0,
                angle=0.0, magnification=1.0, flip=False,
                repetition_type=2,
                repetition_offsets=[(0, 0), (50, 0), (100, 0)],
            ))

        walker = walker_mod.CellGraphWalker(store)
        rects = walker.walk_to_root(root=0, layer=1, datatype=0)
        # Each offset adds to the placement's (100, 0) anchor:
        # (100, 0) -> rect (100, 0, 110, 10)
        # (150, 0) -> rect (150, 0, 160, 10)
        # (200, 0) -> rect (200, 0, 210, 10)
        assert rects.shape == (3, 4)
        np.testing.assert_array_equal(rects, np.array([
            [100, 0, 110, 10],
            [150, 0, 160, 10],
            [200, 0, 210, 10],
        ], dtype=np.int32))
        assert walker.stats.repetition_instances == 3
        assert walker.stats.placements_expanded == 3
        assert walker.stats.rectangles_emitted == 3

    def test_repetition_combines_with_rotation(self, tmp_path: Path):
        """Same fixture but with the placement rotated 90 deg -- each
        rep_offset still expands independently but each instance also
        gets the rotation applied. Confirms compose order."""
        body = (
            _cellname(0, b"ROOT")
            + _cellname(1, b"LEAF")
            + _cell_header(1)
            + _rect(layer=1, datatype=0, w=10, h=10, x=0, y=0)
            + _cell_header(0)
        )
        path = tmp_path / "rep_rot.oas"
        path.write_bytes(_file_header() + body + _file_footer())
        store = store_mod.OasisGeometryStore(path)
        store.run()
        store._placements.setdefault(0, []).append(
            store_mod.Placement(
                target=1, target_kind="refnum",
                x=0, y=0,
                angle=180.0, magnification=1.0, flip=False,
                repetition_type=2,
                repetition_offsets=[(0, 0), (50, 50)],
            ))

        walker = walker_mod.CellGraphWalker(store)
        rects = walker.walk_to_root(root=0, layer=1, datatype=0)
        # 180 deg of LEAF's rect (0,0)-(10,10) -> bbox (-10, -10, 0, 0).
        # Offset 1: +(0, 0)   -> (-10, -10, 0, 0)
        # Offset 2: +(50, 50) -> (40, 40, 50, 50)
        np.testing.assert_array_equal(rects, np.array([
            [-10, -10, 0, 0],
            [40, 40, 50, 50],
        ], dtype=np.int32))


class TestVectorizeEquivalence:
    """M1.13.2 vectorization rewrote ``_expand_placement`` to batch the
    K-repetition fan-out. These tests lock the output to known-correct
    values across **both** the leaf fast path (target has no nested
    placements) and the slow path (target has child placements, so K
    recursive calls remain). Equivalence with the pre-vectorize M1.11c
    code is established here because the old code is no longer in tree
    -- TestRepetitionExpansion already covers the simple cases."""

    def _setup_leaf(self, tmp_path: Path, name: str = "leaf.oas"):
        """Two-cell store: ROOT -> LEAF, LEAF has one rect (0,0)-(10,10).
        Tests inject placements directly so they can dial K and
        offsets per case without re-writing OASIS bytes."""
        body = (
            _cellname(0, b"ROOT")
            + _cellname(1, b"LEAF")
            + _cell_header(1)
            + _rect(layer=1, datatype=0, w=10, h=10, x=0, y=0)
            + _cell_header(0)
        )
        path = tmp_path / name
        path.write_bytes(_file_header() + body + _file_footer())
        store = store_mod.OasisGeometryStore(path)
        store.run()
        return store

    def test_large_K_leaf_fast_path(self, tmp_path: Path):
        """K=500 reps on a leaf. Pre-vectorize this exercised the Python
        loop 500 times; post-vectorize it's one batch. Output values
        must match the analytical expected positions exactly."""
        store = self._setup_leaf(tmp_path, "K500.oas")
        offsets = [(i * 50, 0) for i in range(500)]
        store._placements.setdefault(0, []).append(
            store_mod.Placement(
                target=1, target_kind="refnum",
                x=100, y=0,
                angle=0.0, magnification=1.0, flip=False,
                repetition_type=2,
                repetition_offsets=offsets,
            ))
        walker = walker_mod.CellGraphWalker(store)
        rects = walker.walk_to_root(root=0, layer=1, datatype=0)
        assert rects.shape == (500, 4)
        # Each instance: rect (0,0,10,10) + (100 + i*50, 0)
        expected = np.empty((500, 4), dtype=np.int32)
        for i in range(500):
            x0 = 100 + i * 50
            expected[i] = [x0, 0, x0 + 10, 10]
        np.testing.assert_array_equal(rects, expected)
        assert walker.stats.repetition_instances == 500
        assert walker.stats.cells_visited == 500 + 1  # +1 for ROOT

    def test_slow_path_target_has_placements(self, tmp_path: Path):
        """Force the slow path: a placement whose target has its OWN
        placement (LEAF -> via MID -> from ROOT). Each of K reps must
        still produce the right MID's nested geometry."""
        # ROOT -> places MID at (100, 0) with 2 reps (0,0)/(200,0)
        # MID  -> places LEAF at (0, 0)
        # LEAF -> 1 rect (0,0)-(10,10)
        body = (
            _cellname(0, b"ROOT")
            + _cellname(1, b"MID")
            + _cellname(2, b"LEAF")
            + _cell_header(2)
            + _rect(layer=1, datatype=0, w=10, h=10, x=0, y=0)
            + _cell_header(1)
            # MID has no decoded placement in bytes -- we'll inject
            + _cell_header(0)
        )
        path = tmp_path / "slow.oas"
        path.write_bytes(_file_header() + body + _file_footer())
        store = store_mod.OasisGeometryStore(path)
        store.run()
        # MID -> LEAF, single instance at origin.
        store._placements.setdefault(1, []).append(
            store_mod.Placement(
                target=2, target_kind="refnum",
                x=0, y=0,
                angle=0.0, magnification=1.0, flip=False,
                repetition_type=0, repetition_offsets=[],
            ))
        # ROOT -> MID, 2 reps so we exercise the K loop in slow path.
        store._placements.setdefault(0, []).append(
            store_mod.Placement(
                target=1, target_kind="refnum",
                x=100, y=0,
                angle=0.0, magnification=1.0, flip=False,
                repetition_type=2,
                repetition_offsets=[(0, 0), (200, 0)],
            ))
        walker = walker_mod.CellGraphWalker(store)
        rects = walker.walk_to_root(root=0, layer=1, datatype=0)
        # Instance 1: MID at (100, 0)   -> LEAF rect -> (100, 0, 110, 10)
        # Instance 2: MID at (300, 0)   -> LEAF rect -> (300, 0, 310, 10)
        np.testing.assert_array_equal(rects, np.array([
            [100, 0, 110, 10],
            [300, 0, 310, 10],
        ], dtype=np.int32))
        # Stats: 2 ROOT->MID expansions + 2 MID->LEAF expansions
        assert walker.stats.placements_expanded == 4
        assert walker.stats.repetition_instances == 4

    def test_slow_path_with_rotation_matches_manual(self, tmp_path: Path):
        """ROOT -> places MID at angle 90, with 2 reps. MID -> LEAF.
        Verifies that the composed_M passed into the slow path actually
        gets used K times correctly (it's a single matrix shared across
        K, so a bug that drops the M would still pass test_slow_path
        above where angle=0)."""
        body = (
            _cellname(0, b"ROOT")
            + _cellname(1, b"MID")
            + _cellname(2, b"LEAF")
            + _cell_header(2)
            + _rect(layer=1, datatype=0, w=10, h=2, x=0, y=0)
            + _cell_header(1)
            + _cell_header(0)
        )
        path = tmp_path / "slow_rot.oas"
        path.write_bytes(_file_header() + body + _file_footer())
        store = store_mod.OasisGeometryStore(path)
        store.run()
        store._placements.setdefault(1, []).append(
            store_mod.Placement(
                target=2, target_kind="refnum",
                x=0, y=0,
                angle=0.0, magnification=1.0, flip=False,
                repetition_type=0, repetition_offsets=[],
            ))
        store._placements.setdefault(0, []).append(
            store_mod.Placement(
                target=1, target_kind="refnum",
                x=0, y=0,
                angle=90.0, magnification=1.0, flip=False,
                repetition_type=2,
                repetition_offsets=[(0, 0), (100, 0)],
            ))
        walker = walker_mod.CellGraphWalker(store)
        rects = walker.walk_to_root(root=0, layer=1, datatype=0)
        # LEAF rect (0,0)-(10,2) under 90deg rotation -> (-2, 0)-(0, 10).
        # Then MID is placed at (0,0) (rep 1) and (100, 0) (rep 2).
        # Instance 1: (-2, 0, 0, 10)
        # Instance 2: (98, 0, 100, 10)
        np.testing.assert_array_equal(rects, np.array([
            [-2, 0, 0, 10],
            [98, 0, 100, 10],
        ], dtype=np.int32))

    def test_arbitrary_angle_skips_all_K_in_one_warning(self, tmp_path: Path):
        """A non-quarter-turn placement with K=5 reps. Pre-vectorize
        warned + skipped per-K (5 warnings). Post-vectorize warns once
        but still accounts for all 5 skipped instances in stats."""
        store = self._setup_leaf(tmp_path, "bad_angle.oas")
        store._placements.setdefault(0, []).append(
            store_mod.Placement(
                target=1, target_kind="refnum",
                x=0, y=0,
                angle=45.0, magnification=1.0, flip=False,
                repetition_type=2,
                repetition_offsets=[(0, 0), (10, 0), (20, 0), (30, 0), (40, 0)],
            ))
        walker = walker_mod.CellGraphWalker(store)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            rects = walker.walk_to_root(root=0, layer=1, datatype=0)
        assert rects.shape == (0, 4)
        assert walker.stats.arbitrary_angle_skipped == 5
        # Vectorized path warns once for the whole placement; previously
        # it warned per-K (5 times). Either is acceptable as long as the
        # stats counter is correct.
        assert any("45" in str(w.message) for w in caught)

    def test_repetition_with_no_offsets_is_single_instance(self, tmp_path):
        """``repetition_offsets=[]`` means no repetition record -- a
        single placement at (p.x, p.y). Verify the vectorized code
        treats this as K=1 and doesn't drop the rect."""
        store = self._setup_leaf(tmp_path, "no_rep.oas")
        store._placements.setdefault(0, []).append(
            store_mod.Placement(
                target=1, target_kind="refnum",
                x=42, y=7,
                angle=0.0, magnification=1.0, flip=False,
                repetition_type=0, repetition_offsets=[],
            ))
        walker = walker_mod.CellGraphWalker(store)
        rects = walker.walk_to_root(root=0, layer=1, datatype=0)
        np.testing.assert_array_equal(
            rects, np.array([[42, 7, 52, 17]], dtype=np.int32))
        assert walker.stats.repetition_instances == 1

    def test_batch_transform_helper_K1_matches_apply_to_rects(self):
        """Direct unit test on ``_batch_transform_rects``: with K=1 the
        output must equal ``Transform.apply_to_rects`` (single-instance
        legacy path used by TestTransformBasics)."""
        rects = np.array([
            [0, 0, 10, 5],
            [-3, -8, 7, 2],
        ], dtype=np.int32)
        M = np.array([[0, -1], [1, 0]], dtype=np.float64)  # 90deg CCW
        t = np.array([100, 50], dtype=np.float64)
        # Reference: build a single-instance Transform and apply.
        ref = walker_mod.Transform(M=M, t=t).apply_to_rects(rects)
        # Batch with K=1.
        out = walker_mod.CellGraphWalker._batch_transform_rects(
            rects, M, t.reshape(1, 2))
        np.testing.assert_array_equal(out, ref)

    def test_batch_transform_helper_K_independent_translation(self):
        """K=3 instances under identity rotation: each output row must
        be the input row plus its instance translation."""
        rects = np.array([[0, 0, 10, 10]], dtype=np.int32)
        M = np.eye(2, dtype=np.float64)
        ts = np.array([[5, 0], [50, 0], [500, 0]], dtype=np.float64)
        out = walker_mod.CellGraphWalker._batch_transform_rects(rects, M, ts)
        np.testing.assert_array_equal(out, np.array([
            [5, 0, 15, 10],
            [50, 0, 60, 10],
            [500, 0, 510, 10],
        ], dtype=np.int32))


# ── pick_top_cell heuristic ─────────────────────────────────────────────────


class _FakeStore:
    """Minimal duck-typed store: only the attributes pick_top_cell touches.

    pick_top_cell reads ``cells`` (refnum -> name), ``_placements``
    (refnum -> [Placement]), and ``_rect_buffers`` (refnum -> {(L,D): seq}).
    The sequences only need ``__len__``; pick_top_cell never inspects the
    elements. We construct stores directly instead of round-tripping
    OASIS bytes so each test isolates the exact graph shape.
    """

    def __init__(
        self,
        cells: dict,
        placements: dict | None = None,
        rect_buffers: dict | None = None,
    ) -> None:
        self.cells = dict(cells)
        self._placements = placements or {}
        self._rect_buffers = rect_buffers or {}


def _pl(target):
    """Build a Placement with only ``target`` set -- the picker ignores
    every other field, so the rest can stay at safe defaults."""
    return store_mod.Placement(
        target=target, target_kind="refnum",
        x=0, y=0, angle=0.0, magnification=1.0, flip=False,
        repetition_type=None, repetition_offsets=[],
    )


class TestPickTopCell:
    def test_empty_store_returns_none(self):
        assert walker_mod.pick_top_cell(_FakeStore({})) is None

    def test_single_cell_is_the_root(self):
        s = _FakeStore({0: b"ONLY"})
        assert walker_mod.pick_top_cell(s) == 0

    def test_single_unreferenced_root(self):
        # ROOT places LEAF; ROOT is unreferenced -> root.
        s = _FakeStore(
            cells={0: b"ROOT", 1: b"LEAF"},
            placements={0: [_pl(1)]},
        )
        assert walker_mod.pick_top_cell(s) == 0

    def test_string_target_resolves_via_reverse_map(self):
        # PLACEMENT carries an inline name string instead of a refnum.
        s = _FakeStore(
            cells={0: b"ROOT", 1: b"LEAF"},
            placements={0: [_pl(b"LEAF")]},
        )
        assert walker_mod.pick_top_cell(s) == 0

    def test_d2db_style_prefers_placements_over_rects(self):
        # Reproduces the user-observed bug: partial-load D2DB where
        # iMerge_Top (lots of PLACEMENTs, zero own rects) was picked
        # last and a leaf with many rects won. With the new heuristic,
        # iMerge_Top wins.
        s = _FakeStore(
            cells={
                10: b"iMerge_Top",
                1: b"BIG_LEAF",
                2: b"SMALL_LEAF",
            },
            placements={
                # iMerge_Top has 3 placements pointing at internal cells
                # we haven't decoded yet (refnums 100/101/102) so those
                # targets don't appear in cells -- iMerge_Top stays a root.
                10: [_pl(100), _pl(101), _pl(102)],
            },
            rect_buffers={
                # BIG_LEAF has the most stored rectangles. Under the old
                # heuristic it would win; under the new one iMerge_Top
                # (3 placements) does.
                1: {(17, 102): list(range(3000))},
                2: {(17, 102): list(range(500))},
            },
        )
        assert walker_mod.pick_top_cell(s) == 10

    def test_flat_layout_falls_back_to_rect_count(self):
        # No placements anywhere -- e.g. a flattened test fixture.
        # Picker falls back to "most rectangles among roots".
        s = _FakeStore(
            cells={0: b"A", 1: b"B", 2: b"C"},
            placements={},
            rect_buffers={
                0: {(1, 0): list(range(10))},
                1: {(1, 0): list(range(100))},   # winner
                2: {(1, 0): list(range(50))},
            },
        )
        assert walker_mod.pick_top_cell(s) == 1

    def test_every_cell_referenced_returns_last_defined(self):
        # Pathological cycle: A places B, B places A. Both are
        # referenced, so the "unreferenced" set is empty and the
        # picker falls back to "highest refnum" (the writer-emitted-
        # last convention).
        s = _FakeStore(
            cells={0: b"A", 1: b"B"},
            placements={0: [_pl(1)], 1: [_pl(0)]},
        )
        assert walker_mod.pick_top_cell(s) == 1

    def test_ties_on_placements_are_stable(self):
        # Two roots with the same placement count -- max() is stable
        # for equal keys so whichever one max() encounters first wins.
        # We don't pin which but assert it's one of the candidates.
        s = _FakeStore(
            cells={0: b"R0", 1: b"R1", 2: b"LEAF"},
            placements={0: [_pl(2)], 1: [_pl(2)]},
        )
        assert walker_mod.pick_top_cell(s) in (0, 1)
