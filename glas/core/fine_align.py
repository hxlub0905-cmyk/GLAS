"""Qt-free fine-alignment compute (F8).

Extracted verbatim from ``glas/app/gds_align_tool.py`` so the per-image batch
work can run inside a ``ProcessPoolExecutor`` worker process. The functions are
unchanged — only their home moved — so every result stays byte/value-identical
to the in-app path (§7 / F6 "no functional change").

Why a separate module: on Windows (and any ``spawn`` start method) each pool
worker *re-imports* the module that hosts the task function. The app module
pulls in PyQt6 and builds widgets, which a worker process must never do; this
module depends only on numpy / cv2 / shapely (via ``gds_boolean``) and the
sibling ``oasis_random`` reader, so it imports cheaply and Qt-free.

Layer rasterization, template synthesis and ``cv2.matchTemplate`` matching all
live here. The GUI-only overlay-stroking helper (``overlay_outlines_on_sem``)
stays in the app module.
"""
from __future__ import annotations

import contextlib
import multiprocessing as mp
import os
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None  # rasterize_layer falls back to a pure-numpy scanline fill

import devlog
import gds_boolean
import oasis_random


# ── F13: batch re-run + mask-export decision helpers ─────────────────────────
# Qt-free pure logic, shared by the app's OverlayExportWorker / re-run wiring and
# unit-tested directly (the app module needs PyQt6, this one does not).

# Manifest column order for OverlayExportWorker (F5 M6 + F15).
# ``gray_png`` / ``label_png`` are appended last (replacing F13's ``mask_png``):
# the per-image simulated-GLV grayscale and integer ROI label map filenames
# (blank when not written for that image). Older raw/overlay readers that index
# the leading columns are unaffected.
OVERLAY_MANIFEST_COLS = [
    "image_id", "raw_png", "overlay_png",
    "fine_dx_nm", "fine_dy_nm", "score", "status", "gray_png", "label_png",
    # ``label_view_png`` is a human-viewable colourised preview of ``label_png``
    # (F24): the integer ``label_png`` looks all-black in a viewer because its
    # pixel values are tiny label ids (1, 2, …). The preview paints each id with
    # its POI colour so an operator can eyeball the ROIs; the exact integer
    # ``label_png`` stays the machine contract (``label == id``) so downstream
    # ``gray[label == id]`` is unaffected.
    "label_view_png",
]


def rerun_should_overwrite(old_refined, new_score: float, status: str) -> bool:
    """F13 Q1=C: a batch re-run only replaces an image's stored alignment when
    the new run is *strictly better*, so re-running can never make results worse.

    ``old_refined`` is the existing ``(dx, dy, score)`` tuple (or ``None`` when
    the image had no prior result); ``status`` is the worker's objective status.
    A non-"ok" re-run never clobbers a prior result.
    """
    if status != "ok":
        return False
    if old_refined is None:
        return True
    return new_score > old_refined[2]


def mask_should_export(refined, threshold: float) -> bool:
    """F13 Q2: a per-image GDS mask is written only for images that were
    fine-aligned (``refined`` is not ``None``) *and* whose score meets the
    threshold, so every exported mask is trustworthy (MMH needs no fallback).
    ``refined`` is ``(dx, dy, score)`` or ``None``."""
    return refined is not None and refined[2] >= threshold


def rerun_image_subset(images, image_ids):
    """Pick the image objects whose ``image_id`` is in ``image_ids`` (F13 batch
    re-run of a selected / low-score subset), preserving dataset order."""
    idset = {str(i) for i in image_ids}
    return [im for im in images if str(getattr(im, "image_id", im)) in idset]


def images_needing_fine_align(images, refined):
    """Images with coordinates that have no fine-align result yet (F24).

    "Run all" folds into one-click "Export all": only the un-run images are
    computed, while the images the operator already ran (whose ``image_id`` is in
    ``refined``) reuse their stored offset — fine-align is deterministic, so
    re-running them would just waste time. Images without coordinates can't be
    aligned and are skipped. Dataset order is preserved."""
    done = {str(k) for k in (refined or {})}
    out = []
    for im in images:
        if not getattr(im, "has_coords", False):
            continue
        if str(getattr(im, "image_id", im)) in done:
            continue
        out.append(im)
    return out


def batch_worker_count(override: int = 0, cap: int = 16) -> int:
    """Worker (process) count for batch fine-align / image export (F14).

    An explicit ``override`` (> 0, from the UI) wins; otherwise one worker per
    CPU, capped at ``cap``. The old cap of 8 guarded against cv2's internal
    threads multiplying across the process pool — F14 sets ``cv2.setNumThreads(1)``
    inside each worker (see the pool initializers), so a higher cap no longer
    oversubscribes and many-core boxes are no longer left idle."""
    if override and override > 0:
        return max(1, int(override))
    import os
    return max(1, min(os.cpu_count() or 1, cap))


# ── Rasterization helper (used by Boolean masks / template) ──────────────────


