"""F25 M1: the unified align+export per-image compute must be byte-identical to
the old two-pass (fine-align then export) flow it replaces.

``overlay_export.align_and_export_one_image`` walks each ROI once and then both
fine-aligns (when not already aligned) and rasterizes the export products. These
tests pin it against ``fine_align._fine_align_image`` (the refined offset) and
``overlay_export.export_one_image`` (the PNGs + manifest row) on identical
inputs, using the tiny two-cell OASIS fixture from the random-access tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("cv2")
pytest.importorskip("shapely")
import cv2                       # noqa: E402
import fine_align               # noqa: E402
import overlay_export           # noqa: E402
import oasis_random             # noqa: E402
import test_oasis_random as tr  # noqa: E402


_CFG = {
    "fov_w": 100.0, "fov_h": 100.0, "nm_auto": True, "nm_manual": 0.0,
    "bg_glv": 80, "blur_sigma_px": 1.0, "search_radius_nm": 40.0,
}
# Same POI in both representations: fine-align wants (spec, fg); export wants
# (spec, colour, fg).
_POI_SPECS = [(("raw", 17, 0), 200)]
_POI_COLORED = [(("raw", 17, 0), (255, 0, 0), 200)]
_ALL = dict(export_raw=True, export_overlay=True, export_gray=True,
            export_label=True)


def _reader(tmp_path):
    data, _, _ = tr._build_two_cell()
    p = tmp_path / "two.oas"
    p.write_bytes(data)
    return oasis_random.RandomAccessReader(p, wanted_layers={(17, 0)})


def _sem(tmp_path):
    path = tmp_path / "D1.png"
    # Deterministic non-uniform frame so matchTemplate has structure to lock to.
    arr = (np.indices((50, 50)).sum(axis=0) * 5).astype(np.uint8)
    cv2.imwrite(str(path), arr)
    return path


def _files(d: Path):
    return sorted(p.name for p in d.iterdir())


def _assert_dirs_byte_identical(a: Path, b: Path):
    assert _files(a) == _files(b)
    for name in _files(a):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def test_fused_fresh_align_matches_separate(tmp_path):
    rar = _reader(tmp_path)
    sem = _sem(tmp_path)
    coarse = (60.0, 60.0)

    # Old path A: fine-align computes the refined offset.
    fa = fine_align._fine_align_image(
        ("D1", coarse, str(sem), True), rar, 0, _POI_SPECS, _CFG,
        lambda: False)
    refined = (fa[1], fa[2], fa[3])

    # Old path B: export rasterizes given that refined offset.
    out_sep = tmp_path / "sep"
    out_sep.mkdir()
    row_sep = overlay_export.export_one_image(
        ("D1", coarse, refined, str(sem), True), rar, 0, _POI_COLORED, _CFG,
        str(out_sep), score_thr=-1.0, **_ALL)

    # New fused path: one call does both.
    out_fused = tmp_path / "fused"
    out_fused.mkdir()
    fa2, row_fused = overlay_export.align_and_export_one_image(
        ("D1", coarse, None, str(sem), True), rar, 0, _POI_COLORED, _CFG,
        str(out_fused), score_thr=-1.0, **_ALL)

    assert fa2 == fa                     # identical refined offset + status
    assert row_fused == row_sep          # identical manifest row
    _assert_dirs_byte_identical(out_sep, out_fused)
    # The scenario really exercised the products (guard against a vacuous pass).
    assert row_fused["gray_png"] and row_fused["label_png"]
    assert row_fused["overlay_png"] and row_fused["raw_png"]


def test_fused_reused_align_matches_export(tmp_path):
    rar = _reader(tmp_path)
    sem = _sem(tmp_path)
    coarse = (60.0, 60.0)
    refined = (3.0, -2.0, 0.85)          # a previously-stored alignment

    out_sep = tmp_path / "sep"
    out_sep.mkdir()
    row_sep = overlay_export.export_one_image(
        ("D1", coarse, refined, str(sem), True), rar, 0, _POI_COLORED, _CFG,
        str(out_sep), score_thr=-1.0, **_ALL)

    out_fused = tmp_path / "fused"
    out_fused.mkdir()
    fa2, row_fused = overlay_export.align_and_export_one_image(
        ("D1", coarse, refined, str(sem), True), rar, 0, _POI_COLORED, _CFG,
        str(out_fused), score_thr=-1.0, **_ALL)

    assert fa2 is None                   # reused → orchestrator keeps _refined
    assert row_fused == row_sep
    _assert_dirs_byte_identical(out_sep, out_fused)


def test_fused_csv_only_skips_walk(tmp_path, monkeypatch):
    # An already-aligned image with no image products requested must NOT walk
    # the ROI (the cheap CSV re-export path) — yet still report its offsets.
    rar = _reader(tmp_path)
    sem = _sem(tmp_path)
    walked = []
    for fn in ("poi_polys_for_roi", "poi_polys_and_geometry_for_roi"):
        orig = getattr(fine_align, fn)
        monkeypatch.setattr(fine_align, fn,
                            lambda *a, _f=fn, **k: walked.append(_f))

    out = tmp_path / "out"
    out.mkdir()
    fa, row = overlay_export.align_and_export_one_image(
        ("D1", (60.0, 60.0), (1.0, 2.0, 0.9), str(sem), True), rar, 0,
        _POI_COLORED, _CFG, str(out), score_thr=0.0,
        export_raw=False, export_overlay=False, export_gray=False,
        export_label=False)

    assert walked == []                  # no ROI walk at all
    assert fa is None
    assert row["fine_dx_nm"] == 1.0 and row["fine_dy_nm"] == 2.0
    assert row["score"] == 0.9 and row["status"] == "ok"
    assert _files(out) == []             # nothing written


def test_fused_missing_file(tmp_path):
    rar = _reader(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    fa, row = overlay_export.align_and_export_one_image(
        ("D1", (60.0, 60.0), None, str(tmp_path / "nope.png"), False), rar, 0,
        _POI_COLORED, _CFG, str(out), score_thr=0.0, **_ALL)
    assert fa == ("D1", 0.0, 0.0, 0.0, 0, "missing-file")
    assert row["status"] == "missing-file"
    assert _files(out) == []


def test_afe_pool_task_uses_shared_reader(tmp_path, monkeypatch):
    # The unified pool task pulls the reader from fine_align's per-worker global
    # (the shared F23 warm pool), not a private export reader.
    rar = _reader(tmp_path)
    monkeypatch.setattr(fine_align, "_G", {"rar": rar})
    out = tmp_path / "out"
    out.mkdir()
    fa, row = overlay_export._afe_pool_task(
        ("D1", None, None, "", False), 0, _POI_COLORED, _CFG, str(out),
        False, False, False, False, 0.0)
    # No coords / no file → graceful row, and it ran against the shared reader.
    assert row["image_id"] == "D1"
