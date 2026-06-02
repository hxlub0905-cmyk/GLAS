# Session Log

> 紀錄原則：每 (日期, 任務) 一條；同天同 task 的多次來回已合併。完整逐 commit 細節見 git history。

---

## [2026-06-02] 狀態快照：進行中任務的「未做 / 未驗證」盤點

**變更類型：** 文件整理（無碼變更）。前提：本沙箱無 numpy/cv2/shapely/PyQt6 →
**所有 pytest 皆未實際跑過**（僅 `py_compile` 過），全部待 user 本地 `pytest tests/ -v`。

- **[F9] OASIS 匯出**：已實作 M1–M6（writer + ROI 裁剪 + 匯出對話框 + 開發者模式）。
  未驗證＝GUI 匯出（全區 + ROI）→ KLayout 開檔幾何/座標/裁切正確、`pytest
  test_oasis_writer`。未做＝M5 收尾（README/§1/§5 補匯出說明、從 §8 移除）。
- **[F10] OASIS debug mode**：已實作 M1–M4。未驗證＝`pytest test_oasis_debug
  test_layout_export`、GUI（匯出勾 Debug 看報告 + sidecar、File→Diagnose OASIS）。
- **[F11] 整顆 chip 匯出**：**plan 待核准**；部分實作（WholeChipExportWorker，
  NameError 已修）。未做＝M1 兩模式座標可見 + 裁剪框帶入、OasisExportDialog「匯出
  範圍」選項。未驗證＝整 chip → KLayout 比對、OOM/效能、`pytest`。
- **[F13] S_BOUNDING_BOX 剪枝**：M1–M3.5 已實作並 commit（收集 + reachable_bbox
  短路 + repetition `_candidate_offsets` 裁剪 + DEBUG tracing）。驗證＝剪枝/裁剪
  正確（`--debug` 看 `sbbox_used` 大、無 `sbbox_violations`/`CLAMP-MISMATCH`）。
  未達＝M4「首次 walk 秒級」**做不到**——真檔有 mega-cell（refnum 271517，1351 萬
  placement，decode ~283s），非剪枝可解 → 拆為 [F14] 暫擱。未驗證＝`pytest
  test_oasis_random`（新增 TestStdBboxPrune / TestRepetitionClamp）。
- **[F14]（backlog）**：placement-heavy mega-cell 優化（numpy 欄位陣列 decode +
  walk 向量化 + 快取），**user 選擇暫擱**。
- **[F12]**：撤案（無索引表 OASIS，2026-05-28）。

**唯一還有「實作未做」的進行中任務＝F11**（且 plan 未核准）；F9/F10/F13 皆只差
user 本地驗證。**影響檔案：** 無（純文件）。**Branch：** `claude/magical-davinci-Ibo8K`

---

## [2026-06-02] [F13] 最終根因：單一 mega-cell（1351 萬 placement）decode ~4.7min（暫擱）

**變更類型：** 診斷結論（無碼變更，純記錄）· **狀態：user 決定先停（不投入 mega-cell 優化）**

**根因（決定性）：** tracing 顯示 `SLOW load_cell 271517: 283.5s (~0 geom specs,
13,518,432 places)`。即 ROI 落點下方有**一顆 cell 含 1351 萬筆獨立 PLACEMENT 記錄**
（非 AREF 陣列壓縮，攤平成個別 placement）。OASIS 在 cell *內部*無空間索引 → 要找出
ROI 內的少數 placement，必須把 1351 萬筆**全部 decode 過一遍**（Python 逐筆 varint，
物理下限 ~1–2min；283s 大半耗在建 1351 萬個 `Placement` dataclass + walk 的 1351 萬次
Python 迴圈）。

**重要結論：** S_BOUNDING_BOX 剪枝（M1–M2）+ repetition 裁剪（M3.5）**正確且有效** ——
正是它們讓 walk 從 root 秒級剪枝、跳過其餘 29 萬 cell、精準定位到這顆 mega-cell。瓶頸
已從「全樹遍歷」縮到「單一病態 cell」，與原問題本質不同。

