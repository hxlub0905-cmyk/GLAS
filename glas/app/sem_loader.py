"""SEM image loading for the GDS align tool (F2 M3, F31).

Three media; all return a list of :class:`SemImage` and the GUI doesn't care
which one produced it:

* **Load KLARF (rSEM)** -- parse a KLARF defect list with the bundled
  ``klarf_parser`` (glas/core) and pull out, per defect, its image
  filename + ``XREL`` / ``YREL`` (die-corner nm coordinates). These
  coordinates drive the auto-jump: ``gds_fov.klarf_to_gds`` converts
  them to chip-corner GDS nm and the canvas centres there.
* **Load KLARF (EBI patch, F31)** -- the same call, for KLARFs whose patches
  live in ONE multi-page TIFF named at lot level (``TiffFileName``) and
  addressed per defect through ``IMAGECOUNT`` / ``IMAGELIST`` columns. Each
  defect gets its own TIFF pages (typically two: page 1 test, page 2 ref).
  These files have no per-defect filename, and 1.2 ones are not parseable by
  ``klarf_parser`` at all, so this path reads through the vendored read-only
  ``klarf_doc`` instead; see :func:`_load_klarf_ebi`.
* **Load Folder** -- scan a directory for image files. No coordinates
  are available, so these images can be browsed but won't auto-jump
  until the user keys a coordinate for them (F32 CoordinateKeyPanel).

**Images without a file are kept (F32).** A KLARF that carries defect
coordinates but no review images -- or whose images were never copied next
to it -- is still a defect list worth navigating: GLAS jumps to each defect's
GDS position and shows the layout on a blank canvas. So a defect with
coordinates survives here with ``file_path=None`` (``exists`` False); only a
defect with neither an image nor coordinates is dropped, because nothing can
be done with it.

**Coordinates leave here in nm, always.** KLARF 1.2 stores XREL/YREL in µm and
1.8 in nm; the conversion happens at load time because ``gds_fov.klarf_to_gds``
takes nm unconditionally and is frozen (CLAUDE.md §7 -- its sign/direction was
verified against real layout by the user). A 1.8 file converts by ×1, so the
existing path is unchanged.
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

# KLARF 1.2 spells it as a bare statement in µm: ``DiePitch 1000.0 1200.0;``
# (F31). Anchored to the line start so it can't match the 1.8 form's tail.
_DIE_PITCH_12_RE = re.compile(
    r"(?m)^[ \t]*DiePitch[ \t]+([-\d.eE+]+)[ \t]+([-\d.eE+]+)[ \t]*;")

# klarf_parser lives in glas/core; sem_loader is in glas/app.
_CORE = Path(__file__).resolve().parent.parent / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

_IMAGE_EXTS = {".png", ".tif", ".tiff", ".jpg", ".jpeg", ".bmp"}


@dataclass
class SemImage:
    """One SEM image in the dataset.

    ``xrel`` / ``yrel`` are KLARF die-corner coordinates **in nm** (converted at
    load time, see the module docstring), or ``None`` for folder-loaded images
    that carry no coordinates. ``file_path`` is the resolved path (may not exist
    on disk if the KLARF references images that weren't copied alongside it).

    F31 fields:

    ``pages``
        Every TIFF page belonging to this defect, 0-based, in KLARF order
        (typically ``(test, ref)``). Empty for ordinary single-image files --
        that is what "not a multi-page dataset" looks like.
    ``page``
        The page currently chosen for alignment / export, or ``None`` to read
        the file as a plain image (the pre-F31 behaviour). Derived from
        ``pages`` via :meth:`page_for_ordinal`; recompute it rather than
        assuming it, since the user can change which page aligns without
        reloading the KLARF.
    ``id_source``
        Where ``image_id`` came from, so a downstream consumer can tell whether
        it may join on KLARF ``DEFECTID``: ``"klarf-defectid"``,
        ``"filename-stem"``, ``"row-index"`` (a KLARF with no DEFECTID column
        -- ids are positional and join on nothing), or ``""`` when unknown.
    ``coord_source`` (F32)
        Where ``xrel`` / ``yrel`` came from: ``"klarf"`` (read from the
        defect list), ``"manual-gds"`` / ``"manual-klarf"`` (keyed by the
        operator, in GDS µm or KLARF die-corner nm respectively), or ``""``
        when there are no coordinates. Drives the list badge; an exporter can
        use it to tell a keyed position from a measured one.
    """
    image_id: str
    filename: str
    file_path: Optional[Path]
    xrel: Optional[float] = None
    yrel: Optional[float] = None
    pages: tuple = ()
    page: Optional[int] = None
    id_source: str = ""
    coord_source: str = ""

    @property
    def has_coords(self) -> bool:
        return self.xrel is not None and self.yrel is not None

    @property
    def exists(self) -> bool:
        return self.file_path is not None and self.file_path.exists()

    def page_for_ordinal(self, ordinal: int) -> Optional[int]:
        """The 0-based TIFF page for the ``ordinal``-th image of this defect
        (1-based: 1 = test, 2 = ref), or ``None`` for a single-image file.

        A defect with fewer pages than asked for falls back to its **last**
        page rather than failing: the alternative is dropping that defect from
        an export for a reason the operator never sees. Callers that need to
        report the fallback compare the result against ``pages[ordinal - 1]``.
        """
        if not self.pages:
            return None
        i = max(1, int(ordinal)) - 1
        return self.pages[i] if i < len(self.pages) else self.pages[-1]


def _col_lookup(columns: list[str]) -> dict[str, int]:
    """Map upper-cased column name -> index, for case-insensitive
    XREL/YREL/DEFECTID access."""
    return {c.upper(): i for i, c in enumerate(columns)}


def _to_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


#: Which image of a defect drives alignment, 1-based (F31 Q4). Page 1 is the
#: test frame and page 2 the reference; ref wins by default because it is the
#: same location on a neighbouring die and so carries no defect to confuse the
#: template match. Overridden per batch from the fine-align settings.
DEFAULT_ALIGN_PAGE_ORDINAL = 2


def _load_klarf_ebi(p: Path, ordinal: int, notes: list) -> Optional[list]:
    """Load an EBI-patch KLARF, or return ``None`` if this isn't one.

    "Is one" means: the vendored :mod:`klarf_doc` can read it, a patch TIFF is
    findable (``TiffFileName``, or a same-stem ``.tif`` beside the KLARF), and
    its defect→page mapping resolves. Anything else -- including any error
    reading the TIFF -- returns ``None`` so :func:`load_klarf` falls through to
    the unchanged rSEM path. This ordering matters: rSEM KLARFs must keep
    behaving exactly as before, so the new path only claims files the old one
    cannot serve.

    Page numbering is decided by ``klarf_doc.defect_image_map``, which prefers
    the IMAGELIST page ids (auto-detecting 0- vs 1-based against the real page
    count) and otherwise assigns pages sequentially in defect order. Its
    ``notes`` explain which rule applied and are passed back to the caller --
    a mapping that silently guessed is the failure mode that mis-assigns a
    whole lot.
    """
    try:
        import klarf_doc
        import tiff_index
    except ImportError:                     # pragma: no cover - both are vendored
        return None
    try:
        doc = klarf_doc.load(str(p))
    except (OSError, ValueError, UnicodeError):
        return None
    tiff = doc.tiff_path()
    if not tiff:
        return None
    try:
        n_pages = tiff_index.n_pages(tiff)
    except (OSError, ValueError):
        notes.append(f"Patch TIFF could not be indexed: {tiff}")
        return None
    imap = doc.defect_image_map(n_pages)
    if imap["mode"] is None:
        return None
    notes.extend(imap["notes"])

    to_nm = float(doc.unit_info()["to_nm"])
    i_id = doc.col_index("DEFECTID")
    i_x, i_y = doc.col_index("XREL"), doc.col_index("YREL")
    id_source = "klarf-defectid" if i_id >= 0 else "row-index"
    tiff_path = Path(tiff)
    out: list[SemImage] = []
    n_skipped = 0
    n_no_image = 0
    for k, (row, pages) in enumerate(zip(doc.defects, imap["pages"])):
        image_id = (str(row[i_id]) if 0 <= i_id < len(row) else str(k + 1))
        xrel = _to_float(row[i_x]) if 0 <= i_x < len(row) else None
        yrel = _to_float(row[i_y]) if 0 <= i_y < len(row) else None
        if not pages and (xrel is None or yrel is None):
            # No patch (IMAGECOUNT 0) *and* no coordinates: nothing to show,
            # nothing to jump to.
            n_skipped += 1
            continue
        img = SemImage(
            image_id=image_id,
            # F32: a defect with coordinates but no patch keeps its place in
            # the list (navigable, drawn on a blank canvas); it has no file.
            filename=tiff_path.name if pages else "",
            file_path=tiff_path if pages else None,
            # µm (1.2) → nm; 1.8 multiplies by 1 and is bit-for-bit unchanged.
            xrel=None if xrel is None else xrel * to_nm,
            yrel=None if yrel is None else yrel * to_nm,
            pages=tuple(int(pg) for pg in pages),
            id_source=id_source,
            coord_source="klarf" if (xrel is not None and yrel is not None) else "",
        )
        img.page = img.page_for_ordinal(ordinal)
        out.append(img)
        if not pages:
            n_no_image += 1
    if n_skipped:
        notes.append(f"{n_skipped} defect(s) carry no patch image and no "
                     "coordinates and were skipped.")
    if n_no_image:
        notes.append(f"{n_no_image} defect(s) carry no patch image; kept for "
                     "navigation (coordinates only).")
    if not out:
        return None
    return out


def _load_klarf_coords_only(p: Path, notes: list) -> Optional[list]:
    """F32 fallback: a KLARF the rSEM parser yields nothing from, but that
    :mod:`klarf_doc` can read -- typically a 1.2 flat file with no patch TIFF
    beside it (``klarf_parser`` has no 1.2 ``DefectRecordSpec`` support, see
    F31). Every defect with XREL/YREL becomes an image-less :class:`SemImage`
    so the operator can still step through the defects on the layout.

    Only called when the rSEM path returned **no** images: a KLARF the old path
    can serve must keep loading exactly as before, filenames included.
    """
    try:
        import klarf_doc
    except ImportError:                     # pragma: no cover - vendored
        return None
    try:
        doc = klarf_doc.load(str(p))
    except (OSError, ValueError, UnicodeError):
        return None
    i_x, i_y = doc.col_index("XREL"), doc.col_index("YREL")
    if i_x < 0 or i_y < 0 or not doc.defects:
        return None
    to_nm = float(doc.unit_info()["to_nm"])
    i_id = doc.col_index("DEFECTID")
    id_source = "klarf-defectid" if i_id >= 0 else "row-index"
    out: list[SemImage] = []
    for k, row in enumerate(doc.defects):
        xrel = _to_float(row[i_x]) if i_x < len(row) else None
        yrel = _to_float(row[i_y]) if i_y < len(row) else None
        if xrel is None or yrel is None:
            continue
        out.append(SemImage(
            image_id=str(row[i_id]) if 0 <= i_id < len(row) else str(k + 1),
            filename="",
            file_path=None,
            xrel=xrel * to_nm,
            yrel=yrel * to_nm,
            id_source=id_source,
            coord_source="klarf",
        ))
    if not out:
        return None
    notes.append(f"No image files referenced: {len(out)} defect(s) loaded "
                 "as coordinates only (KLARF %s)." % doc.version)
    return out


def load_klarf(path: str | Path, *,
               align_page_ordinal: int = DEFAULT_ALIGN_PAGE_ORDINAL,
               notes: Optional[list] = None) -> list[SemImage]:
    """Parse a KLARF file into a list of :class:`SemImage`.

    Tries the EBI-patch shape first (one multi-page TIFF for the whole lot, see
    :func:`_load_klarf_ebi`); if the file isn't that shape, falls through to the
    original rSEM path below, unchanged: image filenames come from the parser's
    ``_image_filename`` field and are resolved relative to the KLARF file's
    directory, defects with neither an image nor coordinates are skipped
    (nothing to display, nothing to jump to -- F32 keeps the ones that have
    coordinates, image-less), and XREL/YREL are read case-insensitively from
    the defect columns. When that path yields nothing at all, a last resort
    reads coordinates only through ``klarf_doc`` (:func:`_load_klarf_coords_only`).

    ``align_page_ordinal`` picks which of a defect's images alignment uses
    (1-based; only meaningful for the EBI path). Pass ``notes`` a list to
    collect human-readable remarks about how the pages were mapped.
    """
    from klarf_parser import KlarfParser

    p = Path(path)
    notes = notes if notes is not None else []
    ebi = _load_klarf_ebi(p, align_page_ordinal, notes)
    if ebi is not None:
        return ebi
    parsed = KlarfParser().parse(p)
    columns = parsed.get("defect_columns", []) or []
    cols = _col_lookup(columns)
    out: list[SemImage] = []
    base_dir = p.parent
    for i, defect in enumerate(parsed.get("defects", []) or []):
        fname = defect.get("_image_filename", "") or ""
        # XREL/YREL stored under their column name; values are strings.
        xrel = _to_float(defect.get(columns[cols["XREL"]])) if "XREL" in cols else None
        yrel = _to_float(defect.get(columns[cols["YREL"]])) if "YREL" in cols else None
        has_coords = xrel is not None and yrel is not None
        if not fname and not has_coords:
            continue
        if "DEFECTID" in cols:
            image_id = str(defect.get(columns[cols["DEFECTID"]], i + 1))
            id_source = "klarf-defectid"
        else:
            image_id = str(i + 1)
            id_source = "row-index"
        out.append(SemImage(
            image_id=image_id,
            filename=fname,
            file_path=(base_dir / fname) if fname else None,
            xrel=xrel,
            yrel=yrel,
            id_source=id_source,
            coord_source="klarf" if has_coords else "",
        ))
    if out:
        return out
    return _load_klarf_coords_only(p, notes) or []


def read_die_pitch_nm(path: str | Path) -> Optional[tuple[float, float]]:
    """Return the KLARF ``DiePitch`` as ``(x_nm, y_nm)``, or ``None`` if
    the field is absent. DiePitch lives in the LotRecord (not the defect
    rows) and the parser doesn't surface it structurally, so this scans
    the raw text. Used to auto-fill the Coordinate Setup die size.

    Both spellings are accepted: 1.8's ``Field DiePitch 2 {x, y}`` (nm, returned
    as-is) and 1.2's ``DiePitch x y;`` (µm, **scaled to nm** so the caller gets
    one unit regardless of source — same reason XREL/YREL are converted at load
    time, see the module docstring)."""
    try:
        text = Path(path).read_text(errors="ignore")
    except OSError:
        return None
    m = _DIE_PITCH_RE.search(text)
    scale = 1.0
    if not m:
        m = _DIE_PITCH_12_RE.search(text)
        scale = 1000.0                      # 1.2 stores µm
    if not m:
        return None
    try:
        return float(m.group(1)) * scale, float(m.group(2)) * scale
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
                id_source="filename-stem",
            ))
    return out
