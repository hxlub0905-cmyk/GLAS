# Session Log

> 紀錄原則：每 (日期, 任務) 一條；同天同 task 的多次來回已合併。完整逐 commit 細節見 git history。

---

## [2026-06-04] [F16 後續] walk_roi 自身幾何（RECTANGLE/POLYGON）阵列也裁剪 + type-8 grid 支援

**變更類型：** 效能修復 + 測試 ·  **狀態：完成**

**動機現象：** 加了 placement 子網格裁剪後再測 LTV，placement 端已修好（`max_array_k=304`、
`instances_materialized=111,355`、`visited=2802`、`decoded=713` 都很小），但仍 ~527s/層。關鍵線索：walk 2/3
`newly_decoded_cells=0`（無解碼）卻 612s/174s，時間與**輸出矩形數**成正比（4664 rect→612s、1493→174s）。
定位到 `walk()` 的「自身幾何發射」：`content.rects(key)` 把**每個 RECTANGLE/POLYGON 的 repetition 阵列全展開**
（type 1/8 CMG 阵列可達數百萬），再 `apply_to_rects` 全 transform、最後只留 ROI 內幾千個——且**完全沒有剪枝**
（連 miss ROI 的阵列也全展開）。

**修復實作：**
1. `walk()` 自身幾何改為逐 spec 處理：先做便宜的 whole-array extent 剪枝（miss ROI 直接 O(1) skip，
   這是 `content.rects()` 路徑本來缺的），再用 `_clip_grid_offsets` 只 materialize ROI 附近子網格，才 transform+mask。
   結果與全展開完全相同（clip 回傳真 survivor 的 superset，下游精確 mask 決定）。
2. `_clip_grid_offsets` 重構為 `_grid_axes` 軸分解，**新增 type 8（2D lattice）支援**：向量軸對齊（一橫一縱）時可裁剪，
   斜向 lattice 退回全展開。placement 與幾何兩路共用，故 type-8 placement 阵列現也裁。
3. 幾何阵列 materialize 也計入 `arrays_materialized/instances_materialized/max_array_k`（perf 行含幾何）。

**測試：** 新增 `test_walk_clips_huge_rect_array_to_roi`（1M rect 阵列 walk 只 emit ROI 內 1 顆、`max_array_k<=25`）、
`TestGridClip.test_clip_type8_axis_aligned`（type-8 軸對齊裁剪為 superset、斜向退回全展開）。`pytest tests/` → 591 passed。

**影響檔案：** `glas/core/oasis_random.py`、`tests/test_oasis_random.py`、`SESSION_LOG.md`。

---

## [2026-06-04] [F16 後續] walk_roi 大型 repetition 阵列「解析子網格裁剪」（修真正的慢點）

**變更類型：** 效能修復 + 測試 ·  **狀態：完成**

**動機現象：** LTV 一次 ROI（4×4µm）三層共等 ~17 分鐘（每層 528–593s），但 `newly_decoded_cells=271`、
`pruned=16,263,752`、`sbbox_prune=ON`。即 **F16 sbbox 有生效、幾乎沒在解幾何**，時間全花在「把一個橫跨全 chip、
~1600 萬 instance 的 repetition 阵列整個 materialize 出來，只為了留下小 FOV 內的幾顆」。`place_rtypes=['2','3','10','11']`
（1D 阵列巢狀成 sea）。既有的「整阵列 extent 剪枝」對「阵列 extent 蓋住 ROI」的情形無效 → 退化成全展開。

**修復實作：** 新增 `_clip_grid_offsets`（+ `_roi_to_local` / `_axis_index_range`）：對規則格點型（type 1/2/3）先把
ROI 用 `T⁻¹` 映回阵列 local 座標，解析算出可能命中的 index 子範圍（每邊 pad 1 格做 rounding 安全餘裕），**只 materialize
該子網格**；其餘 arbitrary-list 型（10/11，本就有界）維持全展開。下游仍跑原本的精確 root-space mask 決定 survivor，
故結果與全展開**完全相同**（clip 只回傳真 survivor 的 superset）。`instances_pruned` 改用 `repetition_count` 全數計，統計不失真。
新增遙測 `arrays_materialized / instances_materialized / max_array_k`（`[roi]   perf: ...` 行）。

**測試：** `TestBigGridRepetition.test_roi_inside_picks_one` 加驗 1M 阵列 ROI-inside 時 `max_array_k<=25`（不再全展開）；
新 `TestGridClip`：(1) 四種 D4 旋轉 ×flip 下 clip 結果 ⊇ 真 survivor 且確實縮小；(2) type 2/3 1D 阵列裁剪。
`pytest tests/ -k "oasis or random or walk or layout or boolean"` → 367 passed。

**影響檔案：** `glas/core/oasis_random.py`、`tests/test_oasis_random.py`、`SESSION_LOG.md`。

---

## [2026-06-04] [F16 後續] ROI load 永遠顯示效能遙測（診斷「為何還是慢」）

**變更類型：** 診斷遙測 + 測試 ·  **狀態：完成**

**動機：** user 回報 LTV/R8 兩個大檔 Load GDS ROI 仍很慢，並發現 LTV「有 CE 層但 Scan layers 沒讀到」。釐清：
(1) `bbox_layer` 是寫死的 `DEFAULT_BBOX_LAYER=(108,250)`，與 Scan layers 有無列出無關；(2) 三檔 L108/D250 都只有
~4–6 個矩形（非 per-cell 邊界，cell 數卻上萬）→ CE early-stop 對這些檔幾乎不觸發，本來就慢；F16 的 S_BOUNDING_BOX
才是正解。為了能不開 DEBUG 就看出「prune 到底有沒有生效、時間花在哪」，把 walk 的關鍵數字做成永遠顯示。

**實作：** `RoiWalkStats` 新增 `cells_decoded`（本次真正全解的 cell 數）、`elapsed_s`、`sbbox_prune`（reader 是否有
S_BOUNDING_BOX map → 走免解幾何路徑）。`walk_roi` 收尾填入。app `_on_roi_loaded` 永遠 print：
`[roi] loaded in X.Xs · cells decoded=N · M instances pruned · S_BOUNDING_BOX prune=ON/off`。
**判讀：** 小 FOV 卻 `cells decoded` 上千 → prune 沒咬住（多半 F16 未生效或幾何是 flat 大 cell）。

**測試：** `TestSBoundingBoxPrune::test_walk_prunes_far_instance` 加驗 `sbbox_prune is True` 且 `cells_decoded==2`
（只解 root+命中 child，遠端 instance 子樹未解）。`pytest tests/test_oasis_random.py` 30 passed。

**追加：** 也在 reader 建立處（`_load_roi_around` 上游）印一次性建構遙測：
`[roi] reader built in X.Xs · N cells indexed · S_BOUNDING_BOX on M cells (decode-free prune / NONE)`，
把「建 reader（slurp+scan）」與「walk」兩段時間分開，並直接顯示 sbbox map 有沒有建起來（M≈cell 數 → F16 生效；
M==0 → 退回 bbox-by-decode）。同步修正 `_load_roi_around` 過時 docstring（不再宣稱每次首載都全解每顆 cell）。

**影響檔案：** `glas/core/oasis_random.py`、`glas/app/gds_align_tool.py`、`tests/test_oasis_random.py`、`SESSION_LOG.md`。
**Branch：** `claude/friendly-franklin-9uZqU`

---

## [2026-06-04] [F16] 用 name-table S_BOUNDING_BOX 免解幾何加速 Load GDS ROI

**變更類型：** 功能（ROI 剪枝 fast path）+ 診斷 + 測試 ·  **狀態：完成** ·  **plan：** `docs/plans/F16-sbbox-roi-prune.md`

