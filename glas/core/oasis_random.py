"""Random-access single-cell decoder for OASIS (F2 M3.5b).

The full-file ``OasisGeometryStore`` decodes every record front to back
(hours on a 345 MB D2DB). For ROI-bounded load we instead seek straight
to a cell's CELL record using the ``S_CELL_OFFSET`` byte-offset index
(M3.5a) and decode *only that one cell* — its own geometry + its
PLACEMENT children + a local bounding box. The top-down ROI walker
(M3.5c) drives this, descending only into cells whose placed bbox
touches the SEM image's field of view, so the vast majority of the file
is never decoded.

Why seeking mid-stream is safe: a CELL record resets all OASIS modal
state (``reset_on_cell_boundary``), so decoding that starts at a CELL
byte offset needs no prior context. CBLOCK substreams inside the cell
are handled transparently by ``OasisStream``; we clear any dangling
substream frames before each seek.

Public surface::

    rar = RandomAccessReader(path, wanted_layers={(17, 0)})
    rar.has_offsets()           # False -> fall back to full decode
    content = rar.load_cell(refnum_or_name)   # memoized
    content.rects((17, 0))      # ndarray (N, 4) cell-local x1,y1,x2,y2
    content.placements          # list[Placement] (children)
    content.bbox                # (x0,y0,x1,y1) of own geometry, or None
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import oasis_streamer as oas      # noqa: E402
import devlog                      # noqa: E402  (dev-mode coloured tags)
import layerscan_cache             # noqa: E402  (F12 M3: layer-scan sidecar)
import cellcache                   # noqa: E402  (F16-B: decoded-cell sidecar)
from oasis_store import Placement  # noqa: E402
from oasis_walker import Transform  # noqa: E402

# F26 M2a-integrate: optional native rectangle-run decoder. When the compiled
# extension is present (a CI-built .pyd dropped into glas/core, or a local
# build_ext), per-cell decode (_decode_at — the batch ROI-walk hot path) gobbles
# whole RECTANGLE runs in one C call instead of per-record Python varint loops.
# Absent → the pure-Python path below runs unchanged. The two MUST stay
# byte-identical (CLAUDE.md §7); test_oasis_native_decode pins them together.
try:
    import oasis_fastdecode as _FAST   # noqa: E402
    if getattr(_FAST, "VERSION", 0) < 4:
        # <4 lacks the rewind-before-modal invariant + the started flag that
        # _decode_at_native's per-run gobble relies on; fall back to Python.
        _FAST = None
except Exception:                       # pragma: no cover - no build present
    _FAST = None

# F27 M1: walk helpers (roi_overlap_mask) need VERSION >= 5; keep a separate
# gate so an older v4 .pyd still accelerates decode but not the walk.
_FASTW = _FAST if (_FAST is not None and getattr(_FAST, "VERSION", 0) >= 5) \
    else None
# F27 M3: native subtree walk (walk_rects_native) needs VERSION >= 6.
_FASTWALK = _FAST if (_FAST is not None and getattr(_FAST, "VERSION", 0) >= 6) \
    else None
# F27 M3b caps: flattening materializes the WHOLE reachable graph's geometry
# (ROI-independent), so it's only worth it for a small graph. A big production
# chip (E3B ~13k cells) would pay a multi-second whole-chip decode + array build
# per worker before any output — so bail to the Python walk above these. (M3c:
# a shared/persisted flatten will lift this.)
_NATIVE_WALK_MAX_CELLS = 4000
_NATIVE_WALK_MAX_RECTS = 400_000

LayerKey = tuple[int, int]
Bbox = tuple[float, float, float, float]

# Calibre D2DB per-cell boundary layer: one rectangle per geometry cell
# whose extent equals the cell's bbox (F2 M3.5e.3, verified on
# E3B_CMG_CMP_D2DB_250930.oas — 12/12 sampled cells). Used to make the
# reachable_bbox prune pass read ~one rectangle per cell instead of
# decoding the cell's full geometry. Override per file if needed.
DEFAULT_BBOX_LAYER: LayerKey = (108, 250)


class WalkCancelled(Exception):
    """Raised inside walk_roi when the caller's cancel_cb returns True."""

def _env_debug_level() -> int:
    v = os.environ.get("MMH_GDS_DEBUG", "").lower()
    if v in ("2", "trace", "verbose", "all"):
        return 2
    if v in ("1", "true", "yes", "on"):
        return 1
    return 0


_DEBUG_LEVEL = _env_debug_level()
DEBUG = _DEBUG_LEVEL >= 1     # level 1: concise per-load summary
TRACE = _DEBUG_LEVEL >= 2     # level 2: deep per-cell / per-section telemetry


def set_debug(on: bool, level: int = 1) -> None:
    """Toggle ROI debug output. ``level`` 1 = concise summary (decode/walk
    timings, cache hits, errors); 2 = full trace. Also via MMH_GDS_DEBUG=1|2."""
    global DEBUG, TRACE, _DEBUG_LEVEL
    _DEBUG_LEVEL = (level if on else 0)
    DEBUG = _DEBUG_LEVEL >= 1
    TRACE = _DEBUG_LEVEL >= 2


def _dbg(msg: str) -> None:
    if DEBUG:
        print(f"{devlog.tag('roi', stream=sys.stderr)} {msg}",
              file=sys.stderr, flush=True)


def _trace(msg: str) -> None:
    if TRACE:
        print(f"{devlog.tag('roi', stream=sys.stderr)} {msg}",
              file=sys.stderr, flush=True)


def _hexdump(buf: bytes, center: int, span: int = 12) -> str:
    lo = max(0, center - span)
    hi = min(len(buf), center + span)
    parts = []
    for i in range(lo, hi):
        mark = ">" if i == center else " "
        parts.append(f"{mark}{buf[i]:02x}")
    return f"@{lo}..{hi}:" + "".join(parts)


