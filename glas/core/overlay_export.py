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

import os
import time
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
        "gray_png": "", "label_png": "", "label_view_png": "",
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
            for idx, (spec, color, fg_glv) in enumerate(poi):
                # ``polys`` (exterior rings) stroke the overlay; ``geom`` keeps
                # Boolean interior holes for the grayscale/label fill (F15).
                polys, geom = fine_align.poi_polys_and_geometry_for_roi(
                    rar, root, roi, spec, cancel_cb=cancel_cb,
                    nm_per_px=nm_per_px)
                if polys:
                    entries.append((polys, color))
                if geom is not None and not geom.is_empty:
                    geoms_fgs.append((geom, fg_glv))
                    # label id = POI's 1-based position, so it matches the
                    # manifest ``label_map`` regardless of which layers are empty.
                    geoms_ids.append((geom, idx + 1))
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
            # geoms_fgs / geoms_ids are built in lockstep from the SAME per-POI
            # geom (same order), so when both gray and label are exported the
            # two rasters are identical — render them from ONE make_mask pass per
            # POI instead of two. Byte-identical to the separate renderers (§7:
            # strengthens the F15 gray↔label pixel-for-pixel invariant).
            gray_img = lbl_img = None
            if gate and geoms_fgs and export_gray and export_label:
                geoms_all = [(g, fg, lid)
                             for (g, fg), (_g, lid) in zip(geoms_fgs, geoms_ids)]
                gray_img, lbl_img = fine_align.render_gray_and_label_from_geoms(
                    geoms_all, anchor, W, H, nm_per_px,
                    c.get("bg_glv", 80), c.get("blur_sigma_px", 1.0))
            elif gate and export_gray and geoms_fgs:
                gray_img = fine_align.render_grayscale_from_geoms(
                    geoms_fgs, anchor, W, H, nm_per_px,
                    c.get("bg_glv", 80), c.get("blur_sigma_px", 1.0))
            elif gate and export_label and geoms_ids:
                lbl_img = fine_align.render_label_image(
                    geoms_ids, anchor, W, H, nm_per_px)
            if gray_img is not None:
                name = f"{base}_gray.png"
                cv2.imwrite(str(out_dir / name), gray_img)
                row["gray_png"] = name
            if lbl_img is not None:
                lbl = lbl_img
                name = f"{base}_label.png"
                cv2.imwrite(str(out_dir / name), lbl)
                row["label_png"] = name
                # F24: a human-viewable colourised preview alongside the exact
                # integer label map. The label.png above keeps ``label == id``
                # (machine contract); this paints each id with its POI colour so
                # an operator can see the ROIs instead of an all-black image.
                id_to_rgb = {i + 1: color
                             for i, (_spec, color, _fg) in enumerate(poi)}
                view = fine_align.colorize_label_map(lbl, id_to_rgb)
                vname = f"{base}_label_view.png"
                # colorize returns RGB; cv2 writes BGR → flip channels.
                cv2.imwrite(str(out_dir / vname), view[:, :, ::-1])
                row["label_view_png"] = vname
    return row