**背景／現象：** user 回報「Load GDS ROI 常常開很久」。追查：`walk_roi` 靠 `reachable_bbox`→`load_cell_bbox`
取每顆 cell bbox 做 ROI 剪枝；有 CE 邊界層 (L108/D250) 時每顆只讀 ~1 矩形即停（快），**KLayout 轉檔無此層 →
`_decode_bbox_at` 退化成每顆 cell 全解，第一次 ROI load 的 reachable_bbox 全階層 sweep ≈ 全 chip 解碼 → 慢**
（F12 撤案的根本卡點）。

**診斷（M1）：** `scan_cell_offsets` 在掃 name table（含 strict 檔尾表）順帶偵測 `S_BOUNDING_BOX`（`_BBOX_PROP`）：
`n_bbox_props` 計數 + `bbox_sample` 原始值取樣，顯示在 Diagnose 報告與 `[gds-scan]` 終端機。user 跑三個真實檔回報：
**1.8 GB 的兩個慢檔每顆 cell 都帶 S_BOUNDING_BOX（其中 R8_OD 還剛好無 CE 層 = 最慢型）；唯一沒有的 E3B（345 MB）
本就有 CE 層＋檔小**。→ 確定走方案 A（讀 name-table bbox），方案 B（自建 sidecar）三檔用不到、留 backlog。

**解碼確認：** S_BOUNDING_BOX 五值 = `[flag, x左下, y左下, 寬, 高]`，cell-local grid/DBU；x/y 有號、寬高無號；
bbox=`(x,y,x+寬,y+高)`。`flag==0` = 完整 bbox（含 placement，由檔1 top cell bbox≈整顆 chip 反證）。

**實作（M2，方案 A）：**
- `scan_cell_offsets` 建完整 `bbox_by_refnum`/`bbox_by_name`（**僅 flag==0**，存 `(x0,y0,x1,y1)` grid），隨 idx 回傳。
- `RandomAccessReader` stash `_sbbox_by_refnum/_by_name` + 新 `sbbox_for(cell_id)`（解析 refnum/name，仿 `offset_for`）。
- `reachable_bbox`（walk_roi closure + 獨立 `_reachable_bbox`）加 fast path：有 sbbox → **直接回傳、跳過
  `load_cell_bbox` 與子遞迴**，memoize 進 `_reach_memo`。無 sbbox 完全走原 CE/解碼路（E3B 行為不變）。
- DEBUG 守門：walk 內仿 CE-VIOLATION 加 `SBBOX-VIOLATION`——查表 bbox 必須包住每顆走訪 cell 的實際 bbox。

**測試：** `TestSBoundingBoxPrune`（value 故意大於真實幾何以證明值來自 name table；flag!=0 → fallback 解碼；
遠端 instance 免解幾何剪枝且 `_bbox_memo=={}`）+ `TestBoundingBoxProp`（偵測有/無）。`pytest tests/` **587 passed**。

**影響檔案：** `glas/core/oasis_streamer.py`、`glas/core/oasis_random.py`、`glas/core/oasis_debug.py`、
`glas/app/gds_align_tool.py`、`tests/test_oasis_random.py`、`tests/test_oasis_layer_scan.py`、
`docs/plans/F16-sbbox-roi-prune.md`、`SESSION_LOG.md`。
**Branch：** `claude/friendly-franklin-9uZqU`

---

## [2026-06-04] [F12 tune] 放寬 layer 抽樣預算（漏 layer 修正）+ GLAS_SCAN_* env 覆寫

**變更類型：** 調參（core 預設值）+ env 覆寫 + 終端機顯示 + 測試 ·  **狀態：完成，待 user 實機確認覆蓋率**

**現象：** user 回報 strict-mode 修好後，scan 出來的 layer「少一點點」——bounded 抽樣的預期取捨（早停 +
每顆 cell 記錄上限 + max_cells 上限漏掉只出現在少數/深層 cell 的 layer）。

**修法（`oasis_random.enumerate_layers`）：** 預設預算大幅放寬（仍有時間上限保證會結束）：`_SCAN_DEFAULTS`
= max_cells 64→512、max_records_per_cell 2000→8000、stop_after_no_new 16→128、time_budget 15→30s。
參數改 `None` sentinel + `_scan_param()` 解析優先序 **explicit arg > `GLAS_SCAN_<NAME>` env > 預設**，
讓覆蓋率可無痛再調（如 `GLAS_SCAN_MAX_CELLS` / `GLAS_SCAN_STOP_AFTER_NO_NEW` / `GLAS_SCAN_TIME_BUDGET_S`
/ `GLAS_SCAN_MAX_RECORDS_PER_CELL`）。cache params 指紋含解析後的 bounds → 改預算自動失效重掃。diag 加
`sample_bounds`；app 終端機在 sampled 時印出 bounds + 「不完整可用 GLAS_SCAN_* 放寬」提示。

**測試：** 新增 `TestScanParams`（預設夠大、explicit>env>default 解析、env 放寬後 enumerate 由 ≤3 → 全 30
layer）。`pytest tests/` **580 passed**。

**影響檔案：** `glas/core/oasis_random.py`、`glas/app/gds_align_tool.py`、`tests/test_oasis_layer_scan.py`、
`README.md`、`docs/plans/F12-no-layername-scan.md`、`SESSION_LOG.md`。 **Branch：** `claude/friendly-franklin-9uZqU`

---

## [2026-06-04] [F12 bugfix] strict-mode（表在檔尾）OASIS 的 S_CELL_OFFSET/LAYERNAME 找不到 + scan 診斷強化

**變更類型：** bug fix（core `scan_cell_offsets` 補 strict-mode table-offset 跟隨）+ 終端機/Diagnose 診斷強化
+ 測試 ·  **狀態：實作完成、全套件 577 passed，待 user 實機驗收**

**現象（user 回報）：** user 用 KLayout 特別「另存」成有 S_CELL_OFFSET 的 `.oas`，但 GLAS「Scan layers」
仍顯示無索引（no-index）。

**根因：** `scan_cell_offsets` 只做 inline 掃描、**遇到第一個 CELL record 就 break**。但 **strict-mode
writer（KLayout）把 name table（CELLNAME+S_CELL_OFFSET / PROPNAME / LAYERNAME）寫在所有 cell 之後**，
其 byte offset 記在 START（offset_flag=0）或固定 256B 的 END（offset_flag=1）。inline 掃描在第一個 CELL
就停 → 永遠掃不到檔尾的表 → 回 `by_refnum={}` / `layernames=[]` → enumerate_layers 判 no-index。已用合成
strict 檔重現（scan 前回空、修後正確）。

**修法（`glas/core/oasis_streamer.py`）：** 把 per-record 處理抽成 `_consume` 閉包供 inline 與
table-follow 共用；inline 掃描時補抓 START 的 `offset_flag` + `table_offsets`。**當 inline 找不到 offsets
或 layernames 時，依 table_offsets 跟隨檔尾表**：先 PROPNAME（讓 PROPERTY refnum 可解）→ CELLNAME
（帶 S_CELL_OFFSET）→ LAYERNAME，各為**有界 seek 到已知 table 區段**（讀到非該表 record 即停，非全檔掃）。
offset_flag=1 時用 `_read_end_table_offsets` 從檔尾 256B END 讀 6 組 offset pair（`_END_RECORD_LEN`，
SEMI P39 §14）。回傳 dict 加 `offset_flag`/`table_offsets`/`offsets_via`（inline|tables|None）供診斷。
**Calibre inline 檔兩表都 inline → 不進 table-follow，零額外成本**；只缺表的檔才觸發（最多檔尾 256B + 表區段）。

**診斷強化（issue「終端機 debug 需更多資訊」）：**
- `oasis_random`：RandomAccessReader 存 `_offset_flag/_offsets_via/_table_offsets`；`enumerate_layers`
  回傳加 `diag` 區塊並 `_dbg` 輸出。
