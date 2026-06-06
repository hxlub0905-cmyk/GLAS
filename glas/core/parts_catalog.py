"""PART/CHIP catalog for GLAS (F21 M1).

A repo-shipped JSON catalog mapping fab product PART codes (e.g. ``TMVG10``)
to one or more CHIPs, each carrying the constants the alignment workflow
needs: chip-corner offset, chip size, GDS-default offset, default FOV, and
default nm/pixel. The catalog replaces the manual ``Coordinate Setup`` form
that previously required users to copy RFL columns by hand.

Design rules (Q&A in ``docs/plans/F21-part-chip-catalog.md``):

* The canonical file is ``glas/data/parts.json`` shipped with the repo
  (Q1 = A). User-local overrides are not supported in M1.
* OASIS file paths are NOT stored — PART/CHIP and the .oas file are
  decoupled (Q2). The catalog only supplies coordinates / FOV / scale.
* Unknown CHIPs are not selectable in the UI (Q4 = A); editing is gated
  behind developer mode (M4).
* FOV is a per-CHIP default in nm; the UI can override per-session
  without writing back (Q5 = B). Default is ``DEFAULT_FOV_NM`` per axis.
* ``nm_per_px`` may be ``None``, meaning "auto = FOV / image px".

The module is intentionally Qt-free so it can be unit-tested headlessly
and reused outside the app (CLAUDE.md §6).
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


CATALOG_SCHEMA = "glas-parts-v1"

# Default per-axis FOV when a CHIP record omits the field. Matches a typical
# SEM acquisition window; the UI lets advanced users override per session.
DEFAULT_FOV_NM = 1500.0


class CatalogError(ValueError):
    """Raised when a catalog file is unreadable or schema-invalid."""


@dataclass
class ChipSpec:
    """Coordinate constants for one CHIP within a PART.

    Units mirror the existing Coordinate Setup widget so the migration is
    1-to-1: chip offsets / size in µm (lower-left origin), FOV in nm,
    ``nm_per_px`` in nm/pixel (``None`` = let the app compute it from the
    image dimensions).
    """

    chip_x_um: float = 0.0
    chip_y_um: float = 0.0
    chip_w_um: float = 0.0
    chip_h_um: float = 0.0
    gds_off_x_um: float = 0.0
    gds_off_y_um: float = 0.0
    fov_w_nm: float = DEFAULT_FOV_NM
    fov_h_nm: float = DEFAULT_FOV_NM
    nm_per_px: Optional[float] = None
    notes: str = ""

    def chip_corner_nm(self) -> tuple[float, float]:
        """Compute the chip-corner offset (nm) fed to ``klarf_to_gds`` —
        same formula as ``CoordinateSetupPanel._chip_corner_nm`` (@3472).
        """
        return ((self.chip_x_um - self.gds_off_x_um) * 1e3,
                (self.chip_y_um - self.gds_off_y_um) * 1e3)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ChipSpec":
        """Build from a JSON dict, accepting partial records (missing keys
        fall back to dataclass defaults). Unknown keys are ignored so a
        future schema bump doesn't crash old loaders."""
        if not isinstance(data, dict):
            raise CatalogError(
                f"chip record must be a JSON object, got {type(data).__name__}")
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in data.items() if k in known}
        # Coerce nm_per_px: JSON ``null`` -> Python ``None``; numeric strings
        # may sneak in from a hand-edited catalog.
        if "nm_per_px" in kwargs and kwargs["nm_per_px"] is not None:
            try:
                kwargs["nm_per_px"] = float(kwargs["nm_per_px"])
            except (TypeError, ValueError) as exc:
                raise CatalogError(
                    f"nm_per_px must be a number or null: {exc}") from exc
        for key in ("chip_x_um", "chip_y_um", "chip_w_um", "chip_h_um",
                    "gds_off_x_um", "gds_off_y_um", "fov_w_nm", "fov_h_nm"):
            if key in kwargs:
                try:
                    kwargs[key] = float(kwargs[key])
                except (TypeError, ValueError) as exc:
                    raise CatalogError(
                        f"{key} must be numeric: {exc}") from exc
        return cls(**kwargs)


@dataclass
class PartSpec:
    """A PART (product code) and its CHIPs."""

    description: str = ""
    chips: dict[str, ChipSpec] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "chips": {cid: c.to_dict() for cid, c in self.chips.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PartSpec":
        if not isinstance(data, dict):
            raise CatalogError(
                f"part record must be a JSON object, got {type(data).__name__}")
        chips_raw = data.get("chips", {})
        if not isinstance(chips_raw, dict):
            raise CatalogError("'chips' must be a JSON object")
        chips = {str(cid): ChipSpec.from_dict(cs)
                 for cid, cs in chips_raw.items()}
        return cls(description=str(data.get("description", "")), chips=chips)


def default_catalog_path() -> Path:
    """Canonical catalog location bundled with the repo
    (``glas/data/parts.json``). Resolves regardless of cwd."""
    return Path(__file__).resolve().parent.parent / "data" / "parts.json"


def load_catalog(path: str | Path | None = None) -> dict[str, PartSpec]:
    """Load the catalog from disk. Returns an empty dict if the file is
    missing (a fresh checkout that hasn't shipped a seed yet); raises
    :class:`CatalogError` for unreadable / malformed files so the UI can
    surface a clear error instead of silently dropping data.
    """
    p = Path(path) if path is not None else default_catalog_path()
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"could not read catalog {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CatalogError(f"catalog root must be a JSON object, got "
                           f"{type(raw).__name__}")
    schema = raw.get("schema")
    if schema != CATALOG_SCHEMA:
        raise CatalogError(
            f"catalog schema mismatch: expected {CATALOG_SCHEMA!r}, "
            f"got {schema!r}")
    parts_raw = raw.get("parts", {})
    if not isinstance(parts_raw, dict):
        raise CatalogError("'parts' must be a JSON object")
    return {str(pid): PartSpec.from_dict(ps)
            for pid, ps in parts_raw.items()}


def save_catalog(path: str | Path,
                 parts: dict[str, PartSpec]) -> None:
    """Atomically write the catalog: tempfile in the same directory then
    ``os.replace`` so a crash mid-write can't truncate the live file
    (CLAUDE.md §7 cache-rule analogue).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": CATALOG_SCHEMA,
        "parts": {pid: ps.to_dict() for pid, ps in parts.items()},
    }
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
