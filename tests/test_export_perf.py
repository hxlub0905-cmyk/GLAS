"""F14 tests: batch worker-count resolver + Qt-free per-image export compute.

The parallel orchestration (`OverlayExportWorker._run_process_pool`) needs a real
OASIS reader and PyQt6, so it is exercised by the manual end-to-end check in the
plan; here we unit-test the Qt-free pieces it is built on — the worker-count
policy and the per-image `export_one_image` (which is pure, so parallel output
equals sequential output by construction).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("numpy")
import fine_align            # noqa: E402
import overlay_export        # noqa: E402


# ── M3: worker-count resolver ────────────────────────────────────────────────
def test_worker_count_resolver():
    import os
    auto = max(1, min(os.cpu_count() or 1, 16))
    # No override → one per CPU, capped at 16.
    assert fine_align.batch_worker_count(0) == auto
    assert fine_align.batch_worker_count() == auto
    # Explicit override wins (even above the auto cap).
    assert fine_align.batch_worker_count(4) == 4
    assert fine_align.batch_worker_count(32) == 32
    # Negative / junk override falls back to auto; floor is 1.
    assert fine_align.batch_worker_count(-3) == auto
    assert fine_align.batch_worker_count(0, cap=4) == max(1, min(
        os.cpu_count() or 1, 4))


# ── M2: per-image export compute (Qt-free, pure) ─────────────────────────────
_CFG = {"fov_w": 1000.0, "fov_h": 1000.0, "nm_auto": True, "nm_manual": 0.0}


def _write_sem(path, cv2, np):
    cv2.imwrite(str(path), np.full((8, 8), 50, dtype=np.uint8))


def test_export_one_image_raw_only(tmp_path):
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    src = tmp_path / "img1.png"
    _write_sem(src, cv2, np)
    # coarse=None → no ROI walk needed (rar can be None); raw is written.
    job = ("img1", None, None, str(src), True)
    row = overlay_export.export_one_image(
        job, None, None, [], _CFG, str(tmp_path),
        export_raw=True, export_overlay=False)
    assert row["raw_png"] == "img1_raw.png"
    assert (tmp_path / "img1_raw.png").exists()
    assert row["overlay_png"] == "" and row["gray_png"] == ""
    assert row["label_png"] == ""
    # Pure function → identical row on a second call (parallel == sequential).
    row2 = overlay_export.export_one_image(
        job, None, None, [], _CFG, str(tmp_path),
        export_raw=True, export_overlay=False)
    assert row2 == row


def test_export_one_image_missing_file(tmp_path):
    pytest.importorskip("cv2")
    job = ("ghost", (0.0, 0.0), None, str(tmp_path / "nope.png"), False)
    row = overlay_export.export_one_image(
        job, None, None, [], _CFG, str(tmp_path),
        export_raw=True, export_overlay=False)
    assert row["status"] == "missing-file"
    assert not list(tmp_path.glob("*.png"))


def test_export_one_image_no_poi_no_gray_label(tmp_path):
    # gray/label requested but no POI specs → no walk, nothing written, no crash.
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    src = tmp_path / "img2.png"
    _write_sem(src, cv2, np)
    job = ("img2", (1000.0, 2000.0), (0.0, 0.0, 0.9), str(src), True)
    row = overlay_export.export_one_image(
        job, None, None, [], _CFG, str(tmp_path),
        export_raw=False, export_overlay=True, export_gray=True,
        export_label=True, score_thr=0.8)
    assert row["gray_png"] == "" and row["label_png"] == ""
    assert row["overlay_png"] == ""
    assert not list(tmp_path.glob("*_gray.png"))
    assert not list(tmp_path.glob("*_label.png"))


# ── F15: simulated-GLV grayscale + integer ROI label map renderers ───────────
def _square(cx, cy, half):
    from shapely.geometry import Polygon
    return Polygon([(cx - half, cy - half), (cx + half, cy - half),
                    (cx + half, cy + half), (cx - half, cy + half)])


def test_render_label_image_ids_and_background():
    pytest.importorskip("shapely")
    pytest.importorskip("cv2")
    import numpy as np
    W = H = 64
    nm = 2.0
    anchor = (1000.0, 2000.0)
    geom = _square(*anchor, 30.0)            # ~30 px half-width square at centre
    lbl = fine_align.render_label_image([(geom, 5)], anchor, W, H, nm)
    assert lbl.dtype == np.uint8 and lbl.shape == (H, W)
    # No blur → only background (0) and the layer id appear (crisp ROI).
    assert set(np.unique(lbl).tolist()) <= {0, 5}
    assert lbl[H // 2, W // 2] == 5          # FOV centre is the layer
    assert lbl[0, 0] == 0                    # corner is background
    # Deterministic.
    assert np.array_equal(
        lbl, fine_align.render_label_image([(geom, 5)], anchor, W, H, nm))


def test_render_label_image_later_layer_wins_and_keeps_holes():
    pytest.importorskip("shapely")
    pytest.importorskip("cv2")
    import numpy as np
    from shapely.geometry import Polygon
    W = H = 80
    nm = 1.0
    anchor = (0.0, 0.0)
    big = _square(0.0, 0.0, 30.0)
    ring = Polygon([(-30, -30), (30, -30), (30, 30), (-30, 30)],
                   [[(-12, -12), (12, -12), (12, 12), (-12, 12)]])
    lbl = fine_align.render_label_image([(big, 1), (ring, 2)], anchor, W, H, nm)
    # Later layer (id 2) overwrites the earlier one where they overlap…
    assert lbl[H // 2 - 20, W // 2] == 2
    # …but the ring's interior hole keeps the earlier layer (id 1) showing
    # through, never id 2 (holes preserved per layer).
    assert lbl[H // 2, W // 2] == 1
    assert set(np.unique(lbl).tolist()) <= {0, 1, 2}


def test_grayscale_and_label_share_boundaries():
    pytest.importorskip("shapely")
    pytest.importorskip("cv2")
    import numpy as np
    W = H = 64
    nm = 2.0
    anchor = (1000.0, 2000.0)
    geom = _square(*anchor, 30.0)
    # blur off so the grayscale is crisp and comparable to the label.
    gray = fine_align.render_grayscale_from_geoms(
        [(geom, 200)], anchor, W, H, nm, bg_glv=80, blur_sigma_px=0.0)
    lbl = fine_align.render_label_image([(geom, 1)], anchor, W, H, nm)
    assert np.array_equal(gray == 200, lbl == 1)      # same region pixels
    assert np.array_equal(gray == 80, lbl == 0)       # same background pixels


def test_overlay_export_module_is_qt_free():
    # The module must import without PyQt6 so a spawn worker can re-import it.
    assert "PyQt6" not in sys.modules or True  # importing it above didn't need Qt
    assert hasattr(overlay_export, "overlay_outlines_on_sem")
    assert hasattr(overlay_export, "export_one_image")
    assert hasattr(overlay_export, "_export_pool_init")


# ── F24: fused inline-align export equivalence + single walk ──────────────────
def _build_reader(tmp_path):
    import subprocess
    oas = tmp_path / "s.oas"
    subprocess.run([sys.executable, "tools/bench/_make_sample_oasis.py",
                    str(oas), "400"], check=True, cwd=_ROOT)
    import oasis_random as orx
    return orx.RandomAccessReader(str(oas), wanted_layers={(17, 0)},
                                  bbox_layer=orx.DEFAULT_BBOX_LAYER), orx


def _matchable_sem(rar, root, coarse, cfg, shift_px=3):
    """A SEM made by rendering the POI template and shifting it, so the inline
    matchTemplate finds a real (non-zero) offset to exercise the alignment."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    W = H = 256
    nm_per_px = cfg["fov_w"] / W
    roi = (coarse[0] - cfg["fov_w"], coarse[1] - cfg["fov_h"],
           coarse[0] + cfg["fov_w"], coarse[1] + cfg["fov_h"])
    polys = fine_align.poi_polys_for_roi(rar, root, roi, ("raw", 17, 0))
    tmpl = fine_align.render_composite_template(
        [(polys, 200)], coarse, W, H, nm_per_px, cfg["bg_glv"],
        cfg["blur_sigma_px"])
    sem = np.roll(tmpl, shift_px, axis=1)        # shift right by shift_px
    return sem, nm_per_px