@dataclass
class CellContent:
    """One cell's own (cell-local) geometry + children, stored as compact
    descriptors so the bbox scan never materializes a huge repeated array
    (M3.5e). Geometry is expanded lazily — and vectorized — only for the
    few cells whose geometry is actually emitted inside the ROI.

    * ``rect_specs[key]`` -> list of ``(x1, y1, x2, y2, rtype, raw)`` —
      one base rectangle + its repetition descriptor.
    * ``poly_specs[key]`` -> list of ``(base_pts (n,2), rtype, raw)``.
    * ``bbox`` -> analytic ``(x0,y0,x1,y1)`` over all layers (base bbox
      extended by each repetition's extent), without expanding anything.
    """
    rect_specs: dict[LayerKey, list] = field(default_factory=dict)
    poly_specs: dict[LayerKey, list] = field(default_factory=dict)
    # Placements are lazy (F16-B F18): a freshly decoded cell sets ``_placements``
    # (the list); a cache load sets ``_pl_soa`` (struct-of-arrays) and the list is
    # built on demand. With the prep precompute persisted (M7), the prune never
    # iterates placements, so a cache-loaded big cell skips rebuilding ~1.5M
    # Placement objects entirely. Access via the ``placements`` property,
    # ``placement_count()`` and ``placement_at(i)``.
    _placements: Optional[list] = field(default=None, repr=False, compare=False)
    _pl_soa: Optional[dict] = field(default=None, repr=False, compare=False)
    bbox: Optional[Bbox] = None
    # Cache of per-key (base_bbox, array_extent_bbox) ndarrays, built once and
    # reused across every ROI walk visit of this (memoized) cell — the extent
    # build (incl. expand_repetition for arbitrary-list types) is otherwise
    # repeated per visit per layer, which dominated big-chip ROI loads (F16).
    _ext_cache: dict = field(default_factory=dict, repr=False, compare=False)
    # Optional columnar backing (F16-B M2). Populated only when a cell is loaded
    # from the on-disk decode cache; empty for freshly decoded cells (which keep
    # the rect_specs/poly_specs tuple-lists). The accessors below prefer the
    # columnar form when present, so a cache load never rebuilds millions of
    # tuples. Layout:
    #   _rcol[key] = (coords (N,4) int64, rt (N,) int16 [None->-1], rr (N,) object)
    #   _pcol[key] = (pts (Σn,2) int, off (P+1,) int64, rt (P,) int16, rr (P,) object)
    _rcol: dict = field(default_factory=dict, repr=False, compare=False)
    _pcol: dict = field(default_factory=dict, repr=False, compare=False)
    # F16-B M6: cached placement-prune precompute (the ~20s gather over millions
    # of child placements). Cell-local + reachable_bbox only => ROI/transform
    # independent, so it is built once and reused across every ROI and layer of a
    # session (the per-ROI walk then only does a vectorized transform + mask).
    _place_prep: object = field(default=None, repr=False, compare=False)

    # ── lazy placements (F18) ────────────────────────────────────────────────
    @property
    def placements(self) -> list:
        if self._placements is None:
            s = self._pl_soa
            self._placements = ([self._pl_from_soa(s, i)
                                 for i in range(len(s["x"]))]
                                if s is not None else [])
        return self._placements

    @placements.setter
    def placements(self, value) -> None:
        self._placements = value

    def placement_count(self) -> int:
        if self._placements is not None:
            return len(self._placements)
        if self._pl_soa is not None:
            return len(self._pl_soa["x"])
        return 0

    def placement_at(self, i: int):
        if self._placements is not None:
            return self._placements[i]
        if self._pl_soa is not None:
            return self._pl_from_soa(self._pl_soa, i)
        raise IndexError(i)

    @staticmethod
    def _pl_from_soa(s: dict, i: int):
        r = int(s["rt"][i])
        names = s["names"]
        tgt = names[i] if (names and i in names) else int(s["target"][i])
        return Placement(tgt, _KIND_NAMES[int(s["kind"][i])],
                         int(s["x"][i]), int(s["y"][i]), float(s["angle"][i]),
                         float(s["mag"][i]), bool(s["flip"][i]),
                         (None if r < 0 else r), [], s["rr"][i])

    # ── columnar-or-tuple accessors used by the walk ──────────────────────────
    def rect_keys(self):
        return self._rcol.keys() if self._rcol else self.rect_specs.keys()

    def poly_keys(self):
        return self._pcol.keys() if self._pcol else self.poly_specs.keys()

    def rect_count(self, key: LayerKey) -> int:
        col = self._rcol.get(key)
        if col is not None:
            return col[0].shape[0]
        s = self.rect_specs.get(key)
        return len(s) if s else 0

    def poly_count(self, key: LayerKey) -> int:
        col = self._pcol.get(key)
        if col is not None:
            return col[2].shape[0]
        s = self.poly_specs.get(key)
        return len(s) if s else 0

    def total_rects(self) -> int:
        return sum(self.rect_count(k) for k in self.rect_keys())

    def total_polys(self) -> int:
        return sum(self.poly_count(k) for k in self.poly_keys())

    def rect_spec_at(self, key: LayerKey, i: int):
        """``(x1, y1, x2, y2, rtype, raw)`` for the i-th rect on ``key``."""
        col = self._rcol.get(key)
        if col is not None:
            c, rt, rr = col
            r = int(rt[i])
            return (int(c[i, 0]), int(c[i, 1]), int(c[i, 2]), int(c[i, 3]),
                    (None if r < 0 else r), rr[i])
        return self.rect_specs[key][i]

    def poly_spec_at(self, key: LayerKey, i: int):
        """``(base_pts (n,2), rtype, raw)`` for the i-th polygon on ``key``."""
        col = self._pcol.get(key)
        if col is not None:
            pts, off, rt, rr = col
            r = int(rt[i])
            return (pts[off[i]:off[i + 1]], (None if r < 0 else r), rr[i])
        return self.poly_specs[key][i]

    def all_rect_rtypes(self) -> set:
        out: set = set()
        if self._rcol:
            for c, rt, rr in self._rcol.values():
                out.update(None if int(v) < 0 else int(v) for v in np.unique(rt))
        else:
            for specs in self.rect_specs.values():
                for s in specs:
                    out.add(s[4])
        return out

    def all_poly_rtypes(self) -> set:
        out: set = set()
        if self._pcol:
            for pts, off, rt, rr in self._pcol.values():
                out.update(None if int(v) < 0 else int(v) for v in np.unique(rt))
        else:
            for specs in self.poly_specs.values():
                for s in specs:
                    out.add(s[1])
        return out

    def rect_arrays(self, key: LayerKey):
        """``(base (M,4), extent (M,4))`` local-frame bbox arrays for the
        rectangles on ``key`` — ``base`` is each rect's own bbox, ``extent`` is
        that bbox grown by the repetition extent. Built once and cached.

        Vectorized: most rects have no repetition, so ``extent == base`` for them
        (one array copy); only the few repeated rects need a per-spec extent."""
        ck = ("r", key)
        got = self._ext_cache.get(ck)
        if got is None:
            col = self._rcol.get(key)
            if col is not None:
                c, rt, rr = col
                base = c.astype(np.float64)              # vectorized
            else:
                specs = self.rect_specs.get(key) or ()
                M = len(specs)
                if M:
                    base = np.array([s[:4] for s in specs], dtype=np.float64)
                    rt = np.fromiter(((-1 if s[4] is None else s[4])
                                      for s in specs), dtype=np.int16, count=M)
                    rr = np.empty(M, dtype=object)
                    for i, s in enumerate(specs):
                        rr[i] = s[5]
                else:
                    base = np.empty((0, 4), dtype=np.float64)
                    rt = np.empty(0, dtype=np.int16)
                    rr = np.empty(0, dtype=object)
            got = (base, _ext_from_columnar(base, rt, rr))
            self._ext_cache[ck] = got
        return got

    def poly_arrays(self, key: LayerKey):
        """``(base (P,4), extent (P,4))`` local-frame bbox arrays for the
        polygons on ``key``. Built once and cached. The columnar path computes
        every polygon's bbox with a single vectorized ``reduceat`` over the CSR
        point buffer; only repeated polygons need a per-spec extent."""
        ck = ("p", key)
        got = self._ext_cache.get(ck)
        if got is None:
            col = self._pcol.get(key)
            if col is not None:
                pts, off, rt, rr = col
                P = rt.shape[0]
                if P:
                    seg = off[:-1]
                    base = np.empty((P, 4), dtype=np.float64)
                    base[:, 0] = np.minimum.reduceat(pts[:, 0], seg)
                    base[:, 1] = np.minimum.reduceat(pts[:, 1], seg)
                    base[:, 2] = np.maximum.reduceat(pts[:, 0], seg)
                    base[:, 3] = np.maximum.reduceat(pts[:, 1], seg)
                else:
                    base = np.empty((0, 4), dtype=np.float64)
                ext = _ext_from_columnar(base, rt, rr)
            else:
                P = self.poly_count(key)
                base = np.empty((P, 4), dtype=np.float64)
                rt = np.empty(P, dtype=np.int16)
                rr = np.empty(P, dtype=object)
                for i in range(P):
                    b, _rt, _rr = self.poly_spec_at(key, i)
                    base[i, 0] = float(b[:, 0].min()); base[i, 1] = float(b[:, 1].min())
                    base[i, 2] = float(b[:, 0].max()); base[i, 3] = float(b[:, 1].max())
                    rt[i] = -1 if _rt is None else _rt
                    rr[i] = _rr
                ext = _ext_from_columnar(base, rt, rr)
            got = (base, ext)
            self._ext_cache[ck] = got
        return got

    def is_empty(self) -> bool:
        return not (self.rect_specs or self.poly_specs or self.placement_count()
                    or self._rcol or self._pcol)

    def rects(self, key: LayerKey, dtype=np.int32) -> np.ndarray:
        """Materialize all rectangles on ``key`` as ``(N, 4)`` (vectorized
        repetition expansion). Empty ``(0, 4)`` when none."""
        n = self.rect_count(key)
        if not n:
            return np.empty((0, 4), dtype=dtype)
        out = []
        for i in range(n):
            x1, y1, x2, y2, rt, rr = self.rect_spec_at(key, i)
            offs = oas.repetition_offsets_np(rt, rr)        # (M, 2)
            arr = np.empty((offs.shape[0], 4), dtype=np.float64)
            arr[:, 0] = x1 + offs[:, 0]; arr[:, 1] = y1 + offs[:, 1]
            arr[:, 2] = x2 + offs[:, 0]; arr[:, 3] = y2 + offs[:, 1]
            out.append(arr)
        return np.concatenate(out).astype(dtype)

    def polys(self, key: LayerKey) -> list:
        """Materialize polygons on ``key`` as a list of ``(n, 2)`` arrays."""
        out = []
        for i in range(self.poly_count(key)):
            base, rt, rr = self.poly_spec_at(key, i)
            for dx, dy in oas.repetition_offsets_np(rt, rr):
                s = base.copy()
                s[:, 0] += int(dx); s[:, 1] += int(dy)
                out.append(s)
        return out

    # ── on-disk decode cache (F16-B M3): columnar (de)serialization ───────────
    #
    # The repetition descriptor ``rr`` is None for the vast majority of records,
    # so it is stored *sparsely* (only the non-None positions + their raws) to
    # keep the object-array pickled by savez tiny — otherwise serialising/loading
    # an 8.8M-entry mostly-None object array would dominate the cache round-trip.
    @staticmethod
    def _sparse_rr(rr_list):
        idx = [i for i, r in enumerate(rr_list) if r is not None]
        # Build the object array element-wise: np.array([(a,b,c,d)], object)
        # would wrongly make a 2-D array when the raws are equal-length tuples.
        val = np.empty(len(idx), dtype=object)
        for j, i in enumerate(idx):
            val[j] = rr_list[i]
        return (np.array(idx, dtype=np.int64), val)

    @staticmethod
    def _dense_rr(n, idx, val):
        rr = np.full(n, None, dtype=object)
        if idx.shape[0]:
            rr[idx] = val
        return rr

    def to_cache_arrays(self) -> dict:
        """Flatten this cell into a dict of ndarrays for ``np.savez`` — columnar
        geometry (sparse ``rr``) + SoA placements + a tiny JSON meta. ``rt``
        arrays use -1 for a None repetition type; raws round-trip verbatim."""
        out: dict = {}
        rkeys = []
        for key in list(self.rect_keys()):
            n = self.rect_count(key)
            coords = np.empty((n, 4), dtype=np.int64)
            rt = np.empty(n, dtype=np.int16)
            rrs = []
            for i in range(n):
                x1, y1, x2, y2, t, r = self.rect_spec_at(key, i)
                coords[i, 0] = x1; coords[i, 1] = y1
                coords[i, 2] = x2; coords[i, 3] = y2
                rt[i] = -1 if t is None else t
                rrs.append(r)
            l, d = key
            ri, rv = self._sparse_rr(rrs)
            out[f"r__{l}__{d}__coords"] = coords
            out[f"r__{l}__{d}__rt"] = rt
            out[f"r__{l}__{d}__rri"] = ri
            out[f"r__{l}__{d}__rrv"] = rv
            rkeys.append([l, d])
        pkeys = []
        for key in list(self.poly_keys()):
            P = self.poly_count(key)
            parts = []
            off = np.zeros(P + 1, dtype=np.int64)
            rt = np.empty(P, dtype=np.int16)
            rrs = []
            for i in range(P):
                b, t, r = self.poly_spec_at(key, i)
                b = np.asarray(b)
                parts.append(b)
                off[i + 1] = off[i] + b.shape[0]
                rt[i] = -1 if t is None else t
                rrs.append(r)
            pts = (np.concatenate(parts) if parts
                   else np.empty((0, 2), dtype=np.int64))
            l, d = key
            pi, pv = self._sparse_rr(rrs)
            out[f"p__{l}__{d}__pts"] = pts
            out[f"p__{l}__{d}__off"] = off
            out[f"p__{l}__{d}__rt"] = rt
            out[f"p__{l}__{d}__rri"] = pi
            out[f"p__{l}__{d}__rrv"] = pv
            pkeys.append([l, d])
        # Placements as SoA with int target/kind (no 1.5M-object pickle): refnum
        # targets in an int64 array (-1 marks a name target, whose string goes in
        # a sparse side-table); kind as an int8 code. This + lazy rebuild on load
        # is what makes a cache load fast (F18).
        pl = self.placements
        n = len(pl)
        targets = np.empty(n, dtype=np.int64)
        kinds = np.empty(n, dtype=np.int8)
        name_idx = []
        name_val = []
        for i, p in enumerate(pl):
            t = p.target
            if isinstance(t, (int, np.integer)):
                targets[i] = int(t)
            else:                                    # inline name target
                targets[i] = -1
                name_idx.append(i)
                name_val.append(str(t))
            kinds[i] = _KIND_CODES.get(p.target_kind, 2)
        out["pl__target"] = targets
        out["pl__kind"] = kinds
        out["pl__nidx"] = np.array(name_idx, dtype=np.int64)
        out["pl__nval"] = np.array(name_val, dtype=object)
        out["pl__x"] = np.fromiter((p.x for p in pl), dtype=np.int64, count=n)
        out["pl__y"] = np.fromiter((p.y for p in pl), dtype=np.int64, count=n)
        out["pl__angle"] = np.fromiter((p.angle for p in pl), dtype=np.float64, count=n)
        out["pl__mag"] = np.fromiter((p.magnification for p in pl), dtype=np.float64, count=n)
        out["pl__flip"] = np.fromiter((p.flip for p in pl), dtype=bool, count=n)
        out["pl__rt"] = np.fromiter(
            ((-1 if p.repetition_type is None else p.repetition_type) for p in pl),
            dtype=np.int16, count=n)
        pli, plv = self._sparse_rr([p.repetition_raw for p in pl])
        out["pl__rri"] = pli
        out["pl__rrv"] = plv
        meta = {"rkeys": rkeys, "pkeys": pkeys, "n_pl": n,
                "bbox": list(self.bbox) if self.bbox is not None else None}
        out["__meta__"] = np.frombuffer(
            json.dumps(meta).encode("utf-8"), dtype=np.uint8)
        return out

    @classmethod
    def from_cache_arrays(cls, a: dict) -> "CellContent":
        """Rebuild a :class:`CellContent` from :meth:`to_cache_arrays` output.
        Geometry stays columnar (``_rcol``/``_pcol``) and placements stay SoA
        (``_pl_soa``); neither the millions of geometry tuples nor the millions
        of Placement objects are rebuilt — they're materialised lazily on the
        rare paths that need them (F18)."""
        meta = json.loads(bytes(a["__meta__"]).decode("utf-8"))
        rcol = {}
        for l, d in meta["rkeys"]:
            coords = a[f"r__{l}__{d}__coords"]; rt = a[f"r__{l}__{d}__rt"]
            rr = cls._dense_rr(coords.shape[0], a[f"r__{l}__{d}__rri"],
                               a[f"r__{l}__{d}__rrv"])
            rcol[(l, d)] = (coords, rt, rr)
        pcol = {}
        for l, d in meta["pkeys"]:
            rt = a[f"p__{l}__{d}__rt"]
            rr = cls._dense_rr(rt.shape[0], a[f"p__{l}__{d}__rri"],
                               a[f"p__{l}__{d}__rrv"])
            pcol[(l, d)] = (a[f"p__{l}__{d}__pts"], a[f"p__{l}__{d}__off"], rt, rr)
        n = int(meta["n_pl"])
        pl_soa = None
        if n:
            nidx = a["pl__nidx"]; nval = a["pl__nval"]
            names = {int(nidx[j]): str(nval[j]) for j in range(nidx.shape[0])}
            pl_soa = {"target": a["pl__target"], "kind": a["pl__kind"],
                      "x": a["pl__x"], "y": a["pl__y"], "angle": a["pl__angle"],
                      "mag": a["pl__mag"], "flip": a["pl__flip"],
                      "rt": a["pl__rt"],
                      "rr": cls._dense_rr(n, a["pl__rri"], a["pl__rrv"]),
                      "names": names}
        bbox = tuple(meta["bbox"]) if meta["bbox"] is not None else None
        return cls(rect_specs={}, poly_specs={}, _placements=None,
                   _pl_soa=pl_soa, bbox=bbox, _rcol=rcol, _pcol=pcol)


def _iv_contains(iv: tuple, v: int) -> bool:
    """Does an OASIS unsigned-interval ``(min, max)`` contain ``v``?
    ``max == -1`` is the spec's INF sentinel (see decode_interval)."""
    lo, hi = iv
    return v >= lo and (hi < 0 or v <= hi)


def _iv_width(iv: tuple) -> float:
    """Width of an OASIS interval; ``inf`` for an unbounded (``..INF``) one."""
    lo, hi = iv
    return (hi - lo) if hi >= 0 else float("inf")


def _iv_is_all_layers(iv: tuple) -> bool:
    """An ``(0, INF)`` interval — matches every layer, so a LAYERNAME using it
    is a file-wide default/placeholder that can't distinguish layers."""
    return iv[0] == 0 and iv[1] < 0


def resolve_layer_name(layernames: list, layer: int, datatype: int) -> str:
    """Name for ``(layer, datatype)`` from LAYERNAME records, or "" (F3 M2).

    ``layernames`` is ``[(name, layer_iv, datatype_iv), ...]``. Among the
    records containing ``(layer, datatype)`` the *most specific* wins (narrowest
    layer interval, then narrowest datatype interval) so a broad range never
    masks an exact label. An all-layers ``(0, INF)`` catch-all is skipped
    entirely — otherwise a single placeholder LAYERNAME would label every layer
    the same (the observed "every layer shows the first name" bug)."""
    best: Optional[str] = None
    best_key: Optional[tuple] = None
    for name, liv, div in layernames:
        if not name:
            continue
        if not (_iv_contains(liv, layer) and _iv_contains(div, datatype)):
            continue
        if _iv_is_all_layers(liv):
            continue
        key = (_iv_width(liv), _iv_width(div))
        if best is None or key < best_key:
            best, best_key = name, key
    return best or ""


def _analytic_bbox(rect_specs: dict, poly_specs: dict) -> Optional[Bbox]:
    """Cell-local bbox over all layers from descriptors — base geometry
    bbox extended by each repetition's analytic extent (no expansion)."""
    boxes: list = []
    for specs in rect_specs.values():
        for x1, y1, x2, y2, rt, rr in specs:
            ex0, ey0, ex1, ey1 = oas.repetition_extent(rt, rr)
            boxes.append((x1 + ex0, y1 + ey0, x2 + ex1, y2 + ey1))
    for specs in poly_specs.values():
        for base, rt, rr in specs:
            ex0, ey0, ex1, ey1 = oas.repetition_extent(rt, rr)
            boxes.append((base[:, 0].min() + ex0, base[:, 1].min() + ey0,
                          base[:, 0].max() + ex1, base[:, 1].max() + ey1))
    return _union_bbox(boxes)


def _obj1(v) -> np.ndarray:
    """A length-1 object array holding ``v`` verbatim. Built element-wise so a
    tuple ``v`` is stored as a single cell (``np.array([v], object)`` would make
    a 2-D array when ``v`` is an equal-length tuple)."""
    a = np.empty(1, dtype=object)
    a[0] = v
    return a


