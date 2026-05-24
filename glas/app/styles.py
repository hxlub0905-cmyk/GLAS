"""Application-wide soft light theme QSS stylesheet and colour constants."""

# ── Design Tokens §1 ───────────────────────────────────────────────────────────

# Background layers
BG_PAGE      = "#f7f4ef"
BG_PANEL     = "#faf7f3"
BG_SURFACE   = "#fff8f2"
BG_ELEVATED  = "#fff4e8"
BG_INPUT     = "#ffffff"

# Borders
BORDER_DEFAULT = "#e8d8c8"
BORDER_INPUT   = "#c8b8a8"
BORDER_HOVER   = "#8a7060"
BORDER_FOCUS   = "#f29f4b"

# Text — TEXT_HINT was #b0a090, which fell short of WCAG AA on the
# cream BG_PAGE (~2.4:1). Bumped to #8a7660 (~5:1) for hint labels;
# disabled-state QSS rules keep the original #b0a090 since that
# convention is *meant* to read "inactive".
TEXT_PRIMARY   = "#3f3428"
TEXT_SECONDARY = "#7a6a5a"
TEXT_MUTED     = "#7a6a5a"
TEXT_HINT      = "#8a7660"

# Accent orange
ACCENT         = "#f29f4b"
ACCENT_HOVER   = "#f6b56b"
ACCENT_ACTIVE  = "#d97d1e"
ACCENT_BG      = "#fff4e6"
ACCENT_BORDER  = "#efd8b8"

# Semantic colours
SUCCESS        = "#7abf9a"
SUCCESS_BG     = "#ebf7f0"
SUCCESS_BORDER = "#9ec9ad"
SUCCESS_TEXT   = "#3e7f5d"
DANGER         = "#cc7b6c"
DANGER_BG      = "#feeee8"

# MIN / MAX annotation colours
MIN_COLOR  = "#d8894f"
MIN_BG     = "#fff8f0"
MIN_BORDER = "#f0c8a8"
MIN_TEXT   = "#9a5a2a"
MAX_COLOR  = "#6ea8cf"
MAX_BG     = "#f0f7fc"
MAX_BORDER = "#a8c8e0"
MAX_TEXT   = "#3a6a8a"

# Sizing
RADIUS_SM = "5px"
RADIUS_MD = "7px"
RADIUS_LG = "10px"
INPUT_H   = "30px"
BTN_H_SM  = "28px"
BTN_H_MD  = "32px"
BTN_H_LG  = "36px"

# ── Typography scale — fixed sizes; pick one of these in QSS ──────────────────
# (All fonts share the same family declared in QSS '*' selector)
FS_DISPLAY = "16px"   # Workspace big title
FS_TITLE   = "14px"   # Section header / Dialog title
FS_BODY    = "13px"   # Default body / table / button label
FS_LABEL   = "12px"   # Form label / secondary text
FS_CAPTION = "11px"   # Hint / placeholder / status bar
FS_MICRO   = "10px"   # SECTION HEADER uppercase tags

# ── Spacing scale (4 px grid) ─────────────────────────────────────────────────
SP_1 = 4
SP_2 = 8
SP_3 = 12
SP_4 = 16
SP_5 = 24
SP_6 = 32

# ── Backward-compat aliases (used by annotator / legacy code) ─────────────────
BG_BASE       = BG_PAGE
BG_SURFACE_OLD = BG_SURFACE
BORDER        = BORDER_DEFAULT
BORDER_LIGHT  = BORDER_INPUT
TEXT_SECONDARY_V1 = TEXT_SECONDARY
TEXT_MUTED_V1    = TEXT_MUTED
WARNING       = "#d9a24f"
MIN_COLOUR    = MIN_COLOR
MAX_COLOUR    = MAX_COLOR
NORM_COLOUR   = "#8ccaa6"