**未做的優化（user 選擇先停，列入 backlog [F14]）：** placement-heavy cell 改 numpy
欄位陣列 decode（免建千萬物件）+ walk 向量化 ROI 篩選 + 快取/磁碟 sidecar。預估可把
首碰 ~283s 降到 ~30–60s、之後即時；但冷啟動無法達「秒級」（無 cell 內索引）。

**本 session 保留的程式（皆已 commit、對正常檔有益、DEBUG-gated tracing）：**
S_BOUNDING_BOX 收集/短路、repetition `_candidate_offsets` 裁剪、`SLOW load_cell` /
`FULL-ARRAY` / `REACH-FALLBACK` / `BIG-ARRAY` / `prune source` 診斷行。

**影響檔案：** 無（純文件）。**Branch：** `claude/magical-davinci-Ibo8K`

---

## [2026-06-02] [F13] walk_roi 真正瓶頸：chip 級 repetition 陣列全展開 → 解析裁剪

**變更類型：** 效能修正（core）· **狀態：待 user 真檔 GUI 驗收（秒級 + 幾何正確）**

**定位：** user 加 tracing 後回報 `prune source: std_bboxes=True root_std_bbox=(0,0,
7460112,2204400)`（M2 短路有效）、`SLOW load_cell 'iMerge_Top': 5.2s (~0 specs,
5 places)`（root 是薄 top cell，非扁平大 cell；5.2s 是 1.84GB 首次 mmap 暖機）。
Ctrl+C 落在主執行緒 `_on_tick`（walk 在 worker thread，抓不到堆疊）。綜合：卡點在
walk 展開**某個橫跨整顆 chip 的 regular grid placement** —— 陣列整體 bbox 蓋到 2µm
ROI → 通過 array-level 剪枝 → 舊碼 `repetition_offsets_np` 把**全部 K 個**（可達數百萬）
instance materialize（`(K,4)` 陣列幾 GB），即使 ROI 只命中 1~2 個。此期間無 load_cell
→ `cells scanned` 卡個位數不動，完全吻合現象。

**修復（解析裁剪，保證不漏幾何）：**
- 新增 `_candidate_offsets` / `_axis_index_range`（oasis_random）：對**可分離且軸對齊**
  的 regular grid（repetition type 1/2/3）、在**無旋轉** transform（`M` 對角）下，解析
  算出 ROI 附近的 index 視窗（外round + 各邊 widen 2 step），只 materialize 該子網格。
  回傳**保守超集**，下游既有精確 ROI mask 仍逐一判定 → 不漏。旋轉/斜交/不規則
  (type 8/9/10/11) 回 None → 走原本全展開（行為不變）。
- DEBUG 安全網：前 8 個大陣列同時跑完整 materialize 比對選中數，不符印 `CLAMP-MISMATCH`；
  `BIG-ARRAY` 行印 rep 數 → candidate 數。

**測試：** `py_compile` 過；新增 `TestRepetitionClamp`（type1/type2 grid + flip 的裁剪
選集 == 完整選集；旋轉 / type9 fallback）。裁剪邏輯另以 20 萬筆隨機暴力比對驗證為
保守超集（含負 step/負 scale/退化軸，0 violation）。沙箱無 numpy → pytest 待 user 本地跑。

**影響檔案：** `glas/core/oasis_random.py`、`tests/test_oasis_random.py`。
**Branch：** `claude/magical-davinci-Ibo8K`

**追加（同日）：** user 回報加裁剪後仍卡在 `SLOW load_cell 'iMerge_Top'` 之後、**無
`BIG-ARRAY`/`CLAMP-MISMATCH`** → 卡點在未記 log 的 fallback 路。補兩條 tracing（皆在
昂貴運算**之前**印，卡死也看得到）：`FULL-ARRAY`（裁剪不支援的 type 8/9/10/11 或旋轉
transform → 全展開；印 rtype/count/diagT/raw）、`REACH-FALLBACK`（cell 的 S_BOUNDING_BOX
flag!=0 → std_bbox None → reachable_bbox 退回全樹遞迴；印 raw operands）。done 摘要加
`reach_fallback` 計數。待 user 回報哪條觸發以決定下一步（擴充裁剪 type8/9 或處理 flag!=0）。

---

