"""即時效能事件收集器（Qt-free）。

GLAS 的關鍵操作 —— 開檔 + 建索引、載 layer/ROI walk、Boolean 評估、POI build、
Batch align —— 各自呼叫 :meth:`PerfMonitor.record` 把一筆耗時事件丟進這個 session
單例 ``monitor``。monitor 把事件：

  1. 累積到 ring buffer（最近 N 筆）+ per-op 聚合（次數 / 總和 / 平均 / 最大 / 最近），
  2. 若開了 .txt log 檔 → append 一行人類可讀紀錄，
  3. 呼叫 ``on_event`` callback（UI 面板掛這個，把事件 marshal 回 GUI thread 顯示）。

刻意只用標準庫、無 Qt 依賴：和其他 core 模組一樣可被任何地方 import，且 record()
以 RLock 保護、可從任何 thread（ROI / batch worker thread）安全呼叫。

注意：batch fine-align 的 per-image 工作跑在**獨立 process**（spawn pool），那些
process 各自有自己的 monitor 實例、事件不會回到主行程；per-stage 細節仍走既有的
``[fa-timing]`` console 輸出。主行程這邊記錄的是 batch 的**整批**耗時 / 吞吐量。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# op 類別 → 顯示名（面板與 log 共用；未列的類別原樣顯示）。
OP_LABELS = {
    "open": "Open + index",
    "scan": "Scan layers",
    "roi": "ROI walk",
    "boolean": "Boolean eval",
    "poi": "POI build",
    "template": "Template build",
    "batch": "Batch align",
    "batch:read": "  ├ read SEM",
    "batch:poi": "  ├ POI walk+bool",
    "batch:walk": "  │  ├ ROI walk",
    "batch:bool": "  │  └ Boolean",
    "batch:template": "  ├ template",
    "batch:match": "  └ matchTemplate",
    "export": "Image export",
}


@dataclass
class PerfEvent:
    """一筆操作耗時事件。``op`` 為類別 key（見 OP_LABELS），``label`` 為人類可讀
    細節（檔名 / layer / 表達式），``meta`` 為附帶數據（cells、rects、img/s…）。"""
    op: str
    label: str
    ms: float
    t: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)


def format_event(ev: PerfEvent) -> str:
    """人類可讀的單行紀錄（.txt log 與面板 recent 區共用）。"""
    ts = time.strftime("%H:%M:%S", time.localtime(ev.t))
    metas = "  ".join(f"{k}={v}" for k, v in ev.meta.items())
    name = OP_LABELS.get(ev.op, ev.op)
    line = f"{ts}  {name:<14} {ev.ms:9.1f} ms"
    if ev.label:
        line += f"  {ev.label}"
    if metas:
        line += f"   ({metas})"
    return line


class PerfMonitor:
    """執行緒安全的效能事件收集器（session 單例：模組層級 ``monitor``）。"""

    def __init__(self, maxlen: int = 400) -> None:
        self._lock = threading.RLock()
        self._recent: deque[PerfEvent] = deque(maxlen=maxlen)
        # op -> {"n", "total", "max", "last", "label"}；OrderedDict 保留首見順序。
        self._agg: "OrderedDict[str, dict]" = OrderedDict()
        # UI 面板掛這個（單一消費者）。record() 在持鎖外呼叫，避免回呼反向取鎖。
        self.on_event: Optional[Callable[[PerfEvent], None]] = None
        self.enabled = True
        self._fh = None
        self._logpath: Optional[Path] = None

    # ── 記錄 ────────────────────────────────────────────────────────────────
    def record(self, op: str, ms: float, label: str = "", **meta) -> PerfEvent:
        """記一筆事件。可從任何 thread 呼叫。回傳建立的 :class:`PerfEvent`。"""
        ev = PerfEvent(op=str(op), label=str(label), ms=float(ms),
                       t=time.time(), meta=dict(meta))
        cb = None
        with self._lock:
            if not self.enabled:
                return ev
            self._recent.append(ev)
            a = self._agg.get(op)
            if a is None:
                a = {"n": 0, "total": 0.0, "max": 0.0, "last": 0.0, "label": ""}
                self._agg[op] = a
            a["n"] += 1
            a["total"] += ev.ms
            a["last"] = ev.ms
            a["label"] = ev.label
            if ev.ms > a["max"]:
                a["max"] = ev.ms
            if self._fh is not None:
                try:
                    self._fh.write(format_event(ev) + "\n")
                    self._fh.flush()
                except Exception:    # log 失敗絕不可影響量測 / 程式
                    pass
            cb = self.on_event
        if cb is not None:
            try:
                cb(ev)
            except Exception:
                pass
        return ev

    @contextmanager
    def timed(self, op: str, label: str = "", **meta):
        """``with monitor.timed("roi", label=...) as t: ...; t["meta"]["rects"]=N``

        離開時以 perf_counter 量得的毫秒數 + 合併後的 meta 記一筆。``t["meta"]`` 供
        呼叫端補上事後才算得出的數據（如解碼 cell 數）。"""
        box = {"meta": {}}
        t0 = time.perf_counter()
        try:
            yield box
        finally:
            dt = (time.perf_counter() - t0) * 1e3
            merged = {**meta, **box.get("meta", {})}
            self.record(op, dt, label=box.get("label", label), **merged)

    # ── 快照（給 UI 面板）────────────────────────────────────────────────────
    def aggregates(self) -> list[tuple[str, dict]]:
        """``[(op, {n,total,max,last,label,avg}), ...]``，首見順序。"""
        with self._lock:
            out = []
            for op, a in self._agg.items():
                d = dict(a)
                d["avg"] = a["total"] / a["n"] if a["n"] else 0.0
                out.append((op, d))
            return out

    def recent(self, n: int = 50) -> list[PerfEvent]:
        with self._lock:
            return list(self._recent)[-n:]

    def clear(self) -> None:
        with self._lock:
            self._recent.clear()
            self._agg.clear()

    # ── .txt log 檔 sink ──────────────────────────────────────────────────────
    def set_logfile(self, path) -> Optional[Path]:
        """開一個 .txt log 檔開始記錄（覆寫既有檔）。回傳實際路徑或 None（失敗）。
        會先關掉前一個檔。每筆事件 append 一行 :func:`format_event` 格式。"""
        self.close_logfile()
        try:
            p = Path(path)
            fh = open(p, "w", encoding="utf-8")
            fh.write("# GLAS performance log · "
                     f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            fh.write("# time      operation        elapsed     label   (meta)\n")
            fh.flush()
            with self._lock:
                self._fh = fh
                self._logpath = p
            return p
        except Exception:
            return None

    def close_logfile(self) -> None:
        with self._lock:
            fh, self._fh, self._logpath = self._fh, None, None
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass

    @property
    def logpath(self) -> Optional[Path]:
        return self._logpath

    def is_logging(self) -> bool:
        return self._fh is not None


# session 單例：所有插樁點與 UI 面板共用這一個。
monitor = PerfMonitor()
