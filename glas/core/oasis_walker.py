"""Cell-graph walker: expand PLACEMENT hierarchy to root-cell coordinates (F2 M1.11c).

What this is
------------
``oasis_store.OasisGeometryStore`` accumulates per-cell, per-layer
rectangles + polygons + PLACEMENT graph, all in **cell-local**
coordinates. That representation is compact (only unique geometry is
stored once per cell definition) but it's not directly usable for
SEM-alignment work, which needs every rectangle in the root cell's
coordinate frame so we can rasterize and template-match.

``CellGraphWalker`` is the bridge: it takes a populated store, picks a
root cell, and recursively descends the PLACEMENT graph applying each
placement's transform (translation + 90deg rotation + flip + uniform
magnification) to every leaf cell's geometry. The result is a flat
``ndarray[N, 4]`` of (x1, y1, x2, y2) rectangles in root coords.

Per the user's M1.11c sign-off:

* **Rotation support**: 0deg / 90deg / 180deg / 270deg + flip. Arbitrary
  angles are accepted in the PLACEMENT record but skipped at expansion
  time with a ``warnings.warn`` -- avoids silently producing wrong data
  while keeping the walker resilient on production files that have
  mostly quarter-turns and the occasional 45deg outlier.
* **Output**: ``walk_to_root()`` returns one flat ndarray. For
  D2DB-class files the result fits comfortably in laptop memory
  (485K CMG rectangles ~= 7.7 MB at int32 x 4 columns).

What this is NOT
----------------
* No ROI-bbox clipping yet -- caller can ``np.where`` after the fact.
* No lazy / generator API -- if a file grows past memory, add a sibling
  ``walk_lazy()`` method in a follow-up.
* No GUI integration; that's M1.12.

Reference: SEMI P39 §22 (PLACEMENT) and §7.6 (repetition).
"""
from __future__ import annotations

import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import numpy as np

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import oasis_store  # noqa: E402


# ── Affine transform (uniform scale + D4 + translation) ──────────────────────


