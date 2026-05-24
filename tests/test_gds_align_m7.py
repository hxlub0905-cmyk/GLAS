"""Offscreen regression tests for M7 UI/UX polish in
``tools/gds_align_tool.py``.

M7.1 — collapsible right panel: Coordinate Setup + Fine Align are wrapped in
CollapsibleSection but the ``.coord_setup`` / ``.fine_align`` references (used
by tests + signal wiring) still resolve; Coordinate Setup auto-collapses once
after a valid jump / cache load; Fine Align expands when a POI is picked.

Skips when PyQt6 / Qt platform libs are unavailable.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 not available")
np = pytest.importorskip("numpy")

try:
    from PyQt6.QtWidgets import QApplication
    import sem_loader
    _APP = QApplication.instance() or QApplication([])
    import gds_align_tool as gat  # noqa: E402
except Exception as exc:  # pragma: no cover
    pytest.skip(f"Qt runtime unavailable: {exc}", allow_module_level=True)


@pytest.fixture
def mw():
    w = gat.MainWindow()
    yield w
    w.close()


def _poi_doc():
    doc = gat.GdsDocument()
    doc.entries = [gat.LayerEntry(key=gat.LayerKey(17, 0),
                                  polygons=[np.zeros((4, 2))])]
    return doc


class TestCollapsibleRefs:

    def test_panel_refs_survive_wrapping(self, mw):
        # The inner panels must stay reachable (tests + wiring rely on them).
        assert mw.sem_panel.coord_setup is not None
        assert mw.sem_panel.fine_align is not None
        assert hasattr(mw.sem_panel.coord_setup, "values")
        assert hasattr(mw.sem_panel.fine_align, "set_poi")

    @pytest.mark.skipif(gat.CollapsibleSection is None,
                        reason="CollapsibleSection unavailable")
    def test_initial_collapse_state(self, mw):
        # Setup starts open (must be filled); Fine Align starts collapsed.
        assert mw.sem_panel._coord_section.is_collapsed() is False
        assert mw.sem_panel._fine_section.is_collapsed() is True


@pytest.mark.skipif(gat.CollapsibleSection is None,
                    reason="CollapsibleSection unavailable")
class TestAutoCollapse:

    def test_poi_select_expands_fine(self, mw):
        mw.sem_panel.set_fine_collapsed(True)
        mw.layer_panel.set_document(_poi_doc())
        mw.layer_panel._rows[0].poi_btn.setChecked(True)
        assert mw.sem_panel._fine_section.is_collapsed() is False

    def test_jump_collapses_coord_once(self, mw):
        mw._fov_w = mw._fov_h = 2000.0
        img = sem_loader.SemImage(image_id="D1", filename="a.png",
                                  file_path=Path("a.png"), xrel=1.0, yrel=2.0)
        mw._jump_to_image(img)
        assert mw.sem_panel._coord_section.is_collapsed() is True
        assert mw._coord_collapsed_once is True
        # Re-expanding should stick — a second jump must NOT re-collapse it.
        mw.sem_panel.set_coord_collapsed(False)
        mw._jump_to_image(img)
        assert mw.sem_panel._coord_section.is_collapsed() is False

    def test_no_collapse_without_valid_fov(self, mw):
        mw._fov_w = mw._fov_h = 0.0
        img = sem_loader.SemImage(image_id="D1", filename="a.png",
                                  file_path=Path("a.png"), xrel=1.0, yrel=2.0)
        mw._jump_to_image(img)
        assert getattr(mw, "_coord_collapsed_once", False) is False


# ── M7.2 token centralization ────────────────────────────────────────


class TestTokens:

    def test_helpers_emit_qss(self):
        assert "font-size:11px" in gat._hint_qss(11)
        assert "padding:2px 0" in gat._hint_qss(12, pad="2px 0")
        assert gat._TK_SUCCESS in gat._result_qss(gat._TK_SUCCESS)

    def test_semantic_tokens_exist(self):
        for tok in (gat._TK_SUCCESS, gat._TK_DANGER, gat._TK_GUIDANCE_BG,
                    gat._TK_SECTION_HEAD, gat._TK_TOOLBAR_BG):
            assert isinstance(tok, str) and tok.startswith("#")

    def test_fine_result_uses_semantic_colour(self, mw):
        fa = mw.sem_panel.fine_align
        fa._thresh.setValue(0.5)
        fa.set_result(0.9, -10.0, 5.0)          # good -> success green
        assert gat._TK_SUCCESS in fa._result_lbl.styleSheet()
        fa.set_result(0.1, -10.0, 5.0)          # bad -> danger red
        assert gat._TK_DANGER in fa._result_lbl.styleSheet()


# ── M7.3 toolbar icons ───────────────────────────────────────────────


class TestToolbarIcons:

    @pytest.mark.parametrize("name", [
        "folder-open", "folder", "save", "download", "maximize",
        "layers", "target"])
    def test_new_icons_resolve(self, name):
        # Skips silently if the icon helper fell back (src/gui unavailable).
        icon = gat._qicon(name)
        if gat.CollapsibleSection is None:
            pytest.skip("src/gui icons unavailable")
        assert not icon.isNull(), f"missing icon: {name}"

    def test_toolbar_background_is_scoped(self, mw):
        # The toolbar QFrame must scope its background via an objectName
        # selector so it doesn't bleed onto child buttons (CLAUDE.md §6).
        from PyQt6.QtWidgets import QFrame
        bar = next((f for f in mw.findChildren(QFrame)
                    if f.objectName() == "gdsToolbar"), None)
        assert bar is not None
        assert "QFrame#gdsToolbar" in bar.styleSheet()

    def test_goto_button_has_label(self, mw):
        from PyQt6.QtWidgets import QFrame, QPushButton
        bar = next((f for f in mw.findChildren(QFrame)
                    if f.objectName() == "gdsToolbar"), None)
        texts = [b.text().strip() for b in bar.findChildren(QPushButton)]
        assert "Goto" in texts                      # not a bare icon button

    def test_toolbar_buttons_bold(self, mw):
        from PyQt6.QtWidgets import QFrame, QPushButton
        bar = next((f for f in mw.findChildren(QFrame)
                    if f.objectName() == "gdsToolbar"), None)
        assert all(b.font().bold() for b in bar.findChildren(QPushButton))


class TestRefinements:
    """M7 visual refinements (single-column setup, empty hints, weights)."""

    def test_layers_empty_hint(self, mw):
        # No document -> a non-selectable LAYERS placeholder.
        lst = mw.layer_panel.list
        assert lst.count() == 1
        assert "Open an OASIS" in lst.item(0).text()
        from PyQt6.QtCore import Qt
        assert lst.item(0).flags() == Qt.ItemFlag.NoItemFlags

    def test_load_roi_not_primary(self, mw):
        # Only the toolbar "Open OASIS…" should carry the primary emphasis.
        assert mw.sem_panel.load_roi_btn.objectName() != "primaryBtn"

    def test_toolbar_group_labels(self, mw):
        from PyQt6.QtWidgets import QFrame, QLabel
        bar = next((f for f in mw.findChildren(QFrame)
                    if f.objectName() == "gdsToolbar"), None)
        texts = {l.text() for l in bar.findChildren(QLabel)}
        assert {"FILE", "VIEW MODE", "EXPORT"} <= texts

    def test_coord_setup_single_column(self, mw):
        # Single-column form: the grid has effectively one content column
        # (everything added at column 0).
        from PyQt6.QtWidgets import QGridLayout
        lay = mw.sem_panel.coord_setup.layout()
        assert isinstance(lay, QGridLayout)
        assert lay.columnCount() == 1


class TestRound2:
    """M7 round-2 polish: no horizontal scrollbar, single-column Fine Align,
    no dev-code in titles, scroll area, demoted run button."""

    def test_lists_wrap_no_hscroll(self, mw):
        from PyQt6.QtCore import Qt
        for lst in (mw.layer_panel.list, mw.sem_panel.list):
            assert lst.wordWrap() is True
            assert (lst.horizontalScrollBarPolicy()
                    == Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def test_fine_align_single_column(self, mw):
        from PyQt6.QtWidgets import QGridLayout
        lay = mw.sem_panel.fine_align.layout()
        assert isinstance(lay, QGridLayout)
        assert lay.columnCount() == 1

    def test_no_devcode_in_fine_title(self, mw):
        if gat.CollapsibleSection is not None:
            # Wrapped: section header carries the title; inner box title cleared.
            txt = mw.sem_panel._fine_section._btn.text()
            assert "Fine Align" in txt and "M4b" not in txt
        else:
            assert mw.sem_panel.fine_align.title() == "Fine Align"

    def test_run_btn_not_primary(self, mw):
        assert mw.sem_panel.fine_align._run_btn.objectName() != "primaryBtn"

    def test_right_panel_has_scrollarea(self, mw):
        from PyQt6.QtWidgets import QScrollArea
        assert mw.sem_panel.findChildren(QScrollArea)

    def test_right_panel_width(self, mw):
        assert mw.sem_panel.width() == 300


class TestOverviewMap:
    """GDS overview enhancements: defect map (#1), click-to-select (#2), HUD (#3)."""

    def _imgs(self):
        return [
            sem_loader.SemImage(image_id="D1", filename="a.png",
                                file_path=Path("a.png"), xrel=4000.0, yrel=5000.0),
            sem_loader.SemImage(image_id="D2", filename="b.png",
                                file_path=Path("b.png"), xrel=9000.0, yrel=2000.0),
            sem_loader.SemImage(image_id="D3", filename="c.png",
                                file_path=Path("c.png")),               # no coords
        ]

    def test_score_color_bands(self):
        assert gat.GdsCanvas._score_color(None).name() == "#b8a890"
        assert gat.GdsCanvas._score_color(0.9).name() == gat._TK_SUCCESS
        assert gat.GdsCanvas._score_color(0.6).name() == "#b8860b"
        assert gat.GdsCanvas._score_color(0.2).name() == gat._TK_DANGER

    def test_refresh_builds_defects_skips_no_coords(self, mw):
        mw._sem_images = self._imgs()
        mw._chip_corner_x = mw._chip_corner_y = 0.0
        mw._fov_w = mw._fov_h = 2000.0
        mw._refined = {"D1": (0.0, 0.0, 0.93)}
        mw._refresh_overview_defects()
        d = mw.canvas._defects
        assert len(d) == 2                       # D3 (no coords) excluded
        ids = {row[0] for row in d}
        assert ids == {"D1", "D2"}
        d1 = next(r for r in d if r[0] == "D1")
        assert d1[3] == 0.93                      # score carried

    def test_hit_test_and_click_selects(self, mw):
        mw._sem_images = self._imgs()
        mw._chip_corner_x = mw._chip_corner_y = 0.0
        mw._fov_w = mw._fov_h = 2000.0
        mw._refresh_overview_defects()
        mw.canvas.resize(420, 540)
        mw.canvas.set_view_to_bbox(0, 0, 12000, 8000)
        # D1's dot is hittable; empty space is not.
        d1 = next(r for r in mw.canvas._defects if r[0] == "D1")
        sx, sy = mw.canvas._world_to_screen(d1[1], d1[2])
        assert mw.canvas._hit_test_defect(sx, sy) == "D1"
        assert mw.canvas._hit_test_defect(3, 3) is None
        # Clicking a dot selects that image (same flow as the list).
        mw._on_defect_clicked("D2")
        assert mw._current_sem.image_id == "D2"
        assert mw.canvas._current_defect == "D2"

    def test_hud_zoom_factor_reference(self, mw):
        mw.canvas.set_view_to_bbox(0, 0, 1000, 1000)
        assert mw.canvas._fit_zoom == mw.canvas._zoom   # fit == 1.0×

    def test_fit_to_defects_frames_all(self, mw):
        c = mw.canvas
        c.resize(400, 400)
        mw._sem_images = self._imgs()[:2]            # D1, D2 (both have coords)
        mw._chip_corner_x = mw._chip_corner_y = 0.0
        mw._refresh_overview_defects()
        c.set_view_to_bbox(0, 0, 200, 200)           # start zoomed elsewhere
        c.fit_to_defects()
        for _id, gx, gy, _s in c._defects:
            sx, sy = c._world_to_screen(gx, gy)
            assert -1 <= sx <= c.width() + 1 and -1 <= sy <= c.height() + 1

    def test_empty_state_renders_and_suppressed_with_defects(self, mw):
        c = mw.canvas
        c.resize(400, 400)
        assert not c.grab().isNull()                 # no doc, no defects: icon+hint
        mw._sem_images = self._imgs()[:1]
        mw._chip_corner_x = mw._chip_corner_y = 0.0
        mw._refresh_overview_defects()               # defects, still no doc
        assert c._doc is None and c._defects
        assert not c.grab().isNull()                 # dots shown, no crash

    def test_offscreen_arrow_renders(self, mw):
        c = mw.canvas
        c.resize(400, 400)
        c.set_view_to_bbox(0, 0, 10000, 10000)
        c.set_fov_marker(2000, 2000, 500, 500)
        c._pan_nm = (500000, 500000)                 # pan marker off-screen
        ccx, ccy = c._world_to_screen(2000, 2000)
        assert not (0 <= ccx <= c.width() and 0 <= ccy <= c.height())
        assert not c.grab().isNull()                 # arrow paint path runs


class TestViewModes:
    """View-mode selector (SEM / GDS / Minimap) + corner minimap (#9)."""

    def test_default_mode_sem(self, mw):
        assert mw._view_mode == "sem"
        assert mw.canvas.isHidden() and mw.minimap.isHidden()
        assert mw._seg_sem.isChecked()

    def test_modes_are_exclusive(self, mw):
        mw._set_view_mode("gds")
        assert (not mw.canvas.isHidden()) and mw.minimap.isHidden()
        assert mw._seg_gds.isChecked() and not mw._seg_sem.isChecked()
        mw._set_view_mode("minimap")
        assert mw.canvas.isHidden() and (not mw.minimap.isHidden())
        assert mw._seg_mini.isChecked() and not mw._seg_gds.isChecked()
        mw._set_view_mode("sem")
        assert mw.canvas.isHidden() and mw.minimap.isHidden()

    def test_cycle_wraps(self, mw):
        mw._set_view_mode("sem")
        mw._cycle_view_mode(); assert mw._view_mode == "gds"
        mw._cycle_view_mode(); assert mw._view_mode == "minimap"
        mw._cycle_view_mode(); assert mw._view_mode == "sem"

    def test_minimap_receives_defects_and_click(self, mw):
        mw._sem_images = [
            sem_loader.SemImage(image_id="D1", filename="a", file_path=Path("a"),
                                xrel=4000.0, yrel=5000.0),
            sem_loader.SemImage(image_id="D2", filename="b", file_path=Path("b"),
                                xrel=9000.0, yrel=2000.0),
        ]
        mw._chip_corner_x = mw._chip_corner_y = 0.0
        mw._refresh_overview_defects()
        assert len(mw.minimap._defects) == 2
        mw.minimap.resize(212, 152)
        bbox = mw.minimap._bbox()
        sx, sy = mw.minimap._map(9000.0, 2000.0, bbox)   # D2 dot
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtCore import QPointF, QEvent, Qt as _Qt
        ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(sx, sy),
                         _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
                         _Qt.KeyboardModifier.NoModifier)
        mw.minimap.mousePressEvent(ev)
        assert mw._current_sem.image_id == "D2"

    def test_fit_action_not_a_mode(self, mw):
        # _on_fit_view fits the overview to defects (an action, not a mode);
        # it must not change the current view mode.
        mw._set_view_mode("gds")
        mw._on_fit_view()
        assert mw._view_mode == "gds"


