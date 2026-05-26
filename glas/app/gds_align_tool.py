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
import sys
import threading
import traceback
from concurrent.futures import CancelledError, ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from PyQt6.QtCore import Qt, QObject, QPointF, QRect, QRectF, QSize, QThread, QTimer, QElapsedTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction, QBrush, QColor, QFontMetrics, QIcon, QImage, QKeySequence,
    QPainter, QPen, QPixmap,
    QPolygonF, QMouseEvent, QShortcut, QWheelEvent,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QColorDialog,
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter, QStatusBar, QStyle,
    QStyledItemDelegate, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
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
# F8: the Qt-free fine-align compute lives in glas/core/fine_align.py so a
# spawn-based ProcessPool worker can import it without pulling in PyQt6. Pull
# the functions back into this namespace so existing call sites and tests
# (which reference gds_align_tool.<fn>) keep working unchanged.
import fine_align       # noqa: E402
from fine_align import (  # noqa: E402,F401
    _fine_align_image, _fit_mask, _parabola_subpx, _walk_roi_polys,
    fine_align_one, make_template, poi_polys_for_roi,
    rasterize_layer, render_composite_template, render_poi_template,
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


def roi_document_from_reader(rar, root, layer_keys, roi_bbox, cancel_cb=None):
    """ROI load using an already-built :class:`RandomAccessReader` (the big
    file is slurped + indexed once, reused across image clicks). Builds one
    LayerEntry per ``(layer, datatype)`` in ``layer_keys``. Returns
    ``(doc, [(layer_key, stats), ...])``."""
    doc = GdsDocument()
    doc.path = rar._path
    doc.format = "OASIS-ROI"
    doc.top_cell_name = str(root)
    per_layer: list = []
    for idx, (layer, datatype) in enumerate(layer_keys):
        color = QColor(_LAYER_PALETTE[idx % len(_LAYER_PALETTE)])
        entry, stats = _roi_entry(rar, root, layer, datatype, roi_bbox, color,
                                  cancel_cb=cancel_cb)
        if entry is not None:
            doc.entries.append(entry)
        per_layer.append(((layer, datatype), stats))
    doc._recompute_bbox()
    return doc, per_layer


# ── Threaded loader (keeps the GUI responsive on big OASIS files) ────────────


class LayerPickDialog(QDialog):
    """Multi-select list of layers, shown after a successful layer scan.

    Each row is ``L<layer>/D<datatype>  (optional name)``. ``selected_pairs()``
    returns the chosen ``(layer, datatype)`` tuples in stable display order."""

    def __init__(self, parent: Optional[QWidget],
                 layers: list[dict]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pick layers to load")
        self.setModal(True)
        self.setMinimumSize(420, 460)

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 14)
        v.setSpacing(10)

        v.addWidget(QLabel(
            f"<b>{len(layers)} layer(s)</b> discovered in this file. "
            "Ctrl/Shift-click for multi-select.", self,
        ))

        self._list = QListWidget(self)
        self._list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection)
        for entry in sorted(layers, key=lambda d: (d["layer"], d["datatype"])):
            label = f"L{entry['layer']}/D{entry['datatype']}"
            if entry.get("name"):
                label += f"   ·  {entry['name']}"
            item = QListWidgetItem(label, self._list)
            item.setData(Qt.ItemDataRole.UserRole,
                         (int(entry["layer"]), int(entry["datatype"])))
        v.addWidget(self._list, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Use these")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

    def selected_pairs(self) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for item in self._list.selectedItems():
            data = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, tuple) and len(data) == 2:
                out.append((int(data[0]), int(data[1])))
        return out


class LayerFilterDialog(QDialog):
    """Ask the user which layers to actually load.

    A 300 MB OASIS expands to 1.5–6 GB in klayout's in-memory layout, and the
    "tens of GB" future case is completely infeasible to load whole. The
    alignment workflow only needs 1–2 layers (POI + maybe a reference), so we
    let the user list those up front; ``layer_map`` + ``create_other_layers
    = False`` makes klayout skip everything else during the read itself.

    Result via ``filter_pairs()``: list of ``(layer, datatype)`` ints, or an
    empty list meaning "no filter — load everything" (only sensible for
    small files).
    """

    def __init__(self, parent: Optional[QWidget], file_size_mb: float,
                 file_name: str, file_path: str, *,
                 roi_mode: bool = False) -> None:
        super().__init__(parent)
        self._roi_mode = roi_mode
        self.setWindowTitle("Pick ROI layers" if roi_mode else "Layer filter")
        self.setModal(True)
        self.setMinimumWidth(_capped_min_width(540))

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 14)
        v.setSpacing(10)

        risk = "high" if file_size_mb > 200 else (
            "medium" if file_size_mb > 50 else "low")
        if roi_mode:
            info = QLabel(
                f"<b>{file_name}</b> &nbsp; · &nbsp; {file_size_mb:,.0f} MB"
                "<br><br>"
                "Pick the layer(s) to show in ROI mode. Only the geometry "
                "around the clicked SEM image is loaded, so this is fast even "
                "on huge files."
                "<br><br>"
                "Click <b>Scan layers in file</b> to discover what's "
                "available, or type pairs directly: "
                "<code>layer/datatype</code>, comma-separated "
                "(e.g. <code>17/0, 6/101</code>)."
            )
        else:
            info = QLabel(
                f"<b>{file_name}</b> &nbsp; · &nbsp; {file_size_mb:,.0f} MB "
                f"(in-RAM risk: <b>{risk}</b>)<br><br>"
                "Large OASIS / GDS layouts can blow past available RAM when "
                "loaded in full. The alignment workflow only needs a couple "
                "of layers — let the reader skip everything else during "
                "streaming.<br><br>"
                "Click <b>Scan layers in file</b> to discover what's "
                "available, or type pairs directly: "
                "<code>layer/datatype</code>, comma-separated "
                "(e.g. <code>20/0, 30/0</code>)."
            )
        info.setWordWrap(True)
        info.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(info)

        scan_row = QHBoxLayout()
        scan_row.setSpacing(8)
        self._scan_btn = QPushButton("Scan layers in file", self)
        self._scan_btn.setProperty("variant", "primary")
        self._scan_btn.clicked.connect(self._on_scan)
        scan_row.addWidget(self._scan_btn)
        scan_row.addStretch(1)
        v.addLayout(scan_row)

        self._edit = QLineEdit(self)
        self._edit.setPlaceholderText("e.g.  20/0, 30/0")
        v.addWidget(self._edit)


        self._warning = QLabel("", self)
        self._warning.setStyleSheet(_hint_qss(_FS_LABEL, _TK_ACCENT_DK.name()))
        self._warning.setWordWrap(True)
        v.addWidget(self._warning)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self,
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)
        self._ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_btn.setText("Pick root cell…" if roi_mode else "Load")

        self._file_size_mb = file_size_mb
        self._file_path = file_path
        self._pairs: list[tuple[int, int]] = []
        # Scan-mode state (set during _on_scan).
        self._scan_thread: Optional[QThread] = None
        self._scan_worker: Optional[LayerScanWorker] = None
        self._scan_progress: Optional[LoadProgressDialog] = None

    def filter_pairs(self) -> list[tuple[int, int]]:
        return list(self._pairs)

    # ── Layer scan flow ────────────────────────────────────────────────────
    def _on_scan(self) -> None:
        if self._scan_thread is not None:
            return  # already scanning
        self._scan_btn.setEnabled(False)
        self._warning.setText("")

        self._scan_progress = LoadProgressDialog(self)
        self._scan_progress.set_text(
            f"Scanning {Path(self._file_path).name} for available layers…\n"
            "(this can be slow for big files; cancel to fall back to manual "
            "entry)"
        )

        self._scan_thread = QThread(self)
        self._scan_worker = LayerScanWorker(self._file_path)
        self._scan_worker.moveToThread(self._scan_thread)
        self._scan_thread.started.connect(self._scan_worker.run)

        self._scan_worker.progress.connect(self._scan_progress.set_text)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.cancelled.connect(self._on_scan_cancelled)
        self._scan_progress.cancel_requested.connect(self._scan_worker.cancel)

        for sig in (self._scan_worker.finished,
                    self._scan_worker.failed,
                    self._scan_worker.cancelled):
            sig.connect(self._scan_thread.quit)
        self._scan_thread.finished.connect(self._cleanup_scan)

        self._scan_progress.show()
        QApplication.processEvents()
        self._scan_thread.start()

    def _on_scan_finished(self, layers: object) -> None:
        # Open pick dialog and populate the line edit with the chosen pairs.
        if not isinstance(layers, list) or not layers:
            self._warning.setText(
                "Scan completed but found no layers; type pairs manually.")
            return
        pick = LayerPickDialog(self, layers)
        if pick.exec() == QDialog.DialogCode.Accepted:
            picks = pick.selected_pairs()
            if picks:
                self._edit.setText(
                    ", ".join(f"{l}/{d}" for l, d in picks))
                self._warning.setText("")

    def _on_scan_failed(self, msg: str) -> None:
        self._warning.setText(
            f"Scan failed — fall back to manual entry.\n{msg.splitlines()[0]}")

    def _on_scan_cancelled(self) -> None:
        self._warning.setText("Scan cancelled.")

    def _cleanup_scan(self) -> None:
        if self._scan_progress is not None:
            self._scan_progress.shutdown()
            self._scan_progress.close()
            self._scan_progress.deleteLater()
            self._scan_progress = None
        if self._scan_worker is not None:
            self._scan_worker.deleteLater()
            self._scan_worker = None
        if self._scan_thread is not None:
            self._scan_thread.deleteLater()
            self._scan_thread = None
        self._scan_btn.setEnabled(True)

    def _on_ok(self) -> None:
        text = self._edit.text().strip()
        pairs: list[tuple[int, int]] = []
        if text:
            for chunk in text.split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                if "/" in chunk:
                    l_s, d_s = chunk.split("/", 1)
                else:
                    l_s, d_s = chunk, "0"
                try:
                    pairs.append((int(l_s), int(d_s)))
                except ValueError:
                    self._warning.setText(
                        f"Can't parse '{chunk}' — expected layer/datatype "
                        "(integers).")
                    return
        if not pairs and self._file_size_mb > 200:
            # Empty filter on a likely-OOM file: force the user to confirm.
            if self._warning.text() == "":
                self._warning.setText(
                    "No filter set on a large file — loading may exhaust "
                    "memory. Click Load again to proceed anyway.")
                return
        self._pairs = pairs
        self.accept()


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
    """Stream-scan an OASIS file's LAYERNAME table without parsing cell
    content (F2 M1.12b).

    In a SEMI P39 layout the LAYERNAME records sit between START and
    the first CELL, so we can stop iter_records as soon as we hit a
    CELL header. On 300 MB D2DB this completes in under a second --
    klayout's "instant scan" claim is misleading because it still
    walks every shape record's layer field to decide whether to skip
    it; the streamer skips that entire pass.

    De-dup is by ``(layer, datatype)`` since LAYERNAME-text and
    LAYERNAME-geometry emit the same pair under one name. We keep the
    first name we see and prefer non-empty over empty.
    """
    import sys as _sys
    import time as _t
    from pathlib import Path as _Path

    # Make sure the streamer module (glas/core) is importable in the
    # subprocess context (spawn re-imports without main.py's path setup).
    _core = _Path(__file__).resolve().parent.parent / "core"
    if str(_core) not in _sys.path:
        _sys.path.insert(0, str(_core))
    import oasis_streamer as oas

    t0 = _t.monotonic()
    p = path
    size_mb = p.stat().st_size / 1024 / 1024
    q.put(("progress",
           f"Scanning {p.name} ({size_mb:,.0f} MB) for layers via "
           f"oasis_streamer…"))

    layers: list[dict] = []
    seen: set[tuple[int, int]] = set()
    record_count = 0
    with oas.OasisReader(p) as reader:
        for rid, payload in reader.iter_records():
            record_count += 1
            if rid in (oas.LAYERNAME_GEOM, oas.LAYERNAME_TEXT):
                name = payload["name"].decode("ascii", "backslashreplace")
                # Calibre / klayout typically write LAYERNAME with
                # interval kind 3 (n..INF), so the layer / datatype
                # number is the low end of each interval.
                L = int(payload["layer_interval"][0])
                D = int(payload["datatype_interval"][0])
                key = (L, D)
                if key not in seen:
                    seen.add(key)
                    layers.append({"layer": L, "datatype": D, "name": name})
                elif name:
                    # Update with non-empty name if previous was blank.
                    for ent in layers:
                        if ent["layer"] == L and ent["datatype"] == D and not ent["name"]:
                            ent["name"] = name
                            break
            elif rid in (oas.CELL_REFNUM, oas.CELL_NAME):
                # End of header: every LAYERNAME has been seen by now.
                break

    elapsed = _t.monotonic() - t0
    _sys.stderr.write(
        f"[gds-scan] {elapsed:.2f}s  enumerated {len(layers)} layers "
        f"from {record_count:,} pre-CELL records\n")
    _sys.stderr.flush()
    q.put(("done", layers))


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
                cancel_cb=lambda: self._cancel)
        except oasis_random.WalkCancelled:
            self.cancelled.emit()
        except Exception as exc:                       # noqa: BLE001
            self.failed.emit(str(exc))
        else:
            self.finished.emit(doc, per_layer)