STYLE = """
/* ════════════════════════ Base ══════════════════════════════════════════ */
* {
    outline: 0;
}
QWidget {
    background-color: #f7f4ef;
    color: #3f3428;
    font-family: 'Segoe UI', 'PingFang TC', 'Microsoft JhengHei',
                 'Helvetica Neue', Arial, sans-serif;
    font-size: 13px;          /* FS_BODY — single source of truth */
    border: none;
}
QToolTip {
    background: #3f3428;
    color: #faf7f3;
    border: 1px solid #2c2418;
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 12px;
}
QMainWindow::separator { background: #e8d8c8; width: 1px; height: 1px; }

/* ════════════════════════ Panels / Frames ════════════════════════════════ */
QFrame#leftPanel {
    background: #fff7ee;
    border-right: 1px solid #e8d8c8;
}
QFrame#rightPanel {
    background: #fff7ee;
    border-left: 1px solid #e8d8c8;
}
QFrame#viewerHeader {
    background: #fff9f2;
    border-bottom: 1px solid #e8d8c8;
    min-height: 38px;
    max-height: 38px;
}
QFrame#resultsHeader {
    background: #fff9f2;
    border-top: 1px solid #e8d8c8;
    border-bottom: 1px solid #e8d8c8;
    min-height: 30px;
    max-height: 30px;
}
QGroupBox {
    border: 0.5px solid #e8d8c8;
    border-radius: 8px;
    margin-top: 18px;
    padding: 12px 10px 10px 10px;
    background: #fff8f2;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: #e8963a;
    font-weight: 700;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.8px;
}

/* ════════════════════════ Splitter ══════════════════════════════════════ */
QSplitter::handle { background: #e8d8c8; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical   { height: 1px; }

/* ════════════════════════ Labels ════════════════════════════════════════ */
QLabel { color: #7c6d5b; background: transparent; }
QLabel#panelTitle {
    color: #f29f4b;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    padding: 8px 12px 4px 12px;
}

/* ════════════════════════ Stat chips ════════════════════════════════════ */
QLabel#statChip {
    background: #fff4e6;
    border: 0.5px solid #efd8b8;
    border-radius: 8px;
    padding: 3px 10px;
    color: #8a6830;
    font-size: 11px;
    font-weight: 500;
}
QLabel#statChipMin {
    background: #fff8f0;
    border: 0.5px solid #f0c8a8;
    border-radius: 8px;
    padding: 3px 10px;
    color: #9a5a2a;
    font-size: 11px;
    font-weight: 500;
}
QLabel#statChipMax {
    background: #f0f7fc;
    border: 0.5px solid #a8c8e0;
    border-radius: 8px;
    padding: 3px 10px;
    color: #3a6a8a;
    font-size: 11px;
    font-weight: 500;
}
QLabel#statChipAlert {
    background: #ffeee8;
    border: 0.5px solid #f0c0a8;
    border-radius: 8px;
    padding: 3px 10px;
    color: #a04030;
    font-size: 11px;
    font-weight: 500;
}

/* ════════════════════════ Buttons — default (= secondary variant) ═══════
 *  All buttons share fixed dimensions so they line up across the app.
 *  Variants are selected via setObjectName(...) or [variant="..."] property. */
QPushButton {
    background: #fffdf9;
    color: #6f6254;
    border: 1px solid #c8b49e;
    border-radius: 6px;
    padding: 0 16px;
    font-size: 13px;
    font-weight: 500;
    min-height: 32px;
}
QPushButton:hover    { background: #fff4e8; border-color: #b09e86; color: #3f3428; }
QPushButton:pressed  { background: #f0e0cb; }
QPushButton:disabled { color: #c8b89e; border-color: #dfd0be; background: #faf6f0; }

/* Primary orange — for the single most important action on a page */
QPushButton#primaryBtn,
QPushButton[variant="primary"] {
    background: #f29f4b;
    color: #ffffff;
    border: 1px solid #d97d1e;
    border-radius: 6px;
    padding: 0 18px;
    font-size: 13px;
    font-weight: 600;
    min-height: 32px;
}
QPushButton#primaryBtn:hover,
QPushButton[variant="primary"]:hover    { background: #f6b56b; border-color: #d97d1e; }
QPushButton#primaryBtn:pressed,
QPushButton[variant="primary"]:pressed  { background: #d97d1e; }
QPushButton#primaryBtn:disabled,
QPushButton[variant="primary"]:disabled { background: #f0d8b8; border-color: #e0c4a0; color: #ffffff; }

/* Secondary — outline style (now also the default style above) */
QPushButton#secondaryBtn,
QPushButton[variant="secondary"] {
    background: #ffffff;
    color: #d97d1e;
    border: 1px solid #f29f4b;
    border-radius: 6px;
    padding: 0 16px;
    font-size: 13px;
    font-weight: 500;
    min-height: 32px;
}
QPushButton#secondaryBtn:hover,
QPushButton[variant="secondary"]:hover    { background: #fff4e6; border-color: #d97d1e; color: #b06614; }
QPushButton#secondaryBtn:pressed,
QPushButton[variant="secondary"]:pressed  { background: #fee5cc; }
QPushButton#secondaryBtn:disabled,
QPushButton[variant="secondary"]:disabled { background: #faf6f0; border-color: #e8d8c8; color: #c8b89e; }

/* Ghost — minimal chrome for tertiary actions */
QPushButton#ghostBtn,
QPushButton[variant="ghost"] {
    background: transparent;
    color: #7a6a5a;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 0 12px;
    font-size: 13px;
    font-weight: 500;
    min-height: 32px;
}
QPushButton#ghostBtn:hover,
QPushButton[variant="ghost"]:hover    { background: #f4ede4; color: #3f3428; }
QPushButton#ghostBtn:pressed,
QPushButton[variant="ghost"]:pressed  { background: #ebe1d4; }
QPushButton#ghostBtn:disabled,
QPushButton[variant="ghost"]:disabled { color: #c8b89e; }

/* Success green — for "execute / run" actions (shares geometry with primary) */
QPushButton#successBtn,
QPushButton#runSingle,
QPushButton[variant="success"] {
    background: #ebf7f0;
    border: 1px solid #9ec9ad;
    color: #3e7f5d;
    border-radius: 6px;
    padding: 0 18px;
    font-size: 13px;
    font-weight: 600;
    min-height: 32px;
}
QPushButton#successBtn:hover,
QPushButton#runSingle:hover,
QPushButton[variant="success"]:hover    { background: #ddf1e5; border-color: #88b898; color: #376f54; }
QPushButton#successBtn:pressed,
QPushButton#runSingle:pressed,
QPushButton[variant="success"]:pressed  { background: #d2eadc; }
QPushButton#successBtn:disabled,
QPushButton#runSingle:disabled,
QPushButton[variant="success"]:disabled { background: #f1efe9; border-color: #d8d0c4; color: #b8b0a4; }

/* runBatch — kept for backward compat, dimensions matched */
QPushButton#runBatch {
    background: #fff1e4;
    border: 1px solid #efb67f;
    color: #9a5a22;
    border-radius: 6px;
    padding: 0 18px;
    font-size: 13px;
    font-weight: 600;
    min-height: 32px;
}
QPushButton#runBatch:hover   { background: #ffe8d3; border-color: #eea55b; color: #8f4f1f; }
QPushButton#runBatch:pressed { background: #ffe0c0; }

/* Segmented view-mode buttons */
QPushButton#segLeft {
    border-top-left-radius: 12px;
    border-bottom-left-radius: 12px;
    border-top-right-radius: 0;
    border-bottom-right-radius: 0;
    border-right: none;
    padding: 4px 14px;
    font-size: 12px;
}
QPushButton#segMid {
    border-radius: 0;
    border-right: none;
    padding: 4px 14px;
    font-size: 12px;
}
QPushButton#segRight {
    border-top-right-radius: 12px;
    border-bottom-right-radius: 12px;
    border-top-left-radius: 0;
    border-bottom-left-radius: 0;
    padding: 4px 14px;
    font-size: 12px;
}
QPushButton#segLeft:checked,
QPushButton#segMid:checked,
QPushButton#segRight:checked {
    background: #f6b56b;
    border-color: #f29f4b;
    color: #ffffff;
}

/* Detail CD toggle */
QPushButton#detailCD {
    background: #fffdf9;
    color: #6f6254;
    border: 1px solid #dfd0be;
    border-radius: 6px;
    padding: 4px 12px;
    font-size: 12px;
}
QPushButton#detailCD:hover   { background: #f0faf5; border-color: #88c4a8; color: #3a6650; }
QPushButton#detailCD:checked { background: #d0f0e0; border-color: #5ab080; color: #2a5540; font-weight: 700; }
QPushButton#detailCD:pressed { background: #c0e8d0; }

/* Profile delete button */
QPushButton#profileDeleteBtn {
    background: transparent;
    border: none;
    color: #c8b8a8;
    font-size: 14px;
    font-weight: 700;
    padding: 0;
    border-radius: 3px;
    min-height: 0;
}
QPushButton#profileDeleteBtn:hover  { background: #f4d0c8; color: #b04030; border: 1px solid #efb6a0; }
QPushButton#profileDeleteBtn:pressed { background: #ead0c8; color: #902820; }

/* ════════════════════════ CollapsibleSection headers ═══════════════════ */
QPushButton#sectionHeader1 {
    background: #fff4e8;
    color: #c97028;
    border: none;
    border-top: 1px solid #efd8b8;
    border-bottom: 1px solid #efd8b8;
    border-radius: 0;
    padding: 0 10px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.8px;
    text-transform: uppercase;
    text-align: left;
}
QPushButton#sectionHeader1:hover  { background: #ffede0; color: #b05c20; }
QPushButton#sectionHeader1:pressed { background: #ffe4cc; }

QPushButton#sectionHeader2 {
    background: #f9f4ee;
    color: #9a7050;
    border: none;
    border-top: 0.5px solid #ede4d8;
    border-bottom: 0.5px solid #ede4d8;
    border-radius: 0;
    padding: 0 10px;
    font-size: 10px;
    font-weight: 600;
    text-align: left;
}
QPushButton#sectionHeader2:hover  { background: #f5ede4; color: #7a5030; }
QPushButton#sectionHeader2:pressed { background: #f0e4d8; }

QPushButton#sectionHeader3 {
    background: #f4f0ec;
    color: #9f8f7b;
    border: none;
    border-top: 0.5px solid #e8e0d8;
    border-bottom: 0.5px solid #e8e0d8;
    border-radius: 0;
    padding: 0 10px;
    font-size: 10px;
    font-weight: 500;
    text-align: left;
}
QPushButton#sectionHeader3:hover  { background: #eee8e0; color: #7a6858; }
QPushButton#sectionHeader3:pressed { background: #e8e0d8; }

/* ════════════════════════ Toolbar ══════════════════════════════════════ */
QToolBar {
    background: #f2ece4;
    border-bottom: 1px solid #e8d8c8;
    spacing: 2px;
    padding: 3px 6px;
}
QToolButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 5px;
    padding: 5px 12px;
    color: #7c6d5b;
    font-size: 12px;
}
QToolButton:hover  { background: #fffdf9; border-color: #e8d8c8; color: #3f3428; }
QToolButton:pressed { background: #f4e8da; }

/* ════════════════════════ Slider ════════════════════════════════════════ */
QSlider::groove:horizontal {
    height: 3px;
    background: #e8d8c8;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #f29f4b;
    border: 2px solid #e6953d;
    width: 14px; height: 14px;
    border-radius: 7px;
    margin: -6px 0;
}
QSlider::handle:horizontal:hover { background: #f6b56b; }
QSlider::sub-page:horizontal {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #e6953d, stop:1 #f29f4b);
    border-radius: 2px;
}

/* ════════════════════════ SpinBox ══════════════════════════════════════ */
QSpinBox, QDoubleSpinBox {
    background: #ffffff;
    border: 1.5px solid #c8b8a8;
    border-radius: 5px;
    padding: 2px 8px;
    color: #3f3428;
    font-size: 13px;
    min-height: 30px;
    selection-background-color: #f6b56b;
}
QSpinBox:hover, QDoubleSpinBox:hover { border-color: #8a7060; }
QSpinBox:focus, QDoubleSpinBox:focus { border-color: #f29f4b; background: #fffef9; }
QSpinBox:disabled, QDoubleSpinBox:disabled { background: #f5f0ea; color: #b0a090; }
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    background: #fff4e8;
    border: none;
    width: 18px;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
    background: #efddc8;
}

/* ════════════════════════ CheckBox ══════════════════════════════════════ */
QCheckBox { spacing: 7px; color: #7a6a5a; }
QCheckBox::indicator {
    width: 14px; height: 14px;
    border: 1.5px solid #c0ad96;
    border-radius: 3px;
    background: #ffffff;
}
QCheckBox::indicator:checked { background: #e6953d; border-color: #f29f4b; image: none; }

/* ════════════════════════ Tree ══════════════════════════════════════════ */
QTreeWidget {
    background: #f2ece4;
    alternate-background-color: #faf5ee;
    border: none;
    show-decoration-selected: 1;
}
QTreeWidget::item { padding: 3px 2px; border-radius: 3px; }
QTreeWidget::item:selected { background: #f6c38c; color: #3f3428; }
QTreeWidget::item:hover:!selected { background: #f6efe6; }
QTreeWidget QHeaderView::section {
    background: #fff7ee;
    border: none;
    border-bottom: 1px solid #e8d8c8;
    padding: 5px 8px;
    color: #9f8f7b;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

/* ════════════════════════ ListWidget ════════════════════════════════════ */
QListWidget {
    background: #f2ece4;
    alternate-background-color: #faf5ee;
    border: none;
}
QListWidget::item { padding: 3px 2px; border-radius: 3px; }
QListWidget::item:selected { background: #f6c38c; color: #3f3428; }
QListWidget::item:hover:!selected { background: #f6efe6; }

/* ════════════════════════ Table ════════════════════════════════════════ */
QTableWidget {
    background: #f2ece4;
    gridline-color: #eee4d8;
    alternate-background-color: #faf4ec;
    border: none;
    selection-background-color: #f6c38c;
}
QTableWidget::item { padding: 4px 8px; }
QTableWidget::item:selected { color: #3f3428; }
QTableWidget QHeaderView::section {
    background: #fff9f2;
    border: none;
    border-bottom: 1px solid #e8d8c8;
    border-right: 1px solid #eadfce;
    padding: 6px 8px;
    color: #f29f4b;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.3px;
}
QTableCornerButton::section { background: #fff9f2; }

/* ════════════════════════ ScrollBar ════════════════════════════════════ */
QScrollBar:vertical {
    background: #faf5ee;
    width: 11px;
    margin: 0;
    border-radius: 5px;
}
QScrollBar::handle:vertical {
    background: #d8c8b6;
    border-radius: 5px;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover  { background: #b8a898; }
QScrollBar::handle:vertical:pressed{ background: #9a8878; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }

QScrollBar:horizontal {
    background: #faf5ee;
    height: 11px;
    margin: 0;
    border-radius: 5px;
}
QScrollBar::handle:horizontal {
    background: #d8c8b6;
    border-radius: 5px;
    min-width: 28px;
}
QScrollBar::handle:horizontal:hover  { background: #b8a898; }
QScrollBar::handle:horizontal:pressed{ background: #9a8878; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }

/* ════════════════════════ TabWidget / TabBar ════════════════════════════ */
QTabWidget::pane {
    border: 1px solid #e8d8c8;
    border-top: none;
    background: #f7f4ef;
}
QTabWidget::tab-bar { alignment: left; }
QTabBar { background: transparent; }
QTabBar::tab {
    background: #efe8de;
    color: #9a8a7a;
    border: 0.5px solid #e8d8c8;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 6px 18px 5px 18px;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background: #f7f4ef;
    color: #e8963a;
    font-weight: 700;
    border-top: 2px solid #f29f4b;
    border-bottom: none;
}
QTabBar::tab:hover:!selected {
    background: #f6efe6;
    color: #5a4d3e;
}

/* ════════════════════════ LineEdit ══════════════════════════════════════ */
QLineEdit {
    background: #ffffff;
    border: 1.5px solid #c8b8a8;
    border-radius: 5px;
    padding: 2px 8px;
    color: #3f3428;
    font-size: 13px;
    min-height: 30px;
    selection-background-color: #f6b56b;
}
QLineEdit:hover  { border-color: #8a7060; }
QLineEdit:focus  { border-color: #f29f4b; background: #fffef9; }
QLineEdit:disabled { background: #f5f0ea; color: #b0a090; }
QLineEdit:read-only { background: #f5f0ea; color: #8a7a6a; }

/* ════════════════════════ ComboBox ══════════════════════════════════════ */
QComboBox {
    background: #ffffff;
    border: 1.5px solid #c8b8a8;
    border-radius: 5px;
    padding: 2px 28px 2px 10px;
    color: #3f3428;
    font-size: 13px;
    min-height: 30px;
}
QComboBox:hover  { border-color: #8a7060; }
QComboBox:focus  { border-color: #f29f4b; }
QComboBox:disabled { background: #f5f0ea; color: #b0a090; }
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: right center;
    border: none;
    width: 22px;
    background: transparent;
}
QComboBox::down-arrow {
    width: 0; height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #9f8f7b;
}
/* Popup view — also targeted as QListView since the actual popup is a
   top-level QListView (Qt6/Linux does not always honour QAbstractItemView
   when the popup is reparented out of the rightPanel). */
QComboBox QAbstractItemView,
QComboBox QListView {
    background: #fffdf9;
    border: 1px solid #c8b49e;
    border-radius: 5px;
    selection-background-color: #f6c38c;
    selection-color: #3f3428;
    color: #3f3428;
    padding: 2px;
    outline: 0;
}
QComboBox QAbstractItemView::item,
QComboBox QListView::item {
    background: #fffdf9;
    color: #3f3428;
    padding: 6px 12px;
    min-height: 22px;
    border: none;
}
QComboBox QAbstractItemView::item:hover,
QComboBox QListView::item:hover {
    background: #fff4e6;
    color: #3f3428;
}
QComboBox QAbstractItemView::item:selected,
QComboBox QListView::item:selected {
    background: #f6c38c;
    color: #3f3428;
}

/* ════════════════════════ Dialog ════════════════════════════════════════ */
QDialog { background: #f7f4ef; }
QDialogButtonBox QPushButton { min-width: 80px; }

/* ════════════════════════ MenuBar / Menu ════════════════════════════════ */
QMenuBar {
    background: #f2ece4;
    border-bottom: 1px solid #e8d8c8;
}
QMenuBar::item { padding: 5px 12px; color: #7c6d5b; }
QMenuBar::item:selected { background: #fffdf9; color: #3f3428; }
QMenu {
    background: #fffdf9;
    border: 1px solid #e8d8c8;
    border-radius: 5px;
    padding: 4px 0;
}
QMenu::item { padding: 6px 22px 6px 16px; color: #6f6254; }
QMenu::item:selected { background: #f6c38c; color: #3f3428; }
QMenu::separator { height: 1px; background: #e8d8c8; margin: 3px 8px; }

/* ════════════════════════ StatusBar ═════════════════════════════════════ */
QStatusBar {
    background: #f0e9e0;
    color: #8a7a6a;
    border-top: 1px solid #e8d8c8;
    font-size: 11px;
    padding: 2px 10px;
}
QStatusBar::item { border: none; }

/* ════════════════════════ ProgressBar ══════════════════════════════════ */
QProgressBar {
    background: #fff8f2;
    border: 0.5px solid #e0d0c0;
    border-radius: 5px;
    text-align: center;
    font-size: 11px;
    color: #7c6d5b;
    max-height: 12px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                                stop:0 #e6953d, stop:1 #f29f4b);
    border-radius: 4px;
}
QProgressBar#progressDone::chunk {
    background: #7abf9a;
}

/* ════════════════════════ TextEdit ══════════════════════════════════════ */
QTextEdit {
    background: #f5ede4;
    border: 0.5px solid #e0d0c0;
    border-radius: 5px;
    color: #7a6a5a;
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 11px;
}

/* ════════════════════════ GraphicsView ══════════════════════════════════ */
QGraphicsView {
    background: #fbf8f3;
    border: none;
}

/* ════════════════════════ ScrollArea ════════════════════════════════════ */
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }

/* ════════════════════════ Right panel — stronger input borders ══════════ */
QFrame#rightPanel QSpinBox,
QFrame#rightPanel QDoubleSpinBox {
    border: 1.5px solid #8a7060;
    background: #ffffff;
    border-radius: 5px;
    color: #3f3428;
}
QFrame#rightPanel QSpinBox:hover,
QFrame#rightPanel QDoubleSpinBox:hover { border-color: #6a5040; }
QFrame#rightPanel QSpinBox:focus,
QFrame#rightPanel QDoubleSpinBox:focus { border-color: #f29f4b; }
QFrame#rightPanel QComboBox {
    border: 1.5px solid #8a7060;
    background: #ffffff;
    color: #3f3428;
}
QFrame#rightPanel QComboBox:hover { border-color: #6a5040; }
QFrame#rightPanel QComboBox:focus { border-color: #f29f4b; }
QFrame#rightPanel QComboBox::down-arrow { border-top-color: #6b5a4a; }
QFrame#rightPanel QLineEdit {
    border: 1.5px solid #8a7060;
    background: #ffffff;
    color: #3f3428;
}
QFrame#rightPanel QLineEdit:hover { border-color: #6a5040; }
QFrame#rightPanel QLineEdit:focus { border-color: #f29f4b; }
QFrame#rightPanel QCheckBox::indicator {
    border: 1.5px solid #8a7060;
    border-radius: 3px;
    background: #ffffff;
}
QFrame#rightPanel QCheckBox::indicator:checked {
    background: #e6953d;
    border-color: #f29f4b;
}

/* ════════════════════════ Icon Rail (activity bar) ═══════════════════════ */
/* Rail blends into the right panel background — only a single hairline
 * separates it from the content area. Tabs are flat by default, gain a
 * subtle warm tint on hover, and a clearly-saturated tint when active.
 * No outlined card style — the bg shift IS the affordance. */
QFrame#railPanel {
    background: transparent;
    border-right: 1px solid #efe0ce;
}

QToolButton#railTab {
    background: transparent;
    color: #9a8878;
    border: none;
    border-radius: 6px;
    padding: 4px 2px 2px 2px;
    font-size: 10px;
    font-weight: 500;
}
QToolButton#railTab:hover {
    background: #fbeede;
    color: #5a4d3e;
}
QToolButton#railTab:checked {
    background: #fee5cc;
    color: #c97028;
    font-weight: 600;
}
QToolButton#railTab:disabled {
    color: #c8b8a0;
    background: transparent;
}

/* Pin button at the bottom of the rail. Unchecked = auto-hide drawer
 * mode; checked = panel is pinned open. */
QToolButton#railPin {
    background: transparent;
    color: #b8a898;
    border: 1px solid transparent;
    border-radius: 14px;
    min-height: 0;
    padding: 0;
}
QToolButton#railPin:hover {
    background: #fee5cc;
    color: #c97028;
}
QToolButton#railPin:checked {
    background: #f29f4b;
    color: #ffffff;
    border-color: #d97d1e;
}
QToolButton#railPin:checked:hover {
    background: #d97d1e;
}

/* KLARF Export button */
QPushButton#klarfExportBtn {
    background: #E1F5EE;
    border: 1px solid #1D9E75;
    color: #085041;
    border-radius: 7px;
    padding: 8px 0;
    font-weight: 600;
    font-size: 13px;
}
QPushButton#klarfExportBtn:hover {
    background: #9FE1CB;
    border-color: #0F6E56;
}
QPushButton#klarfExportBtn:pressed {
    background: #5DCAA5;
}
"""
