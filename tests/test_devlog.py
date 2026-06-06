"""Tests for devlog — the dev-mode coloured-tag helper (glas/core/devlog.py)."""
from __future__ import annotations

import io
import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1] / "glas" / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import devlog  # noqa: E402


class _FakeTTY(io.StringIO):
    """A stream that claims to be a TTY (so colour is enabled)."""

    def isatty(self) -> bool:
        return True


class _FakePipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _clear_color_env(monkeypatch):
    for var in ("NO_COLOR", "GLAS_NO_COLOR", "PYCHARM_HOSTED"):
        monkeypatch.delenv(var, raising=False)


class TestColorGating:
    def test_tag_plain_on_non_tty(self, monkeypatch):
        _clear_color_env(monkeypatch)
        out = devlog.tag("roi", stream=_FakePipe())
        assert out == "[roi]"           # no escape codes when piped

    def test_tag_colored_on_tty(self, monkeypatch):
        _clear_color_env(monkeypatch)
        out = devlog.tag("roi", stream=_FakeTTY())
        assert out.startswith("\033[") and out.endswith("\033[0m")
        assert "[roi]" in out

    def test_no_color_env_forces_plain(self, monkeypatch):
        _clear_color_env(monkeypatch)
        monkeypatch.setenv("NO_COLOR", "1")
        assert devlog.tag("roi", stream=_FakeTTY()) == "[roi]"

    def test_glas_no_color_env_forces_plain(self, monkeypatch):
        _clear_color_env(monkeypatch)
        monkeypatch.setenv("GLAS_NO_COLOR", "1")
        assert devlog.tag("fa-timing", stream=_FakeTTY()) == "[fa-timing]"

    def test_pycharm_enables_color_without_tty(self, monkeypatch):
        _clear_color_env(monkeypatch)
        monkeypatch.setenv("PYCHARM_HOSTED", "1")
        # Not a TTY, but PyCharm's console renders ANSI → colour on.
        out = devlog.tag("roi", stream=_FakePipe())
        assert out.startswith("\033[")

    def test_unknown_category_still_tags(self, monkeypatch):
        _clear_color_env(monkeypatch)
        # Unknown category: plain text always contains the label; coloured form
        # falls back to bold rather than raising.
        assert devlog.tag("whatever", stream=_FakePipe()) == "[whatever]"
        assert "[whatever]" in devlog.tag("whatever", stream=_FakeTTY())


class TestPaintDim:
    def test_paint_roundtrips_plain(self, monkeypatch):
        _clear_color_env(monkeypatch)
        assert devlog.paint("hi", "roi", stream=_FakePipe()) == "hi"

    def test_paint_wraps_when_colored(self, monkeypatch):
        _clear_color_env(monkeypatch)
        out = devlog.paint("hi", "roi", stream=_FakeTTY(), bold=True)
        assert "hi" in out and out.endswith("\033[0m") and out.startswith("\033[")

    def test_dim_plain_vs_colored(self, monkeypatch):
        _clear_color_env(monkeypatch)
        assert devlog.dim("x", stream=_FakePipe()) == "x"
        assert devlog.dim("x", stream=_FakeTTY()) != "x"