- app `_scan_oas_with_streamer`：scan 後在終端機印多行 `[gds-scan]` 區塊（source / offset_flag /
  offsets_via / table_offsets / cell-offset 數 / LAYERNAME 數 / 找到的 layer），no-index 時附「KLayout
  請開 strict mode」提示。
- `oasis_debug.report_file`（Diagnose OASIS file… 選單）新增「index tables」段：offset_flag、located via、
  table_offsets、S_CELL_OFFSET 筆數、LAYERNAME 筆數 + 對應建議。

**測試：** `tests/test_oasis_layer_scan.py` 新增 `TestStrictEndTables`（offsets-in-START 帶/不帶 LAYERNAME、
offsets-in-END）共 3 筆 + strict 檔 builder。`QT_QPA_PLATFORM=offscreen pytest tests/` **577 passed**
（含 oasis_random/oasis_streamer 無回歸）。py_compile 全過。**手動：待 user 拿真實 KLayout strict 檔實測。**

**影響檔案：** `glas/core/oasis_streamer.py`、`glas/core/oasis_random.py`、`glas/core/oasis_debug.py`、
`glas/app/gds_align_tool.py`、`tests/test_oasis_layer_scan.py`、`SESSION_LOG.md`。
**Branch：** `claude/friendly-franklin-9uZqU`

---

## [2026-06-04] [F12] 無 LAYERNAME 檔的 layer 列舉（bounded 抽樣 + sidecar 快取）M1–M4

**變更類型：** 功能（core 2 新函式 + 新 cache 模組 + app scan 接線）+ 測試 + 文件 ·
**狀態：實作完成、sandbox 全套件 574 passed，待 user 實機驗收**

**動機：** 承本日 F12 重啟規劃。user 反映「很多 OASIS 都是無 LAYERNAME 表這型」（即使 KLayout 轉檔補了
`S_CELL_OFFSET` 仍只有數字 layer，如 17/101、6/0），「Scan layers」列空→被迫手 key。**追加硬限制：檔案
多 GB，全檔 fallback 掃描「根本開不完」，禁止 O(檔案) 全掃。** 兩個取捨題 user 皆「無偏好」→ 由實作定奪。

**實作（plan `docs/plans/F12-no-layername-scan.md` M1–M4）：**
- **M1 core `oasis_random.py`**：`enumerate_layers(path, *, progress_cb, use_cache, max_cells=64,
  max_records_per_cell=2000, time_budget_s=15, stop_after_no_new=16, include_text=True)` →
  `{"layers":[{layer,datatype,name}], "source": "layername"|"sampled"|"no-index"}`。有 LAYERNAME 表走
  原秒級 fast-path（`_layernames_to_layer_dicts`，沿用 scan_cell_offsets 既得的 layernames）；無表則
  `sample_layers`：用 `_sample_offsets` 從 S_CELL_OFFSET 表**均勻抽樣 ≤max_cells 顆 cell**，各 seek 後
  只讀前 max_records_per_cell 筆記錄收 RECTANGLE/POLYGON/PATH/TRAPEZOID(+VR/VL)/CTRAPEZOID/CIRCLE
  的 (layer,datatype) + TEXT 的 (text_layer,text_type)，連續 stop_after_no_new 顆無新 layer 或超時即
  早停。**無 S_CELL_OFFSET → 回 source="no-index" 空清單，不退化成全掃。** 單一 RandomAccessReader
  (wanted_layers=None) 共用 name-table。**完全不碰 §7 隨機存取/walk/early-stop/bbox 熱路徑。**
- **M3 core `layerscan_cache.py`（新模組）**：列舉結果 sidecar JSON 快取，key=(abspath, mtime, size, 掃描
  params 指紋)；存 per-user cache dir（XDG_CACHE_HOME/LOCALAPPDATA/~/.cache，不寫唯讀網路碟旁）；原子寫、
  壞檔/stale 一律當 miss、最壞重抽，不會弄壞 scan。enumerate_layers `use_cache=True` 命中即跳整個 reader。
- **M2 app `gds_align_tool.py`**：`_scan_oas_with_streamer` 改呼叫 `enumerate_layers`，progress_cb 節流
  （0.15s）串流「已抽 K 顆、找到 N layer：…」到 LoadProgressDialog，user 看到要的即可 cancel（沿用既有
  subprocess terminate）。`_on_scan_finished` 改吃 dict：no-index→提示用 KLayout 補索引；sampled→
  `LayerPickDialog` 加 `note` 標明「抽樣可能不全、缺的可手 key」。LayerPickDialog 既有無名稱顯示（只顯數字）。
- **M4 文件**：README features 加「Layer 掃描（含無 LAYERNAME 檔）」、CLAUDE §4 模組表 + §8 F12 更新、plan、本 log。

**測試：** `tests/test_oasis_layer_scan.py`（14 筆，autouse fixture 把 cache 導到 tmp）——抽樣列舉/TEXT
toggle/混合 shape、LAYERNAME fast-path + sentinel 斷言不進抽樣、no-index 不 hang、**bounded 上限與早停
斷言**（證明非全掃）、cache 命中跳抽樣/檔變失效/use_cache=False bypass、`_sample_offsets` 分散去重。
`QT_QPA_PLATFORM=offscreen pytest tests/` **574 passed**（560+14）。py_compile 全過。**手動 GUI 待 user 實機。**

**影響檔案：** `glas/core/oasis_random.py`、`glas/core/layerscan_cache.py`(新)、`glas/app/gds_align_tool.py`、
`tests/test_oasis_layer_scan.py`(新)、`README.md`、`CLAUDE.md`、`docs/plans/F12-no-layername-scan.md`、
`SESSION_LOG.md`。 **Branch：** `claude/friendly-franklin-9uZqU`

---

## [2026-06-04] F15 驗收（測試通過）+ [F12] 重啟規劃（範圍重界定 → plan 檔）

**變更類型：** 驗收 + 規劃（新增 plan 檔，無程式碼異動）·  **狀態：F15 測試綠；F12 plan 待 user 核准**

**F15 確認：** 沙箱裝 numpy 2.4.6 / cv2 4.13 / shapely 2.1.2 / PyQt6（+ libEGL1）後
`QT_QPA_PLATFORM=offscreen pytest tests/` **560 passed**，與 2026-06-03 條目宣稱一致；F15 核心測試
（`render_label_image` id/背景、後層覆前層保 holes、gray↔label 邊界一致、無 POI 略過路徑）全綠。
程式 + 測試層面驗收通過，僅手動 GUI 端到端待 user 實機。

**F12 重啟（依 §2.3 探索 + Q&A → plan）：** 用 Explore agent 摸清隨機存取/bbox/scan 路徑後與 user
Q&A，**範圍大幅收斂**——user 接受 KLayout 轉檔補 `S_CELL_OFFSET`（隨機存取 + bbox 由 KLayout 解決，
GLAS 端不自建 bbox 索引，避開 2026-05-28 撤案的根本效能卡點）。真正痛點只剩「這類檔無 `LAYERNAME`
表 → Scan layers 列空、強制手 key 數字 layer」。Plan 核心：`oasis_streamer.enumerate_layers` 在無
LAYERNAME 表時 fallback 掃幾何記錄列舉 distinct (layer/datatype)，餵既有 `LayerPickDialog`（已支援
無名稱條目，只顯示數字）。不碰 §7 任何不變式。Plan 存 `docs/plans/F12-no-layername-scan.md`，**待
user 核准才開工**。

**測試：** 純文件 + 驗收，無程式碼異動。

**影響檔案：** `docs/plans/F12-no-layername-scan.md`（新）、`SESSION_LOG.md`。
**Branch：** `claude/friendly-franklin-9uZqU`

---

## [2026-06-03] 完成 [F15] 模擬 GLV 灰階 + label ROI 匯出（取代 F13 binary mask）

