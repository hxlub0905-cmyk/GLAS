"""MainWindow._apply_fa_timing must honour an externally-set GLAS_FA_TIMING
(e.g. a timing launcher .bat) even with dev mode off — the env var reflects dev
mode but must never clobber an explicit external opt-in. Regression guard for
F26 timing-measurement ergonomics.

Exercised via the unbound method + a fake ``self`` so no Qt window is built.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

for _sub in ("glas/core", "glas/app"):
    _p = Path(__file__).resolve().parents[1] / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    import PyQt6.QtWidgets  # noqa: F401
except Exception:  # pragma: no cover
    pytest.skip("PyQt6 unavailable", allow_module_level=True)

import fine_align  # noqa: E402
import gds_align_tool as gat  # noqa: E402


def _apply(dev_mode: bool):
    fake = types.SimpleNamespace(_dev_mode=dev_mode)
    gat.MainWindow._apply_fa_timing(fake)


def test_dev_off_external_env_kept(monkeypatch):
    # A timing .bat set the env; dev mode is off. Timing must stay on and the
    # env var must survive (the spawned pool workers inherit it).
    monkeypatch.setenv("GLAS_FA_TIMING", "1")
    _apply(dev_mode=False)
    assert fine_align._FA_TIMING is True
    assert os.environ.get("GLAS_FA_TIMING") == "1"


def test_dev_off_no_env_silent(monkeypatch):
    monkeypatch.delenv("GLAS_FA_TIMING", raising=False)
    _apply(dev_mode=False)
    assert fine_align._FA_TIMING is False
    assert "GLAS_FA_TIMING" not in os.environ


def test_dev_on_sets_env(monkeypatch):
    monkeypatch.delenv("GLAS_FA_TIMING", raising=False)
    _apply(dev_mode=True)
    assert fine_align._FA_TIMING is True
    assert os.environ.get("GLAS_FA_TIMING") == "1"
