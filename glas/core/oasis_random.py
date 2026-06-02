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

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import oasis_streamer as oas      # noqa: E402
from oasis_store import Placement  # noqa: E402
from oasis_walker import Transform  # noqa: E402

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

DEBUG = os.environ.get("MMH_GDS_DEBUG", "").lower() in ("1", "true", "yes", "on")


def set_debug(on: bool) -> None:
    """Toggle ROI debug tracing (also via env MMH_GDS_DEBUG=1)."""
    global DEBUG
    DEBUG = bool(on)


def _dbg(msg: str) -> None:
    if DEBUG:
        print(f"[roi] {msg}", file=sys.stderr, flush=True)


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
    placements: list = field(default_factory=list)
    bbox: Optional[Bbox] = None

    def is_empty(self) -> bool:
        return (not self.rect_specs and not self.poly_specs
                and not self.placements)

    def rects(self, key: LayerKey, dtype=np.int32) -> np.ndarray:
        """Materialize all rectangles on ``key`` as ``(N, 4)`` (vectorized
        repetition expansion). Empty ``(0, 4)`` when none."""
        specs = self.rect_specs.get(key)
        if not specs:
            return np.empty((0, 4), dtype=dtype)
        out = []
        for x1, y1, x2, y2, rt, rr in specs:
            offs = oas.repetition_offsets_np(rt, rr)        # (M, 2)
            arr = np.empty((offs.shape[0], 4), dtype=np.float64)
            arr[:, 0] = x1 + offs[:, 0]; arr[:, 1] = y1 + offs[:, 1]
            arr[:, 2] = x2 + offs[:, 0]; arr[:, 3] = y2 + offs[:, 1]
            out.append(arr)
        return np.concatenate(out).astype(dtype)

    def polys(self, key: LayerKey) -> list:
        """Materialize polygons on ``key`` as a list of ``(n, 2)`` arrays."""
        specs = self.poly_specs.get(key)
        if not specs:
            return []
        out = []
        for base, rt, rr in specs:
            for dx, dy in oas.repetition_offsets_np(rt, rr):
                s = base.copy()
                s[:, 0] += int(dx); s[:, 1] += int(dy)
                out.append(s)
        return out


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


