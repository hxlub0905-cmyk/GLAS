"""F31 M3: which page of a multi-page patch TIFF the workers align against.

Two properties, and the first matters more:

1. **Nothing changes for single-image datasets.** Every pre-F31 caller passes a
   job tuple with no page element, and a job that carries an explicit ``None``
   must behave the same. Both are pinned against the results the same inputs
   produced before the page argument existed (§7 / the ``test_export_fused``
   byte-identity guard).
2. **The page is actually honoured.** Aligning defect D against page 0 and
   against page 1 of the same TIFF gives different offsets when the frames
   differ, and switching back reproduces the first answer exactly.

Built on the same tiny two-cell OASIS fixture as ``test_export_fused``.
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
import fine_align                # noqa: E402
import overlay_export            # noqa: E402
import oasis_random              # noqa: E402
import tiff_index                # noqa: E402
import test_oasis_random as tr   # noqa: E402

# Search radius 10 nm = 5 px at this FOV. It has to stay small: fine_align_one
# crops a radius-wide border off the template before matching, and a wider
# border eats all of this tiny fixture's structure, leaving a flat patch that
# short-circuits to score 0 (which would make every assertion below vacuous).
_CFG = {
    "fov_w": 100.0, "fov_h": 100.0, "nm_auto": True, "nm_manual": 0.0,
    "bg_glv": 80, "blur_sigma_px": 1.0, "search_radius_nm": 10.0,
}
_W = _H = 50
_NM_PER_PX = _CFG["fov_w"] / _W
_POI_SPECS = [(("raw", 17, 0), 200)]
_POI_COLORED = [(("raw", 17, 0), (255, 0, 0), 200)]
_ALL = dict(export_raw=True, export_overlay=True, export_gray=True,
            export_label=True)
_COARSE = (60.0, 60.0)


def _reader(tmp_path):
    data, _, _ = tr._build_two_cell()
    p = tmp_path / "two.oas"
    p.write_bytes(data)
    return oasis_random.RandomAccessReader(p, wanted_layers={(17, 0)})


def _frame(rar, shift):
    """A synthetic SEM frame that the aligner can actually lock onto: the POI
    template itself, rolled ``shift`` px to the right. Alignment should then
    recover ``-shift * nm_per_px`` in x, and two pages rolled differently must
    produce different offsets — with an unrelated pattern the match scores 0 and
    the test proves nothing."""
    roi = (_COARSE[0] - 100, _COARSE[1] - 100,
           _COARSE[0] + 100, _COARSE[1] + 100)
    polys = fine_align.poi_polys_for_roi(rar, 0, roi, ("raw", 17, 0))
    tmpl = fine_align.render_composite_template(
        [(polys, 200)], _COARSE, _W, _H, _NM_PER_PX,
        _CFG["bg_glv"], _CFG["blur_sigma_px"])
    return np.roll(tmpl, shift, axis=1)


def _png(tmp_path, rar, shift=0, name="D1.png"):
    p = tmp_path / name
    cv2.imwrite(str(p), _frame(rar, shift))
    return p


def _multipage(tmp_path, rar, shifts, name="batch.tif"):
    tifffile = pytest.importorskip("tifffile")
    p = tmp_path / name
    with tifffile.TiffWriter(str(p)) as tw:
        for s in shifts:
            tw.write(_frame(rar, s), contiguous=False)
    return p


@pytest.fixture(autouse=True)
def _close_handles():
    yield
    tiff_index.close_cached_tiffs()


def _align(job, rar):
    return fine_align._fine_align_image(job, rar, 0, _POI_SPECS, _CFG,
                                        lambda: False)


# ── 1. the single-image path is untouched ────────────────────────────────────

def test_short_job_and_explicit_none_agree(tmp_path):
    """A 4-element job (every pre-F31 caller) and a 5-element one ending in
    None must be the same call."""
    rar = _reader(tmp_path)
    sem = _png(tmp_path, rar)
    assert _align(("D1", _COARSE, str(sem), True), rar) == \
        _align(("D1", _COARSE, str(sem), True, None), rar)


def test_export_short_job_and_explicit_none_agree(tmp_path):
    rar = _reader(tmp_path)
    sem = _png(tmp_path, rar)
    outs = []
    for job in (("D1", _COARSE, None, str(sem), True),
                ("D1", _COARSE, None, str(sem), True, None)):
        d = tmp_path / f"o{len(outs)}"
        d.mkdir()
        fa, row = overlay_export.align_and_export_one_image(
            job, rar, 0, _POI_COLORED, _CFG, str(d), score_thr=-1.0, **_ALL)
        outs.append((fa, row, sorted(
            (p.name, p.read_bytes()) for p in d.iterdir())))
    assert outs[0] == outs[1]


def test_missing_file_still_reported(tmp_path):
    rar = _reader(tmp_path)
    fa = _align(("D1", _COARSE, str(tmp_path / "nope.png"), False, None), rar)
    assert fa[5] == "missing-file"


# ── 2. the page is honoured ──────────────────────────────────────────────────

def test_different_pages_give_different_offsets(tmp_path):
    """The regression this whole feature exists for: before F31 every defect
    read page 0, so two defects in one batch TIFF aligned identically."""
    rar = _reader(tmp_path)
    tif = _multipage(tmp_path, rar, [0, 3])
    p0 = _align(("D1", _COARSE, str(tif), True, 0), rar)
    p1 = _align(("D1", _COARSE, str(tif), True, 1), rar)
    assert (p0[1], p0[2]) != (p1[1], p1[2])


def test_switching_back_reproduces_the_offset(tmp_path):
    """Changing the setting and changing it back is not allowed to drift."""
    rar = _reader(tmp_path)
    tif = _multipage(tmp_path, rar, [0, 3])
    first = _align(("D1", _COARSE, str(tif), True, 0), rar)
    _align(("D1", _COARSE, str(tif), True, 1), rar)
    assert _align(("D1", _COARSE, str(tif), True, 0), rar) == first


def test_page_matches_the_equivalent_standalone_image(tmp_path):
    """Page N of a batch TIFF and the same frame as its own file must align
    identically — the page read is the only difference."""
    rar = _reader(tmp_path)
    tif = _multipage(tmp_path, rar, [0, 3])
    solo = _png(tmp_path, rar, 3, "solo.png")
    from_page = _align(("D1", _COARSE, str(tif), True, 1), rar)
    from_file = _align(("D1", _COARSE, str(solo), True, None), rar)
    assert from_page[1:] == from_file[1:]


def test_export_products_come_from_the_selected_page(tmp_path):
    """The exported raw PNG is the frame that was aligned, so downstream can
    trust that label/gray sit on the same grid as the patch they name."""
    rar = _reader(tmp_path)
    tif = _multipage(tmp_path, rar, [0, 3])
    got = {}
    for page in (0, 1):
        d = tmp_path / f"p{page}"
        d.mkdir()
        overlay_export.align_and_export_one_image(
            ("D1", _COARSE, None, str(tif), True, page), rar, 0, _POI_COLORED,
            _CFG, str(d), score_thr=-1.0, **_ALL)
        got[page] = cv2.imread(str(d / "D1_raw.png"), cv2.IMREAD_GRAYSCALE)
    assert np.array_equal(got[0], _frame(rar, 0))
    assert np.array_equal(got[1], _frame(rar, 3))


def test_two_defects_in_one_tiff_do_not_collide(tmp_path):
    """End-to-end shape of the real dataset: four pages, two defects, two pages
    each. Their exported frames must differ."""
    rar = _reader(tmp_path)
    tif = _multipage(tmp_path, rar, [0, 2, 3, 4])
    frames = []
    for did, page in (("D1", 1), ("D2", 3)):     # each defect's ref page
        d = tmp_path / did
        d.mkdir()
        overlay_export.align_and_export_one_image(
            (did, _COARSE, None, str(tif), True, page), rar, 0, _POI_COLORED,
            _CFG, str(d), score_thr=-1.0, **_ALL)
        frames.append(cv2.imread(str(d / f"{did}_raw.png"),
                                 cv2.IMREAD_GRAYSCALE))
    assert not np.array_equal(frames[0], frames[1])


def test_unreadable_page_degrades_to_missing_file(tmp_path):
    """A page past the end of the file must not take the batch down with it."""
    rar = _reader(tmp_path)
    tif = _multipage(tmp_path, rar, [0, 3])
    fa = _align(("D1", _COARSE, str(tif), True, 7), rar)
    assert fa[5] == "missing-file"


def test_recovered_offset_matches_the_known_shift(tmp_path):
    """Non-vacuity guard for every assertion above: the frames really are
    alignable, and the page really drives the answer. Page 1 is the template
    rolled 3 px right, so alignment must report −3 px in x (image x right = GDS
    anchor x down, §7) and ~0 in y, at a near-perfect score."""
    rar = _reader(tmp_path)
    tif = _multipage(tmp_path, rar, [0, 3])
    p0, p1 = (_align(("D1", _COARSE, str(tif), True, pg), rar) for pg in (0, 1))
    assert p0[3] > 0.9 and p1[3] > 0.9              # score
    assert abs(p0[1]) < 0.5 and abs(p0[2]) < 0.5    # unshifted page
    assert p1[1] == pytest.approx(-3 * _NM_PER_PX, abs=0.5)
    assert abs(p1[2]) < 0.5