def rasterize_layer(
    polygons: list[np.ndarray],
    bbox_nm: tuple[float, float, float, float],
    nm_per_px: float,
) -> np.ndarray:
    """Rasterize a set of polygons (nm coords) into a uint8 binary mask.

    The mask is sized to the bbox at the requested ``nm_per_px``. Pixels inside
    any polygon are 255, outside are 0. Y is flipped so the resulting image
    follows screen / SEM convention (origin top-left, y increasing downward),
    keeping it directly comparable to a SEM frame after coarse offset.

    Falls back to a pure-numpy scanline fill if cv2 is unavailable.
    """
    x0, y0, x1, y1 = bbox_nm
    w_nm = max(1.0, x1 - x0)
    h_nm = max(1.0, y1 - y0)
    W = max(1, int(round(w_nm / nm_per_px)))
    H = max(1, int(round(h_nm / nm_per_px)))
    mask = np.zeros((H, W), dtype=np.uint8)
    if not polygons:
        return mask

    if cv2 is not None:
        cv_polys: list[np.ndarray] = []
        for poly in polygons:
            px = (poly[:, 0] - x0) / nm_per_px
            # Y flip so image y=0 is at top; nm bbox y0 (low) maps to bottom.
            py = (y1 - poly[:, 1]) / nm_per_px
            pts = np.stack([px, py], axis=1).round().astype(np.int32)
            cv_polys.append(pts)
        cv2.fillPoly(mask, cv_polys, color=255)
        return mask

    # Pure-numpy fallback (slower; only hit when cv2 unavailable in dev env).
    for poly in polygons:
        px = (poly[:, 0] - x0) / nm_per_px
        py = (y1 - poly[:, 1]) / nm_per_px
        _scanline_fill(mask, np.stack([px, py], axis=1))
    return mask


def _scanline_fill(mask: np.ndarray, pts: np.ndarray) -> None:
    """Even-odd scanline polygon fill into ``mask`` (numpy fallback)."""
    H, W = mask.shape[:2]
    n = pts.shape[0]
    if n < 3:
        return
    y_min = max(0, int(np.floor(pts[:, 1].min())))
    y_max = min(H - 1, int(np.ceil(pts[:, 1].max())))
    for y in range(y_min, y_max + 1):
        xs: list[float] = []
        for i in range(n):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % n]
            if (y0 <= y < y1) or (y1 <= y < y0):
                t = (y - y0) / (y1 - y0) if y1 != y0 else 0.0
                xs.append(x0 + t * (x1 - x0))
        xs.sort()
        for j in range(0, len(xs) - 1, 2):
            xs0 = max(0, int(np.floor(xs[j])))
            xs1 = min(W - 1, int(np.ceil(xs[j + 1])))
            if xs1 >= xs0:
                mask[y, xs0:xs1 + 1] = 255


# ── POI template + cv2.matchTemplate fine alignment ──────────────────────────


def make_template(mask: np.ndarray, fg_glv: int = 200, bg_glv: int = 80,
                  blur_sigma_px: float = 1.0) -> np.ndarray:
    """Synthesize a SEM-like template from a binary POI ``mask``: structure
    pixels get ``fg_glv``, background ``bg_glv``, then an optional Gaussian
    blur softens the edges so it matches a real (band-limited) SEM frame
    (plan M4b)."""
    img = np.where(mask > 0, np.uint8(fg_glv), np.uint8(bg_glv)).astype(np.uint8)
    if blur_sigma_px and blur_sigma_px > 0 and cv2 is not None:
        k = int(max(1, round(blur_sigma_px * 3))) * 2 + 1
        img = cv2.GaussianBlur(img, (k, k), float(blur_sigma_px))
    return img


def _fit_mask(mask: np.ndarray, height_px: int, width_px: int) -> np.ndarray:
    """Clamp/pad a rasterized mask to exactly ``(height_px, width_px)``."""
    if mask.shape == (height_px, width_px):
        return mask
    fixed = np.zeros((height_px, width_px), dtype=np.uint8)
    h = min(mask.shape[0], height_px)
    w = min(mask.shape[1], width_px)
    fixed[:h, :w] = mask[:h, :w]
    return fixed


def render_composite_template(poi_layers: list, anchor: tuple, width_px: int,
                              height_px: int, nm_per_px: float,
                              bg_glv: int = 80,
                              blur_sigma_px: float = 1.0) -> np.ndarray:
    """Composite several POI layers into one SEM-like grey template (plan F3).

    ``poi_layers`` is ``[(polygons, fg_glv), ...]`` — each layer's polygons
    (nm) are rasterized over the FOV centred on ``anchor`` and painted at that
    layer's ``fg_glv`` onto a shared ``bg_glv`` background (later layers paint
    over earlier ones where they overlap). One Gaussian blur softens the edges
    so the result matches a band-limited SEM frame. With a single layer this is
    identical to the old single-POI template."""
    gx, gy = anchor
    half_w = width_px / 2.0 * nm_per_px
    half_h = height_px / 2.0 * nm_per_px
    bbox = (gx - half_w, gy - half_h, gx + half_w, gy + half_h)
    img = np.full((height_px, width_px), np.uint8(bg_glv), dtype=np.uint8)
    for polygons, fg_glv in poi_layers:
        if not polygons:
            continue
        mask = _fit_mask(rasterize_layer(polygons, bbox, nm_per_px),
                         height_px, width_px)
        img[mask > 0] = np.uint8(fg_glv)
    if blur_sigma_px and blur_sigma_px > 0 and cv2 is not None:
        k = int(max(1, round(blur_sigma_px * 3))) * 2 + 1
        img = cv2.GaussianBlur(img, (k, k), float(blur_sigma_px))
    return img