class RandomAccessReader:
    """Seek-and-decode a single cell at a time, memoized.

    ``wanted_layers`` restricts which geometry is *kept* (other layers
    are still decoded to keep the stream in sync, but their bulk data is
    dropped) — same semantics as ``OasisReader``."""

    def __init__(self, path: str | Path,
                 wanted_layers: Optional[set[LayerKey]] = None,
                 *, dtype=np.int32,
                 bbox_layer: Optional[LayerKey] = None) -> None:
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
        idx = oas.scan_cell_offsets(path, shared_buf=shared)
        self._by_refnum: dict[int, int] = idx["by_refnum"]
        self._by_name: dict[str, int] = idx["by_name"]
        # OASIS START `unit` = grid steps per micron. Raw coordinates are in
        # grid steps; 1 grid = 1000/unit nm. The decoder returns raw grid
        # coords, so geometry must be scaled by this to reach nm (the frame
        # the FOV box / KLARF / RFL all use). unit==1000 -> 1.0 (no-op).
        self._unit = idx.get("unit")
        self._layernames = idx.get("layernames") or []
        # F13: KLayout per-cell S_BOUNDING_BOX (raw 5-int operand lists), if the
        # file carries them. When present, std_bbox() yields each cell's full
        # reachable bbox directly — no CE layer, no recursion, no decode. M1
        # only loads + exposes them (std_bbox / diagnostics); wiring into the
        # reachable_bbox prune is M2, after the operand format is confirmed on a
        # real file. Empty dicts -> std_bbox() returns None -> prune unchanged.
        self._sbbox_by_refnum: dict[int, list] = idx.get("sbbox_by_refnum") or {}
        self._sbbox_by_name: dict[str, list] = idx.get("sbbox_by_name") or {}
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
            dtype=self._dtype, bbox_layer=self._bbox_layer)

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

    def has_std_bboxes(self) -> bool:
        """True when the file carries KLayout per-cell S_BOUNDING_BOX (F13)."""
        return bool(self._sbbox_by_refnum) or bool(self._sbbox_by_name)

    def std_bbox_raw(self, cell_id: object) -> Optional[list]:
        """Raw S_BOUNDING_BOX operand list for ``cell_id`` (refnum or name),
        or None. Exposed for the F13 M1 diagnostic that confirms the operand
        format on a real file before it's trusted for pruning."""
        if isinstance(cell_id, int):
            return self._sbbox_by_refnum.get(cell_id)
        if isinstance(cell_id, bytes):
            cell_id = cell_id.decode("ascii", "replace")
        if isinstance(cell_id, str):
            return self._sbbox_by_name.get(cell_id)
        return None

    def std_bbox(self, cell_id: object) -> Optional[Bbox]:
        """Per-cell bounding box from S_BOUNDING_BOX in the cell-local *grid*
        frame (same frame as ``reachable_bbox``), or None when absent.

        Operand format is **assumed** ``[flag, x, y, w, h]`` (SEMI P39 §31);
        this is verified per-file by the F13 M1 diagnostic before M2 wires it
        into the prune. ``flag`` bit 0 marks an empty cell -> no box."""
        raw = self.std_bbox_raw(cell_id)
        if not raw or len(raw) < 5:
            return None
        flag, x, y, w, h = raw[0], raw[1], raw[2], raw[3], raw[4]
        if flag & 1:                      # empty-cell flag -> no geometry
            return None
        return (float(x), float(y), float(x + w), float(y + h))

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

    def load_cell(self, cell_id: object) -> CellContent:
        """Decode just the cell ``cell_id`` (memoized). Returns an empty
        :class:`CellContent` if the cell has no known offset."""
        if cell_id in self._memo:
            return self._memo[cell_id]
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
            if DEBUG and self._n_loaded % 500 == 0:
                _dbg(f"… {self._n_loaded:,} cells decoded so far "
                     f"(last {cell_id!r} @ {offset})")
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
                           placements=placements, bbox=bbox)

    def _decode_at(self, offset: int) -> CellContent:
        reader = self._reader
        f = reader._f
        f.clear_substreams()
        f.seek(int(offset))

        rect_specs: dict[LayerKey, list] = {}
        poly_specs: dict[LayerKey, list] = {}
        placements: list = []
        seen_cell_header = False

        for rid, payload in reader.iter_records():
            if rid in (oas.CELL_REFNUM, oas.CELL_NAME):
                if seen_cell_header:
                    break                  # next cell -> our cell is done
                seen_cell_header = True
                continue
            if rid == oas.END:
                break
            if rid == oas.RECTANGLE:
                if payload.get("filtered_out"):
                    continue
                key = (payload["layer"], payload["datatype"])
                x1 = payload["x"]; y1 = payload["y"]
                # Store the base rect + its repetition descriptor; expanded
                # lazily (and vectorized) only if this cell lands in the ROI.
                rect_specs.setdefault(key, []).append((
                    x1, y1, x1 + payload["width"], y1 + payload["height"],
                    payload.get("repetition_type"), payload.get("repetition_raw")))
            elif rid == oas.POLYGON:
                if payload.get("filtered_out"):
                    continue
                pts = payload.get("points") or []
                if not pts:
                    continue
                ax = payload["x"]; ay = payload["y"]
                base = np.asarray(pts, dtype=self._dtype)
                base[:, 0] += ax
                base[:, 1] += ay
                pkey = (payload["layer"], payload["datatype"])
                poly_specs.setdefault(pkey, []).append((
                    base, payload.get("repetition_type"),
                    payload.get("repetition_raw")))
            elif rid in (oas.PLACEMENT_NOMAG, oas.PLACEMENT_MAG):
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

        return CellContent(rect_specs=rect_specs, poly_specs=poly_specs,
                           placements=placements,
                           bbox=_analytic_bbox(rect_specs, poly_specs))


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
    bx1 = np.minimum(boxes[:, 0], boxes[:, 2])
    by1 = np.minimum(boxes[:, 1], boxes[:, 3])
    bx2 = np.maximum(boxes[:, 0], boxes[:, 2])
    by2 = np.maximum(boxes[:, 1], boxes[:, 3])
    return (bx1 <= roi[2]) & (bx2 >= roi[0]) & (by1 <= roi[3]) & (by2 >= roi[1])


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
    _dbg(f"walk_roi root={root_id!r} layer={layer}/{datatype} "
         f"roi_nm={tuple(roi_bbox)} scale={scale} roi_grid={roi}")
    _t0 = time.perf_counter()
    cells_at_start = rar._n_loaded
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
             "poly_rtype": set(), "ce_viol": 0}

    def reachable_bbox(cid: object) -> Optional[Bbox]:
        """Bbox (cid-local frame) of all geometry reachable from cid —
        own + children, over full repetition extent. Memoized; cycles
        return None."""
        if cid in reach_memo:
            return reach_memo[cid]
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
        content = rar.load_cell(cid)
        stats.cell_visits += 1
        # Debug: does the CE early-stop bbox actually bound the cell's real
        # geometry? (M3.5e.3 assumes CE rect == cell full bbox.) Compare the
        # full-decode own bbox against the CE-only bbox for descended cells.
        if DEBUG and content.bbox is not None:
            _ce = rar.load_cell_bbox(cid)
            _ceb = _ce.bbox if _ce is not None else None
            ob = content.bbox
            inside = (_ceb is not None and _ceb[0] <= ob[0] and _ceb[1] <= ob[1]
                      and _ceb[2] >= ob[2] and _ceb[3] >= ob[3])
            if not inside:
                _feat["ce_viol"] += 1
                if _feat["ce_viol"] <= 6:
                    _dbg(f"  CE-VIOLATION cell {cid!r}: own_bbox={ob} "
                         f"ce_bbox={_ceb}")
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
        for _specs in content.rect_specs.values():
            for _s in _specs:
                _feat["rect_rtype"].add(_s[4])
        for _specs in content.poly_specs.values():
            for _s in _specs:
                _feat["poly_rtype"].add(_s[1])
        # Emit this cell's own geometry (transformed) that hits the ROI.
        # Materialize lazily here — only for cells the walk actually visits.
        own = content.rects(key)
        if own.size:
            r = T.apply_to_rects(own.astype(np.float64))
            m = _roi_overlap_mask(r, roi)
            if m.any():
                rect_out.append(r[m])
                stats.rects_emitted += int(m.sum())
        for pts in content.polys(key):
            tp = T.apply_to_points(pts.astype(np.float64))
            bb = np.array([[tp[:, 0].min(), tp[:, 1].min(),
                            tp[:, 0].max(), tp[:, 1].max()]])
            if _roi_overlap_mask(bb, roi)[0]:
                poly_out.append(tp)
                stats.polys_emitted += 1
        if depth >= max_depth:
            return
        for pl in content.placements:
            rtype, rraw = pl.repetition_type, pl.repetition_raw
            base = Transform.from_placement(pl.x, pl.y, pl.angle, pl.flip,
                                            pl.magnification)
            if base is None:
                stats.arbitrary_angle_skipped += oas.repetition_count(rtype, rraw)
                continue
            cb = reachable_bbox(pl.target)
            if cb is None:
                stats.unknown_target_skipped += 1
                continue
            placed = _xform_bbox(base, cb)            # parent-local, offset 0
            # Cheap whole-array prune: extend placed bbox by the repetition
            # extent and test the whole array against ROI before touching
            # any individual instance (avoids materializing huge grids).
            ex0, ey0, ex1, ey1 = oas.repetition_extent(rtype, rraw)
            arr_local = np.array([[placed[0] + ex0, placed[1] + ey0,
                                   placed[2] + ex1, placed[3] + ey1]])
            if not _roi_overlap_mask(T.apply_to_rects(arr_local), roi)[0]:
                stats.instances_pruned += oas.repetition_count(rtype, rraw)
                continue
            if pl.target in visiting:
                stats.cycles_skipped += oas.repetition_count(rtype, rraw)
                continue
            # Array may intersect ROI — now materialize offsets (vectorized)
            # for per-instance pruning.
            oa = oas.repetition_offsets_np(rtype, rraw)          # (K,2)
            K = oa.shape[0]
            plb = np.empty((K, 4), dtype=np.float64)
            plb[:, 0] = placed[0] + oa[:, 0]; plb[:, 1] = placed[1] + oa[:, 1]
            plb[:, 2] = placed[2] + oa[:, 0]; plb[:, 3] = placed[3] + oa[:, 1]
            rootb = T.apply_to_rects(plb)                        # -> root coords
            mask = _roi_overlap_mask(rootb, roi)
            sel = np.flatnonzero(mask)
            stats.instances_pruned += K - len(sel)
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
    _dbg(f"walk_roi done in {time.perf_counter() - _t0:.1f}s: "
         f"rects={stats.rects_emitted} polys={stats.polys_emitted} "
         f"newly_decoded_cells={rar._n_loaded - cells_at_start} "
         f"pruned={stats.instances_pruned} reader_errors={len(rar.errors)}")
    _dbg(f"  features: place_rtypes={sorted(str(x) for x in _feat['rtype'])} "
         f"angles={sorted(_feat['angle'])} flip={_feat['flip']} "
         f"mags={sorted(_feat['mag'])} name_ref={_feat['name_ref']} "
         f"rect_rtypes={sorted(str(x) for x in _feat['rect_rtype'])} "
         f"poly_rtypes={sorted(str(x) for x in _feat['poly_rtype'])} "
         f"ce_violations={_feat['ce_viol']}")
    if rar.errors:
        for cid, off, m in rar.errors[:8]:
            _dbg(f"  ERROR cell {cid!r} @ {off}: {m}")
    return {"rects": rects, "polys": poly_out, "stats": stats}


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