def _auto_batch_workers() -> int:
    """Thread-pool size for batch fine-align (F6 M3): one per core, capped at
    8 so a many-core box doesn't oversubscribe cv2's own internal threads."""
    return max(1, min(os.cpu_count() or 1, 8))


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
            workers = min(_auto_batch_workers(), n)
            # Small batches: process spawn + per-process reader build costs more
            # than it saves, so run them in-thread on a single cloned reader.
            if n <= 2 or workers <= 1:
                self._run_in_thread()
            else:
                self._run_process_pool(workers)
        except Exception as exc:                       # noqa: BLE001
            self.failed.emit(str(exc))

    def _run_in_thread(self) -> None:
        """Sequential fallback for tiny batches: one cloned reader, in this
        worker thread. The walk's ``cancel_cb`` reads the event so a mid-walk
        cancel still bails promptly."""
        n = len(self._jobs)
        rar = self._rar.clone()
        done = 0
        try:
            for job in self._jobs:
                if self._cancel.is_set():
                    self.cancelled.emit()
                    return
                try:
                    res = fine_align._fine_align_image(
                        job, rar, self._root, self._poi_specs, self._cfg,
                        self._cancel.is_set)
                except oasis_random.WalkCancelled:
                    self.cancelled.emit()
                    return
                done += 1
                if res is not None:
                    self.progress.emit(done, n, res[0])
                    self.result.emit(*res)
        finally:
            rar.close()
        self.finished.emit(done)

    def _run_process_pool(self, workers: int) -> None:
        """Parallel path: fan the per-image jobs out to a spawn-based process
        pool. Each worker rebuilds its reader once via the initializer. On
        cancel, futures that haven't started are dropped; in-flight images run
        to completion and their results are still kept (deterministic partial
        results, like the old thread-pool drain)."""
        n = len(self._jobs)
        rar = self._rar
        # Reader params (not the live reader, which owns an unpicklable mmap):
        # each worker rebuilds an identical reader from these.
        initargs = (str(rar._path), rar._init_wanted, rar._dtype,
                    rar._bbox_layer, self._root, self._poi_specs, self._cfg)
        ctx = mp.get_context("spawn")
        ex = ProcessPoolExecutor(max_workers=workers, mp_context=ctx,
                                 initializer=fine_align._pool_init,
                                 initargs=initargs)
        done = 0
        dropped = False
        try:
            futures = [ex.submit(fine_align._pool_task, job)
                       for job in self._jobs]
            for fut in as_completed(futures):
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
        finally:
            ex.shutdown(wait=True)
        if self._cancel.is_set():
            self.cancelled.emit()
        else:
            self.finished.emit(done)