def render_poi_template(polygons: list, anchor: tuple, width_px: int,
                        height_px: int, nm_per_px: float,
                        fg_glv: int = 200, bg_glv: int = 80,
                        blur_sigma_px: float = 1.0) -> np.ndarray:
    """Single-POI template (plan M4b) — thin wrapper over
    :func:`render_composite_template` with one layer."""
    return render_composite_template(
        [(polygons, fg_glv)], anchor, width_px, height_px, nm_per_px,
        bg_glv, blur_sigma_px)


# ── F15: export-time simulated-GLV grayscale + integer ROI label map ─────────
#
# The downstream tool (MMH) wants ONE aligned image to measure on plus ROI info
# it can read fast. We give it a simulated-GLV grayscale (the working image) and
# an integer label map (label == id → that POI layer's exact ROI, a single numpy
# boolean index). Both are painted from the *same* per-layer hole-preserving
# geometry via ``gds_boolean.make_mask`` — so the grayscale and the label map
# agree pixel-for-pixel on region boundaries — using the same FOV corner as the
# F13 mask path (§7: ``y_min`` raised one pixel so ``invert_y`` lands on the same
# SEM pixels as ``overlay_outlines_on_sem`` / ``rasterize_layer``).


def _fov_min_corner(anchor: tuple, width_px: int, height_px: int,
                    nm_per_px: float) -> tuple:
    """Bottom-left GDS ``(x_min_nm, y_min_nm)`` of the FOV centred on ``anchor``
    for :func:`gds_boolean.make_mask`, matching the F13 mask-export convention
    (y raised one pixel; §7)."""
    x_min = anchor[0] - width_px / 2.0 * nm_per_px
    y_min = anchor[1] - (height_px / 2.0 - 1.0) * nm_per_px
    return x_min, y_min


def render_label_image(poi_geoms_ids: list, anchor: tuple, width_px: int,
                       height_px: int, nm_per_px: float) -> np.ndarray:
    """Integer ROI label map (F15). ``poi_geoms_ids`` is ``[(geom, label_id),
    ...]``; each layer's hole-preserving geometry is rasterised (via
    :func:`gds_boolean.make_mask`) onto a 0 background and painted with its
    integer ``label_id`` — **no blur**, so ``label == id`` is the exact ROI for
    that POI layer (a single numpy boolean index downstream). Later layers
    overwrite earlier ones where they overlap. uint8 → up to 255 layers."""
    lbl = np.zeros((height_px, width_px), dtype=np.uint8)
    x_min, y_min = _fov_min_corner(anchor, width_px, height_px, nm_per_px)
    for geom, label_id in poi_geoms_ids:
        if geom is None or getattr(geom, "is_empty", False):
            continue
        m = gds_boolean.make_mask(geom, width_px=width_px, height_px=height_px,
                                  x_min_nm=x_min, y_min_nm=y_min,
                                  nm_per_px=nm_per_px)
        lbl[m > 0] = np.uint8(label_id)
    return lbl


def colorize_label_map(lbl: np.ndarray, id_to_rgb: dict,
                       bg_rgb: tuple = (0, 0, 0)) -> np.ndarray:
    """Human-viewable RGB preview of an integer label map (F24).

    The exact integer ``label_png`` (from :func:`render_label_image`) is the
    machine contract — its pixel value *is* the POI's label id, so it looks
    almost all-black in a viewer (ids are 1, 2, …). This paints each id with a
    distinct visible colour so an operator can eyeball which ROI is which,
    without touching the integer map. ``id_to_rgb`` is ``{label_id: (r, g, b)}``
    (typically the POI overlay colours); ids absent from the map are left at
    ``bg_rgb``. Returns an ``(H, W, 3)`` uint8 RGB array."""
    H, W = lbl.shape[:2]
    rgb = np.empty((H, W, 3), dtype=np.uint8)
    rgb[:, :] = (int(bg_rgb[0]), int(bg_rgb[1]), int(bg_rgb[2]))
    for label_id, color in id_to_rgb.items():
        m = lbl == np.uint8(label_id)
        if m.any():
            rgb[m] = (int(color[0]), int(color[1]), int(color[2]))
    return rgb


def render_grayscale_from_geoms(poi_geoms_fgs: list, anchor: tuple,
                                width_px: int, height_px: int, nm_per_px: float,
                                bg_glv: int = 80,
                                blur_sigma_px: float = 1.0) -> np.ndarray:
    """SEM-like simulated-GLV grayscale (F15) — the hole-preserving sibling of
    :func:`render_composite_template`. ``poi_geoms_fgs`` is ``[(geom, fg_glv),
    ...]``; each layer's hole-preserving geometry is rasterised (via
    :func:`gds_boolean.make_mask`) onto a ``bg_glv`` background, painted at its
    ``fg_glv``, then softened with one Gaussian blur so it resembles a
    band-limited SEM frame. Shares the make_mask raster + FOV corner with
    :func:`render_label_image`, so the grayscale and the label map line up
    pixel-for-pixel on region boundaries."""
    img = np.full((height_px, width_px), np.uint8(bg_glv), dtype=np.uint8)
    x_min, y_min = _fov_min_corner(anchor, width_px, height_px, nm_per_px)
    for geom, fg_glv in poi_geoms_fgs:
        if geom is None or getattr(geom, "is_empty", False):
            continue
        m = gds_boolean.make_mask(geom, width_px=width_px, height_px=height_px,
                                  x_min_nm=x_min, y_min_nm=y_min,
                                  nm_per_px=nm_per_px)
        img[m > 0] = np.uint8(fg_glv)
    if blur_sigma_px and blur_sigma_px > 0 and cv2 is not None:
        k = int(max(1, round(blur_sigma_px * 3))) * 2 + 1
        img = cv2.GaussianBlur(img, (k, k), float(blur_sigma_px))
    return img