def _analytic_bbox_columnar(rcol: dict, poly_specs: dict) -> Optional[Bbox]:
    """Same analytic cell bbox as :func:`_analytic_bbox`, but reading the
    columnar rectangle form ``rcol[key] = (coords (N,4), rt (N,), rr (N,))`` the
    native decode path (F26 M2a) produces. Identical result: ``_ext_from_columnar``
    grows each row by exactly the extent :func:`repetition_extent` would, and a
    union of per-key min/max equals a union of every per-rect box."""
    boxes: list = []
    for coords, rt, rr in rcol.values():
        if coords.shape[0]:
            ext = _ext_from_columnar(coords.astype(np.float64), rt, rr)
            boxes.append((ext[:, 0].min(), ext[:, 1].min(),
                          ext[:, 2].max(), ext[:, 3].max()))
    for specs in poly_specs.values():
        for base, rt, rr in specs:
            ex0, ey0, ex1, ey1 = oas.repetition_extent(rt, rr)
            boxes.append((base[:, 0].min() + ex0, base[:, 1].min() + ey0,
                          base[:, 0].max() + ex1, base[:, 1].max() + ey1))
    return _union_bbox(boxes)


class RandomAccessReader:
    """Seek-and-decode a single cell at a time, memoized.

    ``wanted_layers`` restricts which geometry is *kept* (other layers
    are still decoded to keep the stream in sync, but their bulk data is
    dropped) — same semantics as ``OasisReader``."""

    def __init__(self, path: str | Path,
                 wanted_layers: Optional[set[LayerKey]] = None,
                 *, dtype=np.int32,
                 bbox_layer: Optional[LayerKey] = None,
                 prebuilt_index: Optional[dict] = None) -> None:
        self._path = Path(path)
        self._dtype = dtype
        # A per-cell boundary layer (e.g. CE 108/250): one rectangle whose
        # extent equals the cell's own-geometry bbox. When present we can
        # compute reachable_bbox by decoding only up to that rectangle
        # (placements come before it in the stream) and skip the cell's bulk
        # device geometry — turning the prune pass from "decode the whole
        # file" into "~50 records per cell" (F2 M3.5e.3). It MUST survive the
        # layer filter, so it is unioned into wanted_layers below.
        self._bbox_layer = bbox_layer
        if wanted_layers is not None and bbox_layer is not None:
            wanted_layers = set(wanted_layers) | {bbox_layer}
        # Post-union wanted set, kept so clone() can build an independent
        # reader with the exact same filter (F6 M3 thread-pool batch).
        self._init_wanted = (set(wanted_layers)
                             if wanted_layers is not None else None)
        # F6 M1/M2: the random-access path only touches a few cells, so map
        # the file read-only instead of slurping it whole — a 345 MB layout no
        # longer costs 345 MB of RAM. mmap falls back to slurp transparently
        # when unavailable (see OasisStream). M2: map the file exactly ONCE
        # and share that buffer between the offset-scan pass and the persistent
        # geometry reader (each gets its own cursor), instead of mapping twice.
        self._owned_stream = oas.OasisStream(open(path, "rb"), use_mmap=True)
        shared = self._owned_stream._buf
        self._reader = oas.OasisReader(
            path, wanted_layers=wanted_layers,
            defer_repetition=True, shared_buf=shared)
        # F23 M1: the name-table scan is the dominant per-reader build cost on
        # big production files. When a caller already holds the index (the GUI's
        # main reader does), it can hand it in via ``prebuilt_index`` so this
        # reader skips the rescan. The dict is exactly what scan_cell_offsets
        # returns (all plain dict/list/int -> picklable across the spawn pool);
        # the mmap'd ``_reader`` above is still built normally since the worker
        # needs it to decode geometry. The injected index must match this file
        # (caller's responsibility — the batch pool keys on the file path).
        idx = (prebuilt_index if prebuilt_index is not None
               else oas.scan_cell_offsets(path, shared_buf=shared))
        # Kept so this reader can in turn hand its index to another (index_snapshot).
        self._idx = idx
        self._by_refnum: dict[int, int] = idx["by_refnum"]
        self._by_name: dict[str, int] = idx["by_name"]
        # OASIS START `unit` = grid steps per micron. Raw coordinates are in
        # grid steps; 1 grid = 1000/unit nm. The decoder returns raw grid
        # coords, so geometry must be scaled by this to reach nm (the frame
        # the FOV box / KLARF / RFL all use). unit==1000 -> 1.0 (no-op).
        self._unit = idx.get("unit")
        self._layernames = idx.get("layernames") or []
        # Index provenance, kept for diagnostics (F12): how / whether the
        # S_CELL_OFFSET + LAYERNAME tables were located (inline vs strict-mode
        # end tables) — surfaced by enumerate_layers and the Diagnose report.
        self._offset_flag = idx.get("offset_flag")
        self._offsets_via = idx.get("offsets_via")
        self._table_offsets = idx.get("table_offsets")
        # S_BOUNDING_BOX (F16): a per-cell *complete* bbox read straight from
        # the name table lets the ROI prune skip decoding geometry — the fast
        # path for big files lacking the CE 108/250 boundary layer. The maps
        # (cid-local grid bbox, keyed like the offset maps) feed reachable_bbox;
        # _n_bbox_props / _bbox_sample stay for the Diagnose report.
        self._n_bbox_props = idx.get("n_bbox_props") or 0
        self._bbox_sample = idx.get("bbox_sample") or []
        self._sbbox_by_refnum: dict[int, Bbox] = idx.get("bbox_by_refnum") or {}
        self._sbbox_by_name: dict[str, Bbox] = idx.get("bbox_by_name") or {}
        self._nm_per_grid = (1000.0 / self._unit) if self._unit else 1.0
        _dbg(f"OASIS unit (grid steps per micron) = {self._unit!r} "
             f"-> 1 grid = {self._nm_per_grid} nm "
             f"(geometry scaled by this to nm)")
        self._memo: dict[object, CellContent] = {}
        self._bbox_memo: dict[object, CellContent] = {}
        # cid -> reachable bbox (cid-local frame), reused across walk_roi
        # calls; see walk_roi. cid not in the map = not yet computed.
        self._reach_memo: dict[object, Optional[Bbox]] = {}
        # (cell_id, offset, message) for any cell whose offset didn't land
        # on a CELL record or whose decode desynced. ROI load stays alive
        # and reports these instead of crashing.
        self.errors: list[tuple] = []
        self._n_loaded = 0
        self._n_cache_hits = 0   # cells served from the on-disk decode cache
        _dbg(f"RandomAccessReader: {len(self._by_refnum):,} offsets indexed "
             f"from {self._path.name} (wanted={wanted_layers} "
             f"bbox_layer={bbox_layer})")

    def clone(self) -> "RandomAccessReader":
        """An independent reader over the same file/filter (F6 M3).

        Used by the thread-pool batch fine-align so each worker thread owns a
        private reader (private ``_memo`` / cursor) with no shared mutable
        state — results are therefore identical to the sequential path. Each
        clone maps the file read-only; the OS shares the physical pages across
        clones, so N readers do not cost N× the RAM."""
        return RandomAccessReader(
            self._path, wanted_layers=self._init_wanted,
            dtype=self._dtype, bbox_layer=self._bbox_layer,
            prebuilt_index=self._idx)

    def index_snapshot(self) -> dict:
        """The name-table index (F23 M1), shaped like ``scan_cell_offsets``'s
        return. Hand it to another ``RandomAccessReader`` over the *same file*
        via ``prebuilt_index=`` so it skips the rescan. All values are plain
        dict/list/tuple/int, so this is picklable across the spawn pool."""
        return self._idx

    def close(self) -> None:
        """Release the file map (F6 M2). Drops the shared-buffer wrappers
        first, then closes the single owned mmap so no reference still points
        into a closed mapping. Safe to call more than once."""
        try:
            self._reader.close()
        except Exception:
            pass
        try:
            self._owned_stream.close()
        except Exception:
            pass

    def __enter__(self) -> "RandomAccessReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def has_offsets(self) -> bool:
        return bool(self._by_refnum)

    def layer_display_name(self, layer: int, datatype: int) -> str:
        """OASIS LAYERNAME for ``(layer, datatype)``, or "" (F3 M2)."""
        return resolve_layer_name(self._layernames, layer, datatype)

    def offset_for(self, cell_id: object) -> Optional[int]:
        """Byte offset of ``cell_id``'s CELL record, or None if unknown.
        ``cell_id`` may be a cellname refnum (int) or an inline name
        (str / bytes)."""
        if isinstance(cell_id, int):
            return self._by_refnum.get(cell_id)
        if isinstance(cell_id, bytes):
            cell_id = cell_id.decode("ascii", "replace")
        if isinstance(cell_id, str):
            return self._by_name.get(cell_id)
        return None

    def sbbox_for(self, cell_id: object) -> Optional[Bbox]:
        """S_BOUNDING_BOX *complete* bbox (cid-local grid frame) for
        ``cell_id``, or None if the file didn't carry one for this cell (F16).
        Resolves refnum / name like :meth:`offset_for`. When present this is
        the cell's full reachable extent (own + placements), so the ROI walk
        uses it directly and skips decoding the subtree."""
        if isinstance(cell_id, int):
            return self._sbbox_by_refnum.get(cell_id)
        if isinstance(cell_id, bytes):
            cell_id = cell_id.decode("ascii", "replace")
        if isinstance(cell_id, str):
            return self._sbbox_by_name.get(cell_id)
        return None

    def load_cell(self, cell_id: object) -> CellContent:
        """Decode just the cell ``cell_id`` (memoized). Returns an empty
        :class:`CellContent` if the cell has no known offset."""
        if cell_id in self._memo:
            return self._memo[cell_id]
        # F16-B: a big flat merge cell costs minutes to decode; reuse a sidecar
        # from a previous session if the source file is unchanged.
        cached = cellcache.load(self._path, cell_id, self._init_wanted)
        if cached is not None:
            self._memo[cell_id] = cached
            self._n_loaded += 1
            self._n_cache_hits += 1
            _trace(f"load_cell {cell_id!r}: served from on-disk cache")
            return cached
        offset = self.offset_for(cell_id)
        if offset is None:
            _dbg(f"load_cell {cell_id!r}: no offset (unknown cell)")
            content = CellContent()
            self._memo[cell_id] = content
            return content

        # Guard: the offset must land on a CELL record (id 13/14). If it
        # doesn't, the S_CELL_OFFSET table is unusable for this cell —
        # report it rather than desyncing the decoder into garbage.
        buf = self._reader._f._buf
        first = buf[offset] if 0 <= offset < len(buf) else -1
        if first not in (oas.CELL_REFNUM, oas.CELL_NAME):
            msg = (f"offset {offset} is record id {first}, not CELL "
                   f"(13/14); {_hexdump(buf, offset)}")
            _dbg(f"load_cell {cell_id!r}: BAD OFFSET — {msg}")
            self.errors.append((cell_id, offset, msg))
            content = CellContent()
            self._memo[cell_id] = content
            return content

        try:
            content = self._decode_at(offset)
        except oas.OasisFormatError as exc:
            cur = self._reader._f.tell()
            msg = f"decode desync near byte {cur}: {exc}"
            _dbg(f"load_cell {cell_id!r} @ {offset}: DECODE ERROR — {msg}; "
                 f"{_hexdump(buf, min(cur, len(buf) - 1))}")
            self.errors.append((cell_id, offset, msg))
            content = CellContent()
        else:
            # Throttle: a per-cell line per call floods the console (and the
            # per-line flush is slow on Windows). Print a heartbeat every
            # 500 cells instead; errors above are always shown.
            self._n_loaded += 1
            if TRACE and self._n_loaded % 500 == 0:
                _trace(f"… {self._n_loaded:,} cells decoded so far "
                       f"(last {cell_id!r} @ {offset})")
            # F16-B: persist big cells so the next session loads them in seconds.
            nrec = (content.placement_count() + content.total_rects()
                    + content.total_polys())
            if nrec >= cellcache.min_records():
                if cellcache.save(self._path, cell_id, self._init_wanted, content):
                    _dbg(f"cached decoded cell {cell_id!r} "
                         f"({nrec:,} records) → sidecar (next session loads fast)")
        self._memo[cell_id] = content
        return content

    def load_cell_bbox(self, cell_id: object) -> CellContent:
        """Lightweight load for the ``reachable_bbox`` prune pass: when a
        boundary layer is configured, decode only up to that cell's boundary
        rectangle — collecting its PLACEMENT children and own-geometry bbox —
        then stop, skipping the cell's bulk device geometry (often >200K
        records). Memoized separately from :meth:`load_cell`.

        Correctness relies on PLACEMENT records preceding the boundary
        rectangle in the stream (verified for Calibre D2DB: every sampled
        geometry cell has ``last_placement_index < boundary_rect_index``). A
        cell with no boundary rectangle (a pure placement container) is
        decoded to its end — cheap, since containers carry no geometry. With
        no ``bbox_layer`` configured this falls back to the full load."""
        if self._bbox_layer is None:
            return self.load_cell(cell_id)
        if cell_id in self._bbox_memo:
            return self._bbox_memo[cell_id]
        offset = self.offset_for(cell_id)
        if offset is None:
            content = CellContent()
            self._bbox_memo[cell_id] = content
            return content
        buf = self._reader._f._buf
        first = buf[offset] if 0 <= offset < len(buf) else -1
        if first not in (oas.CELL_REFNUM, oas.CELL_NAME):
            msg = (f"offset {offset} is record id {first}, not CELL "
                   f"(13/14); {_hexdump(buf, offset)}")
            _dbg(f"load_cell_bbox {cell_id!r}: BAD OFFSET — {msg}")
            self.errors.append((cell_id, offset, msg))
            content = CellContent()
            self._bbox_memo[cell_id] = content
            return content
        try:
            content = self._decode_bbox_at(offset)
        except oas.OasisFormatError as exc:
            cur = self._reader._f.tell()
            msg = f"decode desync near byte {cur}: {exc}"
            _dbg(f"load_cell_bbox {cell_id!r} @ {offset}: DECODE ERROR — {msg}")
            self.errors.append((cell_id, offset, msg))
            content = CellContent()
        else:
            self._n_loaded += 1
            if DEBUG and self._n_loaded % 500 == 0:
                _dbg(f"… {self._n_loaded:,} cells scanned so far "
                     f"(last {cell_id!r} @ {offset})")
        self._bbox_memo[cell_id] = content
        return content

    # ── F11: read-only reachable-bbox accessor (whole-chip extent) ──────────
    #
    # Mirrors the reachable_bbox closure inside walk_roi (own + children over
    # repetition extent, memoized in self._reach_memo) but is a standalone,
    # geometry-read-only method so it never touches the walk / CE early-stop
    # hot path (CLAUDE.md §7). Used to size the whole-chip export tile grid.
    def reachable_bbox(self, cell_id: object, *, cancel_cb=None):
        """Bbox in the cell's local *grid* frame of all geometry reachable
        from ``cell_id`` (own + placed children over repetition extent), or
        ``None`` for an empty / cyclic cell. Shares the walk's ``_reach_memo``
        cache."""
        return self._reachable_bbox(cell_id, set(), cancel_cb)

    def _reachable_bbox(self, cid, computing, cancel_cb):
        if cid in self._reach_memo:
            return self._reach_memo[cid]
        sb = self.sbbox_for(cid)            # F16: name-table complete bbox
        if sb is not None:
            self._reach_memo[cid] = sb
            return sb
        if cid in computing:
            return None
        if cancel_cb is not None and cancel_cb():
            raise WalkCancelled()
        computing.add(cid)
        content = self.load_cell_bbox(cid)
        boxes: list = []
        if content.bbox is not None:
            boxes.append(content.bbox)
        for pl in content.placements:
            T = Transform.from_placement(pl.x, pl.y, pl.angle, pl.flip,
                                         pl.magnification)
            if T is None:
                continue
            cb = self._reachable_bbox(pl.target, computing, cancel_cb)
            if cb is None:
                continue
            placed = _xform_bbox(T, cb)
            ex0, ey0, ex1, ey1 = oas.repetition_extent(
                pl.repetition_type, pl.repetition_raw)
            boxes.append((placed[0] + ex0, placed[1] + ey0,
                          placed[2] + ex1, placed[3] + ey1))
        computing.discard(cid)
        res = _union_bbox(boxes)
        self._reach_memo[cid] = res
        return res

    def reachable_bbox_nm(self, cell_id: object, *, cancel_cb=None):
        """:meth:`reachable_bbox` scaled to nm (root coordinates), or
        ``None``. The whole-chip extent for ``root``."""
        b = self.reachable_bbox(cell_id, cancel_cb=cancel_cb)
        if b is None:
            return None
        s = getattr(self, "_nm_per_grid", 1.0) or 1.0
        return (b[0] * s, b[1] * s, b[2] * s, b[3] * s)

    # ── internal ────────────────────────────────────────────────────────────
    def _decode_bbox_at(self, offset: int) -> CellContent:
        """Decode a cell only far enough to know its placements + own bbox,
        stopping at the boundary-layer rectangle (see load_cell_bbox)."""
        reader = self._reader
        f = reader._f
        f.clear_substreams()
        f.seek(int(offset))
        bl = self._bbox_layer

        placements: list = []
        ce_spec = None
        run_boxes: list = []          # own bbox for the rare no-CE geometry cell
        seen_cell_header = False

        for rid, payload in reader.iter_records():
            if rid in (oas.CELL_REFNUM, oas.CELL_NAME):
                if seen_cell_header:
                    break
                seen_cell_header = True
                continue
            if rid == oas.END:
                break
            if rid in (oas.PLACEMENT_NOMAG, oas.PLACEMENT_MAG):
                placements.append(Placement(
                    target=payload["cell_ref"],
                    target_kind=payload["cell_ref_kind"],
                    x=payload["x"], y=payload["y"],
                    angle=float(payload["angle"]),
                    magnification=float(payload["magnification"]),
                    flip=bool(payload["flip"]),
                    repetition_type=payload.get("repetition_type"),
                    repetition_offsets=[],
                    repetition_raw=payload.get("repetition_raw"),
                ))
            elif rid == oas.RECTANGLE:
                if payload.get("filtered_out"):
                    continue
                key = (payload["layer"], payload["datatype"])
                x1 = payload["x"]; y1 = payload["y"]
                spec = (x1, y1, x1 + payload["width"], y1 + payload["height"],
                        payload.get("repetition_type"),
                        payload.get("repetition_raw"))
                if key == bl:
                    ce_spec = spec
                    break                  # got placements + own bbox -> stop
                ex0, ey0, ex1, ey1 = oas.repetition_extent(spec[4], spec[5])
                run_boxes.append((spec[0] + ex0, spec[1] + ey0,
                                  spec[2] + ex1, spec[3] + ey1))
            elif rid == oas.POLYGON:
                if payload.get("filtered_out"):
                    continue
                pts = payload.get("points") or []
                if not pts:
                    continue
                ax = payload["x"]; ay = payload["y"]
                base = np.asarray(pts, dtype=self._dtype)
                base[:, 0] += ax; base[:, 1] += ay
                ex0, ey0, ex1, ey1 = oas.repetition_extent(
                    payload.get("repetition_type"), payload.get("repetition_raw"))
                run_boxes.append((base[:, 0].min() + ex0, base[:, 1].min() + ey0,
                                  base[:, 0].max() + ex1, base[:, 1].max() + ey1))

        if ce_spec is not None:
            rect_specs = {bl: [ce_spec]}
            bbox = _analytic_bbox(rect_specs, {})
        else:
            rect_specs = {}
            bbox = _union_bbox(run_boxes)
        return CellContent(rect_specs=rect_specs, poly_specs={},
                           _placements=placements, bbox=bbox)

    def _decode_at(self, offset: int) -> CellContent:
        if _FAST is not None:
            return self._decode_at_native(offset)
        return self._decode_at_py(offset)

    def _decode_at_py(self, offset: int) -> CellContent:
        reader = self._reader
        f = reader._f
        f.clear_substreams()
        f.seek(int(offset))

        rect_specs: dict[LayerKey, list] = {}
        poly_specs: dict[LayerKey, list] = {}
        placements: list = []
        seen_cell_header = False

        # Hot loop over a flat cell can see millions of records, so keep it
        # lean (F16-B M1): cache the last (layer, datatype) -> list so the
        # common run of same-layer geometry skips dict.setdefault (which would
        # allocate a throwaway [] on every call), and build the immutable
        # Placement positionally.
        _rk = _pk = None
        _rlist = _plist = None
        for rid, payload in reader.iter_records():
            if rid == oas.RECTANGLE:
                if payload.get("filtered_out"):
                    continue
                key = (payload["layer"], payload["datatype"])
                if key != _rk:
                    _rlist = rect_specs.get(key)
                    if _rlist is None:
                        _rlist = rect_specs[key] = []
                    _rk = key
                x1 = payload["x"]; y1 = payload["y"]
                # Store the base rect + its repetition descriptor; expanded
                # lazily (and vectorized) only if this cell lands in the ROI.
                _rlist.append((
                    x1, y1, x1 + payload["width"], y1 + payload["height"],
                    payload.get("repetition_type"), payload.get("repetition_raw")))
            elif rid == oas.POLYGON:
                if payload.get("filtered_out"):
                    continue
                pts = payload.get("points") or []
                if not pts:
                    continue
                pkey = (payload["layer"], payload["datatype"])
                if pkey != _pk:
                    _plist = poly_specs.get(pkey)
                    if _plist is None:
                        _plist = poly_specs[pkey] = []
                    _pk = pkey
                ax = payload["x"]; ay = payload["y"]
                base = np.asarray(pts, dtype=self._dtype)
                base[:, 0] += ax
                base[:, 1] += ay
                _plist.append((base, payload.get("repetition_type"),
                               payload.get("repetition_raw")))
            elif rid in (oas.PLACEMENT_NOMAG, oas.PLACEMENT_MAG):
                placements.append(Placement(
                    payload["cell_ref"], payload["cell_ref_kind"],
                    payload["x"], payload["y"], float(payload["angle"]),
                    float(payload["magnification"]), bool(payload["flip"]),
                    payload.get("repetition_type"), [],
                    payload.get("repetition_raw")))
            elif rid in (oas.CELL_REFNUM, oas.CELL_NAME):
                if seen_cell_header:
                    break                  # next cell -> our cell is done
                seen_cell_header = True
            elif rid == oas.END:
                break

        return CellContent(rect_specs=rect_specs, poly_specs=poly_specs,
                           _placements=placements,
                           bbox=_analytic_bbox(rect_specs, poly_specs))

    def _decode_at_native(self, offset: int) -> CellContent:
        """F26 M2a-integrate: native-accelerated twin of :meth:`_decode_at_py`.

        Same generator drive (so PLACEMENT §22.6 / POLYGON / CELL / CBLOCK
        handling stays in the proven pure-Python decoders), but every time a
        RECTANGLE is yielded we hand the cursor to ``decode_rect_run`` to gobble
        the *rest* of that same-(layer, datatype) run in one C call — bulk
        ``(N,4)`` array instead of a Python varint loop + per-rect tuple. Rects
        land in the columnar ``_rcol`` form (which every CellContent accessor
        already prefers); the result is byte-identical to ``_decode_at_py``
        (test_oasis_native_decode pins the two paths together)."""
        reader = self._reader
        modal = reader._modal
        run = _FAST.decode_rect_run
        filtered_out = reader._layer_filtered_out
        f = reader._f
        f.clear_substreams()
        f.seek(int(offset))

        rcol_blocks: dict[LayerKey, list] = {}   # key -> [(coords, rt, rr), …]
        poly_specs: dict[LayerKey, list] = {}
        placements: list = []
        seen_cell_header = False
        _pk = None
        _plist = None

        def _gobble():
            # Decode the run of same-(layer,dt) rects after the just-yielded
            # one; write the advanced modal + cursor back so the generator
            # resumes correctly. Returns the (N,4) int64 array (no repetition —
            # decode_rect_run rewinds before a repeated/foreign record).
            # started=1: the modal layer IS this run's layer, so a first rect on
            # a different layer stops immediately — the gobbled rects are all on
            # (modal.layer, modal.datatype), never the next run's layer.
            new_pos, rects, lay, dtp, w, h, gx, gy, xyrel, _stop = run(
                f._buf, f._pos, modal.layer, modal.datatype,
                modal.geometry_w, modal.geometry_h,
                modal.geometry_x, modal.geometry_y,
                1 if modal.xy_relative else 0, 1)
            modal.layer = lay; modal.datatype = dtp
            modal.geometry_w = w; modal.geometry_h = h
            modal.geometry_x = gx; modal.geometry_y = gy
            modal.xy_relative = bool(xyrel)
            f._pos = new_pos
            return rects

        for rid, payload in reader.iter_records():
            if rid == oas.RECTANGLE:
                if payload.get("filtered_out"):
                    # Still gobble (at C speed) so the cursor skips the whole
                    # filtered run without a per-rect Python loop; drop output.
                    _gobble()
                    continue
                key = (payload["layer"], payload["datatype"])
                blk = rcol_blocks.get(key)
                if blk is None:
                    blk = rcol_blocks[key] = []
                x1 = payload["x"]; y1 = payload["y"]
                rt0 = payload.get("repetition_type")
                # The generator already decoded this first rect (it may carry a
                # repetition); store it as a 1-row block in stream order.
                blk.append((
                    np.array([[x1, y1, x1 + payload["width"],
                               y1 + payload["height"]]], dtype=np.int64),
                    np.array([-1 if rt0 is None else rt0], dtype=np.int16),
                    _obj1(payload.get("repetition_raw"))))
                rects = _gobble()           # the rest of this run, in C
                n = rects.shape[0]
                if n:
                    # native rects carry no repetition: rt all -1, rr all None.
                    # np.empty(object) is already None-initialized — no fill.
                    blk.append((np.asarray(rects, dtype=np.int64),
                                np.full(n, -1, dtype=np.int16),
                                np.empty(n, dtype=object)))
            elif rid == oas.POLYGON:
                if payload.get("filtered_out"):
                    continue
                pts = payload.get("points") or []
                if not pts:
                    continue
                pkey = (payload["layer"], payload["datatype"])
                if pkey != _pk:
                    _plist = poly_specs.get(pkey)
                    if _plist is None:
                        _plist = poly_specs[pkey] = []
                    _pk = pkey
                ax = payload["x"]; ay = payload["y"]
                base = np.asarray(pts, dtype=self._dtype)
                base[:, 0] += ax
                base[:, 1] += ay
                _plist.append((base, payload.get("repetition_type"),
                               payload.get("repetition_raw")))
            elif rid in (oas.PLACEMENT_NOMAG, oas.PLACEMENT_MAG):
                placements.append(Placement(
                    payload["cell_ref"], payload["cell_ref_kind"],
                    payload["x"], payload["y"], float(payload["angle"]),
                    float(payload["magnification"]), bool(payload["flip"]),
                    payload.get("repetition_type"), [],
                    payload.get("repetition_raw")))
            elif rid in (oas.CELL_REFNUM, oas.CELL_NAME):
                if seen_cell_header:
                    break                  # next cell -> our cell is done
                seen_cell_header = True
            elif rid == oas.END:
                break

        # Concatenate each key's stream-ordered blocks into one columnar entry.
        rcol: dict = {}
        for key, blocks in rcol_blocks.items():
            if len(blocks) == 1:
                rcol[key] = blocks[0]
            else:
                rcol[key] = (np.vstack([b[0] for b in blocks]),
                             np.concatenate([b[1] for b in blocks]),
                             np.concatenate([b[2] for b in blocks]))
        return CellContent(rect_specs={}, poly_specs=poly_specs,
                           _placements=placements, _rcol=rcol,
                           bbox=_analytic_bbox_columnar(rcol, poly_specs))


