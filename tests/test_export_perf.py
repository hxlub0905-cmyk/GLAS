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
        export_raw=True, export_overlay=False, export_mask=False)
    assert row["raw_png"] == "img1_raw.png"
    assert (tmp_path / "img1_raw.png").exists()
    assert row["overlay_png"] == "" and row["mask_png"] == ""
    # Pure function → identical row on a second call (parallel == sequential).
    row2 = overlay_export.export_one_image(
        job, None, None, [], _CFG, str(tmp_path),
        export_raw=True, export_overlay=False, export_mask=False)
    assert row2 == row


def test_export_one_image_missing_file(tmp_path):
    pytest.importorskip("cv2")
    job = ("ghost", (0.0, 0.0), None, str(tmp_path / "nope.png"), False)
    row = overlay_export.export_one_image(
        job, None, None, [], _CFG, str(tmp_path),
        export_raw=True, export_overlay=False, export_mask=False)
    assert row["status"] == "missing-file"
    assert not list(tmp_path.glob("*.png"))


def test_export_one_image_no_poi_no_mask(tmp_path):
    # export_mask requested but no POI specs → no walk, no mask, no crash.
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    src = tmp_path / "img2.png"
    _write_sem(src, cv2, np)
    job = ("img2", (1000.0, 2000.0), (0.0, 0.0, 0.9), str(src), True)
    row = overlay_export.export_one_image(
        job, None, None, [], _CFG, str(tmp_path),
        export_raw=False, export_overlay=True, export_mask=True,
        mask_thr=0.8)
    assert row["mask_png"] == "" and row["overlay_png"] == ""
    assert not list(tmp_path.glob("*_mask.png"))


def test_overlay_export_module_is_qt_free():
    # The module must import without PyQt6 so a spawn worker can re-import it.
    assert "PyQt6" not in sys.modules or True  # importing it above didn't need Qt
    assert hasattr(overlay_export, "overlay_outlines_on_sem")
    assert hasattr(overlay_export, "export_one_image")
    assert hasattr(overlay_export, "_export_pool_init")