def align_and_export_one_image(job, rar, root, poi_colored, cfg, out_dir,
                               export_raw, export_overlay, export_gray,
                               export_label, score_thr, cancel_cb=None):
    """Unified per-image compute (F25): walk the POI ROI **once**, then both
    fine-align (when the image isn't already aligned) AND rasterize every
    requested export product from that single walk — replacing the old
    walk-twice fine-align-pass + export-pass sequence.

    ``job`` is ``(image_id, coarse|None, prior_refined|None, path, exists)``;
    ``poi_colored`` is ``[(spec, (r, g, b), fg_glv), ...]`` (the spec walks the
    ROI, the colour strokes the overlay, the ``fg_glv`` paints the grayscale,
    the POI's 1-based position is its label id).

    Pure per-image work with no shared mutable state, so the result is identical
    whether run in-thread or across the process pool (§7), and **byte-identical**
    to ``fine_align._fine_align_image`` (the refined offset) composed with
    :func:`export_one_image` (the PNGs / manifest row) on the same inputs.

    Returns ``(fa_result, row)``:
      * ``fa_result`` = ``(image_id, dx, dy, score, used_r, status)`` when this
        call freshly fine-aligned the image (``prior_refined`` was ``None``), so
        the orchestrator can stream it into ``_refined`` / the batch panel; it is
        ``None`` when a stored alignment was reused (the orchestrator then leaves
        ``_refined`` untouched, exactly like the F24 two-pass flow).
      * ``row`` = the overlay-manifest row (filenames + dx/dy/score/status).
    """
    image_id, coarse, prior_refined, path, exists = job
    c = cfg
    out_dir = Path(out_dir)
    row = {
        "image_id": str(image_id), "raw_png": "", "overlay_png": "",
        "gray_png": "", "label_png": "", "label_view_png": "",
        "fine_dx_nm": "", "fine_dy_nm": "", "score": "", "status": "not-run",
    }

    def _finish(refined, fa_result):
        if refined is not None:
            row["fine_dx_nm"] = round(refined[0], 3)
            row["fine_dy_nm"] = round(refined[1], 3)
            row["score"] = round(refined[2], 6)
            row["status"] = "ok"
        return fa_result, row

    # F26 diagnostic: one timing line per image (gated on the same
    # GLAS_FA_TIMING / dev-mode switch fine_align uses, so the spawned export
    # pool workers inherit it). Splits each defect's export into read / walk /
    # match / raster and records the worker pid + how many cells THIS image
    # freshly decoded — so a batch shows exactly where its wall-clock goes
    # (e.g. "walk dominates, decode ~0 after the first image" => the bottleneck
    # is the walk/raster, not native decode). Off => the perf_counter calls and
    # the print are skipped entirely.
    _timing = fine_align._FA_TIMING
    _t0 = time.perf_counter() if _timing else 0.0
    _n0 = getattr(rar, "_n_loaded", 0) if _timing else 0
    # Extra walk counters (F26 diagnosis): reachable_bbox entries newly computed
    # this image, cells the walk actually visited, and repetition-instances
    # visited — so a "walk dominates but decode is ~0" line tells us WHICH:
    # bbox recompute (reach_new big), whole-tree traversal (cellvisits big), or
    # repetition blow-up (instances big).
    _r0 = len(getattr(rar, "_reach_memo", ())) if _timing else 0
    _cv0 = getattr(rar, "_walk_cellvisits_total", 0) if _timing else 0
    _iv0 = getattr(rar, "_walk_visited_total", 0) if _timing else 0
    _tp0 = getattr(rar, "_walk_tplace_total", 0.0) if _timing else 0.0
    _tr0 = getattr(rar, "_walk_trect_total", 0.0) if _timing else 0.0
    _tpo0 = getattr(rar, "_walk_tpoly_total", 0.0) if _timing else 0.0
    _bw0 = getattr(rar, "_t_bwalk", 0.0) if _timing else 0.0
    _bu0 = getattr(rar, "_t_bunion", 0.0) if _timing else 0.0
    _bm0 = getattr(rar, "_t_bmorph", 0.0) if _timing else 0.0
    _seg = {"read": 0.0, "walk": 0.0, "match": 0.0, "raster": 0.0}
    _mark = [_t0]

    def _lap(key):
        if _timing:
            now = time.perf_counter()
            _seg[key] = (now - _mark[0]) * 1e3
            _mark[0] = now

    def _emit():
        if not _timing:
            return
        total = (time.perf_counter() - _t0) * 1e3
        n_dec = getattr(rar, "_n_loaded", 0) - _n0
        reach_new = len(getattr(rar, "_reach_memo", ())) - _r0
        cellvisits = getattr(rar, "_walk_cellvisits_total", 0) - _cv0
        instances = getattr(rar, "_walk_visited_total", 0) - _iv0
        tplace = (getattr(rar, "_walk_tplace_total", 0.0) - _tp0) * 1e3
        trect = (getattr(rar, "_walk_trect_total", 0.0) - _tr0) * 1e3
        tpoly = (getattr(rar, "_walk_tpoly_total", 0.0) - _tpo0) * 1e3
        bwalk = (getattr(rar, "_t_bwalk", 0.0) - _bw0) * 1e3
        bunion = (getattr(rar, "_t_bunion", 0.0) - _bu0) * 1e3
        bmorph = (getattr(rar, "_t_bmorph", 0.0) - _bm0) * 1e3
        print(f"[export-timing] pid={os.getpid()} img={image_id}  "
              f"read={_seg['read']:.0f} walk={_seg['walk']:.0f} "
              f"match={_seg['match']:.0f} raster={_seg['raster']:.0f}  "
              f"total={total:.0f}ms  cells_decoded={n_dec} "
              f"reach_new={reach_new} cellvisits={cellvisits} "
              f"instances={instances}  "
              f"[walk: place={tplace:.0f} rect={trect:.0f} poly={tpoly:.0f}]  "
              f"[bool: walk={bwalk:.0f} union={bunion:.0f} morph={bmorph:.0f}]  "
              f"status={row['status']}", flush=True)

    if cancel_cb is not None and cancel_cb():
        return None, row

    need_align = prior_refined is None and coarse is not None
    want_products = export_overlay or export_gray or export_label
    # The SEM frame is only needed to align, to copy the raw PNG, or to size /
    # paint an overlay/gray/label. A reused-alignment CSV-only re-export (or a
    # no-coords image with no products) needs none of it — report the stored
    # offsets from the index alone and never touch the image file, so a missing
    # SEM is not wrongly flagged.
    if not (need_align or export_raw or want_products):
        return _finish(prior_refined, None)

    sem = (cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
           if (cv2 and exists) else None)
    if sem is None:
        row["status"] = "missing-file"
        fa = ((str(image_id), 0.0, 0.0, 0.0, 0, "missing-file")
              if prior_refined is None else None)
        _emit()
        return fa, row

    base = _safe_name(image_id)
    if export_raw:
        name = f"{base}_raw.png"
        cv2.imwrite(str(out_dir / name), sem)
        row["raw_png"] = name

    H, W = sem.shape[:2]
    nm_per_px = (c["nm_manual"] if (not c["nm_auto"] and
                 c["nm_manual"] > 0) else c["fov_w"] / max(1, W))

    _lap("read")
    fa_result = None
    refined = prior_refined

    # The walk is the heavy step — do it only when something needs it: a fresh
    # alignment, or an image product to rasterize. (A pure CSV re-export of
    # already-aligned images therefore never walks.)
    if coarse is not None and nm_per_px > 0 and poi_colored \
            and (need_align or want_products):
        roi = (coarse[0] - c["fov_w"], coarse[1] - c["fov_h"],
               coarse[0] + c["fov_w"], coarse[1] + c["fov_h"])
        # Holes are only needed by the gray/label fill; the template + overlay
        # need exterior rings only, so skip the (raw-POI) unary_union otherwise
        # — preserving the F23 fine-align fast path.
        need_geom = export_gray or export_label
        entries = []          # [(polys, color)]   → overlay outlines
        poi_layers = []       # [(polys, fg_glv)]  → composite template
        geoms_fgs = []        # [(geom, fg_glv)]   → grayscale
        geoms_ids = []        # [(geom, label_id)] → label map
        for idx, (spec, color, fg_glv) in enumerate(poi_colored):
            if need_geom:
                polys, geom = fine_align.poi_polys_and_geometry_for_roi(
                    rar, root, roi, spec, cancel_cb=cancel_cb,
                    nm_per_px=nm_per_px)
            else:
                polys = fine_align.poi_polys_for_roi(
                    rar, root, roi, spec, cancel_cb=cancel_cb,
                    nm_per_px=nm_per_px)
                geom = None
            if polys:
                entries.append((polys, color))
                poi_layers.append((polys, fg_glv))
            if geom is not None and not geom.is_empty:
                geoms_fgs.append((geom, fg_glv))
                geoms_ids.append((geom, idx + 1))

        _lap("walk")
        if need_align:
            if not poi_layers:
                # No geometry in this ROI → nothing to match against.
                _emit()
                return _finish(None, (str(image_id), 0.0, 0.0, 0.0, 0, "flat"))
            template = fine_align.render_composite_template(
                poi_layers, coarse, W, H, nm_per_px,
                c["bg_glv"], c["blur_sigma_px"])
            radius_px = c["search_radius_nm"] / nm_per_px
            dx, dy, score, used_r = fine_align.fine_align_one(
                sem, template, nm_per_px, radius_px)
            fa_result = (str(image_id), dx, dy, score, int(used_r), "ok")
            refined = (dx, dy, score)

        _lap("match")
        if want_products and refined is not None:
            anchor = (coarse[0] + refined[0], coarse[1] + refined[1])
            if export_overlay and entries:
                rgb = overlay_outlines_on_sem(sem, entries, anchor, nm_per_px)
                name = f"{base}_overlay.png"
                cv2.imwrite(str(out_dir / name), rgb[:, :, ::-1])
                row["overlay_png"] = name
            gate = fine_align.mask_should_export(refined, score_thr)
            gray_img = lbl_img = None
            if gate and geoms_fgs and export_gray and export_label:
                geoms_all = [(g, fg, lid)
                             for (g, fg), (_g, lid) in zip(geoms_fgs, geoms_ids)]
                gray_img, lbl_img = fine_align.render_gray_and_label_from_geoms(
                    geoms_all, anchor, W, H, nm_per_px,
                    c.get("bg_glv", 80), c.get("blur_sigma_px", 1.0))
            elif gate and export_gray and geoms_fgs:
                gray_img = fine_align.render_grayscale_from_geoms(
                    geoms_fgs, anchor, W, H, nm_per_px,
                    c.get("bg_glv", 80), c.get("blur_sigma_px", 1.0))
            elif gate and export_label and geoms_ids:
                lbl_img = fine_align.render_label_image(
                    geoms_ids, anchor, W, H, nm_per_px)
            if gray_img is not None:
                name = f"{base}_gray.png"
                cv2.imwrite(str(out_dir / name), gray_img)
                row["gray_png"] = name
            if lbl_img is not None:
                name = f"{base}_label.png"
                cv2.imwrite(str(out_dir / name), lbl_img)
                row["label_png"] = name
                id_to_rgb = {i + 1: color
                             for i, (_spec, color, _fg) in enumerate(poi_colored)}
                view = fine_align.colorize_label_map(lbl_img, id_to_rgb)
                vname = f"{base}_label_view.png"
                cv2.imwrite(str(out_dir / vname), view[:, :, ::-1])
                row["label_view_png"] = vname

    _lap("raster")
    _emit()
    return _finish(refined, fa_result)


