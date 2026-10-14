"""F32: navigate + display without an image file, without a KLARF, or with
neither (keyed coordinates) -- offscreen Qt tests.

Three dependencies used to be hard:

* ``SemViewer`` drew nothing without a pixmap, so an image-less defect (or a
  keyed coordinate) had no layout, no drag, no zoom, no HUD.
* every navigation path read ``img.xrel / yrel`` as set by the KLARF loader,
  so a folder image could never jump.
* the toolbar Goto drove only the (hidden by default) GDS canvas.

The tests below pin the replacements: the viewer's blank canvas behaves like an
image of the same size (incl. the fold invariant Set Offset relies on), keyed
coordinates land on the image as die-corner nm and go through the ordinary
path, and a keyed Goto becomes a virtual current image that is *not* in the
dataset list.

Skips when PyQt6 / its platform libs are unavailable (project pattern).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 not available")

try:  # Qt platform libs (libEGL etc.) may still be missing on a bare box.
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QPixmap, QColor
    _APP = QApplication.instance() or QApplication([])
    import gds_align_tool as gat  # noqa: E402
    import sem_loader  # noqa: E402
    import gds_fov  # noqa: E402
except Exception as exc:  # pragma: no cover - environment-dependent
    pytest.skip(f"Qt runtime unavailable: {exc}", allow_module_level=True)


def _img(image_id="1", xrel=None, yrel=None, path=None, coord_source=""):
    return sem_loader.SemImage(image_id=image_id,
                               filename=path.name if path else "",
                               file_path=path, xrel=xrel, yrel=yrel,
                               coord_source=coord_source)


# ── SemViewer blank canvas ───────────────────────────────────────────────────


class TestSemViewerBlank:

    def test_empty_viewer_has_no_geometry(self):
        v = gat.SemViewer()
        v.resize(400, 400)
        assert v.native_size() is None
        assert v._compute_geometry() is None
        assert not v.is_blank()

    def test_blank_gives_geometry_and_native_size(self):
        v = gat.SemViewer()
        v.resize(400, 400)
        v.set_blank(1024, 512)
        assert v.is_blank()
        assert v.native_size() == (1024, 512)
        geom = v._compute_geometry()
        assert geom is not None
        ox, oy, dw, dh, s = geom
        # Fit-to-window of a 2:1 canvas inside a square widget.
        assert dw == pytest.approx(400) and dh == pytest.approx(200)
        assert s == pytest.approx(400 / 1024)

    def test_blank_paints_and_maps_overlay_coordinates(self):
        """After a paint the screen<->GDS mapping works exactly as over an
        image: the anchor sits at the canvas centre."""
        v = gat.SemViewer()
        v.resize(400, 400)
        v.set_blank(200, 200)
        v.set_overlay([], (5000.0, 7000.0), 10.0)
        v.grab()                         # runs paintEvent offscreen
        assert v._img_rect is not None and v._scale > 0
        w = v._view_to_world(200.0, 200.0)
        assert w == pytest.approx((5000.0, 7000.0))
        # +1 native px right = +nm_per_px in GDS x; up on screen = +y.
        sx, sy = v._world_to_view(5010.0, 7010.0)
        assert sx == pytest.approx(200.0 + v._scale)
        assert sy == pytest.approx(200.0 - v._scale)

    def test_blank_hides_cta_and_set_image_restores_empty_state(self):
        v = gat.SemViewer()
        v.resize(400, 400)
        v.set_blank(100, 100)
        assert not v._cta_btn.isVisible() or v._cta_btn.isHidden()
        v.set_image(None)
        assert not v.is_blank() and v.native_size() is None
        assert v._compute_geometry() is None

    def test_real_image_wins_over_blank(self):
        v = gat.SemViewer()
        v.resize(400, 400)
        v.set_blank(1000, 1000)
        pm = QPixmap(64, 32)
        pm.fill(QColor("#303030"))
        v._pixmap = pm
        v._blank_size = None
        assert v.native_size() == (64, 32)
        assert not v.is_blank()

    def test_zoom_works_on_blank(self):
        v = gat.SemViewer()
        v.resize(400, 400)
        v.set_blank(200, 200)
        v.zoom_by(1.2)
        assert v._view_zoom == pytest.approx(1.2)
        ox, oy, dw, dh, s = v._compute_geometry()
        assert dw == pytest.approx(480)

    @pytest.mark.parametrize("drag", [(0.0, 0.0), (37.0, -12.5), (-300.0, 90.0)])
    def test_fold_invariant_holds_on_blank(self, drag):
        """render(anchor, drag) == render(anchor - drag, 0) -- what makes Set
        Offset leave the geometry visually put, now on a blank canvas too."""
        v = gat.SemViewer()
        v.resize(400, 400)
        v.set_blank(300, 300)
        v.set_overlay([], (1000.0, 2000.0), 2.0)
        v.grab()
        v._drag_x, v._drag_y = drag
        a = v._world_to_view(1234.0, 1999.0)
        v._anchor = (1000.0 - drag[0], 2000.0 - drag[1])
        v._drag_x = v._drag_y = 0.0
        b = v._world_to_view(1234.0, 1999.0)
        assert a == pytest.approx(b)

    def test_blank_caption_defaults_to_hint(self):
        v = gat.SemViewer()
        v.set_blank(10, 10)
        assert v._caption == gat.SemViewer.BLANK_HINT
        v.set_blank(10, 10, "defect 7 (no image)")
        assert v._caption == "defect 7 (no image)"

    def test_missing_file_caption_mentions_defect_when_no_filename(self):
        v = gat.SemViewer()
        v.set_image(_img("42", xrel=1.0, yrel=2.0))
        assert "42" in v._caption and "no image" in v._caption
        assert not v.is_blank()           # the caller decides on blank


# ── MainWindow: image-less defects, keyed coordinates, virtual goto ──────────


@pytest.fixture
def mw():
    w = gat.MainWindow()
    w._fov_w = 1500.0
    w._fov_h = 1200.0
    w._chip_corner_x = 100.0
    w._chip_corner_y = 50.0
    yield w
    w.close()


class TestImagelessDefect:

    def test_selecting_a_coord_only_image_shows_a_blank_canvas(self, mw):
        img = _img("7", xrel=10_100.0, yrel=20_050.0, coord_source="klarf")
        mw._sem_images = [img]
        mw.sem_panel.set_images([img])
        mw._on_sem_image_selected(img)
        assert mw.sem_viewer.is_blank()
        assert mw._current_image_gds() == pytest.approx((10_000.0, 20_000.0))
        # Virtual size follows the FOV aspect; auto nm/px is FOV / width.
        w, h = mw.sem_viewer.native_size()
        assert w == gat.MainWindow._VIRTUAL_CANVAS_W
        assert h == pytest.approx(w * 1200.0 / 1500.0, abs=1)
        assert mw._effective_nm_per_px() == pytest.approx(1500.0 / w)

    def test_manual_nm_per_px_sets_the_virtual_size(self, mw):
        mw._nm_auto = False
        mw._nm_per_px_manual = 3.0
        img = _img("7", xrel=1.0, yrel=2.0)
        mw._sem_images = [img]
        mw._on_sem_image_selected(img)
        assert mw.sem_viewer.native_size() == (500, 400)
        assert mw._effective_nm_per_px() == 3.0

    def test_image_without_coords_stays_in_empty_state(self, mw):
        img = _img("x")
        mw._sem_images = [img]
        mw._on_sem_image_selected(img)
        assert not mw.sem_viewer.is_blank()
        assert mw._current_image_gds() is None

    def test_list_badges_no_image_and_keyed(self, mw):
        imgs = [_img("a"),                                         # no coords
                _img("b", xrel=1.0, yrel=2.0, coord_source="klarf"),  # no file
                _img("c", xrel=1.0, yrel=2.0, coord_source="manual-gds")]
        mw.sem_panel.set_images(imgs)
        from PyQt6.QtCore import Qt
        badges = [mw.sem_panel.list.item(i).data(Qt.ItemDataRole.UserRole + 2)
                  for i in range(3)]
        assert badges == ["no coords", "no image", "keyed"]
        assert "(no image)" in mw.sem_panel.list.item(1).text()

    def test_roi_button_enabled_for_coord_only_images(self, mw):
        img = _img("7", xrel=1.0, yrel=2.0)
        mw._sem_images = [img]
        mw._rar = object()
        mw._refresh_action_states()
        assert mw.sem_panel.load_roi_btn.isEnabled()


class TestKeyedCoordinate:

    def test_apply_gds_um_lands_as_die_corner_nm(self, mw):
        img = _img("f1")
        mw._sem_images = [img]
        mw.sem_panel.set_images([img])
        mw._current_sem = img
        mw._apply_keyed_coord(img, 12.345, -6.0, unit="gds-um")
        # Stored as KLARF die-corner nm (inverse of klarf_to_gds with the
        # current chip corner) ...
        assert (img.xrel, img.yrel) == pytest.approx((12_445.0, -5_950.0))
        assert img.coord_source == "manual-gds"
        # ... so the ordinary path lands exactly on the keyed GDS position.
        assert mw._current_image_gds() == pytest.approx((12_345.0, -6_000.0))
        from PyQt6.QtCore import Qt
        assert mw.sem_panel.list.item(0).data(Qt.ItemDataRole.UserRole + 2) == "keyed"

    def test_apply_klarf_nm_is_stored_verbatim(self, mw):
        img = _img("f1")
        mw._sem_images = [img]
        mw._current_sem = img
        mw._apply_keyed_coord(img, 20_267_174.0, 20_652_619.0, unit="klarf-nm")
        assert (img.xrel, img.yrel) == (20_267_174.0, 20_652_619.0)
        assert img.coord_source == "manual-klarf"
        gx, gy = gds_fov.klarf_to_gds(img.xrel, img.yrel, 100.0, 50.0)
        assert mw._current_image_gds() == pytest.approx((gx, gy))

    def test_keyed_image_joins_the_export_candidates(self, mw):
        import fine_align
        img = _img("f1")
        mw._sem_images = [img]
        assert fine_align.images_needing_fine_align([img], {}) == []
        mw._apply_keyed_coord(img, 1.0, 2.0, unit="gds-um")
        assert fine_align.images_needing_fine_align([img], {}) == [img]

    def test_origin_delta_applies_to_keyed_images_like_klarf_ones(self, mw):
        img = _img("f1")
        mw._sem_images = [img]
        mw._current_sem = img
        mw._apply_keyed_coord(img, 10.0, 10.0, unit="gds-um")
        mw._origin_dx, mw._origin_dy = 5.0, -5.0
        assert mw._current_image_gds() == pytest.approx((10_005.0, 9_995.0))

    def test_panel_reflects_the_selected_image_in_the_chosen_unit(self, mw):
        img = _img("k", xrel=10_100.0, yrel=20_050.0, coord_source="klarf")
        mw._sem_images = [img]
        mw._on_sem_image_selected(img)
        panel = mw.sem_panel.coord_key
        panel.set_unit("gds-um")
        assert panel.values() == pytest.approx((10.0, 20.0))
        panel.set_unit("klarf-nm")
        assert panel.values() == pytest.approx((10_100.0, 20_050.0))

    def test_apply_from_panel_signal(self, mw):
        img = _img("f1")
        mw._sem_images = [img]
        mw._on_sem_image_selected(img)
        panel = mw.sem_panel.coord_key
        panel.set_unit("gds-um")
        panel.set_values(3.0, 4.0)
        panel.apply_btn.click()
        assert img.has_coords
        assert mw._current_image_gds() == pytest.approx((3_000.0, 4_000.0))
        assert mw.sem_viewer.is_blank()   # no file behind this image


class TestVirtualGoto:

    def test_goto_creates_a_virtual_current_image_outside_the_list(self, mw):
        real = _img("r", xrel=1.0, yrel=2.0)
        mw._sem_images = [real]
        mw._goto_virtual(50_000.0, 60_000.0)
        cur = mw._current_sem
        assert cur is not None and cur is not real
        assert cur.coord_source == "manual-gds" and not cur.exists
        assert mw._is_virtual(cur)
        assert cur not in mw._sem_images
        assert mw._current_image_gds() == pytest.approx((50_000.0, 60_000.0))
        assert mw.sem_viewer.is_blank()

    def test_toolbar_goto_routes_through_the_virtual_target(self, mw):
        mw._goto_edit.setText("12.5, -3")
        mw._on_goto_gds()
        assert mw._is_virtual(mw._current_sem)
        assert mw._current_image_gds() == pytest.approx((12_500.0, -3_000.0))

    def test_guidance_does_not_demand_a_sem_for_a_virtual_target(self, mw):
        mw._sem_images = []
        mw._goto_virtual(1.0, 2.0)
        mw._rar = object()               # "an OASIS is open" for the guidance
        mw._update_guidance()
        assert "Load SEM" not in mw._guidance.text()
        assert "keyed" in mw._guidance.text()

    def test_panel_go_button_uses_the_fov_window(self, mw):
        mw._rar = None                   # no reader: just the navigation state
        panel = mw.sem_panel.coord_key
        panel.set_unit("klarf-nm")
        panel.set_values(10_100.0, 20_050.0)
        panel.go_btn.click()
        assert mw._is_virtual(mw._current_sem)
        assert mw._current_image_gds() == pytest.approx((10_000.0, 20_000.0))

    def test_selecting_a_real_image_replaces_the_virtual_target(self, mw):
        mw._goto_virtual(1.0, 2.0)
        real = _img("r", xrel=1.0, yrel=2.0)
        mw._sem_images = [real]
        mw._on_sem_image_selected(real)
        assert mw._current_sem is real


class TestFolderLoadClearsKlarfPath:

    def test_klarf_path_reset(self, mw, tmp_path, monkeypatch):
        mw._klarf_path = "/old/lot.klarf"
        monkeypatch.setattr(gat.QFileDialog, "getExistingDirectory",
                            staticmethod(lambda *a, **k: str(tmp_path)))
        mw._on_load_folder()
        assert mw._klarf_path == ""
