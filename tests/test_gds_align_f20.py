"""F20 tests: OpenOasisWizard three-page flow + MainWindow integration."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for sub in ("glas/core", "glas/app"):
    if str(_ROOT / sub) not in sys.path:
        sys.path.insert(0, str(_ROOT / sub))

try:
    from PyQt6.QtWidgets import QApplication, QWizard
except Exception:  # pragma: no cover
    pytest.skip("PyQt6 unavailable", allow_module_level=True)

import gds_align_tool as gat


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def wiz(qapp):
    w = gat.OpenOasisWizard()
    yield w
    w.close()


def _fake_oas(tmp_path: Path) -> Path:
    """Create a minimal .oas placeholder so File-pick page accepts it.
    The scan/index probe will simply yield empty, which is OK for these
    tests — we exercise the wizard machinery, not the parser."""
    p = tmp_path / "design.oas"
    p.write_bytes(b"\x00" * 32)
    return p


# ── _FilePickPage ────────────────────────────────────────────────────────────

class TestFilePickPage:
    def test_starts_incomplete(self, wiz):
        page = wiz._file_page
        assert page.isComplete() is False
        assert page.file_path() is None

    def test_complete_after_path_set(self, wiz, tmp_path):
        page = wiz._file_page
        path = _fake_oas(tmp_path)
        page._edit.setText(str(path))
        page._update_info(str(path))
        assert page.isComplete() is True
        assert page.file_path() == str(path)

    def test_info_shows_size_and_name(self, wiz, tmp_path):
        page = wiz._file_page
        path = _fake_oas(tmp_path)
        page._update_info(str(path))
        assert "design.oas" in page._info.text()
        assert "MB" in page._info.text()


# ── _LayerPickPage ───────────────────────────────────────────────────────────

class TestLayerPickPage:
    def test_starts_incomplete(self, wiz):
        page = wiz._layer_page
        assert page.isComplete() is False
        assert page.layer_keys() == []

    def test_parse_text_pairs(self, wiz):
        page = wiz._layer_page
        page._edit.setText("17/0, 6/101, 20")
        assert page.layer_keys() == [(17, 0), (6, 101), (20, 0)]
        assert page.isComplete() is True

    def test_parse_text_invalid_returns_empty(self, wiz):
        page = wiz._layer_page
        page._edit.setText("not-a-number")
        assert page.layer_keys() == []

    def test_scan_result_renders_list(self, wiz):
        page = wiz._layer_page
        # Stub the scan worker result path: feed the finished slot directly.
        page._on_scan_finished({
            "layers": [{"layer": 17, "datatype": 0, "name": "M1"},
                       {"layer": 30, "datatype": 0, "name": "POLY"}],
            "by_name": {"top": 100},
        })
        assert page._list.count() == 2
        # Selecting an item makes the page complete (manual entry not needed).
        page._list.item(0).setSelected(True)
        assert page.isComplete() is True
        assert (17, 0) in page.layer_keys()


# ── _RootCellPage ────────────────────────────────────────────────────────────

class TestRootCellPage:
    def test_populates_from_scan_result(self, wiz):
        wiz._layer_page._scan_result = {
            "by_name": {"alpha": 1, "top_cell": 2, "beta": 3},
        }
        page = wiz._root_page
        page.initializePage()
        assert page._combo.count() == 3
        # Recommended: name containing "top" → "top_cell".
        assert page._combo.currentText() == "top_cell"
        assert page.isComplete() is True

    def test_no_scan_result_no_cells(self, wiz, tmp_path):
        # No scan run + file lacks a real index → combo stays empty.
        wiz._layer_page._scan_result = {}
        wiz._file_page._file_path = str(_fake_oas(tmp_path))
        # Force initializePage to consult the (empty) scan_result and the
        # fake file's lack of a name table.
        page = wiz._root_page
        page.initializePage()
        assert page._combo.count() == 0
        assert page.isComplete() is False


# ── MainWindow integration ───────────────────────────────────────────────────

class TestMainWindowOpenROI:
    def test_cancelling_wizard_changes_nothing(self, qapp, monkeypatch):
        mw = gat.MainWindow()
        try:
            sentinel_rar = mw._rar
            sentinel_root = mw._roi_root
            # Patch wizard exec to return Rejected → no state changes.
            monkeypatch.setattr(
                gat.OpenOasisWizard, "exec",
                lambda self: int(gat.QDialog.DialogCode.Rejected))
            mw._on_open_roi()
            assert mw._rar is sentinel_rar
            assert mw._roi_root == sentinel_root
        finally:
            mw.close()

    def test_legacy_dialogs_removed(self):
        assert not hasattr(gat, "LayerFilterDialog")
        assert not hasattr(gat, "LayerPickDialog")


class TestWizardPolish:
    """T5–T8 wizard polish."""

    def test_browse_remembers_last_dir(self, wiz, tmp_path, monkeypatch):
        # T5: clicking Browse stores the directory in QSettings so the
        # next session opens the file picker in the same place.
        from PyQt6.QtCore import QSettings
        from PyQt6.QtWidgets import QFileDialog
        sample = tmp_path / "design.oas"
        sample.write_bytes(b"\x00" * 32)
        # Stub the modal QFileDialog so the test is non-interactive.
        monkeypatch.setattr(
            QFileDialog, "getOpenFileName",
            staticmethod(lambda *a, **k: (str(sample), "")))
        wiz._file_page._on_browse()
        stored = QSettings("GLAS", "GLAS").value(
            wiz._file_page._SETTINGS_LAST_DIR, "", type=str)
        assert stored == str(tmp_path)

    def test_subtitles_have_explicit_colour(self, wiz):
        # T6: subtitle text wraps in a coloured <span> so it doesn't read
        # as Qt's default washed-out grey.
        for page in (wiz._file_page, wiz._layer_page, wiz._root_page):
            sub = page.subTitle()
            assert "color:" in sub
            assert "span" in sub

    def test_next_button_has_accent_stylesheet(self, wiz):
        # T8: Next / Finish buttons carry an accent stylesheet so the
        # forward action visually dominates Cancel.
        from PyQt6.QtWidgets import QWizard
        for which in (QWizard.WizardButton.NextButton,
                      QWizard.WizardButton.FinishButton):
            btn = wiz.button(which)
            qss = btn.styleSheet()
            # Either the accent or accent-dark colour should appear.
            assert ("#f29f4b" in qss.lower() or "#c97028" in qss.lower())
