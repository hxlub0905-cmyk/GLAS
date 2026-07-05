"""即時效能事件收集器（Qt-free）· F28 M1（地基原型自 open PR #15 復用 + 擴充）。

GLAS 的關鍵操作 —— 開檔 + 建索引、載 layer/ROI walk、Boolean 評估、POI/template build、
對位、批次 export（含 per-worker 明細、ramp 狀態）—— 各自呼叫 :meth:`PerfMonitor.record`
把一筆事件丟進 session 單例 ``monitor``。monitor 把事件：

  1. 累積到 ring buffer（最近 N 筆）+ per-op 聚合（次數 / 總和 / 平均 / 最大 / 最近），
  2. 若開了 ``echo_console`` → 用 :mod:`devlog` 上色印一行（讓 UI 與終端共用同一事件流），
  3. 若開了 .txt log 檔 → append 一行人類可讀紀錄，
  4. 呼叫 ``on_event`` callback（UI HUD 掛這個，把事件 marshal 回 GUI thread 顯示）。

**F28 擴充（相對 PR #15）：** 每筆事件多帶 ``category``（粗分類，供 UI 上色 / 篩選；預設取 ``op`` 的
``:`` 前綴）與 ``level``（info / warn / error，供 UI 標紅）。顏色對照放 app 端（`perf_panel`），
core 只存分類字串，維持 Qt-free。

刻意只用標準庫（+ 同為 core 的 Qt-free `devlog`）：可被任何地方 import，且 record() 以 RLock
保護、可從任何 thread（ROI / batch worker thread）安全呼叫。

注意：批次 export 的 per-image 工作跑在**獨立 process**（spawn pool），那些 process 各自有自己的
monitor、事件不會自動回主行程。F28 M4 的即時 worker 監控改由**主行程**的 ``fine_align.run_ramped``
in-flight 追蹤 + 完成時回傳的 per-image timing 來 record（免跨行程 IPC）。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Optional

try:
    import devlog                       # Qt-free sibling; optional console sink
except Exception:                       # pragma: no cover - keep monitor usable
    devlog = None

# 事件等級（UI 依此標色：warn/error → 紅）。
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"

# 粗分類的正典集合（UI 篩選 chips + 顏色對照的 key；顏色對照本身在 app 端）。未列的
# 分類 UI 以中性色顯示，仍可運作。
CATEGORIES = ("open", "scan", "roi", "decode", "boolean", "poi", "template",
              "export", "worker", "ramp", "align", "cache", "warn")

# op 類別 → 顯示名（聚合表與 log 共用；未列的 op 原樣顯示）。
OP_LABELS = {
    "open": "Open + index",
    "scan": "Scan layers",
    "roi": "ROI walk",
    "boolean": "Boolean eval",
    "poi": "POI build",
    "template": "Template build",
    "align": "matchTemplate",
    "batch": "Batch export",
    "export": "Image export",
    "ramp": "Worker ramp",
    "cache": "Cell cache",
}


def _derive_category(op: str) -> str:
    """預設分類 = ``op`` 的 ``:`` 前綴（如 ``worker:24908`` → ``worker``、
    ``batch:walk`` → ``batch``），讓 UI 能把同族事件歸同色/同篩選而呼叫端免每次指定。"""
    return op.split(":", 1)[0] if op else "misc"


@dataclass
class PerfEvent:
    """一筆操作事件。

    ``op`` 為聚合 key（見 OP_LABELS，可含 ``:`` 細分如 ``worker:24908``）；``label`` 為
    人類可讀細節（檔名 / layer / img id）；``category`` 為粗分類（UI 上色/篩選，預設取 op 前綴）；
    ``level`` 為 info/warn/error（UI 標紅）；``meta`` 為附帶數據（cells、rects、img/s…）。"""
    op: str
    label: str
    ms: float
    t: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)
    category: str = ""
    level: str = LEVEL_INFO

    def __post_init__(self) -> None:
        if not self.category:
            self.category = _derive_category(self.op)


def format_event(ev: PerfEvent) -> str:
    """人類可讀的單行紀錄（.txt log / console sink / 面板 recent 區共用）。"""
    ts = time.strftime("%H:%M:%S", time.localtime(ev.t))
    metas = "  ".join(f"{k}={v}" for k, v in ev.meta.items())
    name = OP_LABELS.get(ev.op, ev.op)
    flag = "" if ev.level == LEVEL_INFO else f" !{ev.level}"
    line = f"{ts}  {name:<14} {ev.ms:9.1f} ms{flag}"
    if ev.label:
        line += f"  {ev.label}"
    if metas:
        line += f"   ({metas})"
    return line


class PerfMonitor:
    """執行緒安全的效能事件收集器（session 單例：模組層級 ``monitor``）。"""

    def __init__(self, maxlen: int = 400) -> None:
        self._lock = threading.RLock()
        self._recent: "deque[PerfEvent]" = deque(maxlen=maxlen)
        # op -> {"n","total","max","last","label","category","level"}；OrderedDict
        # 保留首見順序。
        self._agg: "OrderedDict[str, dict]" = OrderedDict()
        # UI HUD 掛這個（單一消費者）。record() 在持鎖外呼叫回呼，避免回呼反向取鎖。
        self.on_event: Optional[Callable[[PerfEvent], None]] = None
        # UI HUD 的頂部總覽列掛這個：任何 thread 可 :meth:`set_summary` 推 ramp/吞吐/
        # RAM/進度 等即時 KPI（與 per-event 流分開，不進 ring buffer / 聚合）。
        self.on_summary: Optional[Callable[[dict], None]] = None
        self.enabled = True
        # 開了才也印到終端（用 devlog 上色）；預設關，避免與既有 print 雙重輸出。
        self.echo_console = False
        self._fh = None
        self._logpath = None

    # ── 記錄 ────────────────────────────────────────────────────────────────
    def record(self, op: str, ms: float, label: str = "", *,
               category: str = "", level: str = LEVEL_INFO, **meta) -> PerfEvent:
        """記一筆事件。可從任何 thread 呼叫。回傳建立的 :class:`PerfEvent`。"""
        ev = PerfEvent(op=str(op), label=str(label), ms=float(ms),
                       t=time.time(), meta=dict(meta),
                       category=str(category), level=str(level))
        cb = None
        with self._lock:
            if not self.enabled:
                return ev
            self._recent.append(ev)
            a = self._agg.get(op)
            if a is None:
                a = {"n": 0, "total": 0.0, "max": 0.0, "last": 0.0,
                     "label": "", "category": ev.category, "level": ev.level}
                self._agg[op] = a
            a["n"] += 1
            a["total"] += ev.ms
            a["last"] = ev.ms
            a["label"] = ev.label
            a["category"] = ev.category
            a["level"] = ev.level
            if ev.ms > a["max"]:
                a["max"] = ev.ms
            if self._fh is not None:
                try:
                    self._fh.write(format_event(ev) + "\n")
                    self._fh.flush()
                except Exception:    # log 失敗絕不可影響量測 / 程式
                    pass
            cb = self.on_event
        if self.echo_console and devlog is not None:
            try:
                print(f"{devlog.tag(ev.category)} {format_event(ev)}", flush=True)
            except Exception:
                pass
        if cb is not None:
            try:
                cb(ev)
            except Exception:
                pass
        return ev

    def set_summary(self, **fields) -> None:
        """推一組即時 KPI 到 UI 總覽列（phase/ramp/throughput/ram/progress 任意子集）。
        可從任何 thread 呼叫（UI 端負責 marshal 回 GUI thread）。無訂閱者 → no-op。"""
        cb = self.on_summary
        if cb is not None and fields:
            try:
                cb(dict(fields))
            except Exception:
                pass

    @contextmanager
    def timed(self, op: str, label: str = "", *, category: str = "",
              level: str = LEVEL_INFO, **meta):
        """``with monitor.timed("roi", label=...) as t: ...; t["meta"]["rects"]=N``

        離開時以 perf_counter 量得的毫秒數 + 合併後的 meta 記一筆。``t["meta"]`` 供呼叫端
        補上事後才算得出的數據（如解碼 cell 數）；``t["level"]`` 可事後升級為 warn/error。"""
        box = {"meta": {}, "label": label, "level": level}
        t0 = time.perf_counter()
        try:
            yield box
        finally:
            dt = (time.perf_counter() - t0) * 1e3
            merged = {**meta, **box.get("meta", {})}
            self.record(op, dt, label=box.get("label", label),
                        category=category, level=box.get("level", level), **merged)

    # ── 快照（給 UI 面板）────────────────────────────────────────────────────
    def aggregates(self) -> list:
        """``[(op, {n,total,max,last,label,category,level,avg}), ...]``，首見順序。"""
        with self._lock:
            out = []
            for op, a in self._agg.items():
                d = dict(a)
                d["avg"] = a["total"] / a["n"] if a["n"] else 0.0
                out.append((op, d))
            return out

    def recent(self, n: int = 50) -> list:
        with self._lock:
            return list(self._recent)[-n:]

    def clear(self) -> None:
        with self._lock:
            self._recent.clear()
            self._agg.clear()

    # ── .txt log 檔 sink ──────────────────────────────────────────────────────
    def set_logfile(self, path):
        """開一個 .txt log 檔開始記錄（覆寫既有檔）。回傳實際路徑或 None（失敗）。
        會先關掉前一個檔。每筆事件 append 一行 :func:`format_event` 格式。"""
        self.close_logfile()
        try:
            from pathlib import Path
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
    def logpath(self):
        return self._logpath

    def is_logging(self) -> bool:
        return self._fh is not None


# session 單例：所有插樁點與 UI HUD 共用這一個。
monitor = PerfMonitor()
