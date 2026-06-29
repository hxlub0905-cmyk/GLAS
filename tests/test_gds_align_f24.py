"""F24 tests: one-click "Export all" (fine-align only the un-run images, then
export) + the human-viewable colourised label preview (``label_view_png``).

The pure helpers (``images_needing_fine_align`` / ``colorize_label_map``) need
only numpy; the ``_on_export_all`` wiring tests additionally need PyQt6.
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


# ── _on_export_all wiring (PyQt6) ────────────────────────────────────────────

try:
    from PyQt6.QtWidgets import QApplication, QMessageBox
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


class TestOnExportAll:

    def test_no_images_does_nothing(self, mw, monkeypatch):
        launched = []
        monkeypatch.setattr(mw, "_launch_fa",
                            lambda *a: launched.append(a))
        mw._sem_images = []
        mw._on_export_all()
        assert launched == []

    def test_all_run_exports_directly(self, mw, monkeypatch):
        exported, launched = [], []
        monkeypatch.setattr(mw, "_on_export_alignment",
                            lambda: exported.append(True))
        monkeypatch.setattr(mw, "_launch_fa", lambda *a: launched.append(a))
        mw._sem_images = [_img("D1", 1.0, 2.0)]
        mw._refined = {"D1": (0.0, 0.0, 0.9)}
        mw._on_export_all()
        assert exported == [True] and launched == []
        assert mw._export_after_fa is False   # straight export, no batch

    def test_only_un_run_images_launched(self, mw, monkeypatch):
        captured = {}
        monkeypatch.setattr(mw, "_enter_batch_workspace", lambda: None)
        monkeypatch.setattr(mw, "_refresh_batch_panel", lambda *a, **k: None)
        monkeypatch.setattr(mw, "_poi_specs", lambda: ["spec"])
        monkeypatch.setattr(mw, "_coarse_gds", lambda im: (0.0, 0.0))
        monkeypatch.setattr(mw.batch_panel, "set_rerun_defaults",
                            lambda *a, **k: None)
        monkeypatch.setattr(mw, "_launch_fa",
                            lambda specs, jobs, cfg: captured.update(jobs=jobs))
        mw._rar = object()
        mw._roi_root = "root"
        mw._fov_w = mw._fov_h = 1000.0
        mw._sem_images = [_img("D1", 1.0, 2.0), _img("D2", 3.0, 4.0),
                          _img("D3")]                       # D3 has no coords
        mw._refined = {"D1": (0.0, 0.0, 0.9)}               # D1 already run
        mw._on_export_all()
        assert [j[0] for j in captured["jobs"]] == ["D2"]   # only the un-run one
        assert mw._export_after_fa is True

    def test_finish_hands_off_to_export(self, mw, monkeypatch):
        exported = []
        monkeypatch.setattr(mw, "_on_export_alignment",
                            lambda: exported.append(True))
        monkeypatch.setattr(mw, "_refresh_overview_defects", lambda: None)
        monkeypatch.setattr(mw, "_refresh_batch_panel", lambda *a, **k: None)

        class _FakeTimer:
            @staticmethod
            def singleShot(_ms, fn):
                fn()
        monkeypatch.setattr(gat, "QTimer", _FakeTimer)
        mw._current_sem = None
        mw._export_after_fa = True
        mw._on_fa_finished(3)
        assert exported == [True] and mw._export_after_fa is False

    def test_finish_without_flag_does_not_export(self, mw, monkeypatch):
        exported = []
        monkeypatch.setattr(mw, "_on_export_alignment",
                            lambda: exported.append(True))
        monkeypatch.setattr(mw, "_refresh_overview_defects", lambda: None)
        monkeypatch.setattr(mw, "_refresh_batch_panel", lambda *a, **k: None)
        mw._current_sem = None
        mw._export_after_fa = False
        mw._on_fa_finished(3)
        assert exported == []

    def test_failure_clears_flag_no_export(self, mw, monkeypatch):
        monkeypatch.setattr(mw, "_refresh_batch_panel", lambda *a, **k: None)
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
        mw._export_after_fa = True
        mw._on_fa_failed("boom")
        assert mw._export_after_fa is False

    def test_cancel_clears_flag_no_export(self, mw, monkeypatch):
        monkeypatch.setattr(mw, "_refresh_batch_panel", lambda *a, **k: None)
        monkeypatch.setattr(mw, "_refresh_overview_defects", lambda: None)
        mw._export_after_fa = True
        mw._on_fa_cancelled()
        assert mw._export_after_fa is False