def render_gray_and_label_from_geoms(poi_geoms: list, anchor: tuple,
                                     width_px: int, height_px: int,
                                     nm_per_px: float, bg_glv: int = 80,
                                     blur_sigma_px: float = 1.0) -> tuple:
    """Render BOTH the simulated-GLV grayscale and the integer ROI label map
    from a SINGLE :func:`gds_boolean.make_mask` raster per POI geom.

    ``poi_geoms`` is ``[(geom, fg_glv, label_id), ...]``. The default F15/F24
    export emits gray *and* label, and they are rasterised from the same
    hole-preserving geometry with identical FOV corner / size / nm_per_px — so
    :func:`render_grayscale_from_geoms` and :func:`render_label_image` each ran
    ``make_mask`` over the very same geom independently (two ``cv2.fillPoly``
    passes per POI). This computes each per-POI mask once and paints both
    outputs from it: the grayscale gets ``fg_glv`` (then one Gaussian blur), the
    label gets ``label_id`` (crisp, no blur). The result is byte-identical to
    the two separate renderers and *strengthens* the F15 pixel-for-pixel
    invariant — a single shared raster cannot drift between gray and label.
    Returns ``(gray, label)``."""
    img = np.full((height_px, width_px), np.uint8(bg_glv), dtype=np.uint8)
    lbl = np.zeros((height_px, width_px), dtype=np.uint8)
    x_min, y_min = _fov_min_corner(anchor, width_px, height_px, nm_per_px)
    for geom, fg_glv, label_id in poi_geoms:
        if geom is None or getattr(geom, "is_empty", False):
            continue
        m = gds_boolean.make_mask(geom, width_px=width_px, height_px=height_px,
                                  x_min_nm=x_min, y_min_nm=y_min,
                                  nm_per_px=nm_per_px)
        sel = m > 0
        img[sel] = np.uint8(fg_glv)
        lbl[sel] = np.uint8(label_id)
    if blur_sigma_px and blur_sigma_px > 0 and cv2 is not None:
        k = int(max(1, round(blur_sigma_px * 3))) * 2 + 1
        img = cv2.GaussianBlur(img, (k, k), float(blur_sigma_px))
    return img, lbl


def _parabola_subpx(res: np.ndarray, bx: int, by: int, axis: int) -> float:
    """Sub-pixel peak offset (∈ [-1, 1]) from a 3-point parabola fit around
    the score-map peak along ``axis`` (0 = x, 1 = y)."""
    h, w = res.shape
    if axis == 0:
        if bx <= 0 or bx >= w - 1:
            return 0.0
        a, b, c = float(res[by, bx - 1]), float(res[by, bx]), float(res[by, bx + 1])
    else:
        if by <= 0 or by >= h - 1:
            return 0.0
        a, b, c = float(res[by - 1, bx]), float(res[by, bx]), float(res[by + 1, bx])
    denom = a - 2.0 * b + c
    if denom == 0.0:
        return 0.0
    off = 0.5 * (a - c) / denom
    return off if abs(off) <= 1.0 else 0.0