# ── ProcessPool batch entry (F14; mirrors fine_align._pool_init/_pool_task) ──
#
# A RandomAccessReader isn't picklable (mmap + offset index), so each worker
# rebuilds one from the file path + filter, once per process, and stashes it
# here with the immutable export context. SEM frames are read from disk inside
# the worker (the job carries only a path), so no large array is ever pickled.

_GE: dict = {}


def _export_pool_init(path, wanted_layers, dtype, bbox_layer, prebuilt_index,
                      root, poi, cfg, out_dir, export_raw, export_overlay,
                      export_gray, export_label, score_thr):
    """ProcessPoolExecutor initializer: build the per-process reader + cache the
    immutable export context. Runs once in each worker process.

    ``prebuilt_index`` is the orchestrator reader's ``index_snapshot()`` over the
    *same file* (F23 M1, now applied to export too): the name-table scan is the
    dominant per-reader build cost on big files, so each worker skips its own
    ``scan_cell_offsets`` rescan. It is plain dict/list/int — picklable across
    the spawn boundary — and ``None`` falls back to a fresh scan."""
    if cv2 is not None:
        try:
            cv2.setNumThreads(1)
        except Exception:  # pragma: no cover - older cv2
            pass
    _GE["rar"] = oasis_random.RandomAccessReader(
        path, wanted_layers=wanted_layers, dtype=dtype, bbox_layer=bbox_layer,
        prebuilt_index=prebuilt_index)
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


# ── F25 unified align+export pool task ───────────────────────────────────────
#
# The fused worker reuses the SAME warm F23 pool as the fine-align path (keyed on
# reader identity; the reader is built once by ``fine_align._pool_init``). Only
# the per-batch context (root / coloured POI specs / cfg / out-dir / export flags
# / threshold) rides each task, so a POI / search-radius / export-option change
# still hits the warm pool. The reader is read back from ``fine_align`` rather
# than re-stashed here, so there is exactly one reader per worker process.

def _afe_pool_task(job, root, poi_colored, cfg, out_dir,
                   export_raw, export_overlay, export_gray, export_label,
                   score_thr):
    """ProcessPoolExecutor task: align+export one image with this process's
    private reader (built by ``fine_align._pool_init``). Cancellation is handled
    by the orchestrator dropping not-yet-started futures."""
    return align_and_export_one_image(
        job, fine_align.pool_reader(), root, poi_colored, cfg, out_dir,
        export_raw, export_overlay, export_gray, export_label, score_thr)
