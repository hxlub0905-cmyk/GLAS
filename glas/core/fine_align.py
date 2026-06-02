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

import numpy as np

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None  # rasterize_layer falls back to a pure-numpy scanline fill

import gds_boolean
import oasis_random


# ── F13: batch re-run + mask-export decision helpers ─────────────────────────
# Qt-free pure logic, shared by the app's OverlayExportWorker / re-run wiring and
# unit-tested directly (the app module needs PyQt6, this one does not).

# Manifest column order for OverlayExportWorker (F5 M6 + F13 ``mask_png``).
# ``mask_png`` is appended last so any index-based reader of the older columns is
# unaffected; it carries the per-image GDS mask filename (blank when no mask was
# written for that image).
OVERLAY_MANIFEST_COLS = [
    "image_id", "raw_png", "overlay_png",
    "fine_dx_nm", "fine_dy_nm", "score", "status", "mask_png",
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
    res = oasis_random.walk_roi(rar, root, roi_bbox, layer, datatype,
                                cancel_cb=cancel_cb)
    polys = [np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], dtype=np.float64)
             for x1, y1, x2, y2 in res["rects"].tolist()]
    polys += [np.asarray(p, dtype=np.float64) for p in res["polys"]]
    return polys


def poi_polys_for_roi(rar, root, roi_bbox, poi_spec, cancel_cb=None):
    """POI polygons (nm, root coords) for a given ROI, for batch fine align
    (plan M4b "Run all"). ``poi_spec`` is ``('raw', layer, datatype)`` or
    ``('expr', expr_text, bindings[, recipes])``; the latter walks each bound
    layer over the ROI and evaluates the Boolean expression, resolving any
    nested synthetic references via ``recipes`` (``{name: (expr, bindings)}``)."""
    kind = poi_spec[0]
    if kind == "raw":
        _, layer, datatype = poi_spec
        return _walk_roi_polys(rar, root, roi_bbox, layer, datatype, cancel_cb)
    # expression POI
    expr, bindings = poi_spec[1], poi_spec[2]
    recipes = poi_spec[3] if len(poi_spec) > 3 else {}
    x0, y0, x1, y1 = roi_bbox
    cx, cy, w, h = (x0 + x1) / 2.0, (y0 + y1) / 2.0, (x1 - x0), (y1 - y0)

    def raw_provider(layer: int, datatype: int):
        ps = _walk_roi_polys(rar, root, roi_bbox, layer, datatype, cancel_cb)
        return gds_boolean.polys_to_geometry(ps)

    geom = gds_boolean.resolve_expression(
        expr, bindings, raw_provider=raw_provider,
        recipe_provider=lambda n: recipes.get(n),
        fov_bbox=gds_boolean.fov_box(cx, cy, w, h))
    return gds_boolean.geometry_to_polygons(geom)


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
    poi_layers = []
    for spec, fg in poi_specs:
        polys = poi_polys_for_roi(rar, root, roi, spec, cancel_cb=cancel_is_set)
        if polys:
            poi_layers.append((polys, fg))
    if not poi_layers:
        return (str(image_id), 0.0, 0.0, 0.0, 0, "flat")
    template = render_composite_template(
        poi_layers, anchor, W, H, nm_per_px, c["bg_glv"], c["blur_sigma_px"])
    radius_px = c["search_radius_nm"] / nm_per_px
    dx, dy, score, used_r = fine_align_one(sem, template, nm_per_px, radius_px)
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


def _pool_init(path, wanted_layers, dtype, bbox_layer, root, poi_specs, cfg):
    """ProcessPoolExecutor initializer: build the per-process reader + cache the
    immutable batch context. Runs once in each worker process."""
    _G["rar"] = oasis_random.RandomAccessReader(
        path, wanted_layers=wanted_layers, dtype=dtype, bbox_layer=bbox_layer)
    _G["root"] = root
    _G["specs"] = poi_specs
    _G["cfg"] = cfg


def _never_cancel() -> bool:
    return False


def _pool_task(job):
    """ProcessPoolExecutor task: fine-align one image using this process's
    private reader. Cancellation is handled by the orchestrator dropping
    not-yet-started futures, so the task itself never checks a flag."""
    return _fine_align_image(job, _G["rar"], _G["root"], _G["specs"],
                             _G["cfg"], _never_cancel)