@dataclass(frozen=True)
class Transform:
    """Affine transform applied as ``v_out = M @ v_in + t``.

    Magnification is folded into ``M`` so the apply step is a single
    matmul. Translation is in **root-cell units** (the same scale as
    the rectangle data after the parent transform has been applied).

    For OASIS D4 transforms (quarter-turn + flip), ``M`` entries are in
    ``{-1, 0, 1}`` and the bbox of a rectangle under M stays
    axis-aligned, so ``apply_to_rects`` returns an exact ndarray ``(N, 4)``
    rather than a conservative bounding box. With non-trivial mag, the
    matrix entries scale linearly; mag != 1 is rare in production
    OASIS but supported.
    """
    M: np.ndarray
    t: np.ndarray

    @staticmethod
    def identity() -> "Transform":
        return Transform(M=np.eye(2, dtype=np.float64),
                         t=np.zeros(2, dtype=np.float64))

    @staticmethod
    def from_placement(x: int, y: int, angle_deg: float,
                       flip: bool, mag: float,
                       *, quarter_turn_tol: float = 0.01,
                       ) -> Optional["Transform"]:
        """Build a transform from a PLACEMENT record's fields.

        Returns ``None`` if the angle is not within tolerance of a
        quarter-turn -- the caller is expected to warn and skip.
        OASIS specifies that the flip (mirror about the x-axis) is
        applied **before** the rotation; we mirror that ordering by
        right-multiplying the rotation matrix by ``diag(1, -1)``.
        """
        a = angle_deg % 360
        rounded = round(a / 90) * 90
        # Reject anything more than ``quarter_turn_tol`` off a multiple
        # of 90deg. 0.01deg corresponds to ~17 urad which is well below
        # any meaningful layout precision.
        if abs(a - rounded) > quarter_turn_tol:
            return None
        quarter = int(round(rounded / 90)) % 4
        # CCW rotations (right-handed coordinate system).
        rotations = (
            np.array([[1, 0], [0, 1]], dtype=np.float64),    # 0deg
            np.array([[0, -1], [1, 0]], dtype=np.float64),   # 90deg
            np.array([[-1, 0], [0, -1]], dtype=np.float64),  # 180deg
            np.array([[0, 1], [-1, 0]], dtype=np.float64),   # 270deg
        )
        M = rotations[quarter]
        if flip:
            F = np.array([[1, 0], [0, -1]], dtype=np.float64)
            M = M @ F
        if mag != 1.0:
            M = M * mag
        t = np.array([x, y], dtype=np.float64)
        return Transform(M=M, t=t)

    def compose(self, child: "Transform") -> "Transform":
        """Return ``T`` such that ``T(v) = self(child(v))``.

        Derivation:
            T_child(v)  = M_c v + t_c
            T_parent(u) = M_p u + t_p
            T_parent(T_child(v)) = M_p (M_c v + t_c) + t_p
                                = (M_p M_c) v + (M_p t_c + t_p)

        So composed M is the matrix product and composed t is the
        parent transform applied to the child's translation, plus the
        parent's own translation.
        """
        return Transform(
            M=self.M @ child.M,
            t=self.M @ child.t + self.t,
        )

    def apply_to_rects(self, rects: np.ndarray) -> np.ndarray:
        """Transform ``(N, 4)`` rectangles ``(x1, y1, x2, y2)``.

        Computes all 4 corners per rectangle, applies the affine, and
        recovers the new axis-aligned bbox via min/max along each
        axis. For D4 transforms this is exact; for non-axis-aligned
        rotations it would be a conservative bbox, but those code
        paths are rejected upstream by ``from_placement``.
        """
        if rects.shape[0] == 0:
            return rects.copy()
        n = rects.shape[0]
        # corners: (N, 4, 2). Order: (x1,y1), (x2,y1), (x2,y2), (x1,y2).
        corners = np.empty((n, 4, 2), dtype=np.float64)
        corners[:, 0, 0] = rects[:, 0]
        corners[:, 0, 1] = rects[:, 1]
        corners[:, 1, 0] = rects[:, 2]
        corners[:, 1, 1] = rects[:, 1]
        corners[:, 2, 0] = rects[:, 2]
        corners[:, 2, 1] = rects[:, 3]
        corners[:, 3, 0] = rects[:, 0]
        corners[:, 3, 1] = rects[:, 3]
        # Apply: corners @ M.T + t  (broadcasting over the leading axes).
        transformed = corners @ self.M.T + self.t
        # bbox = min/max along corner-axis. For D4 the result is exact.
        out = np.empty((n, 4), dtype=rects.dtype)
        xs = transformed[:, :, 0]
        ys = transformed[:, :, 1]
        out[:, 0] = np.floor(xs.min(axis=1)).astype(rects.dtype)
        out[:, 1] = np.floor(ys.min(axis=1)).astype(rects.dtype)
        out[:, 2] = np.ceil(xs.max(axis=1)).astype(rects.dtype)
        out[:, 3] = np.ceil(ys.max(axis=1)).astype(rects.dtype)
        return out

    def apply_to_points(self, pts: np.ndarray) -> np.ndarray:
        """Transform ``(n, 2)`` point list (used for polygons)."""
        if pts.shape[0] == 0:
            return pts.copy()
        transformed = pts.astype(np.float64) @ self.M.T + self.t
        return np.round(transformed).astype(pts.dtype)


# ── Root-cell picking ────────────────────────────────────────────────────────


