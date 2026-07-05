"""即時效能監控 HUD —— 獨立視窗（F28 M2；原型自 open PR #15 復用 + 大改）。

一個獨立的深色「監控台」top-level 視窗（可自由移動 / 丟副螢幕），顯示 :mod:`perfmon`
monitor 的事件流：

  * 頂部 **總覽列**：階段 / ramp / 吞吐 / RAM / 進度（由 :meth:`update_overview` 餵）；
  * 中段 **聚合表**：per-op 最近 / 次數 / 平均 / 最大，op 名依分類上色；
  * 下段 **分類彩色 log**：每筆事件依 ``category`` 上色（warn/error 標紅底）；可按分類 chip
    篩選、可暫停、可存 ``.txt``、可清除。

``monitor.record()`` 可能在 worker thread 觸發（ROI / batch），故透過 ``_Bridge`` 的
pyqtSignal 把事件 marshal 回 GUI thread（Qt 跨 thread signal 預設 queued）才動 widget。
"""
from __future__ import annotations

import time
from collections import deque
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
)

import perfmon

# ── 深色監控台配色（獨立視窗，刻意與奶油色主 app 區隔，彩色 log 更醒目）─────────────
_BG = "#1e1f22"
_PANEL = "#26282c"
_LOG_BG = "#161719"
_BORDER = "#33363b"
_TEXT = "#d6d9df"
_MUTED = "#888e98"

# 分類 → 顏色（在深底上都清楚可辨、彼此夠分）。未列分類走 _CAT_DEFAULT。
CATEGORY_COLORS = {
    "open": "#6cb6ff",     # blue    — 開檔 + 建索引
    "scan": "#7ee0c0",     # teal    — 掃 layer
    "roi": "#4ec9b0",      # cyan    — ROI walk
    "decode": "#f2a04b",   # amber   — cell 解碼
    "boolean": "#c586c0",  # purple  — Boolean
    "poi": "#dcdcaa",      # khaki   — POI build
    "template": "#9cdcfe",  # l-blue  — template
    "export": "#89d185",   # green   — image export
    "worker": "#b8a2ff",   # lilac   — pool worker
    "ramp": "#e6c07b",     # gold    — worker ramp
    "align": "#4fc1ff",    # br-blue — matchTemplate
    "cache": "#9aa0a6",    # gray    — cell cache
    "warn": "#f28b82",     # coral   — 警示
}
_CAT_DEFAULT = "#b0b4bb"
_WARN_BG = "#3a2a1c"
_ERROR_BG = "#3c2222"


def category_color(cat: str) -> str:
    return CATEGORY_COLORS.get(cat, _CAT_DEFAULT)


class _Bridge(QObject):
    """把 monitor 事件 / summary 從任意 thread 安全送回 GUI thread。"""
    event = pyqtSignal(object)
    summary = pyqtSignal(dict)


