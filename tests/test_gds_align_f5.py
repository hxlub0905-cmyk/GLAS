"""Tests for F5 — fine-align diagnostics + workflow helpers in
``glas/app/gds_align_tool.py``.

These cover the pure-logic pieces (results-table rows + status derivation,
score histogram binning, residual median, filesystem-safe names, the overlay
outline raster, and the overlay manifest writer). Importing the module needs
PyQt6; the array helpers additionally need numpy. Everything skips cleanly when
those are unavailable.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_APP = Path(__file__).resolve().parents[1] / "glas" / "app"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

pytest.importorskip("PyQt6", reason="PyQt6 required to import gds_align_tool")
np = pytest.importorskip("numpy")

import gds_align_tool as gat  # noqa: E402


class _Img:
    def __init__(self, image_id):
        self.image_id = image_id


# ── score_histogram ──────────────────────────────────────────────────


class TestScoreHistogram:

    def test_counts_and_total(self):
        bins = gat.score_histogram([0.05, 0.15, 0.95], nbins=10)
        assert len(bins) == 10
        assert sum(bins) == 3
        assert bins[0] == 1   # 0.05 -> bin 0
        assert bins[1] == 1   # 0.15 -> bin 1
        assert bins[9] == 1   # 0.95 -> bin 9

    def test_clamps_and_skips_none(self):
        bins = gat.score_histogram([None, 1.0, 2.0, -1.0], nbins=4)
        assert sum(bins) == 3        # None skipped
        assert bins[-1] == 2         # 1.0 and 2.0 clamp into last bin
        assert bins[0] == 1          # -1.0 clamps into first bin

    def test_degenerate_range(self):
        assert gat.score_histogram([0.5], nbins=5, lo=1.0, hi=1.0) == [0] * 5


# ── median / residual_median ─────────────────────────────────────────


class TestMedian:

    def test_odd_even(self):
        assert gat._median([3, 1, 2]) == 2
        assert gat._median([1, 2, 3, 4]) == 2.5

    def test_residual_median_ok_only(self):
        refined = {"a": (10.0, -4.0, 0.9), "b": (20.0, 0.0, 0.8),
                   "c": (30.0, 4.0, 0.7)}
        assert gat.residual_median(refined, ["a", "b", "c"]) == (20.0, 0.0)

    def test_residual_median_empty(self):
        assert gat.residual_median({}, ["x"]) is None


# ── fine_align_result_rows ───────────────────────────────────────────


class TestResultRows:

    def test_status_and_blanks(self):
        images = [_Img("a"), _Img("b"), _Img("c"), _Img("d")]
        refined = {"a": (1.0, 2.0, 0.9), "b": (3.0, 4.0, 0.2)}
        meta = {"a": (5, "ok"), "b": (5, "ok"), "c": (0, "no-coords")}
        rows = gat.fine_align_result_rows(
            images, refined, meta, threshold=0.5)
        by_id = {r["image_id"]: r for r in rows}
        assert by_id["a"]["status"] == "ok"
        assert by_id["b"]["status"] == "low-score"   # ok but score < threshold
        assert by_id["c"]["status"] == "no-coords"
        assert by_id["c"]["score"] is None
        assert by_id["d"]["status"] == "not-run"     # no meta entry
        assert by_id["a"]["used_radius"] == 5


# ── _safe_name ───────────────────────────────────────────────────────


class TestSafeName:

    def test_sanitises(self):
        assert gat._safe_name("img 1/2:3") == "img_1_2_3"
        assert gat._safe_name("keep-._OK") == "keep-._OK"
        assert gat._safe_name("") == "image"


# ── overlay_outlines_on_sem ──────────────────────────────────────────


class TestOverlayOutlines:

    def test_shape_and_background(self):
        sem = np.full((50, 50), 60, np.uint8)
        rgb = gat.overlay_outlines_on_sem(sem, [], anchor=(0.0, 0.0),
                                          nm_per_px=1.0)
        assert rgb.shape == (50, 50, 3)
        # Empty entries -> SEM broadcast to grey RGB, unchanged.
        assert tuple(rgb[0, 0]) == (60, 60, 60)

    def test_draws_outline_colour(self):
        sem = np.full((50, 50), 60, np.uint8)
        # A square centred on the anchor; at nm_per_px=1 and a 50px FOV the
        # outline lands well inside the frame.
        poly = np.array([[-10, -10], [10, -10], [10, 10], [-10, 10]],
                        dtype=np.float64)
        rgb = gat.overlay_outlines_on_sem(
            sem, [([poly], (255, 0, 0))], anchor=(0.0, 0.0), nm_per_px=1.0)
        # Some pixel must now carry the red outline colour.
        red = (rgb[:, :, 0] == 255) & (rgb[:, :, 1] == 0) & (rgb[:, :, 2] == 0)
        assert red.any()


# ── OverlayExportWorker manifest ─────────────────────────────────────


class TestManifest:

    def test_write_manifest_csv_json(self, tmp_path):
        w = gat.OverlayExportWorker(
            rar=None, root=None, poi_specs_colored=[], jobs=[], cfg={},
            out_dir=str(tmp_path), export_raw=True, export_overlay=False)
        rows = [
            {"image_id": "a", "raw_png": "a_raw.png", "overlay_png": "",
             "fine_dx_nm": 1.0, "fine_dy_nm": 2.0, "score": 0.9,
             "status": "ok"},
            {"image_id": "b", "raw_png": "", "overlay_png": "",
             "fine_dx_nm": "", "fine_dy_nm": "", "score": "",
             "status": "missing-file"},
        ]
        path = w._write_manifest(rows)
        assert Path(path).name == "overlay_manifest.csv"
        with open(path, newline="") as f:
            got = list(csv.DictReader(f))
        assert [r["image_id"] for r in got] == ["a", "b"]
        assert got[0]["status"] == "ok"
        jpath = tmp_path / "overlay_manifest.json"
        payload = json.loads(jpath.read_text())
        assert payload["schema"] == "mmh-gds-overlay-v1"
        assert len(payload["images"]) == 2
