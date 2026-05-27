# [F11] 整顆 chip OASIS 匯出 + GDS 座標可見性

> **狀態：** planned — 待核准
> **§8 ID：** [F11]
> **建立：** 2026-05-27
> **負責 branch：** claude/adoring-cannon-oKZKo

---

## Goal & Context

F9 已能匯出**目前載入 FOV/ROI** 的 raw + Boolean layer 成 .oas（user 已驗收：KLayout 開得起來、
boolean layer 也看得到）。但 user 真正要的是：

1. **整顆 chip 匯出**——原始 layer + Boolean 新 layer，涵蓋整片，而非只有當前 FOV。
   （Q&A：Boolean 採**全 chip 重算**；目標檔**蠻大**。）
2. **GDS 座標可見性**——要 clip 特定區域需知道 GDS 座標，但目前讀數不明顯。
   （Q&A：**兩者都要**——常駐 cursor 座標 + 裁剪框一鍵帶入。）

**現況限制（探索結果）：**
- GLAS 刻意移除 full-load、改走 ROI 隨機存取（`gds_align_tool.py:12`）；Boolean layer 是
  **FOV-local**（`:46`、`_recompute_recipes(fov)`），非全 chip。
- 座標讀數 `_status_cursor` 只接 **GDS 畫布 hover**（`:4598`）、且會被其他訊息蓋掉（`:5139/5150/5228`）；
  SEM 模式下看不到。

---

## Q&A Decisions

- **Q1 Boolean 範圍：** 全 chip 重算（正確的全域合成）。
- **Q2 檔案大小：** 蠻大 → 效能/記憶體是首要風險；必須 worker thread + 進度 + cancel；保留 tiled fallback。
- **Q3 座標顯示：** 常駐 cursor GDS 讀數 **且** 裁剪框「帶入目前視窗/ROI 範圍」鈕。

---

## Milestones

### M1: GDS 座標可見性（快速里程碑，先做）  [status: planned]

- [ ] 常駐、明顯的 cursor GDS 座標讀數，**SEM 與 GDS 兩模式都可見**：SEM viewer 也 emit GDS 座標
      （overlay anchor 換算）；主視窗用獨立常駐 widget 顯示，不被其他 status 訊息蓋掉（與 `_status_cursor`
      的暫時訊息分離）。同時顯示 µm + nm（裁剪框用 nm）。
- [ ] `OasisExportDialog` 裁剪區加「**帶入目前視窗/ROI 範圍**」鈕 → 自動填四格（doc.bbox_nm 或
      canvas viewport bbox）。
- [ ] 驗證：SEM/GDS 移動滑鼠都看得到座標；裁剪框一鍵帶入正確。

### M2: 整 chip RAW layer 匯出（worker + 進度）  [status: planned]

- [ ] core helper：對選定 raw layer 做**全 chip 遍歷**取幾何（oasis_random ROI=root bbox 全走，或復用
      walker/store 全展開）；回 `(layer, datatype, polygons)`。不污染對位中的 `self._doc`（獨立結構）。
- [ ] 放 QThread worker + `LoadProgressDialog` + cancel（仿 ROI load worker），避免凍 UI。
- [ ] 驗證：整 chip raw 匯出 → KLayout 開、與原檔同區比對一致。

### M3: 整 chip Boolean 重算 + 匯出  [status: planned]

- [ ] 在 M2 取得的整 chip raw 幾何上，以**整 chip bbox** 重算 recipes（沿用 `_eval_expression` /
      `_recompute_recipes` 的邏輯，fov=whole-chip）；morph shrink 的 bbox 用整 chip extent。
- [ ] 與 raw 一起寫出 .oas。
- [ ] **效能風險（最高）**：全域 shapely 在大 chip 可能慢/OOM。緩解：worker + cancel + 事前估算幾何量
      並警告；若實測不可行 → 退回 tiled（Q1 次選）做為逃生口（另開子里程碑）。

### M4: 匯出對話框 scope 選項 + 接線  [status: planned]

- [ ] `OasisExportDialog` 加「匯出範圍」選擇：**目前 FOV（現狀）/ 整顆 chip**。
- [ ] 整 chip 模式走 M2/M3 的 worker 流程；FOV 模式維持 F9 既有路徑。
- [ ] Debug 報告 / sidecar 沿用 F10。

### M5: 測試 + 文件  [status: planned]

- [ ] core 全 chip 遍歷 + 整 chip boolean 重算的單元測試（小型合成 OASIS）。
- [ ] `py_compile` + `pytest`；README/CLAUDE 更新。

---

## Affected Files

- `glas/app/gds_align_tool.py`（座標讀數、export dialog scope、worker）
- `glas/core/layout_export.py`（整 chip 蒐集 helper）或新 core 模組
- 可能 `glas/core/oasis_random.py`（全 chip 走訪入口，若需要）
- 測試 + `README.md` / `CLAUDE.md` / `SESSION_LOG.md` / 本 plan

---

## Risks / Open Questions

- **效能（最高）：** 「全 chip 重算 boolean + 大檔」是最重組合；全域 shapely 可能 OOM/極慢。需 worker +
  cancel + 量級警告；tiled 為 fallback。實測 user 真實檔後再定是否需 tiled。
- 全 chip 走訪 = 放棄 ROI 剪枝、等同 full load；大檔記憶體峰值高（幾何 + shapely + writer bytes 三份）。
  可評估邊走邊寫（streaming）降低峰值，但 boolean 需要全域幾何故難全 streaming。
- 整 chip 匯出 cellname / 座標原點沿用 doc.top_cell_name / 既有 nm 座標（與 F9 一致）。
- 不動 §7 不變式（OASIS decode / 座標換算 / 對位）；新增獨立匯出路徑。

---

## 驗證方式

- [ ] M1：兩模式座標可見 + 裁剪框帶入；M2/M3：整 chip 匯出 → KLayout 與原檔比對；M5：`pytest` 綠
- [ ] `SESSION_LOG.md` 有紀錄

---

## 完成後

- SESSION_LOG 註記 `完成 [F11]`；從 `CLAUDE.md` §8 移除；本檔留作 design history。