# ── M3.5c: top-down ROI walker ───────────────────────────────────────────────


@dataclass
class RoiWalkStats:
    cell_visits: int = 0
    instances_visited: int = 0
    instances_pruned: int = 0
    arbitrary_angle_skipped: int = 0
    cycles_skipped: int = 0
    unknown_target_skipped: int = 0
    rects_emitted: int = 0
    polys_emitted: int = 0
    # Diagnostics surfaced to the UI (F16): wall-clock of the walk, how many
    # cells were *fully decoded* (the real cost — should be small for a tight
    # FOV once pruning works), and whether the decode-free S_BOUNDING_BOX prune
    # was in effect. cells_decoded staying high on a tiny FOV => the prune isn't
    # biting (no S_BOUNDING_BOX and no per-cell CE boundary => bbox-by-decode).
    cells_decoded: int = 0
    cells_cached: int = 0      # cells served from the on-disk decode cache
    t_decode: float = 0.0      # wall-clock inside load_cell (decode + cache load)
    elapsed_s: float = 0.0
    sbbox_prune: bool = False
    # F16 perf-triage: arrays that survived the cheap whole-array prune and had
    # to materialize per-instance offsets, the total instances so materialized,
    # and the largest single array. instances_materialized ≫ instances_visited
    # means a giant regular grid is being expanded just to keep a handful (fix:
    # analytic sub-grid clip); instances_visited huge means recursion blow-up.
    arrays_materialized: int = 0
    instances_materialized: int = 0
    max_array_k: int = 0
    # Per-record scan counts (the cost is per-record numpy overhead, so these
    # reveal how many placements / geometry specs the walk iterates — large here
    # with small instances_materialized => the prune loop, not expansion, is hot).
    placements_scanned: int = 0
    rect_specs_scanned: int = 0
    poly_specs_scanned: int = 0
    # Wall-clock spent in each section of the walk (sums across all visits) —
    # tells us which loop dominates a slow ROI load.
    t_place: float = 0.0
    t_rect: float = 0.0
    t_poly: float = 0.0