## [2026-06-02] [F13] walk_roi 卡死診斷：DEBUG cross-check 全 decode + 加 tracing

**變更類型：** 效能修正 + 診斷（core）· **狀態：待 user pull 後回報 tracing**

**現象：** user 真檔（`R8_OD_to_VC_KKKK.oas`，有 CE 層 108/250）`--debug` 開 ROI
→ progress 卡在 `5 / 292883 cells scanned` 不動、等 ~10min；terminal 停在
`walk_roi root='iMerge_Top' layer=6/0` 之後無 `done`。

**根因（DEBUG 專屬）：** walk() 的 DEBUG cross-check 對**每個 visited cell** 呼叫
`load_cell_bbox(cid)`（CE-violation 檢查，M3.5e.3 舊診斷）。撞到「沒有 CE 矩形」的
大 cell 時，`_decode_bbox_at` 一路 decode 到 cell 結尾 = 全 body（可達百萬 records）
→ 卡死在單顆。`_n_loaded` 因此卡在個位數不動。與 F13 短路無關，是 `--debug` 才觸發
的二次全 decode。

**修復：**
- walk() DEBUG cross-check 改：有 `std_bbox` 的 cell 走**免費**檢查（已載入的
  `content.bbox` ⊇ `std_bbox` lookup，O(1)）；只有**無 std_bbox** 的 cell 才做
  `load_cell_bbox` 的 CE 檢查，且抽樣上限 200 次（診斷非正確性需求）。
- 加 tracing：walk 起始印 `prune source: std_bboxes=.. root_std_bbox=.. bbox_layer=..`
  （可確認 M2 短路是否生效）；`load_cell` 單顆 >1s 印 `SLOW load_cell` + spec/placement
  數（定位卡點是哪顆大 cell）。

**待釐清：** 若非 --debug 仍慢，瓶頸可能是 `walk()` 對 ROI 命中的**巨大 flat cell**
做 `load_cell` 全 decode（cell 內 geometry 無空間索引，F13 樹剪枝幫不上）——
等 user 回報 `SLOW load_cell` / `prune source` 行確認。也需確認 user 已 pull M2 短路。

**測試：** `py_compile` 過。**影響檔案：** `glas/core/oasis_random.py`。
**Branch：** `claude/magical-davinci-Ibo8K`

---

## [2026-06-02] [F13] M2：S_BOUNDING_BOX 接上 reachable_bbox 剪枝（格式已確認）

**變更類型：** 新功能 M2（core 剪枝短路 + 測試）· **狀態：待 user 真檔 GUI 驗收**

**格式確認（雙重）：** (1) KLayout 源碼 `dbOASISWriter.cc` 依序 push
`flag, bbox.left, bbox.bottom, bbox.width, bbox.height` → operand =
`[flag, x, y, w, h]`（cell-local grid）。(2) user 真檔 `R8_OD_to_VC_KKKK.oas`
（unit=2000、292,883 cells **全有** S_BOUNDING_BOX）root `iMerge_Top`
raw=`[0,0,0,7460112,2204400]` → nm `(0,0,3730056,1102200)`=3.73×1.10mm 合理 die。

**實作：**
- `oasis_random.std_bbox`：flag 判斷 `& 1` → `!= 0`（KLayout 用 0x2 標退化/依賴
  external 的無效 box；非零一律回 None → 該 cell fallback，不漏幾何）。docstring
  改「assumed」為 KLayout 源碼確認。
- `reachable_bbox`（walk_roi closure）與 `_reachable_bbox`（method）：開頭加
  `std_bbox(cid)` 短路——命中直接回傳並寫 `_reach_memo`，**不 load_cell_bbox、不
  遞迴**。未命中走既有 CE/full-decode fallback（§7 不變式：命中才短路→不退化）。
  全 cell 都有 bbox 時，root 一次查詢即完成整個剪枝，walk 只 full-decode ROI 內少數
  cell → 無 CE 層大檔首次 walk 從 10min+ 變秒級。
- walk() DEBUG cross-check：對已 full-decode 的 cell 驗 `std_bbox ⊇ own_bbox`，
  違反印 `SBBOX-VIOLATION` + `sbbox_violations` 計數，`sbbox_used` 計命中（抓格式錯）。

