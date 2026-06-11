# CLAUDE.md — GLAS 專案 Context

本檔案在 Claude Code 啟動時自動載入，作為專案的工作指南與架構索引。
人類讀者請看 `README.md`；完整變更歷史請看 `SESSION_LOG.md`。

---

## 1. 專案簡介

**GLAS（GDS-Layout Alignment for SEM）** 是一套把 **GDS/OASIS layout** 對位到
**SEM 影像**的獨立桌面工具，核心能力為：

1. **大檔 OASIS streaming / random-access 解析** — 自寫 parser，對數百 MB 的
   production OASIS 做秒級 ROI 隨機存取（不依賴 klayout / gdstk）。
2. **KLARF ↔ GDS 座標換算 + FOV 空間查詢** — 由 SEM defect 的 die-corner 座標
   定位到 layout 的對應位置。
3. **即時 Boolean 表達式引擎** — HMI 風格表達式（`L0 = [(A > W:10) & B] < H:10`）
   即時合成 layer，輸出 shapely polygon + uint8 mask。
4. **SEM↔GDS overlay 對位** — 手動拖動 + `cv2.matchTemplate` 自動 fine-align，
   匯出 per-image alignment offset（CSV / JSON）。
5. **OASIS 匯出**（F9/F11，開發者模式）— 自寫 OASIS writer（`oasis_streamer` decode 的逆），把選定
   raw / Boolean layer 反向寫出 `.oas`（KLayout 可開）。範圍可選目前 FOV（含 GDS 座標框裁剪 ROI）或
   整顆 chip（分 tile 串流 + 全 chip 重算 Boolean，記憶體受單 tile 控制）。

- **語言/框架**：Python 3.9+、PyQt6 6.5+
- **科學計算**：NumPy 1.24+、OpenCV 4.8+、shapely 2.0+
- **進入點**：`python main.py`
- **由來**：原為 MMH 專案 `tools/gds_align_tool.py`（plan F2），2026-05-24 抽離成
  獨立 repo（complete design history 見 `docs/plans/F2-gds-align-tool.md`）。

---

## 2. 🚨 工作規則（最優先）

### 2.1 Session Log 必填

**每次 session 對程式碼進行任何變更（功能、bug fix、重構、文件更新），都必須在
`SESSION_LOG.md` 最上方新增一筆紀錄。** 即便只是一行 bug fix 也要寫；只有純
read-only 探索（沒動任何檔案）才可略過。

紀錄格式（參考既有條目）：變更類型 / 動機現象 / 修復實作 / 測試 / 影響檔案 / Branch。

### 2.2 Session 開始時的動作

進入新 session 後，**先讀 `SESSION_LOG.md` 最上方 3-5 條**，了解最近狀態與未完成項。

### 2.3 規劃流程 (Planning Discipline)

凡使用者提**新功能 / 重構 / 撰寫企劃**，**不可直接動工**，必依下列順序：

1. **探索**：用 Explore agent 掃描相關區域，理解現有實作與慣例
2. **Q&A**：用 `AskUserQuestion` 與使用者來回確認關鍵岔路；不可猜測意圖
3. **產 plan**：討論收斂後才產生 plan 檔，存於 `docs/plans/<feature-id>-<slug>.md`，
   採 Milestone + checkbox 結構（範本：`docs/plans/_template.md`）
4. **核准**：明確請使用者核准 plan 才能開工
5. **執行 + 同步更新**：每完成一個 subtask 就勾掉 checkbox，並在 `SESSION_LOG.md` 補條目
6. **§8 連動**：在 §8 任務清單以 `- [Fxx] ...` 註冊；跨 session 任務移到「進行中」區

可**跳過**此流程的情境：小 bug fix、typo、純讀取問題、文件微調。判斷不出大小時 → 預設走全流程。

---

## 3. 常用指令

```bash
python main.py                          # 啟動 GUI
pytest tests/ -v                        # 完整測試（~707 項，需 numpy / cv2 / shapely / PyQt6）
python3 -m py_compile <file>            # 修改後語法檢查
```

無 lint / format 工具；commit 前請手動跑相關測試。PyQt6 測試在無顯示環境用
`QT_QPA_PLATFORM=offscreen` 跑；無 Qt 環境的 GUI 測試會自動 skip。

---

## 4. 目錄結構