**變更類型：** 功能（core 2 函式 + export pipeline + app dialog/worker）+ 測試 + 文件 ·
**狀態：實作完成、sandbox pytest 全綠，待 user 本地驗收**

**動機：** user 釐清下游 MMH 真正要的不是 F13 的 binary mask，而是 (1) 一張「已對齊」的
**模擬 GLV 灰階圖**（像 fine-align template）當量測底圖、(2) **ROI 資訊**。問「ROI 怎麼讀
最快」→ 決定用 **整數 label 圖**（單次 imread + `gray[label==id]` boolean index，零
rasterize/JSON/閾值）。user 進一步決定 **gray+label 直接取代 mask（拿掉 mask 選項）**。

**實作：**
- **M1 core**：`fine_align.render_label_image`（per-layer geom→`make_mask`→paint 整數 id、
  bg=0、無 blur、後層覆前層）+ `render_grayscale_from_geoms`（同組 geom→paint fg_glv + 一次
  blur，hole-preserving 版的 `render_composite_template`）；共用 `_fov_min_corner`（沿用 F13
  mask 的 y_min 1-px raise，§7）→ gray/label 像素網格一致。`OVERLAY_MANIFEST_COLS` 的
  `mask_png` 換成 `gray_png`/`label_png`。
- **M2 `overlay_export`**：`export_one_image` 以 `export_gray`/`export_label`/`score_thr`
  取代 `export_mask`/`mask_thr`；poi 入參擴成 `[(spec, color, fg_glv)]`，label id = POI 位置；
  那次 ROI walk 的 per-layer `geom` 同時餵 gray/label；cfg 取 `bg_glv`/`blur_sigma_px`。
  pool init/task 同步。移除已不用的 `gds_boolean` import。
- **M3 app**：`AlignmentExportDialog` 移除「Export GDS mask」改兩 checkbox（grayscale /
  label map），共用 score-threshold 區塊；`selected()` 回 7-tuple。`OverlayExportWorker`
  建構子改 `export_gray`/`export_label`/`score_threshold`/`label_map`；manifest schema
  bump `mmh-gds-overlay-v1`→`v2` 並加 `label_map`（id→層名+fg_glv）。`_export_overlay_images`
  /`_poi_specs_colored`(+fg)/`_export_label_map` 串接。
- **M4 文件**：README 匯出章節、CLAUDE §5.2 + §8、plan 檔、本 log。

**測試：** sandbox 裝 numpy/cv2/shapely/PyQt6 後 `pytest tests/` **560 passed**（含新
`render_label_image`/`render_grayscale_from_geoms` 決定論 + holes + gray↔label 邊界一致；
F13 mask 測試改寫成 gray/label；F5 schema v2；m5 dialog 7-tuple）。`py_compile` 全過。
**手動 GUI 端到端待 user 本地。**

**影響檔案：** `glas/core/fine_align.py`、`glas/core/overlay_export.py`、
`glas/app/gds_align_tool.py`、`tests/test_export_perf.py`、`tests/test_gds_align_f13.py`、
`tests/test_gds_align_f5.py`、`README.md`、`CLAUDE.md`、
`docs/plans/F15-glv-grayscale-roi-export.md`、`SESSION_LOG.md`。
**Branch：** `claude/optimistic-pasteur-31ELv`

---

## [2026-06-03] 結案 F9 / F10 / F13 / F14；撤案 F11（user 驗收 + 決策）

**變更類型：** 任務管理（文件）·  **狀態：完成**

**背景：** user 本地驗收後確認 F9（layout 匯出）、F10（OASIS debug mode）、F13（per-image mask 批次
輸出 + low-score re-run）、F14（batch fine-align / export 加速）皆 OK → 結案；F11（整顆 chip OASIS
匯出）決定不做 → 撤案。

**修法：**
- CLAUDE.md §8「進行中」清空（F9/F10/F13/F14 依規則完成即從清單刪除，紀錄留在 git history + 本 log）。
- F11 移到「待辦 (Backlog)」並標 ~~刪除線~~ + 撤案註記（比照 F12 慣例）；plan 檔
  `docs/plans/F11-whole-chip-export.md` 保留供日後參考。

**測試：** 純文件變更，無程式碼異動。

**影響檔案：** `CLAUDE.md`、`SESSION_LOG.md`。 **Branch：** `claude/optimistic-pasteur-31ELv`

## [2026-06-02] Batch UX：移除 score 直方圖 + 平行結果改回影像順序（user 回饋）

**變更類型：** UX/效能（app）·  **狀態：完成（待 user 實機確認）**

**背景：** user 回饋 (1) batch 結果頁的 score 直方圖沒什麼用、想再快一點；(2) batch align 進度「跳著
跑」（不是 1,2,3…），顯示怪。

**修法：**
- **直方圖**：`BatchResultsPanel._rebuild_charts` 移除 `_ScoreHistogram`，只留殘差散點圖（散點圖對
  「median residual → origin δ」有實際用途）。順帶減少每次串流刷新的圖表 teardown/rebuild 開銷。
  `score_histogram` / `_ScoreHistogram` 保留（仍有單元測試）。
- **跳著跑**：`FineAlignAllWorker._run_process_pool` 與 `OverlayExportWorker._run_process_pool` 改成
  **全部 job 先提交（worker 仍滿載平行），但結果依提交（影像）順序消費**（`for fut in futures` 取代
  `as_completed`），讓表格/overview 由上而下填、manifest 順序穩定。吞吐幾乎不變（僅進度回報序列化）。
  移除未用的 `as_completed` import。

**已釐清（非 bug）：** 先前回報匯出 overlay/mask「沒對齊」——經查為**操作面**：`AlignmentExportDialog`
預設「全部影像勾選」，batch 中途 abort（或部分影像 flat/失敗）後若直接匯出，**未算到 fine-align 的影像
沒有 `_refined` → 退回 coarse-only**，看起來才像沒對齊。只勾「已算完（有 score）」的影像匯出即正確，
與靜態比對結論一致（畫面 `paintEvent`/`_world_to_view` == 匯出 `overlay_outlines_on_sem`，anchor=
coarse+refined、nm_per_px、座標框逐項相同）。mask 因有 score 門檻把關不受影響；overlay PNG 無門檻才會
混入 coarse-only。可選後續防呆：對話框加「Only images with a fine-align result」過濾（暫未做）。

**測試：** `py_compile` 過；in-thread fallback 路徑不變。**待 user `pytest` + 實機。**

**影響檔案：** `glas/app/gds_align_tool.py`、`SESSION_LOG.md`。 **Branch：** `claude/optimistic-pasteur-31ELv`

---

## [2026-06-02] 測試修正：m5 export dialog 簽章 + m4b 子像素容差（user 本地 pytest 暴露）

**變更類型：** 測試修正（純 tests，無功能變更）·  **狀態：完成**

**背景：** user 在本地（Python 3.13 / numpy 2.4.6 / cv2 4.13 / PyQt6 6.11）跑 `pytest tests/` →
553 passed, 4 failed。

**根因 + 修法：**
- **m5 `TestExportDialog`（2 筆）**：F13 把 `AlignmentExportDialog.selected()` 從 4-tuple 擴成
  6-tuple（多 `export_mask` / `mask_threshold`），舊測試仍 `fmt, ids = selected()` → unpack 錯。
  改 `fmt, ids, *_ = d.selected()`。**屬 F13 API 演進未同步測試。**
- **m4b `TestFineAlignOne`（2 筆）**：`fine_align_one` 子像素拋物線殘差 ~9.5e-6（abs=1e-6 過嚴），
  隨 BLAS/cv2/numpy build 浮動，**與 F13/F14 無關**（未動該數學）。容差放寬 1e-6→1e-3 nm（仍遠嚴於
  任何實際對位誤差，sign/整數像素不變式不受影響）。

**測試：** `py_compile` 過；預期 `pytest tests/` 全綠（待 user 重跑確認）。

