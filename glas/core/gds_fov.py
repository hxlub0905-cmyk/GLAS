"""KLARF<->GDS coordinate conversion + FOV spatial query (F2 M2.2 / M2.3).

Why this exists
---------------
The GDS-SEM alignment workflow needs two cheap, dependency-light
operations on the flattened layer geometry produced by
``oasis_walker``:

* **M2.3 coordinate conversion** -- SEM images carry KLARF coordinates
  (``XREL`` / ``YREL``) measured from the *die corner*; the GDS layout
  lives in *chip-corner* coordinates. The chip corner's position
  relative to the die corner (``chip_corner_x`` / ``chip_corner_y``) is
  read by the user from the RFL file. Converting one to the other is a
  single subtraction (see ``klarf_to_gds``).

* **M2.2 FOV query** -- given a field-of-view rectangle (centre +
  size, in GDS nm), pull out only the polygons whose bounding box
  overlaps it. A SEM FOV holds a few hundred polygons out of the
  hundreds of thousands in a layer, so a vectorised numpy bbox filter
  is plenty fast (sub-millisecond) without an R-tree.

Coordinate systems
-------------------
::

    KLARF: die corner   = (0, 0); XREL/YREL are nm from the die corner.
    GDS:   chip corner  = (0, 0).
           chip_corner_x/y = chip corner relative to die corner (nm).

    GDS_x = XREL - chip_corner_x
    GDS_y = YREL - chip_corner_y     (Y assumed same direction for now;
                                      flip in klarf_to_gds if a real
                                      measurement shows otherwise.)

The chip_corner offset is *derived* from the RFL file rather than
entered directly: the RFL gives die size, the GDS chip's centre
relative to the die centre, and the chip size -- all in mm, lower-left
origins -- and ``rfl_to_chip_corner`` turns those into chip_corner nm
via ``chip_corner = die_size/2 + chip_centre_offset - chip_size/2``.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional, Union

import numpy as np

LayerKey = tuple[int, int]
Number = Union[int, float, np.ndarray]

MM_TO_NM = 1_000_000.0   # GDS / KLARF work in nm; the RFL file is in mm.


# ── M2.3: RFL -> chip-corner offset ──────────────────────────────────


def rfl_to_chip_corner(
    die_w_mm: float,
    die_h_mm: float,
    chip_center_x_mm: float,
    chip_center_y_mm: float,
    chip_w_mm: float,
    chip_h_mm: float,
) -> tuple[float, float]:
    """Compute the chip-corner offset (nm) from RFL parameters (mm).

    The RFL file gives, all in mm and all assuming lower-left origins
    (X right, Y up; see plan M2.3 Q&A — conventions confirmed
    2026-05-20):

    * die size ``(die_w, die_h)`` -- equals KLARF ``DiePitch``;
    * the GDS chip's **centre relative to the die centre**
      ``(chip_center_x, chip_center_y)``;
    * the GDS chip's size ``(chip_w, chip_h)``.

    KLARF ``XREL`` / ``YREL`` are measured from the *die corner*, so the
    chip corner relative to the die corner is::

        chip_corner = die_size/2 + chip_centre_offset - chip_size/2

    Feed the result straight into :func:`klarf_to_gds` as
    ``chip_corner_x`` / ``chip_corner_y``.

    Returns:
        ``(chip_corner_x, chip_corner_y)`` in nm.
    """
    cc_x = (die_w_mm / 2.0 + chip_center_x_mm - chip_w_mm / 2.0) * MM_TO_NM
    cc_y = (die_h_mm / 2.0 + chip_center_y_mm - chip_h_mm / 2.0) * MM_TO_NM
    return cc_x, cc_y


# ── M2.3: KLARF -> GDS coordinate conversion ─────────────────────────


def klarf_to_gds(
    xrel: Number,
    yrel: Number,
    chip_corner_x: float,
    chip_corner_y: float,
    *,
    flip_y: bool = False,
) -> tuple[Number, Number]:
    """Convert KLARF (die-corner) coordinates to GDS (chip-corner) nm.

    Accepts scalars or numpy arrays for ``xrel`` / ``yrel`` (arrays are
    converted element-wise), so the same call works for a single image
    or a whole KLARF batch.

    Args:
        xrel, yrel: KLARF coordinates relative to the die corner (nm).
        chip_corner_x, chip_corner_y: chip corner position relative to
            the die corner (nm), from the RFL file.
        flip_y: if True, negate the converted Y. Reserved for the case
            where a real measurement shows KLARF and GDS disagree on Y
            direction (see plan M2.3 / risk 6); default keeps them
            aligned.

    Returns:
        ``(gds_x, gds_y)`` in chip-corner nm.
    """
    gds_x = xrel - chip_corner_x
    gds_y = yrel - chip_corner_y
    if flip_y:
        gds_y = -gds_y
    return gds_x, gds_y


# ── M2.2: FOV bbox filtering ─────────────────────────────────────────


def fov_bounds(cx: float, cy: float, fov_w: float, fov_h: float
               ) -> tuple[float, float, float, float]:
    """Return the FOV's axis-aligned bounds ``(x_min, y_min, x_max,
    y_max)`` from its centre and size (all nm)."""
    hw = fov_w / 2.0
    hh = fov_h / 2.0
    return (cx - hw, cy - hh, cx + hw, cy + hh)


def _overlap_mask(bboxes: np.ndarray,
                  x_min: float, y_min: float,
                  x_max: float, y_max: float) -> np.ndarray:
    """Boolean mask of rows in ``bboxes`` (N, 4 as x1,y1,x2,y2) whose
    AABB overlaps the FOV. Tolerates rows where x1>x2 / y1>y2 by
    normalising first, so it doesn't matter whether the caller stored
    corners min-first."""
    if bboxes.size == 0:
        return np.zeros((0,), dtype=bool)
    bx1 = np.minimum(bboxes[:, 0], bboxes[:, 2])
    by1 = np.minimum(bboxes[:, 1], bboxes[:, 3])
    bx2 = np.maximum(bboxes[:, 0], bboxes[:, 2])
    by2 = np.maximum(bboxes[:, 1], bboxes[:, 3])
    return (bx1 <= x_max) & (bx2 >= x_min) & (by1 <= y_max) & (by2 >= y_min)


def query_fov(
    cx: float,
    cy: float,
    fov_w: float,
    fov_h: float,
    bboxes: np.ndarray,
) -> np.ndarray:
    """Return the rows of ``bboxes`` (N, 4) whose AABB overlaps the FOV.

    ``bboxes`` rows are ``(x1, y1, x2, y2)`` in GDS nm (chip-corner
    coordinates). Returns an ``(M, 4)`` view-copy with ``M <= N``;
    empty ``(0, 4)`` when nothing overlaps. Pure numpy broadcast, no
    spatial index.
    """
    arr = np.asarray(bboxes)
    if arr.ndim != 2 or arr.shape[1] != 4:
        raise ValueError(f"bboxes must be (N, 4); got {arr.shape}")
    x_min, y_min, x_max, y_max = fov_bounds(cx, cy, fov_w, fov_h)
    mask = _overlap_mask(arr, x_min, y_min, x_max, y_max)
    return arr[mask]


def fov_overlap_indices(
    cx: float,
    cy: float,
    fov_w: float,
    fov_h: float,
    bboxes: np.ndarray,
) -> np.ndarray:
    """Return the integer row indices of ``bboxes`` (N, 4) overlapping
    the FOV. Useful when the caller needs to select *parallel* data
    (e.g. the polygon list that matches each bbox row), not just the
    bboxes themselves. Empty ``(0,)`` int array when nothing overlaps.
    """
    arr = np.asarray(bboxes)
    if arr.size == 0:
        return np.empty((0,), dtype=np.intp)
    if arr.ndim != 2 or arr.shape[1] != 4:
        raise ValueError(f"bboxes must be (N, 4); got {arr.shape}")
    x_min, y_min, x_max, y_max = fov_bounds(cx, cy, fov_w, fov_h)
    return np.flatnonzero(_overlap_mask(arr, x_min, y_min, x_max, y_max))


def query_fov_multi(
    cx: float,
    cy: float,
    fov_w: float,
    fov_h: float,
    layers: Mapping[LayerKey, np.ndarray],
    keys: Optional[Iterable[LayerKey]] = None,
) -> dict[LayerKey, np.ndarray]:
    """Query several layers at once.

    Args:
        layers: mapping ``(layer, datatype) -> bboxes (N, 4)``.
        keys: which layer keys to query; ``None`` means every key in
            ``layers``.

    Returns:
        ``{key: (M, 4) ndarray}`` for each requested key present in
        ``layers``. Keys absent from ``layers`` are skipped silently.
    """
    if keys is None:
        keys = list(layers.keys())
    out: dict[LayerKey, np.ndarray] = {}
    for k in keys:
        arr = layers.get(k)
        if arr is None:
            continue
        out[k] = query_fov(cx, cy, fov_w, fov_h, arr)
    return out


def query_fov_klarf(
    xrel: float,
    yrel: float,
    chip_corner_x: float,
    chip_corner_y: float,
    fov_w: float,
    fov_h: float,
    layers: Mapping[LayerKey, np.ndarray],
    keys: Optional[Iterable[LayerKey]] = None,
    *,
    flip_y: bool = False,
) -> dict[LayerKey, np.ndarray]:
    """FOV query driven by KLARF coordinates.

    Converts the image's ``(xrel, yrel)`` die-corner coordinates to
    chip-corner GDS nm via :func:`klarf_to_gds`, then uses that as the
    FOV centre for :func:`query_fov_multi`. This is the entry point the
    GUI uses when the user clicks a SEM image in the list.
    """
    cx, cy = klarf_to_gds(xrel, yrel, chip_corner_x, chip_corner_y,
                          flip_y=flip_y)
    return query_fov_multi(cx, cy, fov_w, fov_h, layers, keys)