def fine_align_one(sem_img: np.ndarray, template_full: np.ndarray,
                   nm_per_px: float, search_radius_px: float) -> tuple:
    """Refine the SEM↔GDS alignment by template matching (plan M4b).

    ``template_full`` is the synthetic POI rendered at the *expected* (coarse)
    position, the same size as ``sem_img``. Its centre is cropped (leaving a
    ``search_radius_px`` border) and slid over the SEM with
    ``TM_CCOEFF_NORMED``; the peak's displacement from the centred position is
    the residual misalignment. Returns ``(dx_nm, dy_nm, score, used_radius_px)``
    where ``(dx_nm, dy_nm)`` is the correction to add to the overlay anchor so
    the GDS lands on the SEM structure."""
    if cv2 is None:
        raise RuntimeError("opencv (cv2) is required for fine alignment")
    H, W = sem_img.shape[:2]
    if template_full.shape[:2] != (H, W):
        raise ValueError("template must match the SEM image size")
    r = int(round(search_radius_px))
    r = max(1, min(r, (min(H, W) - 1) // 2))
    tmpl = np.ascontiguousarray(template_full[r:H - r, r:W - r])
    sem = np.ascontiguousarray(sem_img)
    if tmpl.size == 0 or float(tmpl.std()) < 1e-6 or float(sem.std()) < 1e-6:
        return 0.0, 0.0, 0.0, r          # flat template/image → no signal
    res = cv2.matchTemplate(sem.astype(np.uint8), tmpl.astype(np.uint8),
                            cv2.TM_CCOEFF_NORMED)
    _, maxv, _, maxloc = cv2.minMaxLoc(res)
    bx, by = int(maxloc[0]), int(maxloc[1])
    sx = _parabola_subpx(res, bx, by, 0)
    sy = _parabola_subpx(res, bx, by, 1)
    ex = (bx + sx) - r            # SEM structure offset from GDS, +x = right
    ey = (by + sy) - r            # +y = down (image row)
    # Anchor correction: move the overlay onto the SEM structure. Image x is
    # right, GDS x is right (so anchor.x decreases to shift right); image y is
    # down, GDS y is up (so anchor.y increases to shift down).
    return (-ex * nm_per_px, ey * nm_per_px, float(maxv), r)


def _walk_roi_polys(rar, root, roi_bbox, layer, datatype, cancel_cb=None):
    """Walk one layer's ROI geometry into a list of polygon ndarrays (nm)."""
    # F27 M3/M4: walk_roi_fast uses the native subtree walk when the graph is
    # native-able (small/viable chip), else the byte-identical pure-Python
    # BATCHED walk (fast on the big dense-leaf production chip too).
    res = oasis_random.walk_roi_fast(rar, root, roi_bbox, layer, datatype,
                                     cancel_cb=cancel_cb)
    polys = [np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], dtype=np.float64)
             for x1, y1, x2, y2 in res["rects"].tolist()]
    polys += [np.asarray(p, dtype=np.float64) for p in res["polys"]]
    return polys


def poi_polys_and_geometry_for_roi(rar, root, roi_bbox, poi_spec,
                                   cancel_cb=None):
    """POI outline polygons *and* the hole-preserving resolved geometry for a
    ROI, from a single walk (F13). ``poi_spec`` is ``('raw', layer, datatype)``
    or ``('expr', expr_text, bindings[, recipes])``.

    ``poi_polys_for_roi`` returns only the flattened exterior rings
    (``geometry_to_polygons`` drops interior holes), which is fine for stroking
    overlay outlines but would *fill* Boolean interior exclusions (subtraction /
    complement) once rasterised into a mask. The mask path therefore needs the
    geometry, not the rings. Returns ``(polys, geom)`` so one ROI walk feeds
    both the overlay (polys) and the mask (geom)."""
    # F27 M4 diagnosis: split the "walk" phase into oasis-walk / shapely-union /
    # boolean-morphology so the export timing shows WHERE the time goes (on a
    # dense FOV the shapely Boolean/morphology, not the geometry walk, dominates).
    def _acc(attr, dt):
        setattr(rar, attr, getattr(rar, attr, 0.0) + dt)

    kind = poi_spec[0]
    if kind == "raw":
        _, layer, datatype = poi_spec
        _t = time.perf_counter()
        polys = _walk_roi_polys(rar, root, roi_bbox, layer, datatype, cancel_cb)
        _acc("_t_bwalk", time.perf_counter() - _t)
        _t = time.perf_counter()
        geom = gds_boolean.polys_to_geometry(polys)
        _acc("_t_bunion", time.perf_counter() - _t)
        return polys, geom
    # expression POI
    expr, bindings = poi_spec[1], poi_spec[2]
    recipes = poi_spec[3] if len(poi_spec) > 3 else {}
    x0, y0, x1, y1 = roi_bbox
    cx, cy, w, h = (x0 + x1) / 2.0, (y0 + y1) / 2.0, (x1 - x0), (y1 - y0)

    def raw_provider(layer: int, datatype: int):
        _t = time.perf_counter()
        ps = _walk_roi_polys(rar, root, roi_bbox, layer, datatype, cancel_cb)
        _acc("_t_bwalk", time.perf_counter() - _t)
        _t = time.perf_counter()
        g = gds_boolean.polys_to_geometry(ps)
        _acc("_t_bunion", time.perf_counter() - _t)
        return g

    _w0 = getattr(rar, "_t_bwalk", 0.0); _u0 = getattr(rar, "_t_bunion", 0.0)
    _t = time.perf_counter()
    geom = gds_boolean.resolve_expression(
        expr, bindings, raw_provider=raw_provider,
        recipe_provider=lambda n: recipes.get(n),
        fov_bbox=gds_boolean.fov_box(cx, cy, w, h))
    # morphology/boolean = resolve total minus the walk+union it triggered
    _acc("_t_bmorph", (time.perf_counter() - _t)
         - (getattr(rar, "_t_bwalk", 0.0) - _w0)
         - (getattr(rar, "_t_bunion", 0.0) - _u0))
    _t = time.perf_counter()
    out = gds_boolean.geometry_to_polygons(geom)
    _acc("_t_bunion", time.perf_counter() - _t)
    return out, geom