class TestSplitNoGap:

    def test_sem_panel_flush_after_fit(self, mw):
        mw.resize(1600, 900)
        mw.show()
        for _ in range(3):
            _APP.processEvents()
        mw._fit_split_sizes()
        _APP.processEvents()
        sp = mw.centralWidget()
        # The SEM panel's right edge reaches the splitter's right edge — no
        # leftover gap dumped into the fixed pane.
        assert mw.sem_panel.geometry().right() >= sp.width() - 2


class TestEmptyListPlaceholder:

    def test_placeholder_when_no_images(self, mw):
        lst = mw.sem_panel.list
        assert lst.count() == 1
        assert "Load SEM" in lst.item(0).text()
        # The placeholder isn't selectable / clickable.
        from PyQt6.QtCore import Qt
        assert lst.item(0).flags() == Qt.ItemFlag.NoItemFlags

    def test_images_replace_placeholder(self, mw):
        img = sem_loader.SemImage(image_id="D1", filename="a.png",
                                  file_path=Path("a.png"), xrel=1.0, yrel=2.0)
        mw.sem_panel.set_images([img])
        assert mw.sem_panel.list.count() == 1
        assert mw.sem_panel.list.item(0).text().startswith("D1")
        mw.sem_panel.set_images([])                 # back to placeholder
        assert "Load SEM" in mw.sem_panel.list.item(0).text()