def _xform_bbox(T: Transform, bbox: Bbox) -> np.ndarray:
    """Transform a single bbox (x0,y0,x1,y1) and return the new
    axis-aligned bbox as a length-4 float array."""
    arr = np.array([[bbox[0], bbox[1], bbox[2], bbox[3]]], dtype=np.float64)
    return T.apply_to_rects(arr)[0]


def _union_bbox(boxes: list) -> Optional[Bbox]:
    if not boxes:
        return None
    a = np.asarray(boxes, dtype=np.float64)
    return (float(a[:, 0].min()), float(a[:, 1].min()),
            float(a[:, 2].max()), float(a[:, 3].max()))


def _roi_overlap_mask(boxes: np.ndarray, roi: Bbox) -> np.ndarray:
    """Boolean mask over (N,4) boxes that overlap ``roi`` (x0,y0,x1,y1)."""
    if boxes.shape[0] == 0:
        return np.zeros((0,), dtype=bool)
    # F27 M1: native C loop for the hot float64 case (byte-identical), removing
    # the 5 vectorized numpy ops' per-call dispatch the walk pays every visit.
    if (_FASTW is not None and boxes.dtype == np.float64
            and boxes.flags["C_CONTIGUOUS"]):
        return _FASTW.roi_overlap_mask(boxes, roi[0], roi[1], roi[2], roi[3])
    bx1 = np.minimum(boxes[:, 0], boxes[:, 2])
    by1 = np.minimum(boxes[:, 1], boxes[:, 3])
    bx2 = np.maximum(boxes[:, 0], boxes[:, 2])
    by2 = np.maximum(boxes[:, 1], boxes[:, 3])
    return (bx1 <= roi[2]) & (bx2 >= roi[0]) & (by1 <= roi[3]) & (by2 >= roi[1])


# Quarter-turn rotation matrices (m00, m01, m10, m11), CCW — matches
# Transform.from_placement. Used to build placement transforms in bulk without
# allocating a Transform/ndarray per placement record.
# Placement target_kind <-> int8 code, for the compact SoA cache (F18).
_KIND_NAMES = ("refnum", "name", "modal")
_KIND_CODES = {"refnum": 0, "name": 1, "modal": 2}


_D4_ROT = ((1.0, 0.0, 0.0, 1.0), (0.0, -1.0, 1.0, 0.0),
           (-1.0, 0.0, 0.0, -1.0), (0.0, 1.0, -1.0, 0.0))


def _ext_from_columnar(base: np.ndarray, rt: np.ndarray, rr) -> np.ndarray:
    """``ext`` = ``base`` grown by each rect/poly's repetition extent, vectorized
    (F16-B "B"). Regular types (1/2/3/8/9) are computed with grouped numpy ops;
    arbitrary-list types (10/11, and any rare 4-7) fall back to a per-spec
    :func:`repetition_extent`. Equivalent to looping ``repetition_extent`` over
    every ``rt>0`` row — but ~6x+ faster on millions of regular CMG arrays
    (whose per-call branching + tuple build dominated the per-session first ROI).
    ``rt`` uses -1 for no repetition; ``rr`` is the per-row object array."""
    ext = base.copy()
    rep = np.flatnonzero(rt > 0)
    if rep.size == 0:
        return ext
    rtv = rt[rep]

    def _grow(idx, dx, dy):
        z = np.zeros(idx.size, dtype=np.float64)
        ext[idx, 0] += np.minimum(z, dx); ext[idx, 1] += np.minimum(z, dy)
        ext[idx, 2] += np.maximum(z, dx); ext[idx, 3] += np.maximum(z, dy)

    def _flat(idx):                       # (k, n) int array of flat tuples
        return np.array(rr[idx].tolist(), dtype=np.float64)

    s = rep[rtv == 1]                     # rr=(nx, ny, xs, ys)
    if s.size:
        p = _flat(s)
        _grow(s, (p[:, 0] - 1) * p[:, 2], (p[:, 1] - 1) * p[:, 3])
    s = rep[rtv == 2]                     # rr=(nx, xs)
    if s.size:
        p = _flat(s)
        _grow(s, (p[:, 0] - 1) * p[:, 1], np.zeros(s.size))
    s = rep[rtv == 3]                     # rr=(ny, ys)
    if s.size:
        p = _flat(s)
        _grow(s, np.zeros(s.size), (p[:, 0] - 1) * p[:, 1])
    s = rep[rtv == 9]                     # rr=(nd, (dx, dy))
    if s.size:
        nd = np.fromiter((rr[i][0] for i in s), np.float64, s.size)
        dv = np.array([rr[i][1] for i in s], dtype=np.float64)
        _grow(s, (nd - 1) * dv[:, 0], (nd - 1) * dv[:, 1])
    s = rep[rtv == 8]                     # rr=(nn, mm, (nvx,nvy), (mvx,mvy))
    if s.size:
        nn = np.fromiter((rr[i][0] for i in s), np.float64, s.size)
        mm = np.fromiter((rr[i][1] for i in s), np.float64, s.size)
        nvec = np.array([rr[i][2] for i in s], dtype=np.float64)
        mvec = np.array([rr[i][3] for i in s], dtype=np.float64)
        ax = (nn - 1) * nvec[:, 0]; ay = (nn - 1) * nvec[:, 1]
        bx = (mm - 1) * mvec[:, 0]; by = (mm - 1) * mvec[:, 1]
        cx = np.stack([np.zeros_like(ax), ax, bx, ax + bx], axis=1)
        cy = np.stack([np.zeros_like(ay), ay, by, ay + by], axis=1)
        ext[s, 0] += cx.min(1); ext[s, 1] += cy.min(1)
        ext[s, 2] += cx.max(1); ext[s, 3] += cy.max(1)
    handled = ((rtv == 1) | (rtv == 2) | (rtv == 3) | (rtv == 8) | (rtv == 9))
    for i in rep[~handled]:               # 10/11 arbitrary lists, rare 4-7
        e0, e1, e2, e3 = oas.repetition_extent(int(rt[i]), rr[i])
        ext[i, 0] += e0; ext[i, 1] += e1; ext[i, 2] += e2; ext[i, 3] += e3
    return ext


def _roi_to_local(T: "Transform", roi: Bbox) -> Bbox:
    """Map ``roi`` (root coords) into ``T``'s local frame as an axis-aligned
    bbox. For D4 transforms (M entries in {-1,0,1}·mag) this is exact."""
    Minv = np.linalg.inv(T.M)
    cs = np.array([[roi[0], roi[1]], [roi[2], roi[1]],
                   [roi[2], roi[3]], [roi[0], roi[3]]], dtype=np.float64)
    loc = (cs - T.t) @ Minv.T
    return (float(loc[:, 0].min()), float(loc[:, 1].min()),
            float(loc[:, 0].max()), float(loc[:, 1].max()))


def _axis_index_range(n: int, step: float, lo_val: float, hi_val: float):
    """Inclusive ``(i0, i1)`` grid indices in ``[0, n)`` whose instance
    ``i*step`` can satisfy ``lo_val <= i*step <= hi_val``, padded by one
    index each side for rounding safety. ``i0 > i1`` means empty."""
    if n <= 1 or step == 0:
        return (0, n - 1) if (lo_val <= 0.0 <= hi_val) else (1, 0)
    a, b = lo_val / step, hi_val / step
    if step < 0:
        a, b = b, a
    i0 = max(0, int(np.floor(a)) - 1)
    i1 = min(n - 1, int(np.ceil(b)) + 1)
    return (i0, i1)


def _grid_axes(rtype, raw):
    """Decompose an analytically-clippable repetition into orthogonal axes
    ``[(count, vx, vy), ...]`` (each axis purely horizontal or vertical), or
    ``None`` for types that must be materialized in full (arbitrary lists 10/11,
    skew/oblique lattices). Covers the regular grids 1/2/3 and the common
    Manhattan case of type 8 (2-D lattice with axis-aligned vectors)."""
    if rtype == 1:
        nx, ny, xs, ys = raw
        return [(nx, xs, 0), (ny, 0, ys)]
    if rtype == 2:
        nx, xs = raw
        return [(nx, xs, 0)]
    if rtype == 3:
        ny, ys = raw
        return [(ny, 0, ys)]
    if rtype == 8:
        nn, mm, n_vec, m_vec = raw
        a, b = (nn, n_vec[0], n_vec[1]), (mm, m_vec[0], m_vec[1])
        horiz = [ax for ax in (a, b) if ax[1] != 0 and ax[2] == 0]
        vert = [ax for ax in (a, b) if ax[2] != 0 and ax[1] == 0]
        # Only a clean orthogonal grid (one horizontal axis, one vertical) lets
        # the two indices be clipped independently; anything else -> full.
        if len(horiz) == 1 and len(vert) == 1:
            return [horiz[0], vert[0]]
        return None
    return None


def _clip_grid_offsets(rtype, raw, placed: np.ndarray, T: "Transform",
                       roi: Bbox) -> np.ndarray:
    """Materialize *only* the repetition offsets whose placed instance (a box
    ``placed`` in the array's local frame) can overlap ``roi`` — turning a
    chip-spanning million-instance array straddling a tiny FOV into a handful of
    candidates without building the full grid. The caller still applies the
    exact root-space ROI mask, so the survivor set is identical to materializing
    the whole array; the analytic clip (padded one index per side) only ever
    returns a *superset* of the true survivors. Non-grid / arbitrary-list types
    fall back to the full materialization."""
    axes = _grid_axes(rtype, raw)
    if axes is None:
        return oas.repetition_offsets_np(rtype, raw)
    rl = _roi_to_local(T, roi)
    px0, px1 = min(placed[0], placed[2]), max(placed[0], placed[2])
    py0, py1 = min(placed[1], placed[3]), max(placed[1], placed[3])
    grids = []
    for (n, vx, vy) in axes:
        if vx != 0:        # horizontal axis -> constrain by the ROI x-span
            i0, i1 = _axis_index_range(n, vx, rl[0] - px1, rl[2] - px0)
        elif vy != 0:      # vertical axis -> constrain by the ROI y-span
            i0, i1 = _axis_index_range(n, vy, rl[1] - py1, rl[3] - py0)
        else:              # zero vector: n coincident copies
            i0, i1 = 0, n - 1
        if i0 > i1:
            return np.empty((0, 2), dtype=np.float64)
        idx = np.arange(i0, i1 + 1, dtype=np.float64)
        grids.append(np.column_stack((idx * vx, idx * vy)))      # (k, 2)
    out = grids[0]
    for g in grids[1:]:                                          # cartesian sum
        out = (out[:, None, :] + g[None, :, :]).reshape(-1, 2)
    return out


# ── F27 M3: native subtree walk (flatten cell graph → walk_rects_native) ──────


