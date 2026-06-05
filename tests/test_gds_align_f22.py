"""F22 tests: WelcomeDialog (5-slide onboarding + persistent dismiss)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for sub in ("glas/core", "glas/app"):
    if str(_ROOT / sub) not in sys.path:
        sys.path.insert(0, str(_ROOT / sub))

try:
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover
    pytest.skip("PyQt6 unavailable", allow_module_level=True)

import gds_align_tool as gat


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dlg(qapp):
    d = gat.WelcomeDialog()
    yield d
    d.close()


class TestWelcomeDialog:
    def test_slides_loaded(self, dlg):
        assert dlg._stack.count() == 5

    def test_starts_on_first_slide(self, dlg):
        assert dlg._stack.currentIndex() == 0
        assert dlg._prev_btn.isEnabled() is False
        assert "Next" in dlg._next_btn.text()

    def test_next_advances(self, dlg):
        dlg._on_next()
        assert dlg._stack.currentIndex() == 1
        assert dlg._prev_btn.isEnabled() is True

    def test_last_slide_button_says_got_it(self, dlg):
        for _ in range(dlg._stack.count() - 1):
            dlg._on_next()
        assert "Got it" in dlg._next_btn.text()

    def test_got_it_accepts(self, dlg):
        for _ in range(dlg._stack.count() - 1):
            dlg._on_next()
        # Don't show again is on by default — clicking Got it persists it.
        assert dlg._dont_show.isChecked() is True
        dlg._on_next()
        from PyQt6.QtWidgets import QDialog as _QD
        assert dlg.result() == _QD.DialogCode.Accepted

    def test_prev_stops_at_first(self, dlg):
        dlg._on_prev()
        assert dlg._stack.currentIndex() == 0

    def test_dots_reflect_progress(self, dlg):
        dlg._on_next()
        dlg._on_next()
        # Two filled + three empty after advancing twice from the start.
        assert dlg._dots.text().count("●") == 3
        assert dlg._dots.text().count("○") == 2

    def test_dont_show_persists_to_qsettings(self, qapp, monkeypatch):
        # Use a private settings key so this test can't leak into others.
        store = {}
        monkeypatch.setattr(
            QSettings, "setValue",
            lambda self, k, v: store.__setitem__(k, v))
        d = gat.WelcomeDialog()
        d._dont_show.setChecked(True)
        d.accept()
        assert store.get(gat.WelcomeDialog.SETTING_KEY) is True

    def test_dont_show_unchecked_no_persist(self, qapp, monkeypatch):
        store = {}
        monkeypatch.setattr(
            QSettings, "setValue",
            lambda self, k, v: store.__setitem__(k, v))
        d = gat.WelcomeDialog()
        d._dont_show.setChecked(False)
        d.accept()
        # Nothing related to the welcome flag should have been written.
        assert gat.WelcomeDialog.SETTING_KEY not in store


class TestMenuIntegration:
    def test_help_menu_has_show_welcome(self, qapp):
        mw = gat.MainWindow()
        try:
            help_menu = next((a.menu() for a in mw.menuBar().actions()
                              if a.text() == "&Help"), None)
            assert help_menu is not None
            labels = [a.text() for a in help_menu.actions()]
            assert any("welcome" in t.lower() for t in labels)
        finally:
            mw.close()

    def test_show_welcome_method_exists(self, qapp):
        mw = gat.MainWindow()
        try:
            assert callable(getattr(mw, "_show_welcome_dialog", None))
        finally:
            mw.close()
