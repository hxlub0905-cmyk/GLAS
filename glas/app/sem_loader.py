"""SEM image loading for the GDS align tool (F2 M3).

Two media (plan M3 / Q-answer "KLARF + Folder 都做"):

* **Load KLARF** -- parse a KLARF defect list with the bundled
  ``klarf_parser`` (glas/core) and pull out, per defect, its image
  filename + ``XREL`` / ``YREL`` (die-corner nm coordinates). These
  coordinates drive the auto-jump: ``gds_fov.klarf_to_gds`` converts
  them to chip-corner GDS nm and the canvas centres there.
* **Load Folder** -- scan a directory for image files. No coordinates
  are available, so these images can be browsed but won't auto-jump
  until the user keys an offset (or M4 fine-aligns).

Both return a list of :class:`SemImage`; the GUI doesn't care which
medium produced it.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Field DiePitch 2 {23376636, 32874750}  (nm). Surfaced for the M3
# Coordinate Setup panel's die-size auto-fill (plan Q15).
_DIE_PITCH_RE = re.compile(
    r"DiePitch\s+\d+\s*\{\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*\}")

# klarf_parser lives in glas/core; sem_loader is in glas/app.
_CORE = Path(__file__).resolve().parent.parent / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

_IMAGE_EXTS = {".png", ".tif", ".tiff", ".jpg", ".jpeg", ".bmp"}


@dataclass
class SemImage:
    """One SEM image in the dataset.

    ``xrel`` / ``yrel`` are KLARF die-corner coordinates in nm, or
    ``None`` for folder-loaded images that carry no coordinates.
    ``file_path`` is the resolved path (may not exist on disk if the
    KLARF references images that weren't copied alongside it).
    """
    image_id: str
    filename: str
    file_path: Optional[Path]
    xrel: Optional[float] = None
    yrel: Optional[float] = None

    @property
    def has_coords(self) -> bool:
        return self.xrel is not None and self.yrel is not None

    @property
    def exists(self) -> bool:
        return self.file_path is not None and self.file_path.exists()


def _col_lookup(columns: list[str]) -> dict[str, int]:
    """Map upper-cased column name -> index, for case-insensitive
    XREL/YREL/DEFECTID access."""
    return {c.upper(): i for i, c in enumerate(columns)}


def _to_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_klarf(path: str | Path) -> list[SemImage]:
    """Parse a KLARF file into a list of :class:`SemImage`.

    Image filenames come from the parser's ``_image_filename`` field and
    are resolved relative to the KLARF file's directory. Defects without
    an image are skipped (nothing to display). XREL/YREL are read
    case-insensitively from the defect columns.
    """
    from klarf_parser import KlarfParser

    p = Path(path)
    parsed = KlarfParser().parse(p)
    columns = parsed.get("defect_columns", []) or []
    cols = _col_lookup(columns)
    out: list[SemImage] = []
    base_dir = p.parent
    for i, defect in enumerate(parsed.get("defects", []) or []):
        fname = defect.get("_image_filename", "") or ""
        if not fname:
            continue
        # XREL/YREL stored under their column name; values are strings.
        xrel = _to_float(defect.get(columns[cols["XREL"]])) if "XREL" in cols else None
        yrel = _to_float(defect.get(columns[cols["YREL"]])) if "YREL" in cols else None
        if "DEFECTID" in cols:
            image_id = str(defect.get(columns[cols["DEFECTID"]], i + 1))
        else:
            image_id = str(i + 1)
        out.append(SemImage(
            image_id=image_id,
            filename=fname,
            file_path=base_dir / fname,
            xrel=xrel,
            yrel=yrel,
        ))
    return out


def read_die_pitch_nm(path: str | Path) -> Optional[tuple[float, float]]:
    """Return the KLARF ``DiePitch`` as ``(x_nm, y_nm)``, or ``None`` if
    the field is absent. DiePitch lives in the LotRecord (not the defect
    rows) and the parser doesn't surface it structurally, so this scans
    the raw text. Used to auto-fill the Coordinate Setup die size."""
    try:
        text = Path(path).read_text(errors="ignore")
    except OSError:
        return None
    m = _DIE_PITCH_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None


def load_folder(path: str | Path) -> list[SemImage]:
    """Scan a directory (non-recursive) for image files, sorted by name.
    Folder images carry no coordinates (``xrel`` / ``yrel`` = None)."""
    d = Path(path)
    if not d.is_dir():
        return []
    out: list[SemImage] = []
    for f in sorted(d.iterdir()):
        if f.is_file() and f.suffix.lower() in _IMAGE_EXTS:
            out.append(SemImage(
                image_id=f.stem,
                filename=f.name,
                file_path=f,
            ))
    return out