def flatten_cell_graph(rar: "RandomAccessReader", root: object,
                       layer: int, datatype: int):
    """Flatten the ROI-independent cell graph reachable from ``root`` into CSR
    arrays for :func:`oasis_fastdecode.walk_rects_native` (F27 M3).

    Returns ``None`` — caller falls back to the pure-Python walk — when the
    subtree is NOT native-able: a POLYGON on ``(layer, datatype)``, any rect or
    placement repetition, a non-D4 placement, or a name-ref (non-int) target.
    Those cases keep the (byte-identical) Python walk; the native kernel only
    ever runs on the plain rect / no-rep / single-layer / D4 case.

    Also returns ``None`` (Python fallback) when the graph is too large to
    flatten cheaply — flattening materializes the *whole* reachable graph's
    geometry (ROI-independent), which on a 13k-cell production chip is a
    multi-second whole-chip decode + array build; the batch's Python walk (with
    its per-cell decode cache) is the right path there until a shared/persisted
    flatten (M3c) exists. Detection short-circuits (``return None``) the instant
    it sees a non-native-able record OR crosses a size cap — no wasted work.

    Layout (all grid-frame, cell-local): ``(rect_coords (ΣNr,4) f64,
    rect_off (N+1) i64, pl_target (ΣNp) i64, pl_M (ΣNp,4) f64, pl_t (ΣNp,2) f64,
    pl_off (N+1) i64, reach_bbox (N,4) f64, root_index)``."""
    key = (layer, datatype)
    # Cheap pre-check (no decode): a chip with more indexed cells than the cap
    # is never worth flattening — bail before touching any geometry so a big
    # file drops straight to the Python walk with no stall.
    if len(getattr(rar, "_by_refnum", ()) or ()) > _NATIVE_WALK_MAX_CELLS:
        return None
    order: list = []
    idx: dict = {}
    total_rects = 0
    # Iterative DFS (no recursion-limit blow-up on deep hierarchies). Each cap
    # crossing / non-native record returns None immediately so a big or
    # poly/rep graph bails after decoding at most _NATIVE_WALK_MAX_CELLS cells.
    stack = [root]
    while stack:
        cid = stack.pop()
        if cid in idx:
            continue
        idx[cid] = len(order)
        order.append(cid)
        if len(order) > _NATIVE_WALK_MAX_CELLS:
            return None
        c = rar.load_cell(cid)
        if c.poly_count(key):
            return None                                  # polygon
        nr = c.rect_count(key)
        total_rects += nr
        if total_rects > _NATIVE_WALK_MAX_RECTS:
            return None
        for i in range(nr):
            if c.rect_spec_at(key, i)[4] is not None:    # rect repetition
                return None
        for pl in c.placements:
            if pl.repetition_type is not None:
                return None                              # placement repetition
            if not isinstance(pl.target, (int, np.integer)):
                return None                              # name-ref target
            if Transform.from_placement(pl.x, pl.y, pl.angle, pl.flip,
                                        pl.magnification) is None:
                return None                              # non-D4
            stack.append(pl.target)

    N = len(order)
    rect_parts: list = []
    rect_off = np.empty(N + 1, dtype=np.int64); rect_off[0] = 0
    pl_target: list = []
    pl_M: list = []
    pl_t: list = []
    pl_off = np.empty(N + 1, dtype=np.int64); pl_off[0] = 0
    reach = np.zeros((N, 4), dtype=np.float64)
    for i, cid in enumerate(order):
        c = rar.load_cell(cid)
        nr = c.rect_count(key)
        rr = (c.rects(key).astype(np.float64) if nr
              else np.empty((0, 4), dtype=np.float64))
        rect_parts.append(rr)
        rect_off[i + 1] = rect_off[i] + rr.shape[0]
        for pl in c.placements:
            tf = Transform.from_placement(pl.x, pl.y, pl.angle, pl.flip,
                                          pl.magnification)
            pl_target.append(idx[pl.target])
            pl_M.append((tf.M[0, 0], tf.M[0, 1], tf.M[1, 0], tf.M[1, 1]))
            pl_t.append((tf.t[0], tf.t[1]))
        pl_off[i + 1] = len(pl_target)
        rb = rar.reachable_bbox(cid)
        if rb is not None:
            reach[i] = rb
    rect_coords = (np.ascontiguousarray(np.vstack(rect_parts))
                   if rect_parts else np.empty((0, 4), dtype=np.float64))
    pt = np.array(pl_target, dtype=np.int64)
    pM = (np.ascontiguousarray(np.array(pl_M, dtype=np.float64).reshape(-1, 4))
          if pl_M else np.empty((0, 4), dtype=np.float64))
    pT = (np.ascontiguousarray(np.array(pl_t, dtype=np.float64).reshape(-1, 2))
          if pl_t else np.empty((0, 2), dtype=np.float64))
    return (rect_coords, rect_off, pt, pM, pT, pl_off,
            np.ascontiguousarray(reach), idx[root])


def _flatten_cached(rar: "RandomAccessReader", root: object,
                    layer: int, datatype: int):
    """Memoized :func:`flatten_cell_graph` — the flatten is ROI-independent, so
    one build per (root, layer, datatype) is reused across every defect's ROI
    (and cached ``None`` means 'not native-able, always use Python')."""
    cache = getattr(rar, "_flat_cache", None)
    if cache is None:
        cache = rar._flat_cache = {}
    k = (root, layer, datatype)
    if k not in cache:
        cache[k] = flatten_cell_graph(rar, root, layer, datatype)
    return cache[k]


