"""F24 tests: one-click "Export all" (fine-align only the un-run images, then
export) + the human-viewable colourised label preview (``label_view_png``).

The pure helpers (``images_needing_fine_align`` / ``colorize_label_map``) need
only numpy; the ``_on_export`` wiring tests additionally need PyQt6 (F25 folded
the old two-button "Export all" into the single unified Export path).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

# conftest puts glas/core + glas/app on sys.path; importing flat works.
_ROOT = Path(__file__).resolve().parents[1]
for sub in ("glas/core", "glas/app"):
    if str(_ROOT / sub) not in sys.path:
        sys.path.insert(0, str(_ROOT / sub))

import fine_align  # noqa: E402
import sem_loader  # noqa: E402


def _img(image_id, x=None, y=None):
    return sem_loader.SemImage(image_id=image_id, filename=f"{image_id}.png",
                               file_path=Path(f"{image_id}.png"),
                               xrel=x, yrel=y)


# ── images_needing_fine_align (F24) ──────────────────────────────────────────

class TestImagesNeedingFineAlign:

    def test_all_un_run(self):
        imgs = [_img("D1", 1.0, 2.0), _img("D2", 3.0, 4.0)]
        todo = fine_align.images_needing_fine_align(imgs, {})
        assert [i.image_id for i in todo] == ["D1", "D2"]

    def test_skips_already_run(self):
        imgs = [_img("D1", 1.0, 2.0), _img("D2", 3.0, 4.0),
                _img("D3", 5.0, 6.0)]
        todo = fine_align.images_needing_fine_align(
            imgs, {"D1": (0.0, 0.0, 0.9), "D3": (0.0, 0.0, 0.8)})
        assert [i.image_id for i in todo] == ["D2"]

    def test_all_run_returns_empty(self):
        imgs = [_img("D1", 1.0, 2.0)]
        assert fine_align.images_needing_fine_align(
            imgs, {"D1": (0.0, 0.0, 0.9)}) == []

    def test_excludes_no_coords(self):
        imgs = [_img("D1", 1.0, 2.0), _img("D2")]  # D2 has no coords
        todo = fine_align.images_needing_fine_align(imgs, {})
        assert [i.image_id for i in todo] == ["D1"]

    def test_preserves_dataset_order(self):
        imgs = [_img(n, 1.0, 2.0) for n in ("D3", "D1", "D2")]
        todo = fine_align.images_needing_fine_align(imgs, {"D1": (0, 0, 0.9)})
        assert [i.image_id for i in todo] == ["D3", "D2"]

    def test_handles_none_refined(self):
        imgs = [_img("D1", 1.0, 2.0)]
        assert [i.image_id for i in
                fine_align.images_needing_fine_align(imgs, None)] == ["D1"]


# ── colorize_label_map (F24 label_view) ──────────────────────────────────────

class TestColorizeLabelMap:

    def test_paints_each_id_its_colour(self):
        lbl = np.array([[0, 1], [2, 1]], dtype=np.uint8)
        view = fine_align.colorize_label_map(
            lbl, {1: (255, 0, 0), 2: (0, 255, 0)})
        assert view.shape == (2, 2, 3) and view.dtype == np.uint8
        assert tuple(view[0, 1]) == (255, 0, 0)   # id 1
        assert tuple(view[1, 1]) == (255, 0, 0)   # id 1
        assert tuple(view[1, 0]) == (0, 255, 0)   # id 2

    def test_background_is_bg_rgb(self):
        lbl = np.array([[0, 1]], dtype=np.uint8)
        view = fine_align.colorize_label_map(
            lbl, {1: (10, 20, 30)}, bg_rgb=(7, 8, 9))
        assert tuple(view[0, 0]) == (7, 8, 9)     # id 0 → background

    def test_unmapped_id_stays_background(self):
        lbl = np.array([[0, 5]], dtype=np.uint8)
        view = fine_align.colorize_label_map(lbl, {1: (255, 255, 255)})
        assert tuple(view[0, 1]) == (0, 0, 0)     # id 5 not in map → bg

    def test_all_black_label_is_now_visible(self):
        # The reported bug: a single-POI label map is 0/1 → looks all-black.
        # The preview must make the id-1 region clearly non-black.
        lbl = np.zeros((4, 4), dtype=np.uint8)
        lbl[1:3, 1:3] = 1
        view = fine_align.colorize_label_map(lbl, {1: (0, 180, 255)})
        assert view.max() > 0 and tuple(view[1, 1]) == (0, 180, 255)


# ── _on_export wiring (PyQt6) — F25 single unified Export path ────────────────

try:
    from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox
    from PyQt6.QtWidgets import QDialog
except Exception:  # pragma: no cover
    pytest.skip("PyQt6 unavailable", allow_module_level=True)

import gds_align_tool as gat  # noqa: E402
import parts_catalog  # noqa: E402
from parts_catalog import ChipSpec, PartSpec  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def mw(qapp, tmp_path, monkeypatch):
    catalog_path = tmp_path / "parts.json"
    parts_catalog.save_catalog(catalog_path, {
        "TMVG10": PartSpec(description="test", chips={
            "C1": ChipSpec(chip_x_um=10.0, chip_y_um=20.0,
                           chip_w_um=1000.0, chip_h_um=2000.0,
                           fov_w_nm=1500.0, fov_h_nm=1500.0)}),
    })
    monkeypatch.setattr(parts_catalog, "default_catalog_path",
                        lambda: catalog_path)
    monkeypatch.setattr(gat, "default_catalog_path", lambda: catalog_path)
    w = gat.MainWindow()
    yield w
    w.close()


def _patch_export_dialog(monkeypatch, sel, accepted=True):
    """Stub AlignmentExportDialog so _on_export's options come from ``sel``
    (fmt, ids, raw, overlay, gray, label, score_thr) without a real modal."""
    code = (QDialog.DialogCode.Accepted if accepted
            else QDialog.DialogCode.Rejected)

    class _Dlg:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return code

        def selected(self):
            return sel

    monkeypatch.setattr(gat, "AlignmentExportDialog", _Dlg)


class TestOnExport:

    def _stub_common(self, mw, monkeypatch):
        monkeypatch.setattr(mw, "_enter_batch_workspace", lambda: None)
        monkeypatch.setattr(mw, "_refresh_batch_panel", lambda *a, **k: None)
        monkeypatch.setattr(mw, "_poi_specs_colored", lambda: ["spec"])
        monkeypatch.setattr(mw, "_coarse_gds", lambda im: (0.0, 0.0))
        monkeypatch.setattr(mw.batch_panel, "set_rerun_defaults",
                            lambda *a, **k: None)
        monkeypatch.setattr(gat.QFileDialog, "getSaveFileName",
                            staticmethod(lambda *a, **k: ("/tmp/a.csv", "")))
        monkeypatch.setattr(gat.QFileDialog, "getExistingDirectory",
                            staticmethod(lambda *a, **k: "/tmp/out"))

    def test_no_images_does_nothing(self, mw, monkeypatch):
        launched = []
        monkeypatch.setattr(mw, "_launch_export",
                            lambda *a: launched.append(a))
        mw._sem_images = []
        mw._on_export()
        assert launched == []

    def test_dialog_cancel_aborts(self, mw, monkeypatch):
        launched = []
        monkeypatch.setattr(mw, "_launch_export",
                            lambda *a: launched.append(a))
        _patch_export_dialog(monkeypatch, sel=None, accepted=False)
        mw._sem_images = [_img("D1", 1.0, 2.0)]
        mw._on_export()
        assert launched == []

    def test_unified_pass_covers_all_selected_with_prior_refined(
            self, mw, monkeypatch):
        captured = {}
        self._stub_common(mw, monkeypatch)
        monkeypatch.setattr(
            mw, "_launch_export",
            lambda specs, jobs, cfg, out_dir, *a: captured.update(
                jobs=jobs, out_dir=out_dir))
        mw._rar = object()
        mw._roi_root = "root"
        mw._fov_w = mw._fov_h = 1000.0
        mw._sem_images = [_img("D1", 1.0, 2.0), _img("D2", 3.0, 4.0)]
        mw._refined = {"D1": (0.5, 0.6, 0.9)}            # D1 already aligned
        # overlay product requested → an output dir is picked.
        _patch_export_dialog(monkeypatch, sel=(
            "csv", {"D1", "D2"}, False, True, False, False, 0.0))
        mw._on_export()
        # ALL selected images are jobs (one walk each), carrying prior_refined.
        assert [j[0] for j in captured["jobs"]] == ["D1", "D2"]
        assert captured["jobs"][0][2] == (0.5, 0.6, 0.9)   # D1 reuse
        assert captured["jobs"][1][2] is None              # D2 un-run
        assert captured["out_dir"] == "/tmp/out"
        assert mw._export_pending is not None

    def test_raw_only_requests_output_dir(self, mw, monkeypatch):
        # Raw-only export is an image product → an output dir must be picked
        # (regression: raw used to skip the dir and land PNGs in the CWD), but
        # it needs no POI / OASIS (no walk).
        captured = {}
        self._stub_common(mw, monkeypatch)
        monkeypatch.setattr(mw, "_launch_export",
                            lambda specs, jobs, cfg, out_dir, *a:
                            captured.update(out_dir=out_dir, specs=specs))
        mw._sem_images = [_img("D1", 1.0, 2.0)]
        mw._refined = {"D1": (0.0, 0.0, 0.9)}            # aligned → no walk
        _patch_export_dialog(monkeypatch, sel=(
            "csv", {"D1"}, True, False, False, False, 0.0))   # raw only
        mw._on_export()
        assert captured["out_dir"] == "/tmp/out"         # dir WAS requested
        assert captured["specs"] == []                   # no POI needed

    def test_csv_only_skips_output_dir(self, mw, monkeypatch):
        captured = {}
        self._stub_common(mw, monkeypatch)
        # No image products → getExistingDirectory must NOT be needed; make it
        # raise so the test fails if _on_export asks for an output dir.
        monkeypatch.setattr(gat.QFileDialog, "getExistingDirectory",
                            staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("should not pick a dir"))))
        monkeypatch.setattr(mw, "_launch_export",
                            lambda specs, jobs, cfg, out_dir, *a:
                            captured.update(out_dir=out_dir))
        mw._sem_images = [_img("D1", 1.0, 2.0)]
        mw._refined = {"D1": (0.0, 0.0, 0.9)}            # already aligned
        _patch_export_dialog(monkeypatch, sel=(
            "csv", {"D1"}, False, False, False, False, 0.0))
        mw._on_export()
        assert captured["out_dir"] == ""

    def test_finish_writes_alignment_manifest_and_clears_pending(
            self, mw, monkeypatch):
        wrote = []
        monkeypatch.setattr(mw, "_refresh_overview_defects", lambda: None)
        monkeypatch.setattr(mw, "_refresh_batch_panel", lambda *a, **k: None)
        monkeypatch.setattr(
            mw, "_write_alignment_manifest",
            lambda images, fmt, path: wrote.append((fmt, path)) or path)
        mw._current_sem = None
        mw._export_pending = {"images": [_img("D1", 1.0, 2.0)],
                              "fmt": "csv", "path": "/tmp/a.csv"}
        mw._on_export_finished(1, "")
        assert wrote == [("csv", "/tmp/a.csv")]
        assert mw._export_pending is None

    def test_failure_clears_pending(self, mw, monkeypatch):
        monkeypatch.setattr(mw, "_refresh_batch_panel", lambda *a, **k: None)
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
        mw._export_pending = {"images": [], "fmt": "csv", "path": "/tmp/a.csv"}
        mw._on_fa_failed("boom")
        assert mw._export_pending is None

    def test_cancel_clears_pending(self, mw, monkeypatch):
        monkeypatch.setattr(mw, "_refresh_batch_panel", lambda *a, **k: None)
        monkeypatch.setattr(mw, "_refresh_overview_defects", lambda: None)
        mw._export_pending = {"images": [], "fmt": "csv", "path": "/tmp/a.csv"}
        mw._on_fa_cancelled()
        assert mw._export_pending is None