def _safe_name(s: str) -> str:
    """Filesystem-safe basename from an image id (F5 M6)."""
    out = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in str(s))
    return out or "image"


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

    _COLS = ["image_id", "raw_png", "overlay_png", "fine_dx_nm", "fine_dy_nm",
             "score", "status"]

    def __init__(self, rar, root, poi_specs_colored, jobs, cfg, out_dir,
                 export_raw: bool, export_overlay: bool) -> None:
        super().__init__()
        self._rar = rar
        self._root = root
        self._poi = list(poi_specs_colored)   # [(spec, (r,g,b)), ...]
        self._jobs = jobs    # [(image_id, coarse|None, refined|None, path, exists)]
        self._cfg = cfg
        self._out_dir = Path(out_dir)
        self._export_raw = export_raw
        self._export_overlay = export_overlay
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            n = len(self._jobs)
            done = 0
            c = self._cfg
            manifest = []
            for image_id, coarse, refined, path, exists in self._jobs:
                if self._cancel.is_set():
                    self.cancelled.emit()
                    return
                done += 1
                self.progress.emit(done, n, str(image_id))
                row = {
                    "image_id": str(image_id), "raw_png": "", "overlay_png": "",
                    "fine_dx_nm": "" if refined is None else round(refined[0], 3),
                    "fine_dy_nm": "" if refined is None else round(refined[1], 3),
                    "score": "" if refined is None else round(refined[2], 6),
                    "status": "ok" if refined is not None else "not-run",
                }
                sem = (cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
                       if (cv2 and exists) else None)
                if sem is None:
                    row["status"] = "missing-file"
                    manifest.append(row)
                    continue
                base = _safe_name(image_id)
                if self._export_raw:
                    name = f"{base}_raw.png"
                    cv2.imwrite(str(self._out_dir / name), sem)
                    row["raw_png"] = name
                if self._export_overlay and coarse is not None and self._poi:
                    H, W = sem.shape[:2]
                    nm_per_px = (c["nm_manual"] if (not c["nm_auto"] and
                                 c["nm_manual"] > 0) else c["fov_w"] / max(1, W))
                    if nm_per_px > 0:
                        roi = (coarse[0] - c["fov_w"], coarse[1] - c["fov_h"],
                               coarse[0] + c["fov_w"], coarse[1] + c["fov_h"])
                        entries = []
                        for spec, color in self._poi:
                            polys = poi_polys_for_roi(
                                self._rar, self._root, roi, spec,
                                cancel_cb=self._cancel.is_set)
                            if polys:
                                entries.append((polys, color))
                        anchor = (coarse if refined is None else
                                  (coarse[0] + refined[0], coarse[1] + refined[1]))
                        rgb = overlay_outlines_on_sem(sem, entries, anchor,
                                                      nm_per_px)
                        name = f"{base}_overlay.png"
                        # overlay returns RGB; cv2 writes BGR → flip channels.
                        cv2.imwrite(str(self._out_dir / name), rgb[:, :, ::-1])
                        row["overlay_png"] = name
                manifest.append(row)
            mpath = self._write_manifest(manifest)
        except oasis_random.WalkCancelled:
            self.cancelled.emit()
        except Exception as exc:                       # noqa: BLE001
            self.failed.emit(str(exc))
        else:
            self.finished.emit(done, mpath)

    def _write_manifest(self, rows) -> str:
        csv_path = self._out_dir / "overlay_manifest.csv"
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self._COLS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in self._COLS})
        json_path = self._out_dir / "overlay_manifest.json"
        with open(json_path, "w") as f:
            json.dump({"schema": "mmh-gds-overlay-v1", "columns": self._COLS,
                       "images": rows}, f, indent=2)
        return str(csv_path)


# ── GUI overlay-outline helper (raster stroking; rasterize_layer + template /
#    fine-align compute moved to glas/core/fine_align.py in F8) ───────────────


def _draw_polyline_np(rgb: np.ndarray, pts: np.ndarray, color: tuple) -> None:
    """Stroke a closed polyline into an RGB array (numpy fallback for the
    overlay helper when cv2 is unavailable)."""
    H, W = rgb.shape[:2]
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        xs = np.linspace(x0, x1, steps).round().astype(int)
        ys = np.linspace(y0, y1, steps).round().astype(int)
        m = (xs >= 0) & (xs < W) & (ys >= 0) & (ys < H)
        rgb[ys[m], xs[m]] = color


def overlay_outlines_on_sem(sem_gray: np.ndarray, entries: list, anchor: tuple,
                            nm_per_px: float, thickness: int = 1) -> np.ndarray:
    """Draw layer outlines over a SEM frame, returning an (H, W, 3) uint8 RGB.

    The SEM grayscale is widened to grey RGB, then each entry's polygon
    *outlines* are stroked in its colour. ``entries`` is ``[(polygons, (r,g,b)),
    ...]`` with polygons as (N, 2) nm arrays. The FOV is centred on ``anchor``
    at ``nm_per_px``, mirroring :func:`rasterize_layer`'s mapping (X right, Y
    flipped to screen convention) so the outlines land on the SEM structure for
    a given coarse/refined anchor. Self-contained raster — it does not touch the
    SemViewer screen drawing (F5 M1). Used by both the before/after preview and
    the overlay PNG export (M6)."""
    H, W = sem_gray.shape[:2]
    rgb = np.repeat(sem_gray.astype(np.uint8)[:, :, None], 3, axis=2).copy()
    gx, gy = anchor
    x0 = gx - W / 2.0 * nm_per_px
    y1 = gy + H / 2.0 * nm_per_px
    for polygons, color in entries:
        col = (int(color[0]), int(color[1]), int(color[2]))
        for poly in polygons:
            arr = np.asarray(poly, dtype=np.float64)
            if arr.shape[0] < 2:
                continue
            px = (arr[:, 0] - x0) / nm_per_px
            py = (y1 - arr[:, 1]) / nm_per_px
            pts = np.stack([px, py], axis=1)
            if cv2 is not None:
                ip = pts.round().astype(np.int32)
                cv2.polylines(rgb, [ip], isClosed=True, color=col,
                              thickness=max(1, thickness),
                              lineType=cv2.LINE_AA)
            else:
                _draw_polyline_np(rgb, pts, col)
    return rgb


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
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(8, 6, 8, 0)
        btn_row.addWidget(self._expr_btn)
        layout.addLayout(btn_row)

        hint = QLabel("checkbox: show/hide  ·  POI: fine-align template  ·  swatch: colour")
        hint.setStyleSheet(_hint_qss(_FS_MICRO, pad="6px 10px"))
        hint.setWordWrap(True)
        layout.addWidget(hint)

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

        # 次文
        hint_item = QListWidgetItem("toolbar → Open OASIS…")
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
        self._corner_overlay: Optional[QWidget] = None   # M7-ov #9 minimap

    def set_corner_overlay(self, w: Optional[QWidget]) -> None:
        """Host a small floating widget (the minimap) pinned bottom-right."""
        self._corner_overlay = w
        self._reposition_overlay()

    def _reposition_overlay(self) -> None:
        w = self._corner_overlay
        if w is None:
            return
        m = 12
        w.move(max(0, self.width() - w.width() - m),
               max(0, self.height() - w.height() - m))

    def resizeEvent(self, ev) -> None:  # type: ignore[override]
        super().resizeEvent(ev)
        self._reposition_overlay()

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
                   "Follow the steps above to get started")

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