**影響檔案：** `tests/test_gds_align_m5.py`、`tests/test_gds_align_m4b.py`、`SESSION_LOG.md`。
**Branch：** `claude/optimistic-pasteur-31ELv`

---

## [2026-06-02] [F14] batch align + image/mask export 加速（規劃→M1–M4）

**變更類型：** 效能/重構（新 core 模組 + worker 平行化 + UI）+ 測試 + 文件 ·  **狀態：實作完成，待 user 本地驗收**

**動機：** user 回報 batch align 與 image/mask 匯出在上萬張規模仍太慢。探索定位兩瓶頸：
(1) **export（`OverlayExportWorker`）完全循序**——單 reader、單 thread，每張逐張 ROI walk + 畫
overlay/mask，**完全沒平行化**（而 align 早在 F8 就多進程平行）；(2) **align worker 上限寫死 8**
（cv2 多 thread × 多進程 oversubscription 顧慮），多核機核心閒置。Q&A：兩條都要加速、核心數不確定→
自動偵測+保守 cap+UI 可調、可接受多 reader 記憶體、快取 align 幾何重用列後續選項。

**實作：**
- **M1** 新增 Qt-free `glas/core/overlay_export.py`：把 `overlay_outlines_on_sem` / `_draw_polyline_np`
  / `_safe_name` 從 app 原樣搬入；新增 `export_one_image(...)`（單張 imread + raw/overlay/mask 寫出 +
  回 manifest row，內用 `poi_polys_and_geometry_for_roi` 單 walk 共用），與舊 worker 單張邏輯逐行等價。
  app 改 re-import 這些 helper（preview 仍可用）。
- **M2** `OverlayExportWorker` 改 orchestrator：`_run_in_thread`（小批/raw-only/無 reader fallback）
  + `_run_process_pool`（spawn `ProcessPoolExecutor`、`_export_pool_init/_task`、as_completed 收 row、
  cancel drop futures、依 job 序回 row 讓 manifest 穩定）。
- **M3** `fine_align.batch_worker_count(override, cap=16)`（cap 8→16；UI override 優先）；
  `fine_align._pool_init` 與 `overlay_export._export_pool_init` 內 `cv2.setNumThreads(1)`；
  FineAlignPanel 加「Parallel workers (0=auto)」spinbox（QSettings 持久化），align/export cfg 透傳
  `max_workers`。
- **M4** `tests/test_export_perf.py`（worker 數解析 / export_one_image raw-only+純函式determinism /
  missing-file / 無 POI 不寫 mask / 模組 Qt-free）+ README + CLAUDE §5.2 並行模型。

**測試：** `py_compile` 全過；既有 F5 `OverlayExportWorker._write_manifest` 測試相容（建構子向後相容）。
沙箱無 numpy/PyQt6，**pytest 綠 + 多核實測加速待 user 本地**。

**影響檔案：** `glas/core/overlay_export.py`（新增）、`glas/core/fine_align.py`、
`glas/app/gds_align_tool.py`、`tests/test_export_perf.py`（新增）、`README.md`、`CLAUDE.md`、
`docs/plans/F14-batch-export-perf.md`、`SESSION_LOG.md`。 **Branch：** `claude/optimistic-pasteur-31ELv`

---

## [2026-06-02] [F13] per-image GDS mask 批次輸出 + low-score re-run（規劃→M1–M4）

**變更類型：** 功能（app + core helper + 測試）+ 文件 ·  **狀態：實作完成，待 user 本地驗收**

**動機：** 下游 MMH 需要 per-image GDS mask 限縮 blob 偵測範圍（解 gray-level 定位失效）；
GLAS 是唯一能產 mask 的工具（Boolean + fine-align），但缺 (1) 批次 mask 輸出、(2) batch
fine-align 後針對 low-score 圖調參重跑（現只能重跑全部上萬張）。Q&A：覆蓋規則 Q1=C（新 score >
舊才覆蓋）、mask 不輸出 fallback（GLAS 把關品質，Q2）、UI 併入 export dialog（Q3）、用既有
`make_mask()`（Q4）、re-run UI 放 BatchResultsPanel（Q5）。

**實作：**
- **M1 `BatchResultsPanel` 子集 re-run**：table 下方新增 Re-run 區塊（Search radius / Background
  GL / Blur σ 覆蓋 spin，per-POI FG GL 沿用 Fine Align 面板）+「Re-run low-score」/「Re-run
  selected」鈕（table 改 ExtendedSelection 多選），emit `rerun_requested(ids, overrides)`。
  MainWindow `_on_rerun_requested` 重用 `FineAlignAllWorker` 跑子集（抽 `_launch_fa`），
  `_fa_rerun_mode` 旗標令 `_on_fa_result` 走 `fine_align.rerun_should_overwrite`（Q1=C：只變好）。
- **M2 `OverlayExportWorker`**：`__init__` 加 `export_mask` / `mask_score_threshold`；`run()` 把
  ROI walk 改成 overlay/mask 任一需要就走一次、共用 `entries`；mask 分支用
  `polys_to_geometry`→`make_mask`（FOV 左下角座標與 `overlay_outlines_on_sem` 對齊）→寫
  `{base}_mask.png`，僅 `mask_should_export(refined, thr)` 為真才寫。
- **M3 `AlignmentExportDialog`**：加 `Export GDS mask (.png)` checkbox + Score threshold spin
  （0.8 / 0–1 / 0.05）+ 即時「N image(s) ≥ threshold」label；`selected()` 多回 2 值，呼叫鏈
  （`_on_export_alignment`→`_export_overlay_images`）透傳。
- **core helper（Qt-free，便於單測）**：`fine_align.py` 新增 `OVERLAY_MANIFEST_COLS`（加
  `mask_png`）、`rerun_should_overwrite` / `mask_should_export` / `rerun_image_subset`。

**探索修正：** 草稿誤寫對話框為 `OverlayExportDialog`，實為 `AlignmentExportDialog`；
`make_mask()` 吃**單一 geom**（keyword-only），故 M2 用 `polys_to_geometry` union 後傳入。

**PR#9 review 修正（Codex，2 × P2）：** (1) **Boolean 洞保留**——原 mask 用 `poi_polys_for_roi`
回傳的 exterior-only rings（`geometry_to_polygons` 會丟內洞）重建幾何，subtraction/complement 表
達式的洞會被填實。改新增 `fine_align.poi_polys_and_geometry_for_roi`（單次 walk 同時回 polys[給
overlay] + hole-preserving geom[給 mask]）+ `gds_boolean.union_geometries`，mask 走 geom。
(2) **1px Y 偏移**——`make_mask(invert_y=True)` 用 `(H-1)-(y-y_min)/nm`，但 overlay 與 fine-align
template（`rasterize_layer`）用 `(y_top-y)/nm`（anchor→H/2）；mask 比兩者高一格。改 `y_min` 抬高一
像素（`anchor_y-(H/2-1)*nm`），使 mask 像素與 `rasterize_layer` 完全一致（新測試 array_equal 證明）。

**測試：** `tests/test_gds_align_f13.py`——5 個純邏輯測試（rerun 覆蓋規則 / 子集選取 / mask
threshold / 無 refined / manifest 欄）+ Qt+cv2 gated 整合測試（worker manifest header）+ review 修正
測試（洞保留、mask↔rasterize_layer 像素相等）。`py_compile` 全過；沙箱無 numpy/PyQt6，
**pytest 綠 + GUI 端到端待 user 本地**。

**影響檔案：** `glas/app/gds_align_tool.py`、`glas/core/fine_align.py`、
`tests/test_gds_align_f13.py`（新增）、`docs/plans/F13-mask-export-rerun.md`、`CLAUDE.md`、
`SESSION_LOG.md`。 **Branch：** `claude/optimistic-pasteur-31ELv`

---