def pick_top_cell(store) -> "Optional[int]":
    """Heuristic root-cell selection for an OasisGeometryStore.

    OASIS encodes the intended top via the ``S_TOP_CELL`` property, but
    the streamer does not currently surface property values. Until it
    does (or until the gds_align_tool gains a "pick root" dropdown),
    callers rely on this heuristic:

    1. Compute ``roots = cells - placement_targets`` -- cells not the
       target of any decoded PLACEMENT. In a fully-decoded OASIS that
       is exactly one cell; in a partial-load slice it can be many.
    2. If a single root, return it.
    3. **If multiple roots, prefer the one with the most PLACEMENT
       records.** In hierarchical layouts (e.g. Calibre D2DB
       ``iMerge_Top``) the root is a *placement aggregator* whose own
       cell body has zero rectangles -- the geometry lives in leaf
       cells one or more levels down. Picking the cell with the most
       rectangles instead lands on a leaf and produces a 0-polygon
       walk. Observed on user's 345 MB D2DB under partial load.
    4. **Only when no root cell has any placements** (flat layouts,
       single-cell test files) fall back to "cell with the most
       rectangles".
    5. If every cell is referenced (pathological), return the
       last-defined cellname (a Calibre / klayout writer convention).
    """
    referenced: set = set()
    rev = {n: r for r, n in store.cells.items()}
    for placements in store._placements.values():
        for p in placements:
            tgt = p.target
            if isinstance(tgt, int):
                referenced.add(tgt)
            elif isinstance(tgt, str) and tgt in rev:
                referenced.add(rev[tgt])

    roots = set(store.cells.keys()) - referenced
    if not roots:
        if store.cells:
            return max(store.cells.keys())
        return None
    if len(roots) == 1:
        return next(iter(roots))

    def _placements_in(r: int) -> int:
        return len(store._placements.get(r, []))

    def _rects_in(r: int) -> int:
        return sum(len(b) for b in store._rect_buffers.get(r, {}).values())

    by_placements = max(roots, key=_placements_in, default=None)
    if by_placements is not None and _placements_in(by_placements) > 0:
        return by_placements
    return max(roots, key=_rects_in, default=None)


# ── Walker ───────────────────────────────────────────────────────────────────


@dataclass
class WalkStats:
    """Diagnostic counters surfaced after a walk_to_root call."""
    cells_visited: int = 0
    placements_expanded: int = 0
    repetition_instances: int = 0
    arbitrary_angle_skipped: int = 0
    cycles_skipped: int = 0
    unknown_target_skipped: int = 0
    rectangles_emitted: int = 0
    polygons_emitted: int = 0


