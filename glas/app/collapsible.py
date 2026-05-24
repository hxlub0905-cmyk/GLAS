"""Reusable collapsible section widget for settings panels.

Usage:
    sec = CollapsibleSection("Filters", tier=2, collapsed=False)
    form = QFormLayout()
    form.addRow("Min area", spin)
    wrap = QWidget(); wrap.setLayout(form)
    sec.add_widget(wrap)
    parent_layout.addWidget(sec)

    # Optional trailing widget in the header (e.g. a delete button):
    btn = QPushButton("×")
    sec = CollapsibleSection("Measure 1", tier=2, trailing_widget=btn)
"""
from __future__ import annotations

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QSizePolicy

# ── Tier visual tokens (kept here so they travel with the widget) ─────────────
_TIER_BG     = {1: "#fff4e8", 2: "#f9f4ee", 3: "#f4f0ec"}
_TIER_BORDER = {1: "#efd8b8", 2: "#ede4d8", 3: "#e8e0d8"}

# Inline QSS applied to the toggle button when a trailing_widget is present.
# Uses type-selector pseudo-states (safe here: QPushButton has no child buttons).
# The base background is transparent so the container's background colour shows.
_TIER_HOVER_QSS = {
    1: ("QPushButton{background:transparent;border:none;text-align:left;}"
        "QPushButton:hover{background:#ffede0;}"
        "QPushButton:pressed{background:#ffe4cc;}"),
    2: ("QPushButton{background:transparent;border:none;text-align:left;}"
        "QPushButton:hover{background:#f5ede4;}"
        "QPushButton:pressed{background:#f0e4d8;}"),
    3: ("QPushButton{background:transparent;border:none;text-align:left;}"
        "QPushButton:hover{background:#eee8e0;}"
        "QPushButton:pressed{background:#e8e0d8;}"),
}


class CollapsibleSection(QWidget):
    """A titled section whose content can be collapsed/expanded.

    Visual tiers:
      1 = accent header  (top-level section, orange)
      2 = standard header  (parameter group, muted orange)
      3 = subtle header  (advanced / rarely-changed, grey)

    Pass ``trailing_widget`` to embed a widget (e.g. a delete button) on the
    right side of the section header without breaking the collapse behaviour.
    """

    def __init__(
        self,
        title: str,
        tier: int = 1,
        collapsed: bool = False,
        content_margins: tuple[int, int, int, int] = (8, 4, 8, 6),
        trailing_widget: "QWidget | None" = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._title = title
        self._collapsed = collapsed
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── toggle button ────────────────────────────────────────────────────
        self._btn = QPushButton()
        self._btn.setObjectName(f"sectionHeader{tier}")
        self._btn.setFlat(True)
        self._btn.setCheckable(False)
        self._btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._btn.setFixedHeight(24)
        self._btn.clicked.connect(self._toggle)
        self._update_label()

        if trailing_widget is not None:
            # Header: container (full-width background + borders) wrapping the
            # toggle button and the trailing widget side by side.
            container = QWidget()
            bg  = _TIER_BG.get(tier, "#f9f4ee")
            bdr = _TIER_BORDER.get(tier, "#ede4d8")
            container.setStyleSheet(
                f"background:{bg};"
                f"border-top:1px solid {bdr};"
                f"border-bottom:1px solid {bdr};"
            )
            chl = QHBoxLayout(container)
            chl.setContentsMargins(0, 0, 4, 0)
            chl.setSpacing(0)
            self._btn.setStyleSheet(_TIER_HOVER_QSS.get(tier, _TIER_HOVER_QSS[2]))
            chl.addWidget(self._btn, stretch=1)
            chl.addWidget(trailing_widget)
            outer.addWidget(container)
        else:
            # Original path: just the toggle button as the header.
            self._btn.setStyleSheet("text-align: left;")
            outer.addWidget(self._btn)

        # ── content body ─────────────────────────────────────────────────────
        self._body = QWidget()
        self._body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(*content_margins)
        self._body_layout.setSpacing(5)
        outer.addWidget(self._body)
        self._body.setVisible(not collapsed)

    # ── public API ────────────────────────────────────────────────────────────

    @property
    def content_layout(self) -> QVBoxLayout:
        return self._body_layout

    def add_widget(self, w: QWidget) -> None:
        self._body_layout.addWidget(w)

    def set_collapsed(self, val: bool) -> None:
        self._collapsed = val
        self._body.setVisible(not val)
        self._update_label()

    def is_collapsed(self) -> bool:
        return self._collapsed

    # ── private ───────────────────────────────────────────────────────────────

    def _toggle(self) -> None:
        self.set_collapsed(not self._collapsed)

    def _update_label(self) -> None:
        arrow = "▸" if self._collapsed else "▾"
        self._btn.setText(f"  {arrow}  {self._title}")