def poi_polys_for_roi(rar, root, roi_bbox, poi_spec, cancel_cb=None):
    """POI polygons (nm, root coords) for a given ROI, for batch fine align
    (plan M4b "Run all") and overlay outlines. ``poi_spec`` is
    ``('raw', layer, datatype)`` or ``('expr', expr_text, bindings[, recipes])``;
    the latter walks each bound layer over the ROI and evaluates the Boolean
    expression, resolving any nested synthetic references via ``recipes``
    (``{name: (expr, bindings)}``). For the hole-preserving geometry (mask
    export) use :func:`poi_polys_and_geometry_for_roi`."""
    # F23 batch-perf: a raw POI only needs the outline polygons here — the
    # template rasterizer fills overlapping polys idempotently, so it does NOT
    # need the unioned geometry. poi_polys_and_geometry_for_roi would compute a
    # unary_union (30 ms–1 s on a dense FOV) only for us to drop it via [0]; the
    # raw walk result is returned directly instead. Expression POIs still go
    # through the full path because their Boolean evaluation needs the geometry.
    if poi_spec[0] == "raw":
        _, layer, datatype = poi_spec
        return _walk_roi_polys(rar, root, roi_bbox, layer, datatype, cancel_cb)
    return poi_polys_and_geometry_for_roi(
        rar, root, roi_bbox, poi_spec, cancel_cb)[0]


# ── Optional per-stage timing (diagnostic) ───────────────────────────────────
# Set GLAS_FA_TIMING=1 before launching to have each batch worker accumulate
# per-image stage times and print a running per-worker average every
# GLAS_FA_TIMING_EVERY images (default 25). Stages: read (cv2.imread), poi
# (ROI walk + any Boolean/union), template (rasterize + blur), match
# (cv2.matchTemplate). Lets us see where a real batch actually spends its time
# instead of guessing. Off → the four perf_counter() calls are the only cost
# (nanoseconds), so the production path is unaffected.
_FA_TIMING = bool(os.environ.get("GLAS_FA_TIMING"))
try:
    _FA_TIMING_EVERY = max(1, int(os.environ.get("GLAS_FA_TIMING_EVERY", "25")))
except ValueError:
    _FA_TIMING_EVERY = 25
_FA_TIMING_ACC = {"read": 0.0, "poi": 0.0, "template": 0.0, "match": 0.0,
                  "n": 0}


def _record_timing(read_s: float, poi_s: float, tmpl_s: float,
                   match_s: float) -> None:
    a = _FA_TIMING_ACC
    a["read"] += read_s
    a["poi"] += poi_s
    a["template"] += tmpl_s
    a["match"] += match_s
    a["n"] += 1
    n = a["n"]
    # Print after the very first image (so even a tiny batch — fewer images than
    # workers — yields a line per worker), then every _FA_TIMING_EVERY. The n==1
    # line includes that worker's cold ROI walk (memo not yet warm); later lines
    # are the steady-state average.
    if n == 1 or n % _FA_TIMING_EVERY == 0:
        read_ms, poi_ms = a["read"] / n * 1e3, a["poi"] / n * 1e3
        tmpl_ms, match_ms = a["template"] / n * 1e3, a["match"] / n * 1e3
        # Bold-highlight whichever of the two heavy stages (poi / match)
        # dominates, so the bottleneck pops out of the line at a glance.
        poi_s = f"poi(walk+bool)={poi_ms:.1f}"
        match_s = f"match={match_ms:.1f}"
        if poi_ms >= match_ms:
            poi_s = devlog.paint(poi_s, "fa-timing", bold=True)
        else:
            match_s = devlog.paint(match_s, "fa-timing", bold=True)
        print(f"{devlog.tag('fa-timing')} "
              f"{devlog.dim(f'pid={os.getpid()} n={n}')}  "
              f"read={read_ms:.1f}  {poi_s}  "
              f"template={tmpl_ms:.1f}  {match_s}  {devlog.dim('ms/img')}",
              flush=True)


def _fine_align_image(job, rar, root, poi_specs, cfg, cancel_is_set):
    """Process ONE image: walk the POI ROI, render the template, match.

    Pure per-image work with no shared mutable state (``rar`` is the calling
    thread's / process's private reader), so the result is independent of
    execution order — identical whether run sequentially, across a thread pool
    or across a process pool (F6 M3 / F8). Returns
    ``(image_id, dx, dy, score, used_radius_px, status)`` or ``None`` if
    cancelled before any work was done."""
    image_id, anchor, path, exists = job
    if cancel_is_set():
        return None
    c = cfg
    if anchor is None:
        return (str(image_id), 0.0, 0.0, 0.0, 0, "no-coords")
    t0 = time.perf_counter()
    sem = (cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
           if (cv2 and exists) else None)
    if sem is None:
        return (str(image_id), 0.0, 0.0, 0.0, 0, "missing-file")
    H, W = sem.shape[:2]
    nm_per_px = (c["nm_manual"] if (not c["nm_auto"] and
                 c["nm_manual"] > 0) else c["fov_w"] / max(1, W))
    if nm_per_px <= 0:
        return (str(image_id), 0.0, 0.0, 0.0, 0, "no-scale")
    roi = (anchor[0] - c["fov_w"], anchor[1] - c["fov_h"],
           anchor[0] + c["fov_w"], anchor[1] + c["fov_h"])
    t1 = time.perf_counter()
    poi_layers = []
    for spec, fg in poi_specs:
        polys = poi_polys_for_roi(rar, root, roi, spec, cancel_cb=cancel_is_set)
        if polys:
            poi_layers.append((polys, fg))
    if not poi_layers:
        return (str(image_id), 0.0, 0.0, 0.0, 0, "flat")
    t2 = time.perf_counter()
    template = render_composite_template(
        poi_layers, anchor, W, H, nm_per_px, c["bg_glv"], c["blur_sigma_px"])
    radius_px = c["search_radius_nm"] / nm_per_px
    t3 = time.perf_counter()
    dx, dy, score, used_r = fine_align_one(sem, template, nm_per_px, radius_px)
    if _FA_TIMING:
        _record_timing(t1 - t0, t2 - t1, t3 - t2,
                       time.perf_counter() - t3)
    return (str(image_id), dx, dy, score, int(used_r), "ok")