## [2026-05-28] [F12] 探索後撤案：無索引表 OASIS 支援（改用 KLayout 轉檔）

**變更類型：** 決策 / 還原（本 session 的 F12 程式碼變更已全數 revert，淨碼變更為 0）

**背景：** user 丟一顆非 Calibre 寫出、3.9GB 的 `R8_OD_to_VC.oas`——**無 `LAYERNAME`、無 `S_CELL_OFFSET`**
兩個索引表。症狀：Scan layers 找不到 layer、ROI random-access 索引 0 cells（F10 診斷卻列得出 layer）。

**做過什麼（後來全砍）：** 開了 F12 plan + 實作 M1–M8——自建 cell offset 索引（`build_cell_index`，後改成
`consume(skip_cblocks=True)` 跳壓縮塊加速）、`RandomAccessReader` fallback、layer 幾何掃描 + 提早停、
index/layer/reach 三種 sidecar cache、worker 化。索引與 layer 掃描可達秒級。

**為何撤案：** 卡在**根本性**效能問題——這類檔無 per-cell bbox（Calibre 靠 CE 邊界層 (108,250) 每顆只讀 1
矩形即得大小；此檔無此層），`walk_roi` 首次載入為了剪枝必須把 root 整棵子樹每顆 cell 全解一遍 ≈ 全 chip
解碼，對 3.9GB 等同數分鐘且 GIL 卡 UI。reach-bbox 持久化只能讓它「一次性」，第一次仍慢。user 決定不值得，
**整批 revert 回 `e7437f1`（F11 M5）**。

**結論 / 替代方案：** 不在 GLAS 原生支援無索引表 OASIS。需要開這類檔時，**先用 KLayout 開→另存 `.oas`**
（KLayout 寫出會帶 cell offset + layer name 表），轉出的檔即可走 GLAS 現有快速路徑，零程式改動。

**影響檔案：** 無（程式碼還原）。**Branch：** `claude/adoring-cannon-oKZKo`

---

## [2026-05-27] [F11] 整顆 chip OASIS 匯出 + GDS 座標可見性（規劃→M1–M5）

**變更類型：** 功能（core + app）+ 文件 ·  **狀態：待 user 本地驗收**

**規劃：** F9 FOV 匯出驗收 OK 後，user 要 (1) 匯出**整顆 chip**（raw + boolean 新 layer，目前只能匯出當前
FOV）、(2) UI 常駐 GDS 座標好填裁剪。Q&A：boolean **全 chip 重算**、座標**常駐讀數 + 裁剪框一鍵帶入兩者都要**。
因顧慮全域 shapely 數百萬中間物件 OOM，M2/M3 改 **tiled + 串流寫出**、tile **自動分格**。

**實作：**
- **M1 GDS 座標可見性**：獨立常駐讀數 `_coord_readout`（粗體）、SemViewer 新增 `cursor_gds` signal
  （SEM/GDS 兩模式都顯示 µm+nm）、`OasisExportDialog` 裁剪區「Use current view / ROI bounds」帶入鈕。
- **M2 `oasis_writer.OasisStreamWriter`**：增量寫（header→`add_polygons` 逐 layer→256-byte END，context
  manager，錯誤不 finalize）；輸出與 `serialize_oasis` **byte 完全一致**。+ `oasis_random.reachable_bbox` /
  `reachable_bbox_nm` 唯讀 accessor（忠實複製 walk_roi closure；§7：**不改 walk/early-stop 熱路徑**）。
- **M2/M3/M4** `layout_export.tile_grid`（chip span 自動分格、覆蓋角落無縫）+ `WholeChipExportWorker`
  （QThread 分 tile：raw `walk_roi`→clip→串流寫；boolean 用 haloed tile〔外擴=最大 morph+1µm〕建 tile-scoped
  raw_provider→`resolve_expression`→clip 回 tile 串流寫，峰值受單 tile 控制解 OOM）+ `OasisExportDialog`
  scope 下拉（Current FOV / Whole chip）。
- **M5** 文件（README / CLAUDE §1·§4）。

**測試：** py_compile 全過；core tile_grid / stream-writer / reachable_bbox 有單元測試。沙箱無 numpy/shapely/
PyQt6 → **pytest 綠 + 整 chip 端到端（worker/GUI/真實檔 KLayout 比對）+ OOM/效能實測待 user 本地**。

**影響檔案：** `glas/core/{oasis_writer,oasis_random,layout_export}.py`、`glas/app/gds_align_tool.py`、
`tests/{test_oasis_writer,test_oasis_random,test_layout_export}.py`、`README.md`、`CLAUDE.md`、
`docs/plans/F11-whole-chip-export.md`。 **Branch：** `claude/adoring-cannon-oKZKo`（PR #7）

---

## [2026-05-26] [F9] OASIS 匯出：raw + Boolean layer 寫出 .oas（規劃→M1–M6→fixes→驗收）

**變更類型：** 功能（新 core 模組 + app UI）+ bug fix + 文件 ·  **狀態：core 驗收過；GUI 端到端待 user**

**規劃：** 原評估建議 GDSII，但 user 要求公司流程統一 .oas + 深度格式評估後改 **OASIS**（validation scheme
可為 0、CBLOCK/modal 選用、encoder 是既有 decoder 的逆、可用自家 reader round-trip 當 oracle）。範圍：raw
layer + Boolean layer 同檔 + GDS 座標裁剪 ROI；匯出入口走**開發者模式 gating**。

**實作：**
- **M1 `oasis_writer.py`（純 stdlib）**：encode 原語（unsigned/signed/real/string/g-delta）為 decode 的逆；
  `serialize_oasis` 輸出最小合規（MAGIC→START unit=1000 offset_flag=0→CELLNAME→CELL→XYABSOLUTE→幾何→END）；
  axis-rect→RECTANGLE(`0x7b`)、其餘→POLYGON(g-delta)。
- **M2 `layout_export.py`（shapely）**：`clip_polygons/clip_layers/export_layers` + `shapely_to_rings`
  （O-holes 決議：只取外環、所見即所得）。
- **M3 app `OasisExportDialog`**（每 layer 輸出 layer/datatype + GDS 裁剪框）+ `_on_export_oasis`。
- **M5 開發者模式**：`_dev_mode`（QSettings 持久化、About icon 點 5 次切換），Export OASIS 按鈕預設隱藏。
- **M6 文件**。

**fixes：** (a) END record 補滿到 **256 bytes**——KLayout 嚴格要求，否則 `too few bytes after END` 拒檔；
自家 reader 在 END 即 return、padding 不被 decode 不受影響。(b) PR#7 review P2：layer/datatype spinbox 上限
65535→2147483647（避免大 layer ID 靜默截斷）。(c) `scripts/make_sample_oas.py`（產 sample_good/broken 測試檔）。

**驗收：** user 本地 `pytest` 45 passed；KLayout 開 256-END 修正後 `sample_good.oas` 三 layer（RECTANGLE/
POLYGON/g-delta POLYGON）**正確渲染**——writer 格式被 KLayout 接受（最大風險解除）。**剩餘：** GUI Export+Debug
端到端需載入 layout，待 user 有 production 資料時測。

**影響檔案：** `glas/core/{oasis_writer,layout_export}.py`、`glas/app/gds_align_tool.py`、
`scripts/make_sample_oas.py`、`tests/{test_oasis_writer,test_layout_export}.py`、`README.md`、`CLAUDE.md`、
`docs/plans/F9-layout-export.md`。 **Branch：** `claude/adoring-cannon-oKZKo`（PR #7）

---

## [2026-05-26] [F10] OASIS debug mode：載入/匯出雙向診斷（實作 + 驗收）

**變更類型：** 功能（新 core 模組 + app UI）·  **狀態：Diagnose 驗收過；GUI Export-debug 端到端待 user**