class PerfWindow(QWidget):
    """即時效能監控 HUD（獨立視窗）。傳入 :class:`perfmon.PerfMonitor`（預設 session 單例）。

    ``closed`` 在使用者關掉視窗（X）時 emit，讓主視窗的 toggle 鈕同步狀態（視窗只隱藏、
    不真的銷毀，除非主視窗關閉時呼叫 :meth:`shutdown`）。"""

    _COLS = ["", "Operation", "last", "count", "avg", "max"]
    closed = pyqtSignal()

    def __init__(self, monitor=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Window)
        self.setWindowTitle("GLAS · Performance Monitor")
        self.resize(720, 560)
        self._monitor = monitor if monitor is not None else perfmon.monitor
        self._rows: dict = {}                       # op -> table row
        self._events: "deque" = deque(maxlen=600)   # for filter rebuilds
        self._active_cats: set = set(perfmon.CATEGORIES) | {"misc"}
        self._paused = False
        self._chips: dict = {}

        self._build_ui()

        # 接上 monitor（queued cross-thread）。存穩定 emit 參照以便 detach 比對。
        self._bridge = _Bridge()
        self._bridge.event.connect(self._on_event, Qt.ConnectionType.QueuedConnection)
        self._bridge.summary.connect(self.update_overview,
                                     Qt.ConnectionType.QueuedConnection)
        self._emit = self._bridge.event.emit
        self._emit_summary = self._bridge.summary.emit
        self._monitor.on_event = self._emit
        self._monitor.on_summary = self._emit_summary
        self._prime_from_monitor()

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.setStyleSheet(self._qss())
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # 標題 + 工具列
        bar = QHBoxLayout()
        bar.setSpacing(6)
        title = QLabel("⚡  Performance Monitor")
        title.setObjectName("title")
        bar.addWidget(title)
        bar.addStretch(1)
        self._pause_btn = QPushButton("⏸  Pause")
        self._pause_btn.setObjectName("tool")
        self._pause_btn.clicked.connect(self._toggle_pause)
        self._log_btn = QPushButton("● Rec .txt")
        self._log_btn.setObjectName("tool")
        self._log_btn.setToolTip("把每筆事件即時寫到一個 .txt 檔（可回貼分析）")
        self._log_btn.clicked.connect(self._toggle_logfile)
        clear_btn = QPushButton("Clear")
        clear_btn.setObjectName("tool")
        clear_btn.clicked.connect(self._clear)
        for w in (self._pause_btn, self._log_btn, clear_btn):
            bar.addWidget(w)
        root.addLayout(bar)

        # 總覽列（KPI tiles）
        self._tiles: dict = {}
        strip = QHBoxLayout()
        strip.setSpacing(8)
        for key, cap in (("phase", "PHASE"), ("ramp", "WORKERS"),
                         ("throughput", "THROUGHPUT"), ("ram", "FREE RAM"),
                         ("progress", "PROGRESS")):
            tile, val = self._make_tile(cap)
            self._tiles[key] = val
            strip.addWidget(tile, 1)
        root.addLayout(strip)

        # 聚合表
        self._table = QTableWidget(0, len(self._COLS), self)
        self._table.setHorizontalHeaderLabels(self._COLS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setShowGrid(False)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(0, 16)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for c in range(2, len(self._COLS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setMaximumHeight(190)
        root.addWidget(self._table)

        # 分類篩選 chips
        chips = QHBoxLayout()
        chips.setSpacing(4)
        chips.addWidget(self._muted_label("Filter:"))
        for cat in perfmon.CATEGORIES:
            chip = QToolButton(self)
            chip.setObjectName("chip")
            chip.setText(cat)
            chip.setCheckable(True)
            chip.setChecked(True)
            chip.setStyleSheet(self._chip_qss(category_color(cat)))
            chip.toggled.connect(lambda on, c=cat: self._toggle_category(c, on))
            self._chips[cat] = chip
            chips.addWidget(chip)
        chips.addStretch(1)
        root.addLayout(chips)

        # 事件 log（深底、等寬、彩色）
        self._log = QPlainTextEdit(self)
        self._log.setObjectName("log")
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(600)
        root.addWidget(self._log, 1)

    def _make_tile(self, caption: str):
        tile = QFrame(self)
        tile.setObjectName("tile")
        lay = QVBoxLayout(tile)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(1)
        cap = QLabel(caption)
        cap.setObjectName("tileCap")
        val = QLabel("—")
        val.setObjectName("tileVal")
        lay.addWidget(cap)
        lay.addWidget(val)
        return tile, val

    def _muted_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color:{_MUTED}; font-size:11px;")
        return lbl

    # ── monitor 既有狀態（視窗晚於事件建立時補回）──────────────────────────────
    def _prime_from_monitor(self) -> None:
        for op, a in self._monitor.aggregates():
            self._upsert_row(op, a)
        for ev in self._monitor.recent(120):
            self._events.append(ev)
            self._append_line(ev)

    # ── 事件處理（GUI thread）──────────────────────────────────────────────────
    def _on_event(self, ev) -> None:
        self._events.append(ev)
        for op, a in self._monitor.aggregates():
            if op == ev.op:
                self._upsert_row(op, a)
                break
        if not self._paused:
            self._append_line(ev)

    def _append_line(self, ev) -> None:
        if ev.category not in self._active_cats:
            return
        color = category_color(ev.category)
        ts = time.strftime("%H:%M:%S", time.localtime(ev.t))
        name = perfmon.OP_LABELS.get(ev.op, ev.op)
        metas = "  ".join(f"{k}={v}" for k, v in ev.meta.items())
        bg = ""
        if ev.level == perfmon.LEVEL_ERROR:
            bg = f"background:{_ERROR_BG};"
        elif ev.level == perfmon.LEVEL_WARN:
            bg = f"background:{_WARN_BG};"
        flag = "" if ev.level == perfmon.LEVEL_INFO else f" ⚠{ev.level}"
        detail = f"  {ev.label}" if ev.label else ""
        meta_html = (f" <span style='color:{_MUTED}'>({metas})</span>"
                     if metas else "")
        html = (f"<span style='{bg}'>"
                f"<span style='color:{_MUTED}'>{ts}</span> "
                f"<span style='color:{color}'>●</span> "
                f"<span style='color:{color}'>{_esc(name)}</span> "
                f"<span style='color:{_TEXT}'>{ev.ms:8.1f} ms{flag}"
                f"{_esc(detail)}</span>{meta_html}</span>")
        self._log.appendHtml(html)

    def _rebuild_log(self) -> None:
        self._log.clear()
        for ev in self._events:
            self._append_line(ev)

    def _upsert_row(self, op: str, a: dict) -> None:
        name = perfmon.OP_LABELS.get(op, op)
        avg = a.get("avg", a["total"] / a["n"] if a["n"] else 0.0)
        color = QColor(category_color(a.get("category", "")))
        cells = ["", name, f"{a['last']:.0f}", str(a["n"]),
                 f"{avg:.0f}", f"{a['max']:.0f}"]
        row = self._rows.get(op)
        if row is None:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._rows[op] = row
        for c, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if c == 0:
                item.setText("●")
                item.setForeground(color)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            elif c == 1:
                item.setForeground(color)
            else:
                item.setForeground(QColor(_TEXT))
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter)
            if a.get("level") == perfmon.LEVEL_WARN and c == 1:
                item.setForeground(QColor(CATEGORY_COLORS["warn"]))
            self._table.setItem(row, c, item)

    # ── 總覽列（GUI thread；M4 餵資料）─────────────────────────────────────────
    def update_overview(self, summary: dict) -> None:
        """更新頂部 KPI tiles。``summary`` 可含 phase/ramp/throughput/ram/progress
        任意子集（缺的不動）。可透過 :meth:`push_summary` 從 worker thread 安全呼叫。"""
        for key, val in (summary or {}).items():
            lbl = self._tiles.get(key)
            if lbl is not None:
                lbl.setText(str(val))

    def push_summary(self, **fields) -> None:
        """thread-safe：從任何 thread 更新總覽列（marshal 回 GUI thread）。"""
        self._bridge.summary.emit(dict(fields))

    # ── 工具列動作 ──────────────────────────────────────────────────────────────
    def _toggle_category(self, cat: str, on: bool) -> None:
        if on:
            self._active_cats.add(cat)
        else:
            self._active_cats.discard(cat)
        self._rebuild_log()

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self._pause_btn.setText("▶  Resume" if self._paused else "⏸  Pause")
        if not self._paused:
            self._rebuild_log()

    def _clear(self) -> None:
        self._monitor.clear()
        self._events.clear()
        self._rows.clear()
        self._table.setRowCount(0)
        self._log.clear()

    def _toggle_logfile(self) -> None:
        if self._monitor.is_logging():
            self._monitor.close_logfile()
            self._reflect_logging_state()
            return
        default = f"glas_perf_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save performance log", default, "Text files (*.txt)")
        if not path:
            return
        if not path.lower().endswith(".txt"):
            path += ".txt"
        self._monitor.set_logfile(path)
        self._reflect_logging_state()

    def _reflect_logging_state(self) -> None:
        if self._monitor.is_logging():
            self._log_btn.setText("■ Stop rec")
            self._log_btn.setToolTip(f"logging → {Path(self._monitor.logpath).name}")
        else:
            self._log_btn.setText("● Rec .txt")
            self._log_btn.setToolTip("把每筆事件即時寫到一個 .txt 檔（可回貼分析）")

    # ── 生命週期 ────────────────────────────────────────────────────────────────
    def closeEvent(self, e) -> None:
        # 使用者按 X：只隱藏、不銷毀（主視窗 toggle 可再開）；通知主視窗同步狀態。
        e.ignore()
        self.hide()
        self.closed.emit()

    def shutdown(self) -> None:
        """主視窗真的要關時呼叫：切斷 callback + 關 log 檔 + 真正關閉。"""
        if self._monitor.on_event is self._emit:
            self._monitor.on_event = None
        if self._monitor.on_summary is self._emit_summary:
            self._monitor.on_summary = None
        self._monitor.close_logfile()
        super().close()

    # ── 樣式 ────────────────────────────────────────────────────────────────────
    def _qss(self) -> str:
        return f"""
        QWidget {{ background:{_BG}; color:{_TEXT};
                   font-family:'Segoe UI','Noto Sans',sans-serif; font-size:12px; }}
        QLabel#title {{ font-size:14px; font-weight:600; color:#eef0f4; }}
        QFrame#tile {{ background:{_PANEL}; border:1px solid {_BORDER};
                       border-radius:7px; }}
        QLabel#tileCap {{ color:{_MUTED}; font-size:9px; letter-spacing:1px; }}
        QLabel#tileVal {{ color:#eef0f4; font-size:15px; font-weight:600; }}
        QPushButton#tool {{ background:{_PANEL}; border:1px solid {_BORDER};
                            border-radius:6px; padding:4px 10px; color:{_TEXT}; }}
        QPushButton#tool:hover {{ border-color:#4a4e55; background:#2e3136; }}
        QToolButton#chip {{ border:1px solid {_BORDER}; border-radius:9px;
                            padding:1px 8px; font-size:10px; color:{_MUTED};
                            background:transparent; }}
        QTableWidget {{ background:{_PANEL}; border:1px solid {_BORDER};
                        border-radius:7px; gridline-color:transparent; }}
        QHeaderView::section {{ background:{_PANEL}; color:{_MUTED};
                                border:0; border-bottom:1px solid {_BORDER};
                                padding:4px; font-size:10px; }}
        QPlainTextEdit#log {{ background:{_LOG_BG}; border:1px solid {_BORDER};
                              border-radius:7px;
                              font-family:'Consolas','DejaVu Sans Mono',monospace;
                              font-size:11px; color:{_TEXT}; }}
        """

    @staticmethod
    def _chip_qss(color: str) -> str:
        return (f"QToolButton#chip {{ color:{_MUTED}; }}"
                f"QToolButton#chip:checked {{ color:#17181a; background:{color};"
                f" border-color:{color}; font-weight:600; }}")


def _esc(text: str) -> str:
    """最小 HTML escape（log 走 appendHtml）。"""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))
