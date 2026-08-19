"""F31 M2: the vendored read-only KLARF reader (``klarf_doc``).

``klarf_doc`` is ADEPT's ``ingest/klarf_core.py`` minus its writer; these tests
pin the surface GLAS's EBI-patch ingest actually leans on, so a future re-sync
against ADEPT fails loudly rather than quietly changing which page a defect
gets:

* KLARF 1.2 **flat** parses at all -- GLAS's own ``klarf_parser`` cannot read
  this format (measured: empty ``defect_columns``, every field falling into
  ``_extra_N``), which is why this module was vendored;
* units come from the version (1.2 µm / 1.8 nm) -- a 1000× error in the ROI if
  it's wrong;
* ``defect_image_map`` assigns each defect its TIFF pages by the IMAGELIST page
  ids when they are trustworthy, and sequentially when they are not, saying
  which rule it used.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("glas/core", "glas/app"):
    _p = _ROOT / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import klarf_doc  # noqa: E402


# Two defects × two patches, pages addressed 1-based through IMAGELIST.
KLARF12_EBI = """\
FileVersion 1 2;
LotID "LOT001";
DeviceID "DEV01";
StepID "STEP01";
DiePitch 1000.0 1200.0;
WaferID "W01";
TiffFileName LOT001.tif;
TiffSpec 6.1 2 "IMAGEVERSION" "IMAGEXYPOS";
InspectionTest 1;
DefectRecordSpec 10 DEFECTID XREL YREL XINDEX YINDEX XSIZE YSIZE CLASSNUMBER IMAGECOUNT IMAGELIST ;
DefectList
 1 100.500 200.500 1 2 0.5 0.5 1 2 1 0 2 0
 2 300.000 400.000 2 3 0.6 0.6 4 2 3 0 4 0;
SummarySpec 5 TESTNO NDEFECT DEFDENSITY NDIE NDEFDIE ;
SummaryList
 1 2 1.0000000000e-03 10 2;
EndOfFile;
"""

KLARF18_EBI = """\
Record FileRecord  "1.8"
{
  Record LotRecord "LOT.01"
  {
    Record WaferRecord "W01"
    {
      Field DiePitch 2 {23376636, 32874750}
      Field TiffFileName 1 {"LOT001.tif"}

      List DefectList
      {
        Columns 8 { int32 DEFECTID,  int32 XREL,  int32 YREL,  int32 XINDEX,
        int32 YINDEX,  int32 CLASSNUMBER,  int32 IMAGECOUNT,  ImageList IMAGELIST  }
        Data 2
        {
          1 100500 200500 0 0 1 2 1 0 2 0 ;
          2 300000 400000 1 -1 2 2 3 0 4 0 ;
        }
      }
    }
  }
}
EndOfFile;
"""


class TestVersionAndUnits:

    def test_12_flat_parses(self):
        """The format GLAS's own parser cannot read."""
        d = klarf_doc.load(KLARF12_EBI)
        assert d.version == "1.2"
        assert d.defect_columns[:3] == ["DEFECTID", "XREL", "YREL"]
        assert len(d.defects) == 2
        assert d.defects[0][:3] == ["1", "100.500", "200.500"]

    def test_12_is_micrometres(self):
        assert klarf_doc.load(KLARF12_EBI).unit_info()["to_nm"] == 1000.0

    def test_18_is_nanometres(self):
        assert klarf_doc.load(KLARF18_EBI).unit_info()["to_nm"] == 1.0

    def test_tiff_file_name_both_spellings(self):
        assert klarf_doc.load(KLARF12_EBI).tiff_file_name == "LOT001.tif"
        assert klarf_doc.load(KLARF18_EBI).tiff_file_name == "LOT001.tif"


