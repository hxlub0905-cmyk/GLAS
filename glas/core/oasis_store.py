"""Per-cell, per-layer geometry accumulator on top of OasisReader (F2 M1.11b).

What this is
------------
``oasis_streamer.OasisReader`` walks an OASIS file record by record and
yields each shape as a dict payload. That's the right shape for testing
and debugging, but for the GDS-SEM alignment use case we want **dense
numpy arrays** so we can rasterize / template-match a chosen layer in
the SEM's field of view.

``OasisGeometryStore`` is the bridge between those two worlds. It
drives the reader's ``iter_records()`` loop, groups every geometry
record by ``(parent_cell, layer, datatype)``, and accumulates the
results into ``int32`` ndarrays that fit in main memory on a personal
laptop.

What this is NOT
----------------
This module **does not** apply PLACEMENT transforms or walk the cell
hierarchy. Every coordinate is in the cell-local frame, exactly as it
appears in the OASIS file. Resolving root-cell coordinates requires
applying the PLACEMENT graph (rotation + mirror + magnification +
repetition); that is the job of M1.11c (``oasis_walker.py``) and is
intentionally kept separate so this storage layer can be reviewed and
tested in isolation.

Memory budget
-------------
The user's 345 MB Calibre D2DB has 37 layers and an estimated ~20 M
unique rectangles across all cells. At ``int32`` x 4 columns each
rectangle costs 16 bytes; storing one whole layer is therefore on the
order of a few hundred MB, well within laptop RAM. To stay within that
budget the store enforces a layer filter on files above a soft
threshold (``REQUIRE_FILTER_BYTES``): without one, the user has to
explicitly opt in with ``allow_unfiltered=True``.

Public surface
--------------
::

    store = OasisGeometryStore(path, wanted_layers={(17, 102)})
    store.run()                     # walks the whole file once

    store.cells                     # dict: refnum -> name (None at top level)
    store.rectangles_for(cell_ref, layer, datatype)   # -> ndarray (N, 4)
    store.polygons_for(cell_ref, layer, datatype)     # -> list[ndarray (n, 2)]
    store.placements_for(cell_ref)  # -> list[Placement]
    store.summary()                 # -> dict of cell/layer/record counts

The four ``*_for`` queries return empty containers (not None) when the
requested key isn't populated, so callers can iterate without
defensive ``if x is not None`` boilerplate.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

# Resolve the streamer module whether we're imported as ``tools.oasis_store``
# from the repo root or run as ``python tools/oasis_store.py``.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import oasis_streamer as oas  # noqa: E402


# ── Soft policy: refuse to walk huge files without a layer filter ────────────


REQUIRE_FILTER_BYTES = 50 * 1024 * 1024  # 50 MB
"""Files larger than this raise unless the caller passes ``wanted_layers``
or explicitly opts in with ``allow_unfiltered=True``. Memory on a
personal laptop is the constraint; the 37-layer 345 MB D2DB without a
filter would balloon to multi-GB of rectangles."""


# ── Per-cell PLACEMENT entry ─────────────────────────────────────────────────


@dataclass
class Placement:
    """One PLACEMENT record stored verbatim from the parser payload.

    The transform fields (x, y, angle, magnification, flip) plus the
    repetition offset list are everything the M1.11c walker will need
    to expand instances into root-cell coordinates. ``target`` is
    either a cellname refnum (int) or an inline a-string (str),
    matching ``OasisReader``'s ``cell_ref`` field.
    """
    target: object
    target_kind: str        # 'refnum' | 'name' | 'modal'
    x: int
    y: int
    angle: float
    magnification: float
    flip: bool
    repetition_type: Optional[int]
    repetition_offsets: list[tuple[int, int]]
    # Compact (rtype, raw) descriptor when offsets are deferred (M3.5e
    # random-access load); None for the eager full-decode path.
    repetition_raw: Optional[tuple] = None


# ── Chunked ndarray growth for rectangles ────────────────────────────────────


class _RectBuffer:
    """Append-only buffer that grows in geometric chunks.

    A bare Python list with ``np.array(list)`` at the end works for
    thousands of rectangles but blows up well before the 5-million mark
    the user's D2DB hits per layer (each Python tuple costs ~100 bytes,
    so 5 M tuples eat 500 MB before the ndarray even gets allocated).
    Chunked ndarray growth keeps the live memory bounded at roughly
    2x the final size, with one O(N) concat at finalize time.
    """

    INITIAL_CAPACITY = 1024
    MAX_CHUNK = 1_000_000

    def __init__(self, dtype=np.int32) -> None:
        self._dtype = dtype
        self._chunks: list[np.ndarray] = []
        self._current = np.empty((self.INITIAL_CAPACITY, 4), dtype=dtype)
        self._used = 0

    def add(self, x1: int, y1: int, x2: int, y2: int) -> None:
        if self._used == self._current.shape[0]:
            self._chunks.append(self._current)
            new_size = min(self._current.shape[0] * 2, self.MAX_CHUNK)
            self._current = np.empty((new_size, 4), dtype=self._dtype)
            self._used = 0
        self._current[self._used] = (x1, y1, x2, y2)
        self._used += 1

    def __len__(self) -> int:
        return sum(c.shape[0] for c in self._chunks) + self._used

    def to_ndarray(self) -> np.ndarray:
        if not self._chunks and self._used == 0:
            return np.empty((0, 4), dtype=self._dtype)
        parts = self._chunks + [self._current[:self._used]]
        return np.concatenate(parts, axis=0)


# ── Geometry store ───────────────────────────────────────────────────────────


class OasisGeometryStore:
    """Drive an OasisReader and accumulate per-cell, per-layer geometry.

    Construction holds onto a file path; nothing is read until ``run()``
    is called. After ``run()``:

    * ``cells`` maps refnum -> name (as decoded from CELLNAME records).
    * ``rectangles_for(cell, layer, datatype)`` returns an int32 ndarray
      of shape ``(N, 4)`` where each row is ``(x1, y1, x2, y2)``.
    * ``polygons_for(cell, layer, datatype)`` returns a list of int32
      ndarrays, one per POLYGON record, each of shape ``(n, 2)``.
    * ``placements_for(cell)`` returns the list of PLACEMENT entries
      seen inside that cell, in file order.

    ``wanted_layers`` is forwarded straight to the underlying reader so
    the filter is applied at decode time -- filtered records still
    advance the byte cursor but their payloads come back stripped of
    heavy data, and we skip the accumulation step entirely.
    """

    def __init__(self, path: str | Path, *,
                 wanted_layers: Optional[set[tuple[int, int]]] = None,
                 allow_unfiltered: bool = False,
                 dtype=np.int32) -> None:
        self._path = Path(path)
        if not self._path.exists():
            raise FileNotFoundError(self._path)
        size = self._path.stat().st_size
        if (size > REQUIRE_FILTER_BYTES
                and wanted_layers is None
                and not allow_unfiltered):
            raise ValueError(
                f"refusing to walk {self._path.name} "
                f"({size:,} bytes > {REQUIRE_FILTER_BYTES:,} threshold) "
                f"without a layer filter -- pass wanted_layers={{(L, D), ...}} "
                f"or allow_unfiltered=True to override. The OASIS streamer "
                f"can scan layers cheaply; use that to pick a small set "
                f"first."
            )
        self._wanted_layers = wanted_layers
        self._dtype = dtype

        # Public state filled by run().
        self.cells: dict[int, str] = {}
        # _rect_buffers[cell_id][layer, datatype] -> _RectBuffer
        # _polys[cell_id][layer, datatype] -> list[ndarray]
        # _placements[cell_id] -> list[Placement]
        self._rect_buffers: dict[object, dict[tuple[int, int], _RectBuffer]] = {}
        self._polys: dict[object, dict[tuple[int, int], list[np.ndarray]]] = {}
        self._placements: dict[object, list[Placement]] = {}
        self._cellnames: dict[int, str] = {}
        self._implicit_cellname_idx = 0
        self._record_counts: dict[int, int] = {}
        self._has_run = False

    # ── Run ────────────────────────────────────────────────────────────────
    def run(self, *,
            max_records: int = 0,
            progress_every: int = 0,
            progress_callback=None) -> None:
        """Walk the whole file (or the first ``max_records`` records).

        Args:
            max_records: stop after this many records (0 = no limit).
            progress_every: stderr heartbeat interval. ``0`` disables.
            progress_callback: optional ``callable(total_records, stats_dict)``
                fired at the same interval. ``stats_dict`` contains
                ``cells``, ``rectangles``, ``polygons``, ``placements``
                running totals. Used by ``tools/gds_align_tool.py`` to
                forward progress to the GUI process via mp.Queue.
        """
        if self._has_run:
            raise RuntimeError("OasisGeometryStore.run() can only be called once")
        self._has_run = True

        # Bind callbacks via reader.consume(): hot records (RECTANGLE /
        # POLYGON, 98%+ on D2DB) pull from reader._modal directly so the
        # decoder never builds a payload dict on the hot path. Cold
        # records (PLACEMENT / CELLNAME, < 1%) get their payload dict
        # from reader.last_payload — the dict-alloc cost there is
        # negligible.
        callbacks = {
            oas.RECTANGLE: self._on_rectangle,
            oas.POLYGON: self._on_polygon,
            oas.PLACEMENT_NOMAG: self._on_placement,
            oas.PLACEMENT_MAG: self._on_placement,
            oas.CELLNAME_IMP: self._on_cellname,
            oas.CELLNAME_EXP: self._on_cellname,
        }
        record_counts = self._record_counts

        def on_each(rid: int, count: int):
            record_counts[rid] = record_counts.get(rid, 0) + 1
            if max_records and count >= max_records:
                return False
            if progress_every and count % progress_every == 0:
                rect_count = sum(len(b) for cell in self._rect_buffers.values()
                                 for b in cell.values())
                print(
                    f"  [store] {count:,} records, "
                    f"cells={len(self._placements)}, "
                    f"rect={rect_count:,}",
                    file=sys.stderr,
                )
                if progress_callback is not None:
                    progress_callback(count, {
                        "cells": len(self._placements),
                        "rectangles": rect_count,
                        "polygons": sum(len(p) for cell in self._polys.values()
                                        for p in cell.values()),
                        "placements": sum(len(lst) for lst in self._placements.values()),
                    })
            return None

        with oas.OasisReader(self._path,
                             wanted_layers=self._wanted_layers) as reader:
            reader.consume(callbacks, on_each=on_each)

    # ── Per-record dispatch ────────────────────────────────────────────────
    def _consume(self, rid: int, payload: dict) -> None:
        # CELLNAME table: map refnum -> name. Implicit cellnames use a
        # running counter; explicit ones bring their own refnum.
        if rid in (oas.CELLNAME_IMP, oas.CELLNAME_EXP):
            if payload["explicit"]:
                refnum = payload["refnum"]
            else:
                refnum = self._implicit_cellname_idx
                self._implicit_cellname_idx += 1
            name = payload["name"].decode("ascii", "backslashreplace")
            self._cellnames[refnum] = name
            self.cells[refnum] = name
            return

        # All geometry records carry an ``in_cell`` tag from the reader.
        if rid == oas.RECTANGLE:
            self._consume_rectangle(payload)
        elif rid == oas.POLYGON:
            self._consume_polygon(payload)
        elif rid in (oas.PLACEMENT_NOMAG, oas.PLACEMENT_MAG):
            self._consume_placement(payload)
        # PATH / TRAPEZOID / CTRAPEZOID / CIRCLE / TEXT: payload is fully
        # decoded but storage in this slice is intentionally limited to
        # the two geometry types that dominate the histogram (RECTANGLE
        # 98%, POLYGON 0.02%). The decoder still validated the bytes,
        # so the stream stays in sync; M1.11c can extend storage if a
        # future production file actually populates those record types.

    # ── consume() callbacks (M1.13.3a fast path) ──────────────────────────
    def _on_rectangle(self, reader) -> None:
        """RECTANGLE callback — pull from reader._modal directly.

        Bit-identical to ``_consume_rectangle`` but skips the payload
        dict packing/unpacking the iter_records path goes through. 98%+
        of D2DB records hit this; removing the dict alloc saves the
        bulk of the per-record Python overhead the iter_records path
        carried."""
        m = reader._modal
        if reader._layer_filtered_out(m.layer, m.datatype):
            return
        parent = reader._current_cell
        key = (m.layer, m.datatype)
        x1 = m.geometry_x
        y1 = m.geometry_y
        x2 = x1 + m.geometry_w
        y2 = y1 + m.geometry_h
        bufs = self._rect_buffers.setdefault(parent, {})
        buf = bufs.get(key)
        if buf is None:
            buf = _RectBuffer(dtype=self._dtype)
            bufs[key] = buf
        buf.add(x1, y1, x2, y2)

    def _on_polygon(self, reader) -> None:
        """POLYGON callback — pull from reader._modal directly."""
        m = reader._modal
        if reader._layer_filtered_out(m.layer, m.datatype):
            return
        pts = m.polygon_point_list
        if not pts:
            return
        parent = reader._current_cell
        key = (m.layer, m.datatype)
        ax = m.geometry_x
        ay = m.geometry_y
        arr = np.empty((len(pts), 2), dtype=self._dtype)
        for i, (px, py) in enumerate(pts):
            arr[i, 0] = ax + px
            arr[i, 1] = ay + py
        polys = self._polys.setdefault(parent, {}).setdefault(key, [])
        polys.append(arr)

    def _on_placement(self, reader) -> None:
        """PLACEMENT callback — pull from reader.last_payload (PLACEMENT
        keeps its dict; it's < 0.1% of records on D2DB so the alloc
        cost is negligible)."""
        payload = reader.last_payload
        entry = Placement(
            target=payload["cell_ref"],
            target_kind=payload["cell_ref_kind"],
            x=payload["x"],
            y=payload["y"],
            angle=float(payload["angle"]),
            magnification=float(payload["magnification"]),
            flip=bool(payload["flip"]),
            repetition_type=payload.get("repetition_type"),
            repetition_offsets=list(payload.get("repetition_offsets") or []),
        )
        self._placements.setdefault(reader._current_cell, []).append(entry)

    def _on_cellname(self, reader) -> None:
        """CELLNAME table callback — pull from reader.last_payload."""
        payload = reader.last_payload
        if payload["explicit"]:
            refnum = payload["refnum"]
        else:
            refnum = self._implicit_cellname_idx
            self._implicit_cellname_idx += 1
        name = payload["name"].decode("ascii", "backslashreplace")
        self._cellnames[refnum] = name
        self.cells[refnum] = name

    # ── Legacy iter_records dispatch (kept for backward-compat / tests) ───
    def _consume_rectangle(self, payload: dict) -> None:
        if payload.get("filtered_out"):
            return
        parent = payload["in_cell"]
        key = (payload["layer"], payload["datatype"])
        # Rectangle is defined by lower-left (x, y) + width / height.
        # Store as (x1, y1, x2, y2) so downstream code can spatial-index
        # without doing arithmetic.
        x1 = payload["x"]
        y1 = payload["y"]
        x2 = x1 + payload["width"]
        y2 = y1 + payload["height"]
        bufs = self._rect_buffers.setdefault(parent, {})
        buf = bufs.get(key)
        if buf is None:
            buf = _RectBuffer(dtype=self._dtype)
            bufs[key] = buf
        buf.add(x1, y1, x2, y2)

    def _consume_polygon(self, payload: dict) -> None:
        if payload.get("filtered_out"):
            return
        parent = payload["in_cell"]
        key = (payload["layer"], payload["datatype"])
        pts = payload.get("points") or []
        if not pts:
            return
        # Translate the polygon's point list by the (x, y) anchor so
        # every stored polygon is in the cell's local coordinate system,
        # consistent with how RECTANGLE x1/y1/x2/y2 are stored.
        ax = payload["x"]
        ay = payload["y"]
        arr = np.empty((len(pts), 2), dtype=self._dtype)
        for i, (px, py) in enumerate(pts):
            arr[i, 0] = ax + px
            arr[i, 1] = ay + py
        polys = self._polys.setdefault(parent, {}).setdefault(key, [])
        polys.append(arr)

    def _consume_placement(self, payload: dict) -> None:
        parent = payload["in_cell"]
        entry = Placement(
            target=payload["cell_ref"],
            target_kind=payload["cell_ref_kind"],
            x=payload["x"],
            y=payload["y"],
            angle=float(payload["angle"]),
            magnification=float(payload["magnification"]),
            flip=bool(payload["flip"]),
            repetition_type=payload.get("repetition_type"),
            repetition_offsets=list(payload.get("repetition_offsets") or []),
        )
        self._placements.setdefault(parent, []).append(entry)

    # ── Query API ──────────────────────────────────────────────────────────
    def rectangles_for(self, cell: object, layer: int, datatype: int) -> np.ndarray:
        """Return the ``(N, 4)`` ndarray of (x1, y1, x2, y2) rectangles
        stored under ``cell`` on ``(layer, datatype)``. Empty when
        nothing matches; never returns None."""
        bufs = self._rect_buffers.get(cell)
        if bufs is None:
            return np.empty((0, 4), dtype=self._dtype)
        buf = bufs.get((layer, datatype))
        if buf is None:
            return np.empty((0, 4), dtype=self._dtype)
        return buf.to_ndarray()

    def polygons_for(self, cell: object, layer: int, datatype: int) -> list[np.ndarray]:
        """Return the list of polygon point arrays. Empty list when
        nothing matches; each entry is shape ``(n, 2)``."""
        polys = self._polys.get(cell)
        if polys is None:
            return []
        return list(polys.get((layer, datatype), []))

    def placements_for(self, cell: object) -> list[Placement]:
        """Return PLACEMENT entries inside ``cell`` in file order."""
        return list(self._placements.get(cell, []))

    def layer_pairs_in(self, cell: object) -> set[tuple[int, int]]:
        """Return the set of (layer, datatype) pairs that have stored
        geometry under ``cell`` (rectangles or polygons)."""
        keys = set()
        if cell in self._rect_buffers:
            keys.update(self._rect_buffers[cell].keys())
        if cell in self._polys:
            keys.update(self._polys[cell].keys())
        return keys

    def summary(self) -> dict:
        """Return a compact dict of accumulated state, useful for tests
        and CLI dumps."""
        total_rect = sum(
            len(b) for cell in self._rect_buffers.values()
            for b in cell.values()
        )
        total_poly = sum(
            len(plist) for cell in self._polys.values()
            for plist in cell.values()
        )
        total_place = sum(len(lst) for lst in self._placements.values())
        return {
            "file": str(self._path),
            "wanted_layers": (sorted(self._wanted_layers)
                              if self._wanted_layers is not None else None),
            "cells_with_rectangles": len(self._rect_buffers),
            "cells_with_polygons": len(self._polys),
            "cells_with_placements": len(self._placements),
            "total_rectangles": total_rect,
            "total_polygons": total_poly,
            "total_placements": total_place,
            "cellnames_known": len(self._cellnames),
            "record_counts": dict(self._record_counts),
        }


# ── CLI smoke test (mirrors the streamer's pattern) ──────────────────────────


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Accumulate per-cell, per-layer rectangles + polygons "
                    "+ PLACEMENT graph from an OASIS file (F2 M1.11b "
                    "storage layer).",
    )
    ap.add_argument("path", help="OASIS file to scan")
    ap.add_argument(
        "--layer", action="append", default=[], metavar="L:D",
        help="Layer filter as ``LAYER:DATATYPE`` (may repeat). "
             "Required for files > 50 MB unless --allow-unfiltered.",
    )
    ap.add_argument(
        "--allow-unfiltered", action="store_true",
        help="Override the large-file filter requirement. "
             "Memory use can balloon.",
    )
    ap.add_argument(
        "--max-records", type=int, default=0, metavar="N",
        help="Stop after N records (0 = no limit).",
    )
    ap.add_argument(
        "--progress", type=int, default=500_000, metavar="N",
        help="Stderr progress heartbeat every N records (default 500000).",
    )
    args = ap.parse_args()

    wanted: Optional[set[tuple[int, int]]] = None
    if args.layer:
        wanted = set()
        for spec in args.layer:
            try:
                L, D = spec.split(":")
                wanted.add((int(L), int(D)))
            except ValueError:
                print(f"bad --layer spec {spec!r}, want L:D form",
                      file=sys.stderr)
                return 2

    store = OasisGeometryStore(
        args.path,
        wanted_layers=wanted,
        allow_unfiltered=args.allow_unfiltered,
    )
    store.run(max_records=args.max_records, progress_every=args.progress)

    import json
    summary = store.summary()
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