# ── ProcessPool batch entry (F8) ─────────────────────────────────────────────
#
# A batch can run across processes to escape the GIL: OASIS ROI decoding is a
# tight pure-Python loop that holds the GIL, so a thread pool neither speeds it
# up nor leaves the Qt UI thread any GIL time. With processes the decode runs
# truly in parallel and the workers can't touch the GUI's interpreter at all.
#
# A RandomAccessReader is not picklable (it owns an mmap + an offset index), so
# instead of shipping it to each worker we rebuild one *inside* the worker from
# the file path + filter, once per process, and stash it in this module global
# (``_G``). On spawn that build cost is paid once per worker and amortized over
# every image that worker handles. The SEM frames are read from disk inside the
# worker too (the job only carries a path), so no large array is ever pickled.

_G: dict = {}


def _pool_init(path, wanted_layers, dtype, bbox_layer, prebuilt_index=None):
    """ProcessPoolExecutor initializer: build this worker's private reader once.

    F23 M1: ``prebuilt_index`` (the orchestrator's already-built name-table
    index) lets the worker's reader skip the per-process ``scan_cell_offsets``
    rescan — the dominant per-worker build cost on big files.

    F23 M2: only the *reader* is cached here (it depends on file + layer filter,
    which rarely change). The per-batch context (root / POI specs / cfg) rides
    each task instead (see :func:`_pool_task`), so a persistent pool can be
    reused across batches even when the POI selection or search radius changes."""
    # F14: pin cv2 to one thread per worker so matchTemplate's internal threads
    # don't multiply across the process pool (the reason the old cap was 8).
    if cv2 is not None:
        try:
            cv2.setNumThreads(1)
        except Exception:  # pragma: no cover - older cv2
            pass
    # Match the main process: keep [fa-timing] prints from crashing on a legacy
    # console code page (UnicodeEncodeError) by reconfiguring this worker's
    # stdout to UTF-8 with replacement.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover - non-reconfigurable stream
        pass
    _G["rar"] = oasis_random.RandomAccessReader(
        path, wanted_layers=wanted_layers, dtype=dtype, bbox_layer=bbox_layer,
        prebuilt_index=prebuilt_index)


def _never_cancel() -> bool:
    return False


def _pool_task(job, root, poi_specs, cfg):
    """ProcessPoolExecutor task: fine-align one image using this process's
    private reader. The per-batch context (root / specs / cfg) arrives with the
    job so the worker's reader can be reused across batches (F23 M2).
    Cancellation is handled by the orchestrator dropping not-yet-started
    futures, so the task itself never checks a flag."""
    return _fine_align_image(job, _G["rar"], root, poi_specs, cfg,
                             _never_cancel)


def _pool_warm_probe() -> bool:
    """Warm-up task (F23 M2): proves this worker booted *and* built its reader,
    so :meth:`_BatchPool.ensure_warm` can pay the import + reader-build cost
    before the user clicks Run all."""
    return _G.get("rar") is not None


def pool_reader():
    """This worker process's private reader, built once by :func:`_pool_init`
    (F25). The unified align+export task (in ``overlay_export``) reads it from
    here so the fused worker shares the very same warm pool as the fine-align
    path — keyed on reader identity, the per-batch context rides each task."""
    return _G.get("rar")


# Default idle window before a warm-but-unused pool releases its workers (F23
# M2 idle-timeout). Long enough to cover normal review gaps between batches,
# short enough to free the K mmaps + indexes when the user walks away.
_POOL_IDLE_TIMEOUT_S = 300.0


