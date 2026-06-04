# [F12] 無 LAYERNAME 表時，Scan layers 從幾何列舉數字 layer（免手 key）

> **狀態：** planned
> **§8 ID：** [F12]
> **建立：** 2026-06-04
> **負責 branch：** claude/friendly-franklin-9uZqU

---

## Goal & Context

**問題來源：** user 手上「很多」OASIS 檔屬於非 Calibre 寫出、缺索引表的型態。
2026-05-28 的 F12 撤案是卡在「無 `S_CELL_OFFSET` / 無 CE 邊界層 → 首次 ROI 載入需
全 chip 解碼」這個**根本效能問題**。

**本次重啟的範圍重新界定（依 2026-06-04 Q&A）：** user 接受用 **KLayout 轉檔**補
`S_CELL_OFFSET`（隨機存取 + bbox 那塊由 KLayout 解決，GLAS 端不自建 bbox 索引）。
真正未解的痛點只剩一個：**這類檔（即使 KLayout 轉過）沒有 `LAYERNAME` 表**，只有數字
layer/datatype（例 `17/101`、`6/0`）。目前「Scan layers」靠 `LAYERNAME_GEOM/TEXT`
記錄列舉 layer，沒有這些記錄就**列空 → 強制 user 手 key**。

**成功長相：** 對沒有 LAYERNAME 表的檔按「Scan layers」，能從**實際幾何記錄**列舉出
出現過的數字 (layer/datatype)，丟進既有 `LayerPickDialog` 讓 user 勾選；**只顯示數字**
（無名稱），免手 key。有 LAYERNAME 表的檔維持原本秒級 fast-path 不變。

**跟現有系統的關係：** 延伸 `_scan_oas_with_streamer` 的 fallback 分支；不動隨機存取
/ bbox / walk 熱路徑（§7 不變式完全不碰）。`LayerPickDialog` 已支援無名稱條目
（`gds_align_tool.py:513-515`），UI 端零改動。

---

## Q&A Decisions

### Q1: 無索引檔首次開啟必須一次性全檔掃描建 bbox 索引（分鐘級），可接受嗎？
**選項：** A 可接受、做 GLAS 內建持久化 bbox 索引 / B 太久、需另想辦法
**選擇：** 都不採 — user 改答「**可接受 KLayout 轉檔**補 cell_offset」。
**理由：** 隨機存取 + bbox 由 KLayout 轉檔解決，等同當年撤案的替代方案，GLAS 端不需自建
bbox 索引。**本 plan 不含任何 bbox / 隨機存取索引工作。**

### Q2: GLAS 內建索引的觸發方式？
**選擇：** 同 Q1 — 採 KLayout 轉檔，**不在 GLAS 內建 bbox 索引**。

### Q3: 無 LAYERNAME 表，layer 清單怎麼呈現？
**選項：** A 只顯示數字 / B 提供 UI 讓 user 自訂層名
**選擇：** **A 只顯示數字即可**（如 `L17/D101`）。
**理由：** 多數對位流程夠用；`LayerPickDialog` 已能顯示無名稱條目，省 UI 工。

---

## Milestones

### M1: core — `enumerate_layers` 加幾何 fallback  [status: planned]

把純掃描邏輯抽進 core（Qt-free、可單元測試），LAYERNAME 缺席時改掃幾何記錄。

- [ ] `oasis_streamer.py` 新增 `enumerate_layers(path, *, shared_buf=None,
      progress_cb=None) -> list[dict]`：
  - 先走現有 fast-path：收集 `LAYERNAME_GEOM/TEXT`，在首個 CELL 記錄前若已見過
    LAYERNAME → 回傳（與現行行為 byte-for-byte 等價）。
  - **fallback**：抵達首個 CELL 記錄時若 `seen_layername == False` → 不 break，
    切換成幾何列舉模式，繼續 `iter_records` 到 EOF，從 RECTANGLE(20)/POLYGON(21)/
    PATH(22)/TRAPEZOID(23)/CTRAPEZOID(26)/CIRCLE(27) 的 `payload["layer"]` /
    `payload["datatype"]` 收 distinct pair（name=""）。
  - `progress_cb(records_scanned, layers_so_far)` 週期性回報（供 UI 串流 + 早停）。
- [ ] de-dup 用 `(layer, datatype)`；輸出 dict 結構 `{"layer","datatype","name"}`
      與 fast-path 一致，供 `LayerPickDialog` 直接吃。