class CellGraphWalker:
    """Walk an ``OasisGeometryStore`` rooted at a chosen cell."""

    def __init__(self, store: oasis_store.OasisGeometryStore) -> None:
        self._store = store
        # Reverse lookup: name -> refnum, so we can resolve placements
        # that target an inline a-string instead of a refnum.
        self._name_to_refnum = {
            name: refnum for refnum, name in store.cells.items()
        }
        self.stats = WalkStats()

    # ── Public API ─────────────────────────────────────────────────────────
    def walk_to_root(self, root: Union[int, str],
                     layer: int, datatype: int) -> np.ndarray:
        """Flatten every RECTANGLE on ``(layer, datatype)`` reachable from
        ``root`` into a single ``ndarray[N, 4]`` in root coordinates.

        ``root`` accepts either a cellname refnum (int) or a cell name
        (str). Returns an empty ndarray of shape ``(0, 4)`` when the
        layer is empty or the root has no geometry.
        """
        self.stats = WalkStats()
        root_key = self._resolve_target(root, kind=None)
        if root_key is None:
            raise KeyError(f"unknown root cell: {root!r}")
        chunks: list[np.ndarray] = []
        self._walk(root_key, Transform.identity(),
                   layer, datatype, chunks,
                   poly_out=None, visiting=set())
        if not chunks:
            return np.empty((0, 4), dtype=np.int32)
        return np.concatenate(chunks, axis=0)

    def walk_polygons_to_root(self, root: Union[int, str],
                              layer: int, datatype: int) -> list[np.ndarray]:
        """Flatten every POLYGON on ``(layer, datatype)`` reachable from
        ``root`` into a list of ``(n, 2)`` point arrays in root coords.

        Polygons stay per-instance (one entry per polygon record after
        repetition expansion) so callers can rasterize them
        individually without losing topology.
        """
        self.stats = WalkStats()
        root_key = self._resolve_target(root, kind=None)
        if root_key is None:
            raise KeyError(f"unknown root cell: {root!r}")
        poly_chunks: list[np.ndarray] = []
        self._walk(root_key, Transform.identity(),
                   layer, datatype, rect_out=None,
                   poly_out=poly_chunks, visiting=set())
        return poly_chunks

    # ── Internal recursion ─────────────────────────────────────────────────
    def _walk(self, cell, xform: Transform,
              layer: int, datatype: int,
              rect_out: Optional[list],
              poly_out: Optional[list],
              visiting: set) -> None:
        """Depth-first descent. ``rect_out`` / ``poly_out`` collect
        transformed shapes; pass ``None`` for whichever you don't want.

        ``visiting`` is the recursion-stack ancestor set, used to
        short-circuit cyclic references (which shouldn't appear in a
        well-formed OASIS but defensively cheap to detect).
        """
        if cell in visiting:
            warnings.warn(
                f"cyclic cell reference at {cell!r}; skipping recursion",
                RuntimeWarning,
            )
            self.stats.cycles_skipped += 1
            return
        visiting.add(cell)
        self.stats.cells_visited += 1
        try:
            if rect_out is not None:
                local = self._store.rectangles_for(cell, layer, datatype)
                if local.shape[0] > 0:
                    transformed = xform.apply_to_rects(local)
                    rect_out.append(transformed)
                    self.stats.rectangles_emitted += transformed.shape[0]
            if poly_out is not None:
                for pts in self._store.polygons_for(cell, layer, datatype):
                    poly_out.append(xform.apply_to_points(pts))
                    self.stats.polygons_emitted += 1

            for p in self._store.placements_for(cell):
                self._expand_placement(p, xform, layer, datatype,
                                       rect_out, poly_out, visiting)
        finally:
            visiting.discard(cell)

    def _expand_placement(self, p: oasis_store.Placement,
                          parent_xform: Transform,
                          layer: int, datatype: int,
                          rect_out: Optional[list],
                          poly_out: Optional[list],
                          visiting: set) -> None:
        """Expand one PLACEMENT into possibly-many instances via the
        repetition offset list, recursing into the target cell for each.

        M1.13.2 vectorization: the rotation/flip/mag matrix is the same
        for all K repetition instances of one placement -- only the
        translation varies. We build that ``(2, 2)`` matrix once and the
        ``(K, 2)`` translations in one ndarray, then split into two
        paths:

        * **Leaf fast path** (target has no nested placements and isn't
          on the recursion stack): emit ``K * N`` rectangles in a single
          batched ndarray op, skipping K recursive ``_walk`` calls
          entirely. This was the dominant cost on the D2DB benchmark
          (3.9 M reps from 1,160 placements -> minutes of Python loop).
        * **Slow path** (target has child placements or cycle): loop
          K times as before, but use the pre-batched M/t so we don't
          rebuild the transform via ``from_placement`` each iteration.

        Output is bit-identical to the M1.11c per-K implementation.
        Existing 23 walker tests and TestVectorizeEquivalence (M1.13.2)
        lock the equivalence.
        """
        target = self._resolve_target(p.target, p.target_kind)
        if target is None:
            self.stats.unknown_target_skipped += 1
            return

        # Build rotation/flip/mag once (shared across K instances).
        # ``base_xform.t`` already encodes (p.x, p.y); we'll add per-K
        # offsets below.
        base_xform = Transform.from_placement(
            p.x, p.y, p.angle, p.flip, p.magnification,
        )
        offsets = p.repetition_offsets or [(0, 0)]
        K = len(offsets)
        if base_xform is None:
            warnings.warn(
                f"non-quarter-turn angle {p.angle} for placement of "
                f"{p.target!r} at ({p.x}, {p.y}); skipping {K} instances",
                RuntimeWarning,
            )
            self.stats.arbitrary_angle_skipped += K
            return

        # Vectorized translation composition:
        #   T_place(v) = base.M @ v + (base.t + offset_k)
        #   T_parent(T_place(v)) = (parent.M @ base.M) v
        #                        + parent.M @ (base.t + offset_k) + parent.t
        offsets_arr = np.asarray(offsets, dtype=np.float64)  # (K, 2)
        place_ts = base_xform.t + offsets_arr                 # (K, 2)
        composed_M = parent_xform.M @ base_xform.M            # (2, 2), shared
        composed_ts = place_ts @ parent_xform.M.T + parent_xform.t  # (K, 2)

        self.stats.placements_expanded += K
        self.stats.repetition_instances += K

        # Decide fast vs slow path. Leaf fast-path requires:
        #  - target not on recursion stack (no cycle)
        #  - target has no further placements to descend into
        # Otherwise we must loop K times so each recursion can resolve
        # the child placement graph independently.
        target_has_placements = bool(self._store.placements_for(target))
        if target in visiting or target_has_placements:
            for k in range(K):
                composed = Transform(M=composed_M, t=composed_ts[k])
                self._walk(target, composed, layer, datatype,
                           rect_out, poly_out, visiting)
            return

        # ─── Leaf fast path ──────────────────────────────────────────
        # Each rep counts as one logical cell visit (preserve the
        # ``cells_visited`` semantics that M1.11c production runs
        # relied on).
        self.stats.cells_visited += K
        if rect_out is not None:
            local = self._store.rectangles_for(target, layer, datatype)
            if local.shape[0] > 0:
                emitted = self._batch_transform_rects(
                    local, composed_M, composed_ts,
                )
                rect_out.append(emitted)
                self.stats.rectangles_emitted += emitted.shape[0]
        if poly_out is not None:
            for pts in self._store.polygons_for(target, layer, datatype):
                # M is shared across K -- rotate the polygon shape once,
                # then translate K times. Polygons are <0.1% in
                # production so the per-K append loop is acceptable.
                rotated = pts.astype(np.float64) @ composed_M.T
                for k in range(K):
                    shifted = np.round(rotated + composed_ts[k]).astype(
                        pts.dtype)
                    poly_out.append(shifted)
                self.stats.polygons_emitted += K

    @staticmethod
    def _batch_transform_rects(rects: np.ndarray,
                               M: np.ndarray,
                               translations: np.ndarray) -> np.ndarray:
        """Apply ``v -> M @ v + t_k`` to N rectangles for K translations.

        Returns ``(K * N, 4)`` of [x1, y1, x2, y2] in the input dtype.

        Equivalence with ``Transform(M=M, t=t_k).apply_to_rects(rects)``
        called K times and concatenated: per-rect we compute the 4
        corners, apply ``M`` (no translation yet), reduce to
        ``[xmin, ymin, xmax, ymax]`` per rect, then broadcast the K
        translations on top with the same ``floor / ceil`` rounding the
        single-instance path uses. ``floor(min(M@c) + t)`` ==
        ``floor(min(M@c + t))`` because ``min`` commutes with a constant
        translation, so the rounding result matches bit-for-bit.
        """
        N = rects.shape[0]
        K = translations.shape[0]
        if N == 0 or K == 0:
            return np.empty((0, 4), dtype=rects.dtype)

        # (N, 4, 2) corners, order (x1,y1) (x2,y1) (x2,y2) (x1,y2).
        corners = np.empty((N, 4, 2), dtype=np.float64)
        corners[:, 0, 0] = rects[:, 0]
        corners[:, 0, 1] = rects[:, 1]
        corners[:, 1, 0] = rects[:, 2]
        corners[:, 1, 1] = rects[:, 1]
        corners[:, 2, 0] = rects[:, 2]
        corners[:, 2, 1] = rects[:, 3]
        corners[:, 3, 0] = rects[:, 0]
        corners[:, 3, 1] = rects[:, 3]
        rotated = corners @ M.T              # (N, 4, 2)
        xs = rotated[:, :, 0]
        ys = rotated[:, :, 1]
        xmin = xs.min(axis=1)                # (N,)
        ymin = ys.min(axis=1)
        xmax = xs.max(axis=1)
        ymax = ys.max(axis=1)

        tx = translations[:, 0]              # (K,)
        ty = translations[:, 1]
        out_x1 = np.floor(xmin[None, :] + tx[:, None])  # (K, N)
        out_y1 = np.floor(ymin[None, :] + ty[:, None])
        out_x2 = np.ceil(xmax[None, :] + tx[:, None])
        out_y2 = np.ceil(ymax[None, :] + ty[:, None])
        out = np.stack([out_x1, out_y1, out_x2, out_y2], axis=-1)
        return out.reshape(-1, 4).astype(rects.dtype)

    def _resolve_target(self, target, kind=None) -> Optional[int]:
        """Map a placement target to the integer cell key the store uses.

        ``target`` may be an int refnum, a str (decoded a-string name),
        bytes (raw a-string from older payloads), or ``None`` (modal /
        unresolved). Returns the refnum to use as a dict key in the
        store, or ``None`` when nothing matches.
        """
        if target is None:
            return None
        if isinstance(target, int):
            return target if target in self._store.cells else target
        if isinstance(target, bytes):
            target = target.decode("ascii", "backslashreplace")
        if isinstance(target, str):
            return self._name_to_refnum.get(target)
        return None


