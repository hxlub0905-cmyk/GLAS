"""GLAS — GDS-Layout Alignment for SEM. Load OASIS, browse / compose layers, align to SEM.

This is the main PyQt6 app for GLAS. It was originally developed inside the MMH
project as tools/gds_align_tool.py (plan F2); see docs/plans/F2-gds-align-tool.md
for the full design history. The no-Qt engine lives in glas/core; this module
and its GUI helpers live in glas/app.

Run:  python main.py

The tool is **OASIS-only**: a built-in streaming parser (oasis_streamer) plus
a random-access ROI reader (oasis_random) load only the geometry around each
SEM defect, with no klayout / gdstk dependency. (The earlier full-load engine
and its klayout / gdstk backends — and .gds support — were removed once the
random-access ROI path made them unnecessary.)

Capabilities:

    * Open OASIS (ROI): scan LAYERNAME via the streamer (sub-second even on
      300 MB+ Calibre D2DB), pick layers + root cell, then load geometry on
      demand around each clicked SEM defect.
    * Layer panel: per-layer visibility / colour / opacity + Boolean
      expression layers (M2). rasterize_layer() builds masks for Boolean ops
      and template matching.
    * SEM dual-pane (M3) with auto coordinate jump; overlay drag align +
      origin δ (M4a); POI template + cv2.matchTemplate fine align (M4b).
    * CSV / JSON alignment export (M5).

The single-file shape is deliberate, mirroring tools/histogram_analyzer.py.

M5 alignment export schema (for a future Recipe to anchor its ROI) — one row
per SEM image, columns in ``ALIGNMENT_COLUMNS``::

    image_id      ↔ MeasurementRecord.image_id (the join key)
    klarf_path    source KLARF defect list ("" for folder-loaded images)
    gds_path      source OASIS / GDS file
    poi_layer     POI layer label used for fine align ("" if none)
    coarse_dx_nm  FOV-centre GDS x = klarf_to_gds + fine-tune + origin δ
    coarse_dy_nm  FOV-centre GDS y (blank for images without coordinates)
    fine_dx_nm    per-image template-match correction x (blank if not run)
    fine_dy_nm    per-image template-match correction y
    score         TM_CCOEFF_NORMED match score (blank if not run)
    nm_per_px     overlay scale (nm per native SEM pixel)

The aligned GDS position is ``coarse + fine``. JSON additionally carries
``synthetic_layers`` (the Boolean expression lineage). Optional synthetic-layer
``.gds`` export is deferred — expression layers are FOV-local, not global.
"""
from __future__ import annotations

import csv
import json
import multiprocessing as mp
import os
import re
import sys
import threading
import traceback
from concurrent.futures import CancelledError, ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from PyQt6.QtCore import Qt, QObject, QPointF, QRect, QRectF, QSettings, QSize, QThread, QTimer, QElapsedTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction, QBrush, QColor, QFontMetrics, QIcon, QImage, QKeySequence,
    QPainter, QPen, QPixmap,
    QPolygonF, QMouseEvent, QShortcut, QWheelEvent,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QColorDialog,
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QStatusBar,
    QStyle,
    QStyledItemDelegate, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget, QWizard, QWizardPage,
)

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None  # rasterize_layer falls back to a pure-numpy scanline fill


def _screen_avail(margin: int = 80) -> tuple[int, int]:
    """Available screen (w, h) minus a margin, for sizing dialogs / the main
    window so a hard-coded minimum can never exceed the display (F3 M1)."""
    app = QApplication.instance()
    if app is not None:
        scr = app.primaryScreen()
        if scr is not None:
            g = scr.availableGeometry()
            return (max(320, g.width() - margin), max(240, g.height() - margin))
    return (10_000, 10_000)   # no screen yet → don't constrain


def _capped_min_width(desired: int) -> int:
    """Clamp a desired minimum dialog width to the available screen width so a
    small display isn't forced wider than it is (F3 M1)."""
    return min(desired, _screen_avail()[0])



# ── Theme: pull QSS from main app when available; fallback otherwise ─────────


# GLAS package layout: this module lives in glas/app/; the no-Qt engine
# modules live in glas/core/. Put both dirs on sys.path so the flat sibling
# imports below resolve whether the tool is run as a script (python main.py)
# or imported by tests/conftest.
_HERE = Path(__file__).resolve().parent          # glas/app
_CORE = _HERE.parent / "core"                    # glas/core
for _p in (_CORE, _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# M2 engine modules (glas/core). gds_fov / gds_layer_cache need only numpy;
# gds_boolean guards its shapely import internally (functions raise
# BooleanExprError if shapely is missing), so importing it here is always safe.
import gds_fov          # noqa: E402
import gds_boolean      # noqa: E402
import gds_layer_cache  # noqa: E402
import sem_loader       # noqa: E402
import oasis_random     # noqa: E402
import devlog            # noqa: E402  (dev-mode coloured console tags)
import layout_export     # noqa: E402  (F9: OASIS export — shapely guarded inside)
import oasis_debug        # noqa: E402  (F10: OASIS diagnostics — Qt-free)
# F8: the Qt-free fine-align compute lives in glas/core/fine_align.py so a
# spawn-based ProcessPool worker can import it without pulling in PyQt6. Pull
# the functions back into this namespace so existing call sites and tests
# (which reference gds_align_tool.<fn>) keep working unchanged.
import fine_align       # noqa: E402
from fine_align import (  # noqa: E402,F401
    _fine_align_image, _fit_mask, _parabola_subpx, _walk_roi_polys,
    fine_align_one, make_template, poi_polys_for_roi,
    poi_polys_and_geometry_for_roi,
    rasterize_layer, render_composite_template, render_poi_template,
)
# F14: the Qt-free per-image image/mask export compute (and the moved
# overlay_outlines_on_sem / _safe_name helpers) live in glas/core so the export
# batch can run across a spawn-based ProcessPool, mirroring fine_align (F8).
import overlay_export     # noqa: E402
from overlay_export import (  # noqa: E402,F401
    overlay_outlines_on_sem, _safe_name, _draw_polyline_np,
)
# F21: PART/CHIP catalog replaces the hand-keyed Coordinate Setup form.
import parts_catalog     # noqa: E402
from parts_catalog import (  # noqa: E402
    CATALOG_SCHEMA, DEFAULT_FOV_NM, CatalogError, ChipSpec, PartSpec,
    default_catalog_path, load_catalog, save_catalog,
)

# Design-system styling / widgets / icons (glas/app). Soft-imported so the tool
# still launches if the GUI helpers are unavailable.
try:
    from styles import STYLE as _APP_QSS  # noqa: E402
except Exception:  # pragma: no cover
    _APP_QSS = None

try:
    from collapsible import CollapsibleSection  # noqa: E402
except Exception:  # pragma: no cover
    CollapsibleSection = None
try:
    from icons import qicon as _qicon  # noqa: E402
except Exception:  # pragma: no cover
    def _qicon(_name):
        from PyQt6.QtGui import QIcon
        return QIcon()


# App version. Surfaced in the status-bar brand chip (left edge) and the
# About dialog so a user reporting a bug can quote it at a glance.
GLAS_VERSION = "1.0.0"


_TK_BG_PAGE   = QColor("#f7f4ef")
_TK_BG_PANEL  = QColor("#fff8f2")
_TK_BG_INPUT  = QColor("#ffffff")
_TK_BORDER    = QColor("#e8d8c8")
_TK_BORDER_DK = QColor("#c8b89e")
_TK_TEXT_PRI  = QColor("#3f3428")
_TK_TEXT_SEC  = QColor("#7a6a5a")
_TK_TEXT_HINT = QColor("#8a7660")
_TK_ACCENT    = QColor("#f29f4b")
_TK_ACCENT_DK = QColor("#c97028")
_TK_GRID_FAINT = QColor(0, 0, 0, 26)
_TK_GRID_BOLD  = QColor(0, 0, 0, 64)
_TK_CANVAS_BG  = QColor("#fbf8f3")
# Semantic + structural tokens (M7.2): centralize colours/font-sizes that were
# previously hard-coded inline across the widgets. Values match src/gui/styles.py
# so the tool reads identically to the main app.
_TK_SUCCESS   = "#2e7d32"
_TK_DANGER    = "#b13a3a"
_TK_SECTION_HEAD = "#6b5a4a"
_TK_GUIDANCE_BG   = "#fdf3e3"
_TK_GUIDANCE_TEXT = "#8a6a3a"
_TK_GUIDANCE_BORDER = "#ecd9bf"
_TK_TOOLBAR_BG = "#fff7ee"

# Font-size scale (px), matching styles.py FS_* tokens.
_FS_MICRO, _FS_CAPTION, _FS_LABEL, _FS_BODY, _FS_TITLE = 10, 11, 12, 13, 14

# Primary-emphasis QSS for the "Load SEM…" menu button so it carries the same
# orange weight as the toolbar's "Open OASIS…" entry point.
_LOAD_SEM_BTN_QSS = (
    "QPushButton {"
    f"  background: {_TK_ACCENT.name()};"
    "  color: #ffffff;"
    "  font-weight: 600;"
    "  border: none;"
    "  border-radius: 4px;"
    "  padding: 3px 10px;"
    "}"
    f"QPushButton:hover {{ background: {_TK_ACCENT_DK.name()}; }}"
    "QPushButton::menu-indicator {"
    "  subcontrol-origin: padding;"
    "  subcontrol-position: right center;"
    "  right: 6px;"
    "}"
)

# Per-layer POI toggle (left LayerPanel). Unchecked: a visible outlined chip
# (the old 18×16 flat "P" was invisible against the panel — F3 M4). Checked:
# solid accent so the chosen POI layers stand out.
_POI_BTN_QSS = (
    "QToolButton {"
    "  background: #ffffff;"
    f"  color: {_TK_ACCENT_DK.name()};"
    f"  border: 1px solid {_TK_ACCENT.name()};"
    "  border-radius: 4px;"
    "  font-weight: 700;"
    "  font-size: 10px;"
    "  padding: 0 2px;"
    "}"
    f"QToolButton:hover {{ background: #fff0e0; }}"
    "QToolButton:checked {"
    f"  background: {_TK_ACCENT.name()};"
    "  color: #ffffff;"
    f"  border: 1px solid {_TK_ACCENT_DK.name()};"
    "}"
)


def _hint_qss(size: int = _FS_CAPTION, color: str = "#8a7660",
              pad: str = "") -> str:
    """QSS for a muted hint/caption label."""
    p = f" padding:{pad};" if pad else ""
    return f"color:{color}; font-size:{size}px;{p}"


def _result_qss(color: str, size: int = _FS_LABEL) -> str:
    """QSS for a coloured result label (success/danger/accent)."""
    return f"font-size:{size}px; color:{color};"


def _entry_label(entry) -> str:
    """Display label for a LayerEntry: ``NAME (L17/D0)`` when the OASIS file
    declares a LAYERNAME, else the bare ``L17/D0`` / ``[expr] name`` (F3)."""
    base = entry.key.label()
    name = getattr(entry, "display_name", "")
    return f"{name} ({base})" if (name and not entry.key.synthetic) else base


def _config_list(lst) -> None:
    """Make a QListWidget wrap long rows instead of growing a horizontal
    scrollbar / clipping text in the narrow side panels (M7 R1)."""
    lst.setWordWrap(True)
    lst.setTextElideMode(Qt.TextElideMode.ElideRight)
    lst.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)


# Default layer-color palette (cycled by index of first appearance).
_LAYER_PALETTE = [
    QColor("#e07a5f"), QColor("#3d8ec9"), QColor("#5a9461"), QColor("#d49a3a"),
    QColor("#8a5db5"), QColor("#3aa3b0"), QColor("#c54f8e"), QColor("#7a6a4a"),
    QColor("#b13a3a"), QColor("#3a6fb1"), QColor("#3a8b6f"), QColor("#a87b25"),
]


_FALLBACK_QSS = """
QWidget {
    background-color: #f7f4ef;
    color: #3f3428;
    font-family: 'Segoe UI', 'PingFang TC', 'Microsoft JhengHei', sans-serif;
    font-size: 13px;
}
QFrame#leftPanel { background: #fff7ee; border-right: 1px solid #e8d8c8; }
QLabel#panelTitle {
    color: #f29f4b; font-size: 10px; font-weight: 700;
    letter-spacing: 1.5px; padding: 8px 10px 4px 10px;
}
QPushButton {
    background: #fffdf9; color: #6f6254;
    border: 1px solid #c8b49e; border-radius: 6px;
    padding: 0 14px; min-height: 30px;
}
QPushButton:hover { background: #fff4e8; border-color: #b09e86; color: #3f3428; }
QPushButton[variant="primary"] {
    background: #f29f4b; color: #fff;
    border: 1px solid #d97d1e; font-weight: 600;
}
QPushButton[variant="primary"]:hover { background: #f6b56b; }
QListWidget { background: #f2ece4; border: none; }
QListWidget::item { padding: 4px 6px; border-radius: 3px; }
QListWidget::item:hover { background: #f6e8d8; }
QListWidget::item:selected { background: #f6c38c; color: #3f3428; }
QStatusBar {
    background: #f0e9e0; color: #8a7a6a;
    border-top: 1px solid #e8d8c8; font-size: 11px;
}
QSplitter::handle { background: #e8d8c8; }
QMenuBar { background: #f2ece4; border-bottom: 1px solid #e8d8c8; }
QMenuBar::item { padding: 5px 12px; color: #7c6d5b; }
QMenuBar::item:selected { background: #fffdf9; color: #3f3428; }
"""


# ── Data model ───────────────────────────────────────────────────────────────


@dataclass
class LayerKey:
    """Identifies a layer in the document.

    ``synthetic`` is reserved for M2 — synthesized layers will use
    ``layer = -1`` and a unique ``name`` (and ``synthetic = True``). Keep the
    dataclass extensible from M1 onwards so M2 can drop in without breaking
    LayerPanel ordering / lookup.
    """
    layer: int
    datatype: int
    name: str = ""           # blank for raw layers; populated for synthetic
    synthetic: bool = False

    def label(self) -> str:
        if self.synthetic:
            return f"[expr] {self.name}"
        return f"L{self.layer}/D{self.datatype}"

    def key(self) -> tuple[int, int, str]:
        return (self.layer, self.datatype, self.name)


@dataclass
class LayerEntry:
    key: LayerKey
    polygons: list[np.ndarray]   # each (N, 2) float64, coords in nm
    visible: bool = True
    # Per-layer fill opacity (0-100 %). The overlay outline stays a thin
    # always-visible line; this only fades the fill so the SEM image (or a
    # lower layer) shows through (plan M6.1). Default ~35 % ≈ the old
    # SemViewer overlay alpha of 70/255.
    opacity: int = 35
    color: QColor = field(default_factory=lambda: QColor("#888"))
    # bboxes: (N, 4) float32 [x0, y0, x1, y1] — populated when the ROI / cache
    # builders attach geometry, for O(1) viewport culling at paint time.
    bboxes: Optional[np.ndarray] = None
    # M2.6: for synthetic (expression) layers — the source expression and
    # its {letter: (layer, datatype)} bindings, so the layer can be saved /
    # re-evaluated. ``None`` for raw layers.
    expr_text: Optional[str] = None
    expr_bindings: Optional[dict] = None
    # F3 M2: human-readable layer name from OASIS LAYERNAME (raw layers only;
    # "" when the file declares none). Display-only — NOT part of LayerKey
    # identity, so layer lookup / dedup is unaffected.
    display_name: str = ""

    def fill_alpha(self) -> int:
        """Fill alpha (0-255) derived from ``opacity`` %."""
        return max(0, min(255, round(self.opacity / 100.0 * 255)))


# ── GDS document ─────────────────────────────────────────────────────────────


class GdsDocument:
    """Flat representation of a GDS file's top cell.

    All polygon coordinates are converted to **nanometres** at load time so the
    rest of the tool can deal in a single unit. ``self.bbox_nm`` is the union
    AABB across all layers, suitable for an initial fit-to-view.
    """

    def __init__(self) -> None:
        self.path: Optional[Path] = None
        self.top_cell_name: str = ""
        self.format: str = ""    # "GDSII" or "OASIS"
        self.entries: list[LayerEntry] = []
        self.bbox_nm: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    # ── lookup / mutation ──────────────────────────────────────────────────
    def find(self, key: LayerKey) -> Optional[LayerEntry]:
        for e in self.entries:
            if e.key.key() == key.key():
                return e
        return None

    def visible_entries(self) -> list[LayerEntry]:
        return [e for e in self.entries if e.visible]

    def _recompute_bbox(self) -> None:
        if not self.entries:
            self.bbox_nm = (0.0, 0.0, 0.0, 0.0)
            return
        xs_min, ys_min = np.inf, np.inf
        xs_max, ys_max = -np.inf, -np.inf
        for e in self.entries:
            for poly in e.polygons:
                xs_min = min(xs_min, float(poly[:, 0].min()))
                ys_min = min(ys_min, float(poly[:, 1].min()))
                xs_max = max(xs_max, float(poly[:, 0].max()))
                ys_max = max(ys_max, float(poly[:, 1].max()))
        if not np.isfinite(xs_min):
            self.bbox_nm = (0.0, 0.0, 0.0, 0.0)
        else:
            self.bbox_nm = (xs_min, ys_min, xs_max, ys_max)

    def summary(self) -> str:
        if not self.entries:
            return f"{self.format or 'layout'}: no polygons"
        n_poly = sum(len(e.polygons) for e in self.entries)
        x0, y0, x1, y1 = self.bbox_nm
        prefix = f"{self.format} · " if self.format else ""
        return (f"{prefix}{len(self.entries)} layers · {n_poly} polygons · "
                f"bbox {x1 - x0:.0f}×{y1 - y0:.0f} nm")


def build_roi_document(oas_path, root, layer: int, datatype: int,
                       roi_bbox, *, color: Optional[QColor] = None):
    """Random-access ROI load (M3.5d) for a single layer. Returns
    ``(doc, stats)``. Raises ``ValueError`` if the file has no
    S_CELL_OFFSET index."""
    rar = oasis_random.RandomAccessReader(
        oas_path, wanted_layers={(layer, datatype)},
        bbox_layer=oasis_random.DEFAULT_BBOX_LAYER)
    if not rar.has_offsets():
        raise ValueError("OASIS has no S_CELL_OFFSET index; ROI load "
                         "unavailable (use Open GDS / OASIS instead).")
    doc, per_layer = roi_document_from_reader(
        rar, root, [(layer, datatype)], roi_bbox)
    return doc, per_layer[0][1]


def _roi_entry(rar, root, layer: int, datatype: int, roi_bbox, color,
               cancel_cb=None):
    """Walk one layer's ROI geometry into a LayerEntry (or None when the
    ROI holds nothing on that layer). Returns ``(entry_or_None, stats)``."""
    res = oasis_random.walk_roi(rar, root, roi_bbox, layer, datatype,
                                cancel_cb=cancel_cb)
    polygons: list = []
    bboxes: list = []
    for x1, y1, x2, y2 in res["rects"].tolist():
        polygons.append(np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
                                 dtype=np.float64))
        bboxes.append((x1, y1, x2, y2))
    for p in res["polys"]:
        pf = np.asarray(p, dtype=np.float64)
        polygons.append(pf)
        bboxes.append((pf[:, 0].min(), pf[:, 1].min(),
                       pf[:, 0].max(), pf[:, 1].max()))
    entry = None
    if polygons:
        entry = LayerEntry(
            key=LayerKey(layer=layer, datatype=datatype),
            polygons=polygons, color=color,
            bboxes=np.asarray(bboxes, dtype=np.float32),
            display_name=rar.layer_display_name(layer, datatype))
    return entry, res["stats"]


def roi_document_from_reader(rar, root, layer_keys, roi_bbox, cancel_cb=None,
                             progress_cb=None):
    """ROI load using an already-built :class:`RandomAccessReader` (the big
    file is slurped + indexed once, reused across image clicks). Builds one
    LayerEntry per ``(layer, datatype)`` in ``layer_keys``. Returns
    ``(doc, [(layer_key, stats), ...])``. ``progress_cb(idx, total, key)`` is
    called before each layer so the UI can show an accurate phase."""
    doc = GdsDocument()
    doc.path = rar._path
    doc.format = "OASIS-ROI"
    doc.top_cell_name = str(root)
    per_layer: list = []
    for idx, (layer, datatype) in enumerate(layer_keys):
        if progress_cb is not None:
            progress_cb(idx, len(layer_keys), (layer, datatype))
        color = QColor(_LAYER_PALETTE[idx % len(_LAYER_PALETTE)])
        entry, stats = _roi_entry(rar, root, layer, datatype, roi_bbox, color,
                                  cancel_cb=cancel_cb)
        if entry is not None:
            doc.entries.append(entry)
        per_layer.append(((layer, datatype), stats))
    doc._recompute_bbox()
    return doc, per_layer


# ── Threaded loader (keeps the GUI responsive on big OASIS files) ────────────


# ── F20: Open OASIS Wizard ──────────────────────────────────────────────────


def _wizard_subtitle(html: str) -> str:
    """T6: Qt's default subtitle colour is so washed out the text is hard
    to read on the wizard's cream background. Wrap in an explicit primary
    text colour + slightly larger size so subtitles are first-class copy."""
    return (f'<span style="color:{_TK_TEXT_PRI.name()}; font-size:12px;">'
            f'{html}</span>')


class _FilePickPage(QWizardPage):
    """Step 1 — pick the .oas file and surface its index status."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setTitle("Step 1 — Pick an OASIS file")
        # T6: wizard subtitles use Qt's default washed-out grey; wrap in an
        # explicit colour span so the text actually reads against the
        # warm-cream wizard background.
        self.setSubTitle(_wizard_subtitle(
            "Choose the layout (.oas) file to align against your SEM "
            "images. Large files are fine — only ROI geometry is loaded "
            "on demand."))

        v = QVBoxLayout(self)
        v.setSpacing(8)

        row = QHBoxLayout()
        self._edit = QLineEdit(self)
        self._edit.setReadOnly(True)
        self._edit.setPlaceholderText("(no file selected)")
        self._browse = QPushButton(_qicon("folder-open"), " Browse…", self)
        self._browse.clicked.connect(self._on_browse)
        row.addWidget(self._edit, 1)
        row.addWidget(self._browse)
        v.addLayout(row)

        self._info = QLabel("", self)
        self._info.setWordWrap(True)
        self._info.setStyleSheet(_hint_qss(_FS_LABEL, pad="6px 0"))
        v.addWidget(self._info)

        self._warning = QLabel("", self)
        self._warning.setWordWrap(True)
        self._warning.setStyleSheet(
            f"color:{_TK_ACCENT_DK.name()}; font-size:{_FS_LABEL}px; "
            f"padding:4px 0;")
        v.addWidget(self._warning)
        v.addStretch(1)

        # Expose the chosen path through the wizard field API so the next
        # pages (and the caller) can pick it up via ``wizard.field("file")``.
        self.registerField("file*", self._edit)

        self._file_path: Optional[str] = None
        self._has_offsets = False

    _SETTINGS_LAST_DIR = "wizard/last_oas_dir"

    def _on_browse(self) -> None:
        # T5: remember the last directory the user picked from, so the next
        # time they Open OASIS the dialog opens in the right place. Files
        # are routinely big and live in one or two project folders.
        settings = QSettings("GLAS", "GLAS")
        start = str(settings.value(self._SETTINGS_LAST_DIR, "", type=str) or "")
        path, _ = QFileDialog.getOpenFileName(
            self, "Open OASIS", start,
            "OASIS files (*.oas *.oasis);;All files (*)")
        if not path:
            return
        settings.setValue(self._SETTINGS_LAST_DIR, str(Path(path).parent))
        self._edit.setText(path)
        self._update_info(path)
        self.completeChanged.emit()

    def _update_info(self, path: str) -> None:
        try:
            p = Path(path)
            size_mb = p.stat().st_size / 1024 / 1024
        except OSError as exc:
            self._info.setText(f"⚠ cannot stat file: {exc}")
            self._warning.setText("")
            self._has_offsets = False
            return
        # Cheap index check — only reads the small name-table section.
        try:
            import oasis_streamer
            scan = oasis_streamer.scan_cell_offsets(path)
            n_cells = len(scan.get("by_refnum", {}))
            n_named = len(scan.get("by_name", {}))
            self._has_offsets = bool(n_cells or n_named)
        except Exception:
            self._has_offsets = False
            scan = {}
        index_msg = (
            f"✓ S_CELL_OFFSET index found ({max(n_cells, n_named):,} cells)"
            if self._has_offsets else
            "⚠ no S_CELL_OFFSET index — ROI mode unavailable")
        self._info.setText(
            f"<b>{p.name}</b> · {size_mb:,.1f} MB<br>{index_msg}")
        if not self._has_offsets:
            self._warning.setText(
                "Without an S_CELL_OFFSET index, GLAS can't load ROIs on "
                "demand. Re-save the file with KLayout (File → Save As → "
                "OASIS, strict mode) and try again.")
        else:
            self._warning.setText("")
        self._file_path = path

    def isComplete(self) -> bool:
        # Allow Next even without offsets so the user sees the rest of the
        # wizard; the actual blocker is enforced after Finish.
        return bool(self._file_path) and Path(self._file_path).exists()

    def file_path(self) -> Optional[str]:
        return self._file_path

    def has_offsets(self) -> bool:
        return self._has_offsets


class _LayerPickPage(QWizardPage):
    """Step 2 — scan the file and let the user multi-select layers."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setTitle("Step 2 — Pick layers to load")
        self.setSubTitle(_wizard_subtitle(
            "GLAS only loads the layers you need — picking 1–3 is the norm. "
            "Click Scan to discover what's in the file, or type "
            "<code>layer/datatype</code> pairs directly."))

        v = QVBoxLayout(self)
        v.setSpacing(8)

        scan_row = QHBoxLayout()
        self._scan_btn = QPushButton(_qicon("layers"), " Scan layers", self)
        self._scan_btn.clicked.connect(self._on_scan)
        scan_row.addWidget(self._scan_btn)
        scan_row.addStretch(1)
        v.addLayout(scan_row)

        self._list = QListWidget(self)
        self._list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection)
        self._list.itemSelectionChanged.connect(self._on_list_changed)
        v.addWidget(self._list, 1)

        v.addWidget(QLabel("…or type pairs (e.g. <code>20/0, 30/0</code>):"))
        self._edit = QLineEdit(self)
        self._edit.setPlaceholderText("layer/datatype, comma-separated")
        self._edit.textChanged.connect(lambda _t: self.completeChanged.emit())
        v.addWidget(self._edit)

        self._status = QLabel("", self)
        self._status.setWordWrap(True)
        self._status.setStyleSheet(_hint_qss(_FS_LABEL, pad="4px 0"))
        v.addWidget(self._status)

        # Scan-mode state.
        self._scan_thread: Optional[QThread] = None
        self._scan_worker: Optional[LayerScanWorker] = None
        self._scan_result: dict = {}

    def initializePage(self) -> None:
        # Reset whenever the user revisits the page.
        self._list.clear()
        self._edit.clear()
        self._status.setText("")

    def _on_scan(self) -> None:
        if self._scan_thread is not None:
            return
        path = self.field("file")
        if not path:
            return
        self._scan_btn.setEnabled(False)
        self._status.setText(f"Scanning {Path(path).name}…")
        self._scan_thread = QThread(self)
        self._scan_worker = LayerScanWorker(str(path))
        self._scan_worker.moveToThread(self._scan_thread)
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.progress.connect(self._status.setText)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.cancelled.connect(
            lambda: self._status.setText("Scan cancelled."))
        for sig in (self._scan_worker.finished,
                    self._scan_worker.failed,
                    self._scan_worker.cancelled):
            sig.connect(self._scan_thread.quit)
        self._scan_thread.finished.connect(self._cleanup_scan)
        self._scan_thread.start()

    def _on_scan_finished(self, result: object) -> None:
        if isinstance(result, dict):
            layers = result.get("layers") or []
            self._scan_result = result
        else:
            layers = list(result) if isinstance(result, list) else []
            self._scan_result = {"layers": layers}
        if not layers:
            self._status.setText(
                "Scan found no layers — type pairs manually below.")
            return
        self._list.clear()
        for entry in sorted(layers, key=lambda d: (d["layer"], d["datatype"])):
            label = f"L{entry['layer']}/D{entry['datatype']}"
            if entry.get("name"):
                label += f"   ·  {entry['name']}"
            it = QListWidgetItem(label, self._list)
            it.setData(Qt.ItemDataRole.UserRole,
                       (int(entry["layer"]), int(entry["datatype"])))
        self._status.setText(
            f"Found {len(layers)} layer(s) — select one or more above.")

    def _on_scan_failed(self, msg: str) -> None:
        self._status.setText(
            f"Scan failed: {msg.splitlines()[0]} — use manual entry below.")

    def _cleanup_scan(self) -> None:
        if self._scan_worker is not None:
            self._scan_worker.deleteLater()
            self._scan_worker = None
        if self._scan_thread is not None:
            self._scan_thread.deleteLater()
            self._scan_thread = None
        self._scan_btn.setEnabled(True)

    def _on_list_changed(self) -> None:
        self.completeChanged.emit()

    def _parse_text(self) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for chunk in self._edit.text().split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                if "/" in chunk:
                    l_s, d_s = chunk.split("/", 1)
                else:
                    l_s, d_s = chunk, "0"
                out.append((int(l_s), int(d_s)))
            except ValueError:
                return []   # any parse error invalidates the lot
        return out

    def isComplete(self) -> bool:
        return bool(self.layer_keys())

    def layer_keys(self) -> list[tuple[int, int]]:
        # Selected items take precedence; fall back to text entry so a user
        # can manually key a layer the scan didn't surface.
        from_list: list[tuple[int, int]] = []
        for item in self._list.selectedItems():
            data = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, tuple) and len(data) == 2:
                from_list.append((int(data[0]), int(data[1])))
        if from_list:
            return from_list
        return self._parse_text()

    def scan_result(self) -> dict:
        return self._scan_result


class _RootCellPage(QWizardPage):
    """Step 3 — pick the root (top) cell. We pre-select a name containing
    ``top`` or ``merge``; users keep the default unless they know better."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setTitle("Step 3 — Pick the root cell")
        self.setSubTitle(_wizard_subtitle(
            "The root cell is the top of the layout hierarchy — usually the "
            "whole chip. The recommended default is named ‘top’ or ‘merge’; "
            "if your file uses a different convention, override below."))

        v = QVBoxLayout(self)
        v.setSpacing(8)
        self._combo = QComboBox(self)
        self._combo.setEditable(False)
        self._combo.currentTextChanged.connect(
            lambda _t: self.completeChanged.emit())
        v.addWidget(self._combo)

        self._status = QLabel("", self)
        self._status.setWordWrap(True)
        self._status.setStyleSheet(_hint_qss(_FS_LABEL, pad="4px 0"))
        v.addWidget(self._status)
        v.addStretch(1)

    def initializePage(self) -> None:
        # The names come from the file's S_CELL_OFFSET name table, which
        # ``scan_cell_offsets`` already returned during Step 2. Falling back
        # to a fresh scan keeps the page usable if the user typed pairs and
        # skipped scanning.
        wiz = self.wizard()
        names: list[str] = []
        scan = wiz.layer_page().scan_result() if wiz else {}
        by_name = scan.get("by_name") if isinstance(scan, dict) else None
        if by_name:
            names = list(by_name.keys())
        else:
            path = self.field("file")
            if path:
                try:
                    import oasis_streamer
                    res = oasis_streamer.scan_cell_offsets(path)
                    names = list(res.get("by_name", {}).keys())
                except Exception:
                    names = []
        self._combo.clear()
        if not names:
            self._status.setText(
                "⚠ no cell names found — file may lack a name table.")
            self.completeChanged.emit()
            return
        self._combo.addItems(names)
        default = next(
            (n for n in names if "top" in n.lower() or "merge" in n.lower()),
            names[-1])
        idx = names.index(default)
        self._combo.setCurrentIndex(idx)
        self._status.setText(
            f"Recommended: <b>{default}</b> "
            f"(name looks like a top cell; out of {len(names):,} cells).")
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return bool(self._combo.currentText())

    def root_cell(self) -> str:
        return self._combo.currentText()


class OpenOasisWizard(QWizard):
    """F20: replaces the LayerFilterDialog + LayerPickDialog + QInputDialog
    root-cell cascade with a single three-page wizard. The caller reads the
    chosen file / layers / root cell via :meth:`file_path` / :meth:`layer_keys`
    / :meth:`root_cell` after :py:meth:`exec` returns ``Accepted``."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Open OASIS")
        # ClassicStyle drops the empty Modern/Aero "banner" strip above the
        # title — we don't ship a wizard pixmap, so the band reads as a stray
        # white block (reported on PR #11 screenshot).
        self.setWizardStyle(QWizard.WizardStyle.ClassicStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setOption(QWizard.WizardOption.HaveHelpButton, False)
        self.setMinimumSize(640, 520)

        self._file_page = _FilePickPage(self)
        self._layer_page = _LayerPickPage(self)
        self._root_page = _RootCellPage(self)
        self.addPage(self._file_page)
        self.addPage(self._layer_page)
        self.addPage(self._root_page)

        # T8: keep the Next / Finish button accent-orange so the eye is
        # pulled to the forward action — Qt's default leaves it visually
        # tied with Cancel. Disabled state still reads as muted.
        accent = _TK_ACCENT.name()
        accent_dk = _TK_ACCENT_DK.name()
        next_qss = (
            f"QPushButton {{ background:{accent}; color:#ffffff; "
            f"border:1px solid {accent_dk}; border-radius:4px; "
            f"padding:5px 16px; font-weight:700; }}"
            f"QPushButton:hover {{ background:{accent_dk}; }}"
            f"QPushButton:disabled {{ background:#f6e5cf; "
            f"color:#cdb89b; border:1px solid #e6d2b0; }}")
        for which in (QWizard.WizardButton.NextButton,
                      QWizard.WizardButton.FinishButton):
            btn = self.button(which)
            if btn is not None:
                btn.setStyleSheet(next_qss)

    # Public accessors (used by MainWindow._on_open_roi after exec()).

    def file_path(self) -> Optional[str]:
        return self._file_page.file_path()

    def has_offsets(self) -> bool:
        return self._file_page.has_offsets()

    def layer_keys(self) -> list[tuple[int, int]]:
        return self._layer_page.layer_keys()

    def root_cell(self) -> str:
        return self._root_page.root_cell()

    def layer_page(self) -> "_LayerPickPage":
        return self._layer_page


class _AnimatedBar(QWidget):
    """Flat, self-painted progress bar (F8: reverted from the gradient/glow/
    sheen version, which was costly to repaint during a busy batch).

    Two modes: *determinate* (single flat fill to a fraction, with a centred
    percent label) and *indeterminate* (a single flat block sliding back and
    forth, for phases where total work is unknown). Painting is self-contained
    (a few rounded rects) so the app QSS can't flatten it, but there is no
    animation on a determinate bar — only the indeterminate slider advances, so
    a running batch doesn't burn repaints on the bar. API is unchanged
    (``set_fraction`` / ``set_indeterminate`` / ``advance``)."""

    _TRACK = QColor("#ece0d2")
    _FILL = QColor("#e0863a")

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(14)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self._frac = 0.0
        self._indeterminate = True
        self._phase = 0.0   # 0..1, position of the indeterminate slider

    def set_fraction(self, frac: float) -> None:
        self._indeterminate = False
        self._frac = max(0.0, min(1.0, frac))
        self.update()

    def set_indeterminate(self, on: bool = True) -> None:
        self._indeterminate = on
        self.update()

    def advance(self) -> None:
        # Only the indeterminate slider animates; a determinate bar is static
        # between set_fraction calls, so don't burn repaints on it (F8).
        if not self._indeterminate:
            return
        self._phase = (self._phase + 0.045) % 1.0
        self.update()

    def paintEvent(self, ev) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = float(self.width())
        H = float(self.height())
        y = 0.0
        h = H
        r = h / 2.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._TRACK)
        p.drawRoundedRect(QRectF(0, y, w, h), r, r)
        p.setBrush(self._FILL)
        if self._indeterminate:
            bw = max(48.0, w * 0.30)
            t = self._phase
            tri = 2.0 * t if t < 0.5 else 2.0 * (1.0 - t)   # ping-pong
            x = tri * (w - bw)
            p.drawRoundedRect(QRectF(x, y, bw, h), r, r)
        elif self._frac > 0:
            fw = self._frac * w
            p.drawRoundedRect(QRectF(0, y, fw, h), min(r, fw / 2.0), r)
        p.end()


class LoadProgressDialog(QDialog):
    """Custom progress dialog for the GDS loader.

    QProgressDialog with the main app's QSS produced an empty-looking dialog
    (the styled ``QProgressBar::chunk`` background killed the animation, and the
    inherited label sometimes rendered with no visible text). This dialog uses a
    self-painted :class:`_AnimatedBar` instead: a sweeping fill bar + Braille
    spinner + elapsed clock prove the worker is alive even during opaque
    read-phases. When the caller knows the total work (e.g. batch fine-align),
    :meth:`set_progress` switches the bar to a determinate fill with percent and
    ETA; otherwise it stays indeterminate. Cancel emits ``cancel_requested``."""

    cancel_requested = pyqtSignal()

    _SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Loading layout")
        self.setModal(True)
        self.setMinimumWidth(460)
        self.setSizeGripEnabled(False)
        # Make sure the dialog never inherits a transparent background from
        # accidental ancestor QSS rules.
        self.setStyleSheet(
            "QDialog { background: #fff8f2; border: 1px solid #c8b89e; }"
            "QLabel#progressTitle { color: #3f3428; font-size: 14px;"
            " font-weight: 600; }"
            "QLabel#progressDetail { color: #6f6254; font-size: 12px; }"
            "QLabel#progressSpinner { color: #c97028; font-size: 22px;"
            " font-family: 'Consolas', 'DejaVu Sans Mono', monospace; }"
            "QPushButton { min-height: 28px; padding: 0 14px; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(10)

        self._title = QLabel("Starting…", self)
        self._title.setObjectName("progressTitle")
        self._title.setWordWrap(True)
        layout.addWidget(self._title)

        self._bar = _AnimatedBar(self)
        layout.addWidget(self._bar)

        row = QHBoxLayout()
        row.setSpacing(12)
        self._spinner = QLabel(self._SPINNER[0], self)
        self._spinner.setObjectName("progressSpinner")
        self._spinner.setFixedWidth(28)
        row.addWidget(self._spinner)
        self._detail = QLabel("Elapsed: 0:00", self)
        self._detail.setObjectName("progressDetail")
        row.addWidget(self._detail, 1)
        layout.addLayout(row)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._cancel_btn = QPushButton("Cancel", self)
        self._cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self._cancel_btn)
        layout.addLayout(btn_row)

        self._cancelled = False
        self._spin_idx = 0
        self._progress: Optional[tuple] = None   # (done, total) when known
        self._elapsed = QElapsedTimer()
        self._elapsed.start()
        self._tick = QTimer(self)
        self._tick.setInterval(120)   # 120 ms is smooth without burning CPU
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

    def set_text(self, text: str) -> None:
        self._title.setText(text)

    def set_progress(self, done: int, total: int) -> None:
        """Switch the bar to a determinate fill (done / total). The detail line
        then shows count, percent and an ETA estimated from elapsed time."""
        self._progress = (done, total)
        self._bar.set_fraction(done / total if total else 0.0)
        self._refresh_detail()

    def shutdown(self) -> None:
        """Stop the spinner timer (call before close to avoid late updates)."""
        self._tick.stop()

    def _on_tick(self) -> None:
        self._spin_idx = (self._spin_idx + 1) % len(self._SPINNER)
        self._spinner.setText(self._SPINNER[self._spin_idx])
        self._bar.advance()
        self._refresh_detail()

    def _refresh_detail(self) -> None:
        secs = int(self._elapsed.elapsed() / 1000)
        elapsed = f"Elapsed {secs // 60}:{secs % 60:02d}"
        if self._progress is not None:
            done, total = self._progress
            pct = int(100 * done / total) if total else 0
            eta = ""
            el = self._elapsed.elapsed() / 1000.0
            if 0 < done < total and el > 0:
                rem = el / done * (total - done)
                eta = f"  ·  ETA {int(rem) // 60}:{int(rem) % 60:02d}"
            detail = f"{done} / {total}  ·  {pct}%  ·  {elapsed}{eta}"
        else:
            detail = elapsed
        if self._cancelled:
            detail += "  ·  cancelling at next checkpoint…"
        self._detail.setText(detail)

    def _on_cancel(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        self._cancel_btn.setEnabled(False)
        self.cancel_requested.emit()

    def closeEvent(self, ev) -> None:  # type: ignore[override]
        # Treat X-button as Cancel while load is active.
        if not self._cancelled and self._tick.isActive():
            self._on_cancel()
            ev.ignore()
        else:
            ev.accept()


# Hard cap: stop loading once total polygon count crosses this. Prevents the
# child process from OOM'ing the host machine on huge / hierarchical layouts
# whose flattened expansion is unbounded. Surfaced to the user as a friendly
# "loading aborted" message; later milestones add per-layer lazy loading so
# this limit can be relaxed.
_POLY_HARD_LIMIT = 10_000_000


def _scan_layers_main(path: str, q: "mp.Queue") -> None:
    """Child process: enumerate available layers in an OASIS file without
    keeping polygon data.

    The ``oasis_streamer`` (F2 M1.12b) reads byte-by-byte and stops at the
    first CELL record, so all LAYERNAME entries are surfaced in sub-second
    time even on 300 MB+ Calibre D2DB files. The tool is OASIS-only; ``.gds``
    is not supported (the random-access ROI reader needs OASIS per-cell byte
    offsets).
    """
    try:
        from pathlib import Path as _Path
        p = _Path(path)
        ext = p.suffix.lower()
        if ext not in (".oas", ".oasis"):
            q.put(("failed",
                   f"Unsupported file type '{ext}'. This tool is OASIS-only "
                   "(.oas / .oasis)."))
            return
        sys.stderr.write(
            f"[gds-scan] using oasis_streamer for {p.name}\n")
        sys.stderr.flush()
        _scan_oas_with_streamer(p, q)
    except Exception as exc:
        q.put(("failed",
               f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"))


def _scan_oas_with_streamer(path: "Path", q: "mp.Queue") -> None:
    """Enumerate an OASIS file's layers for the "Scan layers" UI.

    Delegates to ``oasis_random.enumerate_layers`` (F12), which:

    - returns the named LAYERNAME table in sub-second time when present
      (the historical fast path — LAYERNAME records sit before the first
      CELL, so the name table is read without touching cell content); else
    - falls back to a **bounded** cell sample over the S_CELL_OFFSET index
      to surface the numeric ``(layer/datatype)`` pairs that actually appear
      (never a full-file scan — these files are multi-GB); else
    - reports ``no-index`` when the file has neither table (the caller tells
      the user to re-save via KLayout).

    Results are sidecar-cached (F12 M3) so repeat scans of the same file are
    instant. Progress (cells sampled + layers found so far) is streamed back
    on ``q`` so the user can watch layers appear and cancel early.
    """
    import sys as _sys
    import time as _t
    from pathlib import Path as _Path

    # Make sure the core modules (glas/core) are importable in the
    # subprocess context (spawn re-imports without main.py's path setup).
    _core = _Path(__file__).resolve().parent.parent / "core"
    if str(_core) not in _sys.path:
        _sys.path.insert(0, str(_core))
    import oasis_random as _orx

    t0 = _t.monotonic()
    p = path
    size_mb = p.stat().st_size / 1024 / 1024
    q.put(("progress",
           f"Scanning {p.name} ({size_mb:,.0f} MB) for layers…"))

    last_emit = [0.0]

    def _progress(cells_done: int, layers_so_far: list) -> None:
        # Throttle: a queue put per cell floods the UI on big samples.
        now = _t.monotonic()
        if now - last_emit[0] < 0.15:
            return
        last_emit[0] = now
        preview = ", ".join(f"{d['layer']}/{d['datatype']}"
                            for d in layers_so_far[:12])
        more = " …" if len(layers_so_far) > 12 else ""
        q.put(("progress",
               f"Sampling {p.name} — {cells_done} cells scanned, "
               f"{len(layers_so_far)} layer(s): {preview}{more}"))

    res = _orx.enumerate_layers(p, progress_cb=_progress)

    elapsed = _t.monotonic() - t0
    diag = res.get("diag") or {}
    layers = res.get("layers") or []
    # Rich terminal diagnostics: how the index tables were located (the common
    # "UI says no index" cause is a strict-mode file whose tables sit after the
    # cells), plus what was enumerated.
    _sys.stderr.write(
        f"[gds-scan] {p.name}  {size_mb:,.0f} MB  {elapsed:.2f}s\n"
        f"[gds-scan]   source        = {res.get('source')}\n"
        f"[gds-scan]   offset_flag   = {diag.get('offset_flag')}  "
        f"(0=offsets in START, 1=in END, None=non-strict/absent)\n"
        f"[gds-scan]   offsets_via   = {diag.get('offsets_via')}  "
        f"(inline | tables | None=none found)\n"
        f"[gds-scan]   table_offsets = {diag.get('table_offsets')}\n"
        f"[gds-scan]   cell offsets  = {diag.get('n_cell_offsets')}  "
        f"(S_CELL_OFFSET entries)\n"
        f"[gds-scan]   LAYERNAME rows= {diag.get('n_layernames')}\n"
        f"[gds-scan]   S_BOUNDING_BOX= {diag.get('n_bbox_props')}  "
        f"(per-cell bbox in name table -> fast ROI prune without CE layer; "
        f"sample {diag.get('bbox_sample')})\n"
        f"[gds-scan]   layers found  = {len(layers)}  "
        + ", ".join(f"{d['layer']}/{d['datatype']}" for d in layers[:20])
        + (" …" if len(layers) > 20 else "") + "\n")
    if res.get("source") == "sampled":
        sb = diag.get("sample_bounds") or {}
        _sys.stderr.write(
            f"[gds-scan]   sample bounds = {sb}  "
            "(numeric scan is a sample, not exhaustive; widen via GLAS_SCAN_* "
            "env vars if a layer is missing)\n")
    if res.get("source") == "no-index":
        _sys.stderr.write(
            "[gds-scan]   NOTE: no LAYERNAME table and no S_CELL_OFFSET index "
            "were found. If you saved this from KLayout, enable 'strict mode' "
            "in the OASIS writer options so the cell-offset table is written.\n")
    _sys.stderr.flush()
    q.put(("done", res))


class LayerScanWorker(QObject):
    """Run ``_scan_layers_main`` in a subprocess, surface results as signals."""

    progress = pyqtSignal(str)
    finished = pyqtSignal(object)   # list[dict(layer, datatype, name)]
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path
        self._cancel = False
        self._proc: Optional[mp.process.BaseProcess] = None

    def cancel(self) -> None:
        self._cancel = True
        if self._proc is not None and self._proc.is_alive():
            self._proc.terminate()

    def run(self) -> None:
        ctx = mp.get_context("spawn")
        q: mp.Queue = ctx.Queue()
        self._proc = ctx.Process(
            target=_scan_layers_main, args=(self._path, q), daemon=True,
        )
        try:
            self._proc.start()
        except Exception as exc:
            self.failed.emit(f"failed to start scan subprocess: {exc}")
            return

        try:
            while True:
                if self._cancel:
                    if self._proc.is_alive():
                        self._proc.terminate()
                    self._proc.join(timeout=2)
                    self.cancelled.emit()
                    return
                try:
                    msg = q.get(timeout=0.1)
                except Exception:
                    if not self._proc.is_alive() and q.empty():
                        self.failed.emit(
                            f"scan subprocess exited unexpectedly "
                            f"(code {self._proc.exitcode})")
                        return
                    continue
                kind, payload = msg
                if kind == "progress":
                    self.progress.emit(payload)
                elif kind == "done":
                    self.finished.emit(payload)
                    return
                elif kind == "failed":
                    self.failed.emit(payload)
                    return
        finally:
            if self._proc.is_alive():
                self._proc.terminate()
            self._proc.join(timeout=2)



class RoiWalkWorker(QObject):
    """Runs a random-access ROI walk (M3.5d) off the UI thread so the first
    load (which scans every reachable cell's size) doesn't freeze the app.
    Reads only — the reader isn't touched by the UI thread during the walk."""

    finished = pyqtSignal(object, object)   # (GdsDocument, per_layer list)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()
    progress = pyqtSignal(int, int, object)   # (layer idx, total, (layer,dt))

    def __init__(self, rar, root, layer_keys, roi) -> None:
        super().__init__()
        self._rar = rar
        self._root = root
        self._layers = layer_keys
        self._roi = roi
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            doc, per_layer = roi_document_from_reader(
                self._rar, self._root, self._layers, self._roi,
                cancel_cb=lambda: self._cancel,
                progress_cb=lambda i, n, k: self.progress.emit(i, n, k))
        except oasis_random.WalkCancelled:
            self.cancelled.emit()
        except Exception as exc:                       # noqa: BLE001
            self.failed.emit(str(exc))
        else:
            self.finished.emit(doc, per_layer)


def _walk_res_to_polys(res) -> list:
    """walk_roi result dict -> flat list of (N,2) float64 polygon rings
    (rectangles expanded to 4-point rings). Mirrors _roi_entry."""
    polys: list = []
    for x1, y1, x2, y2 in res["rects"].tolist():
        polys.append(np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
                              dtype=np.float64))
    for p in res["polys"]:
        polys.append(np.asarray(p, dtype=np.float64))
    return polys


class WholeChipExportWorker(QObject):
    """F11 M2/M3: export the whole chip to OASIS, tiled + streamed so peak
    memory is bounded by one tile. Per tile: walk selected raw layers and
    clip to the tile; recompute each Boolean recipe over a haloed tile
    (halo ≥ max morph reach so grown geometry from neighbours is included)
    and clip the result back to the exact tile. Reads only."""

    progress = pyqtSignal(int, int)     # (tiles done, total)
    finished = pyqtSignal(int)          # total tiles written
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, *, rar, root, path, unit, cellname, chip_bbox,
                 raw_specs, recipe_specs, recipe_def, target_nm, halo_nm) -> None:
        super().__init__()
        self._rar = rar
        self._root = root
        self._path = path
        self._unit = unit
        self._cellname = cellname
        self._chip_bbox = chip_bbox
        self._raw_specs = raw_specs        # [(out_l, out_d, src_l, src_d)]
        self._recipe_specs = recipe_specs  # [(out_l, out_d, expr, bindings)]
        self._recipe_def = recipe_def
        self._target_nm = target_nm
        self._halo_nm = halo_nm
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        cc = lambda: self._cancel          # noqa: E731
        try:
            tiles = layout_export.tile_grid(self._chip_bbox, self._target_nm)
            total = len(tiles)
            with oasis_writer.OasisStreamWriter(
                    self._path, unit=self._unit, cellname=self._cellname) as w:
                for i, tile in enumerate(tiles):
                    if self._cancel:
                        raise oasis_random.WalkCancelled()
                    for out_l, out_d, src_l, src_d in self._raw_specs:
                        res = oasis_random.walk_roi(
                            self._rar, self._root, tile, src_l, src_d,
                            cancel_cb=cc)
                        rings = layout_export.clip_polygons(
                            _walk_res_to_polys(res), tile)
                        w.add_polygons(out_l, out_d, rings)
                    if self._recipe_specs:
                        self._export_recipes_for_tile(w, tile, cc)
                    self.progress.emit(i + 1, total)
            self.finished.emit(total)
        except oasis_random.WalkCancelled:
            self.cancelled.emit()
        except Exception as exc:                       # noqa: BLE001
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")

    def _export_recipes_for_tile(self, w, tile, cc) -> None:
        m = self._halo_nm
        hx0, hy0, hx1, hy1 = tile[0] - m, tile[1] - m, tile[2] + m, tile[3] + m
        cache: dict = {}

        def raw_provider(layer: int, datatype: int):
            key = (layer, datatype)
            if key not in cache:
                res = oasis_random.walk_roi(self._rar, self._root,
                                            (hx0, hy0, hx1, hy1),
                                            layer, datatype, cancel_cb=cc)
                cache[key] = gds_boolean.polys_to_geometry(
                    _walk_res_to_polys(res))
            return cache[key]

        fov_box = gds_boolean.fov_box((hx0 + hx1) / 2.0, (hy0 + hy1) / 2.0,
                                      hx1 - hx0, hy1 - hy0)
        for out_l, out_d, expr, bindings in self._recipe_specs:
            geom = gds_boolean.resolve_expression(
                expr, bindings, raw_provider=raw_provider,
                recipe_provider=self._recipe_def, fov_bbox=fov_box)
            rings = layout_export.clip_polygons(
                gds_boolean.geometry_to_polygons(geom), tile)
            w.add_polygons(out_l, out_d, rings)


def _auto_batch_workers(override: int = 0) -> int:
    """Process-pool size for batch fine-align / image export. Delegates to
    ``fine_align.batch_worker_count`` (F14: explicit UI override wins, else one
    per core capped at 16; cv2 is single-threaded in workers so the higher cap
    no longer oversubscribes)."""
    return fine_align.batch_worker_count(override)


class FineAlignAllWorker(QObject):
    """Batch fine align (plan M4b "Run all"): for every SEM image with
    coordinates, walk the POI ROI, render a template and run
    ``cv2.matchTemplate``.

    F8: parallelised across a *process* pool. OASIS ROI decoding is a tight
    pure-Python loop that holds the GIL, so a thread pool neither sped it up
    nor left the Qt UI thread any GIL time (the source of the batch "lag").
    Processes run the decode truly in parallel and can't touch the GUI
    interpreter at all. Each worker process rebuilds its own reader from the
    file path (``fine_align._pool_init``); per-image work is independent of
    order, so the output is byte/value identical to the old sequential /
    thread-pool path (§7). Results are emitted from this (single) worker thread
    as futures complete. Tiny batches skip the pool and run in-thread, since
    spawning processes would cost more than it saves."""

    progress = pyqtSignal(int, int, str)        # (done, total, image_id)
    # (image_id, dx, dy, score, used_radius_px, status); status is objective:
    # ok / no-coords / missing-file / no-scale / flat (F5 M2/M5 C3-C4).
    result = pyqtSignal(str, float, float, float, int, str)
    finished = pyqtSignal(int)                  # count aligned
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, rar, root, poi_specs, jobs, cfg) -> None:
        super().__init__()
        self._rar = rar
        self._root = root
        # F3 multi-POI: list of (spec, fg_glv); each spec is walked per ROI and
        # composited at its fg onto the shared bg before matching.
        self._poi_specs = list(poi_specs)
        self._jobs = jobs        # list of (image_id, anchor|None, path, exists)
        self._cfg = cfg          # dict: fov_w/h, nm_auto, nm_manual, bg/blur, search_radius_nm
        # threading.Event set from the GUI thread; the orchestrator reads it to
        # stop submitting / drop pending futures (F8 cancel is image-grained).
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            n = len(self._jobs)
            if n == 0:
                self.finished.emit(0)
                return
            workers = min(_auto_batch_workers(self._cfg.get("max_workers", 0)), n)
            # Small batches: process spawn + per-process reader build costs more
            # than it saves, so run them in-thread on a single cloned reader.
            if n <= 2 or workers <= 1:
                self._run_in_thread()
            else:
                self._run_process_pool(workers)
        except Exception as exc:                       # noqa: BLE001
            self.failed.emit(str(exc))

    def _run_in_thread(self) -> None:
        """Sequential fallback for tiny batches: run in this worker thread on
        the shared reader (like the pre-F6 path). The walk's ``cancel_cb`` reads
        the event so a mid-walk cancel still bails promptly."""
        n = len(self._jobs)
        done = 0
        for job in self._jobs:
            if self._cancel.is_set():
                self.cancelled.emit()
                return
            try:
                res = fine_align._fine_align_image(
                    job, self._rar, self._root, self._poi_specs, self._cfg,
                    self._cancel.is_set)
            except oasis_random.WalkCancelled:
                self.cancelled.emit()
                return
            done += 1
            if res is not None:
                self.progress.emit(done, n, res[0])
                self.result.emit(*res)
        self.finished.emit(done)

    def _run_process_pool(self, workers: int) -> None:
        """Parallel path: fan the per-image jobs out to a spawn-based process
        pool. Each worker rebuilds its reader once via the initializer. All jobs
        are submitted up front (so every worker stays busy), but results are
        consumed in *submission order* (F14: image order) so the table / overview
        fill top-to-bottom instead of jumping around in completion order. On
        cancel, futures that haven't started are dropped; in-flight images run to
        completion and their results are still kept."""
        n = len(self._jobs)
        rar = self._rar
        # F23 M2: reuse a session-persistent, pre-warmable pool keyed on the
        # reader identity + worker count, instead of spawning + tearing one down
        # every Run all. The per-batch context (root / POI specs / cfg) rides
        # each task (F23 M2 refactor) so a POI / search-radius change still hits
        # the warm pool. F23 M1: the already-built name-table index is shipped
        # via the pool initializer so each worker skips its own rescan (plain
        # dict/list/int data, pickles cleanly into the spawn workers).
        done = 0
        dropped = False
        # lease() holds the pool busy for the whole run so its idle-timeout
        # auto-release can't fire mid-batch; on exit the idle timer re-arms.
        # The pool persists across batches (F23 M2) — no per-batch shutdown.
        with fine_align.batch_pool.lease(
                str(rar._path), rar._init_wanted, rar._dtype, rar._bbox_layer,
                rar.index_snapshot(), workers) as ex:
            futures = [ex.submit(fine_align._pool_task, job, self._root,
                                 self._poi_specs, self._cfg)
                       for job in self._jobs]
            for fut in futures:
                if self._cancel.is_set() and not dropped:
                    for f in futures:
                        f.cancel()          # drop not-yet-started tasks
                    dropped = True
                try:
                    res = fut.result()
                except CancelledError:
                    continue                # a pending task we just dropped
                done += 1
                if res is not None:
                    self.progress.emit(done, n, res[0])
                    self.result.emit(*res)
        if self._cancel.is_set():
            self.cancelled.emit()
        else:
            self.finished.emit(done)


class OverlayExportWorker(QObject):
    """Batch export of SEM frames + aligned GDS-outline overlays + a manifest
    (F5 M6). For every selected image it optionally writes ``<id>_raw.png`` and
    ``<id>_overlay.png`` (the POI layer outlines stroked on the SEM at the
    coarse+refined anchor), then a manifest (CSV + JSON) keyed by image_id so a
    downstream tool — e.g. MMH — can join back. Sequential single reader, like
    :class:`FineAlignAllWorker`."""

    progress = pyqtSignal(int, int, str)        # (done, total, image_id)
    finished = pyqtSignal(int, str)             # (count, manifest_csv_path)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    _COLS = fine_align.OVERLAY_MANIFEST_COLS

    def __init__(self, rar, root, poi_specs_colored, jobs, cfg, out_dir,
                 export_raw: bool, export_overlay: bool,
                 export_gray: bool = False, export_label: bool = False,
                 score_threshold: float = 0.0, label_map=None) -> None:
        super().__init__()
        self._rar = rar
        self._root = root
        self._poi = list(poi_specs_colored)   # [(spec, (r,g,b), fg_glv), ...]
        self._jobs = jobs    # [(image_id, coarse|None, refined|None, path, exists)]
        self._cfg = cfg
        self._out_dir = Path(out_dir)
        self._export_raw = export_raw
        self._export_overlay = export_overlay
        # F15 (replaces F13's mask): per-image simulated-GLV grayscale + integer
        # ROI label map, gated by score threshold; ``label_map`` describes the
        # label ids for the manifest.
        self._export_gray = export_gray
        self._export_label = export_label
        self._score_thr = score_threshold
        self._label_map = list(label_map or [])
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            n = len(self._jobs)
            if n == 0:
                self.finished.emit(0, self._write_manifest([]))
                return
            workers = min(
                fine_align.batch_worker_count(self._cfg.get("max_workers", 0)), n)
            # F14: parallelise the export across a process pool (mirrors
            # FineAlignAllWorker). The ROI walk dominates and is GIL-bound, so
            # processes are what actually speed it up. Tiny batches, a single
            # worker, or a raw-only run (no reader to rebuild) stay in-thread.
            if (n <= 2 or workers <= 1 or self._rar is None
                    or not (self._export_overlay or self._export_gray
                            or self._export_label)):
                rows = self._run_in_thread()
            else:
                rows = self._run_process_pool(workers)
        except oasis_random.WalkCancelled:
            self.cancelled.emit()
        except Exception as exc:                       # noqa: BLE001
            self.failed.emit(str(exc))
        else:
            if rows is None:
                self.cancelled.emit()
            else:
                self.finished.emit(len(rows), self._write_manifest(rows))

    def _run_in_thread(self):
        """Sequential fallback (tiny / raw-only batches): export each image on
        this worker thread. Returns the manifest rows in job order, or ``None``
        if cancelled mid-batch."""
        n = len(self._jobs)
        rows = []
        for job in self._jobs:
            if self._cancel.is_set():
                return None
            try:
                row = overlay_export.export_one_image(
                    job, self._rar, self._root, self._poi, self._cfg,
                    self._out_dir, self._export_raw, self._export_overlay,
                    self._export_gray, self._export_label, self._score_thr,
                    cancel_cb=self._cancel.is_set)
            except oasis_random.WalkCancelled:
                return None
            rows.append(row)
            self.progress.emit(len(rows), n, str(row["image_id"]))
        return rows

    def _run_process_pool(self, workers: int):
        """Parallel path: fan the per-image export out to a spawn-based process
        pool. Each worker rebuilds its reader once via the initializer and writes
        its own PNGs. All jobs are submitted up front (workers stay busy), but
        results are consumed in *submission order* (F14: image order) so progress
        ticks top-to-bottom and the manifest is stable / matches the sequential
        output. Returns rows, or ``None`` if cancelled (partial PNGs on disk are
        kept, like the old path)."""
        n = len(self._jobs)
        rar = self._rar
        initargs = (str(rar._path), rar._init_wanted, rar._dtype,
                    rar._bbox_layer, self._root, self._poi, self._cfg,
                    str(self._out_dir), self._export_raw, self._export_overlay,
                    self._export_gray, self._export_label, self._score_thr)
        ctx = mp.get_context("spawn")
        ex = ProcessPoolExecutor(max_workers=workers, mp_context=ctx,
                                 initializer=overlay_export._export_pool_init,
                                 initargs=initargs)
        done = 0
        dropped = False
        rows = []
        try:
            futures = [ex.submit(overlay_export._export_pool_task, job)
                       for job in self._jobs]
            for fut in futures:
                if self._cancel.is_set() and not dropped:
                    for f in futures:
                        f.cancel()          # drop not-yet-started tasks
                    dropped = True
                try:
                    row = fut.result()
                except CancelledError:
                    continue                # a pending task we just dropped
                rows.append(row)
                done += 1
                self.progress.emit(done, n, str(row["image_id"]))
        finally:
            ex.shutdown(wait=True)
        if self._cancel.is_set():
            return None
        return rows

    def _write_manifest(self, rows) -> str:
        csv_path = self._out_dir / "overlay_manifest.csv"
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self._COLS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in self._COLS})
        json_path = self._out_dir / "overlay_manifest.json"
        # v3 (F24): adds the ``label_view_png`` column (human-viewable colourised
        # preview of ``label_png``). Additive — readers that key by column name
        # are unaffected; ``label_png`` stays the exact integer label map.
        manifest = {"schema": "mmh-gds-overlay-v3", "columns": self._COLS,
                    "images": rows}
        # F15: id → POI layer map so downstream can turn label_png pixels back
        # into named layers (read a region with gray[label == id]).
        if self._label_map:
            manifest["label_map"] = self._label_map
        with open(json_path, "w") as f:
            json.dump(manifest, f, indent=2)
        return str(csv_path)


# ── M5: per-image alignment export ───────────────────────────────────────────

# Column order for the alignment CSV / JSON. coarse_dx/dy_nm is the FOV-centre
# GDS coordinate from M3 (klarf_to_gds + fine-tune + origin δ); fine_dx/dy_nm is
# the per-image template-match correction from M4b (blank if not run). The
# aligned GDS position is coarse + fine. A future Recipe consumes image_id ↔
# MeasurementRecord.image_id to anchor its ROI.
ALIGNMENT_COLUMNS = [
    "image_id", "klarf_path", "gds_path", "poi_layer",
    "coarse_dx_nm", "coarse_dy_nm", "fine_dx_nm", "fine_dy_nm",
    "score", "nm_per_px",
]


def alignment_rows(images, refined, *, coarse_of, klarf_path="", gds_path="",
                   poi_layer="", nm_per_px=0.0):
    """Build the per-image alignment rows (plan M5).

    ``coarse_of(img)`` returns the coarse FOV-centre GDS ``(x, y)`` nm or
    ``None``; ``refined`` is ``{image_id: (dx_nm, dy_nm, score)}`` from M4b.
    Blank cells are emitted (not 0) for images with no coordinate / no
    fine-align run so the distinction survives a round-trip."""
    rows = []
    for img in images:
        coarse = coarse_of(img) if coarse_of else None
        ref = refined.get(getattr(img, "image_id", None))
        rows.append({
            "image_id": getattr(img, "image_id", ""),
            "klarf_path": klarf_path,
            "gds_path": gds_path,
            "poi_layer": poi_layer,
            "coarse_dx_nm": "" if coarse is None else round(coarse[0], 3),
            "coarse_dy_nm": "" if coarse is None else round(coarse[1], 3),
            "fine_dx_nm": "" if ref is None else round(ref[0], 3),
            "fine_dy_nm": "" if ref is None else round(ref[1], 3),
            "score": "" if ref is None else round(ref[2], 6),
            "nm_per_px": round(float(nm_per_px), 6),
        })
    return rows


def _median(vals: list) -> float:
    s = sorted(vals)
    n = len(s)
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def fine_align_result_rows(images, refined, fa_meta, threshold):
    """Rows for the results table (F5 M2). Each dict has image_id / score /
    dx_nm / dy_nm / used_radius / status. ``status`` layers a threshold-derived
    'low-score' on top of the worker's objective status; images that were never
    aligned get 'not-run'. ``score`` / ``dx_nm`` / ``dy_nm`` are None when no
    fine-align result exists, so the table can show blanks."""
    rows = []
    for img in images:
        iid = getattr(img, "image_id", "")
        ref = refined.get(iid)
        used_r, status = fa_meta.get(iid, (0, "not-run"))
        score = ref[2] if ref else None
        if status == "ok" and score is not None and score < threshold:
            disp = "low-score"
        else:
            disp = status
        rows.append({
            "image_id": iid,
            "score": score,
            "dx_nm": ref[0] if ref else None,
            "dy_nm": ref[1] if ref else None,
            "used_radius": int(used_r),
            "status": disp,
        })
    return rows


def score_histogram(scores, nbins: int = 10, lo: float = 0.0,
                    hi: float = 1.0) -> list:
    """Counts of scores per equal-width bin over [lo, hi] (F5 M2 C5). Returns a
    length-``nbins`` list[int]; None scores are skipped and out-of-range values
    are clamped into the end bins."""
    bins = [0] * max(1, nbins)
    if hi <= lo:
        return bins
    for s in scores:
        if s is None:
            continue
        t = (s - lo) / (hi - lo)
        idx = int(t * nbins)
        idx = max(0, min(nbins - 1, idx))
        bins[idx] += 1
    return bins


def residual_median(refined, ok_ids):
    """Median (dx, dy) over the given ok image ids (F5 M3 C2), or None if the
    set is empty / has no stored offsets."""
    xs, ys = [], []
    for iid in ok_ids:
        r = refined.get(iid)
        if r is not None:
            xs.append(r[0])
            ys.append(r[1])
    if not xs:
        return None
    return (_median(xs), _median(ys))


def synthetic_layer_specs(doc):
    """Boolean lineage of the synthetic (expression) layers, for the JSON
    export (plan M5)."""
    specs = []
    if doc is None:
        return specs
    for e in doc.entries:
        if e.key.synthetic and e.expr_text:
            specs.append({
                "name": e.key.name,
                "expr": e.expr_text,
                "bindings": {k: list(v)
                             for k, v in (e.expr_bindings or {}).items()},
            })
    return specs


def write_alignment_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ALIGNMENT_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in ALIGNMENT_COLUMNS})


def write_alignment_json(path, rows, synthetic_layers=None):
    payload = {
        "schema": "mmh-gds-alignment-v1",
        "columns": ALIGNMENT_COLUMNS,
        "alignments": rows,
        "synthetic_layers": synthetic_layers or [],
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return payload


# ── LayerPanel widget ────────────────────────────────────────────────────────

class _LayerRow(QWidget):
    """One LayerPanel row: visibility checkbox + POI toggle + colour swatch +
    label. Mutates the bound ``LayerEntry`` in place and emits ``changed`` so
    the canvas / SEM overlay redraw."""

    changed = pyqtSignal()
    poi_toggled = pyqtSignal(bool)   # M4b: this row chosen / cleared as POI
    edit_requested = pyqtSignal()    # F4: edit this synthetic layer's recipe
    delete_requested = pyqtSignal()  # F4: delete this synthetic layer

    def __init__(self, entry: "LayerEntry",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._entry = entry
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 2, 6, 2)
        h.setSpacing(6)

        self._chk = QCheckBox(self)
        self._chk.setChecked(entry.visible)
        self._chk.setToolTip("Show / hide this layer")
        self._chk.toggled.connect(self._on_visible)
        h.addWidget(self._chk)

        self.poi_btn = QToolButton(self)
        self.poi_btn.setText("POI")
        self.poi_btn.setCheckable(True)
        self.poi_btn.setFixedSize(34, 18)
        self.poi_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.poi_btn.setStyleSheet(_POI_BTN_QSS)
        self.poi_btn.setToolTip(
            "Add / remove this layer as a POI alignment template (F3 fine "
            "align). Several layers can be POIs — they composite into one "
            "synthetic template, each at its own grey level.")
        self.poi_btn.toggled.connect(self.poi_toggled)
        h.addWidget(self.poi_btn)

        self._swatch = QPushButton(self)
        self._swatch.setFixedSize(16, 16)
        self._swatch.setCursor(Qt.CursorShape.PointingHandCursor)
        self._swatch.setToolTip("Click to change colour")
        self._swatch.clicked.connect(self._on_color)
        self._apply_swatch()
        h.addWidget(self._swatch)

        # Prefix the OASIS LAYERNAME when the file declares one (F3 M2):
        # "METAL1 (L17/D0)"; raw layers with no name fall back to "L17/D0".
        label = _entry_label(entry)
        n_poly = len(entry.polygons)
        if n_poly:
            label = f"{label}  ·  {n_poly}"
        self._lbl = QLabel(label, self)
        self._lbl.setStyleSheet(f"font-size: {_FS_LABEL}px;")
        self._lbl.setToolTip(label)
        h.addWidget(self._lbl, 1)

        # F4: synthetic (expression) layers get inline edit / delete controls;
        # double-clicking the row also opens the editor.
        if entry.key.synthetic:
            self._edit_btn = QToolButton(self)
            self._edit_btn.setText("✎")
            self._edit_btn.setFixedSize(20, 18)
            self._edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._edit_btn.setToolTip("Edit this expression")
            self._edit_btn.clicked.connect(self.edit_requested)
            h.addWidget(self._edit_btn)

            self._del_btn = QToolButton(self)
            self._del_btn.setText("✕")
            self._del_btn.setFixedSize(20, 18)
            self._del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._del_btn.setToolTip("Delete this expression layer")
            self._del_btn.clicked.connect(self.delete_requested)
            h.addWidget(self._del_btn)

    def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        if self._entry.key.synthetic:
            self.edit_requested.emit()
        super().mouseDoubleClickEvent(ev)

    def _apply_swatch(self) -> None:
        c = self._entry.color
        self._swatch.setStyleSheet(
            f"background: {c.name()}; border: 1px solid #b8a890; "
            f"border-radius: 5px;")

    def _on_visible(self, on: bool) -> None:
        self._entry.visible = bool(on)
        self.changed.emit()

    def _on_color(self) -> None:
        new_color = QColorDialog.getColor(
            self._entry.color, self, f"Color for {self._entry.key.label()}")
        if new_color.isValid():
            self._entry.color = new_color
            self._apply_swatch()
            self.changed.emit()


class LayerPanel(QFrame):
    """Left-side list of layers with toggle + color editing.

    Signals:
        layers_changed — any visibility / color change. Canvas listens and
        redraws.
    """

    layers_changed = pyqtSignal()
    add_expression_requested = pyqtSignal()   # M2.6: "+ Expression…" clicked
    edit_expression_requested = pyqtSignal(str)    # F4: edit recipe by name
    delete_expression_requested = pyqtSignal(str)  # F4: delete recipe by name
    pois_changed = pyqtSignal(object)         # F3: list[LayerEntry] (POI set)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("leftPanel")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFixedWidth(260)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = QLabel("LAYERS")
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        self.list = QListWidget(self)
        self.list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        _config_list(self.list)
        layout.addWidget(self.list, 1)

        self._expr_btn = QPushButton("+ Expression…", self)
        self._expr_btn.setToolTip(
            "Compose a synthetic layer from a Boolean expression, e.g.\n"
            "[(A > W:5) & B] < H:5")
        self._expr_btn.clicked.connect(self.add_expression_requested)
        # U2: Boolean expressions can only bind to raw layers — keep the
        # button greyed out until a document is loaded.
        self._expr_btn.setEnabled(False)
        # U8: a discreet "?" badge replaces the always-visible hint strip
        # ("checkbox / POI / swatch") — same wording in the tooltip,
        # without permanently using ~32 px of vertical space.
        self._row_help_btn = QToolButton(self)
        self._row_help_btn.setText("?")
        self._row_help_btn.setAutoRaise(True)
        self._row_help_btn.setCursor(Qt.CursorShape.WhatsThisCursor)
        self._row_help_btn.setStyleSheet(
            f"QToolButton {{ color:{_TK_TEXT_HINT.name()}; "
            f"font-size:{_FS_LABEL}px; font-weight:700; "
            f"border:1px solid {_TK_BORDER.name()}; border-radius:9px; "
            f"min-width:18px; max-width:18px; min-height:18px; max-height:18px; "
            f"padding:0; }}"
            f"QToolButton:hover {{ background:{_TK_BG_PANEL.name()}; "
            f"color:{_TK_ACCENT_DK.name()}; }}")
        self._row_help_btn.setToolTip(
            "Row controls:\n"
            "• checkbox — show / hide this layer in the overlay\n"
            "• POI — use this layer as the fine-align template\n"
            "• coloured swatch — click to recolour the layer")
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(8, 6, 8, 6)
        btn_row.setSpacing(6)
        btn_row.addWidget(self._expr_btn, 1)
        btn_row.addWidget(self._row_help_btn)
        layout.addLayout(btn_row)

        self._doc: Optional[GdsDocument] = None
        self._rows: list[_LayerRow] = []
        self._poi_entries: list[LayerEntry] = []
        self._show_empty_hint()

    def _show_empty_hint(self) -> None:
        """Muted placeholder when no layers are loaded yet (icon + title +
        hint, so the empty LAYERS column reads as an onboarding cue rather
        than a blank list)."""
        self.list.clear()

        # 圖示行
        icon_item = QListWidgetItem()
        icon_item.setFlags(Qt.ItemFlag.NoItemFlags)
        icon_item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.list.addItem(icon_item)

        # 主文
        title_item = QListWidgetItem("Open an OASIS")
        title_item.setFlags(Qt.ItemFlag.NoItemFlags)
        title_item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
        title_item.setForeground(QColor(_TK_TEXT_SEC))
        font = title_item.font()
        font.setPixelSize(_FS_LABEL)
        font.setBold(True)
        title_item.setFont(font)
        self.list.addItem(title_item)

        # 次文 — primary path + alternative cache restore (T3: surface
        # that File menu also has Load cache… so users who saved one
        # before don't think they have to re-open the OASIS).
        for sub in ("toolbar → Open OASIS…",
                    "or  File → Load cache…"):
            hint_item = QListWidgetItem(sub)
            hint_item.setFlags(Qt.ItemFlag.NoItemFlags)
            hint_item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            hint_item.setForeground(QColor(_TK_TEXT_HINT))
            font2 = hint_item.font()
            font2.setPixelSize(_FS_CAPTION)
            hint_item.setFont(font2)
            self.list.addItem(hint_item)

    def set_document(self, doc: Optional[GdsDocument]) -> None:
        self._doc = doc
        self.list.clear()
        self._rows = []
        self._poi_entries = []
        # U2: + Expression… only makes sense once raw layers exist to bind.
        self._expr_btn.setEnabled(doc is not None and bool(doc.entries))
        if doc is None or not doc.entries:
            self._show_empty_hint()
            self.pois_changed.emit([])
            return
        for entry in doc.entries:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, entry)
            row = _LayerRow(entry)
            row.changed.connect(self.layers_changed)
            row.poi_toggled.connect(
                lambda on, r=row, e=entry: self._on_poi_toggled(on, r, e))
            if entry.key.synthetic:
                row.edit_requested.connect(
                    lambda n=entry.key.name: self.edit_expression_requested.emit(n))
                row.delete_requested.connect(
                    lambda n=entry.key.name: self.delete_expression_requested.emit(n))
            item.setSizeHint(row.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
            self._rows.append(row)
        self.pois_changed.emit([])

    def _on_poi_toggled(self, on: bool, row: "_LayerRow",
                        entry: "LayerEntry") -> None:
        """F3: POI is multi-select — several layers can be active POIs at once.
        Rebuild the set from the rows' checked state (in panel order) so the
        composite ordering is stable. Driven by row state rather than list
        membership because ``LayerEntry`` holds NumPy arrays, whose dataclass
        ``__eq__`` makes ``in`` / ``remove`` raise on array truth-value."""
        ordered = [r._entry for r in self._rows if r.poi_btn.isChecked()]
        self._poi_entries = ordered
        self.pois_changed.emit(list(ordered))

    def check_pois(self, keys) -> None:
        """Re-check the POI toggle on rows whose layer key is in ``keys``,
        emitting ``pois_changed`` once (F5 M4 setup restore)."""
        keyset = set(keys)
        for r in self._rows:
            want = r._entry.key.key() in keyset
            if r.poi_btn.isChecked() != want:
                r.poi_btn.blockSignals(True)
                r.poi_btn.setChecked(want)
                r.poi_btn.blockSignals(False)
        ordered = [r._entry for r in self._rows if r.poi_btn.isChecked()]
        self._poi_entries = ordered
        self.pois_changed.emit(list(ordered))

    def poi_entries(self) -> list["LayerEntry"]:
        return list(self._poi_entries)

    def raw_layer_keys(self) -> list[tuple[int, int]]:
        """``(layer, datatype)`` pairs of the non-synthetic layers, for
        the expression-binding dropdowns."""
        if self._doc is None:
            return []
        return [(e.key.layer, e.key.datatype)
                for e in self._doc.entries if not e.key.synthetic]

    def refresh(self) -> None:
        self.list.viewport().update()


# ── Expression layer dialog (M2.6) ───────────────────────────────────────────


class _ExprPreview(QWidget):
    """Fit-to-view mini canvas for the compose dialog (F4): the expression
    result (filled highlight) over its bound raw layers (thin outlines), so
    the layer can be reviewed in-dialog without returning to the main window."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(200)
        self.setStyleSheet(
            "background:#1a1712; border:1px solid #4a4030; border-radius:6px;")
        self._result: list = []
        self._context: list = []
        self._bbox: Optional[tuple] = None
        self._msg = "press Preview to render the layer here"

    def set_message(self, msg: str) -> None:
        self._msg = msg
        self.update()

    def set_data(self, data: Optional[dict]) -> None:
        if not data:
            self._result, self._context, self._bbox = [], [], None
        else:
            self._result = data.get("result", [])
            self._context = data.get("context", [])
            self._bbox = self._fit_bbox(data)
        self.update()

    @staticmethod
    def _fit_bbox(data: dict) -> Optional[tuple]:
        allp = list(data.get("result", []))
        for polys, _ in data.get("context", []):
            allp += polys
        x0 = y0 = float("inf")
        x1 = y1 = float("-inf")
        for poly in allp:
            a = np.asarray(poly, dtype=float)
            if a.size == 0:
                continue
            x0 = min(x0, a[:, 0].min()); y0 = min(y0, a[:, 1].min())
            x1 = max(x1, a[:, 0].max()); y1 = max(y1, a[:, 1].max())
        if x0 == float("inf"):
            return data.get("fov")
        px = (x1 - x0) * 0.05 or 1.0
        py = (y1 - y0) * 0.05 or 1.0
        return (x0 - px, y0 - py, x1 + px, y1 + py)

    def paintEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        if self._bbox is None or not (self._result or self._context):
            p.setPen(QPen(QColor("#8a7660")))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._msg)
            return
        x0, y0, x1, y1 = self._bbox
        bw = max(x1 - x0, 1e-6)
        bh = max(y1 - y0, 1e-6)
        margin = 10
        aw = rect.width() - 2 * margin
        ah = rect.height() - 2 * margin
        s = min(aw / bw, ah / bh)
        ox = margin + (aw - bw * s) / 2.0
        oy = margin + (ah - bh * s) / 2.0

        def to_polygon(poly):
            pts = [QPointF(ox + (float(pt[0]) - x0) * s,
                           oy + (y1 - float(pt[1])) * s) for pt in poly]
            return QPolygonF(pts)

        p.setBrush(Qt.BrushStyle.NoBrush)
        for polys, color in self._context:
            pen = QPen(QColor(color)); pen.setWidthF(1.0)
            p.setPen(pen)
            for poly in polys:
                if len(poly) >= 2:
                    p.drawPolygon(to_polygon(poly))
        hl = QColor("#ff5fb0")
        fill = QColor(hl); fill.setAlpha(90)
        p.setBrush(QBrush(fill))
        pen = QPen(hl); pen.setWidthF(1.6)
        p.setPen(pen)
        for poly in self._result:
            if len(poly) >= 2:
                p.drawPolygon(to_polygon(poly))


class ExpressionLayerDialog(QDialog):
    """Compose / edit a synthetic layer from a Boolean expression (F4).

    The expression uses letters (``A``, ``B``, …); each referenced letter is
    bound — via a dropdown — to a **raw layer** or **another synthetic layer**
    (nested composition). To build expressions without memorising letters, the
    palette offers clickable layer / synthetic chips (each inserts the next
    free letter and pre-binds it) and operator buttons (``&`` ``|`` ``-`` ``~``
    grow / shrink / brackets) that insert at the cursor. The expression is
    validated live: the Save button is disabled and an inline message shown
    until it parses and every letter is bound.

    ``preview_cb(name, expr, bindings) -> (ok, msg, data)`` (optional) is
    called by the Preview button; the result is rendered in the embedded
    preview canvas (the dialog stays open) so the layer is reviewed in place
    and only committed on Save. ``recipe`` (optional) pre-fills the form for
    editing. ``bindings`` are tagged: ``("raw", layer, datatype)`` /
    ``("ref", name)``.
    """

    _LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def __init__(self, parent: Optional[QWidget],
                 layer_keys: list[tuple[int, int]],
                 ref_names: Optional[list[str]] = None,
                 preview_cb=None,
                 recipe: Optional[dict] = None) -> None:
        super().__init__(parent)
        self._editing = recipe is not None
        self.setWindowTitle(
            "Edit expression layer" if self._editing
            else "Compose expression layer")
        self.setMinimumWidth(_capped_min_width(460))
        self._layer_keys = list(layer_keys)
        self._ref_names = list(ref_names or [])
        self._preview_cb = preview_cb
        self._combos: dict[str, QComboBox] = {}
        self._bind_rows: dict[str, QWidget] = {}
        self._pending_bind: Optional[tuple] = None

        v = QVBoxLayout(self)

        v.addWidget(QLabel("Layer name"))
        self._name_edit = QLineEdit(self)
        self._name_edit.setText(recipe["name"] if self._editing else "L0")
        self._name_edit.textChanged.connect(self._revalidate)
        v.addWidget(self._name_edit)

        v.addWidget(QLabel("Expression"))
        self._expr_edit = QLineEdit(self)
        self._expr_edit.setPlaceholderText("[(A > W:5) & B] < H:5")
        if self._editing:
            self._expr_edit.setText(recipe.get("expr", ""))
        self._expr_edit.textChanged.connect(self._on_expr_changed)
        v.addWidget(self._expr_edit)

        # Insert palette: layer / synthetic chips + operator buttons.
        v.addWidget(self._build_palette())

        v.addWidget(QLabel("Bindings"))
        self._bind_box = QVBoxLayout()
        self._bind_box.setContentsMargins(0, 0, 0, 0)
        v.addLayout(self._bind_box)

        prev_row = QHBoxLayout()
        self._preview_btn = QPushButton("Preview", self)
        self._preview_btn.clicked.connect(self._on_preview)
        prev_row.addWidget(self._preview_btn)
        self._result_lbl = QLabel("", self)
        self._result_lbl.setWordWrap(True)
        prev_row.addWidget(self._result_lbl, 1)
        v.addLayout(prev_row)

        self._preview = _ExprPreview(self)
        v.addWidget(self._preview)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel, self)
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self.reject)
        v.addWidget(self._buttons)

        # Seed binding rows + selections from the recipe (edit) or expression.
        self._rebuild_bindings()
        if self._editing:
            self._apply_recipe_bindings(recipe.get("bindings", {}))
        self._revalidate()

    # ── insert palette ───────────────────────────────────────────────────────
    def _build_palette(self) -> QWidget:
        box = QWidget(self)
        outer = QVBoxLayout(box)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        chips = QHBoxLayout()
        chips.setContentsMargins(0, 0, 0, 0)
        chips.addWidget(QLabel("insert:"))
        for (ly, dt) in self._layer_keys:
            b = QToolButton(box)
            b.setText(f"L{ly}/D{dt}")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip("Insert a new letter bound to this raw layer")
            b.clicked.connect(
                lambda _=False, val=("raw", ly, dt): self._insert_layer(val))
            chips.addWidget(b)
        for nm in self._ref_names:
            b = QToolButton(box)
            b.setText(f"[{nm}]")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip("Insert a new letter bound to this synthetic layer")
            b.clicked.connect(
                lambda _=False, val=("ref", nm): self._insert_layer(val))
            chips.addWidget(b)
        chips.addStretch(1)
        outer.addLayout(chips)

        ops = QHBoxLayout()
        ops.setContentsMargins(0, 0, 0, 0)
        for tok, tip in [("&", "intersection"), ("|", "union"),
                         ("-", "difference"), ("~", "complement"),
                         (" > W:", "grow width / X (nm per side)"),
                         (" > H:", "grow height / Y (nm per side)"),
                         (" < W:", "shrink width / X (nm per side)"),
                         (" < H:", "shrink height / Y (nm per side)"),
                         ("(", ""), (")", "")]:
            b = QToolButton(box)
            b.setText(tok.strip() or tok)
            if tip:
                b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, t=tok: self._insert_text(t))
            ops.addWidget(b)
        ops.addStretch(1)
        outer.addLayout(ops)
        return box

    def _insert_text(self, text: str) -> None:
        self._expr_edit.insert(text)
        self._expr_edit.setFocus()

    def _next_free_letter(self) -> Optional[str]:
        used = set(self._referenced_letters())
        for ch in self._LETTERS:
            if ch not in used:
                return ch
        return None

    def _insert_layer(self, val: tuple) -> None:
        """Insert the next free letter at the cursor and pre-bind it to
        ``val`` (a tagged binding)."""
        letter = self._next_free_letter()
        if letter is None:
            return
        self._pending_bind = (letter, val)
        self._expr_edit.insert(letter)
        self._expr_edit.setFocus()

    # ── binding rows ────────────────────────────────────────────────────────
    def _referenced_letters(self) -> list[str]:
        try:
            _, ast = gds_boolean.parse_expression(self._expr_edit.text())
        except Exception:
            return []
        return sorted(gds_boolean.referenced_layers(ast))

    def _binding_options(self) -> list[tuple[str, tuple]]:
        """(label, tagged-value) for every bindable source."""
        opts = [(f"L{ly}/D{dt}", ("raw", ly, dt))
                for (ly, dt) in self._layer_keys]
        opts += [(f"[{nm}]", ("ref", nm)) for nm in self._ref_names]
        return opts

    def _on_expr_changed(self) -> None:
        self._rebuild_bindings()
        self._revalidate()

    def _rebuild_bindings(self) -> None:
        letters = self._referenced_letters()
        options = self._binding_options()
        for letter in letters:
            if letter in self._combos:
                continue
            combo = QComboBox(self)
            for label, val in options:
                combo.addItem(label, val)
            # Honour a pending pre-bind from a chip click.
            pend = getattr(self, "_pending_bind", None)
            if pend is not None and pend[0] == letter:
                self._select_combo(combo, pend[1])
                self._pending_bind = None
            combo.currentIndexChanged.connect(self._revalidate)
            row = QWidget(self)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.addWidget(QLabel(f"{letter} ="))
            rl.addWidget(combo, 1)
            self._combos[letter] = combo
            self._bind_rows[letter] = row
            self._bind_box.addWidget(row)
        if letters:
            for letter in list(self._combos):
                if letter not in letters:
                    row = self._bind_rows.pop(letter)
                    self._combos.pop(letter)
                    row.setParent(None)
                    row.deleteLater()

    @staticmethod
    def _select_combo(combo: QComboBox, val: tuple) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == val:
                combo.setCurrentIndex(i)
                return

    def _apply_recipe_bindings(self, bindings: dict) -> None:
        for letter, val in bindings.items():
            combo = self._combos.get(letter)
            if combo is not None:
                self._select_combo(combo, gds_boolean.normalize_binding(val))

    # ── getters ─────────────────────────────────────────────────────────────
    def name(self) -> str:
        return self._name_edit.text().strip()

    def expression(self) -> str:
        return self._expr_edit.text().strip()

    def bindings(self) -> dict[str, tuple]:
        return {letter: tuple(combo.currentData())
                for letter, combo in self._combos.items()
                if combo.currentData() is not None}

    # ── validation ───────────────────────────────────────────────────────────
    def _validate(self) -> Optional[str]:
        """Return an error string, or None if the form is valid."""
        if not self.name():
            return "Give the layer a name."
        if not self.expression():
            return "Enter an expression."
        try:
            _, ast = gds_boolean.parse_expression(self.expression())
        except Exception as exc:
            return f"Invalid expression: {exc}"
        refs = gds_boolean.referenced_layers(ast)
        if not self._binding_options():
            return "No layers available to bind."
        missing = [r for r in refs if r not in self.bindings()]
        if missing:
            return f"Bind: {', '.join(sorted(missing))}"
        return None

    def _revalidate(self) -> None:
        err = self._validate()
        ok_btn = self._buttons.button(QDialogButtonBox.StandardButton.Save)
        ok_btn.setEnabled(err is None)
        if err:
            self._result_lbl.setText(err)
            self._result_lbl.setStyleSheet(f"color: {_TK_DANGER};")
        elif not self._result_lbl.text() or self._result_lbl.text() in (
                "Give the layer a name.", "Enter an expression."):
            self._result_lbl.setText("ready")
            self._result_lbl.setStyleSheet(f"color: {_TK_TEXT_SEC};")

    def _on_preview(self) -> None:
        err = self._validate()
        if err:
            self._result_lbl.setText(err)
            self._result_lbl.setStyleSheet(f"color: {_TK_DANGER};")
            return
        if self._preview_cb is None:
            return
        ok, msg, data = self._preview_cb(self.name(), self.expression(),
                                         self.bindings())
        self._result_lbl.setText(msg)
        self._result_lbl.setStyleSheet(
            f"color: {_TK_SUCCESS};" if ok else f"color: {_TK_DANGER};")
        self._preview.set_data(data if ok else None)
        if not ok:
            self._preview.set_message(msg)

    def _on_accept(self) -> None:
        err = self._validate()
        if err:
            QMessageBox.warning(self, "Cannot create layer", err)
            return
        self.accept()


# ── GdsCanvas widget (pan/zoom polygon viewer) ───────────────────────────────


class GdsCanvas(QWidget):
    """Pan/zoom canvas drawing the GDS document's visible layers.

    Internal state:
        _zoom — px per nm
        _pan_nm — (x, y) nm at viewport center
    Mouse: left-drag to pan, wheel to zoom around cursor.
    """

    cursor_pos_nm = pyqtSignal(float, float)  # emitted on mousemove
    defect_clicked = pyqtSignal(str)          # M7-ov: a defect dot was clicked

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(400, 400)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAutoFillBackground(False)

        self._doc: Optional[GdsDocument] = None
        self._zoom: float = 1.0       # px per nm
        self._pan_nm: tuple[float, float] = (0.0, 0.0)
        self._dragging = False
        self._last_drag_pt: Optional[QPointF] = None
        self._last_drawn: int = 0
        self._last_capped: bool = False
        # M3: FOV marker (cx, cy, w, h) in nm, drawn as a dashed box; None hides.
        self._fov_marker: Optional[tuple[float, float, float, float]] = None
        # M7-ov: defect map + HUD state.
        self._defects: list = []          # (image_id, gx, gy, score|None)
        self._current_defect: Optional[str] = None
        self._cursor_screen: Optional[QPointF] = None
        self._fit_zoom: float = 1.0       # zoom that == "fit", for the HUD ×
        self._DOT_HIT_PX = 7.0

    # ── public API ─────────────────────────────────────────────────────────
    def set_document(self, doc: Optional[GdsDocument]) -> None:
        self._doc = doc
        if doc is not None and doc.entries:
            self.fit_to_bbox()
        self.update()

    def refresh(self) -> None:
        self.update()

    def fit_to_bbox(self) -> None:
        if self._doc is None or not self._doc.entries:
            return
        self.set_view_to_bbox(*self._doc.bbox_nm)

    def set_view_to_bbox(self, x0: float, y0: float,
                         x1: float, y1: float) -> None:
        """Pan + zoom so the world bbox ``(x0, y0, x1, y1)`` fills the
        viewport (with a small margin). Used both by Fit view and by the
        'fit to all defect positions' overview (M3 auto-jump)."""
        w = max(1.0, x1 - x0)
        h = max(1.0, y1 - y0)
        vw = max(1, self.width() - 20)
        vh = max(1, self.height() - 20)
        self._zoom = min(vw / w, vh / h)
        self._fit_zoom = self._zoom        # reference for the HUD zoom factor
        self._pan_nm = (0.5 * (x0 + x1), 0.5 * (y0 + y1))
        self.update()

    def set_defects(self, defects: list, current_id: Optional[str] = None) -> None:
        """Defect dots for the overview map (M7-ov #1): list of
        ``(image_id, gx_nm, gy_nm, score|None)``. ``current_id`` is ringed."""
        self._defects = list(defects)
        self._current_defect = current_id
        self.update()

    # ── coord transforms ───────────────────────────────────────────────────
    def _world_to_screen(self, x_nm: float, y_nm: float) -> tuple[float, float]:
        cx, cy = self._pan_nm
        sx = (x_nm - cx) * self._zoom + self.width() * 0.5
        # Flip Y: image y grows downward; world y grows upward.
        sy = self.height() * 0.5 - (y_nm - cy) * self._zoom
        return sx, sy

    def _screen_to_world(self, sx: float, sy: float) -> tuple[float, float]:
        cx, cy = self._pan_nm
        x_nm = (sx - self.width() * 0.5) / self._zoom + cx
        y_nm = -(sy - self.height() * 0.5) / self._zoom + cy
        return x_nm, y_nm

    def viewport_bbox_nm(self) -> tuple[float, float, float, float]:
        """Current visible region as ``(x0, y0, x1, y1)`` nm. Used as the
        live FOV when previewing / evaluating expression layers (M2.6)."""
        x_a, y_a = self._screen_to_world(0, 0)
        x_b, y_b = self._screen_to_world(self.width(), self.height())
        return (min(x_a, x_b), min(y_a, y_b), max(x_a, x_b), max(y_a, y_b))

    def set_fov_marker(self, cx: float, cy: float,
                       fov_w: float, fov_h: float) -> None:
        self._fov_marker = (cx, cy, fov_w, fov_h) if fov_w > 0 and fov_h > 0 else None
        self.update()

    def clear_fov_marker(self) -> None:
        self._fov_marker = None
        self.update()

    # ── events ─────────────────────────────────────────────────────────────
    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            pos = ev.position()
            hit = self._hit_test_defect(pos.x(), pos.y())
            if hit is not None:               # M7-ov #2: click a dot -> select
                self.defect_clicked.emit(hit)
                return                         # don't start a pan
            self._dragging = True
            self._last_drag_pt = pos
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self._last_drag_pt = None
        super().mouseReleaseEvent(ev)

    def mouseDoubleClickEvent(self, ev: QMouseEvent) -> None:  # type: ignore[override]
        """Double-click: on a defect dot → zoom in to its FOV region; on empty
        space → fit to all defects (overview reset) (M7-ov #5)."""
        if ev.button() != Qt.MouseButton.LeftButton:
            super().mouseDoubleClickEvent(ev)
            return
        pos = ev.position()
        hit = self._hit_test_defect(pos.x(), pos.y())
        if hit is not None and self._fov_marker is not None:
            cx, cy, fw, fh = self._fov_marker
            mw = max(fw, 1.0) * 1.5
            mh = max(fh, 1.0) * 1.5
            self.set_view_to_bbox(cx - mw, cy - mh, cx + mw, cy + mh)
        else:
            self.fit_to_defects()
        self._dragging = False
        self._last_drag_pt = None

    def fit_to_defects(self) -> None:
        """Fit the view to all defect dots (or the document bbox if none)."""
        if not self._defects:
            self.fit_to_bbox()
            return
        xs = [d[1] for d in self._defects]
        ys = [d[2] for d in self._defects]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        px = max((x1 - x0) * 0.1, 1000.0)
        py = max((y1 - y0) * 0.1, 1000.0)
        self.set_view_to_bbox(x0 - px, y0 - py, x1 + px, y1 + py)

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        pos = ev.position()
        self._cursor_screen = pos
        if self._dragging and self._last_drag_pt is not None:
            dx = pos.x() - self._last_drag_pt.x()
            dy = pos.y() - self._last_drag_pt.y()
            cx, cy = self._pan_nm
            self._pan_nm = (cx - dx / self._zoom, cy + dy / self._zoom)
            self._last_drag_pt = pos
        else:
            # Pointing-hand over a clickable defect dot.
            over = self._hit_test_defect(pos.x(), pos.y()) is not None
            self.setCursor(Qt.CursorShape.PointingHandCursor if over
                           else Qt.CursorShape.ArrowCursor)
        x_nm, y_nm = self._screen_to_world(pos.x(), pos.y())
        self.cursor_pos_nm.emit(x_nm, y_nm)
        self.update()                          # refresh HUD cursor readout
        super().mouseMoveEvent(ev)

    def leaveEvent(self, ev) -> None:  # type: ignore[override]
        self._cursor_screen = None
        self.update()

    def wheelEvent(self, ev: QWheelEvent) -> None:
        steps = ev.angleDelta().y() / 120.0
        if steps == 0:
            return
        factor = 1.25 ** steps
        # Anchor zoom at the cursor.
        pos = ev.position()
        before_x, before_y = self._screen_to_world(pos.x(), pos.y())
        self._zoom = max(1e-6, self._zoom * factor)
        after_x, after_y = self._screen_to_world(pos.x(), pos.y())
        cx, cy = self._pan_nm
        self._pan_nm = (cx + (before_x - after_x), cy + (before_y - after_y))
        self.update()

    def resizeEvent(self, ev) -> None:  # type: ignore[override]
        super().resizeEvent(ev)

    # ── paint ──────────────────────────────────────────────────────────────
    def paintEvent(self, ev) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), _TK_CANVAS_BG)

        self._draw_grid(p)
        self._draw_layers(p)
        self._draw_defects(p)
        self._draw_fov_marker(p)
        self._draw_origin(p)
        self._draw_scale_bar(p)
        self._draw_hud(p)
        p.end()

    @staticmethod
    def _score_color(score) -> QColor:
        if score is None:
            return QColor("#b8a890")          # not aligned yet
        if score >= 0.7:
            return QColor(_TK_SUCCESS)
        if score >= 0.5:
            return QColor("#b8860b")
        return QColor(_TK_DANGER)

    def _draw_defects(self, p: QPainter) -> None:
        """Defect map (M7-ov #1): a dot per SEM defect, coloured by fine-align
        score; the current image is ringed. Viewport-culled."""
        if not self._defects:
            return
        w, h = self.width(), self.height()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for image_id, gx, gy, score in self._defects:
            sx, sy = self._world_to_screen(gx, gy)
            if sx < -8 or sx > w + 8 or sy < -8 or sy > h + 8:
                continue                       # off-screen: skip
            col = self._score_color(score)
            p.setPen(QPen(QColor("#ffffff"), 1.0))
            p.setBrush(QBrush(col))
            p.drawEllipse(QPointF(sx, sy), 4.5, 4.5)
            if image_id == self._current_defect:
                p.setPen(QPen(_TK_ACCENT_DK, 1.6))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(sx, sy), 8.0, 8.0)

    def _draw_hud(self, p: QPainter) -> None:
        """Corner HUD (M7-ov #3): zoom factor (top-right) + cursor coord in µm
        (top-left), matching SemViewer."""
        if self._doc is None and not self._defects:
            return
        p.setPen(QPen(_TK_TEXT_PRI))
        factor = self._zoom / self._fit_zoom if self._fit_zoom > 0 else 1.0
        p.drawText(self.rect().adjusted(8, 6, -8, -8),
                   Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
                   f"{factor:.2f}×")
        if self._cursor_screen is not None:
            wx, wy = self._screen_to_world(self._cursor_screen.x(),
                                           self._cursor_screen.y())
            p.setPen(QPen(_TK_TEXT_HINT))
            p.drawText(self.rect().adjusted(8, 6, -8, -8),
                       Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                       f"{wx / 1e3:,.3f}, {wy / 1e3:,.3f} µm")

    def _hit_test_defect(self, sx: float, sy: float) -> Optional[str]:
        """image_id of the defect dot under (sx, sy), or None."""
        best = None
        best_d2 = self._DOT_HIT_PX ** 2
        for image_id, gx, gy, _score in self._defects:
            dx, dy = self._world_to_screen(gx, gy)
            d2 = (dx - sx) ** 2 + (dy - sy) ** 2
            if d2 <= best_d2:
                best_d2 = d2
                best = image_id
        return best

    def _draw_fov_marker(self, p: QPainter) -> None:
        if self._fov_marker is None:
            return
        cx, cy, w, h = self._fov_marker
        sx0, sy0 = self._world_to_screen(cx - w / 2.0, cy + h / 2.0)
        sx1, sy1 = self._world_to_screen(cx + w / 2.0, cy - h / 2.0)
        # Translucent fill + thicker dashed outline make the box easy to spot
        # even on a whole-chip view (M7-ov #4).
        fill = QColor(_TK_ACCENT_DK)
        fill.setAlpha(38)
        p.setBrush(QBrush(fill))
        p.setPen(QPen(_TK_ACCENT_DK, 1.8, Qt.PenStyle.DashLine))
        p.drawRect(QRectF(sx0, sy0, sx1 - sx0, sy1 - sy0))
        # Crosshair at the FOV centre.
        ccx, ccy = self._world_to_screen(cx, cy)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_TK_ACCENT_DK, 1.4))
        p.drawLine(QPointF(ccx - 7, ccy), QPointF(ccx + 7, ccy))
        p.drawLine(QPointF(ccx, ccy - 7), QPointF(ccx, ccy + 7))
        # Off-screen indicator: if the FOV centre is panned out of view, draw an
        # edge arrow pointing back to it (M7-ov #4).
        if not (0 <= ccx <= self.width() and 0 <= ccy <= self.height()):
            self._draw_offscreen_arrow(p, ccx, ccy)

    def _draw_offscreen_arrow(self, p: QPainter, tx: float, ty: float) -> None:
        import math
        w, h = self.width(), self.height()
        m = 18.0                                   # inset from the edge
        vcx, vcy = w / 2.0, h / 2.0
        dx, dy = tx - vcx, ty - vcy
        if dx == 0 and dy == 0:
            return
        ts = []
        if dx != 0:
            ex = (w - m) if dx > 0 else m
            ts.append((ex - vcx) / dx)
        if dy != 0:
            ey = (h - m) if dy > 0 else m
            ts.append((ey - vcy) / dy)
        t = min(t for t in ts if t > 0)
        ax, ay = vcx + dx * t, vcy + dy * t
        ang = math.atan2(dy, dx)
        size = 9.0
        tip = QPointF(ax + math.cos(ang) * size, ay + math.sin(ang) * size)
        left = QPointF(ax + math.cos(ang + 2.5) * size,
                       ay + math.sin(ang + 2.5) * size)
        right = QPointF(ax + math.cos(ang - 2.5) * size,
                        ay + math.sin(ang - 2.5) * size)
        tri = QPolygonF([tip, left, right])
        p.setPen(QPen(QColor("#ffffff"), 1.0))
        p.setBrush(QBrush(_TK_ACCENT_DK))
        p.drawPolygon(tri)

    def _draw_grid(self, p: QPainter) -> None:
        # Choose a grid step (nm) that yields ~50–200 px on screen.
        target_px = 80.0
        nm_step = target_px / max(1e-9, self._zoom)
        nm_step = _nice_step(nm_step)
        if nm_step <= 0:
            return
        x_nm_left, y_nm_top = self._screen_to_world(0, 0)
        x_nm_right, y_nm_bot = self._screen_to_world(self.width(), self.height())
        x0 = np.floor(min(x_nm_left, x_nm_right) / nm_step) * nm_step
        x1 = np.ceil(max(x_nm_left, x_nm_right) / nm_step) * nm_step
        y0 = np.floor(min(y_nm_top, y_nm_bot) / nm_step) * nm_step
        y1 = np.ceil(max(y_nm_top, y_nm_bot) / nm_step) * nm_step

        pen_faint = QPen(_TK_GRID_FAINT, 1)
        pen_bold = QPen(_TK_GRID_BOLD, 1)
        p.setPen(pen_faint)
        x = x0
        while x <= x1:
            sx, _ = self._world_to_screen(x, 0)
            p.setPen(pen_bold if abs(x) < 1e-6 else pen_faint)
            p.drawLine(QPointF(sx, 0), QPointF(sx, self.height()))
            x += nm_step
        y = y0
        while y <= y1:
            _, sy = self._world_to_screen(0, y)
            p.setPen(pen_bold if abs(y) < 1e-6 else pen_faint)
            p.drawLine(QPointF(0, sy), QPointF(self.width(), sy))
            y += nm_step

    # Hard cap on polygons drawn per layer per frame. Massive layouts pile
    # up tens of millions of polygons; rendering all of them would freeze the
    # paint thread for seconds. Above this cap we draw the first N (already
    # culled to the viewport) and surface a hint via the status overlay.
    _DRAW_CAP_PER_LAYER = 80_000

    def _draw_layers(self, p: QPainter) -> None:
        if self._doc is None:
            # When defects are loaded (KLARF) but no ROI geometry yet, the
            # defect map is the content — don't cover it with empty-state text.
            if not self._defects:
                self._draw_empty_state(p)
            return
        visible = self._doc.visible_entries()
        if not visible:
            self._draw_empty_state(p, msg="all layers hidden")
            return

        # Viewport bbox in nm.
        x_a, y_a = self._screen_to_world(0, 0)
        x_b, y_b = self._screen_to_world(self.width(), self.height())
        vx0, vx1 = (x_a, x_b) if x_a <= x_b else (x_b, x_a)
        vy0, vy1 = (y_a, y_b) if y_a <= y_b else (y_b, y_a)
        # Skip polygons whose largest dimension is sub-pixel — invisible anyway.
        min_size_nm = 0.7 / max(1e-9, self._zoom)

        capped = False
        drawn = 0
        for entry in visible:
            bbs = entry.bboxes
            if bbs is None or bbs.shape[0] == 0:
                continue
            in_view = (
                (bbs[:, 0] <= vx1) & (bbs[:, 2] >= vx0) &
                (bbs[:, 1] <= vy1) & (bbs[:, 3] >= vy0)
            )
            big_enough = (
                np.maximum(bbs[:, 2] - bbs[:, 0], bbs[:, 3] - bbs[:, 1])
                >= min_size_nm
            )
            idx = np.flatnonzero(in_view & big_enough)
            if idx.size == 0:
                continue
            if idx.size > self._DRAW_CAP_PER_LAYER:
                idx = idx[: self._DRAW_CAP_PER_LAYER]
                capped = True

            fill = QColor(entry.color)
            fill.setAlpha(entry.fill_alpha())
            outline = QColor(entry.color).darker(140)
            p.setBrush(QBrush(fill))
            p.setPen(QPen(outline, 1.2))
            polys = entry.polygons
            for i in idx:
                poly = polys[int(i)]
                qp = QPolygonF()
                for x_nm, y_nm in poly:
                    sx, sy = self._world_to_screen(float(x_nm), float(y_nm))
                    qp.append(QPointF(sx, sy))
                p.drawPolygon(qp)
                drawn += 1

        self._last_drawn = drawn
        self._last_capped = capped
        if capped:
            p.setPen(QPen(QColor("#c97028")))
            p.drawText(8, 18,
                       f"{drawn:,} polygons drawn (capped per layer "
                       f"@ {self._DRAW_CAP_PER_LAYER:,}); zoom in for more")

    def _draw_origin(self, p: QPainter) -> None:
        if self._doc is None:
            return
        sx, sy = self._world_to_screen(0.0, 0.0)
        if 0 <= sx <= self.width() and 0 <= sy <= self.height():
            p.setPen(QPen(_TK_ACCENT_DK, 1.2))
            p.drawLine(QPointF(sx - 6, sy), QPointF(sx + 6, sy))
            p.drawLine(QPointF(sx, sy - 6), QPointF(sx, sy + 6))
            p.setPen(QPen(_TK_TEXT_HINT))
            p.drawText(QPointF(sx + 8, sy - 4), "0,0")

    def _draw_scale_bar(self, p: QPainter) -> None:
        if self._zoom <= 0:
            return
        nm_step = _nice_step(120.0 / self._zoom)
        if nm_step <= 0:
            return
        bar_px = nm_step * self._zoom
        x = self.width() - bar_px - 24
        y = self.height() - 24
        p.setPen(QPen(_TK_TEXT_PRI, 2))
        p.drawLine(QPointF(x, y), QPointF(x + bar_px, y))
        p.drawLine(QPointF(x, y - 4), QPointF(x, y + 4))
        p.drawLine(QPointF(x + bar_px, y - 4), QPointF(x + bar_px, y + 4))
        label = _format_nm(nm_step)
        p.setPen(QPen(_TK_TEXT_SEC))
        p.drawText(QPointF(x, y - 6), label)

    def _draw_empty_state(self, p: QPainter, msg: str = "no GDS loaded") -> None:
        """Icon + title + hint, matching SemViewer's empty state (M7-ov #7)."""
        r = self.rect()
        title_txt, hint_txt = {
            "no GDS loaded": ("No GDS loaded", "Open OASIS to load layers"),
            "all layers hidden": ("All layers hidden",
                                  "Toggle a layer's checkbox to show it"),
        }.get(msg, (msg, ""))
        p.save()
        pm = _qicon("layers").pixmap(QSize(46, 46))
        if not pm.isNull():
            p.drawPixmap(int(r.center().x() - 23), int(r.center().y() - 60), pm)
        f = p.font()
        f.setPixelSize(14)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QPen(_TK_TEXT_SEC))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, title_txt)
        if hint_txt:
            f.setPixelSize(_FS_LABEL)
            f.setBold(False)
            p.setFont(f)
            p.setPen(QPen(_TK_TEXT_HINT))
            p.drawText(r.adjusted(0, 42, 0, 42),
                       Qt.AlignmentFlag.AlignCenter, hint_txt)
        p.restore()


def _nice_step(raw: float) -> float:
    """Round ``raw`` (nm) to a 1/2/5 × 10^k step."""
    if raw <= 0 or not np.isfinite(raw):
        return 0.0
    exp = np.floor(np.log10(raw))
    base = raw / (10.0 ** exp)
    if base < 1.5:
        nice = 1.0
    elif base < 3.5:
        nice = 2.0
    elif base < 7.5:
        nice = 5.0
    else:
        nice = 10.0
    return float(nice * 10.0 ** exp)


def _format_nm(value_nm: float) -> str:
    if value_nm >= 1e6:
        return f"{value_nm / 1e6:.1f} mm"
    if value_nm >= 1e3:
        return f"{value_nm / 1e3:.1f} µm"
    return f"{value_nm:.0f} nm"


# ── SEM-side widgets (M3) ────────────────────────────────────────────────────


def _nice_round(x: float) -> float:
    """Round ``x`` down to a 1 / 2 / 5 × 10ⁿ "nice" value for scale bars."""
    if x <= 0:
        return 1.0
    import math
    exp = math.floor(math.log10(x))
    base = 10.0 ** exp
    for m in (5.0, 2.0, 1.0):
        if x >= m * base:
            return m * base
    return base


class _SemViewerCTA(QPushButton):
    """S12: orange CTA button overlay shown on the empty SEM viewer so a
    first-time user has a clear next action right where they're looking
    (instead of hunting the right column). Hidden as soon as an image is
    loaded."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setText("  Load SEM…  ")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            f"QPushButton {{ background:{_TK_ACCENT.name()}; "
            f"color:#ffffff; border:1px solid {_TK_ACCENT_DK.name()}; "
            f"border-radius:6px; padding:9px 22px; "
            f"font-size:{_FS_LABEL + 1}px; font-weight:700; }}"
            f"QPushButton:hover {{ background:{_TK_ACCENT_DK.name()}; }}")


class SemViewer(QWidget):
    """Right pane: SEM image + draggable GDS overlay (plan M4a).

    The SEM image is scaled to fit, with the loaded GDS ROI geometry drawn
    semi-transparently on top. The overlay is anchored so the defect's GDS
    position (``anchor``) sits at the image centre; dragging slides the GDS
    over the image, and the accumulated drag (nm) is read back by Set Offset
    to become the constant origin correction δ.

    Mapping (native-pixel scale ``s`` = displayed / native pixmap width):
        screen = img_centre + ((px, py) − eff_anchor)/nm_per_px · s   (Y flipped)
        eff_anchor = anchor − drag
    so folding ``drag`` into the origin (anchor += −drag) reproduces the
    dragged view with drag reset — the invariant Set Offset relies on."""

    drag_changed = pyqtSignal()
    cursor_gds = pyqtSignal(object)   # F11 M1: (x_nm, y_nm) or None
    load_sem_requested = pyqtSignal()  # S12: empty-state CTA click

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self._pixmap: Optional[QPixmap] = None
        self._caption = "no SEM image"
        self._entries: list = []          # list[(polys: list[ndarray], QColor, alpha:int)]
        self._anchor: Optional[tuple] = None   # (gx, gy) nm at image centre
        self._nm_per_px: float = 0.0
        self._drag_x = 0.0                # accumulated drag (nm)
        self._drag_y = 0.0
        self._press = None
        self._img_rect: Optional[tuple] = None   # (ox, oy, sw, sh)
        self._scale = 1.0
        # View zoom / pan (wheel + middle/right-drag). zoom 1.0 = fit; pan in
        # screen px on top of centring. Left-drag stays the overlay-align drag.
        self._view_zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._panning = False
        self._MIN_ZOOM = 0.2
        self._MAX_ZOOM = 60.0
        self._cursor_screen: Optional[QPointF] = None
        self.setMouseTracking(True)   # live cursor readout without a button
        # S12: prominent CTA in the empty SEM area so the first action a new
        # user sees in the centre of the screen is loading a SEM.
        self._cta_btn = _SemViewerCTA(self)
        self._cta_btn.clicked.connect(self.load_sem_requested)
        self._cta_btn.show()

    def _reposition_cta(self) -> None:
        # Centre the CTA button slightly below the painted "No SEM image"
        # caption (the caption sits at the widget centre, the CTA sits
        # ~110 px below it).
        if self._cta_btn is None:
            return
        self._cta_btn.adjustSize()
        sz = self._cta_btn.size()
        cx = (self.width() - sz.width()) // 2
        cy = (self.height() - sz.height()) // 2 + 110
        self._cta_btn.move(max(0, cx), max(0, cy))

    def resizeEvent(self, ev) -> None:  # type: ignore[override]
        super().resizeEvent(ev)
        self._reposition_cta()

    def native_size(self) -> Optional[tuple]:
        if self._pixmap is None or self._pixmap.isNull():
            return None
        return self._pixmap.width(), self._pixmap.height()

    def set_image(self, img: Optional["sem_loader.SemImage"]) -> None:
        self._pixmap = None
        if img is None:
            self._caption = "no SEM image"
        elif not img.exists:
            self._caption = (f"{img.filename}\n(file not found next to KLARF — "
                             f"load the image folder)")
        else:
            pm = QPixmap(str(img.file_path))
            if pm.isNull():
                self._caption = f"{img.filename}\n(unreadable image)"
            else:
                self._pixmap = pm
                self._caption = img.filename
        self._drag_x = self._drag_y = 0.0   # temp drag never survives a new image
        # S12: CTA shows only on the empty viewer; hide as soon as any
        # pixmap (even an unreadable one) has been assigned for inspection.
        self._cta_btn.setVisible(self._pixmap is None)
        if self._cta_btn.isVisible():
            self._reposition_cta()
        self.reset_view()
        self.update()

    def set_overlay(self, entries: list, anchor: Optional[tuple],
                    nm_per_px: float) -> None:
        """Geometry to draw on top: ``entries`` = list of
        ``(polys, color, alpha)`` with polys in chip nm and ``alpha`` the
        fill alpha (0-255); ``anchor`` = GDS (nm) that maps to image
        centre; ``nm_per_px`` = overlay scale."""
        self._entries = entries or []
        self._anchor = anchor
        self._nm_per_px = float(nm_per_px or 0.0)
        self.update()

    def clear_overlay(self) -> None:
        self._entries = []
        self._anchor = None
        self.update()

    def drag_offset_nm(self) -> tuple:
        return self._drag_x, self._drag_y

    def reset_drag(self) -> None:
        self._drag_x = self._drag_y = 0.0
        self.update()

    def reset_view(self) -> None:
        """Reset zoom/pan back to fit-the-window."""
        self._view_zoom = 1.0
        self._pan_x = self._pan_y = 0.0

    def _compute_geometry(self) -> Optional[tuple]:
        """Image placement on screen as ``(ox, oy, dw, dh, s)`` given the
        current zoom + pan, or ``None`` if there's no image. Used by both
        paint and wheel/pan so zoom works before the first paint."""
        if self._pixmap is None or self._pixmap.isNull():
            return None
        nw = max(1, self._pixmap.width())
        nh = max(1, self._pixmap.height())
        fit = min(self.width() / nw, self.height() / nh)
        s = fit * self._view_zoom
        dw, dh = nw * s, nh * s
        ox = (self.width() - dw) / 2.0 + self._pan_x
        oy = (self.height() - dh) / 2.0 + self._pan_y
        return ox, oy, dw, dh, s

    def _world_to_view(self, px: float, py: float) -> tuple:
        ox, oy, sw, sh = self._img_rect
        cxs = ox + sw / 2.0
        cys = oy + sh / 2.0
        ax = self._anchor[0] - self._drag_x
        ay = self._anchor[1] - self._drag_y
        nx = (px - ax) / self._nm_per_px
        ny = (py - ay) / self._nm_per_px
        return cxs + nx * self._scale, cys - ny * self._scale

    def _view_to_world(self, sx: float, sy: float) -> Optional[tuple]:
        """Inverse of :meth:`_world_to_view`: screen px → GDS (nm), or
        ``None`` when no overlay anchor / scale is set."""
        if (self._img_rect is None or self._anchor is None
                or self._nm_per_px <= 0 or self._scale <= 0):
            return None
        ox, oy, sw, sh = self._img_rect
        cxs = ox + sw / 2.0
        cys = oy + sh / 2.0
        nx = (sx - cxs) / self._scale
        ny = -(sy - cys) / self._scale
        ax = self._anchor[0] - self._drag_x
        ay = self._anchor[1] - self._drag_y
        return ax + nx * self._nm_per_px, ay + ny * self._nm_per_px

    def paintEvent(self, ev) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.fillRect(self.rect(), _TK_CANVAS_BG)
        geom = self._compute_geometry()
        if geom is not None:
            ox, oy, dw, dh, s = geom
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            p.drawPixmap(QRectF(ox, oy, dw, dh), self._pixmap,
                         QRectF(0, 0, self._pixmap.width(), self._pixmap.height()))
            self._img_rect = (ox, oy, dw, dh)
            self._scale = s
            # GDS overlay.
            if (self._anchor is not None and self._nm_per_px > 0
                    and self._entries):
                p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                p.setClipRect(QRectF(ox, oy, dw, dh))
                for polys, color, alpha in self._entries:
                    fill = QColor(color)
                    fill.setAlpha(alpha)
                    p.setPen(QPen(QColor(color), 1.2))
                    p.setBrush(QBrush(fill))
                    for poly in polys:
                        qp = QPolygonF()
                        for x_nm, y_nm in poly:
                            sx, sy = self._world_to_view(float(x_nm), float(y_nm))
                            qp.append(QPointF(sx, sy))
                        p.drawPolygon(qp)
                p.setClipping(False)
            # Centre crosshair (the defect / FOV centre).
            p.setPen(QPen(_TK_ACCENT_DK, 1.2, Qt.PenStyle.DashLine))
            cx, cy = self.width() // 2, self.height() // 2
            p.drawLine(QPointF(cx - 10, cy), QPointF(cx + 10, cy))
            p.drawLine(QPointF(cx, cy - 10), QPointF(cx, cy + 10))
            if self._anchor is not None and self._nm_per_px > 0:
                p.setPen(QPen(_TK_TEXT_HINT))
                p.drawText(self.rect().adjusted(8, 6, -8, -8),
                           Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter,
                           "left-drag: align GDS, then Set Offset  ·  wheel: zoom  ·  "
                           "middle/right-drag: pan  ·  double-click: reset view")
            self._draw_hud(p)
            p.setPen(QPen(_TK_TEXT_HINT))
            p.drawText(self.rect().adjusted(8, 8, -8, -8),
                       Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
                       self._caption)
        else:
            self._draw_empty_state(p)
        p.end()

    def _draw_empty_state(self, p: QPainter) -> None:
        """Centred onboarding hint when no SEM image is loaded (M7 #3/R7)."""
        r = self.rect()
        pm = _qicon("image").pixmap(QSize(48, 48))
        if not pm.isNull():
            p.drawPixmap(int(r.center().x() - 24), int(r.center().y() - 84), pm)
        else:
            glyph = p.font()
            glyph.setPixelSize(44)
            p.setFont(glyph)
            p.setPen(QPen(QColor("#d8c8b6")))
            p.drawText(r.adjusted(0, -60, 0, -60),
                       Qt.AlignmentFlag.AlignCenter, "▦")
        title = p.font()
        title.setPixelSize(15)
        title.setBold(True)
        p.setFont(title)
        p.setPen(QPen(_TK_TEXT_SEC))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter,
                   self._caption if self._caption != "no SEM image"
                   else "No SEM image")
        sub = p.font()
        sub.setPixelSize(_FS_LABEL)
        sub.setBold(False)
        p.setFont(sub)
        p.setPen(QPen(_TK_TEXT_HINT))
        p.drawText(r.adjusted(0, 56, 0, 56), Qt.AlignmentFlag.AlignCenter,
                   "Load a KLARF defect list or an image folder")

    def _draw_hud(self, p: QPainter) -> None:
        """Corner readout: zoom factor (top-right), cursor GDS coordinate
        (top-left), and a scale bar (bottom-right) when the overlay scale is
        known (plan M6.6 viewer live info)."""
        p.setPen(QPen(_TK_TEXT_PRI))
        # Zoom factor, top-right.
        p.drawText(self.rect().adjusted(8, 6, -8, -8),
                   Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
                   f"{self._view_zoom:.2f}×")
        # Cursor coordinate (GDS µm), top-left.
        if self._cursor_screen is not None:
            w = self._view_to_world(self._cursor_screen.x(),
                                    self._cursor_screen.y())
            if w is not None:
                p.drawText(self.rect().adjusted(8, 6, -8, -8),
                           Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                           f"({w[0] / 1e3:,.3f}, {w[1] / 1e3:,.3f}) µm")
        # Scale bar, bottom-right: a round nm length drawn at the current
        # display scale (overlay nm/px × view scale).
        if self._nm_per_px > 0 and self._scale > 0:
            px_per_nm = self._scale / self._nm_per_px
            target_px = 90.0                       # aim for ~90 px long
            raw_nm = target_px / px_per_nm
            nice_nm = _nice_round(raw_nm)
            bar_px = nice_nm * px_per_nm
            x1 = self.width() - 16
            x0 = x1 - bar_px
            y = self.height() - 26
            p.setPen(QPen(_TK_TEXT_PRI, 2))
            p.drawLine(QPointF(x0, y), QPointF(x1, y))
            p.drawLine(QPointF(x0, y - 4), QPointF(x0, y + 4))
            p.drawLine(QPointF(x1, y - 4), QPointF(x1, y + 4))
            label = (f"{nice_nm / 1e3:g} µm" if nice_nm >= 1000
                     else f"{nice_nm:g} nm")
            p.setPen(QPen(_TK_TEXT_PRI))
            p.drawText(QRectF(x0, y - 22, bar_px, 16),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                       label)

    def leaveEvent(self, ev) -> None:  # type: ignore[override]
        self._cursor_screen = None
        self.cursor_gds.emit(None)
        self.update()

    def mousePressEvent(self, ev: QMouseEvent) -> None:  # type: ignore[override]
        if ev.button() in (Qt.MouseButton.MiddleButton,
                           Qt.MouseButton.RightButton):
            # Pan the view (image + overlay move together).
            self._panning = True
            self._press = ev.position()
        elif (ev.button() == Qt.MouseButton.LeftButton
                and self._anchor is not None and self._nm_per_px > 0):
            # Align the GDS overlay over the image (δ drag).
            self._panning = False
            self._press = ev.position()

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:  # type: ignore[override]
        self._cursor_screen = ev.position()
        self.cursor_gds.emit(self._view_to_world(ev.position().x(),
                                                 ev.position().y()))
        if self._press is None:
            self.update()        # refresh the cursor-coordinate readout
            return
        pos = ev.position()
        mdx = pos.x() - self._press.x()
        mdy = pos.y() - self._press.y()
        self._press = pos
        if self._panning:
            self._pan_x += mdx
            self._pan_y += mdy
            self.update()
            return
        if self._img_rect is None or self._nm_per_px <= 0:
            return
        s = self._scale or 1.0
        self._drag_x += mdx * self._nm_per_px / s
        self._drag_y -= mdy * self._nm_per_px / s
        self.drag_changed.emit()
        self.update()

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:  # type: ignore[override]
        self._press = None
        self._panning = False

    def mouseDoubleClickEvent(self, ev: QMouseEvent) -> None:  # type: ignore[override]
        self.reset_view()
        self.update()

    def wheelEvent(self, ev: QWheelEvent) -> None:  # type: ignore[override]
        step = ev.angleDelta().y()
        if step == 0:
            return
        pos = ev.position()
        self._apply_zoom(1.2 if step > 0 else 1.0 / 1.2, pos.x(), pos.y())

    def zoom_by(self, factor: float) -> None:
        """Zoom around the widget centre (keyboard +/- shortcuts)."""
        self._apply_zoom(factor, self.width() / 2.0, self.height() / 2.0)

    def _apply_zoom(self, factor: float, mx: float, my: float) -> None:
        """Multiply the view zoom by ``factor``, keeping the image point under
        screen (mx, my) fixed."""
        geom = self._compute_geometry()
        if geom is None:
            return
        ox, oy, dw, dh, s = geom
        ix = (mx - ox) / s
        iy = (my - oy) / s
        new_zoom = max(self._MIN_ZOOM,
                       min(self._MAX_ZOOM, self._view_zoom * factor))
        if new_zoom == self._view_zoom:
            return
        nw = max(1, self._pixmap.width())
        nh = max(1, self._pixmap.height())
        fit = min(self.width() / nw, self.height() / nh)
        s2 = fit * new_zoom
        self._view_zoom = new_zoom
        self._pan_x = (mx - ix * s2) - (self.width() - nw * s2) / 2.0
        self._pan_y = (my - iy * s2) - (self.height() - nh * s2) / 2.0
        self.update()


class PartChipPanel(QFrame):
    """PART/CHIP dropdowns + Custom FOV override (F21 M2).

    Replaces the legacy CoordinateSetupPanel: instead of asking the user to
    copy 6 RFL columns by hand, the panel loads a repo-shipped catalog
    (``glas/data/parts.json``) and lets the user pick PART → CHIP from
    dropdowns. The selected ChipSpec drives ``chip_corner_*`` / FOV /
    nm-per-px. An optional "Custom override" group lets the user deviate
    from the catalog FOV / scale per session without writing the catalog
    (Q5 = B). Adding new PART/CHIP is a developer-mode action handled by
    :class:`CatalogEditorDialog` (M4).

    Backward-compat with the existing ``coord_setup`` wiring:

    * ``changed`` signal — emitted whenever the effective values move
    * ``values()`` — same numeric keys MainWindow already consumes, plus
      ``part_id`` / ``chip_id`` (Optional[str]); ``fine_dx`` / ``fine_dy``
      are intentionally absent (the manual fine-tune was removed in F21).
    * ``set_from_meta(meta)`` — restores PART/CHIP when the cache stored
      them (v5+); falls back to a "legacy" mode showing the snapshot
      values when only the v4 coordinate fields are present.

    The right column treats the panel as always-visible (no collapsible
    wrapping) — see :class:`SemPanel`.
    """

    changed = pyqtSignal()
    edit_catalog_requested = pyqtSignal()    # M4: developer-mode entry point

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._catalog: dict[str, PartSpec] = {}
        self._load_error: Optional[str] = None
        # When True, the panel is showing a v4-era cache snapshot and the
        # PART/CHIP dropdowns are inert (the cache pre-dates the catalog).
        self._legacy_snapshot = False
        self._legacy_values: dict = {}
        # Block ``changed`` emissions during programmatic widget updates so a
        # PART/CHIP refresh doesn't trigger a cascade.
        self._suppress = False
        # Per-session Custom override (FOV / nm-per-px). Populated from the
        # current chip the first time Custom is enabled.
        self._customising = False

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)

        # ── PART / CHIP dropdowns ────────────────────────────────────────
        form = QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(4)
        form.setColumnStretch(1, 1)

        part_lbl = QLabel("PART:")
        part_lbl.setStyleSheet(
            f"font-weight:700; color:{_TK_ACCENT_DK.name()}; "
            f"font-size:{_FS_CAPTION}px;")
        chip_lbl = QLabel("CHIP:")
        chip_lbl.setStyleSheet(part_lbl.styleSheet())

        self._part_cb = QComboBox(self)
        self._part_cb.setToolTip(
            "Select the fab PART (product code). Catalog ships with the app — "
            "use Edit catalog… (developer mode) to add a missing PART.")
        self._part_cb.currentTextChanged.connect(self._on_part_changed)

        self._chip_cb = QComboBox(self)
        self._chip_cb.setToolTip(
            "Select the CHIP within the chosen PART. Each CHIP carries its "
            "chip-corner offset, default FOV, and overlay scale.")
        self._chip_cb.currentTextChanged.connect(self._on_chip_changed)

        form.addWidget(part_lbl, 0, 0)
        form.addWidget(self._part_cb, 0, 1)
        form.addWidget(chip_lbl, 1, 0)
        form.addWidget(self._chip_cb, 1, 1)
        v.addLayout(form)

        # ── Effective-values badges ──────────────────────────────────────
        self._corner_lbl = QLabel("→ chip corner: —")
        self._corner_lbl.setWordWrap(True)
        self._corner_lbl.setStyleSheet(_hint_qss(_FS_CAPTION, pad="2px 0"))
        self._corner_lbl.setToolTip(
            "Chip lower-left corner relative to die corner (nm), computed as "
            "(chip_x − gds_off_x) × 1000 — fed to klarf_to_gds().")
        v.addWidget(self._corner_lbl)

        self._fov_lbl = QLabel("→ FOV: —")
        self._fov_lbl.setWordWrap(True)
        self._fov_lbl.setStyleSheet(_hint_qss(_FS_CAPTION, pad="2px 0"))
        self._fov_lbl.setToolTip(
            "SEM image field-of-view used for the GDS ROI box. Per-CHIP "
            "default from the catalog; tick Custom override to change it "
            "for this session.")
        v.addWidget(self._fov_lbl)

        # U5: catalog notes used to live as a dedicated italic label that
        # competed visually with the FOV "(custom)" italic tag. Move it
        # into the chip-corner badge's tooltip — same info, no extra row.

        # ── Custom override toggle + hidden frame ────────────────────────
        self._custom_chk = QCheckBox("Custom override (FOV / scale)", self)
        self._custom_chk.setToolTip(
            "Override the catalog defaults for FOV and overlay scale this "
            "session. Untick to revert to the CHIP's catalog values.")
        self._custom_chk.toggled.connect(self._on_custom_toggled)
        v.addWidget(self._custom_chk)

        self._custom_frame = QFrame(self)
        self._custom_frame.setFrameShape(QFrame.Shape.NoFrame)
        cf = QGridLayout(self._custom_frame)
        cf.setContentsMargins(12, 2, 4, 2)
        cf.setHorizontalSpacing(6)
        cf.setVerticalSpacing(2)

        def _nm(step: int = 100) -> QSpinBox:
            s = QSpinBox(self._custom_frame)
            s.setRange(1, 100_000_000)
            s.setSingleStep(step)
            s.setGroupSeparatorShown(True)
            s.setSuffix(" nm")
            s.valueChanged.connect(self._on_custom_value_changed)
            return s

        self._fov_w = _nm()
        self._fov_h = _nm()
        self._fov_w.setToolTip("Custom FOV width (nm).")
        self._fov_h.setToolTip("Custom FOV height (nm).")

        self._nm_per_px = QDoubleSpinBox(self._custom_frame)
        self._nm_per_px.setDecimals(4)
        self._nm_per_px.setRange(0.0, 1_000_000.0)
        self._nm_per_px.setSingleStep(0.5)
        self._nm_per_px.setGroupSeparatorShown(True)
        self._nm_per_px.setSuffix(" nm/px")
        # U4: a disabled QSpinBox still shows "0.0000" prominently and reads
        # as "scale is zero" to first-time users. Show "auto" instead while
        # the auto checkbox is on, so the value the panel will actually use
        # (FOV ÷ image px) is what the user sees.
        self._nm_per_px.setSpecialValueText("auto (FOV ÷ image px)")
        self._nm_per_px.setEnabled(False)
        self._nm_per_px.valueChanged.connect(self._on_custom_value_changed)
        self._nm_auto = QCheckBox("auto = FOV ÷ image px", self._custom_frame)
        self._nm_auto.setChecked(True)

        def _sync_npx_enabled(on: bool) -> None:
            self._nm_per_px.setEnabled(not on)
            # The special-value text only appears at the spinbox minimum;
            # snap to 0 while auto is on so "auto" stays visible.
            if on:
                self._nm_per_px.setValue(0.0)
        self._nm_auto.toggled.connect(_sync_npx_enabled)
        self._nm_auto.toggled.connect(self._on_custom_value_changed)

        def _add(row: int, text: str, w: QWidget) -> None:
            cap = QLabel(text)
            cap.setStyleSheet(_hint_qss(_FS_CAPTION))
            cf.addWidget(cap, row, 0)
            cf.addWidget(w, row, 1)

        _add(0, "FOV W", self._fov_w)
        _add(1, "FOV H", self._fov_h)
        _add(2, "nm/px", self._nm_per_px)
        cf.addWidget(self._nm_auto, 3, 0, 1, 2)
        self._custom_frame.setVisible(False)
        v.addWidget(self._custom_frame)

        # ── Catalog status + Edit button (M4, dev-mode only) ─────────────
        self._status_lbl = QLabel("")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(
            _hint_qss(_FS_MICRO, _TK_TEXT_HINT.name(), pad="4px 0 0 0"))
        v.addWidget(self._status_lbl)

        self._edit_btn = QPushButton("⚙ Edit catalog…", self)
        self._edit_btn.setToolTip(
            "Developer mode: add or edit PART/CHIP entries in the catalog.")
        self._edit_btn.clicked.connect(self.edit_catalog_requested)
        self._edit_btn.setVisible(False)
        v.addWidget(self._edit_btn)

        # Pull in the catalog now so the dropdowns are populated before
        # MainWindow connects the signal (an empty catalog leaves the
        # widgets disabled with a status-line hint).
        self.reload_catalog()

    # ── Catalog I/O ─────────────────────────────────────────────────────

    def reload_catalog(self) -> None:
        """Re-read ``glas/data/parts.json`` and rebuild the dropdowns. The
        catalog editor (M4) calls this after saving."""
        self._load_error = None
        try:
            self._catalog = load_catalog()
        except CatalogError as exc:
            self._catalog = {}
            self._load_error = str(exc)
        self._legacy_snapshot = False
        self._legacy_values = {}
        self._populate_parts()

    def set_dev_mode(self, on: bool) -> None:
        """Show / hide the Edit catalog button (toggled by MainWindow when
        developer mode is enabled or restored from QSettings)."""
        self._edit_btn.setVisible(bool(on))

    def _populate_parts(self) -> None:
        self._suppress = True
        self._part_cb.clear()
        self._chip_cb.clear()
        if not self._catalog:
            self._part_cb.setEnabled(False)
            self._chip_cb.setEnabled(False)
            self._custom_chk.setEnabled(True)
            # S11: empty-catalog state used to be a muted hint that read
            # as a regular tooltip; promote it to a card with a warning
            # tint so it can't be mistaken for normal copy. The hint
            # leaves the catalog-editor entry point intentionally vague
            # (it's gated behind developer mode and shouldn't advertise
            # how to unlock it).
            if self._load_error:
                self._status_lbl.setText(
                    f"⚠ Catalog error: {self._load_error}\n"
                    f"Fix glas/data/parts.json or use the catalog editor.")
            else:
                self._status_lbl.setText(
                    "⚠ No PART / CHIP data\n"
                    "Ask an administrator to add entries.")
            self._status_lbl.setStyleSheet(
                f"background:#fff0e0; color:#9a6a2a; "
                f"border:1px solid #c8a080; border-radius:4px; "
                f"padding:8px 10px; font-size:{_FS_CAPTION}px;")
        else:
            self._part_cb.setEnabled(True)
            self._chip_cb.setEnabled(True)
            for pid in sorted(self._catalog):
                self._part_cb.addItem(pid)
            self._status_lbl.setText("")
            self._status_lbl.setStyleSheet(
                _hint_qss(_FS_MICRO, _TK_TEXT_HINT.name(), pad="4px 0 0 0"))
        self._suppress = False
        # Drive an initial CHIP/badge update once.
        self._on_part_changed(self._part_cb.currentText())

    # ── Selection callbacks ─────────────────────────────────────────────

    def _on_part_changed(self, pid: str) -> None:
        # Refresh CHIP dropdown to the chosen PART's chips.
        self._suppress = True
        self._chip_cb.clear()
        part = self._catalog.get(pid)
        if part is not None:
            for cid in sorted(part.chips):
                self._chip_cb.addItem(cid)
        self._suppress = False
        self._on_chip_changed(self._chip_cb.currentText())

    def _on_chip_changed(self, _cid: str) -> None:
        # A real chip selection cancels any pending legacy-snapshot mode.
        if self._current_chip() is not None:
            self._legacy_snapshot = False
            self._legacy_values = {}
        # Custom override re-seeded with the new CHIP's catalog defaults
        # so toggling Custom shows reasonable starting values.
        chip = self._current_chip()
        if chip is not None and not self._custom_chk.isChecked():
            self._seed_custom_from_chip(chip)
        self._refresh_badges()
        if not self._suppress:
            self.changed.emit()

    def _on_custom_toggled(self, on: bool) -> None:
        self._customising = bool(on)
        self._custom_frame.setVisible(bool(on))
        chip = self._current_chip()
        if on and chip is not None:
            # Seed spinboxes from the current chip so the override starts
            # at the catalog defaults rather than an empty / zero state.
            self._seed_custom_from_chip(chip)
        self._refresh_badges()
        if not self._suppress:
            self.changed.emit()

    def _on_custom_value_changed(self) -> None:
        self._refresh_badges()
        if not self._suppress and self._customising:
            self.changed.emit()

    # ── Helpers ─────────────────────────────────────────────────────────

    def _current_chip(self) -> Optional[ChipSpec]:
        if self._legacy_snapshot:
            return None
        pid = self._part_cb.currentText()
        cid = self._chip_cb.currentText()
        part = self._catalog.get(pid)
        if part is None:
            return None
        return part.chips.get(cid)

    def _seed_custom_from_chip(self, chip: ChipSpec) -> None:
        self._suppress = True
        self._fov_w.setValue(int(chip.fov_w_nm))
        self._fov_h.setValue(int(chip.fov_h_nm))
        if chip.nm_per_px is not None:
            self._nm_auto.setChecked(False)
            self._nm_per_px.setValue(float(chip.nm_per_px))
        else:
            self._nm_auto.setChecked(True)
            self._nm_per_px.setValue(0.0)
        self._suppress = False

    def _effective_fov(self) -> tuple[float, float]:
        if self._legacy_snapshot:
            return (float(self._legacy_values.get("fov_w", 0.0)),
                    float(self._legacy_values.get("fov_h", 0.0)))
        if self._customising:
            return float(self._fov_w.value()), float(self._fov_h.value())
        chip = self._current_chip()
        if chip is None:
            return DEFAULT_FOV_NM, DEFAULT_FOV_NM
        return float(chip.fov_w_nm), float(chip.fov_h_nm)

    def _effective_scale(self) -> tuple[float, bool]:
        """(nm_per_px_manual, nm_auto). ``manual`` is ignored when ``auto``
        is True; consumer uses FOV ÷ image px instead."""
        if self._legacy_snapshot:
            return (float(self._legacy_values.get("nm_per_px_manual", 0.0)),
                    bool(self._legacy_values.get("nm_auto", True)))
        if self._customising:
            return (float(self._nm_per_px.value()),
                    bool(self._nm_auto.isChecked()))
        chip = self._current_chip()
        if chip is None or chip.nm_per_px is None:
            return 0.0, True
        return float(chip.nm_per_px), False

    def _refresh_badges(self) -> None:
        if self._legacy_snapshot:
            cx = float(self._legacy_values.get("chip_corner_x", 0.0))
            cy = float(self._legacy_values.get("chip_corner_y", 0.0))
        else:
            chip = self._current_chip()
            cx, cy = (chip.chip_corner_nm() if chip is not None else (0.0, 0.0))
        self._corner_lbl.setText(
            f"→ chip corner: {cx:,.0f}, {cy:,.0f} nm  "
            f"({cx / 1e3:,.3f}, {cy / 1e3:,.3f} µm)")
        fw, fh = self._effective_fov()
        tag = " (custom)" if self._customising else (
            " (legacy)" if self._legacy_snapshot else "")
        self._fov_lbl.setText(
            f"→ FOV: {fw:,.0f} × {fh:,.0f} nm  "
            f"({fw / 1e3:,.3f} × {fh / 1e3:,.3f} µm){tag}")
        # U5: surface chip notes in the chip-corner tooltip instead of as a
        # third italic line that crowded the right column.
        chip = self._current_chip()
        base_corner_tip = (
            "Chip lower-left corner relative to die corner (nm), computed "
            "as (chip_x − gds_off_x) × 1000 — fed to klarf_to_gds().")
        if chip is not None and chip.notes:
            self._corner_lbl.setToolTip(f"{base_corner_tip}\n\n{chip.notes}")
        else:
            self._corner_lbl.setToolTip(base_corner_tip)

    # ── Public API for MainWindow ───────────────────────────────────────

    def values(self) -> dict:
        """Snapshot of the effective alignment values. Keys mirror the legacy
        Coordinate Setup dict (so existing consumers keep working) with two
        additions: ``part_id`` / ``chip_id`` (Optional[str]). ``fine_dx`` /
        ``fine_dy`` are intentionally absent (removed in F21 Q6)."""
        if self._legacy_snapshot:
            d = dict(self._legacy_values)
            d.setdefault("part_id", None)
            d.setdefault("chip_id", None)
            return d
        chip = self._current_chip()
        if chip is None:
            return {
                "chip_corner_x": 0.0, "chip_corner_y": 0.0,
                "chip_x_um": 0.0, "chip_y_um": 0.0,
                "chip_w_um": 0.0, "chip_h_um": 0.0,
                "gds_off_x_um": 0.0, "gds_off_y_um": 0.0,
                "fov_w": DEFAULT_FOV_NM if not self._customising
                        else float(self._fov_w.value()),
                "fov_h": DEFAULT_FOV_NM if not self._customising
                        else float(self._fov_h.value()),
                "nm_per_px_manual": (float(self._nm_per_px.value())
                                     if self._customising else 0.0),
                "nm_auto": (bool(self._nm_auto.isChecked())
                            if self._customising else True),
                "part_id": None,
                "chip_id": None,
            }
        cx, cy = chip.chip_corner_nm()
        fw, fh = self._effective_fov()
        npx_manual, npx_auto = self._effective_scale()
        return {
            "chip_corner_x": cx,
            "chip_corner_y": cy,
            "chip_x_um": chip.chip_x_um,
            "chip_y_um": chip.chip_y_um,
            "chip_w_um": chip.chip_w_um,
            "chip_h_um": chip.chip_h_um,
            "gds_off_x_um": chip.gds_off_x_um,
            "gds_off_y_um": chip.gds_off_y_um,
            "fov_w": fw,
            "fov_h": fh,
            "nm_per_px_manual": npx_manual,
            "nm_auto": npx_auto,
            "part_id": self._part_cb.currentText() or None,
            "chip_id": self._chip_cb.currentText() or None,
        }

    def set_from_meta(self, meta) -> None:
        """Restore PART/CHIP from a loaded cache. v5+ caches carry the IDs
        and we re-select the dropdowns; v4 (or unknown PART/CHIP) drops the
        panel into a "legacy snapshot" mode that exposes the stored
        coordinates without offering a re-selection."""
        self._suppress = True
        self._custom_chk.setChecked(False)
        self._customising = False
        self._custom_frame.setVisible(False)

        pid = getattr(meta, "part_id", None)
        cid = getattr(meta, "chip_id", None)
        part = self._catalog.get(pid) if pid else None
        if part is not None and cid in part.chips:
            # v5 round-trip: reselect catalog entry. Chip-corner snapshots
            # always equal the catalog values by construction (Q3) so they
            # come straight from the catalog. FOV / nm-per-px may diverge if
            # the cache was exported with Custom override active — restore
            # that override transparently so the alignment doesn't silently
            # shift back to catalog defaults on cache load.
            self._legacy_snapshot = False
            self._legacy_values = {}
            idx = self._part_cb.findText(pid)
            if idx >= 0:
                self._part_cb.setCurrentIndex(idx)
            idx = self._chip_cb.findText(cid)
            if idx >= 0:
                self._chip_cb.setCurrentIndex(idx)
            chip = part.chips[cid]
            cached_fov_w = float(getattr(meta, "fov_w", 0.0) or 0.0)
            cached_fov_h = float(getattr(meta, "fov_h", 0.0) or 0.0)
            cached_npx = float(getattr(meta, "nm_per_px", 0.0) or 0.0)
            fov_diff = (
                cached_fov_w > 0
                and (abs(cached_fov_w - chip.fov_w_nm) > 0.5
                     or abs(cached_fov_h - chip.fov_h_nm) > 0.5))
            chip_npx = chip.nm_per_px or 0.0
            npx_diff = abs(cached_npx - chip_npx) > 1e-6
            if fov_diff or npx_diff:
                self._customising = True
                self._custom_chk.setChecked(True)
                self._custom_frame.setVisible(True)
                if cached_fov_w > 0:
                    self._fov_w.setValue(int(round(cached_fov_w)))
                    self._fov_h.setValue(int(round(cached_fov_h)))
                if cached_npx > 0:
                    self._nm_auto.setChecked(False)
                    self._nm_per_px.setEnabled(True)
                    self._nm_per_px.setValue(cached_npx)
                else:
                    self._nm_auto.setChecked(True)
                    self._nm_per_px.setEnabled(False)
            self._suppress = False
            self._refresh_badges()
            self.changed.emit()
            return

        # v4 cache (or PART/CHIP no longer in catalog): show the snapshot.
        self._legacy_snapshot = True
        self._legacy_values = {
            "chip_corner_x": float(getattr(meta, "chip_corner_x", 0.0)),
            "chip_corner_y": float(getattr(meta, "chip_corner_y", 0.0)),
            "chip_x_um": float(getattr(meta, "chip_x_um", 0.0)),
            "chip_y_um": float(getattr(meta, "chip_y_um", 0.0)),
            "chip_w_um": float(getattr(meta, "chip_w_um", 0.0)),
            "chip_h_um": float(getattr(meta, "chip_h_um", 0.0)),
            "gds_off_x_um": float(getattr(meta, "gds_off_x_um", 0.0)),
            "gds_off_y_um": float(getattr(meta, "gds_off_y_um", 0.0)),
            "fov_w": float(getattr(meta, "fov_w", 0.0)),
            "fov_h": float(getattr(meta, "fov_h", 0.0)),
            "nm_per_px_manual": float(getattr(meta, "nm_per_px", 0.0)),
            "nm_auto": float(getattr(meta, "nm_per_px", 0.0)) <= 0.0,
        }
        # Block dropdowns — the catalog can't represent this cache.
        self._part_cb.setEnabled(False)
        self._chip_cb.setEnabled(False)
        if pid or cid:
            self._status_lbl.setText(
                f"⚠ cache from PART {pid or '?'} / CHIP {cid or '?'} not in "
                f"current catalog — showing snapshot values only.")
        else:
            self._status_lbl.setText(
                "⚠ legacy cache (no PART/CHIP) — showing snapshot values "
                "only. Re-export from a current PART/CHIP to upgrade.")
        self._suppress = False
        self._refresh_badges()
        self.changed.emit()


class AlignmentDeltaPanel(QFrame):
    """Always-visible Origin δ readout + Set/Clear/Copy controls (F21 M3).

    Replaces the small read-only ``origin δ`` label that used to live at the
    bottom of CoordinateSetupPanel: the value the user is steering toward
    deserves a prominent, persistent display next to the SEM list.
    The numeric value is owned by MainWindow (``_origin_dx`` / ``_origin_dy``);
    this widget just renders and provides the buttons.
    """

    set_requested = pyqtSignal()
    clear_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("alignDeltaPanel")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            f"QFrame#alignDeltaPanel {{ background: {_TK_BG_PANEL.name()}; "
            f"border: 1px solid {_TK_BORDER.name()}; border-radius: 4px; }}")

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 10)
        v.setSpacing(6)

        title = QLabel("ALIGNMENT δ")
        title.setStyleSheet(
            f"color:{_TK_ACCENT_DK.name()}; font-size:{_FS_MICRO}px; "
            f"font-weight:700; letter-spacing:1px;")
        v.addWidget(title)

        self._x_lbl = QLabel("X:    0 nm")
        self._y_lbl = QLabel("Y:    0 nm")
        big_qss = (f"color:{_TK_TEXT_PRI.name()}; "
                   f"font-size:{max(_FS_LABEL + 3, 16)}px; "
                   f"font-weight:700; font-family: monospace;")
        self._x_lbl.setStyleSheet(big_qss)
        self._y_lbl.setStyleSheet(big_qss)
        v.addWidget(self._x_lbl)
        v.addWidget(self._y_lbl)

        # Set / Clear row (two equal stretching buttons — no third icon
        # button so the row never overflows the 300-px-fixed right column
        # when the vertical scrollbar appears).
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.set_btn = QPushButton("Set Offset")
        self.set_btn.setToolTip(
            "Lock in the current GDS overlay drag as the alignment δ. "
            "Shortcut: arrow keys with Ctrl nudge δ by 10 nm.")
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setToolTip("Reset alignment δ and any pending drag.")
        self.set_btn.clicked.connect(self.set_requested)
        self.clear_btn.clicked.connect(self.clear_requested)
        btn_row.addWidget(self.set_btn, 1)
        btn_row.addWidget(self.clear_btn, 1)
        v.addLayout(btn_row)

        # Copy δ — full-width, icon + label, accent-bordered button on its
        # own line. The earlier ⧉ Unicode glyph rendered as an
        # unrecognisable square on some platforms; this version pairs the
        # Lucide-style copy SVG with a clear label, and confirms with a
        # transient label flip ("Copied ✓") so the click feels acknowledged.
        self._copy_btn = QPushButton(_qicon("copy"), " Copy δ to clipboard",
                                     self)
        self._copy_btn.setToolTip("Copy (dx, dy) nm to the system clipboard.")
        self._copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_btn.setStyleSheet(
            f"QPushButton {{ background:#fff5e8; "
            f"color:{_TK_ACCENT_DK.name()}; "
            f"border:1px solid {_TK_ACCENT_DK.name()}; "
            f"border-radius:4px; padding:5px 10px; "
            f"font-size:{_FS_CAPTION}px; font-weight:600; }}"
            f"QPushButton:hover {{ background:{_TK_ACCENT.name()}; "
            f"color:#ffffff; }}"
            f"QPushButton:pressed {{ background:{_TK_ACCENT_DK.name()}; "
            f"color:#ffffff; }}")
        self._copy_btn.clicked.connect(self._on_copy)
        v.addWidget(self._copy_btn)

        # Snap-back timer for the post-copy confirmation label flip.
        self._copy_revert_timer = QTimer(self)
        self._copy_revert_timer.setSingleShot(True)
        self._copy_revert_timer.setInterval(1200)
        self._copy_revert_timer.timeout.connect(
            lambda: self._copy_btn.setText(" Copy δ to clipboard"))

        hint = QLabel("Drag GDS overlay → Set. Ctrl+arrow keys nudge ±10 nm.")
        hint.setWordWrap(True)
        hint.setStyleSheet(_hint_qss(_FS_MICRO, pad="2px 0 0 0"))
        v.addWidget(hint)

        self._dx = 0.0
        self._dy = 0.0

    def set_values(self, dx: float, dy: float) -> None:
        """Update the displayed δ (called by MainWindow whenever
        ``_origin_dx`` / ``_origin_dy`` changes, including live overlay
        drag previews)."""
        self._dx = float(dx)
        self._dy = float(dy)
        # Sign-aware aligned formatting so positive/negative numbers don't
        # jiggle the column width every other update.
        self._x_lbl.setText(f"X: {self._dx:>+10,.0f} nm")
        self._y_lbl.setText(f"Y: {self._dy:>+10,.0f} nm")

    def _on_copy(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        app.clipboard().setText(f"{self._dx:.0f}, {self._dy:.0f}")
        # Transient confirmation so the click feels acknowledged; reverts
        # back to the default label after ~1.2 s.
        self._copy_btn.setText(" Copied ✓")
        self._copy_revert_timer.start()


class CatalogEditorDialog(QDialog):
    """Developer-mode catalog editor (F21 M4).

    Two-pane layout: a tree of PART → CHIP on the left + an add/remove
    toolbar; a form for the selected CHIP's coordinate fields on the right.
    ``Save`` atomically rewrites ``glas/data/parts.json``; the caller
    refreshes its PartChipPanel on accept.

    The dialog operates on an in-memory copy of the catalog so a
    ``Cancel`` (or window-close) discards edits cleanly.
    """

    def __init__(self, parent: Optional[QWidget],
                 catalog: dict[str, PartSpec]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit PART / CHIP catalog")
        self.setModal(True)
        self.setMinimumSize(640, 480)

        # Deep-copy so Cancel reverts cleanly.
        self._catalog: dict[str, PartSpec] = {
            pid: PartSpec.from_dict(p.to_dict())
            for pid, p in catalog.items()
        }

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # ── Left: PART/CHIP tree + add/remove ────────────────────────────
        left = QVBoxLayout()
        self._tree = QListWidget(self)
        self._tree.itemSelectionChanged.connect(self._on_tree_select)
        left.addWidget(self._tree, 1)

        btn_row = QHBoxLayout()
        self._add_part_btn = QPushButton("+ PART")
        self._add_chip_btn = QPushButton("+ CHIP")
        self._del_btn = QPushButton("− Remove")
        self._add_part_btn.clicked.connect(self._on_add_part)
        self._add_chip_btn.clicked.connect(self._on_add_chip)
        self._del_btn.clicked.connect(self._on_del)
        btn_row.addWidget(self._add_part_btn)
        btn_row.addWidget(self._add_chip_btn)
        btn_row.addWidget(self._del_btn)
        left.addLayout(btn_row)

        left_w = QWidget(self)
        left_w.setLayout(left)
        left_w.setMinimumWidth(220)
        root.addWidget(left_w)

        # ── Right: CHIP / PART form ──────────────────────────────────────
        right = QVBoxLayout()
        self._form_box = QGroupBox("(select an entry on the left)")
        form = QGridLayout(self._form_box)
        form.setVerticalSpacing(4)

        self._desc = QLineEdit(self._form_box)
        self._desc.setPlaceholderText(
            "PART description (only used when a PART row is selected)")
        self._desc.textChanged.connect(self._on_form_changed)

        def _um(lo: float = -100_000_000.0,
                hi: float = 100_000_000.0) -> QDoubleSpinBox:
            s = QDoubleSpinBox(self._form_box)
            s.setDecimals(5)
            s.setRange(lo, hi)
            s.setSuffix(" µm")
            s.setGroupSeparatorShown(True)
            s.valueChanged.connect(self._on_form_changed)
            return s

        def _nm() -> QSpinBox:
            s = QSpinBox(self._form_box)
            s.setRange(1, 100_000_000)
            s.setSuffix(" nm")
            s.setGroupSeparatorShown(True)
            s.valueChanged.connect(self._on_form_changed)
            return s

        self._cx = _um(0.0)
        self._cy = _um(0.0)
        self._cw = _um(0.0)
        self._ch = _um(0.0)
        self._gx = _um()
        self._gy = _um()
        self._fw = _nm()
        self._fh = _nm()
        self._nmpx = QDoubleSpinBox(self._form_box)
        self._nmpx.setDecimals(4)
        self._nmpx.setRange(0.0, 1_000_000.0)
        self._nmpx.setSuffix(" nm/px (0 = auto)")
        self._nmpx.valueChanged.connect(self._on_form_changed)
        self._notes = QLineEdit(self._form_box)
        self._notes.setPlaceholderText("Notes (optional)")
        self._notes.textChanged.connect(self._on_form_changed)

        fields = [
            ("Description", self._desc),
            ("Chip X (DieX, µm)", self._cx),
            ("Chip Y (DieY, µm)", self._cy),
            ("Chip W (SizeW, µm)", self._cw),
            ("Chip H (SizeH, µm)", self._ch),
            ("GDS offset X (µm)", self._gx),
            ("GDS offset Y (µm)", self._gy),
            ("FOV W (nm)", self._fw),
            ("FOV H (nm)", self._fh),
            ("nm/px", self._nmpx),
            ("Notes", self._notes),
        ]
        for i, (lbl, w) in enumerate(fields):
            form.addWidget(QLabel(lbl), i, 0)
            form.addWidget(w, i, 1)

        right.addWidget(self._form_box, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        right.addWidget(buttons)
        root.addLayout(right, 1)

        self._suppress = False
        self._refresh_tree()
        self._set_form_enabled(False)

    # ── Helpers ─────────────────────────────────────────────────────────

    def _refresh_tree(self, select: Optional[tuple[str, Optional[str]]] = None
                      ) -> None:
        self._tree.blockSignals(True)
        self._tree.clear()
        for pid in sorted(self._catalog):
            top = QListWidgetItem(f"📦 {pid}")
            top.setData(Qt.ItemDataRole.UserRole, ("part", pid, None))
            self._tree.addItem(top)
            for cid in sorted(self._catalog[pid].chips):
                row = QListWidgetItem(f"     ↳ {cid}")
                row.setData(Qt.ItemDataRole.UserRole, ("chip", pid, cid))
                self._tree.addItem(row)
        self._tree.blockSignals(False)
        if select is not None:
            self._select(*select)

    def _select(self, pid: str, cid: Optional[str]) -> None:
        for i in range(self._tree.count()):
            data = self._tree.item(i).data(Qt.ItemDataRole.UserRole)
            if cid is None and data == ("part", pid, None):
                self._tree.setCurrentRow(i)
                return
            if cid is not None and data == ("chip", pid, cid):
                self._tree.setCurrentRow(i)
                return

    def _current(self) -> Optional[tuple[str, str, Optional[str]]]:
        it = self._tree.currentItem()
        if it is None:
            return None
        return it.data(Qt.ItemDataRole.UserRole)

    def _set_form_enabled(self, on: bool) -> None:
        for w in (self._desc, self._cx, self._cy, self._cw, self._ch,
                  self._gx, self._gy, self._fw, self._fh, self._nmpx,
                  self._notes):
            w.setEnabled(on)

    def _on_tree_select(self) -> None:
        cur = self._current()
        if cur is None:
            self._set_form_enabled(False)
            self._form_box.setTitle("(select an entry on the left)")
            return
        kind, pid, cid = cur
        self._suppress = True
        if kind == "part":
            part = self._catalog[pid]
            self._form_box.setTitle(f"PART · {pid}")
            self._desc.setText(part.description)
            # Numeric fields don't apply to PART — disable them.
            for w in (self._cx, self._cy, self._cw, self._ch, self._gx,
                      self._gy, self._fw, self._fh, self._nmpx, self._notes):
                w.setEnabled(False)
            self._desc.setEnabled(True)
        else:
            chip = self._catalog[pid].chips[cid]
            self._form_box.setTitle(f"CHIP · {pid} / {cid}")
            self._set_form_enabled(True)
            self._desc.setEnabled(False)
            self._desc.setText("")
            self._cx.setValue(chip.chip_x_um)
            self._cy.setValue(chip.chip_y_um)
            self._cw.setValue(chip.chip_w_um)
            self._ch.setValue(chip.chip_h_um)
            self._gx.setValue(chip.gds_off_x_um)
            self._gy.setValue(chip.gds_off_y_um)
            self._fw.setValue(int(chip.fov_w_nm))
            self._fh.setValue(int(chip.fov_h_nm))
            self._nmpx.setValue(float(chip.nm_per_px or 0.0))
            self._notes.setText(chip.notes)
        self._suppress = False

    def _on_form_changed(self) -> None:
        if self._suppress:
            return
        cur = self._current()
        if cur is None:
            return
        kind, pid, cid = cur
        if kind == "part":
            self._catalog[pid].description = self._desc.text()
            return
        chip = self._catalog[pid].chips[cid]
        chip.chip_x_um = float(self._cx.value())
        chip.chip_y_um = float(self._cy.value())
        chip.chip_w_um = float(self._cw.value())
        chip.chip_h_um = float(self._ch.value())
        chip.gds_off_x_um = float(self._gx.value())
        chip.gds_off_y_um = float(self._gy.value())
        chip.fov_w_nm = float(self._fw.value())
        chip.fov_h_nm = float(self._fh.value())
        npx = float(self._nmpx.value())
        chip.nm_per_px = npx if npx > 0 else None
        chip.notes = self._notes.text()

    def _on_add_part(self) -> None:
        pid, ok = QInputDialog.getText(
            self, "Add PART", "New PART code (e.g. TMVG10):")
        if not ok or not pid.strip():
            return
        pid = pid.strip()
        if pid in self._catalog:
            QMessageBox.warning(self, "PART exists",
                                f"PART {pid!r} already exists.")
            return
        self._catalog[pid] = PartSpec()
        self._refresh_tree(select=(pid, None))

    def _on_add_chip(self) -> None:
        cur = self._current()
        if cur is None:
            QMessageBox.information(self, "Pick a PART",
                                    "Select a PART (or one of its CHIPs) first.")
            return
        _, pid, _ = cur
        cid, ok = QInputDialog.getText(
            self, "Add CHIP", f"New CHIP id within {pid}:")
        if not ok or not cid.strip():
            return
        cid = cid.strip()
        part = self._catalog[pid]
        if cid in part.chips:
            QMessageBox.warning(self, "CHIP exists",
                                f"CHIP {cid!r} already exists in {pid}.")
            return
        part.chips[cid] = ChipSpec()
        self._refresh_tree(select=(pid, cid))

    def _on_del(self) -> None:
        cur = self._current()
        if cur is None:
            return
        kind, pid, cid = cur
        target = f"CHIP {pid}/{cid}" if cid else f"PART {pid} (and all CHIPs)"
        if QMessageBox.question(
                self, "Remove?", f"Remove {target}?") != \
                QMessageBox.StandardButton.Yes:
            return
        if cid is None:
            del self._catalog[pid]
            self._refresh_tree()
        else:
            del self._catalog[pid].chips[cid]
            self._refresh_tree(select=(pid, None))

    def _on_save(self) -> None:
        try:
            save_catalog(default_catalog_path(), self._catalog)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed",
                                 f"Could not write catalog: {exc}")
            return
        self.accept()


class FineAlignPanel(QGroupBox):
    """Multi-POI template settings + auto fine-alignment controls (plan F3).

    Toggle one or more POI layers on the left LayerPanel; each appears here
    with its own FG grey-level spinbox. They composite into one synthetic
    template (shared BG / blur), which is matched against the SEM with
    ``cv2.matchTemplate``. Emits ``run_requested`` / ``export_all_requested`` /
    ``preview_requested`` for MainWindow to do the work."""

    run_requested = pyqtSignal()
    export_all_requested = pyqtSignal()
    preview_requested = pyqtSignal()
    results_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Fine Align", parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)

        self._poi_lbl = QLabel("POI: (none — click the POI button on a layer in the LAYERS column)")
        self._poi_lbl.setWordWrap(True)
        self._poi_lbl.setStyleSheet(_hint_qss(_FS_CAPTION))
        self._poi_set = False

        # Per-POI FG grey rows live in their own box, rebuilt by set_pois().
        self._poi_box = QWidget(self)
        self._poi_box_layout = QVBoxLayout(self._poi_box)
        self._poi_box_layout.setContentsMargins(0, 0, 0, 0)
        self._poi_box_layout.setSpacing(3)
        self._poi_fg_spins: dict[tuple, QSpinBox] = {}
        self._poi_keys: list[tuple] = []

        def _spin(lo, hi, val, step=1, dec=0):
            s = QDoubleSpinBox(self) if dec else QSpinBox(self)
            s.setRange(lo, hi)
            s.setValue(val)
            s.setSingleStep(step)
            if dec:
                s.setDecimals(dec)
            return s

        self._bg = _spin(0, 255, 80)
        self._bg.setToolTip("Background grey level for non-structure pixels "
                            "(shared by all POI layers).")
        self._blur = _spin(0.0, 20.0, 1.0, 0.5, 2)
        self._blur.setToolTip("Gaussian blur σ (px) softening template edges.")
        self._radius = _spin(0, 100_000, 200, 50)
        self._radius.setToolTip(
            "Search radius (nm) around the coarse position; bounds how far the "
            "template can shift.")
        self._thresh = _spin(0.0, 1.0, 0.5, 0.05, 2)
        self._thresh.setToolTip(
            "Score threshold: matches at or above are 'good' (green).")
        # F14: parallel worker (process) count for Run all + image/mask export.
        # 0 = auto (one per CPU, capped). Persisted so it survives a restart.
        saved_workers = QSettings("GLAS", "GLAS").value(
            "batch_workers", 0, type=int)
        self._workers = _spin(0, 64, int(saved_workers))
        self._workers.setToolTip(
            "Parallel worker processes for 'Run all' and image/mask export.\n"
            "0 = auto (one per CPU core, capped). Raise on a many-core box; "
            "lower to limit CPU / memory.")
        self._workers.valueChanged.connect(
            lambda v: QSettings("GLAS", "GLAS").setValue("batch_workers", int(v)))

        # Secondary (not primaryBtn): a disabled primary renders as washed-out
        # pale-orange with faint text; secondary greys cleanly (M7 R6).
        self._run_btn = QPushButton("Run fine align", self)
        self._run_btn.setEnabled(False)
        self._run_btn.setToolTip(
            "Refine the alignment of the currently loaded ROI against the "
            "selected SEM image (needs ≥1 POI + loaded ROI + image).")
        self._run_btn.clicked.connect(self.run_requested)

        self._export_all_btn = QPushButton("Export all…", self)
        self._export_all_btn.setEnabled(False)
        self._export_all_btn.setToolTip(
            "Fine-align every image that hasn't been run yet (the ones you "
            "already ran are reused), then open the export dialog for the whole "
            "dataset. Slow on big files; cancellable.")
        self._export_all_btn.clicked.connect(self.export_all_requested)

        self._preview_btn = QPushButton("Preview template…", self)
        self._preview_btn.setEnabled(False)
        self._preview_btn.setToolTip(
            "Show SEM / GDS / synthetic composite template side by side for "
            "the current image (needs ≥1 POI + image).")
        self._preview_btn.clicked.connect(self.preview_requested)

        self._results_btn = QPushButton("Results…", self)
        self._results_btn.setToolTip(
            "Open the batch results overview (table + score histogram + "
            "residual scatter). Available after a single or batch run.")
        self._results_btn.clicked.connect(self.results_requested)

        self._result_lbl = QLabel("")
        self._result_lbl.setStyleSheet(f"font-size:{_FS_LABEL}px;")

        rows = [
            ("span", self._poi_lbl), ("span", self._poi_box),
            ("Background GL", self._bg), ("Blur σ (px)", self._blur),
            ("Search radius (nm)", self._radius),
            ("Score threshold", self._thresh),
            ("Parallel workers (0 = auto)", self._workers),
            ("span", self._run_btn), ("span", self._export_all_btn),
            ("span", self._preview_btn), ("span", self._results_btn),
            ("span", self._result_lbl),
        ]
        # Single-column form (label above input) so labels aren't truncated in
        # the narrow panel (M7 R2), matching CoordinateSetupPanel.
        r = 0
        for label, w in rows:
            if label == "span":
                grid.addWidget(w, r, 0)
                r += 1
            else:
                cap = QLabel(label)
                cap.setStyleSheet(_hint_qss(_FS_CAPTION))
                grid.addWidget(cap, r, 0)
                grid.addWidget(w, r + 1, 0)
                r += 2

    def set_pois(self, items: list) -> None:
        """Rebuild the per-POI FG rows from ``items`` (``[(key, label), ...]``),
        preserving any FG value already entered for a key (F3)."""
        prev = {k: s.value() for k, s in self._poi_fg_spins.items()}
        while self._poi_box_layout.count():
            it = self._poi_box_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._poi_fg_spins = {}
        self._poi_keys = []
        for key, label in items:
            row = QWidget(self._poi_box)
            hl = QHBoxLayout(row)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(4)
            name = QLabel(label, row)
            name.setStyleSheet(_hint_qss(_FS_CAPTION))
            name.setToolTip(label)
            spin = QSpinBox(row)
            spin.setRange(0, 255)
            spin.setValue(int(prev.get(key, 200)))
            spin.setFixedWidth(58)
            spin.setToolTip("Foreground grey level for this POI layer's "
                            "structure pixels.")
            hl.addWidget(name, 1)
            hl.addWidget(QLabel("Foreground GL", row))
            hl.addWidget(spin)
            self._poi_box_layout.addWidget(row)
            self._poi_fg_spins[key] = spin
            self._poi_keys.append(key)

        self._poi_set = bool(items)
        n = len(items)
        self._poi_lbl.setText(
            f"POI: {n} layer{'s' if n != 1 else ''} → composite template"
            if self._poi_set else "POI: (none — click the POI button on a layer in the LAYERS column)")
        self._update_enabled()

    def set_fgs(self, fgs: dict) -> None:
        """Set per-POI Foreground GL spin values by layer key (F5 M4 restore)."""
        for k, v in fgs.items():
            s = self._poi_fg_spins.get(k)
            if s is not None and v is not None:
                s.setValue(int(v))

    def poi_fgs(self) -> dict:
        """Map of ``key -> fg_glv`` for the active POI layers (F3)."""
        return {k: int(s.value()) for k, s in self._poi_fg_spins.items()}

    def _update_enabled(self, running: bool = False) -> None:
        on = self._poi_set and not running
        self._run_btn.setEnabled(on)
        self._export_all_btn.setEnabled(on)
        self._preview_btn.setEnabled(on)

    def set_running(self, running: bool) -> None:
        """Disable the run buttons while a batch is in flight."""
        self._update_enabled(running)

    def values(self) -> dict:
        return {
            "bg_glv": int(self._bg.value()),
            "blur_sigma_px": float(self._blur.value()),
            "search_radius_nm": float(self._radius.value()),
            "score_threshold": float(self._thresh.value()),
            "max_workers": int(self._workers.value()),
        }

    def set_result(self, score: float, dx_nm: float, dy_nm: float) -> None:
        thr = float(self._thresh.value())
        color = (_TK_SUCCESS if score >= thr
                 else "#b8860b" if score >= max(0.0, thr - 0.2) else _TK_DANGER)
        self._result_lbl.setStyleSheet(_result_qss(color))
        self._result_lbl.setText(
            f"score {score:.3f}   Δ ({dx_nm:,.0f}, {dy_nm:,.0f}) nm")

    def clear_result(self) -> None:
        self._result_lbl.setText("")


def _gray_to_pixmap(arr: np.ndarray) -> QPixmap:
    """uint8 grayscale ndarray → QPixmap (F3 preview)."""
    a = np.ascontiguousarray(arr.astype(np.uint8))
    h, w = a.shape[:2]
    img = QImage(a.tobytes(), w, h, w, QImage.Format.Format_Grayscale8)
    return QPixmap.fromImage(img.copy())


def _rgb_to_pixmap(arr: np.ndarray) -> QPixmap:
    """uint8 (H, W, 3) RGB ndarray → QPixmap (F3 preview)."""
    a = np.ascontiguousarray(arr.astype(np.uint8))
    h, w = a.shape[:2]
    img = QImage(a.tobytes(), w, h, 3 * w, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(img.copy())


class TemplatePreviewDialog(QDialog):
    """SEM / GDS / synthetic-template preview plus before/after residual
    overlays for the current image, so a multi-POI composite can be eyeballed
    against the real SEM and the fine-align correction can be seen (F3 M5 +
    F5 M1). ``before_rgb`` / ``after_rgb`` are optional outline overlays
    (coarse anchor vs coarse+refined); when given they add two more tiles."""

    _TILE = 260

    def __init__(self, parent, sem: np.ndarray, gds_rgb: np.ndarray,
                 template: np.ndarray, subtitle: str = "",
                 before_rgb: Optional[np.ndarray] = None,
                 after_rgb: Optional[np.ndarray] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("POI template preview")
        v = QVBoxLayout(self)
        if subtitle:
            cap = QLabel(subtitle)
            cap.setWordWrap(True)
            cap.setStyleSheet(_hint_qss(_FS_LABEL))
            v.addWidget(cap)
        tiles = [("SEM", _gray_to_pixmap(sem)),
                 ("GDS", _rgb_to_pixmap(gds_rgb)),
                 ("Template", _gray_to_pixmap(template))]
        if before_rgb is not None:
            tiles.append(("Overlay · before", _rgb_to_pixmap(before_rgb)))
        if after_rgb is not None:
            tiles.append(("Overlay · after", _rgb_to_pixmap(after_rgb)))
        # Wrap into rows of 3 so 5 tiles don't overflow the screen width.
        grid = QGridLayout()
        grid.setSpacing(10)
        for i, (title, pm) in enumerate(tiles):
            col = QVBoxLayout()
            t = QLabel(title)
            t.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            t.setStyleSheet(f"font-weight:700; color:{_TK_ACCENT_DK.name()};")
            pic = QLabel()
            pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pic.setFixedSize(self._TILE, self._TILE)
            pic.setStyleSheet(f"background:#1a1a1a; border:1px solid {_TK_BORDER.name()};")
            if not pm.isNull():
                pic.setPixmap(pm.scaled(
                    self._TILE, self._TILE, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
            col.addWidget(t)
            col.addWidget(pic)
            holder = QWidget()
            holder.setLayout(col)
            grid.addWidget(holder, i // 3, i % 3)
        v.addLayout(grid)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        v.addWidget(btns)


class _ScoreHistogram(QWidget):
    """Self-painted score distribution bars + a threshold marker line (F5 M2
    C5). Lightweight QPainter — no matplotlib dependency."""

    def __init__(self, bins, threshold: float, lo: float = 0.0,
                 hi: float = 1.0, parent=None) -> None:
        super().__init__(parent)
        self._bins = list(bins)
        self._threshold = threshold
        self._lo, self._hi = lo, hi
        self.setMinimumSize(220, 150)

    def paintEvent(self, ev) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        ml, mr, mt, mb = 8.0, 8.0, 22.0, 18.0
        p.fillRect(self.rect(), QColor("#fbf6ef"))
        p.setPen(QColor("#3f3428"))
        p.drawText(QRectF(0, 2, w, 16), Qt.AlignmentFlag.AlignHCenter,
                   "Score distribution")
        plot_w = max(1.0, w - ml - mr)
        plot_h = max(1.0, h - mt - mb)
        n = len(self._bins)
        mx = max(self._bins) if self._bins else 0
        if n and mx > 0:
            bw = plot_w / n
            for i, c in enumerate(self._bins):
                bh = (c / mx) * plot_h
                x = ml + i * bw
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#e0863a"))
                p.drawRect(QRectF(x + 1, mt + (plot_h - bh), bw - 2, bh))
        # threshold vertical line
        if self._hi > self._lo:
            tx = ml + (self._threshold - self._lo) / (self._hi - self._lo) * plot_w
            p.setPen(QPen(QColor("#c0392b"), 1.5, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(tx, mt), QPointF(tx, mt + plot_h))
        p.setPen(QColor("#6f6254"))
        p.drawLine(QPointF(ml, mt + plot_h), QPointF(ml + plot_w, mt + plot_h))
        p.drawText(QRectF(ml, mt + plot_h + 2, plot_w, 14),
                   Qt.AlignmentFlag.AlignLeft, f"{self._lo:.1f}")
        p.drawText(QRectF(ml, mt + plot_h + 2, plot_w, 14),
                   Qt.AlignmentFlag.AlignRight, f"{self._hi:.1f}")
        p.end()


class _ResidualScatter(QWidget):
    """Self-painted (dx, dy) residual scatter with an origin cross and a median
    marker, to spot a systematic shift vs. random spread vs. outliers (F5 M2
    C1). ``points`` is ``[(dx_nm, dy_nm), ...]``; ``median`` is ``(mx, my)`` or
    None. +dy is drawn upward (cartesian)."""

    def __init__(self, points, median, parent=None) -> None:
        super().__init__(parent)
        self._pts = [(float(x), float(y)) for x, y in points]
        self._median = median
        self.setMinimumSize(220, 200)

    def paintEvent(self, ev) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        p.fillRect(self.rect(), QColor("#fbf6ef"))
        p.setPen(QColor("#3f3428"))
        p.drawText(QRectF(0, 2, w, 16), Qt.AlignmentFlag.AlignHCenter,
                   "Residuals (nm)")
        m = 26.0
        cx = w / 2.0
        cy = h / 2.0 + 6.0
        half = min(w, h) / 2.0 - m
        rng = 1.0
        for x, y in self._pts:
            rng = max(rng, abs(x), abs(y))
        if self._median is not None:
            rng = max(rng, abs(self._median[0]), abs(self._median[1]))
        rng *= 1.15
        scale = half / rng if rng > 0 else 1.0
        # axes
        p.setPen(QPen(QColor("#cbbca6"), 1))
        p.drawLine(QPointF(cx - half, cy), QPointF(cx + half, cy))
        p.drawLine(QPointF(cx, cy - half), QPointF(cx, cy + half))
        # points
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(192, 134, 58, 170))
        for x, y in self._pts:
            px = cx + x * scale
            py = cy - y * scale
            p.drawEllipse(QPointF(px, py), 3.0, 3.0)
        # median marker
        if self._median is not None:
            mxp = cx + self._median[0] * scale
            myp = cy - self._median[1] * scale
            p.setPen(QPen(QColor("#1f6f43"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawLine(QPointF(mxp - 6, myp), QPointF(mxp + 6, myp))
            p.drawLine(QPointF(mxp, myp - 6), QPointF(mxp, myp + 6))
        p.setPen(QColor("#6f6254"))
        p.drawText(QRectF(cx, cy - half - 2, half, 14),
                   Qt.AlignmentFlag.AlignRight, f"±{rng:,.0f}")
        p.end()


class _NumItem(QTableWidgetItem):
    """Table item that sorts by a numeric key instead of its display text, so
    score / dx / dy columns order correctly (blanks sort last)."""

    def __init__(self, text: str, sortval: float) -> None:
        super().__init__(text)
        self._sortval = sortval

    def __lt__(self, other) -> bool:  # type: ignore[override]
        try:
            return self._sortval < other._sortval
        except AttributeError:
            return super().__lt__(other)


class BatchResultsPanel(QWidget):
    """In-window batch fine-align workspace (F7): an inline progress strip
    (shown while a batch runs), a sortable results table (score colour-coded,
    optional below-threshold filter), a score histogram + residual scatter, and
    a one-click 'apply median residual to origin δ' action. Lives in the centre
    splitter beside the SEM overlay; double-clicking a row asks MainWindow to
    swap the overlay to that image in place. Same data/logic as the old
    ``FineAlignResultsDialog`` — only the container changed."""

    image_activated = pyqtSignal(str)
    apply_median_requested = pyqtSignal(float, float)
    cancel_requested = pyqtSignal()
    back_requested = pyqtSignal()
    # F13: (image_ids, overrides) — re-run a subset with overridden params.
    rerun_requested = pyqtSignal(list, dict)

    _OK_BG = QColor("#dff3e6")
    _LOW_BG = QColor("#fdf2cf")
    _BAD_BG = QColor("#f7ded9")

    _SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._rows: list = []
        self._threshold = 0.5
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(8)

        # Header: back to alignment + title.
        header = QHBoxLayout()
        self._back_btn = QPushButton("←  Back to alignment", self)
        self._back_btn.setToolTip("Leave the batch workspace and return to the "
                                  "single-image alignment view.")
        self._back_btn.clicked.connect(self.back_requested)
        header.addWidget(self._back_btn)
        title = QLabel("Batch fine-align", self)
        title.setStyleSheet(f"font-weight:600; color:{_TK_ACCENT_DK.name()};")
        header.addWidget(title, 1)
        v.addLayout(header)

        # Inline progress strip (hidden when idle).
        self._prog_box = QWidget(self)
        pb = QVBoxLayout(self._prog_box)
        pb.setContentsMargins(0, 0, 0, 0)
        pb.setSpacing(4)
        self._bar = _AnimatedBar(self._prog_box)
        pb.addWidget(self._bar)
        prow = QHBoxLayout()
        self._spinner = QLabel(self._SPINNER[0], self._prog_box)
        self._spinner.setStyleSheet(
            "color:#c97028; font-size:18px;"
            " font-family:'Consolas','DejaVu Sans Mono',monospace;")
        self._spinner.setFixedWidth(22)
        prow.addWidget(self._spinner)
        self._prog_lbl = QLabel("", self._prog_box)
        self._prog_lbl.setStyleSheet("color:#6f6254; font-size:12px;")
        prow.addWidget(self._prog_lbl, 1)
        self._cancel_btn = QPushButton("Cancel", self._prog_box)
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        prow.addWidget(self._cancel_btn)
        pb.addLayout(prow)
        v.addWidget(self._prog_box)
        self._prog_box.hide()

        self._cancelled = False
        self._spin_idx = 0
        self._progress: Optional[tuple] = None
        self._cur_id = ""
        self._elapsed = QElapsedTimer()
        self._tick = QTimer(self)
        self._tick.setInterval(120)
        self._tick.timeout.connect(self._on_tick)

        # Summary + filter.
        self._summary = QLabel("No batch results yet.", self)
        self._summary.setStyleSheet(
            f"font-weight:600; color:{_TK_ACCENT_DK.name()};")
        v.addWidget(self._summary)
        self._only_low = QCheckBox("Only show score below threshold", self)
        self._only_low.toggled.connect(lambda _on: self._fill_table())
        v.addWidget(self._only_low)

        # Results table.
        self._table = QTableWidget(0, 6, self)
        self._table.setHorizontalHeaderLabels(
            ["Image", "Score", "dx (nm)", "dy (nm)", "Used r (px)", "Status"])
        self._table.setSortingEnabled(True)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        # F13: multi-row select so 'Re-run selected' can act on several images.
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.cellDoubleClicked.connect(self._on_cell_activated)
        v.addWidget(self._table, 1)

        # Charts (rebuilt on each refresh).
        self._charts = QWidget(self)
        self._charts_l = QHBoxLayout(self._charts)
        self._charts_l.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self._charts)

        btn_row = QHBoxLayout()
        self._apply_btn = QPushButton("Apply median residual to origin δ", self)
        self._apply_btn.setToolTip(
            "Shift the global origin δ by the median (dx, dy) of all matched "
            "images, then re-run to see if residuals converge.")
        self._apply_btn.clicked.connect(self._on_apply_median)
        self._apply_btn.setEnabled(False)
        btn_row.addWidget(self._apply_btn)
        btn_row.addStretch(1)
        v.addLayout(btn_row)

        # F13: re-run a subset (low-score or selected rows) with overridden
        # params, instead of re-running the whole dataset. Per-POI FG GL is
        # inherited live from the Fine Align panel (adjust it there to override).
        rr_box = QGroupBox("Re-run low-score / selected", self)
        rr = QGridLayout(rr_box)
        rr.setContentsMargins(8, 6, 8, 6)
        rr.setHorizontalSpacing(6)
        rr.setVerticalSpacing(4)
        self._rr_radius = QSpinBox(rr_box)
        self._rr_radius.setRange(0, 100_000)
        self._rr_radius.setSingleStep(50)
        self._rr_radius.setValue(200)
        self._rr_radius.setToolTip("Search radius (nm) for the re-run.")
        self._rr_bg = QSpinBox(rr_box)
        self._rr_bg.setRange(0, 255)
        self._rr_bg.setValue(80)
        self._rr_bg.setToolTip("Background grey level for the re-run template.")
        self._rr_blur = QDoubleSpinBox(rr_box)
        self._rr_blur.setRange(0.0, 20.0)
        self._rr_blur.setSingleStep(0.5)
        self._rr_blur.setDecimals(2)
        self._rr_blur.setValue(1.0)
        self._rr_blur.setToolTip("Gaussian blur σ (px) for the re-run template.")
        for col, (lbl, w) in enumerate((
                ("Search radius (nm)", self._rr_radius),
                ("Background GL", self._rr_bg),
                ("Blur σ (px)", self._rr_blur))):
            cap = QLabel(lbl, rr_box)
            cap.setStyleSheet(_hint_qss(_FS_CAPTION))
            rr.addWidget(cap, 0, col)
            rr.addWidget(w, 1, col)
        rr_hint = QLabel("Per-POI FG GL inherited from the Fine Align panel.",
                         rr_box)
        rr_hint.setStyleSheet(_hint_qss(_FS_CAPTION))
        rr.addWidget(rr_hint, 2, 0, 1, 3)
        self._rr_low_btn = QPushButton("Re-run low-score", rr_box)
        self._rr_low_btn.setToolTip(
            "Re-run every image with status 'low-score' using the params above; "
            "results are kept only where the new score is higher.")
        self._rr_low_btn.clicked.connect(self._on_rerun_low)
        self._rr_sel_btn = QPushButton("Re-run selected", rr_box)
        self._rr_sel_btn.setToolTip(
            "Re-run the rows selected in the table above (Ctrl/Shift-click for "
            "several); results are kept only where the new score is higher.")
        self._rr_sel_btn.clicked.connect(self._on_rerun_selected)
        rr.addWidget(self._rr_low_btn, 3, 0, 1, 2)
        rr.addWidget(self._rr_sel_btn, 3, 2)
        v.addWidget(rr_box)

    # ── data ────────────────────────────────────────────────────────────────
    def set_rows(self, rows, threshold: float,
                 rebuild_charts: bool = True) -> None:
        self._rows = list(rows)
        self._threshold = threshold
        n_ok = sum(1 for r in self._rows if r["status"] == "ok")
        n_low = sum(1 for r in self._rows if r["status"] == "low-score")
        self._summary.setText(
            f"{len(self._rows)} images  ·  {n_ok} ok  ·  {n_low} low-score  ·  "
            f"threshold {threshold:.2f}")
        self._fill_table()
        # F8: the histogram/scatter teardown+rebuild is the expensive part;
        # skip it during streaming (rebuild_charts=False) and only redraw the
        # charts on the final refresh.
        if rebuild_charts:
            self._rebuild_charts()
        self._apply_btn.setEnabled(self._median_residual() is not None)

    def _visible_rows(self) -> list:
        if self._only_low.isChecked():
            return [r for r in self._rows if r["score"] is not None
                    and r["score"] < self._threshold]
        return self._rows

    def _fill_table(self) -> None:
        rows = self._visible_rows()
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            score = r["score"]
            id_item = QTableWidgetItem(str(r["image_id"]))
            id_item.setData(Qt.ItemDataRole.UserRole, r["image_id"])
            self._table.setItem(i, 0, id_item)
            self._table.setItem(i, 1, _NumItem(
                "" if score is None else f"{score:.3f}",
                -1.0 if score is None else score))
            self._table.setItem(i, 2, _NumItem(
                "" if r["dx_nm"] is None else f"{r['dx_nm']:,.0f}",
                1e18 if r["dx_nm"] is None else r["dx_nm"]))
            self._table.setItem(i, 3, _NumItem(
                "" if r["dy_nm"] is None else f"{r['dy_nm']:,.0f}",
                1e18 if r["dy_nm"] is None else r["dy_nm"]))
            self._table.setItem(i, 4, _NumItem(
                str(r["used_radius"]), float(r["used_radius"])))
            self._table.setItem(i, 5, QTableWidgetItem(r["status"]))
            bg = (self._OK_BG if r["status"] == "ok"
                  else self._LOW_BG if r["status"] == "low-score"
                  else self._BAD_BG if r["status"] not in ("not-run",)
                  else None)
            if bg is not None:
                for c in range(6):
                    self._table.item(i, c).setBackground(bg)
        self._table.setSortingEnabled(True)
        self._table.resizeColumnsToContents()

    def _rebuild_charts(self) -> None:
        # F14: the score histogram was dropped (low value + its teardown/rebuild
        # was the costly part of each streaming refresh); keep only the residual
        # scatter, which actually guides alignment (median residual → origin δ).
        while self._charts_l.count():
            it = self._charts_l.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        pts = [(r["dx_nm"], r["dy_nm"]) for r in self._rows
               if r["dx_nm"] is not None and r["status"] in ("ok", "low-score")]
        self._charts_l.addWidget(
            _ResidualScatter(pts, self._median_residual()), 1)

    def _on_cell_activated(self, row: int, _col: int) -> None:
        item = self._table.item(row, 0)
        if item is not None:
            iid = item.data(Qt.ItemDataRole.UserRole)
            if iid:
                self.image_activated.emit(str(iid))

    def _median_residual(self):
        xs = [r["dx_nm"] for r in self._rows if r["dx_nm"] is not None
              and r["status"] in ("ok", "low-score")]
        ys = [r["dy_nm"] for r in self._rows if r["dy_nm"] is not None
              and r["status"] in ("ok", "low-score")]
        if not xs:
            return None
        return (_median(xs), _median(ys))

    def _on_apply_median(self) -> None:
        med = self._median_residual()
        if med is not None:
            self.apply_median_requested.emit(med[0], med[1])

    # ── F13: subset re-run ────────────────────────────────────────────────────
    def set_rerun_defaults(self, values: dict) -> None:
        """Seed the re-run override spins from the Fine Align panel's current
        values, so a re-run starts from the same params unless the user tweaks
        them here."""
        self._rr_radius.setValue(int(values.get("search_radius_nm", 200)))
        self._rr_bg.setValue(int(values.get("bg_glv", 80)))
        self._rr_blur.setValue(float(values.get("blur_sigma_px", 1.0)))

    def _rerun_overrides(self) -> dict:
        return {
            "search_radius_nm": float(self._rr_radius.value()),
            "bg_glv": int(self._rr_bg.value()),
            "blur_sigma_px": float(self._rr_blur.value()),
        }

    def _selected_image_ids(self) -> list:
        ids = []
        for idx in self._table.selectionModel().selectedRows():
            item = self._table.item(idx.row(), 0)
            if item is not None:
                iid = item.data(Qt.ItemDataRole.UserRole)
                if iid is not None:
                    ids.append(iid)
        return ids

    def _on_rerun_low(self) -> None:
        ids = [r["image_id"] for r in self._rows if r["status"] == "low-score"]
        if ids:
            self.rerun_requested.emit(ids, self._rerun_overrides())

    def _on_rerun_selected(self) -> None:
        ids = self._selected_image_ids()
        if ids:
            self.rerun_requested.emit(ids, self._rerun_overrides())

    def _set_rerun_enabled(self, on: bool) -> None:
        self._rr_low_btn.setEnabled(on)
        self._rr_sel_btn.setEnabled(on)

    # ── inline progress ───────────────────────────────────────────────────────
    def start_progress(self) -> None:
        self._cancelled = False
        self._progress = None
        self._cur_id = ""
        self._cancel_btn.setEnabled(True)
        self._bar.set_indeterminate(True)
        self._prog_lbl.setText("Starting…")
        self._elapsed.start()
        self._tick.start()
        self._prog_box.show()
        self._set_rerun_enabled(False)

    def set_progress(self, done: int, total: int, image_id: str = "") -> None:
        self._progress = (done, total)
        self._cur_id = image_id
        self._bar.set_fraction(done / total if total else 0.0)
        self._refresh_detail()

    def end_progress(self, text: str = "") -> None:
        self._tick.stop()
        self._prog_box.hide()
        self._set_rerun_enabled(True)

    def _on_cancel_clicked(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        self._cancel_btn.setEnabled(False)
        self.cancel_requested.emit()

    def _on_tick(self) -> None:
        self._spin_idx = (self._spin_idx + 1) % len(self._SPINNER)
        self._spinner.setText(self._SPINNER[self._spin_idx])
        self._bar.advance()
        self._refresh_detail()

    def _refresh_detail(self) -> None:
        secs = int(self._elapsed.elapsed() / 1000)
        elapsed = f"Elapsed {secs // 60}:{secs % 60:02d}"
        if self._progress is not None:
            done, total = self._progress
            pct = int(100 * done / total) if total else 0
            eta = ""
            el = self._elapsed.elapsed() / 1000.0
            if 0 < done < total and el > 0:
                rem = el / done * (total - done)
                eta = f"  ·  ETA {int(rem) // 60}:{int(rem) % 60:02d}"
            cur = f"  ·  {self._cur_id}" if self._cur_id else ""
            detail = f"{done} / {total}  ·  {pct}%  ·  {elapsed}{eta}{cur}"
        else:
            detail = elapsed
        if self._cancelled:
            detail += "  ·  cancelling…"
        self._prog_lbl.setText(detail)


class _ImageListDelegate(QStyledItemDelegate):
    """Paints a small status badge in the right margin of each SEM image row
    (fine-align score colour-coded, or a neutral ``no coords`` tag). Badge
    text / colours travel on the item via the UserRole+2..+4 data roles."""

    _BADGE_MARGIN = 6
    _BADGE_H = 16
    _BADGE_PADDING = 8

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        badge_text = index.data(Qt.ItemDataRole.UserRole + 2)
        badge_fg = index.data(Qt.ItemDataRole.UserRole + 3)
        badge_bg = index.data(Qt.ItemDataRole.UserRole + 4)
        if not badge_text:
            return
        painter.save()
        fm = QFontMetrics(option.font)
        text_w = fm.horizontalAdvance(badge_text) + self._BADGE_PADDING * 2
        badge_rect = QRect(
            option.rect.right() - text_w - self._BADGE_MARGIN,
            option.rect.center().y() - self._BADGE_H // 2,
            text_w,
            self._BADGE_H,
        )
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(badge_bg))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(badge_rect, 3, 3)
        painter.setPen(QColor(badge_fg))
        painter.setFont(option.font)
        painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge_text)
        painter.restore()


class SemPanel(QFrame):
    """Right column: a single 'Load SEM…' button (KLARF / image folder via a
    drop-down) + PART/CHIP catalog (F21) + always-visible Alignment δ panel
    + the image list. Owns no file I/O — it asks MainWindow to open dialogs
    and is handed the resulting :class:`sem_loader.SemImage` list."""

    load_klarf_requested = pyqtSignal()
    load_folder_requested = pyqtSignal()
    load_roi_requested = pyqtSignal()
    image_selected = pyqtSignal(object)   # sem_loader.SemImage

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("rightPanel")
        # 320 px reserves room for a vertical scrollbar (~16 px) plus the
        # combo / spin / button widgets so their controls (▼, ↑↓, …) aren't
        # clipped when the column scrolls (reported on PR #11 screenshot).
        self.setFixedWidth(320)
        # M7 R4: scroll the whole column when both collapsibles + the list
        # exceed the window height, instead of clipping.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("semScrollContent")
        content.setStyleSheet(
            f"QWidget#semScrollContent {{ background: {_TK_BG_PANEL.name()}; }}")
        scroll.setWidget(content)
        v = QVBoxLayout(content)
        v.setContentsMargins(10, 12, 10, 12)
        v.setSpacing(11)

        # U6: the "SEM" panel-title label was redundant with the prominent
        # Load SEM… button immediately below it — removed.

        btn_row = QHBoxLayout()
        self.load_sem_btn = QPushButton("Load SEM…")
        self.load_sem_btn.setToolTip(
            "Load SEM images from a KLARF defect list (carries die-corner "
            "coordinates for auto-jump) or from a plain image folder "
            "(no coordinates).")
        self.load_sem_btn.setStyleSheet(_LOAD_SEM_BTN_QSS)
        sem_menu = QMenu(self.load_sem_btn)
        act_klarf = sem_menu.addAction("From KLARF file…")
        act_klarf.triggered.connect(lambda: self.load_klarf_requested.emit())
        act_folder = sem_menu.addAction("From image folder…")
        act_folder.triggered.connect(lambda: self.load_folder_requested.emit())
        self.load_sem_btn.setMenu(sem_menu)
        btn_row.addWidget(self.load_sem_btn)
        btn_row.addStretch(1)
        v.addLayout(btn_row)

        # F21 M2: PART/CHIP dropdowns replace the legacy Coordinate Setup
        # form. Always visible (no collapse) — selecting CHIP is the primary
        # setup step. ``self.coord_setup`` is preserved as the attribute
        # name so existing signal wiring keeps working.
        self.coord_setup = PartChipPanel(self)
        v.addWidget(self.coord_setup)

        # U7: thin separator between the PART/CHIP block and the Alignment δ
        # block so the right column's four concerns read as four distinct
        # sections instead of one undifferentiated wall of text.
        sep1 = QFrame(self)
        sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setStyleSheet(f"color:{_TK_BORDER.name()};")
        sep1.setFixedHeight(1)
        v.addWidget(sep1)

        # F21 M3: Alignment δ readout, always visible directly below the
        # PART/CHIP block. The numeric value is owned by MainWindow.
        self.alignment_delta = AlignmentDeltaPanel(self)
        # Shortcuts for legacy call sites that wired Set/Clear to MainWindow.
        self.set_offset_btn = self.alignment_delta.set_btn
        self.clear_offset_btn = self.alignment_delta.clear_btn
        # S7: δ has no operational meaning until a SEM image is loaded.
        # Start disabled (greyed) so the controls don't read as actionable
        # before there is something to align against. ``set_images``
        # re-enables the panel as soon as a KLARF / image list arrives.
        self.alignment_delta.setEnabled(False)
        v.addWidget(self.alignment_delta)

        # Image list — the primary defect-navigation control — gets the
        # stretch space so it's the prominent element in the column.
        self.list = QListWidget(self)
        self.list.setMinimumHeight(120)
        _config_list(self.list)
        self.list.setItemDelegate(_ImageListDelegate(self.list))
        self.list.itemClicked.connect(self._on_clicked)
        v.addWidget(self.list, 1)

        # ROI load is an explicit action (not auto-triggered on every FOV
        # edit) so the parser isn't re-run on each keystroke.
        # Secondary (not primary orange) so the only emphasized CTA is the
        # toolbar's "Open OASIS…" entry point (M7 #5).
        self.load_roi_btn = QPushButton("Load GDS ROI here  ▶")
        self.load_roi_btn.setToolTip(
            "Random-access load of the GDS around the selected image "
            "(needs 'Open OASIS (ROI)…' first). Re-click after changing "
            "FOV / Custom override to reload.")
        self.load_roi_btn.clicked.connect(self.load_roi_requested)
        v.addWidget(self.load_roi_btn)

        # Fine Align is only needed while aligning — start collapsed; expands
        # when a POI is selected (MainWindow._on_pois_changed).
        self.fine_align = FineAlignPanel(self)
        self._fine_section = self._wrap_section(
            "Fine Align", self.fine_align, collapsed=True)
        v.addWidget(self._fine_section)

        self._images: list = []
        self._scores: dict = {}
        self._show_list_placeholder()

    def _show_list_placeholder(self) -> None:
        """Fill the empty image list with a muted hint so it doesn't read as a
        blank grey void before any SEM is loaded."""
        self.list.clear()
        item = QListWidgetItem("No SEM images — use “Load SEM…”.")
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setForeground(QColor(_TK_TEXT_HINT))
        self.list.addItem(item)

    def _wrap_section(self, title: str, panel: "QGroupBox", *, collapsed: bool):
        """Wrap a panel in a CollapsibleSection (or return it bare when the
        widget isn't importable). The section header supplies the title, so the
        inner QGroupBox title is cleared to avoid a duplicate label."""
        if CollapsibleSection is None:
            return panel
        panel.setTitle("")
        sec = CollapsibleSection(title, tier=2, collapsed=collapsed)
        sec.add_widget(panel)
        return sec

    def set_fine_collapsed(self, val: bool) -> None:
        if CollapsibleSection is not None:
            self._fine_section.set_collapsed(val)
        self._scores: dict = {}     # image_id -> (score, threshold)

    def set_images(self, images: list) -> None:
        self._images = list(images)
        self._scores = {}
        self.list.clear()
        # S7: an alignment δ is only meaningful once there are SEM images
        # to align. Enable / disable the panel to match.
        self.alignment_delta.setEnabled(bool(self._images))
        if not self._images:
            self._show_list_placeholder()
            return
        for img in self._images:
            item = QListWidgetItem(f"{img.image_id}: {img.filename}")
            item.setData(Qt.ItemDataRole.UserRole, img)
            if not img.has_coords:
                item.setForeground(QColor(_TK_TEXT_SEC))
                item.setData(Qt.ItemDataRole.UserRole + 2, "no coords")
                item.setData(Qt.ItemDataRole.UserRole + 3, "#9a8878")
                item.setData(Qt.ItemDataRole.UserRole + 4, "#f4f0ea")
            self.list.addItem(item)

    def set_score(self, image_id, score: float, threshold: float) -> None:
        """Annotate the matching list row with a colour-coded fine-align
        score badge (green ≥ threshold, amber near, red below) — plan M4b."""
        self._scores[image_id] = (score, threshold)
        for i in range(self.list.count()):
            item = self.list.item(i)
            img = item.data(Qt.ItemDataRole.UserRole)
            if img is None or img.image_id != image_id:
                continue
            score_text = f"{score:.2f}"
            if score >= threshold:
                fg, bg = "#3e7f5d", "#ebf7f0"   # green
            elif score >= threshold * 0.7:
                fg, bg = "#b8860b", "#fff8e0"   # amber
            else:
                fg, bg = "#a32d2d", "#feeeee"   # red
            item.setData(Qt.ItemDataRole.UserRole + 2, score_text)
            item.setData(Qt.ItemDataRole.UserRole + 3, fg)
            item.setData(Qt.ItemDataRole.UserRole + 4, bg)
            break

    def clear_score(self, image_id) -> None:
        """Remove a fine-align score badge (e.g. when a re-run fails), keeping
        the 'no coords' tag for images that never had coordinates (PR#4)."""
        self._scores.pop(image_id, None)
        for i in range(self.list.count()):
            item = self.list.item(i)
            img = item.data(Qt.ItemDataRole.UserRole)
            if img is None or img.image_id != image_id:
                continue
            if not getattr(img, "has_coords", True):
                item.setData(Qt.ItemDataRole.UserRole + 2, "no coords")
                item.setData(Qt.ItemDataRole.UserRole + 3, "#9a8878")
                item.setData(Qt.ItemDataRole.UserRole + 4, "#f4f0ea")
            else:
                item.setData(Qt.ItemDataRole.UserRole + 2, None)
                item.setData(Qt.ItemDataRole.UserRole + 3, None)
                item.setData(Qt.ItemDataRole.UserRole + 4, None)
            self.list.viewport().update()
            break

    def _on_clicked(self, item: QListWidgetItem) -> None:
        img = item.data(Qt.ItemDataRole.UserRole)
        if img is not None:
            self.image_selected.emit(img)


# ── F22: First-run welcome ──────────────────────────────────────────────────


# Five-slide onboarding: tuple of (icon_name, title, html_body). Kept as a
# module-level constant so the layout pass is just data + a stacked widget.
_WELCOME_SLIDES: list[tuple[str, str, str]] = [
    (
        "target",
        "Welcome to GLAS",
        "<p><b>GLAS</b> aligns <b>GDS / OASIS layouts</b> to <b>SEM "
        "images</b> so downstream measurement tools can find the right "
        "feature on every defect.</p>"
        "<p>This 5-step tour walks you through the workflow — you can "
        "re-open it any time from <b>Help → Show welcome…</b></p>",
    ),
    (
        "folder-open",
        "Step 1 — Open OASIS",
        "<p>Click the orange <b>Open OASIS…</b> button on the toolbar. "
        "A wizard guides you through three short steps:</p>"
        "<ol>"
        "<li>Pick the <code>.oas</code> file</li>"
        "<li>Scan + pick the layer(s) you want to load</li>"
        "<li>Confirm the root (top) cell (a recommendation is pre-selected)</li>"
        "</ol>"
        "<p>Big files are fine — GLAS only loads geometry around each "
        "clicked defect.</p>",
    ),
    (
        "layers",
        "Step 2 — Pick PART / CHIP",
        "<p>In the right column, pick <b>PART</b> then <b>CHIP</b> from "
        "the dropdowns. The catalog supplies the chip-corner offset, the "
        "default FOV, and the overlay scale — no need to type RFL "
        "numbers.</p>"
        "<p>Need a CHIP that isn't listed? An administrator can add it "
        "via the catalog editor.</p>",
    ),
    (
        "target",
        "Step 3 — Click a defect, drag, Set Offset",
        "<p>Load a KLARF (right column <b>Load SEM…</b>), then click a "
        "defect in the list. GLAS auto-jumps and loads the GDS ROI.</p>"
        "<p>The half-transparent GDS overlay drops on top of the SEM. "
        "<b>Left-drag</b> it into alignment, then click <b>Set Offset</b> "
        "in the ALIGNMENT δ block to lock it in. δ applies to every "
        "defect afterwards.</p>"
        "<p>Wheel zooms; middle / right-drag pans; <code>Ctrl + arrows</code> "
        "nudge δ by 10 nm.</p>",
    ),
    (
        "download",
        "Step 4 — Fine align, then export",
        "<p>Tick a layer's <b>POI</b> button on the left to mark it as a "
        "template, expand the right-column <b>Fine Align</b> section, and "
        "click <b>Run all</b>. Each defect gets a colour-coded score "
        "badge in the image list (green / amber / red).</p>"
        "<p>When you're happy, use the toolbar's <b>Export Alignment…</b> "
        "to write CSV / JSON for the downstream measurement recipe.</p>"
        "<p>That's the whole workflow. Have fun!</p>",
    ),
]


class WelcomeDialog(QDialog):
    """First-run onboarding (F22). 5 ASCII / icon-based slides with
    Prev / Next navigation, persistent "Don't show again" preference."""

    SETTING_KEY = "welcome_shown_v1"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Welcome to GLAS")
        self.setModal(True)
        self.setMinimumSize(640, 420)

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 12)
        v.setSpacing(10)

        self._stack = QStackedWidget(self)
        for icon, title, body in _WELCOME_SLIDES:
            self._stack.addWidget(self._build_slide(icon, title, body))
        v.addWidget(self._stack, 1)

        # Progress dots (● for visited / current, ○ for upcoming).
        self._dots = QLabel("", self)
        self._dots.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._dots.setStyleSheet(
            f"color:{_TK_ACCENT_DK.name()}; font-size:{_FS_LABEL}px; "
            f"letter-spacing:4px;")
        v.addWidget(self._dots)

        # Footer: Don't show again | Prev | Next/Got it
        footer = QHBoxLayout()
        self._dont_show = QCheckBox("Don't show this again", self)
        self._dont_show.setChecked(True)
        footer.addWidget(self._dont_show)
        footer.addStretch(1)
        self._prev_btn = QPushButton("◀  Prev", self)
        self._prev_btn.clicked.connect(self._on_prev)
        self._next_btn = QPushButton("Next  ▶", self)
        self._next_btn.clicked.connect(self._on_next)
        self._next_btn.setProperty("variant", "primary")
        footer.addWidget(self._prev_btn)
        footer.addWidget(self._next_btn)
        v.addLayout(footer)

        self._update_state()

    @staticmethod
    def _build_slide(icon_name: str, title: str, body_html: str) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(12, 18, 12, 12)
        h.setSpacing(18)
        # Icon column (big version of an existing toolbar icon).
        ic = _qicon(icon_name)
        icon_lbl = QLabel()
        if not ic.isNull():
            icon_lbl.setPixmap(ic.pixmap(96, 96))
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignTop)
        icon_lbl.setFixedWidth(112)
        h.addWidget(icon_lbl)
        # Text column.
        text_col = QVBoxLayout()
        text_col.setSpacing(8)
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"color:{_TK_ACCENT_DK.name()}; "
            f"font-size:{max(_FS_LABEL + 5, 18)}px; font-weight:700;")
        text_col.addWidget(title_lbl)
        body_lbl = QLabel(body_html)
        body_lbl.setWordWrap(True)
        body_lbl.setTextFormat(Qt.TextFormat.RichText)
        body_lbl.setStyleSheet(
            f"color:{_TK_TEXT_PRI.name()}; font-size:{_FS_LABEL}px; "
            f"line-height:1.5;")
        text_col.addWidget(body_lbl)
        text_col.addStretch(1)
        h.addLayout(text_col, 1)
        return w

    def _update_state(self) -> None:
        idx = self._stack.currentIndex()
        n = self._stack.count()
        self._prev_btn.setEnabled(idx > 0)
        is_last = idx == n - 1
        self._next_btn.setText("Got it  ✓" if is_last else "Next  ▶")
        self._dots.setText(
            "  ".join("●" if i <= idx else "○" for i in range(n)))

    def _on_prev(self) -> None:
        i = self._stack.currentIndex()
        if i > 0:
            self._stack.setCurrentIndex(i - 1)
            self._update_state()

    def _on_next(self) -> None:
        i = self._stack.currentIndex()
        if i < self._stack.count() - 1:
            self._stack.setCurrentIndex(i + 1)
            self._update_state()
        else:
            self.accept()

    def accept(self) -> None:  # type: ignore[override]
        if self._dont_show.isChecked():
            QSettings("GLAS", "GLAS").setValue(self.SETTING_KEY, True)
        super().accept()


class DebugReportDialog(QDialog):
    """F10: show a copyable plain-text diagnostic report; optionally note the
    sidecar file it was saved to."""

    def __init__(self, parent, title: str, text: str,
                 saved_path: Optional[str] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(min(720, _screen_avail()[0]), min(560, _screen_avail()[1]))
        v = QVBoxLayout(self)
        if saved_path:
            v.addWidget(QLabel(f"Saved to: {saved_path}"))
        edit = QPlainTextEdit(self)
        edit.setPlainText(text)
        edit.setReadOnly(True)
        edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mono = edit.font()
        mono.setFamily("monospace")
        edit.setFont(mono)
        v.addWidget(edit, 1)
        row = QHBoxLayout()
        copy_btn = QPushButton(" Copy to clipboard", self)
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(text))
        row.addWidget(copy_btn)
        row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        row.addWidget(bb)
        v.addLayout(row)


class OasisExportDialog(QDialog):
    """F9 M3: pick layers (raw + Boolean) and an optional GDS-coordinate crop
    region, then export to OASIS. Synthetic layers get an editable output
    layer/datatype (their internal layer is -1, not writable to OASIS)."""

    def __init__(self, parent, entries, doc_bbox,
                 whole_chip_available: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export OASIS")
        self.setMinimumWidth(_capped_min_width(420))
        self._rows: list[tuple] = []
        self._crop: Optional[tuple] = None
        self._sel: list[tuple] = []
        self._specs: list[tuple] = []

        v = QVBoxLayout(self)

        v.addWidget(QLabel("Export scope"))
        self._scope = QComboBox()
        self._scope.addItem("Current FOV (loaded geometry)", "fov")
        if whole_chip_available:
            self._scope.addItem("Whole chip (re-walk + recompute, tiled)", "whole")
        self._scope.currentIndexChanged.connect(self._on_scope_changed)
        v.addWidget(self._scope)
        v.addSpacing(8)

        v.addWidget(QLabel("Layers to export"))

        grid = QGridLayout()
        grid.addWidget(QLabel("Layer"), 0, 2)
        grid.addWidget(QLabel("Datatype"), 0, 3)
        synth_default = 1000
        for i, e in enumerate(entries, start=1):
            cb = QCheckBox(e.key.label())
            cb.setChecked(True)
            if e.display_name:
                cb.setText(f"{e.key.label()}  ({e.display_name})")
            # OASIS layer/datatype are unbounded unsigned ints (the writer
            # supports large values); use the full QSpinBox int range so a
            # prefilled raw ID > 65535 isn't silently clamped and remapped.
            ls = QSpinBox()
            ls.setRange(0, 2_147_483_647)
            ds = QSpinBox()
            ds.setRange(0, 2_147_483_647)
            if e.key.synthetic:
                ls.setValue(synth_default)
                ds.setValue(0)
                synth_default += 1
            else:
                ls.setValue(max(0, int(e.key.layer)))
                ds.setValue(max(0, int(e.key.datatype)))
            grid.addWidget(cb, i, 0, 1, 2)
            grid.addWidget(ls, i, 2)
            grid.addWidget(ds, i, 3)
            self._rows.append((e, cb, ls, ds))
        v.addLayout(grid)

        v.addSpacing(10)
        self._crop_label = QLabel(
            "Crop region (GDS nm) — leave all blank to export whole layout")
        v.addWidget(self._crop_label)
        crop_grid = QGridLayout()
        self._crop_edits = {}
        x0, y0, x1, y1 = doc_bbox if doc_bbox else (0, 0, 0, 0)
        fields = [("x1 (left)", x0), ("y1 (bottom)", y0),
                  ("x2 (right)", x1), ("y2 (top)", y1)]
        for col, (lbl, ph) in enumerate(fields):
            crop_grid.addWidget(QLabel(lbl), 0, col)
            ed = QLineEdit()
            ed.setPlaceholderText(f"{ph:.0f}" if doc_bbox else "")
            crop_grid.addWidget(ed, 1, col)
            self._crop_edits[lbl] = ed
        v.addLayout(crop_grid)

        self._doc_bbox = doc_bbox
        self._fill_btn = QPushButton(" Use current view / ROI bounds")
        self._fill_btn.setEnabled(bool(doc_bbox))
        self._fill_btn.clicked.connect(self._fill_crop_from_bbox)
        v.addWidget(self._fill_btn)

        v.addSpacing(8)
        self._debug_cb = QCheckBox(
            "Debug: re-read the written file and append a diagnostic report")
        v.addWidget(self._debug_cb)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self)
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _fill_crop_from_bbox(self) -> None:
        if not self._doc_bbox:
            return
        x0, y0, x1, y1 = self._doc_bbox
        vals = {"x1 (left)": x0, "y1 (bottom)": y0,
                "x2 (right)": x1, "y2 (top)": y1}
        for lbl, ed in self._crop_edits.items():
            ed.setText(f"{vals[lbl]:.0f}")

    def _on_scope_changed(self) -> None:
        # Whole-chip ignores the crop region (it covers the entire chip).
        whole = self.scope() == "whole"
        self._crop_label.setEnabled(not whole)
        self._fill_btn.setEnabled(not whole and bool(self._doc_bbox))
        for ed in self._crop_edits.values():
            ed.setEnabled(not whole)

    def _on_accept(self) -> None:
        if self.scope() == "fov":
            raw = [ed.text().strip() for ed in self._crop_edits.values()]
            filled = [t for t in raw if t]
            if filled and len(filled) != 4:
                QMessageBox.warning(
                    self, "Incomplete crop region",
                    "Enter all four coordinates, or leave all blank to export "
                    "the whole layout.")
                return
            if filled:
                try:
                    x1, y1, x2, y2 = (float(t) for t in raw)
                except ValueError:
                    QMessageBox.warning(self, "Invalid crop region",
                                        "Coordinates must be numbers.")
                    return
                if x1 == x2 or y1 == y2:
                    QMessageBox.warning(self, "Invalid crop region",
                                        "The region has zero width or height.")
                    return
                self._crop = (x1, y1, x2, y2)
            else:
                self._crop = None
        else:
            self._crop = None     # whole chip — no crop

        checked = [(e, ls.value(), ds.value())
                   for e, cb, ls, ds in self._rows if cb.isChecked()]
        self._specs = checked
        self._sel = [(out_l, out_d, list(e.polygons))
                     for e, out_l, out_d in checked]
        self.accept()

    def scope(self) -> str:
        return self._scope.currentData()

    def selected_layers(self) -> list:
        """(out_layer, out_datatype, polygons) for the FOV path."""
        return self._sel

    def selected_specs(self) -> list:
        """(LayerEntry, out_layer, out_datatype) — lets the whole-chip path
        recover each layer's source (raw layer/datatype or recipe)."""
        return self._specs

    def crop_bbox(self):
        return self._crop

    def debug_enabled(self) -> bool:
        return self._debug_cb.isChecked()


class AlignmentExportDialog(QDialog):
    """Pick the format (CSV / JSON) and which images to export (plan M5).
    Defaults to every image checked."""

    def __init__(self, parent, images, refined=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export alignment")
        self.setMinimumWidth(_capped_min_width(360))
        # F13: per-image scores so the GDS-mask export can show how many images
        # clear the threshold. ``refined`` is ``{image_id: (dx, dy, score)}``.
        self._refined = dict(refined or {})
        v = QVBoxLayout(self)

        v.addWidget(QLabel("Format"))
        self._fmt = QComboBox(self)
        self._fmt.addItems(["CSV (.csv)", "JSON (.json)"])
        v.addWidget(self._fmt)

        v.addWidget(QLabel("Images"))
        self._list = QListWidget(self)
        for img in images:
            tag = "" if img.has_coords else "  (no coords)"
            item = QListWidgetItem(f"{img.image_id}: {img.filename}{tag}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, img.image_id)
            self._list.addItem(item)
        self._list.itemChanged.connect(lambda _it: self._update_thr_count())
        v.addWidget(self._list, 1)

        row = QHBoxLayout()
        all_btn = QPushButton("Select all", self)
        all_btn.clicked.connect(lambda: self._set_all(True))
        none_btn = QPushButton("Select none", self)
        none_btn.clicked.connect(lambda: self._set_all(False))
        row.addWidget(all_btn)
        row.addWidget(none_btn)
        row.addStretch(1)
        v.addLayout(row)

        # F5 M6: optional image export for MMH hand-off. When either box is
        # ticked, the chosen images are also written as PNGs plus a manifest
        # (image_id ↔ filenames ↔ dx/dy/score) into a folder picked next.
        img_box = QGroupBox("Also export images (for MMH hand-off)", self)
        ibl = QVBoxLayout(img_box)
        self._exp_raw = QCheckBox("Raw SEM image PNG", img_box)
        self._exp_overlay = QCheckBox(
            "Aligned GDS-overlay PNG (POI outlines on SEM)", img_box)
        ibl.addWidget(self._exp_raw)
        ibl.addWidget(self._exp_overlay)

        # F15 (replaces F13's binary mask): a simulated-GLV grayscale working
        # image + an integer ROI label map, both gated by the score threshold so
        # MMH can trust every emitted artifact without fallback logic.
        self._exp_gray = QCheckBox(
            "Export simulated GLV grayscale (.png)", img_box)
        self._exp_gray.setToolTip(
            "Write a per-image SEM-like grayscale: each POI layer painted at its "
            "FG grey on the background grey at the aligned anchor (the downstream "
            "measurement image). Only images with a fine-align score at or above "
            "the threshold below are written.")
        self._exp_label = QCheckBox("Export ROI label map (.png)", img_box)
        self._exp_label.setToolTip(
            "Write a per-image uint8 label map: 0 = background, 1..N = the Nth "
            "POI layer (no blur, crisp ROI). Downstream reads a region with a "
            "single `gray[label == id]` boolean index. Same score gate as above.")
        ibl.addWidget(self._exp_gray)
        ibl.addWidget(self._exp_label)
        thr_row = QHBoxLayout()
        thr_row.addWidget(QLabel("Score threshold", img_box))
        self._score_thr = QDoubleSpinBox(img_box)
        self._score_thr.setRange(0.0, 1.0)
        self._score_thr.setSingleStep(0.05)
        self._score_thr.setDecimals(2)
        self._score_thr.setValue(0.8)
        self._score_thr.valueChanged.connect(lambda _v: self._update_thr_count())
        thr_row.addWidget(self._score_thr)
        thr_row.addStretch(1)
        ibl.addLayout(thr_row)
        self._thr_count = QLabel("", img_box)
        self._thr_count.setStyleSheet(_hint_qss(_FS_CAPTION))
        ibl.addWidget(self._thr_count)
        # Threshold + count only matter when grayscale/label export is on.
        def _thr_enabled(_on=None):
            on = self._exp_gray.isChecked() or self._exp_label.isChecked()
            self._score_thr.setEnabled(on)
            self._update_thr_count()
        self._exp_gray.toggled.connect(_thr_enabled)
        self._exp_label.toggled.connect(_thr_enabled)
        self._score_thr.setEnabled(False)
        self._update_thr_count()
        v.addWidget(img_box)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _set_all(self, on: bool) -> None:
        st = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        for i in range(self._list.count()):
            self._list.item(i).setCheckState(st)

    def _checked_ids(self) -> list:
        return [self._list.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self._list.count())
                if self._list.item(i).checkState() == Qt.CheckState.Checked]

    def _update_thr_count(self) -> None:
        """Show how many of the checked images would get a grayscale/label at the
        current threshold (F15: score >= threshold among the selected images)."""
        if not (self._exp_gray.isChecked() or self._exp_label.isChecked()):
            self._thr_count.setText("")
            return
        thr = float(self._score_thr.value())
        n = sum(1 for iid in self._checked_ids()
                if fine_align.mask_should_export(self._refined.get(iid), thr))
        self._thr_count.setText(f"{n} image(s) ≥ threshold will be written")

    def selected(self) -> tuple:
        """``(fmt, [image_id, ...], export_raw, export_overlay, export_gray,
        export_label, score_threshold)`` where ``fmt`` is 'csv' or 'json'."""
        fmt = "csv" if self._fmt.currentIndex() == 0 else "json"
        return (fmt, self._checked_ids(), self._exp_raw.isChecked(),
                self._exp_overlay.isChecked(), self._exp_gray.isChecked(),
                self._exp_label.isChecked(), float(self._score_thr.value()))


# ── Main window ──────────────────────────────────────────────────────────────


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("GLAS")
        self.resize(1200, 800)
        # Left (260) + right (300) fixed panels + a usable centre + chrome: stop
        # the window shrinking into a cramped, broken layout (F3 M1). Capped to
        # the screen so small displays still open.
        _avw, _avh = _screen_avail()
        self.setMinimumSize(min(940, _avw), min(600, _avh))

        # F9: developer mode gates advanced features (OASIS export). Off by
        # default; enabled by clicking the About-dialog icon 5 times. Persisted
        # in QSettings so it survives a restart.
        self._dev_mode = bool(
            QSettings("GLAS", "GLAS").value("dev_mode", False, type=bool))
        # Match batch fine-align timing to the persisted dev-mode state before
        # any pool spawns, so warmed workers inherit GLAS_FA_TIMING from launch.
        self._apply_fa_timing()

        _icon_path = Path(__file__).resolve().parent / "icons" / "glas_icon_32.svg"
        if _icon_path.exists():
            self.setWindowIcon(QIcon(str(_icon_path)))

        # Top-level horizontal split: layers | (gds canvas | sem viewer) | sem panel
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.layer_panel = LayerPanel(splitter)

        center = QWidget(splitter)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)
        toolbar = self._wrap_toolbar(self._build_toolbar())
        center_layout.addWidget(toolbar)

        # Workflow guidance strip (M6.6): shows the next step to take; hides
        # once setup is complete. Updated by _update_guidance().
        self._guidance = QLabel("")
        self._guidance.setStyleSheet(
            f"background: {_TK_GUIDANCE_BG}; color: {_TK_GUIDANCE_TEXT}; "
            f"border-bottom: 1px solid {_TK_GUIDANCE_BORDER}; "
            f"padding: 5px 12px; font-size: {_FS_LABEL}px;")
        self._guidance.setWordWrap(True)
        center_layout.addWidget(self._guidance)

        self._center_split = QSplitter(Qt.Orientation.Horizontal, center)
        self.canvas = GdsCanvas(self._center_split)
        # F7: batch workspace pane (left = results, right = SEM overlay).
        # Entered via Run all / Results…, not a view mode; hidden otherwise.
        self.batch_panel = BatchResultsPanel(self._center_split)
        self.sem_viewer = SemViewer(self._center_split)
        self._center_split.addWidget(self.canvas)       # idx 0
        self._center_split.addWidget(self.batch_panel)  # idx 1
        self._center_split.addWidget(self.sem_viewer)   # idx 2
        self._center_split.setStretchFactor(0, 1)
        self._center_split.setStretchFactor(1, 1)
        self._center_split.setStretchFactor(2, 1)
        center_layout.addWidget(self._center_split, 1)
        # Single-view UX (M6.3): show SEM+overlay big by default; the GDS
        # overview is an opt-in view mode.
        self.canvas.setVisible(False)
        self.batch_panel.setVisible(False)
        self._view_mode = "sem"
        self._batch_active = False
        self._prev_view_mode = "sem"
        self.batch_panel.image_activated.connect(self._on_results_image_activated)
        self.batch_panel.apply_median_requested.connect(
            self._on_apply_median_residual)
        self.batch_panel.back_requested.connect(self._exit_batch_workspace)
        self.batch_panel.cancel_requested.connect(self._on_fa_cancel_clicked)
        self.batch_panel.rerun_requested.connect(self._on_rerun_requested)
        # M7-ov #9 corner minimap removed (no longer useful in practice — the
        # GDS view-mode overview pane already shows defect positions).

        self.sem_panel = SemPanel(splitter)
        # F21 M3: Set/Clear Offset are wired through the AlignmentDeltaPanel
        # signals (see below); the duplicate-direct wiring on the legacy
        # set_offset_btn / clear_offset_btn attributes is intentionally gone.

        splitter.addWidget(self.layer_panel)
        splitter.addWidget(center)
        splitter.addWidget(self.sem_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        self._main_split = splitter
        self._split_sized = False
        self.setCentralWidget(splitter)

        self._status_bar = QStatusBar(self)
        self.setStatusBar(self._status_bar)
        # Status-bar brand chip (left edge): "GLAS v1.0.0", muted accent
        # colour. Lives here instead of the toolbar so the toolbar is
        # action-only, and the version is always visible for bug reports.
        self._status_brand = QLabel(f"GLAS v{GLAS_VERSION}")
        self._status_brand.setStyleSheet(
            f"color:{_TK_ACCENT_DK.name()}; "
            f"font-size:{_FS_MICRO}px; font-weight:700; "
            f"letter-spacing:1.5px; padding:0 10px;")
        self._status_brand.setToolTip(
            f"GLAS — GDS-Layout Alignment for SEM · version {GLAS_VERSION}")
        self._status_bar.addWidget(self._status_brand)
        self._status_doc = QLabel("no GDS loaded")
        self._status_cursor = QLabel("")
        # F11 M1: dedicated, always-visible GDS cursor coordinate readout (both
        # SEM and GDS panes feed it). Kept separate from _status_cursor so the
        # coordinate isn't clobbered by transient hints.
        self._coord_readout = QLabel("GDS: —")
        self._coord_readout.setStyleSheet(
            "font-weight: 600; padding: 0 8px; color: #3f3428;")
        self._coord_readout.setToolTip(
            "Cursor position in GDS coordinates — type these into the OASIS "
            "export crop fields (nm).")
        self._status_bar.addWidget(self._status_doc, 1)
        self._status_bar.addWidget(self._status_cursor)
        self._status_bar.addPermanentWidget(self._coord_readout)

        self._build_menu()
        self._build_shortcuts()

        # Wire signals.
        self.layer_panel.layers_changed.connect(self._on_layers_changed)
        self.layer_panel.add_expression_requested.connect(self._on_add_expression)
        self.layer_panel.edit_expression_requested.connect(self._on_edit_recipe)
        self.layer_panel.delete_expression_requested.connect(self._on_delete_recipe)
        self.layer_panel.pois_changed.connect(self._on_pois_changed)
        self.canvas.cursor_pos_nm.connect(self._on_cursor)
        self.canvas.defect_clicked.connect(self._on_defect_clicked)
        self.sem_viewer.cursor_gds.connect(self._on_coord)   # F11 M1
        # S12: clicking the empty-viewer CTA pops the same split menu the
        # right column's Load SEM… button shows.
        self.sem_viewer.load_sem_requested.connect(self._on_cta_load_sem)
        self.sem_panel.load_klarf_requested.connect(self._on_load_klarf)
        self.sem_panel.load_folder_requested.connect(self._on_load_folder)
        self.sem_panel.load_roi_requested.connect(self._on_load_roi_clicked)
        self.sem_panel.image_selected.connect(self._on_sem_image_selected)
        self.sem_panel.coord_setup.changed.connect(self._on_coord_changed)
        self.sem_panel.coord_setup.edit_catalog_requested.connect(
            self._on_edit_catalog)
        self.sem_panel.coord_setup.set_dev_mode(self._dev_mode)
        self.sem_panel.alignment_delta.set_requested.connect(self._on_set_offset)
        self.sem_panel.alignment_delta.clear_requested.connect(self._on_clear_offset)
        self.sem_panel.fine_align.run_requested.connect(self._on_run_fine_align)
        self.sem_panel.fine_align.export_all_requested.connect(self._on_export_all)
        self.sem_panel.fine_align.preview_requested.connect(self._on_preview_template)
        self.sem_panel.fine_align.results_requested.connect(self._open_fa_results)
        self.sem_viewer.drag_changed.connect(self._on_overlay_drag)

        self._doc: Optional[GdsDocument] = None
        self._load_path: Optional[str] = None

        # Alignment settings (F21 PartChipPanel is the source of truth;
        # these mirror it via _on_coord_changed). chip + FOV are persisted
        # in / restored from the layer cache. Fine-tune dx/dy were removed
        # (F21 Q6) — all per-FOV correction now goes through origin δ.
        self._chip_corner_x: float = 0.0
        self._chip_corner_y: float = 0.0
        self._fov_w: float = 0.0
        self._fov_h: float = 0.0
        # Constant KLARF->GDS origin correction δ (nm), found by dragging the
        # SEM/GDS overlay (M4a). Applied to every defect; persisted in cache.
        self._origin_dx: float = 0.0
        self._origin_dy: float = 0.0
        self._nm_per_px_manual: float = 0.0
        self._nm_auto: bool = True
        self._sem_images: list = []
        self._current_sem = None
        # M3.5d ROI mode: a persistent random-access reader + chosen root
        # cell + layer. When set, clicking a SEM image loads only the GDS
        # geometry around it instead of relying on a full / partial load.
        self._rar = None
        self._roi_root = None
        self._roi_layers: list[tuple[int, int]] = []
        # Background ROI-walk machinery (M3.5d).
        self._roi_thread: Optional[QThread] = None
        self._roi_worker = None
        self._roi_progress = None
        # F11: whole-chip export worker (set during a running export).
        self._wc_thread: Optional[QThread] = None
        self._wc_worker = None
        self._wc_progress = None
        self._wc_path = None
        self._wc_debug = False
        self._roi_progress_timer: Optional[QTimer] = None
        self._roi_center: Optional[tuple[float, float]] = None
        # Color cycle for synthetic expression layers.
        self._expr_color_idx = 0
        # F4: synthetic-layer "recipes" — the single source of truth for
        # expression layers. Each is {"name", "expr", "bindings", "color"};
        # bindings values are tagged ("raw", l, d) / ("ref", name). The doc's
        # synthetic LayerEntries are derived from these and re-evaluated against
        # each new FOV (ROI load / cache load) so they follow the defect.
        self._recipes: list[dict] = []
        # M4b fine align: chosen POI layer + per-image refined offset.
        self._poi_entries: list[LayerEntry] = []
        self._refined: dict = {}     # image_id -> (dx_nm, dy_nm, score)
        # F13: True while a batch *re-run* is in flight, so results only
        # overwrite when strictly better (rerun_should_overwrite).
        self._fa_rerun_mode = False
        # F24: True while a one-click "Export all" is fine-aligning its un-run
        # images, so the batch finish hands off to the export dialog.
        self._export_after_fa = False
        # F5: per-image (used_radius_px, status) parallel to _refined, for the
        # results table (C3/C4). status: ok / no-coords / missing-file /
        # no-scale / flat.
        self._fa_meta: dict = {}
        # F5 M4: remembered fine-align setup by layer key (visible / colour /
        # opacity / POI / Foreground GL), captured before each ROI reload and
        # re-applied after, so switching defect keeps the setup — the user just
        # presses Run fine align once.
        self._fa_setup: dict = {}
        # M4b "Run all" batch worker state.
        self._fa_thread: Optional[QThread] = None
        self._fa_worker = None
        # F8: coalesce streaming result-table refreshes. Each incoming result
        # only updates the data; this single-shot timer rebuilds the panel at
        # most ~3x/sec instead of once per image (the old per-result refresh was
        # O(N^2) on the GUI thread and froze the UI on big batches).
        self._batch_refresh_timer = QTimer(self)
        self._batch_refresh_timer.setSingleShot(True)
        self._batch_refresh_timer.setInterval(300)
        self._batch_refresh_timer.timeout.connect(
            lambda: self._refresh_batch_panel(rebuild_charts=False))
        # F5 M6 overlay/image export worker state.
        self._ov_thread: Optional[QThread] = None
        self._ov_worker = None
        self._ov_progress = None
        # M5 export: remember the source KLARF / OASIS paths for the manifest.
        self._klarf_path: str = ""
        self._oas_path: str = ""

        # T2: initial state — _status_state also stores the message so a
        # subsequent transient flash can revert back to it.
        self._status_state_msg = "ready — open an OASIS to begin"
        self._status_revert_timer: Optional[QTimer] = None
        self._status_doc.setText(self._status_state_msg)

        self._update_guidance()

    def showEvent(self, ev) -> None:  # type: ignore[override]
        super().showEvent(ev)
        # Pin the fixed side panels and give the centre all remaining width.
        # A QSplitter otherwise dumps leftover space into a fixed-width pane,
        # leaving the right panel floating with a gap to the window edge.
        if not self._split_sized:
            self._split_sized = True
            QTimer.singleShot(0, self._fit_split_sizes)
            # F22: first launch — defer welcome dialog one event loop tick so
            # the main window is already painted underneath it.
            settings = QSettings("GLAS", "GLAS")
            if not settings.value(WelcomeDialog.SETTING_KEY, False, type=bool):
                QTimer.singleShot(0, self._show_welcome_dialog)

    def _show_welcome_dialog(self) -> None:
        """Open the onboarding dialog. Triggered both by first-launch and the
        Help → Show welcome… menu entry."""
        dlg = WelcomeDialog(self)
        dlg.exec()

    def _fit_split_sizes(self) -> None:
        w = self._main_split.width()
        if w <= 0:
            return
        lw = self.layer_panel.width() or 260
        rw = self.sem_panel.width() or 320
        self._main_split.setSizes([lw, max(400, w - lw - rw), rw])

    # ── construction helpers ───────────────────────────────────────────────
    def _build_toolbar(self) -> QWidget:
        bar = QFrame(self)
        bar.setObjectName("gdsToolbar")
        bar.setFrameShape(QFrame.Shape.NoFrame)
        # Scope the background to the frame via an objectName selector — a bare
        # "background:" rule would bleed onto every child button and flatten
        # their variant styling (CLAUDE.md §6).
        bar.setStyleSheet(
            f"QFrame#gdsToolbar {{ background: {_TK_TOOLBAR_BG}; "
            f"border-bottom: 1px solid {_TK_BORDER.name()}; }}"
            # Segmented view-mode buttons: the active one reads as 'selected'.
            f"QFrame#gdsToolbar QPushButton[seg=\"true\"]:checked {{ "
            f"background: {_TK_ACCENT.name()}; color: #ffffff; "
            f"border: 1px solid {_TK_ACCENT_DK.name()}; }}")
        h = QHBoxLayout(bar)
        h.setContentsMargins(10, 6, 10, 6)
        h.setSpacing(8)

        def _divider():
            line = QFrame(bar)
            line.setFrameShape(QFrame.Shape.VLine)
            line.setStyleSheet(f"color: {_TK_BORDER.name()};")
            return line

        def _group(text):
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"color:{_TK_ACCENT_DK.name()}; font-size:{_FS_MICRO}px; "
                f"font-weight:700; letter-spacing:1px; padding: 0 4px;")
            return lbl

        # Brand wordmark moved to the status bar (left edge) so the toolbar
        # only carries actions. See ``_status_brand`` in __init__.

        # ── File group ──
        h.addSpacing(4)
        h.addWidget(_group("FILE"))
        open_btn = QPushButton(_qicon("folder-open"), " Open OASIS…")
        open_btn.setProperty("variant", "primary")
        open_btn.setToolTip(
            "Open an OASIS in random-access ROI mode: scan + pick layer(s) "
            "and a root cell. Clicking a SEM image then loads ONLY the "
            "geometry around it — fast and complete even on huge files. "
            "Needs an S_CELL_OFFSET index.")
        open_btn.clicked.connect(self._on_open_roi)
        h.addWidget(open_btn)
        # U3: Load Cache / Export Cache live in File menu now — they're
        # advanced actions that don't deserve permanent toolbar real estate.

        h.addWidget(_divider())

        # ── View mode (segmented, exclusive) + Minimap (overlay toggle) ──
        # S2: SEM / GDS are mutually exclusive view modes; Minimap is now
        # an independent corner-overlay that composes with both, instead
        # of being a third exclusive mode.
        h.addWidget(_group("VIEW MODE"))
        self._view_group = QButtonGroup(self)
        self._view_group.setExclusive(True)

        def _seg(text, icon, mode, tip):
            b = QPushButton(_qicon(icon), " " + text)
            b.setCheckable(True)
            b.setProperty("seg", "true")
            b.setToolTip(tip)
            b.clicked.connect(lambda: self._set_view_mode(mode))
            self._view_group.addButton(b)
            h.addWidget(b)
            return b

        self._seg_sem = _seg("SEM", "image", "sem",
                             "SEM + overlay only (full width). Shortcut: G")
        self._seg_gds = _seg("GDS", "layers", "gds",
                             "SEM beside the whole-chip GDS overview (50/50). "
                             "Shortcut: G")
        self._seg_sem.setChecked(True)

        h.addWidget(_divider())
        # T1: keep references on self so _refresh_action_states can gate
        # buttons whose meaning depends on what's already loaded.
        self._fit_btn = QPushButton(_qicon("maximize"), " Fit")
        self._fit_btn.setToolTip("Fit the GDS overview to all defects.")
        self._fit_btn.clicked.connect(self._on_fit_view)
        h.addWidget(self._fit_btn)

        # Goto GDS coordinate (µm) — same as klayout Ctrl+G, for direct
        # same-coordinate layout comparison. Loads a ~50µm ROI there.
        self._goto_lbl = QLabel("Goto µm:")
        h.addWidget(self._goto_lbl)
        self._goto_edit = QLineEdit()
        self._goto_edit.setPlaceholderText("e.g. 12345, 6789")
        self._goto_edit.setMaximumWidth(120)
        self._goto_edit.setToolTip(
            "Enter a GDS coordinate (x, y µm) to jump there and load nearby "
            "geometry — same coordinate as klayout's Ctrl+G for layout comparison.")
        self._goto_edit.returnPressed.connect(self._on_goto_gds)
        h.addWidget(self._goto_edit)
        self._goto_btn = QPushButton(_qicon("target"), " Goto")
        self._goto_btn.setToolTip("Goto the typed GDS coordinate (loads a ~50µm ROI there).")
        self._goto_btn.clicked.connect(self._on_goto_gds)
        h.addWidget(self._goto_btn)

        h.addWidget(_divider())

        # ── Export group ──
        h.addWidget(_group("EXPORT"))
        self._align_btn = QPushButton(_qicon("download"), " Export Alignment…")
        self._align_btn.setToolTip(
            "Export per-image alignment offsets (coarse + fine + score) to "
            "CSV / JSON for a future Recipe to anchor its ROI (M5).")
        self._align_btn.clicked.connect(self._on_export_alignment)
        h.addWidget(self._align_btn)

        # F9: OASIS export — advanced, hidden unless developer mode is on.
        self._export_oasis_btn = QPushButton(_qicon("save"), " Export OASIS…")
        self._export_oasis_btn.setToolTip(
            "Export selected raw / Boolean layers to an OASIS (.oas) file, "
            "optionally cropped to a GDS-coordinate region (developer mode).")
        self._export_oasis_btn.clicked.connect(self._on_export_oasis)
        self._export_oasis_btn.setVisible(self._dev_mode)
        h.addWidget(self._export_oasis_btn)

        h.addStretch(1)
        # Bolder labels (user request) — set per-button so it can't bleed
        # background like a parent stylesheet would. Then pin each button's
        # minimum width to its (bold) hint so a non-maximized window can never
        # squeeze a button narrower than its text — the toolbar scrolls
        # horizontally instead (see _wrap_toolbar). (F3 fix)
        for b in bar.findChildren(QPushButton):
            fb = b.font()
            fb.setBold(True)
            b.setFont(fb)
            b.setMinimumWidth(b.sizeHint().width())
        return bar

    def _wrap_toolbar(self, bar: QWidget) -> QScrollArea:
        """Put the toolbar in a horizontal scroll area so a narrow window
        scrolls it instead of clipping button text (F3 fix)."""
        sa = QScrollArea(self)
        sa.setObjectName("gdsToolbarScroll")
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setWidgetResizable(True)
        sa.setWidget(bar)
        sa.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        sa.setStyleSheet(f"QScrollArea#gdsToolbarScroll {{ background: {_TK_TOOLBAR_BG}; }}")
        sb = sa.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        sa.setFixedHeight(bar.sizeHint().height() + sb)
        return sa

    _NUDGE_NM = 10.0   # δ step per arrow-key press

    def _build_shortcuts(self) -> None:
        """Keyboard shortcuts (M6.6): view zoom/reset, overview toggle, and
        arrow-key origin-δ nudge for sub-pixel alignment."""
        def sc(seq, slot):
            s = QShortcut(QKeySequence(seq), self)
            s.activated.connect(slot)
            return s
        sc("Ctrl+0", lambda: (self.sem_viewer.reset_view(),
                              self.sem_viewer.update()))
        sc("Ctrl++", lambda: self.sem_viewer.zoom_by(1.2))
        sc("Ctrl+=", lambda: self.sem_viewer.zoom_by(1.2))
        sc("Ctrl+-", lambda: self.sem_viewer.zoom_by(1.0 / 1.2))
        sc("G", self._cycle_view_mode)
        sc("Ctrl+Up", lambda: self._nudge_origin(0, self._NUDGE_NM))
        sc("Ctrl+Down", lambda: self._nudge_origin(0, -self._NUDGE_NM))
        sc("Ctrl+Left", lambda: self._nudge_origin(-self._NUDGE_NM, 0))
        sc("Ctrl+Right", lambda: self._nudge_origin(self._NUDGE_NM, 0))

    def _nudge_origin(self, dx: float, dy: float) -> None:
        """Shift the global origin δ by (dx, dy) nm and re-jump (M6.6)."""
        self._origin_dx += dx
        self._origin_dy += dy
        self.sem_panel.alignment_delta.set_values(self._origin_dx, self._origin_dy)
        if self._current_sem is not None:
            self._jump_to_image(self._current_sem)
        self._fit_view_to_defects()
        self._status_transient(
            f"origin δ nudged to ({self._origin_dx:,.0f}, "
            f"{self._origin_dy:,.0f}) nm")

    # S2: only two mutually exclusive main views; Minimap is an overlay
    # toggle that composes with either.
    _VIEW_MODES = ("sem", "gds")

    def _set_view_mode(self, mode: str) -> None:
        """Switch the centre view between SEM-only and SEM + GDS overview.
        Also leaves the batch workspace, if active (F7)."""
        if mode not in self._VIEW_MODES:
            return
        self._batch_active = False
        self.batch_panel.setVisible(False)
        self._view_mode = mode
        self.canvas.setVisible(mode == "gds")
        if mode == "gds":
            total = max(2, self._center_split.width())
            self._center_split.setSizes([total // 2, 0, total - total // 2])
        btn = {"sem": self._seg_sem, "gds": self._seg_gds}[mode]
        if not btn.isChecked():
            btn.blockSignals(True)
            btn.setChecked(True)
            btn.blockSignals(False)

    def _enter_batch_workspace(self) -> None:
        """Show the batch workspace (left = results, right = SEM overlay),
        remembering the current view mode so 'Back' can restore it (F7)."""
        if not self._batch_active:
            self._prev_view_mode = self._view_mode
        self._batch_active = True
        self.canvas.setVisible(False)
        self.batch_panel.setVisible(True)
        total = max(2, self._center_split.width())
        left = int(total * 0.55)
        self._center_split.setSizes([0, left, total - left])

    def _exit_batch_workspace(self) -> None:
        """Return from the batch workspace to the previous view mode (F7)."""
        self._set_view_mode(getattr(self, "_prev_view_mode", "sem"))

    def _cycle_view_mode(self) -> None:
        i = self._VIEW_MODES.index(getattr(self, "_view_mode", "sem"))
        self._set_view_mode(self._VIEW_MODES[(i + 1) % len(self._VIEW_MODES)])

    def _on_fit_view(self) -> None:
        """Fit the GDS overview to all defects (Fit action)."""
        self.canvas.fit_to_defects()

    def _on_toggle_overview(self, on: bool) -> None:
        """Back-compat shim: GDS overview on -> 'gds' mode, off -> 'sem'."""
        self._set_view_mode("gds" if on else "sem")

    def _on_layers_changed(self) -> None:
        """A layer's visibility / colour / opacity changed: redraw both the
        GDS overview and the SEM overlay (M6.1/M6.2)."""
        self.canvas.refresh()
        self._update_overlay()

    def _update_guidance(self) -> None:
        """Show the next workflow step; hide the strip once set up (M6.6)."""
        if self._rar is None and self._doc is None:
            msg = "Step 1 — Open an OASIS: toolbar “Open OASIS…” — a 3-page wizard guides you (file → layers → root cell)."
        elif not self._sem_images:
            msg = "Step 2 — Load SEM: right panel “Load SEM…” (KLARF file or image folder)."
        elif self._fov_w <= 0 or self._fov_h <= 0:
            msg = "Step 3 — Pick PART / CHIP (right panel): the catalog supplies chip-corner + FOV automatically."
        elif self._current_sem is None:
            msg = "Step 4 — Click a defect image in the list to jump and load its GDS ROI."
        else:
            msg = ("Step 5 — Left-drag the GDS overlay to align it on the SEM, "
                   "then press Set Offset. Wheel zooms; middle/right-drag pans.")
        self._guidance.setText(msg)
        self._guidance.setVisible(True)
        # T1: every guidance update also refreshes which actions are enabled
        # so the toolbar reflects the current prerequisites.
        self._refresh_action_states()

    def _refresh_action_states(self) -> None:
        """T1: enable/disable toolbar + right-column actions to match the
        current state. Driven by what's loaded; a button is enabled only
        when invoking it would actually do something.

        Defensive ``getattr`` checks let this run even before some widgets
        exist (it's called from ``_update_guidance`` and that fires once
        from ``__init__`` before the menu and toolbar are fully built)."""
        has_doc = self._doc is not None or self._rar is not None
        has_sem = bool(self._sem_images)
        has_defects = has_sem and any(
            getattr(im, "has_coords", False) for im in self._sem_images)
        # GDS overview is only meaningful when there's geometry to draw.
        seg_gds = getattr(self, "_seg_gds", None)
        if seg_gds is not None:
            seg_gds.setEnabled(has_doc)
        # Fit / Goto operate on the overview pane and need a layout.
        for name in ("_fit_btn", "_goto_btn", "_goto_edit", "_goto_lbl"):
            w = getattr(self, name, None)
            if w is not None:
                w.setEnabled(has_doc)
        # Export Alignment needs at least an image list to enumerate rows.
        align = getattr(self, "_align_btn", None)
        if align is not None:
            align.setEnabled(has_sem)
        # Export OASIS (dev) needs geometry; visibility is still gated by
        # dev mode in _refresh_dev_ui.
        oasis = getattr(self, "_export_oasis_btn", None)
        if oasis is not None:
            oasis.setEnabled(has_doc)
        # Right column: Load GDS ROI here needs both a reader and a SEM
        # image to centre on. Disabling it stops the "OASIS not loaded"
        # MessageBox firing on a misclick. T7: once geometry has been
        # loaded for the current image, the action is actually a re-load
        # (after FOV / chip change) — flip the label so it doesn't read
        # as "I haven't loaded anything yet" once a defect is open.
        sem_panel = getattr(self, "sem_panel", None)
        if sem_panel is not None:
            roi_btn = getattr(sem_panel, "load_roi_btn", None)
            if roi_btn is not None:
                roi_btn.setEnabled(self._rar is not None and has_defects)
                already_loaded = (
                    self._doc is not None and bool(self._doc.entries)
                    and self._current_sem is not None)
                roi_btn.setText("Reload GDS ROI  ▶" if already_loaded
                                else "Load GDS ROI here  ▶")

    # ── T2: transient status-bar messages auto-revert ────────────────────────

    def _status_state(self, msg: str) -> None:
        """Set the status-bar's persistent state message. Use for changes
        that reflect the current loaded state (KLARF loaded, ROI opened, ...).
        Cancels any pending transient revert."""
        self._status_state_msg = msg
        timer = getattr(self, "_status_revert_timer", None)
        if timer is not None:
            timer.stop()
        self._status_doc.setText(msg)

    def _status_transient(self, msg: str, ms: int = 4500) -> None:
        """Flash a short confirmation in the status bar then revert to the
        most recent state message. Use for "X complete" style feedback so
        the bar doesn't pile up stale notifications."""
        self._status_doc.setText(msg)
        timer = getattr(self, "_status_revert_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._status_revert_now)
            self._status_revert_timer = timer
        timer.start(ms)

    def _status_revert_now(self) -> None:
        self._status_doc.setText(
            getattr(self, "_status_state_msg",
                    "ready — open an OASIS to begin"))

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("&File")
        open_action = QAction("&Open OASIS…", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._on_open_roi)
        menu.addAction(open_action)
        menu.addSeparator()
        # U3: cache I/O moved from the toolbar to the File menu — used once
        # per session at most, doesn't deserve permanent toolbar space.
        load_cache_action = QAction("&Load cache…", self)
        load_cache_action.triggered.connect(self._on_load_cache)
        menu.addAction(load_cache_action)
        export_cache_action = QAction("&Export cache…", self)
        export_cache_action.triggered.connect(self._on_export_cache)
        menu.addAction(export_cache_action)
        menu.addSeparator()
        # F10: developer-only OASIS diagnostics. Hidden unless dev mode is on.
        self._diagnose_action = QAction("&Diagnose OASIS file…", self)
        self._diagnose_action.triggered.connect(self._on_diagnose_oasis)
        self._diagnose_action.setVisible(self._dev_mode)
        menu.addAction(self._diagnose_action)
        menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu("&Help")
        welcome_action = QAction("Show &welcome…", self)
        welcome_action.triggered.connect(self._show_welcome_dialog)
        help_menu.addAction(welcome_action)
        help_menu.addSeparator()
        about_action = QAction("&About", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    # ── actions ────────────────────────────────────────────────────────────

    def _on_cursor(self, x_nm: float, y_nm: float) -> None:
        self._on_coord((x_nm, y_nm))

    def _on_coord(self, w) -> None:
        """F11 M1: update the dedicated GDS coordinate readout from either
        pane. ``w`` is an ``(x_nm, y_nm)`` tuple or ``None`` (cursor left)."""
        if w is None:
            self._coord_readout.setText("GDS: —")
            return
        x_nm, y_nm = w
        self._coord_readout.setText(
            f"GDS: {x_nm / 1e3:,.3f}, {y_nm / 1e3:,.3f} µm  "
            f"({x_nm:,.0f}, {y_nm:,.0f} nm)")

    # ── M7-ov: defect map on the GDS overview ────────────────────────────────
    def _refresh_overview_defects(self) -> None:
        """Push the defect map (positions + scores) to the overview canvas.
        Uses already-loaded KLARF coords — no parsing / ROI walk (M7-ov #1)."""
        defects = []
        for im in self._sem_images:
            if not im.has_coords:
                continue
            gx, gy = gds_fov.klarf_to_gds(
                im.xrel, im.yrel, self._chip_corner_x, self._chip_corner_y)
            rdx, rdy = self._refined_offset(im)
            gx += self._origin_dx + rdx
            gy += self._origin_dy + rdy
            ref = self._refined.get(im.image_id)
            defects.append((im.image_id, gx, gy, ref[2] if ref else None))
        cur = self._current_sem.image_id if self._current_sem else None
        self.canvas.set_defects(defects, cur)

    def _on_defect_clicked(self, image_id: str) -> None:
        """Clicking a dot in the overview selects that image (same on-demand
        flow as clicking the list — no extra load cost) (M7-ov #2)."""
        img = next((i for i in self._sem_images
                    if i.image_id == image_id), None)
        if img is not None:
            self._on_sem_image_selected(img)

    # ── M3: SEM load + coordinate jump ──────────────────────────────────────
    def _on_cta_load_sem(self) -> None:
        """S12: empty-viewer CTA opens the same KLARF / image-folder split
        menu the right-column Load SEM… button uses, but anchored beneath
        the CTA itself so the dropdown reads as belonging to the button the
        user actually clicked (and not the one in the corner)."""
        menu = self.sem_panel.load_sem_btn.menu()
        if menu is None:
            self._on_load_klarf()
            return
        cta = self.sem_viewer._cta_btn
        menu.exec(cta.mapToGlobal(cta.rect().bottomLeft()))

    def _on_load_klarf(self) -> None:
        # KLARF result files are commonly named <lot>.000 / .001 / … (a
        # numeric "result number" extension), so accept any three-digit
        # extension alongside the named ones.
        path, _ = QFileDialog.getOpenFileName(
            self, "Load KLARF", "",
            "KLARF files (*.klarf *.klf *.txt "
            "*.[0-9][0-9][0-9]);;All files (*)")
        if not path:
            return
        try:
            images = sem_loader.load_klarf(path)
        except Exception as exc:
            QMessageBox.critical(self, "KLARF load failed", str(exc))
            return
        self._sem_images = images
        self._klarf_path = path
        self.sem_panel.set_images(images)
        self._maybe_prewarm_batch_pool()   # F23 M2: batch now plausible
        with_coords = sum(1 for i in images if i.has_coords)
        self._status_state(
            f"KLARF: {len(images)} images ({with_coords} with coords) · "
            f"{Path(path).name}")
        # Overview: frame all defect positions so the FOV marker visibly
        # jumps across the chip as you click different images.
        self._fit_view_to_defects()
        self._update_guidance()

    def _on_load_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Load image folder", "")
        if not path:
            return
        images = sem_loader.load_folder(path)
        self._sem_images = images
        self.sem_panel.set_images(images)
        self._maybe_prewarm_batch_pool()   # F23 M2: batch now plausible
        self._status_state(
            f"Folder: {len(images)} images · {Path(path).name}")
        self._update_guidance()

    def _on_sem_image_selected(self, img) -> None:
        self._current_sem = img
        self.sem_viewer.set_image(img)
        self._jump_to_image(img)
        self._refresh_overview_defects()      # move the "current" ring
        self._update_guidance()
        # Auto-load the ROI geometry around the clicked defect (option 2).
        # Only on an explicit image click — not on fine-tune / coord edits
        # (which also call _jump_to_image) — so the parser isn't re-run on
        # every spinbox keystroke. Skipped if a load is already running.
        if self._rar is not None and self._roi_thread is None \
                and self._fov_w > 0 and self._fov_h > 0:
            pos = self._current_image_gds()
            if pos is not None:
                self._load_roi_around(*pos)

    def _on_coord_changed(self) -> None:
        # F21: PartChipPanel is the source of truth for chip / FOV / scale.
        # Fine-tune dx/dy were removed (Q6) — δ alone carries the residual.
        v = self.sem_panel.coord_setup.values()
        self._chip_corner_x = v["chip_corner_x"]
        self._chip_corner_y = v["chip_corner_y"]
        self._fov_w = v["fov_w"]
        self._fov_h = v["fov_h"]
        self._nm_per_px_manual = v["nm_per_px_manual"]
        self._nm_auto = v["nm_auto"]
        if self._current_sem is not None:
            self._jump_to_image(self._current_sem)
        self._refresh_overview_defects()
        self._update_guidance()

    def _effective_nm_per_px(self) -> float:
        """nm per native image pixel: auto = FOV width / image pixel width,
        else the manually entered value."""
        if not self._nm_auto and self._nm_per_px_manual > 0:
            return self._nm_per_px_manual
        nat = self.sem_viewer.native_size()
        if nat and self._fov_w > 0:
            return self._fov_w / max(1, nat[0])
        return 0.0

    def _update_overlay(self) -> None:
        """Push the loaded GDS ROI geometry to the SEM viewer, anchored on
        the current defect's GDS position, so it can be dragged into
        alignment (M4a)."""
        pos = self._current_image_gds()
        if pos is None or self._doc is None:
            self.sem_viewer.clear_overlay()
            return
        entries = [(e.polygons, e.color, e.fill_alpha())
                   for e in self._doc.visible_entries()
                   if e.polygons]
        self.sem_viewer.set_overlay(entries, pos, self._effective_nm_per_px())

    def _jump_to_image(self, img) -> None:
        """Centre the GDS canvas on the image's converted coordinate and
        draw the FOV box (M3 auto-jump). Needs coords + a positive FOV."""
        if img is None or not img.has_coords:
            self.canvas.clear_fov_marker()
            self._status_cursor.setText("image has no coordinates")
            return
        gx, gy = gds_fov.klarf_to_gds(
            img.xrel, img.yrel, self._chip_corner_x, self._chip_corner_y)
        rdx, rdy = self._refined_offset(img)
        gx += self._origin_dx + rdx
        gy += self._origin_dy + rdy
        # Keep the current (overview) zoom and just move the FOV box, so the
        # marker visibly travels across the chip instead of every image being
        # re-centred + re-zoomed (which made it look like nothing moved).
        self.canvas.set_fov_marker(gx, gy, self._fov_w, self._fov_h)
        self._status_cursor.setText(
            f"image {img.image_id} → GDS ({gx/1e3:,.1f}, {gy/1e3:,.1f}) µm "
            f"= ({gx:,.0f}, {gy:,.0f}) nm")
        # Trace (--trace): print the conversion for klayout/diag comparison.
        # Fires on every jump/drag, so it's level-2 only.
        if oasis_random.TRACE:
            print(f"{devlog.tag('jump')} DID={img.image_id}  "
                  f"XREL={img.xrel} YREL={img.yrel}  "
                  f"chip_corner=({self._chip_corner_x:,.0f}, "
                  f"{self._chip_corner_y:,.0f}) nm  "
                  f"origin=({self._origin_dx:,.0f}, "
                  f"{self._origin_dy:,.0f})  "
                  f"-> gds=({gx/1e3:,.2f}, {gy/1e3:,.2f}) µm", flush=True)
        self._update_overlay()

    def _current_image_gds(self):
        """GDS (x, y) of the selected image incl. origin δ + refined, or None."""
        img = self._current_sem
        if img is None or not img.has_coords:
            return None
        gx, gy = gds_fov.klarf_to_gds(
            img.xrel, img.yrel, self._chip_corner_x, self._chip_corner_y)
        rdx, rdy = self._refined_offset(img)
        return (gx + self._origin_dx + rdx,
                gy + self._origin_dy + rdy)

    def _refined_offset(self, img) -> tuple:
        """Per-image fine-align correction (nm), or (0, 0) if not run (M4b)."""
        if img is None:
            return (0.0, 0.0)
        r = self._refined.get(getattr(img, "image_id", None))
        return (r[0], r[1]) if r else (0.0, 0.0)

    def _fit_view_to_defects(self) -> None:
        """Fit the canvas to the bounding box of every defect's GDS
        position, so the FOV marker visibly jumps across that span as you
        click different images (M3 auto-jump option 1). No-op if no image
        carries coordinates."""
        imgs = [i for i in self._sem_images if i.has_coords]
        if not imgs:
            return
        xs: list[float] = []
        ys: list[float] = []
        for im in imgs:
            gx, gy = gds_fov.klarf_to_gds(
                im.xrel, im.yrel, self._chip_corner_x, self._chip_corner_y)
            xs.append(gx + self._origin_dx)
            ys.append(gy + self._origin_dy)
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        # Pad so edge markers aren't flush against the viewport; at least one
        # FOV so a single-defect dataset still gets a sensible window.
        px = max((x1 - x0) * 0.1, self._fov_w, 1000.0)
        py = max((y1 - y0) * 0.1, self._fov_h, 1000.0)
        self.canvas.set_view_to_bbox(x0 - px, y0 - py, x1 + px, y1 + py)
        self._refresh_overview_defects()

    # ── M4a: SEM↔GDS overlay drag + origin offset δ ──────────────────────────
    def _on_overlay_drag(self) -> None:
        """Live preview of the effective δ while dragging the overlay."""
        dx, dy = self.sem_viewer.drag_offset_nm()
        self.sem_panel.alignment_delta.set_values(
            self._origin_dx - dx, self._origin_dy - dy)

    def _on_set_offset(self) -> None:
        """Fold the current overlay drag into the global origin δ. Folding
        anchor += −drag reproduces the dragged view with drag reset (the
        SemViewer mapping invariant), so the GDS stays put visually."""
        dx, dy = self.sem_viewer.drag_offset_nm()
        if dx == 0 and dy == 0:
            self._status_cursor.setText("drag the GDS overlay first, then Set Offset")
            return
        self._origin_dx -= dx
        self._origin_dy -= dy
        self.sem_viewer.reset_drag()
        self.sem_panel.alignment_delta.set_values(self._origin_dx, self._origin_dy)
        if self._current_sem is not None:
            self._jump_to_image(self._current_sem)
        self._fit_view_to_defects()
        self._status_transient(
            f"origin δ set to ({self._origin_dx:,.0f}, {self._origin_dy:,.0f}) nm")

    def _on_clear_offset(self) -> None:
        self._origin_dx = 0.0
        self._origin_dy = 0.0
        self.sem_viewer.reset_drag()
        self.sem_panel.alignment_delta.set_values(0.0, 0.0)
        if self._current_sem is not None:
            self._jump_to_image(self._current_sem)
        self._fit_view_to_defects()
        self._status_transient("origin δ cleared")

    # ── F3: multi-POI template + auto fine alignment ─────────────────────────
    def _on_pois_changed(self, entries) -> None:
        self._poi_entries = list(entries or [])
        self.sem_panel.fine_align.set_pois(
            [(e.key.key(), _entry_label(e)) for e in self._poi_entries])
        if self._poi_entries:          # expand Fine Align when a POI is picked
            self.sem_panel.set_fine_collapsed(False)
        self._maybe_prewarm_batch_pool()   # F23 M2: POIs ready -> warm the pool

    def _capture_fa_setup(self) -> None:
        """Snapshot per-layer visibility / colour / opacity / POI / Foreground
        GL into ``self._fa_setup`` (keyed by layer key) for restore after the
        next ROI reload (F5 M4)."""
        if self._doc is None:
            return
        fgs = self.sem_panel.fine_align.poi_fgs()
        poi_keys = {e.key.key() for e in self._poi_entries}
        for e in self._doc.entries:
            k = e.key.key()
            self._fa_setup[k] = {
                "visible": e.visible,
                "color": (e.color.red(), e.color.green(), e.color.blue()),
                "opacity": e.opacity,
                "poi": k in poi_keys,
                "fg": fgs.get(k),
            }

    def _apply_fa_setup(self) -> None:
        """Re-apply the remembered setup to the freshly loaded document by layer
        key, so switching defect keeps the fine-align setup (F5 M4). Keys absent
        in this ROI (the defect lacks that layer) are skipped."""
        if not self._fa_setup or self._doc is None:
            return
        poi_keys = []
        for e in self._doc.entries:
            s = self._fa_setup.get(e.key.key())
            if not s:
                continue
            e.visible = bool(s["visible"])
            if s.get("color"):
                e.color = QColor(*s["color"])
            if s.get("opacity") is not None:
                e.opacity = int(s["opacity"])
            if s.get("poi"):
                poi_keys.append(e.key.key())
        # Rebuild rows so checkboxes / swatches reflect the restored state.
        self.layer_panel.set_document(self._doc)
        if poi_keys:
            self.layer_panel.check_pois(poi_keys)
            self.sem_panel.fine_align.set_fgs(
                {k: self._fa_setup[k].get("fg") for k in poi_keys})
        self._update_overlay()
        self.canvas.refresh()

    def _poi_layers(self) -> list:
        """``[(polygons, fg_glv), ...]`` for the active POI layers (F3),
        each at its own FG grey from the panel."""
        fgs = self.sem_panel.fine_align.poi_fgs()
        return [(e.polygons, fgs.get(e.key.key(), 200))
                for e in self._poi_entries if e.polygons]

    def _build_template(self, anchor, W, H, nm_per_px, cfg):
        """Composite the active POI layers into one template at ``anchor``."""
        return render_composite_template(
            self._poi_layers(), anchor, W, H, nm_per_px,
            cfg["bg_glv"], cfg["blur_sigma_px"])

    def _coarse_anchor(self, img):
        """Coarse FOV-centre GDS anchor for ``img`` (klarf→gds + δ),
        excluding any refined offset so the search starts from coarse."""
        gx, gy = gds_fov.klarf_to_gds(
            img.xrel, img.yrel, self._chip_corner_x, self._chip_corner_y)
        return (gx + self._origin_dx, gy + self._origin_dy)

    def _on_run_fine_align(self) -> None:
        """Refine the current image's alignment via composite POI-template
        matching (plan F3, single-image path)."""
        if cv2 is None:
            QMessageBox.warning(self, "Fine align",
                                "opencv (cv2) is required for fine alignment.")
            return
        if not self._poi_layers():
            self._status_doc.setText("fine align: select a POI layer first")
            return
        img = self._current_sem
        if img is None or not img.has_coords:
            self._status_doc.setText("fine align: select an image with coordinates")
            return
        sem = self._load_sem_gray(img)
        if sem is None:
            self._status_doc.setText("fine align: SEM image not readable")
            return
        nm_per_px = self._effective_nm_per_px()
        if nm_per_px <= 0:
            self._status_doc.setText("fine align: set FOV / nm-per-px first")
            return
        anchor = self._coarse_anchor(img)
        H, W = sem.shape[:2]
        cfg = self.sem_panel.fine_align.values()
        template = self._build_template(anchor, W, H, nm_per_px, cfg)
        radius_px = cfg["search_radius_nm"] / nm_per_px
        dx_nm, dy_nm, score, used_r = fine_align_one(sem, template, nm_per_px,
                                                     radius_px)
        self._refined[img.image_id] = (dx_nm, dy_nm, score)
        self._fa_meta[img.image_id] = (int(used_r), "ok")
        self.sem_panel.fine_align.set_result(score, dx_nm, dy_nm)
        self.sem_panel.set_score(img.image_id, score, cfg["score_threshold"])
        self.sem_viewer.reset_drag()
        self._jump_to_image(img)
        self._refresh_overview_defects()      # recolour the dot by new score
        self._status_doc.setText(
            f"fine align {img.image_id}: score {score:.3f}, "
            f"Δ ({dx_nm:,.0f}, {dy_nm:,.0f}) nm")

    @staticmethod
    def _load_sem_gray(img):
        """Load a SEM image as a grayscale uint8 ndarray, or None (M4b)."""
        if cv2 is None or img is None or not getattr(img, "exists", False):
            return None
        arr = cv2.imread(str(img.file_path), cv2.IMREAD_GRAYSCALE)
        return arr

    def _entry_spec(self, e):
        """One POI layer as a batch-walkable spec, or None (F3). Synthetic
        layers carry a recipe snapshot so nested refs can be resolved over
        the ROI during batch fine align."""
        if e.key.synthetic:
            if not e.expr_text or not e.expr_bindings:
                return None
            return ("expr", e.expr_text, dict(e.expr_bindings),
                    self._recipes_map())
        return ("raw", e.key.layer, e.key.datatype)

    def _poi_specs(self):
        """Active POIs as ``[(spec, fg_glv), ...]`` for batch (F3 Run all)."""
        fgs = self.sem_panel.fine_align.poi_fgs()
        out = []
        for e in self._poi_entries:
            spec = self._entry_spec(e)
            if spec is not None:
                out.append((spec, fgs.get(e.key.key(), 200)))
        return out

    def _maybe_prewarm_batch_pool(self) -> None:
        """F23 M2: once an OASIS reader + POIs + a pooled-size batch are all
        ready, spin up the persistent process pool *in the background* so the
        first Run all starts instantly instead of paying the spawn + per-worker
        reader build. No-op when a batch would run in-thread (<=2 images), when
        the pieces aren't ready yet, or when a warm is already underway; the
        pool itself self-guards on its key, so repeat calls are cheap."""
        if cv2 is None or self._rar is None or self._roi_root is None:
            return
        if len(self._sem_images) <= 2:
            return                       # small batch runs in-thread (no pool)
        if not self._poi_specs():
            return
        if getattr(self, "_prewarming", False):
            return
        rar = self._rar
        override = self.sem_panel.fine_align.values().get("max_workers", 0)
        workers = min(fine_align.batch_worker_count(override),
                      len(self._sem_images))
        if workers <= 1:
            return
        # Capture picklable reader params on the GUI thread; the warm itself
        # (process spawn + per-worker reader build) runs off-thread so the UI
        # never blocks. Worker count matches what _run_process_pool will use, so
        # the warmed pool's key is the one Run all reuses.
        args = (str(rar._path), rar._init_wanted, rar._dtype, rar._bbox_layer,
                rar.index_snapshot(), workers)
        self._prewarming = True

        def _warm():
            try:
                fine_align.batch_pool.ensure_warm(*args)
            except Exception:           # best-effort; Run all still works cold
                pass
            finally:
                self._prewarming = False

        threading.Thread(target=_warm, name="glas-batch-prewarm",
                         daemon=True).start()

    def closeEvent(self, ev) -> None:  # type: ignore[override]
        # F23 M2: release the persistent batch pool's idle worker processes
        # (each holds an mmap + name-table index) on quit. Best-effort —
        # ProcessPoolExecutor's atexit handler reaps any stragglers anyway.
        try:
            fine_align.batch_pool.shutdown()
        except Exception:
            pass
        super().closeEvent(ev)

    def _render_gds_preview(self, anchor, W, H, nm_per_px) -> np.ndarray:
        """RGB (H, W, 3) raster of the visible GDS layers over the FOV centred
        on ``anchor`` at SEM resolution, each in its layer colour (F3 M5)."""
        gx, gy = anchor
        half_w = W / 2.0 * nm_per_px
        half_h = H / 2.0 * nm_per_px
        bbox = (gx - half_w, gy - half_h, gx + half_w, gy + half_h)
        rgb = np.full((H, W, 3), 255, dtype=np.uint8)
        entries = self._doc.visible_entries() if self._doc else []
        for e in entries:
            if not e.polygons:
                continue
            mask = _fit_mask(rasterize_layer(e.polygons, bbox, nm_per_px), H, W)
            c = e.color
            rgb[mask > 0] = (c.red(), c.green(), c.blue())
        return rgb

    def _on_preview_template(self) -> None:
        """Pop the SEM / GDS / composite-template comparison (plan F3 M5)."""
        if not self._poi_layers():
            self._status_doc.setText("preview: select a POI layer first")
            return
        img = self._current_sem
        if img is None or not img.has_coords:
            self._status_doc.setText("preview: select an image with coordinates")
            return
        sem = self._load_sem_gray(img)
        if sem is None:
            self._status_doc.setText("preview: SEM image not readable")
            return
        nm_per_px = self._effective_nm_per_px()
        if nm_per_px <= 0:
            self._status_doc.setText("preview: set FOV / nm-per-px first")
            return
        anchor = self._coarse_anchor(img)
        H, W = sem.shape[:2]
        cfg = self.sem_panel.fine_align.values()
        template = self._build_template(anchor, W, H, nm_per_px, cfg)
        gds_rgb = self._render_gds_preview(anchor, W, H, nm_per_px)
        names = "; ".join(_entry_label(e) for e in self._poi_entries)
        ref = self._refined.get(img.image_id)
        score = f"  ·  score {ref[2]:.3f}" if ref else ""
        subtitle = f"{img.image_id}  ·  POI: {names}{score}"
        # Before/after residual overlays: outlines of the visible layers on the
        # SEM at the coarse anchor (before) and coarse+refined anchor (after).
        entries = [(e.polygons,
                    (e.color.red(), e.color.green(), e.color.blue()))
                   for e in self._doc.visible_entries()
                   if e.polygons] if self._doc else []
        before_rgb = after_rgb = None
        if entries:
            before_rgb = overlay_outlines_on_sem(sem, entries, anchor, nm_per_px)
            if ref is not None:
                after_anchor = (anchor[0] + ref[0], anchor[1] + ref[1])
                after_rgb = overlay_outlines_on_sem(
                    sem, entries, after_anchor, nm_per_px)
        TemplatePreviewDialog(self, sem, gds_rgb, template, subtitle,
                              before_rgb, after_rgb).exec()

    def _on_run_fine_align_all(self) -> None:
        """Batch fine-align every image with coordinates (plan F3 Run all)."""
        if cv2 is None:
            QMessageBox.warning(self, "Fine align",
                                "opencv (cv2) is required for fine alignment.")
            return
        if self._fa_thread is not None:
            return  # already running
        specs = self._poi_specs()
        if not specs:
            self._status_doc.setText("run all: select a POI layer first")
            return
        if self._rar is None or self._roi_root is None:
            self._status_doc.setText("run all: open an OASIS (ROI) first")
            return
        if self._fov_w <= 0 or self._fov_h <= 0:
            self._status_doc.setText("run all: set FOV width/height first")
            return
        jobs = [(im.image_id, self._coarse_gds(im),
                 str(im.file_path) if im.file_path else "", bool(im.exists))
                for im in self._sem_images]
        cfg = {
            "fov_w": self._fov_w, "fov_h": self._fov_h,
            "nm_auto": self._nm_auto, "nm_manual": self._nm_per_px_manual,
            **self.sem_panel.fine_align.values(),
        }
        # F7: run inside the batch workspace with inline progress, instead of a
        # modal dialog. Show the (initial) results table and the progress strip.
        self._fa_rerun_mode = False
        self._batch_refresh_timer.stop()     # clear any pending refresh (F8)
        self._enter_batch_workspace()
        self.batch_panel.set_rerun_defaults(self.sem_panel.fine_align.values())
        self._refresh_batch_panel()
        self._launch_fa(specs, jobs, cfg)

    def _on_export_all(self) -> None:
        """F24: one-click "fine-align the un-run images, then export the whole
        dataset". The images the operator already ran (in ``self._refined``) are
        reused — only the rest are computed — and when everything is already
        aligned we jump straight to the export dialog. Replaces the old
        "Run all images" button (fine-align was never the deliverable; the
        downstream export is)."""
        if not self._sem_images:
            self._status_doc.setText("export all: load SEM images first")
            return
        if self._fa_thread is not None:
            return  # a batch is already running
        todo = fine_align.images_needing_fine_align(
            self._sem_images, self._refined)
        if not todo:
            # Everything with coordinates is already aligned — export directly.
            self._on_export_alignment()
            return
        if cv2 is None:
            QMessageBox.warning(self, "Fine align",
                                "opencv (cv2) is required for fine alignment.")
            return
        specs = self._poi_specs()
        if not specs:
            self._status_doc.setText("export all: select a POI layer first")
            return
        if self._rar is None or self._roi_root is None:
            self._status_doc.setText("export all: open an OASIS (ROI) first")
            return
        if self._fov_w <= 0 or self._fov_h <= 0:
            self._status_doc.setText("export all: set FOV width/height first")
            return
        jobs = [(im.image_id, self._coarse_gds(im),
                 str(im.file_path) if im.file_path else "", bool(im.exists))
                for im in todo]
        cfg = {
            "fov_w": self._fov_w, "fov_h": self._fov_h,
            "nm_auto": self._nm_auto, "nm_manual": self._nm_per_px_manual,
            **self.sem_panel.fine_align.values(),
        }
        # Hand off to the export dialog once the batch finishes (see
        # _on_fa_finished); a cancel / failure clears the flag so we never
        # export a half-done set.
        self._export_after_fa = True
        self._fa_rerun_mode = False
        self._batch_refresh_timer.stop()
        self._enter_batch_workspace()
        self.batch_panel.set_rerun_defaults(self.sem_panel.fine_align.values())
        self._refresh_batch_panel()
        self._status_doc.setText(
            f"export all: fine-aligning {len(jobs)} un-run image(s)…")
        self._launch_fa(specs, jobs, cfg)

    def _on_rerun_requested(self, image_ids: list, overrides: dict) -> None:
        """F13: re-run a subset (low-score / selected) with overridden params.
        Same guards as 'Run all'; results only overwrite when strictly better
        (handled in ``_on_fa_result`` via ``_fa_rerun_mode``)."""
        if cv2 is None:
            QMessageBox.warning(self, "Fine align",
                                "opencv (cv2) is required for fine alignment.")
            return
        if self._fa_thread is not None:
            return  # already running
        specs = self._poi_specs()
        if not specs:
            self._status_doc.setText("re-run: select a POI layer first")
            return
        if self._rar is None or self._roi_root is None:
            self._status_doc.setText("re-run: open an OASIS (ROI) first")
            return
        if self._fov_w <= 0 or self._fov_h <= 0:
            self._status_doc.setText("re-run: set FOV width/height first")
            return
        subset = fine_align.rerun_image_subset(self._sem_images, image_ids)
        jobs = [(im.image_id, self._coarse_gds(im),
                 str(im.file_path) if im.file_path else "", bool(im.exists))
                for im in subset]
        if not jobs:
            self._status_doc.setText("re-run: no matching images")
            return
        cfg = {
            "fov_w": self._fov_w, "fov_h": self._fov_h,
            "nm_auto": self._nm_auto, "nm_manual": self._nm_per_px_manual,
            **self.sem_panel.fine_align.values(), **overrides,
        }
        self._fa_rerun_mode = True
        self._batch_refresh_timer.stop()
        self._status_doc.setText(f"re-run: {len(jobs)} image(s)…")
        self._launch_fa(specs, jobs, cfg)

    def _launch_fa(self, specs, jobs, cfg) -> None:
        """Start the batch fine-align worker thread (shared by Run all and the
        F13 subset re-run)."""
        self.batch_panel.start_progress()
        self._fa_thread = QThread(self)
        self._fa_worker = FineAlignAllWorker(
            self._rar, self._roi_root, specs, jobs, cfg)
        self._fa_worker.moveToThread(self._fa_thread)
        self._fa_thread.started.connect(self._fa_worker.run)
        self._fa_worker.progress.connect(self._on_fa_progress)
        self._fa_worker.result.connect(self._on_fa_result)
        self._fa_worker.finished.connect(self._on_fa_finished)
        self._fa_worker.failed.connect(self._on_fa_failed)
        self._fa_worker.cancelled.connect(self._on_fa_cancelled)
        for sig in (self._fa_worker.finished, self._fa_worker.failed,
                    self._fa_worker.cancelled):
            sig.connect(self._fa_thread.quit)
        self._fa_thread.finished.connect(self._cleanup_fa)
        self.sem_panel.fine_align.set_running(True)
        QApplication.processEvents()
        self._fa_thread.start()

    def _on_fa_cancel_clicked(self) -> None:
        """Cancel button in the batch panel: set the worker's threading.Event
        directly from the GUI thread so it takes effect immediately (F5 M5)."""
        if self._fa_worker is not None:
            self._fa_worker.cancel()

    def _refresh_batch_panel(self, rebuild_charts: bool = True) -> None:
        """Rebuild the batch panel rows from the current refined offsets +
        per-image meta (used during streaming and after finish). F8: during
        streaming ``rebuild_charts=False`` skips the (costly) histogram/scatter
        teardown+rebuild — those are only redrawn on the final refresh."""
        thr = self.sem_panel.fine_align.values()["score_threshold"]
        rows = fine_align_result_rows(
            self._sem_images, self._refined, self._fa_meta, thr)
        self.batch_panel.set_rows(rows, thr, rebuild_charts=rebuild_charts)

    def _on_fa_progress(self, done: int, total: int, image_id: str) -> None:
        self.batch_panel.set_progress(done, total, image_id)

    def _on_fa_result(self, image_id: str, dx: float, dy: float,
                      score: float, used_r: int, status: str) -> None:
        thr = self.sem_panel.fine_align.values()["score_threshold"]
        if self._fa_rerun_mode:
            # F13 Q1=C: a re-run only replaces a result when strictly better, so
            # a worse (or now-failing) re-run never clobbers a prior good one.
            if not fine_align.rerun_should_overwrite(
                    self._refined.get(image_id), score, status):
                return
            self._refined[image_id] = (dx, dy, score)
            self._fa_meta[image_id] = (int(used_r), status)
            self.sem_panel.set_score(image_id, score, thr)
        else:
            self._fa_meta[image_id] = (int(used_r), status)
            if status == "ok":
                self._refined[image_id] = (dx, dy, score)
                self.sem_panel.set_score(image_id, score, thr)
            else:
                # Drop any stale offset/badge so a now-failing image isn't still
                # rendered/exported with outdated alignment (PR#4 review).
                self._refined.pop(image_id, None)
                self.sem_panel.clear_score(image_id)
        # Stream the new row into the batch panel as it arrives (F7), but
        # coalesce the rebuilds (F8): kick the single-shot timer so a burst of
        # results refreshes the table at most ~3x/sec, not once per image.
        if not self._batch_refresh_timer.isActive():
            self._batch_refresh_timer.start()

    def _on_fa_finished(self, count: int) -> None:
        self._batch_refresh_timer.stop()      # final refresh supersedes it
        self.batch_panel.end_progress()
        self._status_doc.setText(f"fine align: processed {count} image(s)")
        self._refresh_overview_defects()      # recolour all dots by score
        if self._current_sem is not None:
            self.sem_viewer.reset_drag()
            self._jump_to_image(self._current_sem)
        self._refresh_batch_panel()           # final rows + charts + median
        if self._export_after_fa:
            # F24: the un-run images are now aligned — hand off to the export
            # dialog. Defer to the next event-loop tick so the batch QThread
            # finishes tearing down before the modal dialog opens.
            self._export_after_fa = False
            QTimer.singleShot(0, self._on_export_alignment)

    def _on_fa_failed(self, msg: str) -> None:
        self._export_after_fa = False         # F24: don't export a failed batch
        self._batch_refresh_timer.stop()
        self.batch_panel.end_progress()
        self._refresh_batch_panel()           # show whatever finished
        QMessageBox.critical(self, "Export all failed", msg)

    def _on_fa_cancelled(self) -> None:
        # Partial results are kept (not cleared), so the overview still reflects
        # whatever finished before the cancel (F5 M5).
        self._export_after_fa = False         # F24: cancel aborts the export too
        self._batch_refresh_timer.stop()
        self.batch_panel.end_progress()
        self._status_doc.setText("fine align: cancelled (partial results kept)")
        self._refresh_overview_defects()
        self._refresh_batch_panel()

    def _open_fa_results(self) -> None:
        """'Results…' action: enter the batch workspace and refresh the table
        (F7 — replaces the old modal results dialog)."""
        if not self._sem_images:
            self._status_doc.setText("results: load SEM images first")
            return
        self._enter_batch_workspace()
        self.batch_panel.set_rerun_defaults(self.sem_panel.fine_align.values())
        self._refresh_batch_panel()

    def _on_results_image_activated(self, image_id: str) -> None:
        for im in self._sem_images:
            if str(im.image_id) == str(image_id):
                self._on_sem_image_selected(im)
                break

    def _on_apply_median_residual(self, mx: float, my: float) -> None:
        """C2: shift the global origin δ by the median residual so the next
        Run all should converge. Same sign as the per-image refined offset
        (both are added to the coarse anchor). Existing _refined kept so the
        user can compare before/after."""
        self._origin_dx += mx
        self._origin_dy += my
        self.sem_viewer.reset_drag()
        self.sem_panel.alignment_delta.set_values(self._origin_dx, self._origin_dy)
        if self._current_sem is not None:
            self._jump_to_image(self._current_sem)
        self._fit_view_to_defects()
        self._status_doc.setText(
            f"origin δ += ({mx:,.0f}, {my:,.0f}) nm  ·  re-run Run all to check "
            f"residual convergence")

    def _cleanup_fa(self) -> None:
        self.batch_panel.end_progress()
        if self._fa_worker is not None:
            self._fa_worker.deleteLater()
            self._fa_worker = None
        if self._fa_thread is not None:
            self._fa_thread.deleteLater()
            self._fa_thread = None
        self.sem_panel.fine_align.set_running(False)

    def _coarse_gds(self, img):
        """Coarse FOV-centre GDS (nm) before per-image fine-align, or None."""
        if img is None or not img.has_coords:
            return None
        gx, gy = gds_fov.klarf_to_gds(
            img.xrel, img.yrel, self._chip_corner_x, self._chip_corner_y)
        return (gx + self._origin_dx, gy + self._origin_dy)

    # ── M5: per-image alignment export ───────────────────────────────────────
    def _on_export_alignment(self) -> None:
        if not self._sem_images:
            self._status_doc.setText("export: load SEM images first")
            return
        dlg = AlignmentExportDialog(self, self._sem_images, self._refined)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        fmt, ids, exp_raw, exp_overlay, exp_gray, exp_label, score_thr = \
            dlg.selected()
        if not ids:
            self._status_doc.setText("export: no images selected")
            return
        images = [i for i in self._sem_images if i.image_id in ids]
        gds_path = self._oas_path or (str(self._doc.path)
                                      if self._doc and self._doc.path else "")
        poi = "; ".join(_entry_label(e) for e in self._poi_entries)
        rows = alignment_rows(
            images, self._refined, coarse_of=self._coarse_gds,
            klarf_path=self._klarf_path, gds_path=gds_path,
            poi_layer=poi, nm_per_px=self._effective_nm_per_px())
        ext = "csv" if fmt == "csv" else "json"
        flt = "CSV (*.csv)" if fmt == "csv" else "JSON (*.json)"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export alignment", f"alignment.{ext}", flt)
        if not path:
            return
        try:
            if fmt == "csv":
                write_alignment_csv(path, rows)
            else:
                write_alignment_json(
                    path, rows, synthetic_layer_specs(self._doc))
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self._status_transient(
            f"exported {len(rows)} image(s) → {Path(path).name}")
        if exp_raw or exp_overlay or exp_gray or exp_label:
            self._export_overlay_images(images, exp_raw, exp_overlay,
                                        exp_gray, exp_label, score_thr)

    # ── F5 M6: SEM + aligned-overlay PNG export ──────────────────────────────
    def _poi_specs_colored(self) -> list:
        """``[(spec, (r, g, b), fg_glv), ...]`` for the active POI layers, for
        image export — the spec walks the ROI, the colour strokes the overlay
        outline, the ``fg_glv`` paints the simulated-GLV grayscale (F15). The
        POI's position here is its label id (1-based) in the ROI label map."""
        fgs = self.sem_panel.fine_align.poi_fgs()
        out = []
        for e in self._poi_entries:
            spec = self._entry_spec(e)
            if spec is not None:
                out.append((spec,
                            (e.color.red(), e.color.green(), e.color.blue()),
                            fgs.get(e.key.key(), 200)))
        return out

    def _export_label_map(self) -> list:
        """``[{"id", "layer", "fg_glv"}, ...]`` describing each POI layer's label
        id (F15) for the export manifest, in the same order / filter as
        :meth:`_poi_specs_colored` so ``label == id`` round-trips to a layer."""
        fgs = self.sem_panel.fine_align.poi_fgs()
        out = []
        idx = 0
        for e in self._poi_entries:
            if self._entry_spec(e) is None:
                continue
            idx += 1
            out.append({"id": idx, "layer": _entry_label(e),
                        "fg_glv": int(fgs.get(e.key.key(), 200))})
        return out

    def _export_overlay_images(self, images, exp_raw: bool,
                               exp_overlay: bool, exp_gray: bool = False,
                               exp_label: bool = False,
                               score_thr: float = 0.0) -> None:
        if cv2 is None:
            QMessageBox.warning(self, "Image export",
                                "opencv (cv2) is required for image export.")
            return
        if self._ov_thread is not None:
            return
        specs = self._poi_specs_colored()
        # Overlay / grayscale / label all need an OASIS ROI + a POI layer.
        if (exp_overlay or exp_gray or exp_label) and (
                not specs or self._rar is None or self._roi_root is None):
            QMessageBox.information(
                self, "Overlay export",
                "Overlay / grayscale / label PNGs need an OASIS (ROI) open and "
                "≥1 POI layer; exporting raw images / manifest only.")
            exp_overlay = False
            exp_gray = False
            exp_label = False
        out_dir = QFileDialog.getExistingDirectory(self, "Export images to…")
        if not out_dir:
            return
        jobs = [(im.image_id, self._coarse_gds(im),
                 self._refined.get(im.image_id),
                 str(im.file_path) if im.file_path else "", bool(im.exists))
                for im in images]
        fa = self.sem_panel.fine_align.values()
        cfg = {"fov_w": self._fov_w, "fov_h": self._fov_h,
               "nm_auto": self._nm_auto, "nm_manual": self._nm_per_px_manual,
               # F15: grayscale rendering greys / blur (same as the template).
               "bg_glv": fa["bg_glv"], "blur_sigma_px": fa["blur_sigma_px"],
               # F14: parallel export worker count (0 = auto).
               "max_workers": fa["max_workers"]}
        label_map = self._export_label_map() if exp_label else []
        self._ov_progress = LoadProgressDialog(self)
        self._ov_progress.set_text("Exporting images…")
        self._ov_thread = QThread(self)
        self._ov_worker = OverlayExportWorker(
            self._rar, self._roi_root, specs, jobs, cfg, out_dir,
            exp_raw, exp_overlay, exp_gray, exp_label, score_thr, label_map)
        self._ov_worker.moveToThread(self._ov_thread)
        self._ov_thread.started.connect(self._ov_worker.run)
        self._ov_worker.progress.connect(self._on_ov_progress)
        self._ov_worker.finished.connect(self._on_ov_finished)
        self._ov_worker.failed.connect(self._on_ov_failed)
        self._ov_worker.cancelled.connect(self._on_ov_cancelled)
        self._ov_progress.cancel_requested.connect(
            self._ov_worker.cancel, Qt.ConnectionType.DirectConnection)
        for sig in (self._ov_worker.finished, self._ov_worker.failed,
                    self._ov_worker.cancelled):
            sig.connect(self._ov_thread.quit)
        self._ov_thread.finished.connect(self._cleanup_ov)
        self._ov_progress.show()
        QApplication.processEvents()
        self._ov_thread.start()

    def _on_ov_progress(self, done: int, total: int, image_id: str) -> None:
        if self._ov_progress is not None:
            self._ov_progress.set_text(f"Exporting images…\ncurrent: {image_id}")
            self._ov_progress.set_progress(done, total)

    def _on_ov_finished(self, count: int, manifest: str) -> None:
        self._status_doc.setText(
            f"image export: {count} image(s) + manifest → {Path(manifest).name}")

    def _on_ov_failed(self, msg: str) -> None:
        QMessageBox.critical(self, "Image export failed", msg)

    def _on_ov_cancelled(self) -> None:
        self._status_doc.setText("image export: cancelled")

    def _cleanup_ov(self) -> None:
        if self._ov_progress is not None:
            self._ov_progress.shutdown()
            self._ov_progress.close()
            self._ov_progress.deleteLater()
            self._ov_progress = None
        if self._ov_worker is not None:
            self._ov_worker.deleteLater()
            self._ov_worker = None
        if self._ov_thread is not None:
            self._ov_thread.deleteLater()
            self._ov_thread = None

    def _on_goto_gds(self) -> None:
        """Goto a GDS coordinate (µm) typed in the toolbar — the same thing
        klayout's Ctrl+G does, for direct same-coordinate comparison. Centres
        + zooms the canvas there and loads a ~50µm ROI of geometry."""
        txt = self._goto_edit.text().strip().replace(",", " ")
        parts = txt.split()
        if len(parts) != 2:
            self._status_doc.setText("Goto: enter 'x, y' in µm")
            return
        try:
            x_um, y_um = float(parts[0]), float(parts[1])
        except ValueError:
            self._status_doc.setText("Goto: x, y must be numbers (µm)")
            return
        cx, cy = x_um * 1e3, y_um * 1e3        # µm -> nm
        half = 25_000.0                         # ±25 µm view + ROI window
        self.canvas.set_view_to_bbox(cx - half, cy - half, cx + half, cy + half)
        self.canvas.set_fov_marker(cx, cy, self._fov_w or 2000.0,
                                   self._fov_h or 2000.0)
        self._status_doc.setText(
            f"goto GDS ({x_um:,.3f}, {y_um:,.3f}) µm — compare to klayout "
            f"Ctrl+G at the same coord")
        if self._rar is not None and self._roi_thread is None:
            self._load_roi_around(cx, cy, half_w=half, half_h=half)

    def _on_load_roi_clicked(self) -> None:
        """Explicit 'Load GDS ROI here' button (M3.5d) — only loads on
        demand, never auto-triggered by FOV / fine-tune edits."""
        if self._rar is None:
            QMessageBox.information(
                self, "ROI mode",
                "Open an OASIS in ROI mode first (toolbar "
                "'Open OASIS (ROI)…').")
            return
        pos = self._current_image_gds()
        if pos is None:
            self._status_cursor.setText("select an image with coordinates first")
            return
        self._load_roi_around(*pos)

    def _load_roi_around(self, cx: float, cy: float,
                         *, half_w: float = None, half_h: float = None) -> None:
        """Random-access load of the geometry around GDS (cx, cy) for the
        chosen ROI layer(s). Default half-window is the FOV; ``half_w/half_h``
        override it (e.g. Goto GDS uses a larger window for comparison).

        Runs in a background thread: with S_BOUNDING_BOX the per-cell bbox is
        read from the name table so the prune is decode-free (F16); only files
        lacking it fall back to decoding every reachable cell once to learn its
        size, which can take a while on a big chip — so the UI stays responsive
        with a cancellable progress dialog. Subsequent loads reuse the cache."""
        hw = half_w if half_w is not None else self._fov_w
        hh = half_h if half_h is not None else self._fov_h
        if hw <= 0 or hh <= 0:
            self._status_cursor.setText("set FOV width/height for ROI load")
            return
        if self._roi_thread is not None:      # a load is already running
            return
        roi = (cx - hw, cy - hh, cx + hw, cy + hh)
        self._rar.errors.clear()
        self._roi_center = (cx, cy)
        # F5 M4: remember the current fine-align setup before this defect's ROI
        # replaces the document, so it can be re-applied by layer key on load.
        self._capture_fa_setup()

        self._roi_progress = LoadProgressDialog(self)
        self._roi_phase = (0, len(self._roi_layers), None)
        self._roi_progress.set_text("Loading GDS ROI…\nStarting…")
        self._roi_thread = QThread(self)
        self._roi_worker = RoiWalkWorker(
            self._rar, self._roi_root, self._roi_layers, roi)
        self._roi_worker.moveToThread(self._roi_thread)
        self._roi_thread.started.connect(self._roi_worker.run)
        self._roi_worker.finished.connect(self._on_roi_finished)
        self._roi_worker.failed.connect(self._on_roi_failed)
        self._roi_worker.cancelled.connect(self._on_roi_cancelled)
        self._roi_worker.progress.connect(self._on_roi_phase)
        self._roi_progress.cancel_requested.connect(self._roi_worker.cancel)
        for sig in (self._roi_worker.finished, self._roi_worker.failed,
                    self._roi_worker.cancelled):
            sig.connect(self._roi_thread.quit)
        self._roi_thread.finished.connect(self._cleanup_roi)

        # Live progress: poll the reader's decoded-cell counter.
        self._roi_progress_timer = QTimer(self)
        self._roi_progress_timer.setInterval(200)
        self._roi_progress_timer.timeout.connect(self._tick_roi_progress)
        self._roi_progress_timer.start()

        self._roi_progress.show()
        QApplication.processEvents()
        self._roi_thread.start()

    def _on_roi_phase(self, idx: int, total: int, key) -> None:
        self._roi_phase = (idx, total, key)

    def _tick_roi_progress(self) -> None:
        if self._roi_progress is None or self._rar is None:
            return
        idx, total, key = getattr(self, "_roi_phase", (0, 1, None))
        loaded = self._rar._n_loaded
        cached = getattr(self._rar, "_n_cache_hits", 0)
        layer_txt = (f"layer {idx + 1}/{total} ({key[0]}/{key[1]})"
                     if key is not None else "starting…")
        lines = [f"Loading GDS ROI… ({layer_txt})",
                 f"{loaded:,} cell(s) loaded" + (f", {cached:,} from cache"
                                                 if cached else "")]
        # First load of the big flat merge cell is the slow part; once cached it
        # (and every nearby ROI) is fast. Make that explicit so the wait reads
        # as expected rather than stuck.
        if cached == 0 and loaded < 3:
            lines.append("Decoding a large cell (first time may take minutes; "
                         "later ROIs reuse the cache).")
        lines.append("Cancellable.")
        self._roi_progress.set_text("\n".join(lines))

    def _on_roi_finished(self, doc, per_layer) -> None:
        self._doc = doc
        self.layer_panel.set_document(doc)
        self.canvas.set_document(doc)
        # F4: re-evaluate the Boolean recipes against this defect's ROI so the
        # synthetic layers follow the FOV (instead of being lost on reload).
        expr_errs = self._recompute_recipes(doc.bbox_nm)
        # F5 M4: restore the remembered fine-align setup (visibility / colour /
        # POI / Foreground GL) by layer key, so the user need only press Run
        # fine align once on the new defect. Runs after recompute so synthetic
        # layers exist before POI restore.
        self._apply_fa_setup()
        # Keep the user's current (overview) view; just refresh the FOV box.
        # Don't re-centre/zoom onto the ROI — that's what made the marker
        # look stuck in the middle of the screen. Zoom in manually to
        # inspect the loaded geometry.
        if self._roi_center is not None:
            self.canvas.set_fov_marker(self._roi_center[0], self._roi_center[1],
                                       self._fov_w, self._fov_h)
        # Refresh the SEM overlay with the newly-loaded geometry (M4a).
        self._update_overlay()
        total_rects = sum(s.rects_emitted for _, s in per_layer)
        total_polys = sum(s.polys_emitted for _, s in per_layer)
        pruned = sum(s.instances_pruned for _, s in per_layer)
        errs = len(self._rar.errors)
        msg = (f"ROI {len(self._roi_layers)} layer(s): {total_rects} rects, "
               f"{total_polys} polys ({pruned:,} instances pruned)")
        # Always-on perf telemetry (F16): the time + how many cells were fully
        # decoded tells us instantly whether the prune is biting. A tight FOV
        # that still decodes thousands of cells => no decode-free bbox (no
        # S_BOUNDING_BOX, no per-cell CE boundary) — the slow first-load case.
        elapsed = sum(s.elapsed_s for _, s in per_layer)
        decoded = sum(s.cells_decoded for _, s in per_layer)
        cached = sum(getattr(s, "cells_cached", 0) for _, s in per_layer)
        t_decode = sum(getattr(s, "t_decode", 0.0) for _, s in per_layer)
        sbbox_on = any(s.sbbox_prune for _, s in per_layer)
        print(f"{devlog.tag('roi')} ── loaded in {elapsed:.1f}s · "
              f"{len(per_layer)} layer(s) · "
              f"{total_rects:,} rects {total_polys:,} polys · "
              f"decode {t_decode:.1f}s ({cached} from cache, {decoded} decoded) · "
              f"prune {'ON' if sbbox_on else 'off'}"
              + (f" · ⚠ {errs} decode error(s)" if errs else ""), flush=True)
        if errs:
            msg += f" · ⚠ {errs} cell decode error(s) — run with --debug"
        if expr_errs:
            msg += f" · ⚠ expr: {'; '.join(expr_errs)}"
        self._status_doc.setText(msg)
        if total_rects == 0 and total_polys == 0:
            self._status_cursor.setText(
                "ROI empty — check root cell / chip corner / layer / FOV size")
        # Debug (--debug): where did the loaded geometry land vs the requested
        # centre, and the largest rect (cross-check against klayout goto).
        if oasis_random.DEBUG and doc is not None and doc.entries:
            bb = doc.bbox_nm
            rc = self._roi_center or (0.0, 0.0)
            print(f"{devlog.tag('roi')} geometry bbox = "
                  f"({bb[0]/1e3:,.1f}, {bb[1]/1e3:,.1f}) .. "
                  f"({bb[2]/1e3:,.1f}, {bb[3]/1e3:,.1f}) µm  "
                  f"| requested centre = ({rc[0]/1e3:,.1f}, {rc[1]/1e3:,.1f}) µm  "
                  f"| centre in bbox? "
                  f"{bb[0] <= rc[0] <= bb[2] and bb[1] <= rc[1] <= bb[3]}",
                  flush=True)
            # Dump the LARGEST rectangle (easiest to find in klayout, no
            # sliver false-negatives): goto its centre and confirm a rect of
            # that layer/size is really there.
            best = None  # (area, layer, datatype, x0, y0, x1, y1)
            for e in doc.entries:
                for poly in e.polygons:
                    xs = poly[:, 0]; ys = poly[:, 1]
                    x0, y0, x1, y1 = (float(xs.min()), float(ys.min()),
                                      float(xs.max()), float(ys.max()))
                    area = (x1 - x0) * (y1 - y0)
                    if best is None or area > best[0]:
                        best = (area, e.key.layer, e.key.datatype, x0, y0, x1, y1)
            if best is not None:
                _, ly, dt, x0, y0, x1, y1 = best
                print(f"{devlog.tag('roi')} LARGEST {ly}/{dt} rect: "
                      f"({x0/1e3:,.3f}, {y0/1e3:,.3f}) .. "
                      f"({x1/1e3:,.3f}, {y1/1e3:,.3f}) µm "
                      f"(W×H {(x1-x0)/1e3:,.3f}×{(y1-y0)/1e3:,.3f} µm) "
                      f"-> goto centre ({(x0+x1)/2e3:,.3f}, {(y0+y1)/2e3:,.3f}) µm",
                      flush=True)

    def _on_roi_failed(self, msg: str) -> None:
        self._show_load_error("ROI load failed", msg)

    def _show_load_error(self, title: str, detail: str) -> None:
        """F10: in developer mode, show a copyable report (the error plus an
        automatic structural scan of the offending file) and drop a
        ``.debug.txt`` sidecar; otherwise a plain critical box."""
        if not self._dev_mode:
            QMessageBox.critical(self, title, detail)
            return
        path = getattr(self, "_roi_load_path", None)
        parts = [f"=== {title} ===", "", detail]
        if path:
            try:
                parts += ["", "--- automatic file diagnosis ---",
                          oasis_debug.report_file(path)]
            except Exception as exc:  # noqa: BLE001
                parts += ["", f"(diagnosis scan failed: {exc})"]
        text = "\n".join(parts)
        sidecar = (path + ".debug.txt") if path else None
        if sidecar:
            try:
                Path(sidecar).write_text(text, encoding="utf-8")
            except OSError:
                sidecar = None
        DebugReportDialog(self, title, text, saved_path=sidecar).exec()

    def _on_roi_cancelled(self) -> None:
        self._status_doc.setText("ROI load cancelled")

    def _cleanup_roi(self) -> None:
        if self._roi_progress_timer is not None:
            self._roi_progress_timer.stop()
            self._roi_progress_timer.deleteLater()
            self._roi_progress_timer = None
        if self._roi_progress is not None:
            self._roi_progress.shutdown()
            self._roi_progress.close()
            self._roi_progress.deleteLater()
            self._roi_progress = None
        if self._roi_worker is not None:
            self._roi_worker.deleteLater()
            self._roi_worker = None
        if self._roi_thread is not None:
            self._roi_thread.deleteLater()
            self._roi_thread = None

    def _on_open_roi(self) -> None:
        """F20: three-page wizard replaces the file-dialog + LayerFilter +
        LayerPick + QInputDialog-root-cell cascade."""
        wiz = OpenOasisWizard(self)
        # U1: hide the redundant guidance bar while the wizard is the user's
        # focus; restore on close (regardless of accept / reject).
        self._guidance.setVisible(False)
        try:
            result = wiz.exec()
        finally:
            self._update_guidance()
        if result != QDialog.DialogCode.Accepted:
            return
        path = wiz.file_path()
        layer_keys = wiz.layer_keys()
        root = wiz.root_cell()
        if not (path and layer_keys and root):
            return
        self._roi_load_path = path   # F10: remembered for debug-mode diagnostics
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            import time as _t
            _t0 = _t.perf_counter()
            rar = oasis_random.RandomAccessReader(
                path, wanted_layers=set(layer_keys),
                bbox_layer=oasis_random.DEFAULT_BBOX_LAYER)
            n_sbbox = len(rar._sbbox_by_refnum) + len(rar._sbbox_by_name)
            print(f"{devlog.tag('roi')} reader built in "
                  f"{_t.perf_counter() - _t0:.1f}s · "
                  f"{len(rar._by_refnum):,} cells indexed · S_BOUNDING_BOX on "
                  f"{n_sbbox:,} cells "
                  f"({'decode-free prune' if n_sbbox else 'NONE -> bbox-by-decode'})",
                  flush=True)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            self._show_load_error("ROI open failed", str(exc))
            return
        QApplication.restoreOverrideCursor()
        if not rar.has_offsets():
            QMessageBox.warning(
                self, "ROI mode unavailable",
                "This OASIS has no S_CELL_OFFSET index, so random-access "
                "ROI load isn't possible. Re-export the layout with "
                "per-cell offsets, or load a smaller file.")
            return
        self._rar = rar
        self._oas_path = path
        self._roi_root = root
        self._roi_layers = layer_keys
        # F23 M2: a new layout invalidates any pool warmed for the old file;
        # release those worker processes now (the next prewarm rebuilds for the
        # new reader). shutdown() is a no-op when no pool is live.
        fine_align.batch_pool.shutdown()
        # New layout → drop any recipes from a previously-open file.
        self._recipes = []
        lyr_txt = ", ".join(f"L{l}/D{d}" for l, d in layer_keys)
        self._status_state(
            f"ROI mode: {Path(path).name} · root '{root}' · {lyr_txt}"
            f" · {len(rar._by_refnum):,} cells indexed · click a SEM image")
        # U9: surface the active OASIS in the title bar so a user with
        # several windows open can tell them apart at a glance.
        self.setWindowTitle(f"GLAS — {Path(path).name}")
        self._fit_view_to_defects()
        if self._current_sem is not None:
            self._jump_to_image(self._current_sem)
        self._update_guidance()

    # ── M2.6: expression layers ─────────────────────────────────────────────
    @staticmethod
    def _bboxes_from_polys(polys: list) -> np.ndarray:
        """Per-polygon AABBs as ``(N, 4)`` float32, parallel to ``polys``."""
        if not polys:
            return np.empty((0, 4), dtype=np.float32)
        out = np.empty((len(polys), 4), dtype=np.float32)
        for i, p in enumerate(polys):
            out[i] = (p[:, 0].min(), p[:, 1].min(),
                      p[:, 0].max(), p[:, 1].max())
        return out

    def _eval_expression(self, expr: str, bindings: dict,
                         fov: tuple[float, float, float, float]) -> list:
        """Evaluate ``expr`` over the polygons inside ``fov`` and return a
        list of result polygons (each ``(n, 2)`` ndarray). Bindings may be
        raw layers or references to other synthetic recipes (nested). Raises
        on parse / evaluation error or missing shapely (caller shows it)."""
        x0, y0, x1, y1 = fov
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        w, h = (x1 - x0), (y1 - y0)

        def raw_provider(layer: int, datatype: int):
            entry = (self._doc.find(LayerKey(layer=layer, datatype=datatype))
                     if self._doc is not None else None)
            if entry is None or entry.bboxes is None or entry.bboxes.shape[0] == 0:
                return gds_boolean.polys_to_geometry([])
            idx = gds_fov.fov_overlap_indices(cx, cy, w, h, entry.bboxes)
            sel = [entry.polygons[int(i)] for i in idx]
            return gds_boolean.polys_to_geometry(sel)

        geom = gds_boolean.resolve_expression(
            expr, bindings, raw_provider=raw_provider,
            recipe_provider=self._recipe_def,
            fov_bbox=gds_boolean.fov_box(cx, cy, w, h))
        return gds_boolean.geometry_to_polygons(geom)

    def _next_expr_color(self) -> QColor:
        c = _LAYER_PALETTE[self._expr_color_idx % len(_LAYER_PALETTE)]
        self._expr_color_idx += 1
        return QColor(c)

    # ── F4: synthetic-layer recipe store ────────────────────────────────────
    def _recipe(self, name: str) -> Optional[dict]:
        for r in self._recipes:
            if r["name"] == name:
                return r
        return None

    def _recipe_def(self, name: str):
        """``(expr, bindings)`` for a recipe, or None — the recipe_provider
        passed to :func:`gds_boolean.resolve_expression` for nested refs."""
        r = self._recipe(name)
        return (r["expr"], r["bindings"]) if r is not None else None

    def _recipes_map(self) -> dict:
        """Snapshot ``{name: (expr, bindings)}`` for the batch (F3) path."""
        return {r["name"]: (r["expr"], dict(r["bindings"]))
                for r in self._recipes}

    def _register_recipe(self, name: str, expr: str, bindings: dict,
                         color: Optional[QColor] = None,
                         old_name: Optional[str] = None) -> None:
        """Insert or update a recipe. When ``old_name`` is given (edit), the
        existing recipe is replaced in place (keeping its slot / colour)."""
        bindings = {k: gds_boolean.normalize_binding(v)
                    for k, v in bindings.items()}
        target = old_name or name
        existing = self._recipe(target)
        if color is None:
            color = (QColor(existing["color"]) if existing is not None
                     else self._next_expr_color())
        rec = {"name": name, "expr": expr, "bindings": bindings,
               "color": QColor(color).name()}
        if existing is not None:
            self._recipes[self._recipes.index(existing)] = rec
        else:
            self._recipes.append(rec)

    def _recipe_fov(self) -> tuple[float, float, float, float]:
        """FOV to evaluate recipes over: the loaded ROI extent when present,
        else the whole document extent."""
        if self._doc is not None and self._doc.entries:
            return self._doc.bbox_nm
        return (0.0, 0.0, 0.0, 0.0)

    def _recompute_recipes(self,
                           fov: Optional[tuple[float, float, float, float]]
                           = None) -> list[str]:
        """Rebuild every synthetic LayerEntry from the recipes, evaluated
        against ``fov`` (default: the current ROI / document extent). This is
        what makes Boolean layers follow the defect — it runs after each ROI /
        cache load. Returns a list of per-recipe error strings (empty = ok)."""
        if self._doc is None:
            return []
        self._doc.entries = [e for e in self._doc.entries
                             if not e.key.synthetic]
        if fov is None:
            fov = self._recipe_fov()
        errors: list[str] = []
        for r in self._recipes:
            try:
                polys = self._eval_expression(r["expr"], r["bindings"], fov)
            except Exception as exc:
                errors.append(f"{r['name']}: {exc}")
                polys = []
            key = LayerKey(layer=-1, datatype=0, name=r["name"], synthetic=True)
            self._doc.entries.append(LayerEntry(
                key=key, polygons=polys, visible=True, color=QColor(r["color"]),
                bboxes=self._bboxes_from_polys(polys),
                expr_text=r["expr"], expr_bindings=dict(r["bindings"])))
        self.layer_panel.set_document(self._doc)
        self.canvas.refresh()
        return errors

    def _preview_expression(self, name: str, expr: str,
                            bindings: dict) -> tuple:
        """Preview callback for ExpressionLayerDialog. Evaluates over the
        canvas's current view and returns ``(ok, msg, data)`` for the dialog's
        embedded preview — WITHOUT mutating the main document/canvas, so the
        user reviews the result inside the dialog and only commits on Save.

        ``data`` = ``{"result": [polys], "context": [(polys, QColor)], "fov":
        bbox}`` (``result`` = the synthetic geometry; ``context`` = the bound
        raw layers, drawn faintly for reference)."""
        if self._doc is None:
            return (False, "Load a layout first.", None)
        fov = self.canvas.viewport_bbox_nm()
        try:
            result = self._eval_expression(expr, bindings, fov)
        except Exception as exc:
            return (False, str(exc), None)
        x0, y0, x1, y1 = fov
        cx, cy, w, h = (x0 + x1) / 2.0, (y0 + y1) / 2.0, (x1 - x0), (y1 - y0)
        context: list = []
        for val in bindings.values():
            v = gds_boolean.normalize_binding(val)
            if v[0] != "raw":
                continue
            entry = self._doc.find(LayerKey(layer=v[1], datatype=v[2]))
            if entry is None or entry.bboxes is None or entry.bboxes.shape[0] == 0:
                continue
            idx = gds_fov.fov_overlap_indices(cx, cy, w, h, entry.bboxes)
            context.append(([entry.polygons[int(i)] for i in idx], entry.color))
        data = {"result": result, "context": context, "fov": fov}
        return (True, f"{len(result)} polygons in current view", data)

    def _open_expression_dialog(self, recipe: Optional[dict] = None) -> None:
        """Open the compose dialog to create a new recipe (``recipe`` None) or
        edit an existing one (prefilled)."""
        if self._doc is None or not self._doc.entries:
            QMessageBox.information(self, "No layout",
                                    "Load a GDS / OASIS file first.")
            return
        keys = self.layer_panel.raw_layer_keys()
        if not keys:
            QMessageBox.information(self, "No raw layers",
                                    "There are no raw layers to compose.")
            return
        # Synthetic layers available to reference (exclude the one being edited
        # so a recipe can't bind to itself).
        refs = [r["name"] for r in self._recipes
                if recipe is None or r["name"] != recipe["name"]]
        dlg = ExpressionLayerDialog(
            self, keys, ref_names=refs, preview_cb=self._preview_expression,
            recipe=recipe)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        old_name = recipe["name"] if recipe is not None else None
        self._register_recipe(dlg.name(), dlg.expression(), dlg.bindings(),
                              old_name=old_name)
        errors = self._recompute_recipes()
        n = len(self._recipe(dlg.name())["bindings"])
        if errors:
            self._status_doc.setText("expression error · " + "; ".join(errors))
        else:
            self._status_doc.setText(
                f"expression layer '{dlg.name()}' · {n} binding(s)")

    def _on_add_expression(self) -> None:
        # Deferred so the triggering widget's signal/event handler fully
        # unwinds before the modal dialog (and the set_document that follows)
        # runs — opening exec() inside a row handler that then gets deleted is
        # a use-after-free that crashes PyQt.
        QTimer.singleShot(0, lambda: self._open_expression_dialog(None))

    def _on_edit_recipe(self, name: str) -> None:
        QTimer.singleShot(0, lambda: self._edit_recipe_deferred(name))

    def _edit_recipe_deferred(self, name: str) -> None:
        rec = self._recipe(name)
        if rec is not None:
            self._open_expression_dialog(rec)

    def _on_delete_recipe(self, name: str) -> None:
        # Defer for the same reason as edit: the ✕ click handler lives on a
        # row that _recompute_recipes() → set_document() deletes.
        QTimer.singleShot(0, lambda: self._delete_recipe_deferred(name))

    def _delete_recipe_deferred(self, name: str) -> None:
        rec = self._recipe(name)
        if rec is None:
            return
        # Block deleting a recipe still referenced by another (would orphan it).
        dependents = [r["name"] for r in self._recipes
                      if r is not rec and any(
                          gds_boolean.normalize_binding(v)[:2] == ("ref", name)
                          for v in r["bindings"].values())]
        if dependents:
            QMessageBox.warning(
                self, "Cannot delete",
                f"'{name}' is referenced by: {', '.join(dependents)}. "
                f"Delete or edit those first.")
            return
        self._recipes.remove(rec)
        self._recompute_recipes()
        self._status_transient(f"deleted expression layer '{name}'")

    # ── M2.1: layer cache export / import ───────────────────────────────────
    def _on_export_cache(self) -> None:
        if self._doc is None or not self._doc.entries:
            QMessageBox.information(self, "Nothing to export",
                                    "Load a layout first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export layer cache", "", "Layer cache (*.npz)")
        if not path:
            return
        if not path.lower().endswith(".npz"):
            path += ".npz"
        raw = [e for e in self._doc.entries if not e.key.synthetic]
        layers = []
        for e in raw:
            bbs = (e.bboxes if e.bboxes is not None
                   else self._bboxes_from_polys(e.polygons))
            layers.append((e.key.layer, e.key.datatype, list(e.polygons), bbs))
        try:
            v = self.sem_panel.coord_setup.values()
            meta = gds_layer_cache.make_meta(
                self._doc.path or path,
                chip_corner_x=v["chip_corner_x"],
                chip_corner_y=v["chip_corner_y"],
                chip_x_um=v["chip_x_um"], chip_y_um=v["chip_y_um"],
                chip_w_um=v["chip_w_um"], chip_h_um=v["chip_h_um"],
                gds_off_x_um=v["gds_off_x_um"], gds_off_y_um=v["gds_off_y_um"],
                fov_w=v["fov_w"], fov_h=v["fov_h"],
                origin_dx=self._origin_dx, origin_dy=self._origin_dy,
                nm_per_px=self._effective_nm_per_px(),
                top_cell_name=self._doc.top_cell_name,
                part_id=v.get("part_id"), chip_id=v.get("chip_id"))
            gds_layer_cache.cache_save(path, layers, meta)
            self._save_expr_sidecar(path)
        except Exception as exc:
            QMessageBox.critical(self, "Cache export failed", str(exc))
            return
        self._status_transient(f"cache exported · {Path(path).name}")

    def _on_export_oasis(self) -> None:
        """F9 M3: export selected raw / Boolean layers to OASIS, optionally
        cropped to a GDS-coordinate region. Developer-mode only."""
        if self._doc is None or not self._doc.entries:
            QMessageBox.information(self, "Nothing to export",
                                    "Load a layout first.")
            return
        whole_ok = self._rar is not None and self._roi_root is not None
        dlg = OasisExportDialog(self, self._doc.entries, self._doc.bbox_nm,
                                whole_chip_available=whole_ok)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if not dlg.selected_specs():
            QMessageBox.information(self, "Nothing selected",
                                    "Select at least one layer to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export OASIS", "", "OASIS (*.oas)")
        if not path:
            return
        if not path.lower().endswith(".oas"):
            path += ".oas"
        if dlg.scope() == "whole":
            self._start_whole_chip_export(path, dlg.selected_specs(),
                                          dlg.debug_enabled())
            return
        layers = dlg.selected_layers()
        try:
            n, report = layout_export.export_layers(
                path, layers, crop_bbox=dlg.crop_bbox(),
                unit=1000.0, cellname=self._doc.top_cell_name or "TOP",
                debug=dlg.debug_enabled())
        except Exception as exc:
            QMessageBox.critical(self, "OASIS export failed", str(exc))
            return
        if n == 0:
            QMessageBox.information(
                self, "Nothing exported",
                "No geometry fell inside the crop region.")
            return
        self._status_transient(
            f"OASIS exported · {Path(path).name} ({n} layer{'s' if n != 1 else ''})")
        if report is not None:
            sidecar = path + ".debug.txt"
            try:
                Path(sidecar).write_text(report, encoding="utf-8")
            except OSError:
                sidecar = None
            DebugReportDialog(self, "OASIS export — debug report",
                              report, saved_path=sidecar).exec()

    @staticmethod
    def _whole_chip_halo_nm(exprs: list) -> float:
        """Tile halo (nm) = the largest morph distance in any recipe (so a
        grown shape from a neighbouring tile is included) plus a margin."""
        biggest = 0
        for e in exprs:
            for m in re.finditer(r"[<>]\s*[WHwh]\s*:\s*(\d+)", e or ""):
                biggest = max(biggest, int(m.group(1)))
        return biggest + 1000.0      # +1 µm margin

    def _start_whole_chip_export(self, path, specs, debug: bool) -> None:
        """F11 M2/M3: launch the tiled whole-chip export worker."""
        if self._roi_thread is not None or getattr(self, "_wc_thread", None):
            QMessageBox.information(self, "Busy", "A load/export is running.")
            return
        chip = self._rar.reachable_bbox_nm(self._roi_root)
        if chip is None:
            QMessageBox.critical(self, "Whole-chip export failed",
                                 "Could not determine the chip extent.")
            return
        raw_specs = [(ol, od, e.key.layer, e.key.datatype)
                     for e, ol, od in specs if not e.key.synthetic]
        recipe_specs = [(ol, od, e.expr_text, e.expr_bindings)
                        for e, ol, od in specs
                        if e.key.synthetic and e.expr_text]
        halo = self._whole_chip_halo_nm([r[2] for r in recipe_specs])

        self._wc_path = path
        self._wc_debug = debug
        self._wc_progress = LoadProgressDialog(self)
        self._wc_progress.set_text(
            "Exporting whole chip…\nWalking + recomputing per tile "
            "(streamed to disk; bounded memory). Cancellable.")
        self._wc_thread = QThread(self)
        self._wc_worker = WholeChipExportWorker(
            rar=self._rar, root=self._roi_root, path=path, unit=1000.0,
            cellname=self._doc.top_cell_name or "TOP", chip_bbox=chip,
            raw_specs=raw_specs, recipe_specs=recipe_specs,
            recipe_def=self._recipe_def, target_nm=250_000.0, halo_nm=halo)
        self._wc_worker.moveToThread(self._wc_thread)
        self._wc_thread.started.connect(self._wc_worker.run)
        self._wc_worker.progress.connect(self._wc_progress.set_progress)
        self._wc_worker.finished.connect(self._on_wc_finished)
        self._wc_worker.failed.connect(self._on_wc_failed)
        self._wc_worker.cancelled.connect(self._on_wc_cancelled)
        self._wc_progress.cancel_requested.connect(self._wc_worker.cancel)
        for sig in (self._wc_worker.finished, self._wc_worker.failed,
                    self._wc_worker.cancelled):
            sig.connect(self._wc_thread.quit)
        self._wc_thread.finished.connect(self._cleanup_wc)
        self._wc_progress.show()
        QApplication.processEvents()
        self._wc_thread.start()

    def _on_wc_finished(self, n_tiles: int) -> None:
        if self._wc_progress is not None:
            self._wc_progress.close()
        path = self._wc_path
        self._status_doc.setText(
            f"OASIS whole-chip exported · {Path(path).name} ({n_tiles} tiles)")
        if self._wc_debug:
            report = oasis_debug.report_file(path)
            sidecar = path + ".debug.txt"
            try:
                Path(sidecar).write_text(report, encoding="utf-8")
            except OSError:
                sidecar = None
            DebugReportDialog(self, "Whole-chip export — debug report",
                              report, saved_path=sidecar).exec()

    def _on_wc_failed(self, msg: str) -> None:
        if self._wc_progress is not None:
            self._wc_progress.close()
        if self._dev_mode:
            DebugReportDialog(self, "Whole-chip export failed", msg).exec()
        else:
            QMessageBox.critical(self, "Whole-chip export failed", msg)

    def _on_wc_cancelled(self) -> None:
        if self._wc_progress is not None:
            self._wc_progress.close()
        self._status_doc.setText("whole-chip export cancelled")

    def _cleanup_wc(self) -> None:
        for attr in ("_wc_progress", "_wc_worker", "_wc_thread"):
            obj = getattr(self, attr, None)
            if obj is not None:
                obj.deleteLater()
            setattr(self, attr, None)

    def _save_expr_sidecar(self, npz_path: str) -> None:
        """Write expression-layer recipes next to the cache as
        ``<stem>_expr.json`` (plan M2.6 / F4)."""
        if not self._recipes:
            return
        exprs = [{
            "name": r["name"],
            "expr": r["expr"],
            "bindings": {k: list(v) for k, v in r["bindings"].items()},
            "color": r["color"],
        } for r in self._recipes]
        sidecar = Path(npz_path).with_name(Path(npz_path).stem + "_expr.json")
        sidecar.write_text(json.dumps(exprs, indent=2), encoding="utf-8")

    def _on_load_cache(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load layer cache", "", "Layer cache (*.npz)")
        if not path:
            return
        data = gds_layer_cache.cache_load(path)
        if data is None:
            QMessageBox.critical(self, "Invalid cache",
                                 "File is not a readable layer cache "
                                 "(wrong format or schema).")
            return
        doc = GdsDocument()
        doc.path = Path(path)
        doc.format = "CACHE"
        doc.top_cell_name = data.meta.top_cell_name
        for idx, (layer, datatype, polys, bbs) in enumerate(data.layers):
            color = _LAYER_PALETTE[idx % len(_LAYER_PALETTE)]
            doc.entries.append(LayerEntry(
                key=LayerKey(layer=int(layer), datatype=int(datatype)),
                polygons=list(polys), visible=True, color=QColor(color),
                bboxes=np.asarray(bbs, dtype=np.float32)))
        doc._recompute_bbox()

        # F21: restore PART/CHIP (v5) or legacy-snapshot coords (v4) into the
        # PartChipPanel; its ``changed`` signal mirrors them back into
        # self._chip_corner_* etc.
        self.sem_panel.coord_setup.set_from_meta(data.meta)
        # Restore the origin δ (M4a).
        self._origin_dx = float(getattr(data.meta, "origin_dx", 0.0))
        self._origin_dy = float(getattr(data.meta, "origin_dy", 0.0))
        self.sem_panel.alignment_delta.set_values(
            self._origin_dx, self._origin_dy)

        self._doc = doc
        self._load_path = path
        self.layer_panel.set_document(doc)
        self.canvas.set_document(doc)
        self.setWindowTitle(f"GLAS — {Path(path).name} (cache)")
        self._status_state(
            f"{Path(path).name} (cache)  ·  {doc.summary()}")
        self._restore_expr_sidecar(path)

    def _restore_expr_sidecar(self, npz_path: str) -> None:
        """Recreate expression-layer recipes from ``<stem>_expr.json`` and
        re-evaluate them over the loaded document's extent."""
        # A freshly-loaded cache fully owns the recipe set — clear any from a
        # previously-open file even when this cache has no sidecar.
        self._recipes = []
        sidecar = Path(npz_path).with_name(Path(npz_path).stem + "_expr.json")
        if not sidecar.exists():
            return
        try:
            defs = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for d in defs:
            bindings = {k: gds_boolean.normalize_binding(tuple(v))
                        for k, v in d.get("bindings", {}).items()}
            self._register_recipe(
                d.get("name", "expr"), d.get("expr", ""), bindings,
                color=QColor(d.get("color") or "#d44fa0"))
        self._recompute_recipes(self._doc.bbox_nm)

    # ── F9: developer mode ───────────────────────────────────────────────────

    def _attach_dev_toggle(self, widget: QWidget, dlg: QDialog) -> None:
        """Make ``widget`` count clicks; 5 clicks toggles developer mode."""
        widget.setCursor(Qt.CursorShape.PointingHandCursor)
        clicks = {"n": 0}

        def on_click(_event) -> None:
            clicks["n"] += 1
            if clicks["n"] < 5:
                return
            clicks["n"] = 0
            self._set_dev_mode(not self._dev_mode)
            state = "enabled" if self._dev_mode else "disabled"
            QMessageBox.information(
                dlg, "Developer mode",
                f"Developer mode {state}.\n\n"
                "Advanced features (OASIS export) are now "
                f"{'available' if self._dev_mode else 'hidden'}.")

        widget.mousePressEvent = on_click

    def _set_dev_mode(self, on: bool) -> None:
        self._dev_mode = bool(on)
        QSettings("GLAS", "GLAS").setValue("dev_mode", self._dev_mode)
        self._refresh_dev_ui()
        # Toggling dev mode flips batch fine-align per-stage timing. Drop any
        # warmed pool so the next batch spawns workers that inherit the new
        # GLAS_FA_TIMING env (workers read it at import / spawn time).
        self._apply_fa_timing()
        fine_align.batch_pool.shutdown()

    def _apply_fa_timing(self) -> None:
        """Dev mode ON → batch fine-align prints per-stage timing
        (``[fa-timing] read / poi(walk+bool) / template / match``, per worker)
        to stdout; OFF → silent. Sets both the in-process flag (the in-thread
        small-batch path) and the ``GLAS_FA_TIMING`` env var (inherited by the
        spawned pool workers). No env fiddling — it follows dev mode."""
        on = bool(self._dev_mode)
        fine_align._FA_TIMING = on
        if on:
            os.environ["GLAS_FA_TIMING"] = "1"
        else:
            os.environ.pop("GLAS_FA_TIMING", None)

    def _refresh_dev_ui(self) -> None:
        """Show / hide developer-only controls to match ``self._dev_mode``."""
        btn = getattr(self, "_export_oasis_btn", None)
        if btn is not None:
            btn.setVisible(self._dev_mode)
        act = getattr(self, "_diagnose_action", None)
        if act is not None:
            act.setVisible(self._dev_mode)
        # F21 M4: catalog editor button on the PART/CHIP panel.
        if self.sem_panel is not None:
            self.sem_panel.coord_setup.set_dev_mode(self._dev_mode)

    def _on_edit_catalog(self) -> None:
        """Open the PART/CHIP catalog editor (developer-mode only).
        After Save, refresh the PartChipPanel so the dropdowns pick up new
        entries immediately."""
        panel = self.sem_panel.coord_setup
        dlg = CatalogEditorDialog(self, panel._catalog)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            panel.reload_catalog()
            self._status_transient(
                "catalog saved · PART / CHIP dropdowns refreshed")

    def _on_diagnose_oasis(self) -> None:
        """F10: scan a chosen .oas and show a copyable diagnostic report
        (record histogram, per-layer counts, decode error context)."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Diagnose OASIS file", "",
            "OASIS files (*.oas *.oasis);;All files (*)")
        if not path:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            report = oasis_debug.report_file(path)
        finally:
            QApplication.restoreOverrideCursor()
        sidecar = path + ".debug.txt"
        try:
            Path(sidecar).write_text(report, encoding="utf-8")
        except OSError:
            sidecar = None
        DebugReportDialog(self, "OASIS diagnostics", report,
                          saved_path=sidecar).exec()

    def _show_about(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("About GLAS")
        dlg.setFixedSize(380, 280)

        v = QVBoxLayout(dlg)
        v.setContentsMargins(32, 28, 32, 24)
        v.setSpacing(0)

        _icon_path = Path(__file__).resolve().parent / "icons" / "glas_icon_128.svg"
        if _icon_path.exists():
            icon_lbl = QLabel(dlg)
            pm = QPixmap(str(_icon_path)).scaledToHeight(
                72, Qt.TransformationMode.SmoothTransformation)
            icon_lbl.setPixmap(pm)
            icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            v.addWidget(icon_lbl)
            v.addSpacing(12)
            # F9: click the icon 5 times to toggle developer mode.
            self._attach_dev_toggle(icon_lbl, dlg)

        name_lbl = QLabel("GLAS", dlg)
        name_lbl.setStyleSheet(
            "font-size: 22px; font-weight: 500; color: #3f3428; letter-spacing: 3px;")
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(name_lbl)

        sub_lbl = QLabel("GDS-Layout Alignment for SEM", dlg)
        sub_lbl.setStyleSheet(
            "font-size: 10px; color: #8a7660; letter-spacing: 1.5px; margin-top: 2px;")
        sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(sub_lbl)

        v.addSpacing(16)

        ver_lbl = QLabel(f"Version {GLAS_VERSION}", dlg)
        ver_lbl.setStyleSheet("font-size: 12px; color: #9a8878;")
        ver_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(ver_lbl)

        info_lbl = QLabel(
            "Built-in OASIS streaming parser · No klayout / gdstk dependency", dlg)
        info_lbl.setStyleSheet("font-size: 11px; color: #b0a090;")
        info_lbl.setWordWrap(True)
        info_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(info_lbl)

        v.addStretch(1)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok, dlg)
        bb.accepted.connect(dlg.accept)
        v.addWidget(bb)

        dlg.exec()


def main() -> int:
    # Required on Windows so the child loader process re-imports this module
    # cleanly when it spawns. No-op on POSIX.
    mp.freeze_support()
    # Dev-mode diagnostics print ·, µm, ── etc. A legacy console code page
    # (e.g. cp950 on a zh-TW Windows) would raise UnicodeEncodeError on those
    # and abort the print; reconfigure the std streams to UTF-8 with replacement
    # so logs degrade to '?' instead of crashing. PyCharm's console is already
    # UTF-8, so this is a no-op there.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - very old / non-reconfigurable
            pass
    # Filter two harmless, very chatty Qt warnings (a pixel-sized font reports
    # pointSize()==-1; a dialog whose min-height nudges past the requested
    # geometry triggers a setGeometry notice) while letting everything else
    # through.
    try:
        from PyQt6.QtCore import qInstallMessageHandler

        _QUIET = ("setPointSize: Point size <= 0",
                  "QWindowsWindow::setGeometry: Unable to set geometry")

        def _qt_msg_filter(mode, ctx, message):
            if any(q in message for q in _QUIET):
                return
            print(message, file=sys.stderr, flush=True)

        qInstallMessageHandler(_qt_msg_filter)
    except Exception:
        pass
    if "--trace" in sys.argv:
        oasis_random.set_debug(True, level=2)
        sys.argv = [a for a in sys.argv if a != "--trace"]
        print(f"{devlog.tag('gds-align', stream=sys.stderr)} trace mode ON "
              "(level 2: deep per-cell telemetry)", file=sys.stderr, flush=True)
    elif "--debug" in sys.argv:
        oasis_random.set_debug(True, level=1)
        sys.argv = [a for a in sys.argv if a != "--debug"]
        print(f"{devlog.tag('gds-align', stream=sys.stderr)} debug mode ON "
              "(concise ROI summaries; use --trace for full detail)",
              file=sys.stderr, flush=True)
    app = QApplication(sys.argv)

    app.setApplicationName("GLAS")
    app.setApplicationDisplayName("GLAS")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("GLAS")

    _icon_path = Path(__file__).resolve().parent / "icons" / "glas_icon_256.svg"
    if _icon_path.exists():
        app.setWindowIcon(QIcon(str(_icon_path)))

    if _APP_QSS is not None:
        app.setStyleSheet(_APP_QSS)
    else:
        app.setStyleSheet(_FALLBACK_QSS)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
