# [F16] 用 name-table S_BOUNDING_BOX 加速 Load GDS ROI（免解幾何剪枝）

> **狀態：** done (2026-06-04)
> **§8 ID：** [F16]
> **建立：** 2026-06-04
> **負責 branch：** claude/friendly-franklin-9uZqU

---

## Goal & Context

**問題現象：** user 回報「Load GDS ROI 常常開很久」。追查 `oasis_random.walk_roi` 的 ROI 剪枝路徑：
每顆 cell 的 `reachable_bbox` 由 `load_cell_bbox` 取得——有 CE 邊界層（L108/D250）時每顆只讀 ~1 個邊界
矩形即停（快）；**KLayout 轉出的 `.oas` 沒有 CE 層 → `load_cell_bbox` 退化成「整顆 cell 全解」，
第一次 ROI load 的 reachable_bbox 全階層 sweep ≈ 把整顆 chip 解一遍 → 慢**。這正是 F12 撤案時點出的根本卡點。

**觀察到的機會：** 許多 KLayout 轉出檔在 CELLNAME 表帶 `S_BOUNDING_BOX` 標準屬性（SEMI P39），
即每顆 cell 的**完整 bbox**（含 placement）直接寫在 name table。若有此屬性，ROI 剪枝可像讀 `S_CELL_OFFSET`
一樣**從 name table 直接取 per-cell bbox、完全不解幾何**。

**成功長相：** 帶 S_BOUNDING_BOX（flag==0）的大檔，第一次 Load GDS ROI 不再需要全 chip 解碼；
沒有此屬性的檔維持現行 CE early-stop／解碼路徑，行為與效能不變。

**與現有系統關係：** 延伸 `scan_cell_offsets`（同一趟 name-table 掃描順手收 bbox map）+ 在 `reachable_bbox`
前面加一條 fast path。**不取代** CE early-stop，是它的更快前置捷徑；無屬性時 fallback 原路。

---

## Q&A Decisions

### Q0（前置診斷）: 三個測試檔有沒有 S_BOUNDING_BOX？
先在 `scan_cell_offsets` / Diagnose 報告加偵測（count + 原始 value 取樣），由 user 跑真實檔回報。

### Q1: 走方案 A（讀 name-table bbox）還是方案 B（自建 bbox sidecar）？
**選項：** A（有 S_BOUNDING_BOX 時免費讀） / B（一次性全階層 bbox sweep + sidecar 快取）
**選擇：** **A**
**理由：** 診斷結果——兩個會慢的大檔（1.8 GB）每顆 cell 都帶 S_BOUNDING_BOX（檔3 還剛好無 CE 層，正是最慢型）；
唯一沒有的檔（345 MB E3B）本就有 CE 層＋檔小，不是慢的那個。三檔都用不到 B → B 留 backlog。

### Q2: S_BOUNDING_BOX 五個值怎麼解？
由真實檔取樣解碼確認：`[flag, x_左下, y_左下, 寬, 高]`，cell-local grid/DBU 單位；x/y 有號、寬/高無號；
bbox = `(x, y, x+寬, y+高)`。`flag==0` = **完整 bbox（含 placement）**——由檔1 top cell bbox≈整顆 chip
（top cell 幾乎無自身幾何）反證確認。只採用 flag==0 的條目；flag!=0（僅自身幾何）跳過、走 fallback。

---

## Milestones

### M1: 診斷偵測  [status: done]

- [x] `scan_cell_offsets` 偵測 `S_BOUNDING_BOX`：`n_bbox_props` 計數 + `bbox_sample` 原始值取樣
- [x] `oasis_debug.report_file`（Diagnose 選單）+ `[gds-scan]` 終端機顯示
- [x] user 跑三檔回報 → 確認方案 A 可行、解碼 value 格式

### M2: 方案 A 實作  [status: done]

- [x] `scan_cell_offsets` 建完整 `bbox_by_refnum` / `bbox_by_name`（僅 flag==0，存 `(x0,y0,x1,y1)` grid）
- [x] `RandomAccessReader` stash `_sbbox_by_refnum/_by_name` + `sbbox_for(cell_id)`（解析 refnum/name，仿 `offset_for`）
- [x] `reachable_bbox`（walk_roi closure + 標準 `_reachable_bbox`）加 fast path：有 sbbox 直接回傳、跳過
      `load_cell_bbox` 與子遞迴，memoize 進 `_reach_memo`
- [x] DEBUG 交叉檢查（仿 CE-VIOLATION）：查表 bbox 必須包住每顆走訪 cell 的實際 bbox，否則記 `SBBOX-VIOLATION`
- [x] 測試 `TestSBoundingBoxPrune`（value 大於真實幾何以證明值來自 name table；flag!=0 fallback；遠端 instance
      免解幾何剪枝）+ `TestBoundingBoxProp`（偵測）
- [x] SESSION_LOG + 本 plan

---

## 不變式 / 風險

- **§7 觸碰：** 改動 `reachable_bbox` / CE early-stop 路徑。fast path 只在 sbbox 存在時插隊，無 sbbox 完全走原路
  → 既有 CE 檔行為不變（測試 587 passed 佐證）。
- **正確性風險：** 若 S_BOUNDING_BOX 比真實完整 extent 小，剪枝會漏幾何。靠 (a) 只採 flag==0（完整 bbox 語意，
  由 top-cell 反證）、(b) DEBUG `SBBOX-VIOLATION` 守門 兩道防線。實檔取樣（檔1/檔3）皆一致。
- **記憶體：** 最大檔約 29 萬 cell，bbox map ≈ +60–80 MB；對 1.8 GB 檔工作流可接受。日後可改 ndarray 精簡。

## 後續（未做）

- [F-?] 方案 B：給「大檔 + 無 S_BOUNDING_BOX + 無 CE 層」型做一次性 bbox sweep + sidecar。目前三檔用不到，留 backlog。