class MiniMap(QWidget):
    """A small picture-in-picture defect map (M7-ov #9), floated over the SEM
    view. Draws all defect dots (colour = score) + the current FOV centre,
    auto-fit to the defect bounds. Click a dot to select that image. Renders
    only dots/markers — no OASIS geometry — so it stays cheap."""

    defect_clicked = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(212, 152)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._defects: list = []
        self._current: Optional[str] = None
        self._fov: Optional[tuple] = None     # (cx, cy) nm

    def set_data(self, defects: list, current_id: Optional[str],
                 fov_center: Optional[tuple]) -> None:
        self._defects = list(defects)
        self._current = current_id
        self._fov = fov_center
        self.update()

    def _bbox(self):
        pts = [(d[1], d[2]) for d in self._defects]
        if self._fov is not None:
            pts.append(self._fov)
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        px = max((x1 - x0) * 0.12, 500.0)
        py = max((y1 - y0) * 0.12, 500.0)
        return (x0 - px, y0 - py, x1 + px, y1 + py)

    def _map(self, gx, gy, bbox):
        x0, y0, x1, y1 = bbox
        iw, ih = self.width() - 16, self.height() - 26
        s = min(iw / max(1.0, x1 - x0), ih / max(1.0, y1 - y0))
        cx, cy = 8 + iw / 2.0, 20 + ih / 2.0
        wx, wy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        return cx + (gx - wx) * s, cy - (gy - wy) * s

    def paintEvent(self, ev) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bg = QColor("#fbf8f3")
        bg.setAlpha(238)
        p.setBrush(QBrush(bg))
        p.setPen(QPen(_TK_BORDER_DK, 1))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 6, 6)
        p.setPen(QPen(_TK_TEXT_HINT))
        p.drawText(QRectF(8, 4, self.width() - 16, 14),
                   Qt.AlignmentFlag.AlignLeft, "Defect map")
        bbox = self._bbox()
        if bbox is None:
            p.setPen(QPen(_TK_TEXT_HINT))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "no defects")
            p.end()
            return
        for image_id, gx, gy, score in self._defects:
            sx, sy = self._map(gx, gy, bbox)
            p.setPen(QPen(QColor("#ffffff"), 0.8))
            p.setBrush(QBrush(GdsCanvas._score_color(score)))
            p.drawEllipse(QPointF(sx, sy), 3.2, 3.2)
            if image_id == self._current:
                p.setPen(QPen(_TK_ACCENT_DK, 1.4))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(sx, sy), 6.0, 6.0)
        if self._fov is not None:
            fx, fy = self._map(self._fov[0], self._fov[1], bbox)
            p.setPen(QPen(_TK_ACCENT_DK, 1.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawLine(QPointF(fx - 5, fy), QPointF(fx + 5, fy))
            p.drawLine(QPointF(fx, fy - 5), QPointF(fx, fy + 5))
        p.end()

    def mousePressEvent(self, ev: QMouseEvent) -> None:  # type: ignore[override]
        bbox = self._bbox()
        if bbox is None:
            return
        px, py = ev.position().x(), ev.position().y()
        best, best_d2 = None, 12.0 ** 2
        for image_id, gx, gy, _s in self._defects:
            sx, sy = self._map(gx, gy, bbox)
            d2 = (sx - px) ** 2 + (sy - py) ** 2
            if d2 <= best_d2:
                best, best_d2 = image_id, d2
        if best is not None:
            self.defect_clicked.emit(best)


class CoordinateSetupPanel(QGroupBox):
    """Coordinate setup (RFL Chip-offset table) + FOV + overlay scale +
    origin δ + fine-tune (plan M3 / M4a).

    The user copies the RFL "Chip offset" row directly, all in **µm**
    (lower-left origins): chip corner (DieX/DieY) relative to the die
    corner, chip size (SizeW/SizeH), and the GDS default origin offset.
    The chip-corner offset (nm) the jump logic needs is
    ``(DieX − GDS_off) × 1000``. These + FOV + nm/px + origin δ are
    persisted in the layer cache; fine-tune is per-session. Emits
    ``changed`` so the canvas can re-jump when any value moves."""

    changed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Coordinate Setup", parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)

        def _um(lo: float = -100_000_000.0,
                hi: float = 100_000_000.0) -> QDoubleSpinBox:
            s = QDoubleSpinBox(self)
            s.setDecimals(5)               # RFL gives ~5 dp in um
            s.setRange(lo, hi)
            s.setSingleStep(1.0)
            s.setGroupSeparatorShown(True)
            s.valueChanged.connect(self._on_changed)
            return s

        def _nm(lo: int, hi: int, step: int = 100) -> QSpinBox:
            s = QSpinBox(self)
            s.setRange(lo, hi)
            s.setSingleStep(step)
            s.setGroupSeparatorShown(True)
            s.valueChanged.connect(self._on_changed)
            return s

        # RFL "Chip offset" table (all um, lower-left origins): chip corner
        # (DieX/DieY) rel die corner, chip size (SizeW/SizeH), and the GDS
        # default origin offset. chip_corner = (DieX − GDS_off) → nm.
        self._chip_x = _um(0.0)
        self._chip_y = _um(0.0)
        self._chip_w = _um(0.0)
        self._chip_h = _um(0.0)
        self._gds_off_x = _um()
        self._gds_off_y = _um()
        # FOV + fine-tune stay in nm (canvas works in nm).
        self._fov_w = _nm(0, 100_000_000)
        self._fov_h = _nm(0, 100_000_000)
        self._fine_dx = _nm(0, 0, step=10)   # range set when FOV changes
        self._fine_dy = _nm(0, 0, step=10)
        # Overlay scale (nm per native image pixel).
        self._nm_per_px = QDoubleSpinBox(self)
        self._nm_per_px.setDecimals(4)
        self._nm_per_px.setRange(0.0, 1_000_000.0)
        self._nm_per_px.setSingleStep(0.5)
        self._nm_per_px.setGroupSeparatorShown(True)
        self._nm_per_px.setEnabled(False)   # auto by default
        self._nm_per_px.valueChanged.connect(self._on_changed)
        self._nm_auto = QCheckBox("auto = FOV ÷ image px", self)
        self._nm_auto.setChecked(True)
        self._nm_auto.toggled.connect(
            lambda on: self._nm_per_px.setEnabled(not on))
        self._nm_auto.toggled.connect(self._on_changed)

        self._chip_x.setToolTip("RFL Chip-offset DieX: chip lower-left X relative to die lower-left (µm).")
        self._chip_y.setToolTip("RFL Chip-offset DieY: chip lower-left Y relative to die lower-left (µm).")
        self._chip_w.setToolTip("RFL Chip-offset SizeW: chip width (µm).")
        self._chip_h.setToolTip("RFL Chip-offset SizeH: chip height (µm).")
        self._gds_off_x.setToolTip("RFL GDS default offset X (µm; usually sub-micron, negligible).")
        self._gds_off_y.setToolTip("RFL GDS default offset Y (µm).")
        self._fov_w.setToolTip("SEM image field-of-view width (nm). Sets the FOV box size and the GDS ROI load extent.")
        self._fov_h.setToolTip("SEM image field-of-view height (nm).")
        self._fine_dx.setToolTip("Manual fine-tune X (nm) for the < 1 FOV residual; range ±FOV.")
        self._fine_dy.setToolTip("Manual fine-tune Y (nm) for the < 1 FOV residual; range ±FOV.")

        self._corner_lbl = QLabel("→ chip corner: 0, 0 nm")
        self._corner_lbl.setWordWrap(True)
        self._corner_lbl.setStyleSheet(_hint_qss(_FS_LABEL, pad="2px 0"))
        self._corner_lbl.setToolTip(
            "chip_corner = (DieX − GDS_off) × 1000 (µm→nm); "
            "fed to klarf_to_gds() for coordinate conversion.")
        self._nm_per_px.setToolTip(
            "GDS-to-SEM scale (nm per pixel). auto = FOV width ÷ image pixel width.")
        self._origin_lbl = QLabel("origin δ: 0, 0 nm")
        self._origin_lbl.setWordWrap(True)
        self._origin_lbl.setStyleSheet(_hint_qss(_FS_LABEL, pad="2px 0"))
        self._origin_lbl.setToolTip(
            "Constant KLARF→GDS origin correction δ. Drag the GDS over the SEM "
            "to align, then press Set Offset to fill it.")

        intro = QLabel("One-time setup per OASIS — saved with the cache. "
                       "Copy ①/② from the RFL; ④ comes from dragging.")
        intro.setWordWrap(True)
        intro.setStyleSheet(_hint_qss(_FS_CAPTION, pad="0 0 4px 0"))

        # (kind, label, widget) — "head" rows are bold section dividers.
        layout: list[tuple[str, str, object]] = [
            ("span", "", intro),
            ("head", "① RFL Chip offset — µm (origin: lower-left)", None),
            ("row", "Chip corner X (DieX)", self._chip_x),
            ("row", "Chip corner Y (DieY)", self._chip_y),
            ("row", "Chip width (SizeW)", self._chip_w),
            ("row", "Chip height (SizeH)", self._chip_h),
            ("row", "GDS offset X", self._gds_off_x),
            ("row", "GDS offset Y", self._gds_off_y),
            ("corner", "", None),
            ("head", "② FOV — nm", None),
            ("row", "FOV width", self._fov_w),
            ("row", "FOV height", self._fov_h),
            ("head", "③ Overlay scale — nm/px", None),
            ("row", "nm per pixel", self._nm_per_px),
            ("span", "", self._nm_auto),
            ("head", "④ Origin offset δ — nm (drag + Set Offset)", None),
            ("origin", "", None),
            ("head", "⑤ Fine tune — nm (±FOV)", None),
            ("row", "dx", self._fine_dx),
            ("row", "dy", self._fine_dy),
        ]
        # Single-column form (label above input): the 280px panel can't fit
        # "label : input" side-by-side without truncating these labels (M7).
        r = 0

        def _add(w):
            nonlocal r
            grid.addWidget(w, r, 0)
            r += 1

        for kind, label, widget in layout:
            if kind == "head":
                hdr = QLabel(label)
                hdr.setWordWrap(True)
                hdr.setStyleSheet(
                    f"font-weight:700; color:{_TK_ACCENT_DK.name()}; "
                    f"font-size:{_FS_CAPTION}px; margin-top:8px;")
                _add(hdr)
            elif kind == "corner":
                _add(self._corner_lbl)
            elif kind == "origin":
                _add(self._origin_lbl)
            elif kind == "span":
                _add(widget)
            else:
                cap = QLabel(label)
                cap.setStyleSheet(_hint_qss(_FS_CAPTION))
                _add(cap)
                _add(widget)

        self._fov_w.valueChanged.connect(self._sync_fine_range)
        self._fov_h.valueChanged.connect(self._sync_fine_range)
        self._suppress = False

    def _on_changed(self) -> None:
        cx, cy = self._chip_corner_nm()
        self._corner_lbl.setText(
            f"→ chip corner: {cx:,.0f}, {cy:,.0f} nm  "
            f"({cx / 1e3:,.3f}, {cy / 1e3:,.3f} µm)")
        if not self._suppress:
            self.changed.emit()

    def _sync_fine_range(self) -> None:
        """Fine-tune is bounded to ±FOV (plan: corrects < 1 FOV residual)."""
        rx = int(self._fov_w.value())
        ry = int(self._fov_h.value())
        self._fine_dx.setRange(-rx, rx)
        self._fine_dy.setRange(-ry, ry)

    def _chip_corner_nm(self) -> tuple[float, float]:
        # chip corner (rel die corner) = DieX − GDS_offset, in nm.
        return ((self._chip_x.value() - self._gds_off_x.value()) * 1e3,
                (self._chip_y.value() - self._gds_off_y.value()) * 1e3)

    # ── value access ─────────────────────────────────────────────────────────
    def values(self) -> dict:
        cc_x, cc_y = self._chip_corner_nm()
        return {
            "chip_corner_x": cc_x,
            "chip_corner_y": cc_y,
            "chip_x_um": float(self._chip_x.value()),
            "chip_y_um": float(self._chip_y.value()),
            "chip_w_um": float(self._chip_w.value()),
            "chip_h_um": float(self._chip_h.value()),
            "gds_off_x_um": float(self._gds_off_x.value()),
            "gds_off_y_um": float(self._gds_off_y.value()),
            "fov_w": float(self._fov_w.value()),
            "fov_h": float(self._fov_h.value()),
            "fine_dx": float(self._fine_dx.value()),
            "fine_dy": float(self._fine_dy.value()),
            "nm_per_px_manual": float(self._nm_per_px.value()),
            "nm_auto": bool(self._nm_auto.isChecked()),
        }

    def set_origin(self, dx: float, dy: float) -> None:
        """Update the read-only origin-δ label (the value lives in
        MainWindow; this just reflects it)."""
        self._origin_lbl.setText(f"origin δ: {dx:,.0f}, {dy:,.0f} nm")

    def set_from_meta(self, meta) -> None:
        """Auto-fill the RFL Chip-offset params + FOV from a loaded cache's
        metadata, firing ``changed`` once at the end."""
        self._suppress = True
        self._chip_x.setValue(getattr(meta, "chip_x_um", 0.0))
        self._chip_y.setValue(getattr(meta, "chip_y_um", 0.0))
        self._chip_w.setValue(getattr(meta, "chip_w_um", 0.0))
        self._chip_h.setValue(getattr(meta, "chip_h_um", 0.0))
        self._gds_off_x.setValue(getattr(meta, "gds_off_x_um", 0.0))
        self._gds_off_y.setValue(getattr(meta, "gds_off_y_um", 0.0))
        self._fov_w.setValue(int(getattr(meta, "fov_w", 0.0)))
        self._fov_h.setValue(int(getattr(meta, "fov_h", 0.0)))
        npx = float(getattr(meta, "nm_per_px", 0.0))
        if npx > 0:
            self._nm_per_px.setValue(npx)
            self._nm_auto.setChecked(False)
            self._nm_per_px.setEnabled(True)
        self._sync_fine_range()
        self._suppress = False
        self._on_changed()


class FineAlignPanel(QGroupBox):
    """Multi-POI template settings + auto fine-alignment controls (plan F3).

    Toggle one or more POI layers on the left LayerPanel; each appears here
    with its own FG grey-level spinbox. They composite into one synthetic
    template (shared BG / blur), which is matched against the SEM with
    ``cv2.matchTemplate``. Emits ``run_requested`` / ``run_all_requested`` /
    ``preview_requested`` for MainWindow to do the work."""

    run_requested = pyqtSignal()
    run_all_requested = pyqtSignal()
    preview_requested = pyqtSignal()
    results_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Fine Align", parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)

        self._poi_lbl = QLabel("POI: (none — toggle 'POI' on a layer)")
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

        # Secondary (not primaryBtn): a disabled primary renders as washed-out
        # pale-orange with faint text; secondary greys cleanly (M7 R6).
        self._run_btn = QPushButton("Run fine align", self)
        self._run_btn.setEnabled(False)
        self._run_btn.setToolTip(
            "Refine the alignment of the currently loaded ROI against the "
            "selected SEM image (needs ≥1 POI + loaded ROI + image).")
        self._run_btn.clicked.connect(self.run_requested)

        self._run_all_btn = QPushButton("Run all images", self)
        self._run_all_btn.setEnabled(False)
        self._run_all_btn.setToolTip(
            "Walk each defect's ROI and fine-align every image in the dataset "
            "(slow on big files; cancellable).")
        self._run_all_btn.clicked.connect(self.run_all_requested)

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
            ("span", self._run_btn), ("span", self._run_all_btn),
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
            if self._poi_set else "POI: (none — toggle 'POI' on a layer)")
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
        self._run_all_btn.setEnabled(on)
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
        while self._charts_l.count():
            it = self._charts_l.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        scores = [r["score"] for r in self._rows if r["score"] is not None]
        self._charts_l.addWidget(
            _ScoreHistogram(score_histogram(scores), self._threshold), 1)
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

    def set_progress(self, done: int, total: int, image_id: str = "") -> None:
        self._progress = (done, total)
        self._cur_id = image_id
        self._bar.set_fraction(done / total if total else 0.0)
        self._refresh_detail()

    def end_progress(self, text: str = "") -> None:
        self._tick.stop()
        self._prog_box.hide()

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
    drop-down) + Coordinate Setup + the image list. Owns no file I/O — it
    asks MainWindow to open dialogs and is handed the resulting
    :class:`sem_loader.SemImage` list."""

    load_klarf_requested = pyqtSignal()
    load_folder_requested = pyqtSignal()
    load_roi_requested = pyqtSignal()
    image_selected = pyqtSignal(object)   # sem_loader.SemImage

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("rightPanel")
        self.setFixedWidth(300)          # M7 R8: a touch wider eases density
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

        title = QLabel("SEM")
        title.setObjectName("panelTitle")
        v.addWidget(title)

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

        # Coordinate Setup is a one-time setup — keep it in a collapsible
        # section so it doesn't permanently crowd the image list (M7.1).
        # It starts expanded; MainWindow collapses it after cache load / first
        # successful jump. self.coord_setup / self.fine_align keep pointing at
        # the inner panels (tests + signal wiring rely on those refs).
        self.coord_setup = CoordinateSetupPanel(self)
        self._coord_section = self._wrap_section(
            "Coordinate Setup", self.coord_setup, collapsed=True)
        v.addWidget(self._coord_section)

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
            "FOV / fine-tune to reload.")
        self.load_roi_btn.clicked.connect(self.load_roi_requested)

        # Set / Clear Offset — 屬於 SEM 對位流程，放在 image list 下方
        offset_row = QHBoxLayout()
        offset_row.setSpacing(6)
        self.set_offset_btn = QPushButton("Set Offset")
        self.set_offset_btn.setToolTip(
            "Fold the current GDS drag into the global origin correction δ.")
        self.clear_offset_btn = QPushButton("Clear Offset")
        self.clear_offset_btn.setToolTip("Reset the global δ and drag to zero.")
        offset_row.addWidget(self.set_offset_btn)
        offset_row.addWidget(self.clear_offset_btn)
        v.addLayout(offset_row)

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
        # Seed the collapsed-state FOV badge so it's present from the start.
        self.update_coord_badge({})

    def update_coord_badge(self, values: dict) -> None:
        """Reflect the Coordinate Setup FOV state in the section header badge
        (shown while the section is collapsed): green ``FOV W × H`` (µm) when
        set, muted amber ``not set`` otherwise."""
        if CollapsibleSection is None:
            return
        fov_w = float(values.get("fov_w_nm", values.get("fov_w", 0)) or 0)
        fov_h = float(values.get("fov_h_nm", values.get("fov_h", 0)) or 0)
        if fov_w > 0 and fov_h > 0:
            w_um = int(round(fov_w / 1000))
            h_um = int(round(fov_h / 1000))
            self._coord_section.set_badge(
                f"FOV {w_um} × {h_um}", fg="#3e7f5d", bg="#ebf7f0")
        else:
            self._coord_section.set_badge(
                "not set", fg="#c8a080", bg="#fff0e0")

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

    def set_coord_collapsed(self, val: bool) -> None:
        if CollapsibleSection is not None:
            self._coord_section.set_collapsed(val)

    def set_fine_collapsed(self, val: bool) -> None:
        if CollapsibleSection is not None:
            self._fine_section.set_collapsed(val)
        self._scores: dict = {}     # image_id -> (score, threshold)

    def set_images(self, images: list) -> None:
        self._images = list(images)
        self._scores = {}
        self.list.clear()
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


class AlignmentExportDialog(QDialog):
    """Pick the format (CSV / JSON) and which images to export (plan M5).
    Defaults to every image checked."""

    def __init__(self, parent, images) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export alignment")
        self.setMinimumWidth(_capped_min_width(360))
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

    def selected(self) -> tuple:
        """``(fmt, [image_id, ...], export_raw, export_overlay)`` where ``fmt``
        is 'csv' or 'json'."""
        fmt = "csv" if self._fmt.currentIndex() == 0 else "json"
        ids = [self._list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self._list.count())
               if self._list.item(i).checkState() == Qt.CheckState.Checked]
        return fmt, ids, self._exp_raw.isChecked(), self._exp_overlay.isChecked()


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
        # Coordinate Setup starts collapsed (see SemPanel); don't auto-collapse
        # again so a user re-expanding it sticks.
        self._coord_collapsed_once = True

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
        # overview / minimap are opt-in view modes.
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
        # M7-ov #9: corner minimap floated over the SEM view (hidden unless in
        # 'minimap' mode).
        self.minimap = MiniMap(self.sem_viewer)
        self.minimap.hide()
        self.minimap.defect_clicked.connect(self._on_defect_clicked)
        self.sem_viewer.set_corner_overlay(self.minimap)

        self.sem_panel = SemPanel(splitter)
        # Origin-offset (δ) controls live in the SEM panel (below the image
        # list); they operate on the current SEM-overlay drag.
        self.sem_panel.set_offset_btn.clicked.connect(self._on_set_offset)
        self.sem_panel.clear_offset_btn.clicked.connect(self._on_clear_offset)

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
        self._status_doc = QLabel("no GDS loaded")
        self._status_cursor = QLabel("")
        self._status_bar.addWidget(self._status_doc, 1)
        self._status_bar.addPermanentWidget(self._status_cursor)

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
        self.sem_panel.load_klarf_requested.connect(self._on_load_klarf)
        self.sem_panel.load_folder_requested.connect(self._on_load_folder)
        self.sem_panel.load_roi_requested.connect(self._on_load_roi_clicked)
        self.sem_panel.image_selected.connect(self._on_sem_image_selected)
        self.sem_panel.coord_setup.changed.connect(self._on_coord_changed)
        self.sem_panel.fine_align.run_requested.connect(self._on_run_fine_align)
        self.sem_panel.fine_align.run_all_requested.connect(self._on_run_fine_align_all)
        self.sem_panel.fine_align.preview_requested.connect(self._on_preview_template)
        self.sem_panel.fine_align.results_requested.connect(self._open_fa_results)
        self.sem_viewer.drag_changed.connect(self._on_overlay_drag)

        self._doc: Optional[GdsDocument] = None
        self._load_path: Optional[str] = None

        # Alignment settings (M3 Coordinate Setup panel is the source of
        # truth; these mirror it via _on_coord_changed). chip + FOV are
        # persisted in / restored from the layer cache; fine-tune is
        # per-session.
        self._chip_corner_x: float = 0.0
        self._chip_corner_y: float = 0.0
        self._fov_w: float = 0.0
        self._fov_h: float = 0.0
        self._fine_dx: float = 0.0
        self._fine_dy: float = 0.0
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

        self._status_doc.setText("ready · OASIS streamer (built-in)")

        self._update_guidance()

    def showEvent(self, ev) -> None:  # type: ignore[override]
        super().showEvent(ev)
        # Pin the fixed side panels and give the centre all remaining width.
        # A QSplitter otherwise dumps leftover space into a fixed-width pane,
        # leaving the right panel floating with a gap to the window edge.
        if not self._split_sized:
            self._split_sized = True
            QTimer.singleShot(0, self._fit_split_sizes)

    def _fit_split_sizes(self) -> None:
        w = self._main_split.width()
        if w <= 0:
            return
        lw = self.layer_panel.width() or 260
        rw = self.sem_panel.width() or 280
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

        # GLAS wordmark（toolbar 最左側）
        _wm_path = Path(__file__).resolve().parent / "icons" / "glas_wordmark.svg"
        if _wm_path.exists():
            wm_label = QLabel(bar)
            wm_pixmap = QPixmap(str(_wm_path))
            wm_label.setPixmap(
                wm_pixmap.scaledToHeight(28, Qt.TransformationMode.SmoothTransformation)
            )
            wm_label.setContentsMargins(4, 0, 8, 0)
            h.addWidget(wm_label)
            h.addWidget(_divider())

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

        load_btn = QPushButton(_qicon("folder"), " Load Cache…")
        load_btn.setToolTip("Restore layers + settings from a .npz cache.")
        load_btn.clicked.connect(self._on_load_cache)
        h.addWidget(load_btn)

        export_btn = QPushButton(_qicon("save"), " Export Cache…")
        export_btn.setToolTip(
            "Save the loaded layers + alignment settings to a .npz cache "
            "so the next launch opens instantly (M2.1).")
        export_btn.clicked.connect(self._on_export_cache)
        h.addWidget(export_btn)

        h.addWidget(_divider())

        # ── View mode (segmented, exclusive) ──
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
                             "SEM + overlay only (full width).")
        self._seg_gds = _seg("GDS", "layers", "gds",
                             "SEM beside the whole-chip GDS overview (50/50).")
        self._seg_mini = _seg("Minimap", "target", "minimap",
                              "SEM full width with a defect minimap in the corner.")
        self._seg_sem.setChecked(True)

        h.addWidget(_divider())
        fit_btn = QPushButton(_qicon("maximize"), " Fit")
        fit_btn.setToolTip("Fit the GDS overview / minimap to all defects.")
        fit_btn.clicked.connect(self._on_fit_view)
        h.addWidget(fit_btn)

        # Goto GDS coordinate (µm) — same as klayout Ctrl+G, for direct
        # same-coordinate layout comparison. Loads a ~50µm ROI there.
        h.addWidget(QLabel("Goto µm:"))
        self._goto_edit = QLineEdit()
        self._goto_edit.setPlaceholderText("x, y")
        self._goto_edit.setMaximumWidth(120)
        self._goto_edit.setToolTip(
            "Enter a GDS coordinate (x, y µm) to jump there and load nearby "
            "geometry — same coordinate as klayout's Ctrl+G for layout comparison.")
        self._goto_edit.returnPressed.connect(self._on_goto_gds)
        h.addWidget(self._goto_edit)
        goto_btn = QPushButton(_qicon("target"), " Goto")
        goto_btn.setToolTip("Goto the typed GDS coordinate (loads a ~50µm ROI there).")
        goto_btn.clicked.connect(self._on_goto_gds)
        h.addWidget(goto_btn)

        h.addWidget(_divider())

        # ── Export group ──
        h.addWidget(_group("EXPORT"))
        align_btn = QPushButton(_qicon("download"), " Export Alignment…")
        align_btn.setToolTip(
            "Export per-image alignment offsets (coarse + fine + score) to "
            "CSV / JSON for a future Recipe to anchor its ROI (M5).")
        align_btn.clicked.connect(self._on_export_alignment)
        h.addWidget(align_btn)

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
        self.sem_panel.coord_setup.set_origin(self._origin_dx, self._origin_dy)
        if self._current_sem is not None:
            self._jump_to_image(self._current_sem)
        self._fit_view_to_defects()
        self._status_doc.setText(
            f"origin δ nudged to ({self._origin_dx:,.0f}, "
            f"{self._origin_dy:,.0f}) nm")

    _VIEW_MODES = ("sem", "gds", "minimap")

    def _set_view_mode(self, mode: str) -> None:
        """Switch the centre view between SEM-only / GDS overview / minimap
        (M7-ov view-mode selector). Mutually exclusive. Also leaves the batch
        workspace, if active (F7)."""
        if mode not in self._VIEW_MODES:
            return
        # Leaving the batch workspace (clicking any view button exits it).
        self._batch_active = False
        self.batch_panel.setVisible(False)
        self._view_mode = mode
        # GDS overview pane visible only in 'gds'. Splitter widgets are
        # [canvas, batch_panel, sem_viewer]; size all three explicitly.
        self.canvas.setVisible(mode == "gds")
        if mode == "gds":
            total = max(2, self._center_split.width())
            self._center_split.setSizes([total // 2, 0, total - total // 2])
        # Corner minimap visible only in 'minimap'.
        self.minimap.setVisible(mode == "minimap")
        if mode == "minimap":
            self.sem_viewer._reposition_overlay()
            self.minimap.raise_()
        # Reflect in the segmented buttons (block signals to avoid recursion).
        btn = {"sem": self._seg_sem, "gds": self._seg_gds,
               "minimap": self._seg_mini}[mode]
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
        self.minimap.setVisible(False)
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
        """Fit the active overview/minimap to all defects (Fit action)."""
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
            msg = "Step 1 — Open an OASIS: toolbar “Open OASIS…” (scan + pick layers / root cell)."
        elif not self._sem_images:
            msg = "Step 2 — Load SEM: right panel “Load SEM…” (KLARF file or image folder)."
        elif self._fov_w <= 0 or self._fov_h <= 0:
            msg = "Step 3 — Coordinate Setup (right panel): fill the RFL Chip-offset row + FOV width/height."
        elif self._current_sem is None:
            msg = "Step 4 — Click a defect image in the list to jump and load its GDS ROI."
        else:
            msg = ("Step 5 — Left-drag the GDS overlay to align it on the SEM, "
                   "then press Set Offset. Wheel zooms; middle/right-drag pans.")
        self._guidance.setText(msg)
        self._guidance.setVisible(True)

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("&File")
        open_action = QAction("&Open OASIS…", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._on_open_roi)
        menu.addAction(open_action)
        menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu("&Help")
        about_action = QAction("&About", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    # ── actions ────────────────────────────────────────────────────────────

    def _on_cursor(self, x_nm: float, y_nm: float) -> None:
        self._status_cursor.setText(f"cursor: {x_nm:,.0f}, {y_nm:,.0f} nm")

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
            gx += self._fine_dx + self._origin_dx + rdx
            gy += self._fine_dy + self._origin_dy + rdy
            ref = self._refined.get(im.image_id)
            defects.append((im.image_id, gx, gy, ref[2] if ref else None))
        cur = self._current_sem.image_id if self._current_sem else None
        self.canvas.set_defects(defects, cur)
        fov = self._current_image_gds()       # current FOV centre (or None)
        self.minimap.set_data(defects, cur, fov)

    def _on_defect_clicked(self, image_id: str) -> None:
        """Clicking a dot in the overview selects that image (same on-demand
        flow as clicking the list — no extra load cost) (M7-ov #2)."""
        img = next((i for i in self._sem_images
                    if i.image_id == image_id), None)
        if img is not None:
            self._on_sem_image_selected(img)

    # ── M3: SEM load + coordinate jump ──────────────────────────────────────
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
        with_coords = sum(1 for i in images if i.has_coords)
        self._status_doc.setText(
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
        self._status_doc.setText(
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
        v = self.sem_panel.coord_setup.values()
        self.sem_panel.update_coord_badge(v)
        self._chip_corner_x = v["chip_corner_x"]
        self._chip_corner_y = v["chip_corner_y"]
        self._fov_w = v["fov_w"]
        self._fov_h = v["fov_h"]
        self._fine_dx = v["fine_dx"]
        self._fine_dy = v["fine_dy"]
        self._nm_per_px_manual = v["nm_per_px_manual"]
        self._nm_auto = v["nm_auto"]
        # Re-jump so chip-corner / FOV / fine-tune edits move the box live.
        if self._current_sem is not None:
            self._jump_to_image(self._current_sem)
        self._refresh_overview_defects()      # positions shift with the offsets
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
        gx += self._fine_dx + self._origin_dx + rdx
        gy += self._fine_dy + self._origin_dy + rdy
        # Keep the current (overview) zoom and just move the FOV box, so the
        # marker visibly travels across the chip instead of every image being
        # re-centred + re-zoomed (which made it look like nothing moved).
        self.canvas.set_fov_marker(gx, gy, self._fov_w, self._fov_h)
        self._status_cursor.setText(
            f"image {img.image_id} → GDS ({gx/1e3:,.1f}, {gy/1e3:,.1f}) µm "
            f"= ({gx:,.0f}, {gy:,.0f}) nm")
        # Debug (--debug): print the conversion for klayout/diag comparison.
        if oasis_random.DEBUG:
            print(f"[jump] DID={img.image_id}  XREL={img.xrel} YREL={img.yrel}  "
                  f"chip_corner=({self._chip_corner_x:,.0f}, "
                  f"{self._chip_corner_y:,.0f}) nm  "
                  f"fine+origin=({self._fine_dx + self._origin_dx:,.0f}, "
                  f"{self._fine_dy + self._origin_dy:,.0f})  "
                  f"-> gds=({gx/1e3:,.2f}, {gy/1e3:,.2f}) µm", flush=True)
        self._update_overlay()
        self._maybe_collapse_coord_setup()

    def _maybe_collapse_coord_setup(self) -> None:
        """Auto-collapse the one-time Coordinate Setup once it's been used with
        a valid setup — but only once, so re-expanding it sticks (M7.1)."""
        if getattr(self, "_coord_collapsed_once", False):
            return
        if self._fov_w > 0 and self._fov_h > 0:
            self._coord_collapsed_once = True
            self.sem_panel.set_coord_collapsed(True)

    def _current_image_gds(self):
        """GDS (x, y) of the selected image incl. fine-tune + refined, or None."""
        img = self._current_sem
        if img is None or not img.has_coords:
            return None
        gx, gy = gds_fov.klarf_to_gds(
            img.xrel, img.yrel, self._chip_corner_x, self._chip_corner_y)
        rdx, rdy = self._refined_offset(img)
        return (gx + self._fine_dx + self._origin_dx + rdx,
                gy + self._fine_dy + self._origin_dy + rdy)

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
            xs.append(gx + self._fine_dx + self._origin_dx)
            ys.append(gy + self._fine_dy + self._origin_dy)
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
        self.sem_panel.coord_setup.set_origin(
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
        self.sem_panel.coord_setup.set_origin(self._origin_dx, self._origin_dy)
        if self._current_sem is not None:
            self._jump_to_image(self._current_sem)
        self._fit_view_to_defects()
        self._status_doc.setText(
            f"origin δ set to ({self._origin_dx:,.0f}, {self._origin_dy:,.0f}) nm")

    def _on_clear_offset(self) -> None:
        self._origin_dx = 0.0
        self._origin_dy = 0.0
        self.sem_viewer.reset_drag()
        self.sem_panel.coord_setup.set_origin(0.0, 0.0)
        if self._current_sem is not None:
            self._jump_to_image(self._current_sem)
        self._fit_view_to_defects()
        self._status_doc.setText("origin δ cleared")

    # ── F3: multi-POI template + auto fine alignment ─────────────────────────
    def _on_pois_changed(self, entries) -> None:
        self._poi_entries = list(entries or [])
        self.sem_panel.fine_align.set_pois(
            [(e.key.key(), _entry_label(e)) for e in self._poi_entries])
        if self._poi_entries:          # expand Fine Align when a POI is picked
            self.sem_panel.set_fine_collapsed(False)

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
        """Coarse FOV-centre GDS anchor for ``img`` (klarf→gds + fine + δ),
        excluding any refined offset so the search starts from coarse."""
        gx, gy = gds_fov.klarf_to_gds(
            img.xrel, img.yrel, self._chip_corner_x, self._chip_corner_y)
        return (gx + self._fine_dx + self._origin_dx,
                gy + self._fine_dy + self._origin_dy)

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
        self._batch_refresh_timer.stop()     # clear any pending refresh (F8)
        self._enter_batch_workspace()
        self._refresh_batch_panel()
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

    def _on_fa_failed(self, msg: str) -> None:
        self._batch_refresh_timer.stop()
        self.batch_panel.end_progress()
        self._refresh_batch_panel()           # show whatever finished
        QMessageBox.critical(self, "Run all failed", msg)

    def _on_fa_cancelled(self) -> None:
        # Partial results are kept (not cleared), so the overview still reflects
        # whatever finished before the cancel (F5 M5).
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
        self.sem_panel.coord_setup.set_origin(self._origin_dx, self._origin_dy)
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
        return (gx + self._fine_dx + self._origin_dx,
                gy + self._fine_dy + self._origin_dy)

    # ── M5: per-image alignment export ───────────────────────────────────────
    def _on_export_alignment(self) -> None:
        if not self._sem_images:
            self._status_doc.setText("export: load SEM images first")
            return
        dlg = AlignmentExportDialog(self, self._sem_images)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        fmt, ids, exp_raw, exp_overlay = dlg.selected()
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
        self._status_doc.setText(
            f"exported {len(rows)} image(s) → {Path(path).name}")
        if exp_raw or exp_overlay:
            self._export_overlay_images(images, exp_raw, exp_overlay)

    # ── F5 M6: SEM + aligned-overlay PNG export ──────────────────────────────
    def _poi_specs_colored(self) -> list:
        """``[(spec, (r, g, b)), ...]`` for the active POI layers, for overlay
        export — the spec walks the ROI, the colour strokes the outline."""
        out = []
        for e in self._poi_entries:
            spec = self._entry_spec(e)
            if spec is not None:
                out.append((spec, (e.color.red(), e.color.green(),
                                   e.color.blue())))
        return out

    def _export_overlay_images(self, images, exp_raw: bool,
                               exp_overlay: bool) -> None:
        if cv2 is None:
            QMessageBox.warning(self, "Image export",
                                "opencv (cv2) is required for image export.")
            return
        if self._ov_thread is not None:
            return
        specs = self._poi_specs_colored()
        if exp_overlay and (not specs or self._rar is None
                            or self._roi_root is None):
            QMessageBox.information(
                self, "Overlay export",
                "Overlay PNGs need an OASIS (ROI) open and ≥1 POI layer; "
                "exporting raw images / manifest only.")
            exp_overlay = False
        out_dir = QFileDialog.getExistingDirectory(self, "Export images to…")
        if not out_dir:
            return
        jobs = [(im.image_id, self._coarse_gds(im),
                 self._refined.get(im.image_id),
                 str(im.file_path) if im.file_path else "", bool(im.exists))
                for im in images]
        cfg = {"fov_w": self._fov_w, "fov_h": self._fov_h,
               "nm_auto": self._nm_auto, "nm_manual": self._nm_per_px_manual}
        self._ov_progress = LoadProgressDialog(self)
        self._ov_progress.set_text("Exporting images…")
        self._ov_thread = QThread(self)
        self._ov_worker = OverlayExportWorker(
            self._rar, self._roi_root, specs, jobs, cfg, out_dir,
            exp_raw, exp_overlay)
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

        Runs in a background thread: the first load decodes every reachable
        cell once to learn its size (the OASIS has no per-cell bbox), which
        can take a while on a big chip — so the UI stays responsive with a
        cancellable progress dialog. Subsequent loads reuse the cache."""
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
        self._roi_progress.set_text(
            "Loading GDS ROI…\nScanning cell sizes (first load is slowest; "
            "later loads reuse the cache). Cancellable.")
        self._roi_thread = QThread(self)
        self._roi_worker = RoiWalkWorker(
            self._rar, self._roi_root, self._roi_layers, roi)
        self._roi_worker.moveToThread(self._roi_thread)
        self._roi_thread.started.connect(self._roi_worker.run)
        self._roi_worker.finished.connect(self._on_roi_finished)
        self._roi_worker.failed.connect(self._on_roi_failed)
        self._roi_worker.cancelled.connect(self._on_roi_cancelled)
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

    def _tick_roi_progress(self) -> None:
        if self._roi_progress is not None and self._rar is not None:
            done = self._rar._n_loaded
            total = len(self._rar._by_refnum) or 1
            pct = min(100, int(100 * done / total))
            self._roi_progress.set_text(
                f"Loading GDS ROI…\n{done:,} / ≤{total:,} cells scanned "
                f"({pct}%)\nFirst load is slowest; later loads reuse the "
                f"cache. Cancellable.")

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
            print(f"[roi] geometry bbox = ({bb[0]/1e3:,.1f}, {bb[1]/1e3:,.1f}) .. "
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
                print(f"[roi] LARGEST {ly}/{dt} rect: "
                      f"({x0/1e3:,.3f}, {y0/1e3:,.3f}) .. "
                      f"({x1/1e3:,.3f}, {y1/1e3:,.3f}) µm "
                      f"(W×H {(x1-x0)/1e3:,.3f}×{(y1-y0)/1e3:,.3f} µm) "
                      f"-> goto centre ({(x0+x1)/2e3:,.3f}, {(y0+y1)/2e3:,.3f}) µm",
                      flush=True)

    def _on_roi_failed(self, msg: str) -> None:
        QMessageBox.critical(self, "ROI load failed", msg)

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
        path, _ = QFileDialog.getOpenFileName(
            self, "Open OASIS", "",
            "OASIS files (*.oas *.oasis);;All files (*)")
        if not path:
            return
        # Scan the file for available layers and let the user multi-select
        # (same flow the old full-load entry used), instead of typing
        # layer/datatype pairs by hand.
        size_mb = Path(path).stat().st_size / 1024 / 1024
        dlg = LayerFilterDialog(self, size_mb, Path(path).name, path,
                                roi_mode=True)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        layer_keys = dlg.filter_pairs()
        if not layer_keys:
            QMessageBox.warning(self, "ROI layers",
                                "Pick (or type) at least one layer to load.")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            rar = oasis_random.RandomAccessReader(
                path, wanted_layers=set(layer_keys),
                bbox_layer=oasis_random.DEFAULT_BBOX_LAYER)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "ROI open failed", str(exc))
            return
        QApplication.restoreOverrideCursor()
        if not rar.has_offsets():
            QMessageBox.warning(
                self, "ROI mode unavailable",
                "This OASIS has no S_CELL_OFFSET index, so random-access "
                "ROI load isn't possible. Re-export the layout with "
                "per-cell offsets, or load a smaller file.")
            return
        # Pick the root cell from the cellname list (default: a name that
        # looks like a top cell, else the last-defined name).
        names = list(rar._by_name.keys())
        default = next((n for n in names
                        if "top" in n.lower() or "merge" in n.lower()),
                       names[-1] if names else "")
        root, ok = QInputDialog.getItem(
            self, "ROI root cell", "Root (top) cell:", names,
            names.index(default) if default in names else 0, False)
        if not ok or not root:
            return
        self._rar = rar
        self._oas_path = path
        self._roi_root = root
        self._roi_layers = layer_keys
        # New layout → drop any recipes from a previously-open file (recipes
        # persist across ROI reloads of the SAME file, not across files).
        self._recipes = []
        lyr_txt = ", ".join(f"L{l}/D{d}" for l, d in layer_keys)
        self._status_doc.setText(
            f"ROI mode: {Path(path).name} · root '{root}' · {lyr_txt}"
            f" · {len(rar._by_refnum):,} cells indexed · click a SEM image")
        # Frame all defect positions so the marker has a visible span to
        # jump across; then jump to the current image (auto-loads its ROI).
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
        self._status_doc.setText(f"deleted expression layer '{name}'")

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
                top_cell_name=self._doc.top_cell_name)
            gds_layer_cache.cache_save(path, layers, meta)
            self._save_expr_sidecar(path)
        except Exception as exc:
            QMessageBox.critical(self, "Cache export failed", str(exc))
            return
        self._status_doc.setText(f"cache exported · {Path(path).name}")

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

        # Restore the RFL params + FOV into the Coordinate Setup panel; its
        # ``changed`` signal mirrors them back into self._chip_corner_* etc.
        self.sem_panel.coord_setup.set_from_meta(data.meta)
        # Restore the origin δ (M4a).
        self._origin_dx = float(getattr(data.meta, "origin_dx", 0.0))
        self._origin_dy = float(getattr(data.meta, "origin_dy", 0.0))
        self.sem_panel.coord_setup.set_origin(self._origin_dx, self._origin_dy)

        self._doc = doc
        self._load_path = path
        self.layer_panel.set_document(doc)
        self.canvas.set_document(doc)
        self.setWindowTitle(f"GDS Align Tool — {Path(path).name} (cache)")
        self._status_doc.setText(
            f"{Path(path).name} (cache)  ·  {doc.summary()}")
        self._restore_expr_sidecar(path)
        # Settings came from the cache — collapse the one-time setup section.
        self._coord_collapsed_once = True
        self.sem_panel.set_coord_collapsed(True)

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

        ver_lbl = QLabel("Version 1.0.0", dlg)
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
    if "--debug" in sys.argv:
        oasis_random.set_debug(True)
        sys.argv = [a for a in sys.argv if a != "--debug"]
        print("[gds-align] debug mode ON (ROI walk tracing -> stderr)",
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