**測試：** `py_compile` 全過；新增 `tests/test_oasis_random.py::TestStdBboxPrune`
4 例（雙 property 合成檔：map 收集/換算、短路免遞迴〔std_bbox 故意放大證明〕、
flag!=0 fallback 回幾何、無 property→has_std_bboxes False）。`std_bbox` 換算另以
stdlib 獨立驗證。沙箱無 numpy → pytest 待 user 本地跑。

**影響檔案：** `glas/core/oasis_random.py`、`tests/test_oasis_random.py`、
`docs/plans/F13-sbbox-prune.md`。 **Branch：** `claude/magical-davinci-Ibo8K`

---

## [2026-06-02] [F13] M1：讀 KLayout per-cell S_BOUNDING_BOX（收集 + accessor + 診斷）

**變更類型：** 新功能 M1（core 收集 + accessor + app 診斷，純加法，未動剪枝）·
**狀態：待 user 開檔回報 root 的 S_BOUNDING_BOX raw 數值以確認格式**

**動機：** user 用 KLayout strict「Save As → OASIS」勾 `Standard properties =
Global + per cell bounding box` 重存 `R8_OD_to_VC_NEW.oas`，GLAS 開檔已確認
`S_BOUNDING_BOX present: True`。此 property = 每 cell 含 placement 展開後的整體
bbox（cell-local grid），若讀進來可讓 `reachable_bbox` 直接回傳、免 CE 層免遞迴免
decode，把無 CE 層大檔首次 walk_roi 從 10min+ 變秒級（接續 2026-05-29 [B] 的
「有 → 實作剪枝」分支）。

**實作（M1，純加法、零剪枝改動、不退化）：**
- `oasis_streamer.scan_cell_offsets` header（offset_flag==0）與 tail（==1）兩路徑：
  既有 S_CELL_OFFSET 的 PROPERTY 迴圈加 `elif name == S_BOUNDING_BOX` 收前 5 個
  integer operand，回傳新增 `sbbox_by_refnum` / `sbbox_by_name`（raw int list）。
- `oasis_random.RandomAccessReader`：載入該 map（`_sbbox_by_*`）+ `has_std_bboxes()`
  / `std_bbox_raw()` / `std_bbox()`（假設格式 `[flag,x,y,w,h]` → cell-local grid
  bbox，flag bit0=空 cell→None）。**尚未接進 reachable_bbox**（M2，待格式確認）。
- `gds_align_tool._on_open_roi`：開檔印「per-cell S_BOUNDING_BOX read: N」；Pick root
  後印 root 的 `raw=... → grid bbox → nm`，供 user 對照 KLayout 該 cell bbox 確認
  operand 格式（順序/單位/frame）。

**為何兩階段：** operand 格式若猜錯且偏小→剪枝漏幾何（§7 對位正確性風險），故 M1
先用真檔數值確認格式，M2 才接 `reachable_bbox` 短路（命中才短路、未命中走既有
CE/full-decode fallback→不退化）+ DEBUG `std_bbox ⊇ own-bbox` cross-check。

**測試：** `py_compile` 三檔全過；`std_bbox` 換算邏輯以 stdlib 獨立驗證（含 flag
空 cell / operand 不足 / 負座標）。沙箱無 numpy/pytest → 既有 pytest 待 user 本地跑
（M1 對既有路徑純加 return key + elif 分支，不影響 S_CELL_OFFSET 收集）。

**影響檔案：** `glas/core/oasis_streamer.py`、`glas/core/oasis_random.py`、
`glas/app/gds_align_tool.py`、`docs/plans/F13-sbbox-prune.md`（新）。
**Branch：** `claude/magical-davinci-Ibo8K`

---

## [2026-05-29] [B] ROI 開檔 freeze 診斷 + walk_roi 慢根因（無 CE 層）+ S_BOUNDING_BOX 探查

**變更類型：** 診斷（core + app，純加 log/accessor）·  **狀態：待 user 回報 propnames**

**背景：** offset_flag=1 修正後測試 141 全過，但 user 開實際 KLayout strict 檔
（`R8_OD_to_VC_NEW.oas`，1.84 GB，27425 cells）時三個現象：