```
GLAS/
├── main.py                  # 進入點（把 glas/core + glas/app 放上 sys.path 後啟動 app）
├── conftest.py              # pytest bootstrap：同樣把 core/app 放上 sys.path
├── requirements.txt
├── CLAUDE.md / README.md / SESSION_LOG.md
├── glas/
│   ├── core/                # ⚙️ 無 Qt 引擎（純運算，可被任何專案複用）
│   │   ├── oasis_streamer.py    # OASIS byte-stream decoder（record 0–34 + CBLOCK + repetition）
│   │   ├── oasis_writer.py      # OASIS writer（F9，decode 的逆；RECTANGLE/POLYGON + F11 串流 writer，純 stdlib）
│   │   ├── layout_export.py     # F9 ROI 裁剪 + shapely→rings + F11 tile_grid（shapely）
│   │   ├── oasis_debug.py       # F10 診斷報告（record histogram / round-trip / 錯誤上下文）
│   │   ├── oasis_store.py       # per-cell / per-layer geometry storage（chunked ndarray）
│   │   ├── oasis_walker.py      # cell-graph walker + transform 展開 → root 座標
│   │   ├── oasis_random.py      # S_CELL_OFFSET 隨機存取 + ROI walk + CE 邊界 early-stop
│   │   │                        #   + F12 enumerate_layers/sample_layers（無 LAYERNAME 時 bounded 抽樣列 layer）
│   │   │                        #   + F16 sbbox_for：有 S_BOUNDING_BOX 時 reachable_bbox 免解幾何直接剪枝
│   │   ├── layerscan_cache.py   # F12 layer 列舉結果 sidecar 快取（keyed by mtime+size+params）
│   │   ├── fine_align.py        # F8/F14 batch fine-align worker + _BatchPool 常駐 pool（F23）
│   │   ├── parts_catalog.py     # F21 PART/CHIP catalog data model（ChipSpec/PartSpec，無 Qt）
│   │   ├── devlog.py            # 終端機上色分類 log（tag/paint/dim，ANSI 安全偵測，無 Qt）
│   │   ├── gds_fov.py           # KLARF↔GDS 座標換算 + FOV 空間查詢
│   │   ├── gds_boolean.py       # 遞迴下降 parser + shapely Boolean 引擎 + mask
│   │   ├── gds_layer_cache.py   # layer .npz cache + metadata（schema v4/v5）
│   │   ├── cellcache.py         # F16-B：大 cell 解碼結果 + placement prep 的 sidecar 快取（欄狀 .npz）
│   │   └── klarf_parser.py      # KLARF I/O（自 MMH 複製，純標準庫）
│   └── app/                 # 🖼 PyQt6 app 殼
│       ├── gds_align_tool.py    # 主視窗 + 所有 widget（~4500 行）
│       ├── sem_loader.py        # KLARF / 資料夾載 SEM 影像列表
│       ├── styles.py            # QSS 設計 token（自 MMH 複製）
│       ├── collapsible.py       # CollapsibleSection widget（自 MMH 複製）
│       └── icons/               # Lucide-style SVG icon set（自 MMH 複製）
├── tests/                   # ~707 項（test_oasis_* / test_gds_* / test_sem_loader / test_parts_catalog / test_cellcache / test_devlog / test_accel_*）
│   └── fixtures/sample_real.klarf
└── docs/plans/              # plan 檔（_template.md + F2 design history）
```

**Import 規則（重要）：** core 與 app 以**扁平 sys.path 模組**載入（`import oasis_streamer`、
`import gds_fov`、`import sem_loader` …），不是 `glas.core.X` package import。`main.py` 與
`conftest.py` 把 `glas/core` 與 `glas/app` 兩個目錄放上 `sys.path`，所有 sibling 互相
bare-import。app 模組額外把 `glas/core` 放上 path 以取用引擎。新增模組時沿用此慣例。

---

## 5. 核心架構

### 5.1 OASIS 解析 pipeline（glas/core）

```
oasis_streamer  byte-stream → record stream（decode unsigned/signed/real/delta、CBLOCK 解壓）
oasis_store     record stream → per-cell ndarray（RECTANGLE/POLYGON/PLACEMENT）
oasis_walker    per-cell → root 座標（Transform 矩陣 + repetition 展開）
oasis_random    name-table S_CELL_OFFSET → 隨機存取單 cell + top-down ROI walk（剪枝）
```

大檔生產用途走 `oasis_random`：讀 name table → 從 root DFS、用 child bbox 對 ROI
剪枝、CE 邊界層 early-stop（只 decode 邊界矩形即停），只碰與 ROI 相交的少數 cell。

### 5.2 對位流程（glas/app）