def walk_roi(rar: "RandomAccessReader", root_id: object, roi_bbox: Bbox,
             layer: int, datatype: int, *, max_depth: int = 128,
             cancel_cb=None) -> dict:
    """Collect, in root coordinates, all geometry on ``(layer, datatype)``
    that overlaps ``roi_bbox`` — descending the PLACEMENT hierarchy from
    ``root_id`` but pruning every subtree / repetition instance whose
    placed bbox misses the ROI (M3.5c).

    ``cancel_cb`` (optional) is polled periodically; if it returns True a
    :class:`WalkCancelled` is raised so a background worker can abort.

    Returns ``{"rects": ndarray(N,4), "polys": list[ndarray], "stats":
    RoiWalkStats}``."""
    def _check_cancel():
        if cancel_cb is not None and cancel_cb():
            raise WalkCancelled()
    key = (layer, datatype)
    # The decoder works in raw grid units; the ROI comes in nm. Convert the
    # ROI to grid for the (grid-native) walk, then scale emitted geometry
    # back to nm on the way out. nm_per_grid==1.0 makes this a no-op.
    scale = getattr(rar, "_nm_per_grid", 1.0) or 1.0
    roi = (float(roi_bbox[0]) / scale, float(roi_bbox[1]) / scale,
           float(roi_bbox[2]) / scale, float(roi_bbox[3]) / scale)
    _trace(f"walk_roi root={root_id!r} layer={layer}/{datatype} "
         f"roi_nm={tuple(roi_bbox)} scale={scale} roi_grid={roi}")
    _t0 = time.perf_counter()
    cells_at_start = rar._n_loaded
    hits_at_start = rar._n_cache_hits
    stats = RoiWalkStats()
    rect_out: list = []
    poly_out: list = []
    # reachable_bbox(cid) is a cid-local quantity — independent of the ROI,
    # the target layer and the chosen image — so its result is cached on the
    # reader and reused across every walk_roi call (different layers, and
    # different images / ROIs). The first walk fills it (~one full hierarchy
    # sweep); subsequent walks skip the recursion entirely (M3.5e.3).
    reach_memo: dict[object, Optional[Bbox]] = rar._reach_memo
    computing: set = set()
    _feat = {"rtype": set(), "angle": set(), "flip": False,
             "mag": set(), "name_ref": False, "rect_rtype": set(),
             "poly_rtype": set(), "ce_viol": 0, "sbbox_viol": 0}
    _decode_prof = {"total": 0.0, "max": 0.0, "cell": None,
                    "np": 0, "nr": 0, "npl": 0}

    def reachable_bbox(cid: object) -> Optional[Bbox]:
        """Bbox (cid-local frame) of all geometry reachable from cid —
        own + children, over full repetition extent. Memoized; cycles
        return None."""
        if cid in reach_memo:
            return reach_memo[cid]
        # F16 fast path: a name-table S_BOUNDING_BOX is already the complete
        # (placement-inclusive) bbox, so use it directly — no geometry decode,
        # no child recursion. This is what makes the first ROI load fast on big
        # files without the CE 108/250 boundary layer.
        sb = rar.sbbox_for(cid)
        if sb is not None:
            reach_memo[cid] = sb
            return sb
        if cid in computing:
            return None
        _check_cancel()
        computing.add(cid)
        # Lightweight load: stops at the boundary rectangle (placements +
        # own bbox) when a bbox_layer is configured, else a full decode.
        content = rar.load_cell_bbox(cid)
        boxes: list = []
        if content.bbox is not None:
            boxes.append(content.bbox)
        for pl in content.placements:
            T = Transform.from_placement(pl.x, pl.y, pl.angle, pl.flip,
                                         pl.magnification)
            if T is None:
                continue
            cb = reachable_bbox(pl.target)
            if cb is None:
                continue
            placed = _xform_bbox(T, cb)
            # Repetition extent (analytic — never materialize the array).
            ex0, ey0, ex1, ey1 = oas.repetition_extent(
                pl.repetition_type, pl.repetition_raw)
            boxes.append((placed[0] + ex0, placed[1] + ey0,
                          placed[2] + ex1, placed[3] + ey1))
        computing.discard(cid)
        res = _union_bbox(boxes)
        reach_memo[cid] = res
        return res

    def walk(cid: object, T: Transform, visiting: set, depth: int) -> None:
        _check_cancel()
        _t_load = time.perf_counter()
        _fresh = cid not in rar._memo
        content = rar.load_cell(cid)
        if _fresh:
            _dt = time.perf_counter() - _t_load
            _decode_prof["total"] += _dt
            if _dt > _decode_prof["max"]:
                _decode_prof["max"] = _dt
                _decode_prof["cell"] = cid
                _decode_prof["np"] = content.placement_count()
                _decode_prof["nr"] = content.total_rects()
                _decode_prof["npl"] = content.total_polys()
        stats.cell_visits += 1
        if depth == 0:
            _trace(f"  root {cid!r} loaded in {time.perf_counter() - _t_load:.1f}s "
                 f"(placements={content.placement_count()}, "
                 f"rect_specs={content.total_rects()}, "
                 f"poly_specs={content.total_polys()})")
        # Debug-only consistency checks. When the cell has a name-table
        # S_BOUNDING_BOX (the prune path we actually use), validate THAT against
        # the decoded bbox and skip the CE check entirely — the CE check calls
        # load_cell_bbox, which on a giant flat cell decodes up to a deep
        # boundary rect (tens of seconds) for information we don't rely on.
        if DEBUG and content.bbox is not None:
            ob = content.bbox
            _sb = rar.sbbox_for(cid)
            if _sb is not None:
                if not (_sb[0] <= ob[0] and _sb[1] <= ob[1]
                        and _sb[2] >= ob[2] and _sb[3] >= ob[3]):
                    _feat["sbbox_viol"] += 1
                    if _feat["sbbox_viol"] <= 6:
                        _trace(f"  SBBOX-VIOLATION cell {cid!r}: own_bbox={ob} "
                             f"sbbox={_sb}")
            else:
                # No sbbox -> the CE boundary IS the prune path; validate it.
                _ce = rar.load_cell_bbox(cid)
                _ceb = _ce.bbox if _ce is not None else None
                inside = (_ceb is not None and _ceb[0] <= ob[0]
                          and _ceb[1] <= ob[1] and _ceb[2] >= ob[2]
                          and _ceb[3] >= ob[3])
                if not inside:
                    _feat["ce_viol"] += 1
                    if _feat["ce_viol"] <= 6:
                        _trace(f"  CE-VIOLATION cell {cid!r}: own_bbox={ob} "
                             f"ce_bbox={_ceb}")
        # Feature collection is only for the DEBUG dump; iterating millions of
        # placements per cell is pure waste otherwise (F16-B M7).
        if DEBUG:
            for _pl in content.placements:
                _feat["rtype"].add(_pl.repetition_type)
                _feat["angle"].add(_pl.angle)
                if _pl.flip:
                    _feat["flip"] = True
                if _pl.magnification != 1.0:
                    _feat["mag"].add(_pl.magnification)
                if _pl.target_kind == "name":
                    _feat["name_ref"] = True
            # RECTANGLE / POLYGON own repetition types — the geometry array
            # encoding (may differ from placement repetition; CMG arrays).
            _feat["rect_rtype"].update(content.all_rect_rtypes())
            _feat["poly_rtype"].update(content.all_poly_rtypes())
        # Emit this cell's own geometry (transformed) that hits the ROI.
        # Each RECTANGLE/POLYGON may carry its own repetition (CMG arrays that
        # explode to millions), so clip every spec's array to the ROI before
        # materializing — the same analytic sub-grid clip as the placement loop.
        # A cheap whole-array extent prune skips arrays that miss the ROI in O(1)
        # (the old content.rects()/polys() expanded *every* spec in full).
        if content.rect_count(key):
            # Whole-array extent prune over all rect specs at once (cached local
            # extent bboxes -> one apply_to_rects + mask); expand+clip only the
            # survivors and emit with one more transform.
            _ts = time.perf_counter()
            base_bb, ext_bb = content.rect_arrays(key)
            stats.rect_specs_scanned += base_bb.shape[0]
            keep = _roi_overlap_mask(T.apply_to_rects(ext_bb), roi)
            parts = []
            for i in np.flatnonzero(keep):
                x1, y1, x2, y2, rt, rr = content.rect_spec_at(key, int(i))
                oa = _clip_grid_offsets(rt, rr, base_bb[i], T, roi)
                if oa.shape[0] == 0:
                    continue
                stats.arrays_materialized += 1
                stats.instances_materialized += oa.shape[0]
                if oa.shape[0] > stats.max_array_k:
                    stats.max_array_k = oa.shape[0]
                a = np.empty((oa.shape[0], 4), dtype=np.float64)
                a[:, 0] = x1 + oa[:, 0]; a[:, 1] = y1 + oa[:, 1]
                a[:, 2] = x2 + oa[:, 0]; a[:, 3] = y2 + oa[:, 1]
                parts.append(a)
            if parts:
                allr = parts[0] if len(parts) == 1 else np.concatenate(parts)
                r = T.apply_to_rects(allr)
                m = _roi_overlap_mask(r, roi)
                if m.any():
                    rect_out.append(r[m])
                    stats.rects_emitted += int(m.sum())
            stats.t_rect += time.perf_counter() - _ts
        if content.poly_count(key):
            _ts = time.perf_counter()
            base_bb, ext_bb = content.poly_arrays(key)
            stats.poly_specs_scanned += base_bb.shape[0]
            keep = _roi_overlap_mask(T.apply_to_rects(ext_bb), roi)
            for i in np.flatnonzero(keep):
                base, rt, rr = content.poly_spec_at(key, int(i))
                oa = _clip_grid_offsets(rt, rr, base_bb[i], T, roi)
                if oa.shape[0]:
                    stats.arrays_materialized += 1
                    stats.instances_materialized += oa.shape[0]
                    if oa.shape[0] > stats.max_array_k:
                        stats.max_array_k = oa.shape[0]
                basef = base.astype(np.float64)
                for dx, dy in oa:
                    s = basef.copy()
                    s[:, 0] += dx; s[:, 1] += dy
                    tp = T.apply_to_points(s)
                    bb = np.array([[tp[:, 0].min(), tp[:, 1].min(),
                                    tp[:, 0].max(), tp[:, 1].max()]])
                    if _roi_overlap_mask(bb, roi)[0]:
                        poly_out.append(tp)
                        stats.polys_emitted += 1
            stats.t_poly += time.perf_counter() - _ts
        if depth >= max_depth:
            return
        N = content.placement_count()
        if depth == 0:
            _trace(f"  root own-geometry done at {time.perf_counter() - _t0:.1f}s "
                 f"(rects={stats.rects_emitted}, polys={stats.polys_emitted}); "
                 f"descending {N} placements…")
        if not N:
            return
        _ts_pl = time.perf_counter()
        # Vectorized whole-array prune over ALL placements of this cell. The
        # per-placement gather (matrices, child bboxes, repetition extents) is
        # cell-local and reachable_bbox-only — ROI/transform independent — so it
        # is built once and cached on the CellContent (F16-B M6). Subsequent ROIs
        # and layers reuse it; only the apply_to_rects + ROI mask below is redone.
        prep = content._place_prep
        _big = N >= cellcache.min_records()
        if prep is None and _big:
            # Reuse a persisted gather from a previous session/worker (it is ROI-
            # and layer-independent), so a batch worker skips the ~tens-of-seconds
            # build entirely (F16-B M7).
            prep = cellcache.load_prep(rar._path, cid)
            if prep is not None:
                content._place_prep = prep
        if prep is None:
            # The gather needs every placement, so materialise the list here
            # (a cache-loaded cell builds it once from SoA — only on a prep
            # miss; a prep hit below skips this entirely).
            placements = content.placements
            base_M = np.zeros((N, 2, 2), dtype=np.float64)
            base_t = np.zeros((N, 2), dtype=np.float64)
            cb_arr = np.zeros((N, 4), dtype=np.float64)
            ext = np.zeros((N, 4), dtype=np.float64)
            rcount = np.ones(N, dtype=np.int64)
            valid = np.zeros(N, dtype=bool)
            arb_skip = 0
            unk_skip = 0
            for i, pl in enumerate(placements):
                rt, rr = pl.repetition_type, pl.repetition_raw
                rc = oas.repetition_count(rt, rr)
                rcount[i] = rc
                a = pl.angle % 360.0
                q = int(round(a / 90.0))
                if abs(a - q * 90.0) > 0.01:    # non-quarter-turn -> skip
                    arb_skip += rc
                    continue
                cb = reachable_bbox(pl.target)
                if cb is None:
                    unk_skip += 1
                    continue
                m00, m01, m10, m11 = _D4_ROT[q % 4]
                if pl.flip:                     # mirror after rotation (col 1)
                    m01, m11 = -m01, -m11
                mag = pl.magnification
                if mag != 1.0:
                    m00 *= mag; m01 *= mag; m10 *= mag; m11 *= mag
                base_M[i, 0, 0] = m00; base_M[i, 0, 1] = m01
                base_M[i, 1, 0] = m10; base_M[i, 1, 1] = m11
                base_t[i, 0] = pl.x; base_t[i, 1] = pl.y
                cb_arr[i] = cb
                ex = oas.repetition_extent(rt, rr)
                ext[i, 0] = ex[0]; ext[i, 1] = ex[1]
                ext[i, 2] = ex[2]; ext[i, 3] = ex[3]
                valid[i] = True
            # placed bbox = base applied to child bbox (batched corner
            # transform), then extended by the repetition extent.
            corners = np.empty((N, 4, 2), dtype=np.float64)
            corners[:, 0, 0] = cb_arr[:, 0]; corners[:, 0, 1] = cb_arr[:, 1]
            corners[:, 1, 0] = cb_arr[:, 2]; corners[:, 1, 1] = cb_arr[:, 1]
            corners[:, 2, 0] = cb_arr[:, 2]; corners[:, 2, 1] = cb_arr[:, 3]
            corners[:, 3, 0] = cb_arr[:, 0]; corners[:, 3, 1] = cb_arr[:, 3]
            tc = np.einsum('nij,nkj->nki', base_M, corners) + base_t[:, None, :]
            px, py = tc[:, :, 0], tc[:, :, 1]
            placed_all = np.empty((N, 4), dtype=np.float64)
            placed_all[:, 0] = px.min(axis=1); placed_all[:, 1] = py.min(axis=1)
            placed_all[:, 2] = px.max(axis=1); placed_all[:, 3] = py.max(axis=1)
            arr_local = placed_all + ext
            prep = (base_M, base_t, placed_all, arr_local, rcount, valid,
                    arb_skip, unk_skip)
            content._place_prep = prep
            if _big:                          # persist for next session/worker
                cellcache.save_prep(rar._path, cid, prep)
        (base_M, base_t, placed_all, arr_local, rcount, valid,
         arb_skip, unk_skip) = prep
        stats.placements_scanned += N
        stats.arbitrary_angle_skipped += arb_skip
        stats.unknown_target_skipped += unk_skip
        keep = valid & _roi_overlap_mask(T.apply_to_rects(arr_local), roi)
        stats.instances_pruned += int(rcount[valid & ~keep].sum())
        stats.t_place += time.perf_counter() - _ts_pl   # gather+prune only
        for i in np.flatnonzero(keep):
            pl = content.placement_at(int(i))     # lazy single (no full build)
            rtype, rraw = pl.repetition_type, pl.repetition_raw
            full_k = int(rcount[i])
            if pl.target in visiting:
                stats.cycles_skipped += full_k
                continue
            placed = placed_all[i]
            base = Transform(M=base_M[i], t=base_t[i])
            # Materialize only the sub-grid whose instances can reach the ROI
            # (analytic clip for regular grids; full array for arbitrary lists),
            # then exact-mask in root coords.
            oa = _clip_grid_offsets(rtype, rraw, placed, T, roi)  # (K,2)
            K = oa.shape[0]
            stats.arrays_materialized += 1
            stats.instances_materialized += K
            if K > stats.max_array_k:
                stats.max_array_k = K
            if K == 0:
                stats.instances_pruned += full_k
                continue
            plb = np.empty((K, 4), dtype=np.float64)
            plb[:, 0] = placed[0] + oa[:, 0]; plb[:, 1] = placed[1] + oa[:, 1]
            plb[:, 2] = placed[2] + oa[:, 0]; plb[:, 3] = placed[3] + oa[:, 1]
            rootb = T.apply_to_rects(plb)                        # -> root coords
            mask = _roi_overlap_mask(rootb, roi)
            sel = np.flatnonzero(mask)
            stats.instances_pruned += full_k - len(sel)
            if len(sel) == 0:
                continue
            place_ts = base.t + oa                              # (K,2)
            composed_M = T.M @ base.M
            composed_ts = place_ts @ T.M.T + T.t                # (K,2)
            visiting.add(pl.target)
            for k in sel:
                stats.instances_visited += 1
                walk(pl.target, Transform(M=composed_M, t=composed_ts[k]),
                     visiting, depth + 1)
            visiting.discard(pl.target)

    walk(root_id, Transform.identity(), set(), 0)
    rects = (np.concatenate(rect_out)
             if rect_out else np.empty((0, 4), dtype=np.float64))
    if scale != 1.0:
        rects = rects * scale
        poly_out = [p * scale for p in poly_out]
    rects = np.rint(rects).astype(np.int64)
    poly_out = [np.rint(p).astype(np.int64) for p in poly_out]
    loaded = rar._n_loaded - cells_at_start
    cached = rar._n_cache_hits - hits_at_start
    fresh = loaded - cached
    stats.cells_decoded = fresh
    stats.cells_cached = cached
    stats.t_decode = _decode_prof["total"]
    stats.elapsed_s = time.perf_counter() - _t0
    stats.sbbox_prune = bool(rar._sbbox_by_refnum or rar._sbbox_by_name)
    # Running per-reader totals so an outer caller (e.g. the F26 export timer)
    # can read how much *this* image's walk actually traversed — cell visits and
    # repetition-instance visits — without threading the stats object out.
    rar._walk_cellvisits_total = (getattr(rar, "_walk_cellvisits_total", 0)
                                  + stats.cell_visits)
    rar._walk_visited_total = (getattr(rar, "_walk_visited_total", 0)
                               + stats.instances_visited)
    # Internal walk breakdown (F26 diagnosis): placement gather+prune vs rect
    # emit vs poly emit. The remainder (walk time minus these) is recursion /
    # transform overhead — which is what a native walk would remove.
    rar._walk_tplace_total = (getattr(rar, "_walk_tplace_total", 0.0)
                              + stats.t_place)
    rar._walk_trect_total = (getattr(rar, "_walk_trect_total", 0.0)
                             + stats.t_rect)
    rar._walk_tpoly_total = (getattr(rar, "_walk_tpoly_total", 0.0)
                             + stats.t_poly)
    n_err = len(rar.errors)
    # Level-1 (--debug) summary: one readable line — where the time went, what
    # the cache did, and whether anything went wrong. Deep per-cell / per-spec
    # counters are level-2 (MMH_GDS_DEBUG=2).
    _dbg(f"{layer}/{datatype} in {stats.elapsed_s:.1f}s | "
         f"decode {_decode_prof['total']:.1f}s "
         f"({cached} cached, {fresh} decoded) | "
         f"place {stats.t_place:.1f}s | "
         f"geom {stats.t_rect + stats.t_poly:.1f}s | "
         f"out {stats.rects_emitted}r {stats.polys_emitted}p"
         + (f" | ⚠ {n_err} decode error(s)" if n_err else ""))
    if _decode_prof["cell"] is not None:
        _trace(f"  slowest decode: cell {_decode_prof['cell']!r} "
               f"{_decode_prof['max']:.1f}s (placements={_decode_prof['np']}, "
               f"rect_specs={_decode_prof['nr']}, poly_specs={_decode_prof['npl']})")
    _trace(f"  perf: visited={stats.instances_visited} "
         f"arrays_materialized={stats.arrays_materialized} "
         f"instances_materialized={stats.instances_materialized} "
         f"max_array_k={stats.max_array_k}")
    _trace(f"  scanned: placements={stats.placements_scanned} "
         f"rect_specs={stats.rect_specs_scanned} "
         f"poly_specs={stats.poly_specs_scanned}")
    _trace(f"  features: place_rtypes={sorted(str(x) for x in _feat['rtype'])} "
         f"angles={sorted(_feat['angle'])} flip={_feat['flip']} "
         f"mags={sorted(_feat['mag'])} name_ref={_feat['name_ref']} "
         f"rect_rtypes={sorted(str(x) for x in _feat['rect_rtype'])} "
         f"poly_rtypes={sorted(str(x) for x in _feat['poly_rtype'])} "
         f"ce_violations={_feat['ce_viol']}")
    if rar.errors:
        for cid, off, m in rar.errors[:8]:
            _dbg(f"  ERROR cell {cid!r} @ {off}: {m}")
    return {"rects": rects, "polys": poly_out, "stats": stats}


def walk_roi_fast(rar: "RandomAccessReader", root_id: object, roi_bbox: Bbox,
                  layer: int, datatype: int, *, max_depth: int = 128,
                  cancel_cb=None) -> dict:
    """F27 M3: native-accelerated ``walk_roi`` for the batch/export path.

    When the graph reachable from ``root_id`` is native-able (plain rect /
    no-rep / single-layer / D4 — :func:`_flatten_cached` returns arrays), the
    whole ROI descent runs in one ``walk_rects_native`` C call, ~100× the Python
    walk end-to-end (F27 M3b). The emitted-rect SET is byte-identical to
    :func:`walk_roi` (order differs — rasterization is order-independent); polys
    are empty (native-able excludes polygons). Otherwise (no extension, or the
    subtree has a polygon / repetition / non-D4 / name-ref) it falls straight
    through to the pure-Python :func:`walk_roi`, so the result is always
    identical — the native path only ever runs where it is exact.

    Kept separate from ``walk_roi`` (rather than gating inside it) so the walk's
    diagnostic stats + placement-prep cache — which many callers/tests rely on —
    stay untouched on the Python path. ``cancel_cb`` is honoured between images
    by the caller; a native walk is sub-ms so it is not polled mid-walk (the
    one-time flatten isn't polled either)."""
    if _FASTWALK is not None:
        flat = _flatten_cached(rar, root_id, layer, datatype)
        if flat is not None:
            rc, ro, pt, pM, pT, po, rb, ri = flat
            scale = getattr(rar, "_nm_per_grid", 1.0) or 1.0
            roi = (float(roi_bbox[0]) / scale, float(roi_bbox[1]) / scale,
                   float(roi_bbox[2]) / scale, float(roi_bbox[3]) / scale)
            nat = _FASTWALK.walk_rects_native(
                rc, ro, pt, pM, pT, po, rb, int(ri),
                float(roi[0]), float(roi[1]), float(roi[2]), float(roi[3]),
                int(max_depth))
            if scale != 1.0:
                nat = nat * scale
            rects = np.rint(nat).astype(np.int64)
            st = RoiWalkStats()
            st.rects_emitted = int(rects.shape[0])
            st.sbbox_prune = bool(rar._sbbox_by_refnum or rar._sbbox_by_name)
            return {"rects": rects, "polys": [], "stats": st}
    return walk_roi(rar, root_id, roi_bbox, layer, datatype,
                    max_depth=max_depth, cancel_cb=cancel_cb)


