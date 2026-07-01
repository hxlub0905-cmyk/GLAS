"""F26 diagnostic: with GLAS_FA_TIMING on (dev mode), the fused export path
prints one ``[export-timing]`` line per image so a batch shows where its
wall-clock goes. Off (default), it prints nothing — guarded by test_export_fused
staying byte-identical.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

for _sub in ("glas/core", "glas/app"):
    _p = Path(__file__).resolve().parents[1] / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import fine_align       # noqa: E402
import overlay_export   # noqa: E402


_CFG = {"fov_w": 1000, "fov_h": 1000, "nm_auto": True, "nm_manual": 0,
        "bg_glv": 80, "blur_sigma_px": 1.0}


def test_timing_on_emits_one_line(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(fine_align, "_FA_TIMING", True)
    # exists=False -> SEM read fails -> missing-file path, which still emits.
    job = ("D42", (1000, 2000), None, str(tmp_path / "nope.png"), False)
    overlay_export.align_and_export_one_image(
        job, object(), None, [], _CFG, tmp_path,
        False, True, False, False, 0.0)     # export_overlay=True
    out = capsys.readouterr().out
    assert "[export-timing]" in out
    assert "img=D42" in out
    assert "status=missing-file" in out
    for field in ("cells_decoded=", "reach_new=", "cellvisits=", "instances=",
                  "[walk: place=", "rect=", "poly="):
        assert field in out


def test_timing_off_is_silent(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(fine_align, "_FA_TIMING", False)
    job = ("D7", (1000, 2000), None, str(tmp_path / "nope.png"), False)
    overlay_export.align_and_export_one_image(
        job, object(), None, [], _CFG, tmp_path,
        False, True, False, False, 0.0)
    assert "[export-timing]" not in capsys.readouterr().out