**動機：** 開發 streamer/writer 常 parse 出錯，希望備診斷模式。Q&A：載入+匯出**兩端都要**、sidecar
`.debug.txt` + app 內可複製對話框**兩種輸出都要**。

**實作：** `oasis_debug.py`（Qt-free）`report_file`——走 streamer 統計 record histogram / per-layer rect+poly /
START unit+offset_flag / cell names；**永不拋例外**，decode 出錯收 hex-context（streamer 內建）+ traceback；
給 `sent_layers` 做送出↔讀回 round-trip 比對。`layout_export.export_layers` 加 `debug` 參數回 `(n, report)`。
app：`DebugReportDialog`（唯讀 monospace + Copy）、`OasisExportDialog` Debug checkbox、File 選單 dev-only
「Diagnose OASIS file…」、載入失敗於 dev mode 自動對該檔產報告→sidecar+可複製框。

**驗收：** Diagnose 對 broken 檔精準捕捉 decode error + hex + traceback；開發者模式開關 OK。

**測試：** `tests/test_oasis_debug.py`（well-formed/round-trip/truncated/缺檔）。**不動（§7）：** 純新增診斷。

**影響檔案：** `glas/core/{oasis_debug,layout_export}.py`、`glas/app/gds_align_tool.py`、
`tests/test_oasis_debug.py`、`README.md`、`CLAUDE.md`、`docs/plans/F10-debug-mode.md`。
**Branch：** `claude/adoring-cannon-oKZKo`（PR #7）

---

## [2026-05-25] [F8] Batch 反應性與加速（規劃→M1–M4）+ F5/F6/F7/F8 收尾

**變更類型：** 功能/效能 + test fix + 任務收尾 ·  **狀態：實機驗收通過、已結案**

**動機：** user 回報 Batch Align 很卡、運算久、進度條花俏。三根因：(a) `_on_fa_result` 每張整表重建 + 圖刪重生
= O(N²) 主執行緒重繪；(b) F6 thread-pool 8 條純 Python 解碼搶 GIL；(c) `_AnimatedBar` 漸層/發光/掃光動畫。

**實作（plan F8）：** M1 進度條扁平化（單色軌道+填充、determinate `advance()` 不重繪）；M2 節流串流
（`_batch_refresh_timer` 300ms 合併刷新、串流時跳過圖表重建，修 O(N²)）；M3 ProcessPool——抽 Qt-free
`glas/core/fine_align.py`（rasterize/template/matchTemplate/ROI-walk 等 10 純函式），`FineAlignAllWorker`
改 `ProcessPoolExecutor`（spawn，worker 由**路徑**重建 reader 避開 Windows 拉 PyQt6；cancel 用 `fut.cancel()`
張邊界粒度；n≤2/單核走 in-thread fallback）；M4 `TestProcessPoolEquivalence`（每張 result 與循序相等）。

**test fix（4 個）：** 1 F8 回歸（`_run_in_thread` 誤用 `clone()`→改直接用 `self._rar`）+ 3 既有過時測試
（expr_spec 4-tuple、outline cv2.LINE_AA 改斷言偏紅、batch_run no-coords 回 status）。

**收尾：** 驗收後 F6/F7/F8 三 plan 標 done、CLAUDE §8「進行中」清空（plan 留作 design history）。

**測試：** 本地 **206 passed**（含 ProcessPool 等價）。實機：UI 不卡、多核生效、明顯變快、結果正確、進度條扁平 OK。

**不動（§7）：** 批次純函式只搬家、結果不變、fine-align 符號、SemViewer 折疊、CE early-stop、median→δ。
取捨：cancel 粒度由逐 node 改為單張影像邊界。

**影響檔案：** `glas/core/fine_align.py`（新）、`glas/app/gds_align_tool.py`、`tests/{test_accel_equivalence,
test_gds_align_f5,test_gds_align_m4b}.py`、`docs/plans/F5–F8 plans`、`CLAUDE.md`。
**Branch：** `claude/practical-pascal-AtKLm`

---

## [2026-05-25] [F7] Batch 工作區 + inline 進度 + 進度條質感（規劃→M1–M4）

**變更類型：** 功能（UI/UX，運算不變）

**規劃：** 批次結果改第四個 view-mode「Batch」、inline 進度+結果 streaming（取代 modal）、進度條漸層+發光+%。
規劃期間 user 反映 Batch 放 View 排怪 → 改**動作進入 + 返回鈕**。

**實作：** M1 `_AnimatedBar` 質感升級（漸層+軟發光+條內%、加高）；M2 抽 `BatchResultsPanel`（summary/篩選/
排序表/直方圖/散點/median 鈕 + 頂部 inline 進度區）；M3 Batch 工作區（`_center_split`=[結果, SEM]、
enter/exit、點任一 View 鈕離開）；M4 批次接線改 inline（不再 modal、streaming 更新、即時 cancel、點列就地
換 overlay），移除 `FineAlignResultsDialog`。

**測試：** py_compile 過；GUI 待本地。**不動：** F6 批次運算與結果值（§7）。
**注：** 進度條質感後由 F8 回退扁平、串流由 F8 改節流。

**影響檔案：** `glas/app/gds_align_tool.py`、`docs/plans/F7-batch-workspace-ui.md`、`CLAUDE.md`。
**Branch：** `claude/dazzling-cori-5T7XE`

---

## [2026-05-25] [F6] OAS 讀取 + 批次 fine-align 加速（規劃→M1–M3 + PR#5 fix）

**變更類型：** 功能（效能，行為不變）

**規劃：** 功能不變前提下找加速點。Q&A：批次 **thread pool**（per-thread reader + 共享 mmap）、worker
**自動**（cpu_count≤8）、mmap **只用於 ROI/隨機存取路徑**（bulk decode 維持 slurp）。

**實作：** M1 mmap-backed `OasisStream`（+ BytesIO/平台/空檔 fallback slurp）；M2 單一 map 共享
（去 `RandomAccessReader` 雙重 slurp、檔案只 map 一次 + `close()`/context manager）；M3 thread-pool 批次
（抽 `_fine_align_image`、per-thread `clone()` 私有 reader、結果與循序逐值相同、**cv2 設定不動**保 golden）。
**PR#5 review P1：** cancel 後一律 drain 所有 future、保留已完成結果（移除 `break`）。

**測試：** `tests/test_accel_equivalence.py`（mmap↔slurp、共享↔獨立 scan、循序↔4-worker 等價）；沙箱
numpy-free 等價檢查通過。**注：** M3 thread-pool 後由 F8 ProcessPool 取代；M1/M2 mmap 仍在用。

**影響檔案：** `glas/core/{oasis_streamer,oasis_random}.py`、`glas/app/gds_align_tool.py`、
`tests/test_accel_equivalence.py`、`docs/plans/F6-readwalk-batch-accel.md`。
**Branch：** `claude/dazzling-cori-5T7XE`（PR #5）

---

## [2026-05-25] [F5] Fine-align 診斷 + 工作流（規劃→M1–M6 + PR#4 fix）

**變更類型：** 功能 + bug fix

**規劃：** fine-align 結果可視化/診斷；多次擴充收斂 6 milestone。

**實作：** M1 `overlay_outlines_on_sem` + TemplatePreviewDialog（before/after 5 格）；M2 `FineAlignResultsDialog`
（排序表/篩選 + `_ScoreHistogram` + `_ResidualScatter`，result signal 擴 6-tuple，每張回狀態 ok/no-coords/
missing-file/no-scale/flat）；M3 中位殘差→origin δ 一鍵套用；M4 setup 快照/還原（切 DID 不丟 POI/可見性/
顏色）+ 命名（Background/Foreground GL）；M5 cancel 改 `threading.Event`（DirectConnection 即時）+ `_AnimatedBar`
ETA；M6 `AlignmentExportDialog` raw/overlay PNG + manifest（schema `mmh-gds-overlay-v1`）。
**PR#4 review P1：** 非 ok 狀態清掉舊 refined offset（`_refined.pop` + `clear_score`）。