# ── F12: layer enumeration for files with no LAYERNAME table ─────────────────
#
# Some production OASIS files (non-Calibre writers) ship without a LAYERNAME
# table, so the UI's "Scan layers" finds nothing and the user is forced to type
# raw (layer/datatype) pairs by hand. These files are multi-GB, so a full-file
# geometry scan "never finishes" (user constraint, 2026-06-04). Instead we do a
# *bounded* sample: KLayout-converted copies carry S_CELL_OFFSET, so we seek to
# a spread of cells via that index and read only the first few records of each,
# collecting the (layer, datatype) pairs that actually appear. This is capped by
# max_cells x max_records_per_cell + a wall-clock budget, with early-stop once
# the layer set stops growing. It is intentionally NOT exhaustive: a rare layer
# living only in an unsampled deep cell may be missed (the user can still type
# it). See docs/plans/F12-no-layername-scan.md.

# Geometry record ids that carry a (layer, datatype) in their payload.
_GEOM_LAYER_RIDS = (
    oas.RECTANGLE, oas.POLYGON, oas.PATH,
    oas.TRAPEZOID, oas.TRAPEZOID_VR, oas.TRAPEZOID_VL,
    oas.CTRAPEZOID, oas.CIRCLE,
)

# Default bounds for the no-LAYERNAME layer sample. Generous enough to catch
# layers that live in only a few cells while still finishing on a multi-GB file
# (the wall-clock budget is the hard backstop). Each is overridable per-call
# and via an env var (GLAS_SCAN_*) so coverage can be widened without a code
# change when a file turns out to segregate layers across many cells.
_SCAN_DEFAULTS = {
    "max_cells": 512,
    "max_records_per_cell": 8000,
    "time_budget_s": 30.0,
    "stop_after_no_new": 128,
}


def _scan_param(name: str, explicit, cast):
    """Resolve a sample bound: explicit arg > env GLAS_SCAN_<NAME> > default."""
    if explicit is not None:
        return explicit
    env = os.environ.get("GLAS_SCAN_" + name.upper())
    if env:
        try:
            return cast(env)
        except ValueError:
            pass
    return _SCAN_DEFAULTS[name]


def _layernames_to_layer_dicts(layernames: list) -> list[dict]:
    """Fast path: turn a LAYERNAME table (``[(name, layer_iv, dtype_iv), …]``)
    into the ``[{layer, datatype, name}]`` rows the UI's LayerPickDialog eats.

    De-dup is by ``(layer, datatype)`` (LAYERNAME-text and -geometry emit the
    same pair under one name); keep the first name, prefer non-empty over blank.
    The layer / datatype number is the low end of each interval, matching the
    long-standing app scan (Calibre / KLayout write interval kind 3, n..INF)."""
    out: list[dict] = []
    seen: dict[tuple[int, int], dict] = {}
    for name, liv, div in layernames:
        L = int(liv[0]); D = int(div[0])
        key = (L, D)
        ent = seen.get(key)
        if ent is None:
            ent = {"layer": L, "datatype": D, "name": name or ""}
            seen[key] = ent
            out.append(ent)
        elif name and not ent["name"]:
            ent["name"] = name
    return out


def _sample_offsets(offsets: list[int], max_cells: int) -> list[int]:
    """Pick ``<= max_cells`` byte offsets spread evenly across the (sorted)
    offset table so the sample spans the whole file rather than clustering at
    one end."""
    offs = sorted(set(int(o) for o in offsets))
    n = len(offs)
    if n <= max_cells:
        return offs
    step = n / float(max_cells)
    picked = [offs[min(int(i * step), n - 1)] for i in range(max_cells)]
    # int(i*step) can collide near the tail; de-dup while keeping order.
    seen: set[int] = set()
    uniq = [o for o in picked if not (o in seen or seen.add(o))]
    return uniq


def sample_layers(rar: "RandomAccessReader", *,
                  max_cells: int = 64, max_records_per_cell: int = 2000,
                  time_budget_s: float = 15.0, stop_after_no_new: int = 16,
                  include_text: bool = True,
                  progress_cb=None) -> list[dict]:
    """Bounded layer enumeration over an *already-built* RandomAccessReader.

    Samples up to ``max_cells`` cells (spread across the S_CELL_OFFSET table),
    seeking to each and reading at most ``max_records_per_cell`` records,
    collecting distinct ``(layer, datatype)``. Stops early after
    ``stop_after_no_new`` consecutive sampled cells add nothing, or once
    ``time_budget_s`` elapses. Returns ``[{layer, datatype, name=""}]`` in
    discovery order. NEVER scans the whole file.

    ``progress_cb(cells_done, layers_so_far)`` (optional) is called after each
    sampled cell so the UI can stream results and let the user cancel early."""
    sample = _sample_offsets(list(rar._by_refnum.values()), max_cells)
    if not sample:
        return []

    reader = rar._reader
    f = reader._f
    buf = f._buf
    found: dict[tuple[int, int], dict] = {}
    order: list[dict] = []
    no_new = 0
    t0 = time.monotonic()

    for ci, off in enumerate(sample):
        if time.monotonic() - t0 > time_budget_s:
            break
        first = buf[off] if 0 <= off < len(buf) else -1
        if first not in (oas.CELL_REFNUM, oas.CELL_NAME):
            continue                      # offset doesn't land on a CELL record
        before = len(found)
        f.clear_substreams()
        f.seek(off)
        seen_header = False
        recs = 0
        try:
            for rid, payload in reader.iter_records():
                if rid in (oas.CELL_REFNUM, oas.CELL_NAME):
                    if seen_header:
                        break             # next cell -> this cell is done
                    seen_header = True
                    continue
                if rid == oas.END:
                    break
                key = None
                if rid in _GEOM_LAYER_RIDS:
                    L = payload.get("layer")
                    if L is not None:
                        key = (int(L), int(payload.get("datatype") or 0))
                elif include_text and rid == oas.TEXT:
                    L = payload.get("text_layer")
                    if L is not None:
                        key = (int(L), int(payload.get("text_type") or 0))
                if key is not None and key not in found:
                    ent = {"layer": key[0], "datatype": key[1], "name": ""}
                    found[key] = ent
                    order.append(ent)
                recs += 1
                if recs >= max_records_per_cell:
                    break
        except (oas.OasisFormatError, oas.OasisNotImplemented):
            # Skip a cell that desyncs OR contains an unimplemented record
            # (e.g. XGEOMETRY) and keep sampling the rest — one extension
            # record must not fail the whole layer scan.
            continue

        if len(found) == before:
            no_new += 1
        else:
            no_new = 0
        if progress_cb is not None:
            progress_cb(ci + 1, list(order))
        if no_new >= stop_after_no_new:
            break

    return order


def enumerate_layers(path: str | Path, *, progress_cb=None, use_cache: bool = True,
                     max_cells: int = None, max_records_per_cell: int = None,
                     time_budget_s: float = None, stop_after_no_new: int = None,
                     include_text: bool = True) -> dict:
    """Enumerate the layers in an OASIS file for the "Scan layers" UI (F12).

    Returns ``{"layers": [{layer, datatype, name}], "source": str}`` where
    ``source`` is one of:

    - ``"layername"`` — file has a LAYERNAME table; rows carry names (fast,
      exhaustive, unchanged from the historical scan).
    - ``"sampled"`` — no LAYERNAME table; rows are numeric-only (name="") from
      a bounded cell sample (may be incomplete; user can type missing pairs).
    - ``"no-index"`` — no LAYERNAME *and* no S_CELL_OFFSET; ``layers`` is empty.
      The caller should advise re-saving via KLayout to add the index tables.

    A single RandomAccessReader is built so the name-table (LAYERNAME +
    S_CELL_OFFSET) is read once and shared with the sampling pass.

    The sample bounds (``max_cells`` / ``max_records_per_cell`` /
    ``time_budget_s`` / ``stop_after_no_new``) default to ``_SCAN_DEFAULTS`` but
    may be passed explicitly or set via ``GLAS_SCAN_*`` env vars to widen
    coverage when a file segregates layers across many cells.

    ``use_cache`` (F12 M3): a hit on the per-file sidecar (keyed by mtime+size
    + the resolved sample bounds) returns instantly and skips the
    reader/sample entirely (no ``progress_cb`` calls); the result is cached on
    every miss. Caching is best-effort and can never break a scan."""
    max_cells = _scan_param("max_cells", max_cells, int)
    max_records_per_cell = _scan_param("max_records_per_cell",
                                       max_records_per_cell, int)
    time_budget_s = _scan_param("time_budget_s", time_budget_s, float)
    stop_after_no_new = _scan_param("stop_after_no_new", stop_after_no_new, int)
    cache_params = {
        "max_cells": max_cells,
        "max_records_per_cell": max_records_per_cell,
        "time_budget_s": time_budget_s,
        "stop_after_no_new": stop_after_no_new,
        "include_text": include_text,
    }
    if use_cache:
        cached = layerscan_cache.load(path, cache_params)
        if cached is not None:
            return cached

    rar = RandomAccessReader(path, wanted_layers=None)
    try:
        if rar._layernames:
            result = {"layers": _layernames_to_layer_dicts(rar._layernames),
                      "source": "layername"}
        elif not rar._by_refnum:
            result = {"layers": [], "source": "no-index"}
        else:
            layers = sample_layers(
                rar, max_cells=max_cells,
                max_records_per_cell=max_records_per_cell,
                time_budget_s=time_budget_s, stop_after_no_new=stop_after_no_new,
                include_text=include_text, progress_cb=progress_cb)
            result = {"layers": layers, "source": "sampled"}
        result["diag"] = {
            "offset_flag": rar._offset_flag,
            "offsets_via": rar._offsets_via,
            "table_offsets": rar._table_offsets,
            "n_cell_offsets": len(rar._by_refnum),
            "n_layernames": len(rar._layernames),
            "n_bbox_props": rar._n_bbox_props,
            "bbox_sample": rar._bbox_sample,
            "n_layers": len(result["layers"]),
            "source": result["source"],
            "sample_bounds": {
                "max_cells": max_cells,
                "max_records_per_cell": max_records_per_cell,
                "time_budget_s": time_budget_s,
                "stop_after_no_new": stop_after_no_new,
            },
        }
    finally:
        rar.close()

    _dbg(f"enumerate_layers {Path(path).name}: {result['diag']}")
    if use_cache:
        layerscan_cache.save(path, result, cache_params)
    return result


# ── Debug CLI: dump a single cell's record stream ────────────────────────────


def dump_cell(path, offset: int, max_records: int = 400) -> None:
    """Seek to ``offset`` and print every record (id + key fields) until the
    next CELL / END or a decode error. Used to pinpoint where decoding a
    specific cell desyncs (F2 M3.5 debugging)."""
    reader = oas.OasisReader(path)
    f = reader._f
    f.clear_substreams()
    f.seek(int(offset))
    first = f._buf[offset] if 0 <= offset < len(f._buf) else -1
    print(f"dump cell @ {offset} (first byte id={first})")
    keys_of_interest = ("layer", "datatype", "x", "y", "width", "height",
                        "cell_ref", "cell_ref_kind", "angle", "flip",
                        "magnification", "repetition_type", "filtered_out")
    n = 0
    seen_cell = False
    try:
        for rid, payload in reader.iter_records():
            name = oas.RECORD_NAMES.get(rid, str(rid))
            info = {k: payload[k] for k in keys_of_interest if k in payload}
            pts = payload.get("points")
            if pts:
                info["npts"] = len(pts)
            ro = payload.get("repetition_offsets")
            if ro:
                info["nrep"] = len(ro)
            print(f"  [{n:>4}] @{reader._last_record_start} id={rid:>2} "
                  f"{name:<16} {info}")
            n += 1
            if rid in (oas.CELL_REFNUM, oas.CELL_NAME):
                if seen_cell:
                    print("  -- reached next CELL; stop")
                    break
                seen_cell = True
            elif rid == oas.END:
                print("  -- reached END")
                break
            if n >= max_records:
                print(f"  -- hit max_records={max_records}")
                break
    except Exception as exc:
        print(f"  !! DESYNC after {n} records, cursor @{f.tell()}: {exc}")
        print("  " + _hexdump(f._buf, min(f.tell(), len(f._buf) - 1), span=16))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="Dump a single OASIS cell's records from a byte offset "
                    "(debug the random-access decoder).")
    ap.add_argument("path")
    ap.add_argument("--dump-cell", type=int, required=True, metavar="OFFSET",
                    help="byte offset of the CELL record (from --debug log)")
    ap.add_argument("--max", type=int, default=400)
    args = ap.parse_args()
    dump_cell(args.path, args.dump_cell, args.max)
