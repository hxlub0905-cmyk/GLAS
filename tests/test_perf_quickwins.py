"""Perf / workflow quick-win tests (post-audit).

Covers four changes landed together on the perf-optimization branch:

* export pool ships the orchestrator's name-table index to each worker
  (``overlay_export._export_pool_init(prebuilt_index=…)``) so a worker reuses
  it instead of re-running ``scan_cell_offsets`` — mirrors the F23 fine-align
  path;
* the F24 gray+label export shares one ``make_mask`` raster (already covered in
  ``test_export_perf.py``; the byte-equality test lives there);
* ROI-load clicks coalesce-to-latest instead of being dropped while a load runs;
* the PART/CHIP pick and per-dialog last directory persist across sessions.

The pure / core pieces need only numpy (+ shapely/cv2 where noted); the GUI
wiring additionally needs PyQt6 and is skipped where it is unavailable.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import overlay_export        # noqa: E402
import oasis_random          # noqa: E402
# Reuse the byte-builder from the random-access tests.
import test_oasis_random as tr  # noqa: E402


# ── export pool: prebuilt_index reuse (mirror of F23) ────────────────────────

def test_export_pool_init_accepts_and_reuses_prebuilt_index(tmp_path):
    data, _, _ = tr._build_two_cell()
    p = tmp_path / "two.oas"
    p.write_bytes(data)
    rar = oasis_random.RandomAccessReader(p, wanted_layers={(17, 0)})
    idx = rar.index_snapshot()
    try:
        overlay_export._export_pool_init(
            str(p), {(17, 0)}, np.int32, None, idx,
            root=0, poi=[], cfg={}, out_dir=str(tmp_path),
            export_raw=False, export_overlay=False, export_gray=False,
            export_label=False, score_thr=0.0)
        worker_rar = overlay_export._GE["rar"]
        # The worker reused the injected index (no rescan): same offset maps.
        assert worker_rar._idx is idx
        assert worker_rar._by_refnum == rar._by_refnum
        assert worker_rar._by_name == rar._by_name
    finally:
        overlay_export._GE.clear()


def test_export_pool_init_signature_has_prebuilt_index():
    import inspect
    params = list(inspect.signature(
        overlay_export._export_pool_init).parameters)
    # Mirrors the RandomAccessReader filter args + the injected index, then the
    # immutable export context. Order must match the initargs tuple the worker
    # is launched with.
    assert params[:5] == [
        "path", "wanted_layers", "dtype", "bbox_layer", "prebuilt_index"]


# ── GUI wiring (PyQt6) ───────────────────────────────────────────────────────

try:
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover
    pytest.skip("PyQt6 unavailable", allow_module_level=True)

import gds_align_tool as gat   # noqa: E402
import parts_catalog           # noqa: E402
from parts_catalog import ChipSpec, PartSpec  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path):
    # Redirect QSettings to a throwaway dir so tests never touch (or depend on)
    # the developer's real GLAS settings.
    QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope,
                      str(tmp_path / "qsettings"))
    QSettings("GLAS", "GLAS").clear()
    yield
    QSettings("GLAS", "GLAS").clear()


def _catalog_file(tmp_path):
    catalog_path = tmp_path / "parts.json"
    parts_catalog.save_catalog(catalog_path, {
        "PARTA": PartSpec(description="a", chips={
            "C1": ChipSpec(chip_x_um=10.0, chip_y_um=20.0,
                           chip_w_um=1000.0, chip_h_um=2000.0,
                           fov_w_nm=1500.0, fov_h_nm=1500.0),
            "C2": ChipSpec(chip_x_um=30.0, chip_y_um=40.0,
                           chip_w_um=1000.0, chip_h_um=2000.0,
                           fov_w_nm=1800.0, fov_h_nm=1800.0)}),
        "PARTB": PartSpec(description="b", chips={
            "D1": ChipSpec(chip_x_um=1.0, chip_y_um=2.0,
                           chip_w_um=10.0, chip_h_um=20.0,
                           fov_w_nm=900.0, fov_h_nm=900.0)}),
    })
    return catalog_path


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    path = _catalog_file(tmp_path)
    monkeypatch.setattr(parts_catalog, "default_catalog_path", lambda: path)
    monkeypatch.setattr(gat, "default_catalog_path", lambda: path)
    return path


# ── PART/CHIP persistence ────────────────────────────────────────────────────

class TestPartChipPersistence:

    def test_default_is_alphabetically_first(self, qapp, catalog):
        panel = gat.PartChipPanel()
        # No saved pick → alphabetically-first PART, that PART's first CHIP.
        assert panel._part_cb.currentText() == "PARTA"
        assert panel._chip_cb.currentText() == "C1"

    def test_user_pick_persists_and_restores(self, qapp, catalog):
        panel = gat.PartChipPanel()
        # Simulate a genuine user selection of PARTB → D1, then PARTA → C2.
        panel._part_cb.setCurrentText("PARTA")
        panel._chip_cb.setCurrentText("C2")
        panel._save_selection()                 # textActivated handler
        assert QSettings("GLAS", "GLAS").value(
            panel._SETTINGS_PART, type=str) == "PARTA"
        assert QSettings("GLAS", "GLAS").value(
            panel._SETTINGS_CHIP, type=str) == "C2"
        # A fresh panel restores the saved catalog entry.
        panel2 = gat.PartChipPanel()
        assert panel2._part_cb.currentText() == "PARTA"
        assert panel2._chip_cb.currentText() == "C2"

    def test_restore_only_for_catalog_entries(self, qapp, catalog):
        # A saved pick no longer in the catalog must fall back to the default,
        # never inject a non-catalog id into the dropdown (§7).
        QSettings("GLAS", "GLAS").setValue(
            gat.PartChipPanel._SETTINGS_PART, "GONE")
        QSettings("GLAS", "GLAS").setValue(
            gat.PartChipPanel._SETTINGS_CHIP, "ZZZ")
        panel = gat.PartChipPanel()
        assert panel._part_cb.currentText() == "PARTA"   # default
        assert panel._part_cb.findText("GONE") < 0       # not added
        assert panel._chip_cb.findText("ZZZ") < 0


# ── ROI coalesce-to-latest ───────────────────────────────────────────────────

@pytest.fixture
def mw(qapp, catalog):
    w = gat.MainWindow()
    yield w
    w.close()


class TestRoiCoalesce:

    def _stub_select(self, mw, monkeypatch, pos):
        # Neutralise the unrelated per-click UI work so the test isolates the
        # ROI-load decision.
        monkeypatch.setattr(mw.sem_viewer, "set_image", lambda *a, **k: None)
        monkeypatch.setattr(mw, "_jump_to_image", lambda *a, **k: None)
        monkeypatch.setattr(mw, "_refresh_overview_defects", lambda: None)
        monkeypatch.setattr(mw, "_update_guidance", lambda: None)
        monkeypatch.setattr(mw, "_current_image_gds", lambda: pos)
        loaded = []
        monkeypatch.setattr(mw, "_load_roi_around",
                            lambda *a, **k: loaded.append(a))
        mw._rar = object()
        mw._fov_w = mw._fov_h = 1000.0
        return loaded

    def test_click_while_busy_parks_latest(self, mw, monkeypatch):
        loaded = self._stub_select(mw, monkeypatch, (111.0, 222.0))
        mw._roi_thread = object()                 # a load is "running"
        mw._on_sem_image_selected(object())
        assert loaded == []                        # not loaded now…
        assert mw._roi_pending_pos == (111.0, 222.0)   # …parked instead

    def test_idle_click_loads_immediately(self, mw, monkeypatch):
        loaded = self._stub_select(mw, monkeypatch, (5.0, 6.0))
        mw._roi_thread = None
        mw._on_sem_image_selected(object())
        assert loaded == [(5.0, 6.0)]
        assert mw._roi_pending_pos is None

    def test_cleanup_kicks_pending(self, mw, monkeypatch):
        loaded = []
        monkeypatch.setattr(mw, "_load_roi_around",
                            lambda *a, **k: loaded.append(a))
        mw._rar = object()
        mw._fov_w = mw._fov_h = 1000.0
        mw._roi_center = (9.0, 9.0)
        mw._roi_pending_pos = (1.0, 2.0)
        mw._cleanup_roi()
        assert loaded == [(1.0, 2.0)]
        assert mw._roi_pending_pos is None

    def test_cleanup_skips_pending_equal_to_loaded(self, mw, monkeypatch):
        loaded = []
        monkeypatch.setattr(mw, "_load_roi_around",
                            lambda *a, **k: loaded.append(a))
        mw._rar = object()
        mw._fov_w = mw._fov_h = 1000.0
        mw._roi_center = (4.0, 4.0)
        mw._roi_pending_pos = (4.0, 4.0)           # same spot just loaded
        mw._cleanup_roi()
        assert loaded == []                        # no redundant reload
        assert mw._roi_pending_pos is None


# ── dialog last-directory memory ─────────────────────────────────────────────

class TestDialogDirMemory:

    def test_file_dialog_remembers_parent(self, mw):
        assert mw._dlg_start_dir("klarf") == ""    # nothing yet
        mw._dlg_remember("klarf", str(Path("/a/b/lot.000")))
        assert mw._dlg_start_dir("klarf") == str(Path("/a/b"))

    def test_dir_dialog_remembers_dir_itself(self, mw):
        mw._dlg_remember("folder", str(Path("/x/y/images")), is_dir=True)
        assert mw._dlg_start_dir("folder") == str(Path("/x/y/images"))

    def test_keys_are_independent(self, mw):
        mw._dlg_remember("klarf", str(Path("/k/file.klarf")))
        mw._dlg_remember("export_images", str(Path("/e/out")), is_dir=True)
        assert mw._dlg_start_dir("klarf") == str(Path("/k"))
        assert mw._dlg_start_dir("export_images") == str(Path("/e/out"))
        assert mw._dlg_start_dir("never_set") == ""
