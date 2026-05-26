"""Layer gathering + ROI clipping for OASIS export (F9 M2).

Bridges the app's per-layer polygon lists to :mod:`oasis_writer`. Two
jobs:

* **ROI crop** — clip every polygon to a caller-supplied GDS-coordinate
  bounding box (lower-left ``(x1, y1)`` → upper-right ``(x2, y2)``). When
  no box is given the geometry is written whole.
* **shapely flattening** — turn a shapely geometry into a flat list of
  exterior rings, matching ``gds_boolean.geometry_to_polygons`` (F9 holes
  decision O-holes: interiors are dropped; export mirrors what the canvas
  shows). Clipping a hole-free polygon by a convex box can only yield
  hole-free pieces, so the export stays hole-free end to end.

Kept separate from :mod:`oasis_writer` so the writer stays
dependency-light (stdlib only); this module pulls in numpy + shapely.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

import numpy as np

try:
    from shapely import Polygon, box
    from shapely.geometry.base import BaseGeometry
    _SHAPELY_OK = True
except Exception as exc:  # pragma: no cover - import guard
    _SHAPELY_OK = False
    _SHAPELY_ERR = exc

import oasis_writer


Bbox = tuple[float, float, float, float]


def _require_shapely() -> None:
    if not _SHAPELY_OK:  # pragma: no cover - import guard
        raise RuntimeError(f"shapely is required for layout_export: {_SHAPELY_ERR}")


def shapely_to_rings(geom: "BaseGeometry") -> list[np.ndarray]:
    """Flatten a shapely geometry into exterior-ring ``(n, 2)`` arrays.

    Interiors (holes) are dropped — same convention as
    ``gds_boolean.geometry_to_polygons`` (O-holes). Empty / non-areal
    geometries yield an empty list.
    """
    out: list[np.ndarray] = []
    if geom is None or geom.is_empty:
        return out
    for g in getattr(geom, "geoms", [geom]):
        ext = getattr(g, "exterior", None)
        if ext is None or len(ext.coords) < 4:
            continue
        out.append(np.asarray(ext.coords, dtype=np.float64))
    return out


def clip_polygons(polygons: Iterable[Sequence],
                  crop_bbox: Optional[Bbox]) -> list[np.ndarray]:
    """Clip a layer's polygons to ``crop_bbox``.

    ``crop_bbox`` is ``(x1, y1, x2, y2)`` in GDS nm (corners may be given
    in any order). When ``None`` the polygons pass through unchanged
    (whole-layout export). Returns a flat list of exterior rings; pieces
    fully outside the box drop out.
    """
    if crop_bbox is None:
        return [np.asarray(p, dtype=np.float64) for p in polygons]
    _require_shapely()
    x1, y1, x2, y2 = crop_bbox
    clip = box(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
    out: list[np.ndarray] = []
    for ring in polygons:
        arr = np.asarray(ring, dtype=np.float64)
        if len(arr) < 3:
            continue
        poly = Polygon(arr)
        if not poly.is_valid:
            poly = poly.buffer(0)
        out.extend(shapely_to_rings(poly.intersection(clip)))
    return out


def clip_layers(layers: Iterable,
                crop_bbox: Optional[Bbox]) -> list[tuple[int, int, list]]:
    """Apply :func:`clip_polygons` per layer, dropping layers left empty."""
    result: list[tuple[int, int, list]] = []
    for layer, datatype, polygons in layers:
        rings = clip_polygons(polygons, crop_bbox)
        if rings:
            result.append((int(layer), int(datatype), rings))
    return result


def export_layers(path: Union[str, Path], layers: Iterable,
                   *, crop_bbox: Optional[Bbox] = None,
                   unit: float = 1000.0, cellname: str = "TOP") -> int:
    """Clip ``layers`` to ``crop_bbox`` (or whole when ``None``) and write
    OASIS. Returns the number of non-empty layers written."""
    clipped = clip_layers(layers, crop_bbox)
    oasis_writer.write_oasis(path, clipped, unit=unit, cellname=cellname)
    return len(clipped)