# ── CLI smoke test ───────────────────────────────────────────────────────────


def _main() -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(
        description="Flatten an OASIS file's geometry to root-cell "
                    "coordinates (F2 M1.11c walker layer). Drives the "
                    "M1.11b store and walks the PLACEMENT graph.",
    )
    ap.add_argument("path", help="OASIS file to walk")
    ap.add_argument(
        "--root", required=True,
        help="Root cell: refnum (integer) or cell name (string).",
    )
    ap.add_argument(
        "--layer", required=True, metavar="L:D",
        help="Layer to flatten as LAYER:DATATYPE.",
    )
    ap.add_argument(
        "--max-records", type=int, default=0, metavar="N",
        help="Pass-through to the store: stop after N records (0 = full).",
    )
    ap.add_argument(
        "--allow-unfiltered", action="store_true",
        help="Override the store's large-file filter requirement.",
    )
    args = ap.parse_args()

    try:
        L, D = args.layer.split(":")
        layer, datatype = int(L), int(D)
    except ValueError:
        print(f"bad --layer {args.layer!r}, want L:D", file=sys.stderr)
        return 2

    # Build the store with the requested layer pre-filtered so memory
    # stays bounded on the full file.
    store = oasis_store.OasisGeometryStore(
        args.path,
        wanted_layers={(layer, datatype)},
        allow_unfiltered=args.allow_unfiltered,
    )
    store.run(max_records=args.max_records, progress_every=500_000)

    # Root may be either int (refnum) or str (cell name).
    try:
        root = int(args.root)
    except ValueError:
        root = args.root

    walker = CellGraphWalker(store)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        rects = walker.walk_to_root(root, layer, datatype)

    summary = {
        "root": args.root,
        "layer": [layer, datatype],
        "rectangles_in_root_coords": int(rects.shape[0]),
        "stats": {
            "cells_visited": walker.stats.cells_visited,
            "placements_expanded": walker.stats.placements_expanded,
            "arbitrary_angle_skipped": walker.stats.arbitrary_angle_skipped,
            "cycles_skipped": walker.stats.cycles_skipped,
            "unknown_target_skipped": walker.stats.unknown_target_skipped,
            "rectangles_emitted": walker.stats.rectangles_emitted,
        },
        "warnings": [str(w.message) for w in caught[:10]],
        "warnings_total": len(caught),
    }
    if rects.shape[0] > 0:
        summary["bbox_in_root_coords"] = [
            int(rects[:, 0].min()),
            int(rects[:, 1].min()),
            int(rects[:, 2].max()),
            int(rects[:, 3].max()),
        ]
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
