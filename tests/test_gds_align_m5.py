"""Tests for M5 — per-image alignment export in ``tools/gds_align_tool.py``.

Headless export helpers (``alignment_rows`` / ``write_alignment_csv`` /
``write_alignment_json`` / ``synthetic_layer_specs``) need only the stdlib +
numpy. The dialog + MainWindow integration tests additionally need PyQt6.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

np = pytest.importorskip("numpy")

import gds_align_tool as gat  # noqa: E402
import sem_loader  # noqa: E402


def _img(image_id, x=None, y=None, name="img.png"):
    return sem_loader.SemImage(image_id=image_id, filename=name,
                               file_path=Path(name), xrel=x, yrel=y)


# ── alignment_rows ───────────────────────────────────────────────────


class TestAlignmentRows:

    def test_coords_and_refined(self):
        imgs = [_img("D1", 4000.0, 5000.0)]
        rows = gat.alignment_rows(
            imgs, {"D1": (-500.0, 300.0, 0.93)},
            coarse_of=lambda i: (i.xrel, i.yrel),
            klarf_path="k.000", gds_path="x.oas", poi_layer="L17/D0",
            nm_per_px=100.0)
        r = rows[0]
        assert r["image_id"] == "D1"
        assert r["coarse_dx_nm"] == 4000.0 and r["coarse_dy_nm"] == 5000.0
        assert r["fine_dx_nm"] == -500.0 and r["fine_dy_nm"] == 300.0
        assert r["score"] == 0.93
        assert r["poi_layer"] == "L17/D0" and r["nm_per_px"] == 100.0

    def test_no_coords_blank_coarse(self):
        rows = gat.alignment_rows(
            [_img("D2")], {}, coarse_of=lambda i: None)
        r = rows[0]
        assert r["coarse_dx_nm"] == "" and r["coarse_dy_nm"] == ""

    def test_no_refined_blank_fine(self):
        rows = gat.alignment_rows(
            [_img("D3", 1.0, 2.0)], {}, coarse_of=lambda i: (i.xrel, i.yrel))
        r = rows[0]
        assert r["fine_dx_nm"] == "" and r["score"] == ""
        assert r["coarse_dx_nm"] == 1.0


# ── synthetic_layer_specs ────────────────────────────────────────────


class TestSyntheticSpecs:

    def test_empty_doc(self):
        assert gat.synthetic_layer_specs(None) == []

    def test_collects_expression_layers(self):
        doc = gat.GdsDocument()
        raw = gat.LayerEntry(key=gat.LayerKey(17, 0), polygons=[])
        syn = gat.LayerEntry(
            key=gat.LayerKey(-1, 0, name="L0", synthetic=True), polygons=[],
            expr_text="A & B", expr_bindings={"A": (17, 0), "B": (6, 0)})
        doc.entries = [raw, syn]
        specs = gat.synthetic_layer_specs(doc)
        assert len(specs) == 1
        assert specs[0]["name"] == "L0" and specs[0]["expr"] == "A & B"
        assert specs[0]["bindings"]["A"] == [17, 0]


# ── file round-trips ─────────────────────────────────────────────────


class TestWriteRoundtrip:

    def _rows(self):
        return gat.alignment_rows(
            [_img("D1", 4000.0, 5000.0), _img("D2")],
            {"D1": (-500.0, 300.0, 0.9)},
            coarse_of=lambda i: (i.xrel, i.yrel) if i.has_coords else None,
            klarf_path="k.000", gds_path="x.oas", nm_per_px=100.0)

    def test_csv_roundtrip(self, tmp_path):
        p = tmp_path / "a.csv"
        gat.write_alignment_csv(p, self._rows())
        with open(p) as f:
            r = list(csv.DictReader(f))
        assert list(r[0].keys()) == gat.ALIGNMENT_COLUMNS
        assert r[0]["fine_dx_nm"] == "-500.0"
        assert r[1]["coarse_dx_nm"] == ""      # no-coords image stays blank

    def test_json_roundtrip(self, tmp_path):
        p = tmp_path / "a.json"
        payload = gat.write_alignment_json(
            p, self._rows(), [{"name": "L0", "expr": "A & B", "bindings": {}}])
        with open(p) as f:
            j = json.load(f)
        assert j == payload
        assert j["schema"] == "mmh-gds-alignment-v1"
        assert j["columns"] == gat.ALIGNMENT_COLUMNS
        assert len(j["alignments"]) == 2
        assert j["synthetic_layers"][0]["name"] == "L0"


# ── GUI: dialog + MainWindow integration ─────────────────────────────

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 not available")
try:
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import Qt
    _APP = QApplication.instance() or QApplication([])
except Exception as exc:  # pragma: no cover
    pytest.skip(f"Qt runtime unavailable: {exc}", allow_module_level=True)


class TestExportDialog:

    def test_defaults_all_selected(self):
        d = gat.AlignmentExportDialog(None, [_img("D1", 1, 2), _img("D2")])
        fmt, ids = d.selected()
        assert fmt == "csv" and ids == ["D1", "D2"]

    def test_select_none_then_format(self):
        d = gat.AlignmentExportDialog(None, [_img("D1", 1, 2)])
        d._set_all(False)
        d._fmt.setCurrentIndex(1)
        fmt, ids = d.selected()
        assert fmt == "json" and ids == []


class TestMainWindowExport:

    def test_coarse_gds_excludes_refined(self):
        mw = gat.MainWindow()
        try:
            mw._chip_corner_x = mw._chip_corner_y = 0.0
            mw._fine_dx = 10.0
            mw._fine_dy = 20.0
            mw._origin_dx = mw._origin_dy = 0.0
            img = _img("D1", 4000.0, 5000.0)
            # coarse = klarf_to_gds(4000,5000,0,0) + fine; refined must NOT
            # leak in even if present.
            mw._refined = {"D1": (-999.0, -999.0, 0.5)}
            cx, cy = mw._coarse_gds(img)
            assert cx == pytest.approx(4010.0)
            assert cy == pytest.approx(5020.0)
        finally:
            mw.close()
