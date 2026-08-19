"""Tests for tools/sem_loader.py (F2 M3 SEM image loading)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import sem_loader  # noqa: E402
from sem_loader import (  # noqa: E402
    SemImage, load_klarf, load_folder, read_die_pitch_nm,
)


_FLAT_KLARF = """\
FileVersion 1 8
DefectList 4 DEFECTID XREL YREL IMAGELIST
Data 2
1 1000 2000 Image 1 { "a.tif" } ;
2 3000 4000 Image 1 { "b.tif" } ;
EndOfList
"""

_KLARF_NO_IMG = """\
FileVersion 1 8
DefectList 3 DEFECTID XREL YREL
Data 1
1 1000 2000 ;
EndOfList
"""


class TestLoadKlarf:

    def test_basic(self, tmp_path):
        k = tmp_path / "lot.klarf"
        k.write_text(_FLAT_KLARF)
        imgs = load_klarf(k)
        assert len(imgs) == 2
        a, b = imgs
        assert a.image_id == "1"
        assert a.filename == "a.tif"
        assert a.xrel == 1000 and a.yrel == 2000
        assert a.file_path == tmp_path / "a.tif"
        assert b.xrel == 3000 and b.yrel == 4000

    def test_has_coords_and_exists(self, tmp_path):
        k = tmp_path / "lot.klarf"
        k.write_text(_FLAT_KLARF)
        (tmp_path / "a.tif").write_bytes(b"img")  # only a exists
        imgs = load_klarf(k)
        assert imgs[0].has_coords is True
        assert imgs[0].exists is True
        assert imgs[1].exists is False   # b.tif not created

    def test_defects_without_image_skipped(self, tmp_path):
        k = tmp_path / "lot.klarf"
        k.write_text(_KLARF_NO_IMG)
        imgs = load_klarf(k)
        assert imgs == []


class TestLoadFolder:

    def test_scans_images_sorted(self, tmp_path):
        for name in ["b.png", "a.tif", "c.jpg", "notes.txt", "d.bmp"]:
            (tmp_path / name).write_bytes(b"x")
        imgs = load_folder(tmp_path)
        names = [i.filename for i in imgs]
        assert names == ["a.tif", "b.png", "c.jpg", "d.bmp"]  # txt excluded
        assert all(not i.has_coords for i in imgs)
        assert all(i.exists for i in imgs)

    def test_not_a_dir(self, tmp_path):
        f = tmp_path / "x.png"
        f.write_bytes(b"x")
        assert load_folder(f) == []

    def test_empty_dir(self, tmp_path):
        assert load_folder(tmp_path) == []


class TestSemImage:

    def test_no_coords(self):
        s = SemImage(image_id="1", filename="a.png", file_path=None)
        assert s.has_coords is False
        assert s.exists is False


class TestRealKlarfFixture:
    """Locks in parsing of a real hierarchical KLARF 1.8 (KLA PRIMEVISION).
    Fixture tests/fixtures/sample_real.klarf is a verbatim production file
    with the 1161-row ClassLookupList trimmed to 3 rows (irrelevant to image
    loading). Exercises: hierarchical Record/List structure, 42-column
    DefectList, multi-line defect rows, the ``Images`` (plural) keyword, and
    large (~20M nm) die-corner XREL/YREL."""

    FIX = Path(__file__).resolve().parent / "fixtures" / "sample_real.klarf"

    def test_loads_six_images(self):
        imgs = load_klarf(self.FIX)
        assert len(imgs) == 6
        assert [i.image_id for i in imgs] == [
            "6301", "11205", "25901", "26608", "27301", "168201"]
        assert [i.filename for i in imgs] == [
            f"1.000_0000{n}.jpg" for n in range(1, 7)]

    def test_first_and_last_coords(self):
        imgs = load_klarf(self.FIX)
        assert imgs[0].xrel == 20267174 and imgs[0].yrel == 20652619
        assert imgs[-1].xrel == 20282634 and imgs[-1].yrel == 20642982
        assert all(i.has_coords for i in imgs)

    def test_die_pitch(self):
        # Field DiePitch 2 {23376636, 32874750}  (nm)
        assert read_die_pitch_nm(self.FIX) == (23376636.0, 32874750.0)


class TestReadDiePitch:

    def test_missing(self, tmp_path):
        f = tmp_path / "x.klarf"
        f.write_text("FileVersion 1 8\n")
        assert read_die_pitch_nm(f) is None

    def test_float_form(self, tmp_path):
        f = tmp_path / "x.klarf"
        f.write_text("Field DiePitch 2 {1.0e6, 2.0e6}\n")
        assert read_die_pitch_nm(f) == (1.0e6, 2.0e6)



# ── F31: EBI-patch KLARF ingest ──────────────────────────────────────────────
#
# One multi-page TIFF holds the whole lot's patches; each defect owns a couple
# of pages (page 1 test, page 2 ref) addressed through IMAGECOUNT / IMAGELIST.
# Before F31 both shapes below loaded ZERO images: the 1.2 file is not
# parseable by klarf_parser at all, and the 1.8 one has no per-defect filename
# so every defect was skipped.

import test_klarf_doc as _kd  # noqa: E402  (reuses its two EBI fixtures)


def _write_batch_tiff(path, n_pages):
    tifffile = pytest.importorskip("tifffile")
    np = pytest.importorskip("numpy")
    with tifffile.TiffWriter(str(path)) as tw:
        for i in range(n_pages):
            tw.write(np.full((8, 8), (i * 30 + 7) % 256, dtype=np.uint8),
                     contiguous=False)
    return path


@pytest.fixture
def ebi12(tmp_path):
    _write_batch_tiff(tmp_path / "LOT001.tif", 4)
    k = tmp_path / "lot.klarf"
    k.write_text(_kd.KLARF12_EBI)
    return k


@pytest.fixture
def ebi18(tmp_path):
    _write_batch_tiff(tmp_path / "LOT001.tif", 4)
    k = tmp_path / "lot.klarf"
    k.write_text(_kd.KLARF18_EBI)
    return k


class TestLoadKlarfEbi:

    def test_loads_defects_that_used_to_be_dropped(self, ebi12):
        imgs = load_klarf(ebi12)
        assert [i.image_id for i in imgs] == ["1", "2"]
        assert all(i.filename == "LOT001.tif" for i in imgs)
        assert all(i.exists for i in imgs)

    def test_each_defect_gets_its_own_pages(self, ebi12):
        imgs = load_klarf(ebi12)
        assert [i.pages for i in imgs] == [(0, 1), (2, 3)]

    def test_no_two_defects_share_a_frame(self, ebi12):
        """The bug F31 exists to kill: every defect reading page 0."""
        imgs = load_klarf(ebi12)
        keys = [(str(i.file_path), i.page) for i in imgs]
        assert len(set(keys)) == len(keys)

    def test_defaults_to_ref_the_second_page(self, ebi12):
        # F31 Q4: ref carries no defect, so it template-matches most cleanly.
        assert [i.page for i in load_klarf(ebi12)] == [1, 3]

    def test_ordinal_selects_test_or_ref(self, ebi12):
        assert [i.page for i in load_klarf(ebi12, align_page_ordinal=1)] == [0, 2]
        assert [i.page for i in load_klarf(ebi12, align_page_ordinal=2)] == [1, 3]

    def test_ordinal_past_the_end_falls_back_to_last_page(self, ebi12):
        assert [i.page for i in load_klarf(ebi12, align_page_ordinal=9)] == [1, 3]

    def test_micrometre_coords_converted_to_nm(self, ebi12):
        """KLARF 1.2 stores µm. klarf_to_gds takes nm unconditionally and is
        frozen (§7), so the conversion has to land here."""
        a, b = load_klarf(ebi12)
        assert a.xrel == 100500.0 and a.yrel == 200500.0
        assert b.xrel == 300000.0 and b.yrel == 400000.0

    def test_12_and_18_agree_on_coordinates(self, ebi12, ebi18):
        """The same defects written in µm (1.2) and nm (1.8) must arrive at the
        same nm coordinates -- that equality is what the unit handling means."""
        a = [(i.xrel, i.yrel) for i in load_klarf(ebi12)]
        b = [(i.xrel, i.yrel) for i in load_klarf(ebi18)]
        assert a == b

    def test_18_hierarchical_also_loads(self, ebi18):
        imgs = load_klarf(ebi18)
        assert [i.pages for i in imgs] == [(0, 1), (2, 3)]
        assert [i.page for i in imgs] == [1, 3]

    def test_id_source_is_defectid(self, ebi12):
        assert all(i.id_source == "klarf-defectid" for i in load_klarf(ebi12))

    def test_notes_report_how_pages_were_mapped(self, ebi12):
        notes = []
        load_klarf(ebi12, notes=notes)
        assert any("page" in n.lower() for n in notes)

    def test_defect_without_a_patch_is_skipped_and_noted(self, tmp_path):
        _write_batch_tiff(tmp_path / "LOT001.tif", 2)
        k = tmp_path / "lot.klarf"
        k.write_text(_kd.KLARF12_EBI.replace(
            " 2 300.000 400.000 2 3 0.6 0.6 4 2 3 0 4 0;",
            " 2 300.000 400.000 2 3 0.6 0.6 4 0;"))
        notes = []
        imgs = load_klarf(k, notes=notes)
        assert [i.image_id for i in imgs] == ["1"]
        assert any("no patch image" in n for n in notes)

    def test_missing_tiff_falls_through_to_the_old_path(self, tmp_path):
        """No batch TIFF beside the KLARF => not an EBI dataset. It must not be
        half-loaded; the rSEM path decides (and finds no filenames here)."""
        k = tmp_path / "lot.klarf"
        k.write_text(_kd.KLARF12_EBI)
        assert load_klarf(k) == []


class TestRsemUnaffectedByF31:
    """The pre-F31 path must be untouched: same images, no pages, and the
    single-image read contract (page is None) preserved."""

    FIX = Path(__file__).resolve().parent / "fixtures" / "sample_real.klarf"

    def test_still_six_images_with_no_pages(self):
        imgs = load_klarf(self.FIX)
        assert len(imgs) == 6
        assert all(i.pages == () and i.page is None for i in imgs)

    def test_id_source_recorded(self):
        assert all(i.id_source == "klarf-defectid" for i in load_klarf(self.FIX))

    def test_folder_ids_come_from_filenames(self, tmp_path):
        (tmp_path / "a.png").write_bytes(b"x")
        imgs = load_folder(tmp_path)
        assert [i.id_source for i in imgs] == ["filename-stem"]
        assert imgs[0].page is None


class TestDiePitch12:

    def test_flat_micrometre_form_scaled_to_nm(self, tmp_path):
        f = tmp_path / "x.klarf"
        f.write_text("FileVersion 1 2;\nDiePitch 1000.0 1200.0;\n")
        assert read_die_pitch_nm(f) == (1.0e6, 1.2e6)

    def test_18_form_still_wins_and_is_unscaled(self, tmp_path):
        f = tmp_path / "x.klarf"
        f.write_text("Field DiePitch 2 {23376636, 32874750}\n")
        assert read_die_pitch_nm(f) == (23376636.0, 32874750.0)