```
sem_loader 載 KLARF（die-corner XREL/YREL）→ 選 PART/CHIP（PartChipPanel 從 catalog
帶 chip_corner / FOV / nm-per-px）→ gds_fov.klarf_to_gds 換算 → 點 SEM image
→ oasis_random ROI walk 載入該處 geometry → SemViewer 半透明 overlay
→ 手動拖動（AlignmentDeltaPanel · Set Offset 折進 δ）或 FineAlignPanel
  cv2.matchTemplate 自動 refine
→ 匯出 per-image offset CSV/JSON（schema mmh-gds-alignment-v1，image_id join key）
  + 影像匯出：模擬 GLV 灰階圖 `<id>_gray.png` + ROI label map `<id>_label.png`（F15，
  同一組 per-layer 幾何 rasterize、gray 套 blur / label 不 blur、像素網格一致，下游
  `gray[label==id]` 取 ROI；取代 F13 binary mask；manifest schema mmh-gds-overlay-v2 帶 label_map）
```

**並行模型（F8/F14/F23）：** batch fine-align（`FineAlignAllWorker`）與 image 匯出
（`OverlayExportWorker`，含 raw/overlay/gray/label）的 per-image 工作都是獨立的，跑在 spawn-based `ProcessPoolExecutor`
（compute 抽到 Qt-free 的 `fine_align` / `overlay_export`，worker 各自重建 reader）。worker 數
由 `fine_align.batch_worker_count`（UI「Parallel workers」override；0=auto=每核一個 cap 16）決定，
worker 內 `cv2.setNumThreads(1)` 避免「多進程 × cv2 多執行緒」oversubscription。小批 / raw-only
走 in-thread fallback。**F23 常駐 pool**：`fine_align._BatchPool`（session 單例 `batch_pool`）在
相同 key（path/layers/workers…）下跨多次 Run all 重用同一組 worker，索引透過 `index_snapshot()`
一次注入（省 K× 重掃 name table）；idle 超過 300s 自動釋放（lease refcount 確保批次中不釋放）。

### 5.3 Boolean 引擎（gds_boolean）

HMI 風格表達式 → 遞迴下降 parser → AST → shapely 運算。運算子優先序（高到低）：
`~`（補集）> `> W/H:n` / `< W/H:n`（**方向性** grow/shrink，單位 nm）> `&`（AND）>
`|` / `-`（OR / 差集）。morph 為**方向性 bias**（F4）：`W`=X 軸、`H`=Y 軸、`>`=grow、
`<`=shrink，每邊各 ±n nm（grow=與軸線段的 Minkowski sum；shrink=補集-膨脹-補集 erosion，
需 fov_bbox）。輸出 shapely polygon（canvas 顯示）+ uint8 mask（量測）。

---

## 6. 編碼慣例

- 檔案頂端 `from __future__ import annotations`（支援 3.9 PEP 604）
- core 模組保持**無 Qt 依賴**（只用 numpy / shapely / cv2 / 標準庫），確保可跨專案複用
- core/app 以扁平 sys.path 模組互相 bare-import（見 §4），不用 `glas.core.X`
- 序列化資料用 `@dataclass` + `to_dict()` / `from_dict()` round-trip
- GUI 樣式從 `glas/app/styles.py` 取；新圖示放 `glas/app/icons/<name>.svg`
- 跨執行緒（QThread worker）emit signal 回主執行緒；長運算（ROI walk / 批次 fine-align）
  放 worker thread + `LoadProgressDialog` + cancel，勿凍結 UI

---

## 7. 不要碰的地方（修改前需有測試證據）

| 規則 | 原因 |
|---|---|
| OASIS PLACEMENT info-byte N-bit branch 方向（SEMI P39 §22.6） | M1.10 曾寫反；decoder 與 test 都要對齊 spec |
| `klarf_to_gds`：`GDS = XREL − chip_corner`（Y 預設同向 `flip_y=False`） | user 已實測落點正確；改方向會破壞對位 |
| KLARF↔GDS overlay sign：image x 右=GDS x（anchor.x 減）、image y 下=GDS y 上（anchor.y 加） | M4 對位不變式；fine-align 修正量符號依此 |
| SemViewer 折疊不變式 `render(anchor,drag)==render(anchor−drag,0)` | Set Offset 把拖動折進原點 δ 的基礎 |
| `oasis_random` CE 邊界 early-stop：reachable_bbox 用 `load_cell_bbox`、walk 用完整 `load_cell` | 剪枝靠 bbox、命中才全 decode；混用會慢或漏幾何 |
| layer cache `.npz` 原子寫入 + SCHEMA_VERSION 遷移 | 跨版本舊 cache 必須能開 |
| `cellcache` cell key 含 `wanted_layers`（幾何過濾）；prep key 只含 (file, cell)（placement 不被 layer 過濾、reachable_bbox 用 sbbox → layer 無關） | 鍵錯會回傳不完整/不對的幾何或對不到的 prep |
| `cellcache` 一律 mtime+size 驗證、毀損/版本不符當 miss、原子寫；`_place_prep` 的 index 必須對齊 `content.placements` 順序 | cache 永不可破壞正確性；prep survivor 用 index 取 placement |
| `gds_layer_cache` `_LOADABLE_SCHEMAS` 必須能讀「現在 + 前一版」schema（v4+v5） | F21 加 part_id/chip_id 時，舊 v4 cache 仍須能載入（轉「legacy snapshot」UI 模式） |
| `parts_catalog`：catalog 沒有的 PART/CHIP 不可進下拉、Custom override 只能改 FOV / nm-per-px，不能改 chip_corner | Q4 嚴格清單；chip_corner 來自 catalog 是「不會 key 錯」設計的核心 |