- [ ] 驗證：`tests/test_oasis_layer_scan.py`
  - 用 `oasis_writer.write_oasis` 造一個「有幾何、無 LAYERNAME」的小檔（rect 落在
    17/101 與 6/0）→ `enumerate_layers` 回傳恰好這兩 pair、name 皆空。
  - 有 LAYERNAME 的檔（fixture 或手造）→ 走 fast-path、不進幾何掃描（用 progress_cb
    呼叫次數或 sentinel 斷言未進 fallback）。

### M2: app — 接線 + 串流進度 + cancel  [status: planned]

- [ ] `_scan_oas_with_streamer`（`gds_align_tool.py:967`）改呼叫 core
      `enumerate_layers`，progress_cb 內 `q.put(("progress", "...found N layers:
      17/101, 6/0 …"))`，讓 user 在掃描中就看到 layer 浮現。
- [ ] cancel：沿用 `LayerScanWorker` 既有 subprocess terminate（已可中止長掃描）。
      確認 fallback 的長迴圈不吞 SIGTERM（純 Python 迴圈，process terminate 即停）。
- [ ] `_on_scan_finished`（:673）路徑不變（已能開 `LayerPickDialog`）。空結果文案
      可微調為「此檔無 LAYERNAME 且幾何中未見 layer」。
- [ ] 驗證：`QT_QPA_PLATFORM=offscreen pytest tests/test_gds_align*.py` 綠；
      手動：開無 LAYERNAME 檔 → Scan → 數字 layer 列出 → 勾選 → 帶入 edit。

### M3: （選用）掃描結果 sidecar 快取  [status: planned]

> 「很多檔」→ 每次重開 app 重掃 O(檔案) 很痛。把列舉結果快取，2 次後即時。
> 視 M1/M2 完成後 user 意願決定是否做；不阻擋主功能。

- [ ] 列舉結果以 `(path, st_mtime, st_size)` 為 key 存 sidecar（JSON），命中即跳掃描。
      存放位置沿用 `gds_layer_cache` 的 cache-dir 慣例（檔案可能在唯讀網路碟）。
- [ ] 驗證：第二次 scan 命中快取、不進幾何迴圈（progress_cb 0 次）。

### M4: 文件  [status: planned]

- [ ] README scan 章節補「無 LAYERNAME 檔的數字 layer 列舉」。
- [ ] CLAUDE.md §8 把 F12 從撤案改列「進行中 / 完成」；§4 模組說明微調。
- [ ] SESSION_LOG.md 新增條目；本 plan 勾 checkbox。

---

## Affected Files

- `glas/core/oasis_streamer.py`（新增 `enumerate_layers`）
- `glas/app/gds_align_tool.py`（`_scan_oas_with_streamer` 接線、進度文案）
- `tests/test_oasis_layer_scan.py`（新）
- `docs/plans/F12-no-layername-scan.md`（本檔）、`README.md`、`CLAUDE.md`、`SESSION_LOG.md`
- （M3 選用）`glas/core/gds_layer_cache.py` 或新 sidecar helper

---

## Risks / Open Questions

- **效能**：幾何 fallback 是 O(檔案)，多 GB 檔可能分鐘級。緩解：(1) 進度串流 + cancel，
  user 看到要的 layer 即可早停；(2) M3 sidecar 快取讓 2 次後即時。**不做早停啟發式**
  （怕漏掉罕見 layer）。
- **TEXT 記錄**：先只列幾何 layer（RECTANGLE…CIRCLE）；TEXT(19) 用 `text_layer`，
  視需要再納入（預設不納，避免文字層混入量測層清單）。
- **§7 不變式**：本 plan 完全不碰隨機存取 / walk / early-stop / bbox，無觸及風險。
- **待 user 後續確認**：M3 sidecar 快取是否要做（先把 M1/M2 做完驗收再定）。

---

## 驗證方式

- [ ] 所有 milestone checkbox 已勾（M3 視決定）
- [ ] `pytest tests/test_oasis_layer_scan.py -v` 通過 + 全套件不退步
- [ ] 手動：開一顆無 LAYERNAME 的真實檔 → Scan layers → 數字 layer 正確列出並可勾選載入
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 最終 SESSION_LOG 條目註記 `完成 [F12]`
- 從 `CLAUDE.md` §8 移除 F12（撤案註記改完成）
- 本檔保留作 design history