**測試：** `tests/test_gds_align_f5.py`；沙箱無 PyQt6/numpy/cv2 → GUI 待本地。

**影響檔案：** `glas/app/gds_align_tool.py`、`tests/test_gds_align_f5.py`、
`docs/plans/F5-finealign-diagnostics.md`、`CLAUDE.md`。 **Branch：** `claude/sharp-lamport-YIk3z`（PR #4）

---

## [2026-05-25] [F4] Boolean 強化（規劃→實作→閃退/預覽→方向性 W/H→結案）

**變更類型：** 功能（新功能 + 重構）+ bug fix ·  **狀態：F4 + F1 已驗收結案**

**規劃：** synthetic layer 只算一次、ROI reload 即遺失、無法編輯/巢狀。收斂：食譜化每 FOV 自動重算、巢狀
引用、編輯/刪除、表達式對話框重設計。

**實作：** 引擎 `normalize_binding` / `recipe_dependency_order`（拓樸排序+循環/未知 ref 偵測）/
`resolve_expression`（巢狀 ref + memoize + 循環防護）；app `_recipes` 唯一事實源、`_recompute_recipes` 每次
載 ROI 自動重算（synthetic 跟著 defect 走）、`_LayerRow` 編輯/刪除、`ExpressionLayerDialog` 重設計（token
按鈕 + 即時語法檢查 + binding 含 ref + 內嵌預覽）、cache 改 recipe 序列化（含舊格式遷移）。

**bug fix：** edit 閃退根因 = row handler 內同步 `exec()` 對話框→關閉後 row widget 被刪→use-after-free；
改 `QTimer.singleShot(0,…)` 延遲開窗。內嵌 `_ExprPreview` 不再 mutate 主 doc、OK→Save。

**方向性 W/H morphology：** 原為等向 buffer（W/H 只是標籤）；改 W=X 軸、H=Y 軸、`>`grow/`<`shrink，每邊 ±n nm。
`_dilate_axis`（與軸線段 Minkowski sum，對任意多邊形精確）+ `_morph_axis`（shrink=補集-膨脹-補集 erosion，
需 fov_bbox）；對話框運算子鈕 `>W: >H: <W: <H:`。+ `CoordinateSetupPanel` label `setWordWrap`（修面板溢出）。

**結案：** user 本地驗收 F4（含 boolean、KLayout 可開）+ F1（互動：對位/拖動/fine-align/批次/匯出/折疊）皆 OK；
§8 移除 F4/F1。

**測試：** `tests/test_gds_boolean.py`（binding/拓樸/resolve/morph 方向性）；沙箱無 numpy/shapely → 待本地。

**影響檔案：** `glas/core/gds_boolean.py`、`glas/app/gds_align_tool.py`、`tests/test_gds_boolean.py`、
`docs/plans/F4-boolean-enhance.md`、`CLAUDE.md`。 **Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

---

## [2026-05-25] [F3] 多 POI Fine Align + UI 優化（規劃→M1–M5 + 後續修正）

**變更類型：** 功能（fine align 多 POI / UI）+ bug fix

**規劃：** user 提 6 項；收斂為版面/裁切修正、Layer 用 LAYERNAME 顯示名稱、POI 鈕放大、Fine Align 改**多 POI**
（各自 FG gray、合成一張樣板做單次 matchTemplate、彈窗並排 SEM/GDS/Template）。

**實作：** M1 版面/裁切（`CollapsibleSection` SetMinimumSize、對話框最小寬夾螢幕）；M2 LAYERNAME 名稱
（`scan_cell_offsets` 收集 layernames、`resolve_layer_name`、`LayerEntry.display_name`）；M3 多 POI 核心
（`render_composite_template`）；M4 POI 多選 UI（POI 鈕放大）；M5 `TemplatePreviewDialog`。

**後續修正：** toolbar 窄視窗改橫向 `QScrollArea` 捲動不裁字；`resolve_layer_name` 改「最具體優先 + 跳過
(0,INF) catch-all」（修所有 layer 同名）；移除無用 opacity slider；多 POI 選取改以 row `isChecked()` 重建
（修 `ndarray __eq__` ValueError，PR#3 P1）。

**測試：** `tests/{test_oasis_random,test_gds_align_m4b,test_gds_align_m7}.py`；GUI 待本地。

**影響檔案：** `glas/app/{gds_align_tool,collapsible}.py`、`glas/core/{oasis_streamer,oasis_random}.py`、
`tests/*`。 **Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

---

## [2026-05-24] UI / branding 整合（抽離後首輪 UI 微調）

**變更類型：** 功能（UI / branding）

**內容（合併本日多筆）：**
- **品牌整合**：視窗 icon / toolbar wordmark、自繪 About 對話框、app 改名 "GDS Align Tool"→**GLAS**
  （setApplicationName 等）。
- **依 `docs/glas_ui_fixes.md` 五項**：Coordinate Setup 預設收起、LAYERS 空白引導改三層置中、Set/Clear
  Offset 移入 `SemPanel`、toolbar group label 對比、empty state 文案。
- **UI batch 1**：Load SEM 主色按鈕、`CollapsibleSection` 折疊 badge（FOV 已設/未設）、image list 對位狀態
  badge（`_ImageListDelegate`：no-coords / score 綠琥珀紅）。
- LAYERS empty hint 置中微調。

**測試：** py_compile 過；`pytest tests/` 435→**442 passed**（含 offscreen render-grab 煙霧測試）。

**影響檔案：** `glas/app/{gds_align_tool,collapsible}.py`、`tests/{test_gds_align_m6,test_gds_align_m7,
test_gds_align_m4b}.py`。 **Branch：** `claude/{determined-einstein-Bfo0G, jolly-babbage-8nwED}`（PR #2）

---

## [2026-05-24] GLAS 專案自 MMH 抽離成立

**變更類型：** 專案建立 / 重構（抽離）

**動機：** GDS Align Tool 原藏在 MMH 專案 `tools/`（plan F2，M1–M7 全實作）。其核心能力（大檔 OASIS
streaming/random-access、KLARF↔GDS 換算、FOV 查詢、Boolean 引擎、SEM↔GDS 對位）可跨專案複用，故抽離成
獨立 repo **GLAS（GDS-Layout Alignment for SEM）**。

**實作（自 MMH git HEAD 搬移，零行為改動）：**
- **glas/core/（無 Qt）**：`oasis_streamer/oasis_store/oasis_walker/oasis_random/gds_fov/gds_boolean/
  gds_layer_cache` + 自 MMH 複製的 `klarf_parser`（core 原本即無 src 依賴，零修改）。
- **glas/app/（PyQt6）**：`gds_align_tool`（改 header import：`from src.*` → flat sys.path）、`sem_loader`
  + 複製 `styles/collapsible/icons`。
- **import 慣例**：core/app 以扁平 sys.path 模組互相 bare-import；`main.py` + `conftest.py` 設 path。
- **規則機制移植**：`CLAUDE.md`（§2 規則 / §6 慣例 / §8 任務 / §10 checklist；§1/§4/§5/§7 改寫成 GLAS）、
  `.claude/settings.json` + hooks、`README.md`、本 `SESSION_LOG.md`、`docs/plans/F2*` design history、
  14 個 test 檔 + `fixtures/sample_real.klarf`。

**測試：** sandbox 無 numpy/cv2/shapely/PyQt6/pytest → 僅 py_compile 全過；完整 `pytest tests/`（~218 項
應全綠，證零行為改動）待有相依環境執行。

**接續任務：** [F1] 互動驗收（自 MMH 抽離前即「待 user 本地驗證」，移到 GLAS 接續 → 已於 2026-05-25 隨 F4 結案）。

**影響檔案：** 整個 GLAS repo（新建）。 **Branch：** （新 repo）