1. **Scan layers 列不出 layer** — 該檔**根本沒有 LAYERNAME 表**（KLayout 也只顯示數字
   如 17/101、6/0，無 name），非 bug；「Pick ROI layers」對話框可直接手動輸入數字載入。
2. **開檔/Pick root cell freeze** — 加 `[open-roi]` 計時 print 後，user 回報已不卡；
   只剩按 Pick root cell 時 `QInputDialog` 塞 27425 names 的下拉短暫凍結（次要，待輕量化）。
3. **walk_roi 載入 10min+ 還沒好（UI 不卡，背景在跑）** — 根因確認＝**此檔無 CE 邊界層
   (108/250)**。`_decode_bbox_at` 找不到 bbox_layer 矩形 → 每 cell 解到結尾算 own bbox，
   `reachable_bbox` 遞迴把整棵 27425-cell 樹全解 ≈ 全 chip 解碼。即 [F12] 撤案記錄的根本問題。

**救星方向：** KLayout strict 可為每 cell 寫 `S_BOUNDING_BOX` property（per-cell bbox）。
若此檔有，可直接讀它剪枝、免 CE 層、walk_roi 秒級。為確認存在性：`scan_cell_offsets`
回傳新增 `propnames`（檔尾/檔頭 PROPNAME 表所有名稱，offset_flag 0/1 皆收），`_on_open_roi`
開檔時 print 出來（並標 `S_BOUNDING_BOX present: T/F`）。待 user 回報後決定正式方案
（有 → 實作 S_BOUNDING_BOX 剪枝；無 → 建議 KLayout 重存勾 write cell bounding boxes，
或一次性 bbox sidecar）。**屬新功能，將走 §2.3 規劃流程。**

**測試：** py_compile 全過；合成檔驗證 `propnames` 回傳（含 S_CELL_OFFSET）。

**影響檔案：** `glas/core/oasis_streamer.py`、`glas/app/gds_align_tool.py`。
**Branch：** `claude/magical-davinci-Ibo8K`

---

## [2026-05-29] [B] 修 GLAS 讀不到「offset_flag=1（索引表在檔尾）」OASIS

**變更類型：** Bug fix（core + app）

**現象：** user 用 KLayout「Save As → OASIS (Strict mode)」把無索引檔轉成帶索引的
`R8_OD_to_VC_NEW.oas`（1.84 GB）後，GLAS「Open OASIS (ROI)」跳「This OASIS has no
S_CELL_OFFSET index…」。F10 診斷顯示 `offset_flag: 1`、`CELL(by refnum) x 27425`，檔頭
50 萬筆內 `CELLNAME = 0`——索引「有」，只是 GLAS 沒讀到。

**根因：** `scan_cell_offsets`（oasis_streamer.py）是「從檔頭 iter_records、碰第一個
CELL 就 break」，假設名稱表 / S_CELL_OFFSET 在檔頭（offset_flag=0，Calibre）。SEMI P39：
offset_flag=1 時各表位置記在 **END record**、表在**檔尾**（KLayout strict）。此時掃到第一個
CELL 時 CELLNAME 一筆都沒讀到 → `by_refnum` 空 → 回報 no S_CELL_OFFSET。app 的
`_scan_layers_main` 同樣「碰 CELL 就停」，讀不到檔尾 LAYERNAME。

**修復（純加法，不動 offset_flag=0 既有路徑，遵守 §7）：**
- `oasis_streamer.py`：新增 `_peek_start`（不擾動位置讀 START → 取 offset_flag）；
  `scan_cell_offsets` 開頭 dispatch：offset_flag==1 → `_scan_tail_tables`。新增
  `_read_end_table_offsets`（END 固定 256 bytes、在 `size-256`；含尾端掃描 fallback +
  健全性檢查）解出 6 對 (strict, byte_offset) 表位置、`_iter_table_at`（自指定 offset 讀單一
  表、碰非該表 record 即停，不越界到下一表）、`_scan_tail_tables`（依序讀 PROPNAME→CELLNAME
  〔含 S_CELL_OFFSET PROPERTY〕→LAYERNAME，回填 by_refnum/by_name/layernames）。
  byte_offset==0 視為該表不存在安全略過。
