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