class TestDefectImageMap:

    def test_12_imagelist_pages(self):
        m = klarf_doc.load(KLARF12_EBI).defect_image_map(4)
        assert m["mode"] == "imagelist"
        assert m["base"] == 1                 # ids 1..4 over 4 pages
        assert m["pages"] == [[0, 1], [2, 3]]

    def test_18_imagelist_pages(self):
        m = klarf_doc.load(KLARF18_EBI).defect_image_map(4)
        assert m["mode"] == "imagelist"
        assert m["pages"] == [[0, 1], [2, 3]]

    def test_每顆_defect_的頁互不重疊(self):
        """The property the whole feature rests on: no two defects share a
        page, so no two defects can align against the same frame."""
        for text in (KLARF12_EBI, KLARF18_EBI):
            pages = klarf_doc.load(text).defect_image_map(4)["pages"]
            flat = [p for row in pages for p in row]
            assert len(set(flat)) == len(flat)

    def test_ids_out_of_range_fall_back_to_sequential(self):
        """IMAGELIST ids that don't fit the real page count are not page
        numbers; guessing anyway would mis-assign the whole lot."""
        m = klarf_doc.load(KLARF12_EBI).defect_image_map(2)   # ids go to 4
        assert m["mode"] == "sequential"
        assert m["pages"] == [[0, 1], [2, 3]]
        assert any("do not fit" in n for n in m["notes"])

    def test_ids_within_a_larger_file_are_still_used(self):
        """A batch TIFF holding more pages than this KLARF references is
        normal (a lot re-exported with a defect subset); ids that still fit
        stay authoritative."""
        m = klarf_doc.load(KLARF12_EBI).defect_image_map(99)
        assert m["mode"] == "imagelist"
        assert m["pages"] == [[0, 1], [2, 3]]

    def test_duplicate_ids_fall_back_and_flag_page_count(self):
        """Repeated IMAGELIST ids cannot be page numbers -- two defects would
        share a frame. Falls back to sequential, and says so; the page-count
        mismatch is reported too, since sequential mapping is only as good as
        IMAGECOUNT summing to the real page count."""
        text = KLARF12_EBI.replace(
            " 2 300.000 400.000 2 3 0.6 0.6 4 2 3 0 4 0;",
            " 2 300.000 400.000 2 3 0.6 0.6 4 2 1 0 2 0;")   # reuses ids 1,2
        m = klarf_doc.load(text).defect_image_map(9)
        assert m["mode"] == "sequential"
        assert m["pages"] == [[0, 1], [2, 3]]
        assert any("duplicates" in n for n in m["notes"])
        assert any("TIFF page count" in n for n in m["notes"])

    def test_mode_none_without_image_columns(self):
        """A plain KLARF with no image columns must report "no mapping" so the
        loader falls through to the rSEM path instead of inventing pages."""
        text = KLARF12_EBI.replace(
            "DefectRecordSpec 10 DEFECTID XREL YREL XINDEX YINDEX XSIZE YSIZE "
            "CLASSNUMBER IMAGECOUNT IMAGELIST ;",
            "DefectRecordSpec 8 DEFECTID XREL YREL XINDEX YINDEX XSIZE YSIZE "
            "CLASSNUMBER ;")
        assert klarf_doc.load(text).defect_image_map(4)["mode"] is None

    def test_zero_image_defect_gets_no_pages(self):
        text = KLARF12_EBI.replace(
            " 2 300.000 400.000 2 3 0.6 0.6 4 2 3 0 4 0;",
            " 2 300.000 400.000 2 3 0.6 0.6 4 0;")
        m = klarf_doc.load(text).defect_image_map(2)
        assert m["pages"][1] == []


class TestTiffPath:

    def test_finds_tiff_beside_the_klarf(self, tmp_path):
        (tmp_path / "LOT001.tif").write_bytes(b"II*\x00")
        k = tmp_path / "lot.klarf"
        k.write_text(KLARF12_EBI)
        assert klarf_doc.load(str(k)).tiff_path() == str(tmp_path / "LOT001.tif")

    def test_none_when_absent(self, tmp_path):
        k = tmp_path / "lot.klarf"
        k.write_text(KLARF12_EBI)
        assert klarf_doc.load(str(k)).tiff_path() is None
