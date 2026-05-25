"""Offscreen regression tests for the M6 single-view UX in
``tools/gds_align_tool.py``:

* ``LayerEntry.opacity`` → fill alpha mapping.
* ``_LayerRow`` widget drives the bound entry (visibility / opacity) and
  emits ``changed``.
* ``LayerPanel.set_document`` builds a row widget per layer.
* MainWindow: GDS overview hidden by default + toolbar toggle; the SEM
  overlay carries per-layer alpha and refreshes on ``layers_changed``.

Skips cleanly when PyQt6 / its Qt platform libs are unavailable.
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
    from PyQt6.QtGui import QColor
    _APP = QApplication.instance() or QApplication([])
    import gds_align_tool as gat  # noqa: E402
except Exception as exc:  # pragma: no cover - environment-dependent
    pytest.skip(f"Qt runtime unavailable: {exc}", allow_module_level=True)


def _entry(opacity=35, visible=True):
    poly = np.array([[0, 0], [10, 0], [10, 10], [0, 10]], dtype=float)
    return gat.LayerEntry(key=gat.LayerKey(17, 101), polygons=[poly],
                          opacity=opacity, visible=visible)


# ── LayerEntry opacity → alpha ───────────────────────────────────────


class TestFillAlpha:

    @pytest.mark.parametrize("opacity,alpha", [(0, 0), (100, 255),
                                               (35, 89), (60, 153)])
    def test_fill_alpha(self, opacity, alpha):
        assert _entry(opacity=opacity).fill_alpha() == alpha

    def test_clamps_out_of_range(self):
        e = _entry()
        e.opacity = 250
        assert e.fill_alpha() == 255
        e.opacity = -10
        assert e.fill_alpha() == 0


# ── _LayerRow widget ─────────────────────────────────────────────────


class TestLayerRow:

    def test_checkbox_toggles_visibility_and_emits(self):
        e = _entry(visible=True)
        row = gat._LayerRow(e)
        fired = []
        row.changed.connect(lambda: fired.append(1))
        row._chk.setChecked(False)
        assert e.visible is False
        assert fired

    def test_color_swatch_updates_entry(self, monkeypatch):
        e = _entry()
        row = gat._LayerRow(e)
        monkeypatch.setattr(gat.QColorDialog, "getColor",
                            lambda *a, **k: QColor("#123456"))
        fired = []
        row.changed.connect(lambda: fired.append(1))
        row._on_color()
        assert e.color.name() == "#123456"
        assert fired

    def test_color_cancel_keeps_entry(self, monkeypatch):
        e = _entry()
        before = e.color.name()
        row = gat._LayerRow(e)
        monkeypatch.setattr(gat.QColorDialog, "getColor",
                            lambda *a, **k: QColor())   # invalid = cancelled
        row._on_color()
        assert e.color.name() == before


# ── LayerPanel rows ──────────────────────────────────────────────────


class TestLayerPanel:

    def test_set_document_builds_row_widgets(self):
        panel = gat.LayerPanel()
        doc = gat.GdsDocument()
        doc.entries = [_entry(), _entry(opacity=70)]
        panel.set_document(doc)
        assert panel.list.count() == 2
        w = panel.list.itemWidget(panel.list.item(0))
        assert isinstance(w, gat._LayerRow)

    def test_set_document_none_clears(self):
        panel = gat.LayerPanel()
        panel.set_document(gat.GdsDocument())
        panel.set_document(None)
        # Empty doc shows a non-selectable onboarding hint (icon + title +
        # sub-hint) instead of a blank list.
        assert panel.list.count() == 3
        from PyQt6.QtCore import Qt
        for i in range(panel.list.count()):
            assert panel.list.item(i).flags() == Qt.ItemFlag.NoItemFlags
        assert "Open an OASIS" in panel.list.item(1).text()


# ── MainWindow single-view UX ────────────────────────────────────────


@pytest.fixture
def mw():
    w = gat.MainWindow()
    yield w
    w.close()


class TestSingleViewUX:

    def test_overview_hidden_by_default(self, mw):
        assert mw.canvas.isHidden() is True

    def test_toggle_overview_shows_and_hides(self, mw):
        mw._on_toggle_overview(True)
        assert mw.canvas.isHidden() is False
        mw._on_toggle_overview(False)
        assert mw.canvas.isHidden() is True

    def test_overlay_carries_per_layer_alpha(self, mw):
        e = _entry(opacity=60)
        doc = gat.GdsDocument()
        doc.entries = [e]
        mw._doc = doc
        mw._current_image_gds = lambda: (1000.0, 2000.0)
        mw._effective_nm_per_px = lambda: 2.0
        mw._update_overlay()
        ents = mw.sem_viewer._entries
        assert len(ents) == 1
        polys, color, alpha = ents[0]
        assert alpha == 153          # 60 % of 255

    def test_layers_changed_refreshes_overlay(self, mw):
        e = _entry(opacity=20)
        doc = gat.GdsDocument()
        doc.entries = [e]
        mw._doc = doc
        mw._current_image_gds = lambda: (0.0, 0.0)
        mw._effective_nm_per_px = lambda: 1.0
        # Bump opacity then fire the panel signal the row would emit.
        e.opacity = 90
        mw._on_layers_changed()
        assert mw.sem_viewer._entries[0][2] == round(90 / 100 * 255)

    def test_hidden_layer_excluded_from_overlay(self, mw):
        e = _entry(visible=False)
        doc = gat.GdsDocument()
        doc.entries = [e]
        mw._doc = doc
        mw._current_image_gds = lambda: (0.0, 0.0)
        mw._effective_nm_per_px = lambda: 1.0
        mw._update_overlay()
        assert mw.sem_viewer._entries == []
