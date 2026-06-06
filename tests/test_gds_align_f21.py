"""F21 UI tests: PartChipPanel / AlignmentDeltaPanel / CatalogEditorDialog
and the MainWindow integration (no fine-tune, δ wired to AlignmentDeltaPanel,
cache schema v5 round-trip, dev-mode catalog editor)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

# conftest puts glas/core + glas/app on sys.path; importing flat works.
_ROOT = Path(__file__).resolve().parents[1]
for sub in ("glas/core", "glas/app"):
    if str(_ROOT / sub) not in sys.path:
        sys.path.insert(0, str(_ROOT / sub))

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover
    pytest.skip("PyQt6 unavailable", allow_module_level=True)

import gds_align_tool as gat
import gds_layer_cache
import parts_catalog
import sem_loader
from parts_catalog import ChipSpec, PartSpec


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def mw(qapp, tmp_path, monkeypatch):
    # Redirect the catalog default path to a tmp file so tests don't see /
    # write the real seed catalog.
    catalog_path = tmp_path / "parts.json"
    parts_catalog.save_catalog(catalog_path, {
        "TMVG10": PartSpec(
            description="test product",
            chips={
                "C1": ChipSpec(chip_x_um=10.0, chip_y_um=20.0,
                               chip_w_um=1000.0, chip_h_um=2000.0,
                               fov_w_nm=1500.0, fov_h_nm=1500.0),
                "C2": ChipSpec(chip_x_um=2010.0, chip_y_um=20.0,
                               chip_w_um=1000.0, chip_h_um=2000.0,
                               fov_w_nm=2000.0, fov_h_nm=2000.0,
                               nm_per_px=1.25),
            },
        ),
    })
    monkeypatch.setattr(parts_catalog, "default_catalog_path",
                        lambda: catalog_path)
    monkeypatch.setattr(gat, "default_catalog_path",
                        lambda: catalog_path)
    w = gat.MainWindow()
    yield w
    w.close()


# ── PartChipPanel ────────────────────────────────────────────────────────────

class TestPartChipPanel:
    def test_dropdowns_populated_from_catalog(self, mw):
        p = mw.sem_panel.coord_setup
        assert p._part_cb.count() == 1
        assert p._part_cb.itemText(0) == "TMVG10"
        assert p._chip_cb.count() == 2

    def test_selecting_chip_sets_chip_corner_and_fov(self, mw):
        p = mw.sem_panel.coord_setup
        p._part_cb.setCurrentText("TMVG10")
        p._chip_cb.setCurrentText("C1")
        v = p.values()
        assert v["part_id"] == "TMVG10"
        assert v["chip_id"] == "C1"
        assert v["chip_x_um"] == 10.0
        # chip_corner = (DieX − GDS_off) × 1000
        assert v["chip_corner_x"] == pytest.approx(10_000.0)
        assert v["chip_corner_y"] == pytest.approx(20_000.0)
        # FOV from catalog default.
        assert v["fov_w"] == 1500.0
        assert v["fov_h"] == 1500.0

    def test_values_dict_has_no_fine_tune(self, mw):
        v = mw.sem_panel.coord_setup.values()
        assert "fine_dx" not in v
        assert "fine_dy" not in v

    def test_custom_fov_overrides_catalog(self, mw):
        p = mw.sem_panel.coord_setup
        p._part_cb.setCurrentText("TMVG10")
        p._chip_cb.setCurrentText("C1")
        p._custom_chk.setChecked(True)
        p._fov_w.setValue(3000)
        p._fov_h.setValue(2500)
        v = p.values()
        assert v["fov_w"] == 3000.0
        assert v["fov_h"] == 2500.0
        # Disabling Custom snaps back to catalog defaults.
        p._custom_chk.setChecked(False)
        v2 = p.values()
        assert v2["fov_w"] == 1500.0

    def test_custom_seeded_from_current_chip(self, mw):
        p = mw.sem_panel.coord_setup
        p._chip_cb.setCurrentText("C2")
        p._custom_chk.setChecked(True)
        # C2's default 2000 / 1.25 nm-per-px should pre-populate the
        # override spinboxes so the user is editing from a reasonable start.
        assert p._fov_w.value() == 2000
        assert p._fov_h.value() == 2000
        assert p._nm_auto.isChecked() is False
        assert p._nm_per_px.value() == pytest.approx(1.25)

    def test_empty_catalog_disables_dropdowns(self, mw, monkeypatch):
        monkeypatch.setattr(parts_catalog, "load_catalog", lambda *_: {})
        # Inject same shim into the namespace the app imported from.
        monkeypatch.setattr(gat, "load_catalog", lambda *_: {})
        mw.sem_panel.coord_setup.reload_catalog()
        p = mw.sem_panel.coord_setup
        assert p._part_cb.isEnabled() is False
        assert p._chip_cb.isEnabled() is False
        # S11: empty-catalog hint is rephrased and styled as a warning card
        # ("No PART / CHIP data" + admin advice).
        assert "No PART" in p._status_lbl.text()

    def test_changed_signal_emits_on_chip_select(self, mw):
        p = mw.sem_panel.coord_setup
        fired = {"n": 0}
        p.changed.connect(lambda: fired.__setitem__("n", fired["n"] + 1))
        p._chip_cb.setCurrentText("C2")
        assert fired["n"] >= 1


# ── AlignmentDeltaPanel ──────────────────────────────────────────────────────

class TestAlignmentDeltaPanel:
    def test_set_values_updates_big_labels(self, mw):
        ad = mw.sem_panel.alignment_delta
        ad.set_values(1234.0, -567.0)
        assert "1,234" in ad._x_lbl.text()
        assert "-567" in ad._y_lbl.text()
        assert ad._dx == 1234.0
        assert ad._dy == -567.0

    def test_set_offset_button_emits_signal(self, mw):
        ad = mw.sem_panel.alignment_delta
        # S7: panel starts disabled until SEM images load; enable so the
        # signal-emission contract is testable independently of the gate.
        ad.setEnabled(True)
        fired = {"n": 0}
        ad.set_requested.connect(lambda: fired.__setitem__("n", 1))
        ad.set_btn.click()
        assert fired["n"] == 1

    def test_clear_button_emits_signal(self, mw):
        ad = mw.sem_panel.alignment_delta
        ad.setEnabled(True)
        fired = {"n": 0}
        ad.clear_requested.connect(lambda: fired.__setitem__("n", 1))
        ad.clear_btn.click()
        assert fired["n"] == 1

    def test_copy_to_clipboard(self, mw, qapp):
        ad = mw.sem_panel.alignment_delta
        ad.set_values(100.0, -50.0)
        ad._on_copy()
        assert qapp.clipboard().text() == "100, -50"

    def test_panel_is_not_collapsible(self, mw):
        # Plan Q7: AlignmentDeltaPanel must be always visible (no
        # CollapsibleSection wrapping).
        ad = mw.sem_panel.alignment_delta
        assert ad.isVisibleTo(mw.sem_panel)


# ── Cache schema v5 round-trip ───────────────────────────────────────────────

class TestCacheV5:
    def test_part_chip_round_trip(self, mw, tmp_path):
        meta = gds_layer_cache.make_meta(
            __file__,
            chip_corner_x=10_000.0, chip_corner_y=20_000.0,
            chip_x_um=10.0, chip_y_um=20.0,
            fov_w=1500.0, fov_h=1500.0,
            top_cell_name="TOP",
            part_id="TMVG10", chip_id="C1")
        layers = [(1, 0,
                   [np.array([[0., 0.], [1., 0.], [1., 1.], [0., 1.]],
                             dtype=np.float32)],
                   np.array([[0., 0., 1., 1.]], dtype=np.float32))]
        out = tmp_path / "cache.npz"
        gds_layer_cache.cache_save(out, layers, meta)
        loaded = gds_layer_cache.cache_load(out)
        assert loaded is not None
        assert loaded.meta.schema_version == 5
        assert loaded.meta.part_id == "TMVG10"
        assert loaded.meta.chip_id == "C1"

    def test_set_from_meta_restores_dropdowns(self, mw):
        meta = gds_layer_cache.LayerCacheMeta(
            source_oas="x.oas", source_mtime=0.0, source_size=0,
            chip_corner_x=10_000.0, chip_corner_y=20_000.0,
            chip_x_um=10.0, chip_y_um=20.0,
            fov_w=1500.0, fov_h=1500.0,
            part_id="TMVG10", chip_id="C2")
        mw.sem_panel.coord_setup.set_from_meta(meta)
        v = mw.sem_panel.coord_setup.values()
        assert v["part_id"] == "TMVG10"
        assert v["chip_id"] == "C2"

    def test_set_from_meta_preserves_custom_fov_override(self, mw):
        # Codex PR #11 review: a cache exported with Custom FOV active
        # stores the overridden FOV/scale in meta. The v5 restore path must
        # re-enable Custom and load those values, not silently snap back to
        # the catalog defaults (which would shift the alignment).
        panel = mw.sem_panel.coord_setup
        # The fixture catalog has TMVG10/C1 with fov_w_nm=1500 by default.
        meta = gds_layer_cache.LayerCacheMeta(
            source_oas="x.oas", source_mtime=0.0, source_size=0,
            chip_corner_x=10_000.0, chip_corner_y=20_000.0,
            chip_x_um=10.0, chip_y_um=20.0,
            fov_w=3000.0, fov_h=2500.0,   # divergent from catalog
            nm_per_px=1.75,                # also divergent
            part_id="TMVG10", chip_id="C1")
        panel.set_from_meta(meta)
        v = panel.values()
        assert v["part_id"] == "TMVG10"
        assert v["chip_id"] == "C1"
        # Custom override must be on and carry the cached values.
        assert panel._customising is True
        assert panel._custom_chk.isChecked() is True
        assert v["fov_w"] == 3000.0
        assert v["fov_h"] == 2500.0
        assert v["nm_auto"] is False
        assert v["nm_per_px_manual"] == pytest.approx(1.75)

    def test_set_from_meta_matching_fov_skips_custom(self, mw):
        # Counter-check: when cached FOV / scale equal the catalog defaults,
        # Custom must NOT be enabled (it's only restored on divergence).
        panel = mw.sem_panel.coord_setup
        meta = gds_layer_cache.LayerCacheMeta(
            source_oas="x.oas", source_mtime=0.0, source_size=0,
            chip_corner_x=10_000.0, chip_corner_y=20_000.0,
            chip_x_um=10.0, chip_y_um=20.0,
            fov_w=1500.0, fov_h=1500.0,
            nm_per_px=0.0,   # 0 => auto, matches chip C1 default (nm_per_px=None)
            part_id="TMVG10", chip_id="C1")
        panel.set_from_meta(meta)
        assert panel._customising is False
        assert panel._custom_chk.isChecked() is False

    def test_set_from_meta_legacy_v4(self, mw):
        # v4 cache: no part_id/chip_id → "legacy snapshot" mode shows the
        # stored coordinates and disables the dropdowns.
        meta = gds_layer_cache.LayerCacheMeta(
            source_oas="x.oas", source_mtime=0.0, source_size=0,
            chip_corner_x=99_000.0, chip_corner_y=88_000.0,
            fov_w=2500.0, fov_h=2000.0,
            part_id=None, chip_id=None)
        mw.sem_panel.coord_setup.set_from_meta(meta)
        v = mw.sem_panel.coord_setup.values()
        assert v["chip_corner_x"] == 99_000.0
        assert v["chip_corner_y"] == 88_000.0
        assert v["fov_w"] == 2500.0
        assert mw.sem_panel.coord_setup._part_cb.isEnabled() is False


# ── MainWindow integration ───────────────────────────────────────────────────

class TestMainWindowIntegration:
    def test_no_fine_tune_state(self, mw):
        assert not hasattr(mw, "_fine_dx")
        assert not hasattr(mw, "_fine_dy")

    def test_overlay_drag_updates_alignment_delta_only(self, mw):
        mw._origin_dx, mw._origin_dy = 100.0, -50.0
        mw.sem_viewer._drag_x, mw.sem_viewer._drag_y = 30.0, 40.0
        mw._on_overlay_drag()
        ad = mw.sem_panel.alignment_delta
        assert ad._dx == 70.0    # 100 − 30
        assert ad._dy == -90.0   # −50 − 40

    def test_set_offset_via_alignment_delta_signal(self, mw):
        # S7: panel starts disabled until SEM images load; enable for this
        # signal-routing test.
        mw.sem_panel.alignment_delta.setEnabled(True)
        mw._origin_dx, mw._origin_dy = 0.0, 0.0
        mw.sem_viewer._anchor = (1000.0, 2000.0)
        mw.sem_viewer._nm_per_px = 1.0
        mw.sem_viewer._drag_x, mw.sem_viewer._drag_y = 50.0, -25.0
        mw._current_sem = None
        mw.sem_panel.alignment_delta.set_btn.click()
        # δ now carries the drag; viewer drag reset.
        assert mw._origin_dx == -50.0
        assert mw._origin_dy == 25.0
        assert mw.sem_viewer._drag_x == 0.0

    def test_coarse_gds_uses_only_origin(self, mw):
        mw._chip_corner_x = mw._chip_corner_y = 0.0
        mw._origin_dx, mw._origin_dy = 10.0, 20.0
        img = sem_loader.SemImage(
            image_id="D1", filename="a.png",
            file_path=Path("a.png"), xrel=4000.0, yrel=5000.0)
        cx, cy = mw._coarse_gds(img)
        assert cx == pytest.approx(4010.0)
        assert cy == pytest.approx(5020.0)

    def test_dev_mode_off_hides_edit_catalog(self, mw):
        mw._set_dev_mode(False)
        assert mw.sem_panel.coord_setup._edit_btn.isVisible() is False

    def test_dev_mode_on_shows_edit_catalog(self, mw):
        mw._set_dev_mode(True)
        # Visibility flag flips even if the widget isn't actually painted
        # offscreen.
        assert mw.sem_panel.coord_setup._edit_btn.isVisibleTo(
            mw.sem_panel.coord_setup) is True
        mw._set_dev_mode(False)


# ── CatalogEditorDialog ──────────────────────────────────────────────────────

class TestCatalogEditor:
    def test_save_writes_atomic(self, mw, tmp_path, monkeypatch):
        # Use a fresh per-test path so the dialog write doesn't clobber the
        # session catalog seeded by the mw fixture.
        target = tmp_path / "edited.json"
        monkeypatch.setattr(parts_catalog, "default_catalog_path",
                            lambda: target)
        monkeypatch.setattr(gat, "default_catalog_path", lambda: target)
        dlg = gat.CatalogEditorDialog(mw, {
            "NEW_PART": PartSpec(description="x",
                                 chips={"C9": ChipSpec(chip_x_um=42.0)}),
        })
        dlg._on_save()
        loaded = parts_catalog.load_catalog(target)
        assert "NEW_PART" in loaded
        assert loaded["NEW_PART"].chips["C9"].chip_x_um == 42.0

    def test_add_part_rejects_duplicates(self, mw, monkeypatch):
        # Stub QInputDialog so we can drive Add PART without a modal.
        from PyQt6.QtWidgets import QInputDialog, QMessageBox
        monkeypatch.setattr(QInputDialog, "getText",
                            staticmethod(lambda *a, **k: ("DUP", True)))
        monkeypatch.setattr(QMessageBox, "warning",
                            staticmethod(lambda *a, **k: None))
        dlg = gat.CatalogEditorDialog(mw, {"DUP": PartSpec()})
        dlg._on_add_part()
        # Still exactly one DUP, no silent overwrite.
        assert list(dlg._catalog) == ["DUP"]


class TestActionGating:
    """T1: toolbar + right-column actions enable / disable based on what's
    currently loaded."""

    def test_initial_state_most_buttons_disabled(self, mw):
        # No OASIS, no SEM: only "Open OASIS…" (and SEM seg / Load SEM…)
        # should be live.
        assert mw._seg_gds.isEnabled() is False
        assert mw._fit_btn.isEnabled() is False
        assert mw._goto_btn.isEnabled() is False
        assert mw._goto_edit.isEnabled() is False
        assert mw._align_btn.isEnabled() is False
        assert mw.sem_panel.load_roi_btn.isEnabled() is False

    def test_export_alignment_needs_sem(self, mw):
        from PyQt6.QtCore import QSettings  # noqa: F401
        # Pretend SEM images arrived: align button activates, GDS still
        # disabled (no doc), Load GDS ROI still disabled (no reader).
        from sem_loader import SemImage
        mw._sem_images = [SemImage(
            image_id="D1", filename="a.png",
            file_path=Path("a.png"), xrel=1.0, yrel=2.0)]
        mw._refresh_action_states()
        assert mw._align_btn.isEnabled() is True
        assert mw._seg_gds.isEnabled() is False
        assert mw.sem_panel.load_roi_btn.isEnabled() is False

    def test_goto_fit_need_doc(self, mw):
        # Stub a doc → goto / fit / GDS view become live.
        mw._doc = gat.GdsDocument()
        mw._refresh_action_states()
        assert mw._seg_gds.isEnabled() is True
        assert mw._fit_btn.isEnabled() is True
        assert mw._goto_btn.isEnabled() is True


class TestStatusTransient:
    """T2: status-bar messages classified as transient revert after a delay."""

    def test_transient_then_revert(self, mw):
        mw._status_state("loaded thing")
        mw._status_transient("did the action", ms=50)
        assert "did the action" in mw._status_doc.text()
        mw._status_revert_now()
        assert "loaded thing" in mw._status_doc.text()

    def test_state_clears_pending_revert(self, mw):
        # A new state assignment must cancel the previous transient timer
        # so the state message isn't replaced by a stale revert.
        mw._status_state("initial state")
        mw._status_transient("transient", ms=10_000)
        mw._status_state("new state")
        assert mw._status_revert_timer is not None
        assert mw._status_revert_timer.isActive() is False
        assert mw._status_doc.text() == "new state"


class TestLoadRoiButtonLabel:
    """T7: the right column's Load GDS ROI button changes label after the
    current image's ROI has been loaded, so it no longer reads "I haven't
    loaded anything"."""

    def test_label_before_load(self, mw):
        assert "Load GDS ROI here" in mw.sem_panel.load_roi_btn.text()

    def test_label_flips_to_reload_when_doc_has_entries(self, mw):
        # Simulate an active session: reader + current image + non-empty doc.
        from sem_loader import SemImage
        from gds_align_tool import LayerEntry, LayerKey
        import numpy as np
        mw._rar = object()        # any non-None sentinel
        mw._sem_images = [SemImage(
            image_id="D1", filename="a.png",
            file_path=Path("a.png"), xrel=1.0, yrel=2.0)]
        mw._current_sem = mw._sem_images[0]
        doc = gat.GdsDocument()
        doc.entries = [LayerEntry(key=LayerKey(17, 0),
                                  polygons=[np.zeros((4, 2))])]
        mw._doc = doc
        mw._refresh_action_states()
        assert "Reload" in mw.sem_panel.load_roi_btn.text()