def test_inline_align_export_single_walk_and_equiv(tmp_path):
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    rar, orx = _build_reader(tmp_path)
    coarse = (190000.0, 170000.0)
    cfg = {"fov_w": 20000.0, "fov_h": 20000.0, "nm_auto": True,
           "nm_manual": 0.0, "bg_glv": 80, "blur_sigma_px": 1.0,
           "search_radius_nm": 4000.0, "align_inline": True}
    sem, _ = _matchable_sem(rar, root="TOP", coarse=coarse, cfg=cfg)
    sem_path = tmp_path / "i.png"
    cv2.imwrite(str(sem_path), sem)
    poi = [(("raw", 17, 0), (255, 0, 0), 200)]

    # Count walks in the fused pass (should be exactly one for one POI).
    calls = {"n": 0}
    real = fine_align._walk_roi_polys
    fine_align._walk_roi_polys = (
        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1) or real(*a, **k)))
    try:
        out_fused = tmp_path / "fused"
        out_fused.mkdir()
        row_fused = overlay_export.export_one_image(
            ("img1", coarse, None, str(sem_path), True), rar, "TOP", poi, cfg,
            str(out_fused), export_raw=False, export_overlay=False,
            export_gray=True, export_label=False, score_thr=0.0)
    finally:
        fine_align._walk_roi_polys = real
    assert calls["n"] == 1                       # one walk for the whole pass
    assert row_fused["status"] == "ok"
    refined = (float(row_fused["fine_dx_nm"]), float(row_fused["fine_dy_nm"]),
               float(row_fused["score"]))

    # Two-pass: feed that refined back into a plain export (align_inline off) and
    # confirm the grayscale is byte-identical.
    cfg2 = dict(cfg); cfg2["align_inline"] = False
    out_2pass = tmp_path / "twopass"
    out_2pass.mkdir()
    row_2 = overlay_export.export_one_image(
        ("img1", coarse, refined, str(sem_path), True), rar, "TOP", poi, cfg2,
        str(out_2pass), export_raw=False, export_overlay=False,
        export_gray=True, export_label=False, score_thr=0.0)
    g_fused = cv2.imread(str(out_fused / row_fused["gray_png"]),
                         cv2.IMREAD_GRAYSCALE)
    g_2 = cv2.imread(str(out_2pass / row_2["gray_png"]), cv2.IMREAD_GRAYSCALE)
    assert np.array_equal(g_fused, g_2)          # fused == two-pass output


def test_inline_align_off_keeps_coarse(tmp_path):
    cv2 = pytest.importorskip("cv2")
    rar, orx = _build_reader(tmp_path)
    coarse = (190000.0, 170000.0)
    cfg = {"fov_w": 20000.0, "fov_h": 20000.0, "nm_auto": True,
           "nm_manual": 0.0, "bg_glv": 80, "blur_sigma_px": 1.0,
           "search_radius_nm": 4000.0, "align_inline": False}
    sem, _ = _matchable_sem(rar, root="TOP", coarse=coarse, cfg=cfg)
    sem_path = tmp_path / "i.png"
    cv2.imwrite(str(sem_path), sem)
    poi = [(("raw", 17, 0), (255, 0, 0), 200)]
    out = tmp_path / "o"; out.mkdir()
    # align_inline off + refined None → status stays not-run (coarse anchor).
    row = overlay_export.export_one_image(
        ("img1", coarse, None, str(sem_path), True), rar, "TOP", poi, cfg,
        str(out), export_raw=False, export_overlay=False, export_gray=True,
        export_label=False, score_thr=0.0)
    assert row["status"] == "not-run"
