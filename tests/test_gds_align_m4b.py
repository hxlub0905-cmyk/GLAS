"""Tests for M4b — POI template + cv2.matchTemplate fine alignment in
``tools/gds_align_tool.py``.

Headless core (``make_template`` / ``render_poi_template`` / ``fine_align_one``
/ ``_parabola_subpx``) needs opencv + numpy. The GUI-integration tests
(POI exclusivity, run-fine-align end to end, per-image score) additionally
need PyQt6 and skip when Qt is unavailable.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2", reason="opencv required for M4b")

import gds_align_tool as gat  # noqa: E402


def _square_mask(n=60, lo=20, hi=40):
    m = np.zeros((n, n), np.uint8)
    m[lo:hi, lo:hi] = 255
    return m


# ── make_template ────────────────────────────────────────────────────


class TestMakeTemplate:

    def test_glv_values(self):
        t = gat.make_template(_square_mask(), fg_glv=200, bg_glv=80,
                              blur_sigma_px=0.0)
        assert t.dtype == np.uint8
        assert int(t.max()) == 200 and int(t.min()) == 80

    def test_blur_smooths_edges(self):
        m = _square_mask()
        sharp = gat.make_template(m, 200, 80, 0.0)
        blurred = gat.make_template(m, 200, 80, 2.0)
        # Blur introduces intermediate grey levels not present in the sharp one.
        assert len(np.unique(blurred)) > len(np.unique(sharp))


# ── render_poi_template ──────────────────────────────────────────────


class TestRenderTemplate:

    def test_exact_size(self):
        sq = np.array([[3000, 3000], [5000, 3000], [5000, 5000], [3000, 5000]],
                      float)
        t = gat.render_poi_template([sq], (4000.0, 4000.0), 80, 80, 100.0,
                                    200, 80, 0.0)
        assert t.shape == (80, 80)

    def test_structure_is_centred(self):
        sq = np.array([[3000, 3000], [5000, 3000], [5000, 5000], [3000, 5000]],
                      float)
        t = gat.render_poi_template([sq], (4000.0, 4000.0), 80, 80, 100.0,
                                    255, 0, 0.0)
        # The 2µm square at the FOV centre lands on the middle pixels.
        assert t[40, 40] == 255
        assert t[2, 2] == 0


# ── _parabola_subpx ──────────────────────────────────────────────────


class TestParabola:

    def test_symmetric_peak_zero_offset(self):
        res = np.array([[0.2, 0.9, 0.2]], dtype=np.float32)
        assert gat._parabola_subpx(res, 1, 0, axis=0) == pytest.approx(0.0)

    def test_skewed_peak_positive_offset(self):
        res = np.array([[0.2, 0.9, 0.5]], dtype=np.float32)
        off = gat._parabola_subpx(res, 1, 0, axis=0)
        assert off > 0.0

    def test_edge_returns_zero(self):
        res = np.array([[0.9, 0.2, 0.1]], dtype=np.float32)
        assert gat._parabola_subpx(res, 0, 0, axis=0) == 0.0


# ── fine_align_one ───────────────────────────────────────────────────


class TestFineAlignOne:

    def _template(self):
        return gat.make_template(_square_mask(), 200, 80, 0.0)

    def test_aligned_zero_offset(self):
        t = self._template()
        dx, dy, score, r = gat.fine_align_one(t, t, nm_per_px=5.0,
                                              search_radius_px=8)
        assert (dx, dy) == pytest.approx((0.0, 0.0), abs=1e-6)
        assert score > 0.99

    def test_shift_right_down_signs(self):
        t = self._template()
        sem = np.roll(np.roll(t, 4, axis=1), 3, axis=0)   # +4 right, +3 down
        dx, dy, score, r = gat.fine_align_one(sem, t, nm_per_px=5.0,
                                              search_radius_px=8)
        # ex=+4 -> dx=-20 ; ey=+3 -> dy=+15
        assert dx == pytest.approx(-20.0, abs=1e-6)
        assert dy == pytest.approx(15.0, abs=1e-6)
        assert score > 0.9

    def test_flat_template_no_signal(self):
        flat = np.full((60, 60), 100, np.uint8)
        dx, dy, score, r = gat.fine_align_one(flat, flat, 5.0, 8)
        assert (dx, dy, score) == (0.0, 0.0, 0.0)

    def test_size_mismatch_raises(self):
        with pytest.raises(ValueError):
            gat.fine_align_one(np.zeros((60, 60), np.uint8),
                               np.zeros((40, 40), np.uint8), 5.0, 8)

    def test_apply_correction_realigns(self):
        """Applying (dx_nm, dy_nm) to the render anchor reproduces the SEM
        shift — the core invariant the overlay relies on."""
        sq = np.array([[3000, 3000], [5000, 3000], [5000, 5000], [3000, 5000]],
                      float)
        npp = 100.0
        anchor = (4000.0, 4000.0)
        t = gat.render_poi_template([sq], anchor, 80, 80, npp, 200, 80, 0.0)
        sem = np.roll(np.roll(t, 5, axis=1), 3, axis=0)
        dx, dy, score, r = gat.fine_align_one(sem, t, npp, 12)
        new_anchor = (anchor[0] + dx, anchor[1] + dy)
        re = gat.render_poi_template([sq], new_anchor, 80, 80, npp, 200, 80, 0.0)
        assert np.array_equal(re, sem)


# ── GUI integration ──────────────────────────────────────────────────

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 not available")
try:
    from PyQt6.QtWidgets import QApplication
    import sem_loader
    _APP = QApplication.instance() or QApplication([])
except Exception as exc:  # pragma: no cover
    pytest.skip(f"Qt runtime unavailable: {exc}", allow_module_level=True)


@pytest.fixture
def mw():
    w = gat.MainWindow()
    yield w
    w.close()


def _doc_two_layers():
    sq = np.array([[3000, 3000], [5000, 3000], [5000, 5000], [3000, 5000]],
                  float)
    doc = gat.GdsDocument()
    doc.entries = [
        gat.LayerEntry(key=gat.LayerKey(17, 0), polygons=[sq]),
        gat.LayerEntry(key=gat.LayerKey(6, 0), polygons=[sq + 500]),
    ]
    return doc, sq


class TestPoiSelection:

    def test_multi_select_and_run_enabled(self, mw):
        doc, _ = _doc_two_layers()
        mw._doc = doc
        mw.layer_panel.set_document(doc)
        rows = mw.layer_panel._rows
        rows[0].poi_btn.setChecked(True)
        assert [e.key.label() for e in mw._poi_entries] == ["L17/D0"]
        assert mw.sem_panel.fine_align._run_btn.isEnabled()
        # F3: POI is multi-select — a second layer joins the set (in panel order).
        rows[1].poi_btn.setChecked(True)
        assert rows[0].poi_btn.isChecked() is True
        assert [e.key.label() for e in mw._poi_entries] == ["L17/D0", "L6/D0"]
        # Each active POI gets its own FG spin.
        assert set(mw.sem_panel.fine_align.poi_fgs()) == {(17, 0, ""), (6, 0, "")}
        # Clearing both disables the run buttons.
        rows[0].poi_btn.setChecked(False)
        rows[1].poi_btn.setChecked(False)
        assert mw._poi_entries == []
        assert mw.sem_panel.fine_align._run_btn.isEnabled() is False


class TestRunFineAlign:

    def test_end_to_end(self, mw, tmp_path):
        doc, sq = _doc_two_layers()
        mw._doc = doc
        mw.layer_panel.set_document(doc)
        mw.canvas.set_document(doc)
        mw.layer_panel._rows[0].poi_btn.setChecked(True)
        mw._chip_corner_x = mw._chip_corner_y = 0.0
        mw._fov_w = mw._fov_h = 8000.0
        mw._fine_dx = mw._fine_dy = mw._origin_dx = mw._origin_dy = 0.0
        mw._nm_auto = True
        mw._nm_per_px_manual = 0.0
        npp = 100.0
        anchor = (4000.0, 4000.0)
        t = gat.render_poi_template([sq], anchor, 80, 80, npp, 200, 80, 0.0)
        sem = np.roll(np.roll(t, 5, axis=1), 3, axis=0)
        p = tmp_path / "img.png"
        cv2.imwrite(str(p), sem)
        img = sem_loader.SemImage(image_id="D1", filename="img.png",
                                  file_path=p, xrel=4000.0, yrel=4000.0)
        mw._sem_images = [img]
        mw.sem_panel.set_images([img])
        mw._current_sem = img
        mw.sem_viewer.set_image(img)
        mw.sem_panel.fine_align._radius.setValue(1000)
        mw._on_run_fine_align()
        dx, dy, score = mw._refined["D1"]
        assert dx == pytest.approx(-500.0, abs=1.0)
        assert dy == pytest.approx(300.0, abs=1.0)
        assert score > 0.9
        # Overlay anchor reflects the refined correction.
        gx, gy = mw._current_image_gds()
        assert (gx, gy) == pytest.approx((3500.0, 4300.0), abs=1.0)
        # The list row carries the score badge (UserRole+2 data role).
        from PyQt6.QtCore import Qt as _Qt
        assert mw.sem_panel.list.item(0).data(
            _Qt.ItemDataRole.UserRole + 2) == f"{score:.2f}"

    def test_no_poi_is_noop(self, mw):
        mw._poi_entries = []
        mw._on_run_fine_align()
        assert mw._refined == {}


# ── Run all (batch) ──────────────────────────────────────────────────


class _FakeRar:
    _nm_per_grid = 1.0


def _patch_walk(monkeypatch, square=(3000, 3000, 5000, 5000), on_layer=17):
    import oasis_random

    def fake(rar, root, roi, layer, dt, cancel_cb=None):
        if layer == on_layer:
            return {"rects": np.array([list(square)]), "polys": []}
        return {"rects": np.empty((0, 4)), "polys": []}
    monkeypatch.setattr(oasis_random, "walk_roi", fake)


class TestPoiPolysForRoi:

    def test_raw(self, monkeypatch):
        _patch_walk(monkeypatch)
        polys = gat.poi_polys_for_roi(
            _FakeRar(), "top", (2000, 2000, 6000, 6000), ("raw", 17, 0))
        assert len(polys) == 1 and polys[0].shape == (4, 2)

    def test_raw_empty_layer(self, monkeypatch):
        _patch_walk(monkeypatch)
        polys = gat.poi_polys_for_roi(
            _FakeRar(), "top", (2000, 2000, 6000, 6000), ("raw", 99, 0))
        assert polys == []

    def test_expr(self, monkeypatch):
        _patch_walk(monkeypatch)
        polys = gat.poi_polys_for_roi(
            _FakeRar(), "top", (2000, 2000, 6000, 6000),
            ("expr", "A", {"A": (17, 0)}))
        assert len(polys) == 1


class TestRunAllWorker:

    def test_batch_run(self, monkeypatch, tmp_path):
        _patch_walk(monkeypatch)
        npp = 100.0
        anchor = (4000.0, 4000.0)
        polys = gat.poi_polys_for_roi(
            _FakeRar(), "top", (anchor[0] - 8000, anchor[1] - 8000,
                                anchor[0] + 8000, anchor[1] + 8000),
            ("raw", 17, 0))
        t = gat.render_poi_template(polys, anchor, 80, 80, npp, 200, 80, 0.0)
        sem = np.roll(np.roll(t, 5, axis=1), 3, axis=0)
        p = tmp_path / "d1.png"
        cv2.imwrite(str(p), sem)
        jobs = [("D1", anchor, str(p), True),
                ("D2", None, "", False)]            # no-coords -> skipped
        cfg = {"fov_w": 8000.0, "fov_h": 8000.0, "nm_auto": True,
               "nm_manual": 0.0, "bg_glv": 80,
               "blur_sigma_px": 0.0, "search_radius_nm": 1000.0,
               "score_threshold": 0.5}
        w = gat.FineAlignAllWorker(
            _FakeRar(), "top", [(("raw", 17, 0), 200)], jobs, cfg)
        results = {}
        prog = []
        done = {}
        w.result.connect(lambda i, dx, dy, s: results.__setitem__(i, (dx, dy, s)))
        w.progress.connect(lambda d, n, i: prog.append((d, n)))
        w.finished.connect(lambda c: done.__setitem__("c", c))
        w.run()
        assert done["c"] == 2                       # both visited
        assert prog == [(1, 2), (2, 2)]
        assert "D2" not in results                  # skipped (no coords)
        dx, dy, s = results["D1"]
        assert dx == pytest.approx(-500.0, abs=1.0)
        assert dy == pytest.approx(300.0, abs=1.0)
        assert s > 0.9

    def test_cancel_stops_early(self, monkeypatch, tmp_path):
        _patch_walk(monkeypatch)
        jobs = [("D1", (4000.0, 4000.0), "", False)]
        cfg = {"fov_w": 8000.0, "fov_h": 8000.0, "nm_auto": True,
               "nm_manual": 0.0, "bg_glv": 80,
               "blur_sigma_px": 0.0, "search_radius_nm": 1000.0,
               "score_threshold": 0.5}
        w = gat.FineAlignAllWorker(
            _FakeRar(), "top", [(("raw", 17, 0), 200)], jobs, cfg)
        cancelled = []
        w.cancelled.connect(lambda: cancelled.append(True))
        w.cancel()
        w.run()
        assert cancelled == [True]


class TestPoiSpecs:

    def test_raw_spec(self, mw):
        doc, _ = _doc_two_layers()
        mw.layer_panel.set_document(doc)
        mw.layer_panel._rows[0].poi_btn.setChecked(True)
        assert mw._poi_specs() == [(("raw", 17, 0), 200)]

    def test_expr_spec(self, mw):
        mw._poi_entries = [gat.LayerEntry(
            key=gat.LayerKey(-1, 0, name="L0", synthetic=True), polygons=[],
            expr_text="A & B", expr_bindings={"A": (17, 0), "B": (6, 0)})]
        # Synthetic POIs carry a recipes snapshot (4th element) so nested
        # synthetic refs resolve over the ROI during batch fine align; with no
        # other synthetic layers defined the snapshot is empty.
        assert mw._poi_specs() == [
            (("expr", "A & B", {"A": (17, 0), "B": (6, 0)}, {}), 200)]

    def test_none_spec(self, mw):
        mw._poi_entries = []
        assert mw._poi_specs() == []

    def test_multi_spec(self, mw):
        doc, _ = _doc_two_layers()
        mw.layer_panel.set_document(doc)
        mw.layer_panel._rows[0].poi_btn.setChecked(True)
        mw.layer_panel._rows[1].poi_btn.setChecked(True)
        assert mw._poi_specs() == [(("raw", 17, 0), 200), (("raw", 6, 0), 200)]
