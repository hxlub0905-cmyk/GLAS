"""即時效能監測面板（HUD）。

掛在主視窗的一個 QDockWidget 裡，顯示 :mod:`perfmon` monitor 收到的每筆操作事件：

  * 上半：per-op 聚合表（操作 / 最近 ms / 次數 / 平均 / 最大），即時更新；
  * 下半：最近事件逐行 log（含 meta）；
  * 工具列：開 / 關 .txt log 檔、Clear。

monitor.record() 可能在 worker thread 觸發（ROI / batch），故透過 ``_Bridge`` 的
pyqtSignal 把事件 marshal 回 GUI thread（Qt 跨 thread signal 預設 queued）才更新 widget。
"""
from __future__ import annotations

import time
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import perfmon


class _Bridge(QObject):
    """把 monitor 事件從任意 thread 安全送回 GUI thread。"""
    event = pyqtSignal(object)


class PerfPanel(QWidget):
    """即時效能 HUD。傳入 :class:`perfmon.PerfMonitor`（預設 session 單例）。"""

    _COLS = ["Operation", "last (ms)", "count", "avg (ms)", "max (ms)"]

    def __init__(self, monitor: "perfmon.PerfMonitor | None" = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._monitor = monitor if monitor is not None else perfmon.monitor
        self._rows: dict[str, int] = {}     # op -> table row

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # ── 工具列 ────────────────────────────────────────────────────────────
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self._log_btn = QPushButton("Log to .txt…")
        self._log_btn.setToolTip("把每筆操作即時寫到一個 .txt 檔（可回傳分析）")
        self._log_btn.clicked.connect(self._toggle_log)
        self._clear_btn = QPushButton("Clear")
        self._clear_btn.clicked.connect(self._clear)
        self._status = QLabel("")
        self._status.setStyleSheet("color:#6b5d4a; font-size:11px;")
        bar.addWidget(self._log_btn)
        bar.addWidget(self._clear_btn)
        bar.addWidget(self._status, 1)
        root.addLayout(bar)

        # ── 聚合表 ────────────────────────────────────────────────────────────
        self._table = QTableWidget(0, len(self._COLS), self)
        self._table.setHorizontalHeaderLabels(self._COLS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, len(self._COLS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setMaximumHeight(180)
        root.addWidget(self._table)

        # ── 最近事件 log ──────────────────────────────────────────────────────
        self._log = QPlainTextEdit(self)
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(500)
        self._log.setStyleSheet(
            "font-family: Consolas, 'DejaVu Sans Mono', monospace; "
            "font-size: 11px;")
        root.addWidget(self._log, 1)

        # ── 接上 monitor（queued cross-thread）────────────────────────────────
        # 把 bound signal emit 存成穩定參照：每次存取 ``self._bridge.event.emit``
        # 會回傳新的 wrapper 物件，detach 時無法以身分比對，故快取一份。
        self._bridge = _Bridge()
        self._bridge.event.connect(self._on_event, Qt.ConnectionType.QueuedConnection)
        self._emit = self._bridge.event.emit
        self._monitor.on_event = self._emit
        self._prime_from_monitor()

    # ── monitor 既有狀態（面板晚於事件建立時補回）────────────────────────────
    def _prime_from_monitor(self) -> None:
        for op, a in self._monitor.aggregates():
            self._upsert_row(op, a)
        for ev in self._monitor.recent(50):
            self._log.appendPlainText(perfmon.format_event(ev))
        if self._monitor.is_logging():
            self._reflect_logging_state()

    # ── 事件處理（GUI thread）────────────────────────────────────────────────
    def _on_event(self, ev: "perfmon.PerfEvent") -> None:
        for op, a in self._monitor.aggregates():
            if op == ev.op:
                self._upsert_row(op, a)
                break
        self._log.appendPlainText(perfmon.format_event(ev))

    def _upsert_row(self, op: str, a: dict) -> None:
        name = perfmon.OP_LABELS.get(op, op)
        avg = a.get("avg", a["total"] / a["n"] if a["n"] else 0.0)
        cells = [name, f"{a['last']:.1f}", str(a["n"]),
                 f"{avg:.1f}", f"{a['max']:.1f}"]
        row = self._rows.get(op)
        if row is None:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._rows[op] = row
        for c, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if c > 0:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter)
            self._table.setItem(row, c, item)

    # ── 工具列動作 ────────────────────────────────────────────────────────────
    def _toggle_log(self) -> None:
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
        p = self._monitor.set_logfile(path)
        if p is None:
            self._status.setText("⚠ could not open log file")
        self._reflect_logging_state()

    def _reflect_logging_state(self) -> None:
        if self._monitor.is_logging():
            self._log_btn.setText("Stop logging")
            self._status.setText(f"logging → {Path(self._monitor.logpath).name}")
        else:
            self._log_btn.setText("Log to .txt…")
            self._status.setText("")

    def _clear(self) -> None:
        self._monitor.clear()
        self._rows.clear()
        self._table.setRowCount(0)
        self._log.clear()

    # ── 生命週期 ──────────────────────────────────────────────────────────────
    def detach(self) -> None:
        """切斷 monitor callback（主視窗關閉時呼叫，避免事件打到已毀 widget）。"""
        if self._monitor.on_event is self._emit:
            self._monitor.on_event = None
        self._monitor.close_logfile()
