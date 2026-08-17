"""F31 M4: the overlay manifest contract (schema ``mmh-gds-overlay-v4``).

The manifest is what a downstream consumer reads instead of parsing layout, so
these tests pin the two things it previously could not express:

* **Provenance and grid.** ``id_source`` says whether ``image_id`` is a KLARF
  ``DEFECTID`` (joinable) or a filename stem; ``width_px`` / ``height_px`` /
  ``nm_per_px`` / ``page`` say which pixel grid the label map was drawn on.
  Without them a consumer has to *assume*, and a wrong assumption mis-joins or
  mis-places every row without erroring.
* **Six distinguishable statuses.** "The file isn't there" used to cover three
  different situations with three different responses: never run, aligned but
  below threshold (don't trust the regions), and no coordinates at all (a data
  problem). ``low-score`` in particular used to be reported as ``ok``.
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
import test_oasis_random as tr   # noqa: E402

_CFG = {
    "fov_w": 100.0, "fov_h": 100.0, "nm_auto": True, "nm_manual": 0.0,
    "bg_glv": 80, "blur_sigma_px": 1.0, "search_radius_nm": 10.0,
}
_POI_COLORED = [(("raw", 17, 0), (255, 0, 0), 200)]
_ALL = dict(export_raw=True, export_overlay=True, export_gray=True,
            export_label=True)
_COARSE = (60.0, 60.0)


def _reader(tmp_path):
    data, _, _ = tr._build_two_cell()
    p = tmp_path / "two.oas"
    p.write_bytes(data)
    return oasis_random.RandomAccessReader(p, wanted_layers={(17, 0)})


def _sem(tmp_path, w=50, h=50, name="D1.png"):
    p = tmp_path / name
    cv2.imwrite(str(p), (np.indices((h, w)).sum(axis=0) * 5).astype(np.uint8))
    return p


def _export(tmp_path, job, thr=-1.0, cfg=None, out="o"):
    d = tmp_path / out
    d.mkdir(exist_ok=True)
    _fa, row = overlay_export.align_and_export_one_image(
        job, _reader(tmp_path), 0, _POI_COLORED, cfg or _CFG, str(d),
        score_thr=thr, **_ALL)
    return row


# ── status vocabulary ────────────────────────────────────────────────────────

class TestStatus:

    def test_manifest_status_pure_logic(self):
        assert fine_align.manifest_status(None, None, 0.5) == "no-coords"
        assert fine_align.manifest_status((0, 0), None, 0.5) == "not-run"
        assert fine_align.manifest_status((0, 0), (1, 2, 0.9), 0.5) == "ok"
        assert fine_align.manifest_status((0, 0), (1, 2, 0.1), 0.5) == "low-score"

    def test_at_the_threshold_counts_as_ok(self):
        """Same boundary as ``mask_should_export`` (>=), so a row that says "ok"
        is exactly a row whose gray/label were written."""
        assert fine_align.manifest_status((0, 0), (0, 0, 0.5), 0.5) == "ok"
        assert fine_align.mask_should_export((0, 0, 0.5), 0.5) is True

    def test_low_score_is_no_longer_reported_as_ok(self, tmp_path):
        """The behaviour fix: the score gate used to blank the filenames but
        leave status "ok", so a consumer could not tell an untrustworthy
        alignment from a good one."""
        sem = _sem(tmp_path)
        row = _export(tmp_path, ("D1", _COARSE, None, str(sem), True), thr=2.0)
        assert row["status"] == "low-score"
        assert row["gray_png"] == "" and row["label_png"] == ""
        assert row["score"] != ""          # the score itself is still reported

    def test_no_coords(self, tmp_path):
        sem = _sem(tmp_path)
        row = _export(tmp_path, ("D1", None, None, str(sem), True))
        assert row["status"] == "no-coords"

    def test_missing_file(self, tmp_path):
        row = _export(tmp_path, ("D1", _COARSE, None, str(tmp_path / "no.png"),
                                 False))
        assert row["status"] == "missing-file"

    def test_flat_roi(self, tmp_path):
        """An ROI with no geometry is not the same as "not run" — there was
        nothing to match against."""
        sem = _sem(tmp_path)
        far = (10_000_000.0, 10_000_000.0)
        row = _export(tmp_path, ("D1", far, None, str(sem), True))
        assert row["status"] == "flat"

    def test_not_run_on_a_csv_only_reexport(self, tmp_path):
        """No products requested and no stored alignment: the image was simply
        never aligned."""
        d = tmp_path / "csv"
        d.mkdir()
        _fa, row = overlay_export.align_and_export_one_image(
            ("D1", None, None, "", False), _reader(tmp_path), 0, _POI_COLORED,
            _CFG, str(d), score_thr=0.0, export_raw=False, export_overlay=False,
            export_gray=False, export_label=False)
        assert row["status"] == "no-coords"

    def test_every_status_is_in_the_declared_vocabulary(self, tmp_path):
        sem = _sem(tmp_path)
        rows = [
            _export(tmp_path, ("A", _COARSE, None, str(sem), True), out="a"),
            _export(tmp_path, ("B", _COARSE, None, str(sem), True), thr=2.0,
                    out="b"),
            _export(tmp_path, ("C", None, None, str(sem), True), out="c"),
            _export(tmp_path, ("D", _COARSE, None, str(tmp_path / "x"), False),
                    out="d"),
        ]
        for r in rows:
            assert r["status"] in fine_align.MANIFEST_STATUSES, r


# ── provenance + pixel grid ──────────────────────────────────────────────────

class TestProvenanceAndGrid:

    def test_columns_declared(self):
        for col in ("id_source", "page", "width_px", "height_px", "nm_per_px"):
            assert col in fine_align.OVERLAY_MANIFEST_COLS, col

    def test_grid_matches_the_exported_images(self, tmp_path):
        """The whole point: a consumer can check the label map is on the same
        grid as the patch it is naming."""
        sem = _sem(tmp_path, w=64, h=48)
        row = _export(tmp_path, ("D1", _COARSE, None, str(sem), True))
        assert (row["width_px"], row["height_px"]) == (64, 48)
        lbl = cv2.imread(str(tmp_path / "o" / row["label_png"]),
                         cv2.IMREAD_UNCHANGED)
        assert lbl.shape[:2] == (row["height_px"], row["width_px"])

    def test_nm_per_px_is_per_image_under_auto_scaling(self, tmp_path):
        """Two frames of different widths in one batch genuinely have different
        scales; the alignment CSV's single value cannot express that."""
        a = _export(tmp_path, ("A", _COARSE, None, str(_sem(tmp_path, 50, 50,
                    "a.png")), True), out="a")
        b = _export(tmp_path, ("B", _COARSE, None, str(_sem(tmp_path, 100, 100,
                    "b.png")), True), out="b")
        assert a["nm_per_px"] == pytest.approx(2.0)
        assert b["nm_per_px"] == pytest.approx(1.0)

    def test_manual_scale_is_reported_not_recomputed(self, tmp_path):
        cfg = dict(_CFG, nm_auto=False, nm_manual=3.25)
        row = _export(tmp_path, ("D1", _COARSE, None, str(_sem(tmp_path)), True),
                      cfg=cfg)
        assert row["nm_per_px"] == pytest.approx(3.25)

    def test_page_and_id_source_ride_the_job(self, tmp_path):
        sem = _sem(tmp_path)
        row = _export(tmp_path, ("D1", _COARSE, None, str(sem), True, None,
                                 "klarf-defectid"))
        assert row["id_source"] == "klarf-defectid"
        assert row["page"] == ""           # single-image dataset

    def test_absent_page_and_id_source_are_blank_not_wrong(self, tmp_path):
        """A pre-F31 5-element job must not have provenance invented for it."""
        row = _export(tmp_path, ("D1", _COARSE, None, str(_sem(tmp_path)), True))
        assert row["id_source"] == "" and row["page"] == ""

    def test_row_keys_cover_the_declared_columns(self, tmp_path):
        row = _export(tmp_path, ("D1", _COARSE, None, str(_sem(tmp_path)), True))
        assert set(fine_align.OVERLAY_MANIFEST_COLS) <= set(row)


# ── label_map layer names (interface suggestion 5) ───────────────────────────

class TestLabelMapNames:

    def test_clean_names_produce_no_warnings(self):
        assert fine_align.label_map_warnings(
            [{"id": 1, "layer": "MG"}, {"id": 2, "layer": "EPI_2"}]) == []

    @pytest.mark.parametrize("name", ["poly gate", "M1-metal", "層/1", "2nd"])
    def test_unusable_identifiers_are_flagged(self, name):
        w = fine_align.label_map_warnings([{"id": 1, "layer": name}])
        assert len(w) == 1 and name in w[0]

    def test_duplicate_names_are_flagged(self):
        w = fine_align.label_map_warnings(
            [{"id": 1, "layer": "MG"}, {"id": 2, "layer": "MG"}])
        assert any("ambiguous" in m for m in w)

    def test_names_are_never_rewritten(self):
        """GLAS warns and leaves the name alone: the name is the join between a
        recipe and a label id, so renaming it here would break the recipe that
        already points at it."""
        lm = [{"id": 1, "layer": "poly gate"}]
        fine_align.label_map_warnings(lm)
        assert lm == [{"id": 1, "layer": "poly gate"}]