完整演進與每個決策理由見 `docs/plans/F2-gds-align-tool.md`（design history）。

---

## 8. 任務清單 (Tasks)

> **規則：** 任務以 `[Bxx]`（Bug）或 `[Fxx]`（Feature / Refactor）編號。
> 完成後**直接從本清單刪除**（git history + SESSION_LOG.md 自然留紀錄）。

### 進行中 (In Progress)

- [F24] ROI walk + 批次 fine-align 效能分析與改善。已完成：M0 離線 harness、M1 app 內建
  即時效能監測 HUD（`perfmon`+`perf_panel`）、M3 batch per-stage 診斷、M4 Boolean 共用子式
  去重（`gds_boolean` node_cache）、M5 morph 向量化（`_dilate_axis` −36%）。**未做候選**：
  M6 matchTemplate 金字塔（數據顯示不需要）、M7 無 S_BOUNDING_BOX 檔 bbox sidecar（[F17]，
  消首次 walk 整檔解碼 12.4s）、M8 grid analytic clip。待 user 回傳新 .txt 驗證改善幅度後
  決定是否做 M7。plan：`docs/plans/F24-perf-roiwalk-finealign.md`。

### 待辦 (Backlog)

- [F17] （原 `[F16-B]`，改號避免與「大 cell 解碼快取」F16-B 撞名）S_BOUNDING_BOX 的後續：給「大檔 + 無
  S_BOUNDING_BOX + 無 CE 層」型做一次性 bbox sweep + sidecar 快取。**目前已知三個測試檔都用不到**（會慢的大檔
  都帶 S_BOUNDING_BOX）→ 低優先。F16 方案 A 已完成（`docs/plans/F16-sbbox-roi-prune.md`）。
- [F11] ~~整顆 chip OASIS 匯出（原始 + Boolean 全 chip 重算）+ GDS 座標可見性~~ — **撤案**（2026-06-03，
  user 決定不做）。plan 仍保留於 `docs/plans/F11-whole-chip-export.md` 供日後參考。
- [F12] 無索引表 OASIS：**部分完成（2026-06-04）**。原「全原生支援」（含無 per-cell bbox 的隨機存取）
  仍**撤案**——根本卡在無 bbox → ROI 首次載入需全 chip 解碼；替代仍是用 KLayout 開→另存 `.oas` 補索引。
  但 user 反映「很多檔都這型」後，**已實作其中的 layer 列舉缺口**：無 LAYERNAME 表時，「Scan layers」改用
  bounded cell 抽樣從幾何列舉數字 layer（`oasis_random.enumerate_layers` / `sample_layers`，sidecar 快取
  `layerscan_cache`），免手 key。詳見 `docs/plans/F12-no-layername-scan.md` + SESSION_LOG 2026-05-28 / 06-04。

---

## 9. 相關文件

- `@README.md` — 安裝、使用流程、功能總覽
- `@SESSION_LOG.md` — 完整變更歷史（每次 session 必更新）
- `@docs/plans/F2-gds-align-tool.md` — **design history**（原 MMH 開發過程 M1–M7，含所有 Q&A 決策）
- `@docs/plans/F2-M1.13-parser-perf.md` — parser 效能 sub-plan（design history）

---

## 10. 結束 Session 前 Checklist

1. **修改了程式碼？** → 在 `SESSION_LOG.md` 最上方新增條目（§2.1 格式）
2. **進行中任務有推進？** → 勾掉對應 `docs/plans/` checkbox
3. **完成了 §8 任務？** → 從本檔 §8 移除該條，SESSION_LOG 條目註記 ID
4. **發現新 bug / 新增任務？** → 在 §8 用 `[Bxx]` / `[Fxx]` 補上
5. **語法檢查**：`python3 -m py_compile <修改檔案>`
6. **跑相關測試**：`pytest tests/<相關檔> -v`