- `gds_align_tool.py` `_scan_oas_with_streamer`：header 掃不到 layer 時 fallback 用
  `scan_cell_offsets(p, use_mmap=True)`（mmap 避免 1.84 GB slurp）取檔尾 LAYERNAME 回填清單。

**測試：** `py_compile` 全過。沙箱無 numpy/pytest → 用合成 offset_flag=1 OASIS（START
flag=1 → 2 cells → 檔尾 PROPNAME/CELLNAME+S_CELL_OFFSET/LAYERNAME → END 帶 6 對 offset，
padding 至 256）以純 stdlib 腳本驗證 `scan_cell_offsets` 三種開法（slurp / mmap /
shared_buf＝RandomAccessReader 實際用法）皆正確回 by_refnum/by_name/layernames+unit，且
`verify_cell_offsets` 確認 offset 落在 CELL record；offset_flag=0 路徑回歸不變。tests/
新增 `TestCellOffsetIndexTailTables`（3 例：讀檔尾索引 / offset 落點 / 空表 fallback），
以 pytest stub 跑過。**真檔（R8_OD_to_VC_NEW.oas）GUI 端到端待 user 本地 pytest + 開檔驗收。**

**注意：** 此檔「有」S_CELL_OFFSET，非 F12（完全無索引、已撤案）；本修正只讓 GLAS 正確讀
「索引表在檔尾」的合法 OASIS，未引入 F12 的自建索引 / skip-cblock / reach cache。

**影響檔案：** `glas/core/oasis_streamer.py`、`glas/app/gds_align_tool.py`、
`tests/test_oasis_streamer.py`。 **Branch：** `claude/magical-davinci-Ibo8K`

---

## [2026-05-29] [F11] 修 whole-chip 匯出 `NameError` + ROI 開檔後左側 layers 提示

**變更類型：** Bug fix + UX 微調（app）

**現象：** 選 scope=Whole chip 匯出 OASIS 時，`WholeChipExportWorker.run()` 在
`with oasis_writer.OasisStreamWriter(...)`（gds_align_tool.py:1172）丟
`NameError: name 'oasis_writer' is not defined`。F11 M2 加了 worker 卻漏 import
core 的 `oasis_writer` 模組（FOV 匯出走 `layout_export`，內部自帶 import，所以沒被發現）。

**修復：** 在 app 的 core import 區（layout_export 之後）補 `import oasis_writer`。

**ROI 左側 layers 提示（UX）：** user 回報 Open OASIS→Scan→Pick root cell→OK 後，左側
LAYERS 仍空（且舊 placeholder 寫「Open an OASIS」，誤導）。釐清這是 ROI 隨機存取 **lazy
load 的正常行為**（幾何要等點 SEM 圖、座標設定後才 decode），維持不變；但加
`LayerPanel.show_roi_pending(layer_keys)`：picked root cell 後左側即列出所選 layer（標
「loads on click」）+ 下一步提示「set Coordinate Setup → click a defect image」，讓 user
知道接著要按什麼。於 `_on_open_roi` 設定 `_roi_layers` 後呼叫。

**PR #8 review 修正（Codex P2）：** `show_roi_pending` 清掉本地 `_poi_entries` 卻沒 emit
`pois_changed([])`（`set_document` 清空時有 emit）。開新檔時 `MainWindow._poi_entries` /
Fine Align panel 會殘留前一個檔的 POI，preview/batch 走 `_poi_layers()`/`_poi_specs()` 在
首次點 SEM 圖載入新幾何前可能對舊 POI fine-align。修法：`show_roi_pending` 末尾補
`self.pois_changed.emit([])`，比照 `set_document`。

**測試：** `python3 -m py_compile glas/app/gds_align_tool.py` 通過（沙箱無 PyQt6，
無法跑 GUI 端到端，待 user 本地驗收 whole-chip 匯出 + 左側提示顯示）。

**影響檔案：** `glas/app/gds_align_tool.py`。 **Branch：** `claude/magical-davinci-Ibo8K`

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