class _BatchPool:
    """A spawn-based process pool for batch fine-align that survives across
    batches and can be pre-warmed (F23 M2), instead of being created and torn
    down on every "Run all".

    Keyed on the reader identity ``(path, layer filter, dtype, bbox layer)`` +
    the worker count: a request whose key matches the live pool reuses it (near
    zero startup); a different key shuts the old pool down and builds a new one.
    The per-batch context (root / POI specs / cfg) is *not* baked into the
    workers — it rides each task — so changing the POI selection or search
    radius still reuses the same warm pool.

    Trade-off: a live pool keeps ``workers`` idle processes, each holding its
    own mmap + name-table index. To bound that, the pool **auto-releases** after
    ``idle_timeout`` seconds with no batch running (set ``idle_timeout=None`` to
    disable). It is also released explicitly via :meth:`shutdown` on app close /
    new-file open; worst case the interpreter's atexit handler reaps it at quit.

    Idle-timeout safety: the timer is only armed while no batch is in flight. A
    run holds the pool via :meth:`lease` (a busy refcount); the timer is
    cancelled on acquire and re-armed only when the refcount falls back to zero,
    so it can never shut workers down mid-batch.

    Thread-safe: pre-warm runs on a background thread while the batch worker
    thread leases the pool, and the idle timer fires on its own thread — an
    ``RLock`` guards all state."""

    def __init__(self, idle_timeout: float | None = None) -> None:
        self._lock = threading.RLock()
        self._ex: ProcessPoolExecutor | None = None
        self._key: tuple | None = None
        self._warmed = False
        self._inuse = 0                       # active leases (batches running)
        self._idle_timeout = idle_timeout
        self._idle_timer: threading.Timer | None = None

    @staticmethod
    def _key_for(path, wanted, dtype, bbox, workers) -> tuple:
        wkey = frozenset(wanted) if wanted is not None else None
        dname = getattr(dtype, "__name__", str(dtype))
        return (str(path), wkey, dname, bbox, int(workers))

    def _get_locked(self, path, wanted, dtype, bbox, prebuilt_index,
                    workers) -> ProcessPoolExecutor:
        """Build or reuse the executor for this key (caller holds the lock)."""
        key = self._key_for(path, wanted, dtype, bbox, workers)
        if self._ex is not None and self._key == key:
            return self._ex
        self._shutdown_locked()
        ctx = mp.get_context("spawn")
        self._ex = ProcessPoolExecutor(
            max_workers=int(workers), mp_context=ctx,
            initializer=_pool_init,
            initargs=(str(path), wanted, dtype, bbox, prebuilt_index))
        self._key = key
        self._warmed = False
        return self._ex

    def get(self, path, wanted, dtype, bbox, prebuilt_index,
            workers) -> ProcessPoolExecutor:
        """A ready executor for this reader / worker count, reusing the live one
        when the key matches, else (re)building it. Low-level builder — the run
        path uses :meth:`lease` (which adds busy/idle bookkeeping) instead."""
        with self._lock:
            return self._get_locked(path, wanted, dtype, bbox, prebuilt_index,
                                    workers)

    @contextlib.contextmanager
    def lease(self, path, wanted, dtype, bbox, prebuilt_index, workers):
        """Context manager yielding a ready executor for the duration of a
        batch. Marks the pool busy (cancelling the idle timer) on enter and
        re-arms the idle timer on exit, so the auto-release can never fire while
        a batch is running."""
        with self._lock:
            self._cancel_idle_locked()
            ex = self._get_locked(path, wanted, dtype, bbox, prebuilt_index,
                                  workers)
            self._inuse += 1
        try:
            yield ex
        finally:
            with self._lock:
                self._inuse = max(0, self._inuse - 1)
                self._arm_idle_locked()

    def ensure_warm(self, path, wanted, dtype, bbox, prebuilt_index,
                    workers) -> None:
        """Build the pool (if needed) and force every worker to boot + build its
        reader now, so a later Run all starts instantly. Idempotent: returns
        immediately when the matching pool is already warm. Safe on a background
        thread; best-effort (a key change before Run all just rebuilds). Held as
        a lease so the idle timer can't fire mid-warm; the timer is (re)armed
        when warming finishes, so a never-used warm pool still auto-releases."""
        key = self._key_for(path, wanted, dtype, bbox, workers)
        with self._lock:
            if self._ex is not None and self._key == key and self._warmed:
                return
            self._cancel_idle_locked()
            ex = self._get_locked(path, wanted, dtype, bbox, prebuilt_index,
                                  workers)
            self._inuse += 1
        try:
            # ProcessPoolExecutor spawns workers lazily as tasks are submitted,
            # so submitting one probe per worker forces all `workers` processes
            # to boot and run _pool_init (the cost we want paid up front).
            futs = [ex.submit(_pool_warm_probe) for _ in range(int(workers))]
            for f in futs:
                f.result()
            with self._lock:
                if self._key == key:
                    self._warmed = True
        finally:
            with self._lock:
                self._inuse = max(0, self._inuse - 1)
                self._arm_idle_locked()

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown_locked()

    # ── idle-timer helpers (all callers hold the lock) ──────────────────────
    def _cancel_idle_locked(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _arm_idle_locked(self) -> None:
        self._cancel_idle_locked()
        if (self._idle_timeout and self._ex is not None
                and self._inuse == 0):
            t = threading.Timer(self._idle_timeout, self._on_idle_fire)
            t.daemon = True
            self._idle_timer = t
            t.start()

    def _on_idle_fire(self) -> None:
        # Fires on the Timer's own thread after idle_timeout of no activity.
        # Only release when still idle — a batch that started meanwhile holds a
        # lease (_inuse > 0) and must not have its workers pulled out.
        with self._lock:
            if self._inuse == 0:
                self._shutdown_locked()

    def _shutdown_locked(self) -> None:
        self._cancel_idle_locked()
        if self._ex is not None:
            self._ex.shutdown(wait=False)
            self._ex = None
            self._key = None
            self._warmed = False


# Session-wide singleton: the GUI's batch worker leases it, a background
# pre-warm primes it once the OASIS + POIs are ready, and an idle timer releases
# its workers when the user stops batching for a while (F23 M2).
batch_pool = _BatchPool(idle_timeout=_POOL_IDLE_TIMEOUT_S)
