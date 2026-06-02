"""F13 tests: batch subset re-run + per-image GDS mask export.

The decision logic lives in the Qt-free ``fine_align`` core module so it can be
unit-tested without a Qt runtime; one extra integration test exercises the app's
``OverlayExportWorker`` manifest writer and is skipped when PyQt6 / cv2 are
unavailable (per CLAUDE.md §3).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# conftest puts glas/core + glas/app on sys.path; keep a defensive insert too.
_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# fine_align pulls in numpy / gds_boolean / oasis_random at import.
pytest.importorskip("numpy")
import fine_align  # noqa: E402


class _Img:
    def __init__(self, iid):
        self.image_id = iid


# ── M4: re-run "only improves" overwrite rule (Q1=C) ─────────────────────────
def test_rerun_only_improves():
    # No prior result → an "ok" re-run is always kept.
    assert fine_align.rerun_should_overwrite(None, 0.5, "ok") is True
    # Strictly higher score overwrites…
    assert fine_align.rerun_should_overwrite((0.0, 0.0, 0.5), 0.7, "ok") is True
    # …lower score does NOT overwrite…
    assert fine_align.rerun_should_overwrite((0.0, 0.0, 0.7), 0.5, "ok") is False
    # …and an equal score is not "strictly better", so it is kept as-is.
    assert fine_align.rerun_should_overwrite((0.0, 0.0, 0.5), 0.5, "ok") is False
    # A non-"ok" re-run never clobbers a prior good result, even with a higher
    # nominal score.
    assert fine_align.rerun_should_overwrite((0.0, 0.0, 0.1), 0.9, "flat") is False
    assert fine_align.rerun_should_overwrite(None, 0.9, "no-coords") is False


# ── M4: subset selection for re-run ──────────────────────────────────────────
def test_rerun_selected():
    imgs = [_Img("a"), _Img("b"), _Img("c")]
    # Only the requested ids are picked, in dataset order (not request order).
    assert [i.image_id for i in
            fine_align.rerun_image_subset(imgs, ["c", "a"])] == ["a", "c"]
    # ids compared as strings; unknown ids are ignored.
    assert [i.image_id for i in
            fine_align.rerun_image_subset(imgs, ["b"])] == ["b"]
    assert fine_align.rerun_image_subset(imgs, ["x"]) == []
    assert fine_align.rerun_image_subset(imgs, []) == []


# ── M4: mask export score gate (Q2) ──────────────────────────────────────────
def test_mask_export_threshold():
    assert fine_align.mask_should_export((0.0, 0.0, 0.9), 0.8) is True
    assert fine_align.mask_should_export((0.0, 0.0, 0.8), 0.8) is True   # >=
    assert fine_align.mask_should_export((0.0, 0.0, 0.79), 0.8) is False


def test_mask_export_no_refined():
    assert fine_align.mask_should_export(None, 0.8) is False
    assert fine_align.mask_should_export(None, 0.0) is False


# ── M4: manifest carries the mask_png column ─────────────────────────────────
def test_manifest_mask_png_col():
    cols = fine_align.OVERLAY_MANIFEST_COLS
    assert "mask_png" in cols
    # The original overlay columns are preserved (backward compatibility).
    for c in ("image_id", "raw_png", "overlay_png", "fine_dx_nm",
              "fine_dy_nm", "score", "status"):
        assert c in cols


# ── M4: integration — worker writes a manifest with the mask_png header and
#    skips the mask for a not-fine-aligned image (Qt + cv2 required) ──────────
def test_manifest_csv_header_has_mask_png(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6.QtWidgets")
    cv2 = pytest.importorskip("cv2")
    import csv

    import numpy as np
    from PyQt6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    import gds_align_tool as gat

    src = tmp_path / "img1.png"
    cv2.imwrite(str(src), np.zeros((8, 8), dtype=np.uint8))
    # coarse=None → the ROI walk / mask branch is skipped (no reader needed);
    # refined=None → no mask even though export_mask is on.
    jobs = [("img1", None, None, str(src), True)]
    cfg = {"fov_w": 1000.0, "fov_h": 1000.0, "nm_auto": True, "nm_manual": 0.0}
    w = gat.OverlayExportWorker(
        None, None, [], jobs, cfg, str(tmp_path),
        export_raw=True, export_overlay=False,
        export_mask=True, mask_score_threshold=0.8)
    captured = {}
    w.finished.connect(lambda n, m: captured.update(count=n, manifest=m))
    w.run()

    assert "manifest" in captured, "worker should finish and emit a manifest"
    with open(captured["manifest"], newline="") as f:
        header = next(csv.reader(f))
    assert "mask_png" in header
    # No mask written for the not-aligned image.
    assert not list(Path(tmp_path).glob("*_mask.png"))


# ── PR#9 review: mask must keep Boolean interior holes ───────────────────────
def test_mask_preserves_boolean_holes():
    pytest.importorskip("shapely")
    pytest.importorskip("cv2")
    import gds_boolean as gb
    from shapely.geometry import Polygon

    # 100×100 box (nm) with a 40×40 interior hole, rasterised at 1 nm/px.
    outer = [(0, 0), (100, 0), (100, 100), (0, 100)]
    hole = [(30, 30), (70, 30), (70, 70), (30, 70)]
    geom = Polygon(outer, [hole])
    kw = dict(width_px=100, height_px=100, x_min_nm=0.0, y_min_nm=0.0,
              nm_per_px=1.0)

    # union_geometries (the mask path) keeps the hole: centre reads 0, ring 255.
    umask = gb.make_mask(gb.union_geometries([geom]), **kw)
    assert umask[50, 50] == 0
    assert umask[10, 10] == 255

    # The old path — rebuilding from exterior rings — wrongly fills the hole.
    flat = gb.polys_to_geometry(gb.geometry_to_polygons(geom))
    assert gb.make_mask(flat, **kw)[50, 50] == 255


# ── PR#9 review: mask pixels must match the fine-align template / overlay ─────
def test_mask_matches_rasterize_layer_pixels():
    pytest.importorskip("shapely")
    pytest.importorskip("cv2")
    import numpy as np

    import gds_boolean as gb
    from shapely.geometry import Polygon

    W, H, nm = 64, 48, 2.0
    cx, cy = 1000.0, 2000.0
    poly = np.array([[cx - 30, cy - 20], [cx + 40, cy - 20],
                     [cx + 40, cy + 25], [cx - 30, cy + 25]], dtype=float)
    # Reference: the template rasteriser the alignment was computed against.
    bbox = (cx - W / 2 * nm, cy - H / 2 * nm, cx + W / 2 * nm, cy + H / 2 * nm)
    ref = fine_align.rasterize_layer([poly], bbox, nm)
    # make_mask with the worker's origin (y_min raised one pixel) must match it
    # pixel-for-pixel — same (W/2, H/2) anchor convention as overlay/template.
    got = gb.make_mask(
        Polygon(poly), width_px=W, height_px=H,
        x_min_nm=cx - W / 2.0 * nm, y_min_nm=cy - (H / 2.0 - 1.0) * nm,
        nm_per_px=nm)
    assert ref.shape == got.shape == (H, W)
    assert np.array_equal(ref, got)
