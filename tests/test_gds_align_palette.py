"""Layer overlay palette: distinct colours for any number of layers.

``layer_color(idx)`` uses a hand-picked palette for the first N layers, then a
golden-angle hue spiral so colours never wrap back to the first few entries (the
old behaviour the user saw as "only ~4 colours cycling")."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for sub in ("glas/core", "glas/app"):
    if str(_ROOT / sub) not in sys.path:
        sys.path.insert(0, str(_ROOT / sub))

try:
    from PyQt6.QtGui import QColor  # noqa: F401
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover
    pytest.skip("PyQt6 unavailable", allow_module_level=True)

import gds_align_tool as gat  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _rgb(c):
    return (c.red(), c.green(), c.blue())


class TestLayerColor:

    def test_first_n_match_curated_palette(self, qapp):
        for i, want in enumerate(gat._LAYER_PALETTE):
            assert _rgb(gat.layer_color(i)) == _rgb(want)

    def test_curated_palette_is_larger_than_four(self, qapp):
        # Regression: the user reported only ~4 colours. The curated set alone
        # must offer many more distinct hues.
        assert len(gat._LAYER_PALETTE) >= 16
        assert len({_rgb(c) for c in gat._LAYER_PALETTE}) == len(gat._LAYER_PALETTE)

    def test_beyond_palette_generates_distinct(self, qapp):
        # 40 layers → at least ~36 distinct colours (no 4- or 12-colour repeat).
        seen = {_rgb(gat.layer_color(i)) for i in range(40)}
        assert len(seen) >= 36

    def test_no_short_period_repeat(self, qapp):
        # The old bug was a small cycle: assert colour[i] != colour[i+4] and
        # != colour[i+12] across the generated range.
        for i in range(0, 30):
            assert _rgb(gat.layer_color(i)) != _rgb(gat.layer_color(i + 4))
            assert _rgb(gat.layer_color(i)) != _rgb(gat.layer_color(i + 12))

    def test_returns_fresh_qcolor(self, qapp):
        c = gat.layer_color(0)
        c.setAlpha(10)
        assert gat.layer_color(0).alpha() == 255   # palette entry untouched
