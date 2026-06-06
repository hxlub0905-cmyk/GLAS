"""Unit tests for :mod:`parts_catalog` (F21 M1).

Covers: load round-trip, partial records (missing keys -> defaults),
schema mismatch, malformed JSON, missing-file = empty dict, atomic save
(temp file cleanup), and the shipped seed file.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import parts_catalog
from parts_catalog import (
    CATALOG_SCHEMA,
    DEFAULT_FOV_NM,
    CatalogError,
    ChipSpec,
    PartSpec,
    default_catalog_path,
    load_catalog,
    save_catalog,
)


# ── ChipSpec ─────────────────────────────────────────────────────────────────

class TestChipSpec:
    def test_defaults_match_design(self) -> None:
        c = ChipSpec()
        assert c.chip_x_um == 0.0
        assert c.fov_w_nm == DEFAULT_FOV_NM
        assert c.fov_h_nm == DEFAULT_FOV_NM
        assert c.nm_per_px is None
        assert c.notes == ""

    def test_chip_corner_nm_matches_legacy_formula(self) -> None:
        # The legacy CoordinateSetupPanel formula:
        # chip_corner = (DieX - GDS_off) * 1000  (µm → nm)
        c = ChipSpec(chip_x_um=12.5, chip_y_um=-3.0,
                     gds_off_x_um=0.5, gds_off_y_um=0.0)
        cx, cy = c.chip_corner_nm()
        assert cx == pytest.approx((12.5 - 0.5) * 1e3)
        assert cy == pytest.approx((-3.0 - 0.0) * 1e3)

    def test_to_dict_round_trip(self) -> None:
        c = ChipSpec(chip_x_um=1.5, fov_w_nm=2000.0,
                     nm_per_px=1.25, notes="hi")
        again = ChipSpec.from_dict(c.to_dict())
        assert again == c

    def test_from_dict_partial(self) -> None:
        c = ChipSpec.from_dict({"chip_x_um": 5.0})
        assert c.chip_x_um == 5.0
        # Everything else falls back to defaults.
        assert c.chip_y_um == 0.0
        assert c.fov_w_nm == DEFAULT_FOV_NM
        assert c.nm_per_px is None

    def test_from_dict_ignores_unknown_keys(self) -> None:
        # A future schema bump must not crash an older loader.
        c = ChipSpec.from_dict({"chip_x_um": 1.0, "future_field": 42})
        assert c.chip_x_um == 1.0

    def test_from_dict_rejects_non_dict(self) -> None:
        with pytest.raises(CatalogError):
            ChipSpec.from_dict([1, 2, 3])  # type: ignore[arg-type]

    def test_from_dict_rejects_non_numeric(self) -> None:
        with pytest.raises(CatalogError):
            ChipSpec.from_dict({"chip_x_um": "not-a-number"})

    def test_from_dict_rejects_bad_nm_per_px(self) -> None:
        with pytest.raises(CatalogError):
            ChipSpec.from_dict({"nm_per_px": "oops"})

    def test_from_dict_accepts_null_nm_per_px(self) -> None:
        c = ChipSpec.from_dict({"nm_per_px": None})
        assert c.nm_per_px is None

    def test_from_dict_coerces_numeric_strings(self) -> None:
        # Hand-edited JSON may use quoted numbers; tolerate them.
        c = ChipSpec.from_dict({"chip_x_um": "3.5", "nm_per_px": "1.0"})
        assert c.chip_x_um == 3.5
        assert c.nm_per_px == 1.0


# ── PartSpec ─────────────────────────────────────────────────────────────────

class TestPartSpec:
    def test_to_dict_round_trip(self) -> None:
        p = PartSpec(
            description="demo",
            chips={"C1": ChipSpec(chip_x_um=1.0),
                   "C2": ChipSpec(chip_x_um=2.0)},
        )
        again = PartSpec.from_dict(p.to_dict())
        assert again == p

    def test_from_dict_missing_chips(self) -> None:
        # Treat an absent "chips" key as an empty chip set, not an error —
        # a PART can exist before any CHIP is keyed in.
        p = PartSpec.from_dict({"description": "empty"})
        assert p.description == "empty"
        assert p.chips == {}

    def test_from_dict_rejects_non_dict_chips(self) -> None:
        with pytest.raises(CatalogError):
            PartSpec.from_dict({"chips": [1, 2, 3]})

    def test_from_dict_rejects_non_dict(self) -> None:
        with pytest.raises(CatalogError):
            PartSpec.from_dict("not-an-object")  # type: ignore[arg-type]


# ── load_catalog / save_catalog ──────────────────────────────────────────────

def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestLoadSaveCatalog:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        # A fresh checkout without the seed must not raise — UI shows an
        # empty dropdown instead.
        result = load_catalog(tmp_path / "does-not-exist.json")
        assert result == {}

    def test_round_trip(self, tmp_path: Path) -> None:
        p = tmp_path / "parts.json"
        catalog = {
            "TMVG10": PartSpec(
                description="test part",
                chips={
                    "C1": ChipSpec(chip_x_um=1.0, chip_y_um=2.0,
                                   fov_w_nm=2500.0),
                    "C2": ChipSpec(chip_x_um=3.0, nm_per_px=1.25),
                },
            ),
        }
        save_catalog(p, catalog)
        loaded = load_catalog(p)
        assert loaded == catalog

    def test_schema_mismatch_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "parts.json"
        _write_json(p, {"schema": "wrong-schema", "parts": {}})
        with pytest.raises(CatalogError, match="schema mismatch"):
            load_catalog(p)

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "parts.json"
        p.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(CatalogError, match="could not read"):
            load_catalog(p)

    def test_non_object_root_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "parts.json"
        _write_json(p, [1, 2, 3])
        with pytest.raises(CatalogError, match="root must be"):
            load_catalog(p)

    def test_non_object_parts_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "parts.json"
        _write_json(p, {"schema": CATALOG_SCHEMA, "parts": "nope"})
        with pytest.raises(CatalogError, match="'parts'"):
            load_catalog(p)

    def test_atomic_save_no_tempfile_left(self, tmp_path: Path) -> None:
        p = tmp_path / "parts.json"
        save_catalog(p, {"X": PartSpec(chips={"C1": ChipSpec()})})
        leftovers = [f for f in tmp_path.iterdir() if f.suffix == ".tmp"]
        assert leftovers == [], f"tempfile leaked: {leftovers}"
        assert p.exists()

    def test_save_creates_parent_dir(self, tmp_path: Path) -> None:
        p = tmp_path / "new" / "sub" / "parts.json"
        save_catalog(p, {})
        assert p.exists()

    def test_save_overwrites_existing(self, tmp_path: Path) -> None:
        p = tmp_path / "parts.json"
        save_catalog(p, {"A": PartSpec()})
        save_catalog(p, {"B": PartSpec(description="replaced")})
        loaded = load_catalog(p)
        assert set(loaded.keys()) == {"B"}
        assert loaded["B"].description == "replaced"

    def test_saved_json_is_valid_utf8(self, tmp_path: Path) -> None:
        # ensure_ascii=False is on so CJK notes survive; verify.
        p = tmp_path / "parts.json"
        save_catalog(p, {"X": PartSpec(
            description="中文 description",
            chips={"C1": ChipSpec(notes="備註")})})
        text = p.read_text(encoding="utf-8")
        assert "中文" in text
        assert "備註" in text


# ── default_catalog_path + shipped seed ──────────────────────────────────────

class TestSeedFile:
    def test_default_path_under_glas_data(self) -> None:
        p = default_catalog_path()
        assert p.name == "parts.json"
        assert p.parent.name == "data"
        # glas/data/parts.json — sibling of glas/core/
        assert p.parent.parent.name == "glas"

    def test_shipped_seed_loads_cleanly(self) -> None:
        """The seed file shipped in the repo must round-trip — a broken
        seed would brick a fresh install."""
        p = default_catalog_path()
        if not p.exists():
            pytest.skip("no seed file shipped (fresh checkout)")
        catalog = load_catalog(p)
        assert isinstance(catalog, dict)
        # Sanity-check the placeholder content the plan promises.
        assert "EXAMPLE_PART" in catalog
        ep = catalog["EXAMPLE_PART"]
        assert "C1" in ep.chips
        c1 = ep.chips["C1"]
        # Default FOV in the seed must match the design constant — if a
        # future seed bumps it, this assert flags the spec drift.
        assert c1.fov_w_nm == DEFAULT_FOV_NM
        assert c1.fov_h_nm == DEFAULT_FOV_NM
