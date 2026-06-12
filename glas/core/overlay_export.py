"""Qt-free per-image overlay / mask / raw PNG export compute (F14).

Extracted from ``glas/app/gds_align_tool.py`` so the batch image export can run
inside a ``ProcessPoolExecutor`` worker process — exactly like the fine-align
compute moved to :mod:`fine_align` in F8. ``OverlayExportWorker`` was a single
sequential reader; this module lets each worker process rebuild its own reader
and write its own PNGs in parallel.

Depends only on numpy / cv2 and the sibling ``fine_align`` (which owns the
shapely / ``gds_boolean`` raster) / ``oasis_random`` engines — no PyQt6 — so a
spawn worker can
re-import it cheaply. ``overlay_outlines_on_sem`` lived in the app module before
F14; it moved here (it only ever needed numpy/cv2) and the app re-imports it for
the template preview.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None

import fine_align
import oasis_random


def _safe_name(s: str) -> str:
    """Filesystem-safe basename from an image id (F5 M6)."""
    out = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in str(s))
    return out or "image"


def _draw_polyline_np(rgb: np.ndarray, pts: np.ndarray, color: tuple) -> None:
    """Stroke a closed polyline into an RGB array (numpy fallback for the
    overlay helper when cv2 is unavailable)."""
    H, W = rgb.shape[:2]
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        xs = np.linspace(x0, x1, steps).round().astype(int)
        ys = np.linspace(y0, y1, steps).round().astype(int)
        m = (xs >= 0) & (xs < W) & (ys >= 0) & (ys < H)
        rgb[ys[m], xs[m]] = color


def overlay_outlines_on_sem(sem_gray: np.ndarray, entries: list, anchor: tuple,
                            nm_per_px: float, thickness: int = 1) -> np.ndarray:
    """Draw layer outlines over a SEM frame, returning an (H, W, 3) uint8 RGB.

    The SEM grayscale is widened to grey RGB, then each entry's polygon
    *outlines* are stroked in its colour. ``entries`` is ``[(polygons, (r,g,b)),
    ...]`` with polygons as (N, 2) nm arrays. The FOV is centred on ``anchor``
    at ``nm_per_px``, mirroring :func:`fine_align.rasterize_layer`'s mapping (X
    right, Y flipped to screen convention) so the outlines land on the SEM
    structure for a given coarse/refined anchor. Self-contained raster — it does
    not touch the SemViewer screen drawing (F5 M1). Used by both the before/after
    template preview and the overlay PNG export."""
    H, W = sem_gray.shape[:2]
    rgb = np.repeat(sem_gray.astype(np.uint8)[:, :, None], 3, axis=2).copy()
    gx, gy = anchor
    x0 = gx - W / 2.0 * nm_per_px
    y1 = gy + H / 2.0 * nm_per_px
    for polygons, color in entries:
        col = (int(color[0]), int(color[1]), int(color[2]))
        for poly in polygons:
            arr = np.asarray(poly, dtype=np.float64)
            if arr.shape[0] < 2:
                continue
            px = (arr[:, 0] - x0) / nm_per_px
            py = (y1 - arr[:, 1]) / nm_per_px
            pts = np.stack([px, py], axis=1)
            if cv2 is not None:
                ip = pts.round().astype(np.int32)
                cv2.polylines(rgb, [ip], isClosed=True, color=col,
                              thickness=max(1, thickness),
                              lineType=cv2.LINE_AA)
            else:
                _draw_polyline_np(rgb, pts, col)
    return rgb


def export_one_image(job, rar, root, poi, cfg, out_dir,
                     export_raw: bool, export_overlay: bool,
                     export_gray: bool = False, export_label: bool = False,
                     score_thr: float = 0.0, cancel_cb=None):
    """Write the requested PNGs for ONE image and return its manifest row.

    ``job`` is ``(image_id, coarse|None, refined|None, path, exists)``; ``poi``
    is ``[(spec, (r,g,b), fg_glv), ...]`` (the colour strokes the overlay, the
    ``fg_glv`` paints the grayscale; the POI's 1-based position is its label id).
    One ROI walk per POI feeds the overlay (exterior rings) and the
    grayscale/label (hole-preserving geometry, F15). Pure per-image work with no
    shared state — identical whether run in-thread or across the process pool —
    so the parallel output matches the sequential output (§7)."""
    image_id, coarse, refined, path, exists = job
    out_dir = Path(out_dir)
    c = cfg
    row = {
        "image_id": str(image_id), "raw_png": "", "overlay_png": "",
        "gray_png": "", "label_png": "",
        "fine_dx_nm": "" if refined is None else round(refined[0], 3),
        "fine_dy_nm": "" if refined is None else round(refined[1], 3),
        "score": "" if refined is None else round(refined[2], 6),
        "status": "ok" if refined is not None else "not-run",
    }
    sem = (cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
           if (cv2 and exists) else None)
    if sem is None:
        row["status"] = "missing-file"
        return row
    base = _safe_name(image_id)
    if export_raw:
        name = f"{base}_raw.png"
        cv2.imwrite(str(out_dir / name), sem)
        row["raw_png"] = name
    # overlay, grayscale and label all consume one ROI walk per image.
    if (export_overlay or export_gray or export_label) and coarse is not None \
            and poi:
        H, W = sem.shape[:2]
        nm_per_px = (c["nm_manual"] if (not c["nm_auto"] and
                     c["nm_manual"] > 0) else c["fov_w"] / max(1, W))
        if nm_per_px > 0:
            roi = (coarse[0] - c["fov_w"], coarse[1] - c["fov_h"],
                   coarse[0] + c["fov_w"], coarse[1] + c["fov_h"])
            entries = []
            geoms_fgs = []          # [(geom, fg_glv)]  → grayscale
            geoms_ids = []          # [(geom, label_id)] → label map
            tmpl_layers = []        # [(polys, fg_glv)]  → inline-align template
            # P1/M9: share one walk + union + sub-expression cache across this
            # image's POIs (same ROI) so each raw layer is walked / unioned once.
            walk_memo: dict = {}
            raw_geom_memo: dict = {}
            eval_cache: dict = {}
            for idx, (spec, color, fg_glv) in enumerate(poi):
                # ``polys`` (exterior rings) stroke the overlay; ``geom`` keeps
                # Boolean interior holes for the grayscale/label fill (F15).
                polys, geom = fine_align.poi_polys_and_geometry_for_roi(
                    rar, root, roi, spec, cancel_cb=cancel_cb,
                    walk_memo=walk_memo, raw_geom_memo=raw_geom_memo,
                    eval_cache=eval_cache)
                if polys:
                    entries.append((polys, color))
                    tmpl_layers.append((polys, fg_glv))
                if geom is not None and not geom.is_empty:
                    geoms_fgs.append((geom, fg_glv))
                    # label id = POI's 1-based position, so it matches the
                    # manifest ``label_map`` regardless of which layers are empty.
                    geoms_ids.append((geom, idx + 1))
            # F24: fuse fine-align into the export. When ``align_inline`` is set
            # and no refined offset was supplied (no prior Run all), compute the
            # match here from the SAME walk — render the POI template at the
            # coarse anchor, slide it over the SEM, and use the resulting offset
            # for the aligned grayscale / overlay below. This makes the separate
            # Run-all pass (which would re-walk every image) unnecessary, halving
            # the expensive ROI-walk work for the "aligned templates" use case.
            if (c.get("align_inline") and refined is None and tmpl_layers
                    and cv2 is not None):
                tmpl = fine_align.render_composite_template(
                    tmpl_layers, coarse, W, H, nm_per_px,
                    c.get("bg_glv", 80), c.get("blur_sigma_px", 1.0))
                r_px = (c.get("search_radius_nm", 0.0) / nm_per_px
                        if nm_per_px > 0 else 0.0)
                dx, dy, score, _used = fine_align.fine_align_one(
                    sem, tmpl, nm_per_px, r_px)
                refined = (dx, dy, score)
                row["fine_dx_nm"] = round(dx, 3)
                row["fine_dy_nm"] = round(dy, 3)
                row["score"] = round(score, 6)
                row["status"] = "ok"
            anchor = (coarse if refined is None else
                      (coarse[0] + refined[0], coarse[1] + refined[1]))
            if export_overlay and entries:
                rgb = overlay_outlines_on_sem(sem, entries, anchor, nm_per_px)
                name = f"{base}_overlay.png"
                # overlay returns RGB; cv2 writes BGR → flip channels.
                cv2.imwrite(str(out_dir / name), rgb[:, :, ::-1])
                row["overlay_png"] = name
            # F15 (s: F13 Q2): only emit the grayscale / label for images whose
            # fine-align score meets the threshold, so every exported artifact is
            # trustworthy (MMH needs no fallback). Both are rasterised from the
            # same per-layer geometry (fine_align shares the make_mask raster +
            # FOV corner) so they line up pixel-for-pixel.
            gate = fine_align.mask_should_export(refined, score_thr)
            if export_gray and geoms_fgs and gate:
                img = fine_align.render_grayscale_from_geoms(
                    geoms_fgs, anchor, W, H, nm_per_px,
                    c.get("bg_glv", 80), c.get("blur_sigma_px", 1.0))
                name = f"{base}_gray.png"
                cv2.imwrite(str(out_dir / name), img)
                row["gray_png"] = name
            if export_label and geoms_ids and gate:
                lbl = fine_align.render_label_image(
                    geoms_ids, anchor, W, H, nm_per_px)
                name = f"{base}_label.png"
                cv2.imwrite(str(out_dir / name), lbl)
                row["label_png"] = name
    return row


# ── ProcessPool batch entry (F14; mirrors fine_align._pool_init/_pool_task) ──
#
# A RandomAccessReader isn't picklable (mmap + offset index), so each worker
# rebuilds one from the file path + filter, once per process, and stashes it
# here with the immutable export context. SEM frames are read from disk inside
# the worker (the job carries only a path), so no large array is ever pickled.

_GE: dict = {}


def _export_pool_init(path, wanted_layers, dtype, bbox_layer, root, poi, cfg,
                      out_dir, export_raw, export_overlay, export_gray,
                      export_label, score_thr):
    """ProcessPoolExecutor initializer: build the per-process reader + cache the
    immutable export context. Runs once in each worker process."""
    if cv2 is not None:
        try:
            cv2.setNumThreads(1)
        except Exception:  # pragma: no cover - older cv2
            pass
    _GE["rar"] = oasis_random.RandomAccessReader(
        path, wanted_layers=wanted_layers, dtype=dtype, bbox_layer=bbox_layer)
    _GE["root"] = root
    _GE["poi"] = poi
    _GE["cfg"] = cfg
    _GE["out_dir"] = out_dir
    _GE["raw"] = export_raw
    _GE["overlay"] = export_overlay
    _GE["gray"] = export_gray
    _GE["label"] = export_label
    _GE["thr"] = score_thr


def _export_pool_task(job):
    """ProcessPoolExecutor task: export one image using this process's private
    reader. Cancellation is handled by the orchestrator dropping not-yet-started
    futures, so the task itself never checks a flag."""
    return export_one_image(
        job, _GE["rar"], _GE["root"], _GE["poi"], _GE["cfg"], _GE["out_dir"],
        _GE["raw"], _GE["overlay"], _GE["gray"], _GE["label"], _GE["thr"])
