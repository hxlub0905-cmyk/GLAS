# Session Log

> 紀錄原則：每 (日期, 任務) 一條；同天同 task 的多次來回已合併。完整逐 commit 細節見 git history。

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
