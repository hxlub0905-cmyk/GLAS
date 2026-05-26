# Session Log

---

## [2026-05-26] [F6] 等價測試本地全綠（170 passed），勾 M4 測試 checkbox

**變更類型：** 文件（測試驗收記錄，無程式碼變更）

**動機現象：** F6 加速（mmap 讀取 / 單一 map 共享 / thread-pool 批次）等價測試先前只在沙箱跑過
numpy-free 子集。user 本次在本地 Python 3.9.7 跑
`pytest tests/test_accel_equivalence.py tests/test_oasis_random.py tests/test_oasis_streamer.py -v`
→ **170 passed**（含三組等價：mmap↔slurp、共享map↔獨立scan、循序↔4-worker 並行）。

**修復實作：** F6 plan M4「本地 pytest 全綠」與「驗證方式」對應 checkbox `[ ]→[x]`、標註日期與
170 passed；M4 status 更新為「等價測試本地全綠；實機效能/GUI/mmap 記憶體驗收待本地」。**F6 仍留在
§8**——尚缺實機效能、GUI 批次加速、大檔 mmap 記憶體下降的本地驗收。

**測試：** 即本次驗收事件本身（170 passed）。無程式碼變更。

**影響檔案：** `docs/plans/F6-readwalk-batch-accel.md`、`SESSION_LOG.md`。

**Branch：** `claude/practical-pascal-AtKLm`

## [2026-05-26] 完成 [F5]：本地驗收通過，收尾 plan + §8

**變更類型：** 文件（任務收尾，無程式碼變更）

**動機現象：** F5（fine-align 診斷 + 工作流，M1–M6）程式碼早已完成，先前狀態為「待 user 本地 GUI
驗收」。user 本次 session 回報本地實機驗收（單張 Preview before/after 貼合、Run all 總覽表排序/篩選/
點列跳轉、直方圖/散點、median→δ 套用後重跑殘差收斂）**全部通過**。

**修復實作：** 依 CLAUDE.md §10 收尾——`docs/plans/F5-finealign-diagnostics.md` 將「所有 milestone
checkbox 已勾」「手動本地驗證」兩項 `[ ]→[x]`、6 個 milestone status 標註「2026-05-26 user 本地驗收
通過」；`CLAUDE.md` §8 移除 [F5] 條目（plan 檔保留作 design history）。

**測試：** 無程式碼變更（純文件收尾）。

**影響檔案：** `docs/plans/F5-finealign-diagnostics.md`、`CLAUDE.md`、`SESSION_LOG.md`。

**Branch：** `claude/practical-pascal-AtKLm`

## [2026-05-25] [F6] PR#5 review fix（P1）：批次 cancel 後保留所有已完成結果

**變更類型：** bug fix

**動機現象：** PR#5 review（chatgpt-codex-connector，P1）指出 `FineAlignAllWorker.run()` 在
`as_completed` 迴圈裡一旦 `self._cancel.is_set()` 就 `break`，會丟掉其他**已完成**（或在 pool
`__exit__` 等待期間完成）的 future 結果——那些影像即使運算已完成也不會 emit `result`、不進
`_refined`/結果表。workers>1 時 cancel 行為變得不確定，且違反「partial results kept」的設計意圖。

**修復實作：** 移除 `break`，改成**一律 drain 所有 future**。cancel 一旦設定，未開始的 task 在
`_fine_align_image` 開頭檢查 `cancel_is_set()` 立即回 None、進行中的 walk 經 `cancel_cb` 快速 bail
（仍保持即時反應），但已完成 future 仍會 emit 結果 → 「保留已完成結果」對 workers>1 變確定性。
逐 future 用 `try/except oasis_random.WalkCancelled` 包 `fut.result()`，bail 的 walk 視為無結果、
不中斷整個迴圈。

**測試：** `py_compile` 過。沙箱無 PyQt6 → cancel 路徑互動待 user 本地驗（非 cancel 路徑等價測試不受影響）。

**影響檔案：** `glas/app/gds_align_tool.py`。

**Branch：** `claude/dazzling-cori-5T7XE`（PR #5）

## [2026-05-25] [F7] 實作 M1–M4：Batch 工作區 + inline 進度 + 進度條質感（待本地驗收）

**變更類型：** 功能（UI/UX，運算不變）

**內容：** 依核准的 F7 plan（規劃期間 user 反映「Batch 放 View 那排怪」，Q&A 改為**動作進入+返回鈕**，
不放 segmented）實作四個 milestone：
- **M1 進度條質感**：`_AnimatedBar` 升級——橘→深橘垂直漸層 + 軟外發光（外擴低 alpha rounded rect）+
  determinate 時條內置中 % 數字（先深色全畫、再白色 clip 到填充，兩種底都可讀）+ bar 加高 14→20px、
  更圓潤、保留掃光帶。API 不變 → 全 app 進度條（OASIS 載入/overlay/ROI）同步變精緻。
- **M2 `BatchResultsPanel`**：新 QWidget，把舊 `FineAlignResultsDialog` 的 summary/only-low 篩選/sortable
  表/`_ScoreHistogram`/`_ResidualScatter`/median 鈕搬入；對外 `set_rows(rows,threshold)`；頂部 inline 進度區
  （`_AnimatedBar`+spinner+done/total/%/Elapsed/ETA+Cancel，閒置隱藏）；signals image_activated/
  apply_median_requested/cancel_requested/back_requested。
- **M3 Batch 工作區**：`_center_split` 變 [canvas, batch_panel, sem_viewer]；新增 `_enter_batch_workspace()`
  （記 `_prev_view_mode`、隱藏 canvas/minimap、左結果≈55%/右 SEM≈45%）與 `_exit_batch_workspace()`；
  `_set_view_mode` 開頭一律隱藏 batch_panel（點任一 View 鈕即離開）+ gds 模式 setSizes 改 3 值；
  點結果列 → `_on_sem_image_selected` 就地換右側 overlay、不離開工作區。
- **M4 批次接線改 inline**：`_on_run_fine_align_all` 改 `_enter_batch_workspace()`+`start_progress()`，
  **不再開 modal `LoadProgressDialog`**；`_on_fa_progress`→panel.set_progress、`_on_fa_result`→更新
  `_refined`/badge 並 `_refresh_batch_panel()`（streaming 重填）；finished/cancelled/failed→`end_progress`
  + 重填、保留部分結果；cancel 由 panel 按鈕經 `_on_fa_cancel_clicked` 直接 `worker.cancel()`（threading.Event
  即時）；「Results…」鈕（`_open_fa_results`）改為進工作區+重填。移除已無用的 `FineAlignResultsDialog`
  類別與 `_fa_progress`/`_fa_results_dlg` 屬性。**OASIS 載入/overlay 匯出仍用原 modal 進度。**

**不動：** F6 批次運算與結果值、fine-align 符號、SemViewer 折疊、CE early-stop（§7 不變式）；median→δ 機制。

**測試：** `py_compile` 全過。沙箱無 PyQt6/numpy/cv2 → GUI/外觀/互動（進度條漸層發光%、Run all/Results…
進工作區、inline 進度+ETA、streaming 表、即時 cancel、點列就地換 overlay、← 回對位、四 view 切換）
待 user 本地驗收；`pytest tests/test_gds_align_f5.py`（純函式）不受影響、待本地跑。

**影響檔案：** `glas/app/gds_align_tool.py`、`docs/plans/F7-batch-workspace-ui.md`、`CLAUDE.md`。

**Branch：** `claude/dazzling-cori-5T7XE`

## [2026-05-25] [F7] 規劃：批次對位工作區（Batch view-mode + inline 進度 + 進度條質感）（待核准）

**變更類型：** 文件（plan，尚未動工）

**內容：** user 提出批次對位的 UI/UX 想改善（批次結果是否該有專屬畫面、進度條不夠質感）。探索現有
view-mode（`_VIEW_MODES`+`QButtonGroup`+`_set_view_mode` 切 `_center_split`）、`FineAlignResultsDialog`、
`_AnimatedBar`/`LoadProgressDialog`、批次接線後，用 AskUserQuestion 收斂三個岔路：(1) 批次結果放
**第四個 view-mode「Batch」**（中央左=結果表/直方圖/散點、右=SEM overlay、點列就地換 overlay）、
(2) 批次跑時 **inline 進度+結果 streaming**（取代 modal）、(3) 進度條 **漸層+發光+條內 %**。產出
`docs/plans/F7-batch-workspace-ui.md`（5 milestone：M1 `_AnimatedBar` 質感、M2 抽 `BatchResultsPanel`、
M3 加 Batch view-mode、M4 批次接線改 inline、M5 收尾）。強調純 UI 重新安置+視覺，**不動 F6 批次運算
與 §7 不變式**。§8 註冊 [F7]。**待 user 核准後才開工。**

**測試：** 無（純 plan）。

**影響檔案：** `docs/plans/F7-batch-workspace-ui.md`、`CLAUDE.md`（§8）、`SESSION_LOG.md`。

**Branch：** `claude/dazzling-cori-5T7XE`

## [2026-05-25] [F6] 實作 M1–M3：mmap 讀取 + 單一 map 共享 + thread-pool 批次（待本地驗收）

**變更類型：** 功能（效能加速，行為不變）

**動機現象：** ROI 模式開大檔整檔 slurp 進 RAM（`oasis_streamer.py` `OasisStream`）、`RandomAccessReader`
開檔 slurp 兩次、批次 fine-align（`FineAlignAllWorker`）刻意單執行緒。plan F6 已核准。

**修復實作：**
- **M1 mmap-backed OasisStream**：`OasisStream(base=None, *, use_mmap=False, shared_buf=None)`，`use_mmap`
  且 base 有 fileno → `mmap.mmap(ACCESS_READ)`；BytesIO/平台/空檔 → fallback `base.read()`（行為不變）。
  `close()` 釋放 mmap + 持有的 base。`OasisReader`/`scan_cell_offsets` 加 `use_mmap`（預設 False，bulk
  decode 路徑維持 slurp）；`RandomAccessReader` 內部走 mmap。
- **M2 單一 map 共享**：`shared_buf` 路徑讓 `OasisStream/OasisReader/scan_cell_offsets` 包外部擁有的
  buffer（各自 `_pos`，close 不關共享 map）；`RandomAccessReader.__init__` 建一個 owning mmap，`_buf`
  同時給 persistent reader 與 scan → 檔案只 map 一次、offset index 只算一次。新增
  `RandomAccessReader.close()` / `__enter__/__exit__`（先關 wrapper 再關 owning map，idempotent）。
- **M3 thread-pool 批次**：抽出純函式 `_fine_align_image(...)`；`FineAlignAllWorker.run()` 改
  `ThreadPoolExecutor(max_workers=min(cpu_count,8))`，每 worker thread 經 `threading.local` 用
  `RandomAccessReader.clone()` 取私有 reader（私有 _memo/cursor；mmap 由 OS 共享實體頁，不耗 N× RAM），
  零共享可變狀態 → 結果與循序逐值相同。signal 由 run() 單一 thread 在 future 完成時 emit；cancel 沿用
  `threading.Event`（task 起點 + walk cancel_cb 讀 is_set，cancel 後保留已完成結果）。
  **cv2 執行緒設定維持預設不動**（避免改變 score 數值，保證 golden 等價）。

**測試：** 新增 `tests/test_accel_equivalence.py`（mmap↔slurp 的 read/iter_records/scan 等價、共享map↔
獨立scan 等價、`RandomAccessReader` close idempotent、循序↔4-worker pool 每張 result tuple 等價）。
**沙箱實跑 numpy-free 等價檢查全通過**（OasisStream/iter_records/scan/shared-map）；numpy/cv2/PyQt6-gated
項（load_cell 幾何、批次並行）與實機效能、GUI 批次加速、mmap 記憶體下降待 user 本地驗收。py_compile 全過。

**影響檔案：** `glas/core/oasis_streamer.py`、`glas/core/oasis_random.py`、`glas/app/gds_align_tool.py`、
`tests/test_accel_equivalence.py`、`docs/plans/F6-readwalk-batch-accel.md`。

**Branch：** `claude/dazzling-cori-5T7XE`

---

## [2026-05-25] [F6] 規劃：OAS 讀取 + 批次 fine-align 加速（待核准）

**變更類型：** 文件（plan，尚未動工）

**內容：** user 要求在「功能完全不變」前提下找 OAS 讀取與批次 fine-align 的加速點。探索熱路徑後
（`OasisStream` slurp `oasis_streamer.py:214`、`RandomAccessReader` 雙重 slurp `oasis_random.py:215`、
`FineAlignAllWorker` 刻意單執行緒 `gds_align_tool.py:1116`）與 user 用 AskUserQuestion 收斂三個岔路：
(1) 批次平行化用 **thread pool**（per-thread reader + 共享 mmap；cv2 釋放 GIL）、(2) worker 數
**自動**（cpu_count 上限 8）、(3) mmap **只用在 ROI/隨機存取路徑**（bulk decode 維持 slurp）。
產出 `docs/plans/F6-readwalk-batch-accel.md`（4 milestone：M1 mmap-backed OasisStream + fallback、
M2 單一 map 共享去雙重 slurp、M3 thread-pool 批次、M4 等價總驗收），核心驗收條件為 **golden-output
逐 byte／逐數值等價測試**（mmap↔slurp 幾何相等、共享map↔獨立scan index 相等、循序↔並行批次結果
相等），強調不改演算法與 §7 不變式。§8 註冊 [F6]。**待 user 核准後才開工。**

**測試：** 無（純 plan）。

**影響檔案：** `docs/plans/F6-readwalk-batch-accel.md`、`CLAUDE.md`（§8）、`SESSION_LOG.md`。

**Branch：** `claude/dazzling-cori-5T7XE`

---

## [2026-05-25] [F5] PR#4 review fix：非 ok 批次狀態清掉舊 refined offset

**變更類型：** bug fix

**動機現象：** PR#4 review（P1）指出 `_on_fa_result` 對每張影像更新 `_fa_meta`，但只在
`status == "ok"` 時寫 `_refined`。若某影像先前對位成功、後續 run 回 `flat`/`missing-file`/
`no-scale`/`no-coords`，舊 offset 仍留在 `_refined`，jump/overlay/export 經 `_refined_offset()`
會繼續用過期對位渲染/匯出失敗影像。

**修復實作：** `_on_fa_result` 在非 ok 分支 `self._refined.pop(image_id, None)` 並呼叫新增的
`SemPanel.clear_score(image_id)`（移除 score badge；對本來就無座標的影像保留「no coords」標記）。

**測試：** `py_compile` 過。sandbox 無 PyQt6 → GUI 待 user 驗。

**影響檔案：** `glas/app/gds_align_tool.py`。

**Branch：** `claude/sharp-lamport-YIk3z`（PR #4）

---

## [2026-05-25] [F5] 實作完成（M1–M6，待 user 本地驗收）

**變更類型：** 功能（新功能 + bug fix）

**內容：** fine-align 診斷 + 工作流六個 milestone 全數實作（plan 已核准）：
- **M1 殘差疊圖**：新增純函式 `overlay_outlines_on_sem(sem_gray, entries, anchor, nm_per_px)`
  （對齊 `rasterize_layer` 的 X 右 / Y-flip 慣例，cv2.polylines + numpy fallback `_draw_polyline_np`）；
  `TemplatePreviewDialog` 由 3 格擴成含「Overlay·before（coarse）/ after（coarse+refined）」共 5 格
  （grid 每列 3 格自動換行）；`_on_preview_template` 備好 before/after anchor + entries。
- **M2 批次總覽**：新增 `FineAlignResultsDialog`（可排序表格 image_id/score/dx/dy/used_radius/status、
  score 上色、「只看低於 threshold」篩選）+ 自繪 `_ScoreHistogram`（含 threshold 線）+ `_ResidualScatter`
  （原點十字 + median 標記）；純函式 `fine_align_result_rows` / `score_histogram` / `residual_median`。
  `FineAlignAllWorker.result` signal 擴成 `(image_id,dx,dy,score,used_r,status)`，每張影像都回報
  狀態（ok/no-coords/missing-file/no-scale/flat）；新增 `self._fa_meta`；單張路徑也存 used_r/status。
  `Run all` 完成後自動開窗，Fine Align 面板加「Results…」可重開；雙擊列跳到該影像。
- **M3 中位殘差→δ**：對話框「Apply median residual to origin δ」鈕 → `_on_apply_median_residual`
  把 ok/low-score 影像的 median(dx,dy) 加進 `_origin_dx/dy`（與 refined 同號、沿用 Set Offset 機制），
  不清空既有 `_refined`，提示重跑。
- **M4 setup 持久化 + 命名**：`BG grey`→`Background GL`、每列 `FG`→`Foreground GL`（不動 `values()` key）；
  新增 `self._fa_setup`，`_capture_fa_setup`（載新 ROI 前快照 visible/colour/opacity/POI/FG）+
  `_apply_fa_setup`（`_on_roi_finished` recompute 後依 layer key 還原、缺層略過、不自動 run）；
  `LayerPanel.check_pois` / `FineAlignPanel.set_fgs` 還原 POI 勾選與 FG。
- **M5 cancel 即時生效 + ETA**：`FineAlignAllWorker` cancel 改 `threading.Event`，`cancel_requested`
  以 `DirectConnection` 在 GUI 執行緒直接 set（修「按終止仍一直跑」根因）；`LoadProgressDialog` 先前
  已加自繪 `_AnimatedBar` + `set_progress`（done/total/%/elapsed/ETA）；cancel 後保留部分結果。
- **M6 overlay+image 匯出**：`AlignmentExportDialog` 加「Raw SEM PNG / Aligned overlay PNG」勾選 +
  `selected()` 多回傳兩旗標；新增 `OverlayExportWorker`（逐張輸出 `<id>_raw.png` / `<id>_overlay.png`
  到選定資料夾，overlay 用 M1 helper 在 coarse+refined anchor 描 POI 輪廓、RGB→BGR 翻轉再 imwrite）+
  manifest（`overlay_manifest.csv/.json`，schema `mmh-gds-overlay-v1`，image_id join）；`_safe_name`。

**測試：** `py_compile` 全過。新增 `tests/test_gds_align_f5.py`（score_histogram / residual_median /
fine_align_result_rows status / _safe_name / overlay_outlines_on_sem 落點 / manifest 寫出）。
**sandbox 無 PyQt6/numpy/cv2 → pytest 與 GUI 未跑**，全部 GUI/互動待 user 本地驗收
（preview before/after、Run all 進度條/ETA、即時 cancel、Results 表/直方圖/散點、median→δ、
切 DID 保留 setup 按一次即可、overlay PNG + manifest 匯出）。

**影響檔案：** `glas/app/gds_align_tool.py`、`tests/test_gds_align_f5.py`、
`docs/plans/F5-finealign-diagnostics.md`、`CLAUDE.md`。

**Branch：** `claude/sharp-lamport-YIk3z`

## [2026-05-25] [F5-M5部分] Batch 進度條動態化 + ETA

**變更類型：** 功能（UI）

**動機現象：** batch（Run all）進度對話框只有 Braille spinner + elapsed，缺乏「正在做事」的
實感與剩餘時間。user 要求進度條精美、有動態感。

**修復實作：** `glas/app/gds_align_tool.py` 新增自繪 `_AnimatedBar`（QWidget）：圓角軌道 +
橘色填充，填充上有一條淺色光澤帶隨 `advance()`（每 120ms tick）掃過 → 動態感；支援
determinate（依 fraction 填充）與 indeterminate（滑塊 ping-pong，給 ROI 載入等未知總量階段）。
全自繪不受 app QSS 影響（解掉先前避用 `QProgressBar` 的 chunk 動畫被壓平問題）。
`LoadProgressDialog` 嵌入該 bar + 新增 `set_progress(done,total)`：切 determinate 並由
`_refresh_detail` 顯示「done/total · NN% · Elapsed m:ss · ETA m:ss」（ETA 用 elapsed/done×剩餘）。
`_on_fa_progress` 改呼叫 `set_progress`，title 顯示當前 image_id。對應 F5 M5 的 ETA checkbox 勾掉。

**注意：** M5 其餘兩項（threading.Event 即時 cancel、cancel 後 results 預覽）尚未做。

**測試：** `py_compile` 過。沙箱無 PyQt6 → GUI 動畫/ETA 待 user 本地驗。

**影響檔案：** `glas/app/gds_align_tool.py`、`docs/plans/F5-finealign-diagnostics.md`。

**Branch：** `claude/sharp-lamport-YIk3z`

## [2026-05-25] [F5] 規劃再擴充：納入 setup 持久化 / cancel 修復 / overlay 匯出 / 命名（6 milestone，待核准）

**變更類型：** 文件（plan，尚未動工）

**內容：** user 回饋四項 fine-align 工作流痛點，探索後收斂並擴充 F5：
(#1) `BG grey`→`Background GL`、`FG`→`Foreground GL`（命名）；
(#2) 切 DID 走 `set_document(新doc)` 用全新 `LayerEntry` 重建 → POI 勾選/可見性/顏色遺失，
決策「記住 setup、新 doc 自動重套、user 手動按一次 Run fine align」；
(#3) batch cancel 跑不停根因 = worker 緊迴圈內 `cancel()` 是 queued slot 永不執行，
決策改 `threading.Event` 直接旗標 + 進度對話框 ETA + cancel 後保留已完成結果；
(#4) overlay+image 匯出，決策「可勾選 raw / 對位後 overlay PNG / 兩者 + manifest（image_id join）」
供 MMH 串接，複用 M1 overlay helper。plan 由 3 → 6 milestone（新增 M4 持久化+命名、M5 cancel/ETA、
M6 overlay 匯出）。§8 條目同步。**待 user 核准後才開工。**

**影響檔案：** `docs/plans/F5-finealign-diagnostics.md`、`CLAUDE.md`。

**Branch：** `claude/sharp-lamport-YIk3z`

---

## [2026-05-25] [F5] 規劃擴充：診斷範圍收斂為 3 milestone（待核准）

**變更類型：** 文件（plan，尚未動工）

**內容：** 探索 fine-align 子系統後與 user 收斂 F5 範圍。除原 M1 殘差疊圖、M2 批次結果表，
納入 C1 殘差散點圖、C5 score 直方圖（並入 M2）、C3 狀態/失敗原因 + C4 used radius（表格欄）、
C2 中位殘差→origin δ 一鍵套用（新 M3）。改寫 `docs/plans/F5-finealign-diagnostics.md`
為 3 milestone；§8 F5 條目同步。**待 user 核准後才開工。**

**影響檔案：** `docs/plans/F5-finealign-diagnostics.md`、`CLAUDE.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

---

## [2026-05-25] 完成 [F4] + [F1]：user 驗收通過，§8 結案

**變更類型：** 文件（任務結案，無程式碼變更）

**內容：** user 本地驗收 [F4]（Boolean 食譜化重算 + 巢狀 + 編輯 + 對話框內嵌預覽 + 方向性
W/H morphology + coordinate setup 版面修復）與 [F1]（互動驗收：SEM↔GDS 對位 / 拖動 δ /
fine-align / 批次 Run all / 匯出 / 折疊 UX）皆 OK。從 `CLAUDE.md` §8 移除 [F4]、[F1]；
`docs/plans/F4-boolean-enhance.md` 標 done、勾完驗收 checkbox。§8 進行中僅剩 [F5]（待核准）。

**影響檔案：** `CLAUDE.md`、`docs/plans/F4-boolean-enhance.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

---

## [2026-05-25] [F4] 方向性 W/H morphology + coordinate setup 版面溢出修復

**變更類型：** 功能（語意變更）+ bug fix

**動機現象：** (1) `A > W:10` 看起來把高度也加大 —— 原本 morph 是**等向** buffer，W/H 只是
標籤、`>`/`<` 為 grow/shrink 全方向。user 要 W/H 變成**方向性**（W=寬/X、H=高/Y）。
(2) Coordinate Setup 輸入第一個值後整個面板暴寬、超出 UI。

**修復實作：**
- **方向性 W/H（`gds_boolean.py`）**：`> W:n`=只長寬、`> H:n`=只長高、`< W:n`/`< H:n`=各軸縮，
  每邊各 ±n nm（總 ±2n）。新增 `_dilate_axis`（與軸線段的 Minkowski sum：geom + 平移副本 +
  各邊掃成平行四邊形 → 對任意多邊形精確）與 `_morph_axis`（grow 用 dilate；shrink 用補集-膨脹-
  補集 erosion，需 fov_bbox）。`evaluate` 的 Morph 分支改呼叫 `_morph_axis`；parser 限制軸
  標籤僅 W/H（大小寫不拘）否則報錯。更新 `tests/test_gds_boolean.py` morph 測試（方向性
  面積 + bounds、shrink 缺 fov 報錯、非法軸標籤報錯）。對話框運算子鈕擴成 `>W: >H: <W: <H:`。
- **版面溢出（`gds_align_tool.py`）**：`CoordinateSetupPanel` 的 `_corner_lbl`/`_origin_lbl`
  無 word-wrap，輸入值後標籤文字變長（含千分位 + nm/µm），單行 QLabel 撐寬固定 300px 的 SEM
  面板而溢出。兩個 label 加 `setWordWrap(True)`。

**測試：** `py_compile` 三檔過。sandbox 無 numpy/shapely/PyQt6 → 未跑 pytest / GUI，待 user
本地 `pytest tests/test_gds_boolean.py -v` + 驗 GUI（W/H 方向、coordinate setup 不再溢出）。

**影響檔案：** `glas/core/gds_boolean.py`、`glas/app/gds_align_tool.py`、
`tests/test_gds_boolean.py`、`CLAUDE.md`、`docs/plans/F4-boolean-enhance.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

---

## [2026-05-25] [F4] 修復 edit 閃退 + 對話框內嵌預覽

**變更類型：** bug fix + 功能

**動機現象：** (1) 編輯 expression layer 時偶發閃退（終端機有 Error）。(2) 預覽新 Boolean
layer 要回主視窗才看得到，且 modal 對話框擋住主畫面 canvas。

**修復實作：**
- **閃退根因**：edit/delete 由 `_LayerRow` 的按鈕點擊 / 雙擊 signal 觸發，handler 內同步
  開 modal 對話框（`exec()`）；對話框關閉後 `_recompute_recipes()` → `set_document()` 會
  刪掉那個 row widget，待 `exec()` 返回時控制流回到「已被刪除的 C++ row 物件」的事件
  handler → use-after-free，PyQt6 直接 abort。改為 `_on_edit_recipe`/`_on_delete_recipe`/
  `_on_add_expression` 一律用 `QTimer.singleShot(0, …)` 延遲，等 row 的 handler 完全 unwind
  後再開對話框，避免在 row 事件處理中刪除自身。
- **內嵌預覽**：`ExpressionLayerDialog` 新增 `_ExprPreview` 迷你 canvas（fit-to-view），按
  Preview 直接在對話框內渲染結果（filled highlight）疊在綁定的 raw layer（細外框）上，
  對話框不關、不再動主視窗 doc/canvas；確認無誤再按 **Save**（OK 鈕改名 Save）儲存。
  `_preview_expression` 改回傳 `(ok, msg, data)` 且**不再 mutate 主 doc**（移除原本塞臨時層
  + cancel 時 recompute 的迂迴）。

**測試：** `py_compile` 過。sandbox 無 PyQt6 → GUI 待 user 本地驗（編輯不再閃退、Preview 在
對話框內顯示、Save 才存）。

**影響檔案：** `glas/app/gds_align_tool.py`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

---

## [2026-05-25] [F4] 實作：Boolean 強化（食譜化重算 + 巢狀 + 編輯 + 對話框重設計）

**變更類型：** 功能（新功能 + 重構）

**動機現象：** synthetic（Boolean 表達式）layer 只算一次、載新 ROI（跳 defect）就遺失、
無法編輯、binding 只能綁 raw layer（無法巢狀）。

**修復實作：**
- **引擎（`gds_boolean.py`）**：新增 `normalize_binding`（舊 `(layer,datatype)` →
  `("raw",l,d)`、支援 `("ref",name)`）、`recipe_dependency_order`（拓樸排序 + 循環/未知
  ref 偵測，純函式）、`resolve_expression`（raw/recipe provider 抽象 + 遞迴解析巢狀 ref +
  cache memoize + 循環防護）。core 維持無 Qt。
- **app（`gds_align_tool.py`）**：MainWindow 新增 `self._recipes` 作 synthetic 層唯一事實
  來源；`_recompute_recipes()` 在 `_on_roi_finished`（每次載 ROI/跳 defect 的 FOV）與
  cache 還原時自動重算所有 recipe → synthetic 層跟著 defect 走。`_eval_expression` 改用
  `resolve_expression`（display 路徑）；`poi_polys_for_roi`（F3 batch）同步支援巢狀 +
  recipe 快照。`_LayerRow` 加編輯/刪除按鈕（雙擊=編輯），刪除被其他 recipe 引用時擋下。
  `ExpressionLayerDialog` 重設計：layer/synthetic chip + 運算子按鈕插入 token、即時語法
  檢查（disable OK + inline 錯誤）、binding 下拉含 raw + ref、編輯預填。cache sidecar
  改由 recipe 序列化/還原（tagged binding，含舊格式遷移）；開新 OASIS(ROI)/載新 cache 會
  清掉前一檔的 recipe，ROI reload 則保留。
- **tests**：`tests/test_gds_boolean.py` 加 `normalize_binding` / `recipe_dependency_order`
  / `resolve_expression`（巢狀、循環、未知 ref、舊格式）測試。

**測試：** `py_compile` 三檔皆過。**sandbox 無 numpy/shapely → 未跑 pytest；GUI 互動未驗。**
待 user 本地 `pytest tests/test_gds_boolean.py -v` + GUI 驗收（定義 L0/L1 巢狀、跳 defect
自動重算、編輯連動、刪除、新對話框）。

**影響檔案：** `glas/core/gds_boolean.py`、`glas/app/gds_align_tool.py`、
`tests/test_gds_boolean.py`、`docs/plans/F4-boolean-enhance.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

---

## [2026-05-25] [F4] 規劃：Boolean 強化（食譜化重算 + 巢狀 + 編輯 + 對話框重設計）

**變更類型：** 文件（plan，尚未動工）

**內容：** user 調整優先序：F4 改做 Boolean 強化、原 fine align 診斷改排 F5。經探索確認現況
（synthetic layer 只算一次、ROI reload 即遺失、無法編輯、無法巢狀），Q&A 收斂為四項：
食譜化每 FOV 自動重算、巢狀引用 synthetic、編輯/刪除、表達式對話框完整重設計。
新增 `docs/plans/F4-boolean-enhance.md`（3 milestone）；§8 更新 [F4] 指向新 plan、
[F5] = fine align 診斷。**待 user 核准後才開工。**

**影響檔案：** `docs/plans/F4-boolean-enhance.md`、`docs/plans/F5-finealign-diagnostics.md`
（renumber）、`CLAUDE.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

## [2026-05-25] [F4] 規劃：Fine align 診斷（殘差疊圖 + 批次結果總覽）

**變更類型：** 文件（plan，尚未動工）

**內容：** 經 Q&A 收斂 fine align 強化方向＝結果可視化/診斷，具體交付兩項：殘差疊圖
overlay（對位前/後輪廓畫在 SEM）與批次結果總覽（可排序/篩選/點列跳轉的表格）。
新增 `docs/plans/F4-finealign-diagnostics.md`（2 milestone），於 CLAUDE.md §8 註冊 [F4]。
**待 user 核准後才開工。**

**影響檔案：** `docs/plans/F4-finealign-diagnostics.md`、`CLAUDE.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

## [2026-05-25] [F3] 後續：toolbar 不裁切、layer name catch-all 修正、移除透明度 slider

**變更類型：** UI 修正 + bug fix（PR #3 後續 user 回饋 1/2/3）

**動機/現象：**
1. 視窗非最大化時中間 toolbar 按鈕（Open OASIS / Load Cache / Export Cache…）文字被裁切。
2. 讀 OASIS LAYERNAME 時所有 layer 都顯示同一個名字（NW）。
3. Layers 列的透明度搖桿沒實際用途，要移除。

**修復：**
1. `_build_toolbar` 結尾把每顆按鈕 `setMinimumWidth(sizeHint().width())`（在設粗體後），
   並新增 `_wrap_toolbar()` 用橫向 `QScrollArea`（v-scroll off、h-scroll as-needed、
   高度 = bar + scrollbar extent）包住，窄視窗改為橫向捲動而非裁字。
2. `resolve_layer_name` 改為「最具體（最窄 layer 區間，其次 datatype 區間）優先」，
   並跳過 `(0, INF)` 全層 catch-all（placeholder 名稱不再蓋到每一層）。
   注意：LAYERNAME 表若在檔尾（scan_cell_offsets 於首個 CELL 即停）仍可能收不到，
   屆時退回 L/D；若仍有問題需後續加讀檔尾 name table。
3. `_LayerRow` 移除 opacity slider/`_pct`/`_on_opacity`（`LayerEntry.opacity` 保留，
   渲染用預設值）；移除未用的 `QSlider` import；hint 文字更新。

**測試：** py_compile 全通過；更新 `test_oasis_random.py::TestResolveLayerName`（catch-all
跳過、不蓋其他層）、移除 `test_gds_align_m6.py::test_slider_sets_opacity_and_emits`。
sandbox 無 PyQt6/numpy/cv2，toolbar 捲動/透明度移除等 GUI 行為待 user 本地驗收。

**影響檔案：** `glas/app/gds_align_tool.py`、`glas/core/oasis_random.py`、
`tests/test_oasis_random.py`、`tests/test_gds_align_m6.py`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

## [2026-05-25] [F3] 修正：多 POI 選取以 row 狀態重建，避免 ndarray __eq__ 報錯

**變更類型：** Bug fix（PR #3 review，P1）

**動機/現象：** `LayerEntry` 是含 NumPy 陣列（polygons/bboxes）的 dataclass，
`_on_poi_toggled` 用 `entry not in self._poi_entries` / `.remove(entry)` 會觸發
dataclass `__eq__` 對陣列比較，實際 ROI 資料下選/取消第二個 POI 會丟
`ValueError: truth value of an array ... is ambiguous`，破壞多 POI 互動。

**修復（`glas/app/gds_align_tool.py`）：** 移除多餘且有 bug 的 append/remove 區塊，
直接由各 row 的 `poi_btn.isChecked()` 以 panel 順序重建 `_poi_entries`（原本下方
本就有此重建，append/remove 為冗餘）。不再對 LayerEntry 做相等比較。

**測試：** py_compile 通過；既有 `test_gds_align_m4b.py::test_multi_select_and_run_enabled`
覆蓋多選 toggle 路徑。

**影響檔案：** `glas/app/gds_align_tool.py`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

## [2026-05-25] [F3] M3–M5：多 POI fine align（合成樣板）＋ POI 鈕／預覽彈窗

**變更類型：** 功能（fine align 多 POI / UI）

**動機/現象：** 原 fine align 僅支援單一 POI，真實半導體 SEM 影像含多層結構。改為可選
多個 POI layer，各自輸入 FG gray，合成一張類 SEM 樣板做單次 matchTemplate，並能彈窗
並排 SEM/GDS/Template 做視覺分析。

**修復/實作（`glas/app/gds_align_tool.py`）：**
- 核心：新增 `render_composite_template(poi_layers,...)`（各層 mask 以各自 fg 疊到共用 bg、
  一次 blur）；`render_poi_template` 改為 n=1 thin wrapper（行為不變）。
- `LayerPanel`：POI 改多選，`poi_changed`→`pois_changed(list)`，`_on_poi_toggled` 去互斥、
  以 panel 順序重組；`_LayerRow` POI 鈕放大改「POI」＋`_POI_BTN_QSS`（解決全白看不到）。
- `FineAlignPanel`：移除單一 FG，改 `_poi_box` 每 POI 一列（名稱＋FG spin），`set_pois()`
  保留既有值，新增 `poi_fgs()`；BG/blur/radius/threshold 維持全局；加「Preview template…」。
- `MainWindow`：`_on_pois_changed` / `_poi_layers` / `_build_template` / `_coarse_anchor` /
  `_poi_specs` 全多 POI；`FineAlignAllWorker` 改吃 `poi_specs=[(spec,fg)]`；匯出 `poi_layer`
  多層串接；新增 `TemplatePreviewDialog` + `_on_preview_template` + `_render_gds_preview`。

**測試：** py_compile 全通過；更新 `test_gds_align_m4b.py`（多選、`_poi_specs`、worker
建構子、composite）、`test_gds_align_m7.py`（`set_pois`）。sandbox 無 PyQt6/numpy/cv2，
GUI／matchTemplate 互動驗收待 user 本地。

**影響檔案：** `glas/app/gds_align_tool.py`、`tests/test_gds_align_m4b.py`、
`tests/test_gds_align_m7.py`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`

## [2026-05-25] [F3] M1+M2：版面裁切/最小尺寸修正＋OASIS 圖層名稱顯示

**變更類型：** 功能（UI 修正 + 圖層名稱）

**動機/現象：** (M1) 視窗縮放時版面擠迫、Coordinate Setup 展開時欄位/按鈕被裁切、
對話框最小寬可能撐破小螢幕。(M2) 左側 layer 只顯示 `L17/D0`，user 想看名稱。

**修復/實作：**
- M1（`collapsible.py`）：`CollapsibleSection` body layout 加
  `setSizeConstraint(SetMinimumSize)`，展開段落不再被下方 list 擠壓裁切。
- M1（`gds_align_tool.py`）：新增 `_screen_avail()` / `_capped_min_width()`，三個對話框
  最小寬夾到螢幕；`MainWindow.setMinimumSize(min(940,avw),min(600,avh))`。
- M2（`oasis_streamer.py`）：`scan_cell_offsets` 同輪收集 LAYERNAME → `layernames`。
- M2（`oasis_random.py`）：`resolve_layer_name()` 純函式 + `RandomAccessReader.layer_display_name()`。
- M2（`gds_align_tool.py`）：`LayerEntry.display_name`（display-only，不入 LayerKey identity）；
  `_roi_entry` 填名；`_LayerRow` 顯示 `NAME (L17/D0) · n`，無名稱退回 `L17/D0 · n`。

**測試：** py_compile 全通過；新增 `tests/test_oasis_random.py::TestResolveLayerName` 5 項
（純函式，已於 sandbox 以等價邏輯驗過）。GUI 版面修正待 user 本地確認。

**影響檔案：** `glas/app/gds_align_tool.py`、`glas/app/collapsible.py`、
`glas/core/oasis_streamer.py`、`glas/core/oasis_random.py`、`tests/test_oasis_random.py`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`

## [2026-05-25] 規劃 [F3]：多 POI Fine Align ＋ UI 優化（plan only，尚未動程式）

**變更類型：** 規劃（plan 文件 + §8 任務註冊）

**動機/現象：** user 一次提出 6 項（4 改動 + 2 問題）。問題已於對話回答：(1) Fine
Align 的 FG/BG grey 是合成樣板的前景/背景灰階；(2) View mode 的 minimap 與 SEM mode
差別在 minimap 只是 SEM 滿版再浮一個 defect 色點小地圖、不畫 OASIS 幾何。4 項改動經
`AskUserQuestion` 收斂：維持側欄固定寬只修擠迫/裁切、Layer 列用 OASIS LAYERNAME 顯示
名稱、POI 鈕放大改「POI」字樣、Fine Align 改多 POI（每 POI 自己 FG gray、合成一張樣板、
彈窗並排 SEM/GDS/Template）。

**實作：** 新增 `docs/plans/F3-multipoi-and-ui.md`（5 milestone：M1 版面/裁切、
M2 LAYERNAME 名稱、M3 多 POI 核心、M4 POI 鈕+多選 UI、M5 視覺化彈窗），於 CLAUDE.md
§8「進行中」註冊 [F3]。**尚未修改任何程式碼**，待 user 核准 plan 後從 M1 開工。

**測試：** 無（純規劃）。

**影響檔案：** `docs/plans/F3-multipoi-and-ui.md`、`CLAUDE.md`、`SESSION_LOG.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`

## [2026-05-24] UI batch 1：Load SEM 主色按鈕 / Coord 折疊 badge / image list badge

**變更類型：** 功能（UI / UX）

**動機/現象：** 三項視覺強化：(1) `Load SEM…` 按鈕視覺權重不足，與 `Open OASIS…`
不對等；(2) Coordinate Setup 收起後看不出 FOV 是否已設定；(3) image list 每列無法
一眼看出對位狀態（有無座標 / fine-align 分數）。

**修復/實作：**
- **Fix 1（`gds_align_tool.py`）**：新增 `_LOAD_SEM_BTN_QSS`（橘底白字 + hover 深橘 +
  menu-indicator），`SemPanel` 的 Load SEM 按鈕存為 `self.load_sem_btn` 並套用該 QSS。
  （按鈕實際在 `SemPanel` 而非 toolbar。）
- **Fix 2（`collapsible.py` + `gds_align_tool.py`）**：`CollapsibleSection` header 加
  `self._badge` QLabel（右對齊，no-trailing 路徑也包一層 row）+ `set_badge(text,fg,bg)`
  + `_update_badge_visibility()`（僅在收起且有文字時顯示）；`SemPanel.update_coord_badge()`
  讀 `fov_w_nm`/`fov_w`（皆 nm，/1000→µm）顯示綠色 `FOV W × H` 或琥珀 `not set`，
  於 `__init__` 末 seed 一次、`MainWindow._on_coord_changed` 每次更新。
- **Fix 3（`gds_align_tool.py`）**：新增 `_ImageListDelegate(QStyledItemDelegate)`，在右
  邊距以 UserRole+2/+3/+4 資料畫圓角 badge；`set_images` 對無座標列調暗文字 + 設
  `no coords` 灰 badge；`set_score` 改設分數 badge（綠/琥珀/紅，門檻 `>=t` / `>=0.7t` /
  else），不再 inline 改文字。

**測試：** `py_compile` 兩檔通過；更新 `test_gds_align_m4b.py::test_end_to_end`（改驗 badge
資料角色而非 `[score]` 文字）；`test_gds_align_m7.py` 新增 7 項（accent QSS / coord badge
not-set / set / hidden-when-expanded / no-coords badge / score green / score red）。完整
`pytest tests/` 442 passed。offscreen render-grab 煙霧測試：視窗正常顯示、Load SEM 橘色、
badge 正確。

**影響檔案：** `glas/app/gds_align_tool.py`、`glas/app/collapsible.py`、
`tests/test_gds_align_m7.py`、`tests/test_gds_align_m4b.py`。

**Branch：** `claude/jolly-babbage-8nwED`（PR #2）

## [2026-05-24] LAYERS empty hint 置中微調

**變更類型：** UI 微調

**動機/現象：** `LayerPanel._show_empty_hint()` 的三個 item 用 `AlignCenter`，
改為 `AlignHCenter` 明確水平置中（QListWidget 無 list-wide setAlignment API，
per-item setTextAlignment 即正確機制）。`_group()` 橘色標籤上一輪已完成，本次未動。

**修復/實作（`glas/app/gds_align_tool.py`）：** icon/title/hint 三 item 的
`setTextAlignment` 由 `Qt.AlignmentFlag.AlignCenter` → `AlignHCenter`。

**測試：** `python3 -m py_compile` 通過；`pytest tests/test_gds_align_m6.py
tests/test_gds_align_m7.py` 59 passed。

**影響檔案：** `glas/app/gds_align_tool.py`。

**Branch：** `claude/jolly-babbage-8nwED`（PR #2）

## [2026-05-24] GLAS UI 五項修正（依 docs/glas_ui_fixes.md）

**變更類型：** 功能（UI / UX 微調）

**動機/現象：** 依 `docs/glas_ui_fixes.md` 修正五個 UI 問題：右欄 Coordinate Setup
預設展開把 image list 擠掉、左欄 LAYERS 空白引導視覺太輕、Set/Clear Offset 放在中央
視圖下方定位不清、toolbar group label 對比不足、中央 empty state 與 guidance strip
文字重複。

**修復/實作（`glas/app/gds_align_tool.py`）：**
- 問題1：`SemPanel` 的 Coordinate Setup `_wrap_section(..., collapsed=True)`（原 False）；
  `MainWindow.__init__` 加 `self._coord_collapsed_once = True`，使自動收起邏輯不再干預
  （預設已收起，user 再展開即固定）。
- 問題2：`LayerPanel._show_empty_hint()` 由單行小字改為圖示 + 主文「Open an OASIS」+
  次文「toolbar → Open OASIS…」三層置中結構。
- 問題3：Set/Clear Offset 由中央 `center_layout` 移入 `SemPanel`（image list 下方、
  Load GDS ROI 上方），改名 `self.sem_panel.set_offset_btn/clear_offset_btn`，
  signal 在 `MainWindow.__init__` 重新接線；原 `self._set_offset_btn/_clear_offset_btn`
  區塊整段刪除（無其他 setEnabled 引用）。
- 問題4：`_build_toolbar` 的 `_group()` label 改用 `_TK_ACCENT_DK` 色、letter-spacing
  1px、padding；FILE group 前加 `h.addSpacing(4)`。
- 問題5：`SemViewer._draw_empty_state()` 三步驟提示改為「Follow the steps above to get
  started」，由 guidance strip 負責引導。

**測試：** `python3 -m py_compile` 通過；同步更新 4 個測試的舊行為斷言
（`test_gds_align_m6.py::test_set_document_none_clears`、`test_gds_align_m7.py` 的
`test_initial_collapse_state` / `test_no_collapse_without_valid_fov` / `test_layers_empty_hint`），
`pytest tests/test_gds_align_m6.py tests/test_gds_align_m7.py` 59 passed，
完整 `pytest tests/` 435 passed。

**影響檔案：** `glas/app/gds_align_tool.py`、`tests/test_gds_align_m6.py`、
`tests/test_gds_align_m7.py`。

**Branch：** `claude/jolly-babbage-8nwED`

## [2026-05-24] GLAS 品牌元素整合（icon / wordmark / About）

**變更類型：** 功能（UI / branding）

**動機/現象：** 應用程式仍沿用舊名 "GDS Align Tool"，缺視窗 icon、wordmark 與品牌化
About 對話框。依 `docs/IMPLEMENT.md` 將五項品牌元素整合進 app。

**修復/實作（`glas/app/gds_align_tool.py`）：**
- import：QtGui import 補上 `QIcon`。
- `main()`：設定 `setApplicationName/DisplayName/Version/OrganizationName("GLAS"...)`，
  並以 `icons/glas_icon_256.svg` 設 `app.setWindowIcon`（所有視窗共用）。
- `MainWindow.__init__()`：視窗標題 "GDS Align Tool" → "GLAS"，並以
  `icons/glas_icon_32.svg` 設 titlebar icon。
- `_build_toolbar()`：toolbar 最左側插入 `icons/glas_wordmark.svg` wordmark（高度 28px）
  + VLine 分隔線（沿用既有 `_divider()` helper）。
- `_show_about()`：由 `QMessageBox.information` 升級為自繪 `QDialog`（128 icon + 大字 GLAS
  + subtitle + 版本 + 說明 + OK 按鈕）。
- 四個 SVG（256/128/32/wordmark）此 session 前已置於 `glas/app/icons/`。

**測試：** `python3 -m py_compile glas/app/gds_align_tool.py` 通過；sandbox 無 PyQt6
無法實際啟動 GUI 驗收（taskbar icon / wordmark 顯示 / About 對話框待 user 本地確認）。

**影響檔案：** `glas/app/gds_align_tool.py`。

**Branch：** `claude/determined-einstein-Bfo0G`

## [2026-05-24] GLAS 專案自 MMH 抽離成立

**變更類型：** 專案建立 / 重構（抽離）

**動機/現象：** GDS Align Tool 原藏在 MMH 專案 `tools/` 下（plan F2，M1–M7 全實作）。
其核心能力——大檔 OASIS streaming / random-access 解析、KLARF↔GDS 座標換算、FOV 空間查詢、
即時 Boolean 表達式引擎、SEM↔GDS overlay 對位——不只 MMH 用得到，未來其他專案也想複用。
藏在 MMH 內定位不對，故抽離成獨立 repo **GLAS（GDS-Layout Alignment for SEM）**。

**實作（自 MMH git HEAD 搬移，零行為改動）：**
- **glas/core/（無 Qt 引擎）**：`oasis_streamer` / `oasis_store` / `oasis_walker` / `oasis_random`
  / `gds_fov` / `gds_boolean` / `gds_layer_cache` + 自 MMH `src/core` 複製的 `klarf_parser`。
  core 模組原本即無 src 依賴，零修改。
- **glas/app/（PyQt6 殼）**：`gds_align_tool`（改寫 header：`from src.gui.*` soft import →
  flat `from styles/collapsible/icons`，repo-root path hack → core+app sys.path 設定；
  subprocess streamer import 指向 glas/core）、`sem_loader`（`from src.core.klarf_parser` →
  `from klarf_parser`）+ 自 MMH `src/gui` 複製的 `styles` / `collapsible` / `icons/`（無 src 依賴）。
- **import 慣例**：core/app 以扁平 sys.path 模組互相 bare-import（沿用原 `tools/` 慣例）；
  `main.py` + `conftest.py` 把 `glas/core` 與 `glas/app` 放上 sys.path。
- **規則機制移植**：`CLAUDE.md`（保留 §2 工作規則 / §6 慣例 / §8 任務 / §10 checklist 機制，
  改寫 §1/§4/§5/§7 為 GLAS 實況）、`.claude/settings.json` + `hooks/check_progress.sh` +
  `check_session_log.sh`（SessionStart 訊息改 GLAS，腳本邏輯不變）、`README.md`、本 `SESSION_LOG.md`。
- **design history**：`docs/plans/F2-gds-align-tool.md` + `F2-M1.13-parser-perf.md` + `_template.md`
  搬入保留。
- **tests**：14 個 test 檔（`test_oasis_*` / `test_gds_*` / `test_sem_loader`）+ `fixtures/sample_real.klarf` 搬入。

**測試：** sandbox 無 numpy / cv2 / shapely / PyQt6 / pytest，無法跑完整 suite；已 `py_compile`
全檔通過。完整 `pytest tests/`（~218 項應全綠，證明搬移零行為改動）待有相依的環境執行。

**接續任務：** [F1] 互動驗收（真實 SEM 對位 / 拖動 / fine-align / 批次 / 匯出 / 折疊 UX）—
這些在 MMH 抽離前即標記「待 user 本地驗證」，移到 GLAS 接續。

**影響檔案：** 整個 GLAS repo（新建）。MMH 側對應移除見 MMH SESSION_LOG 同日條目。

**Branch：** （新 repo，待 user 在 GitHub 建立後上傳）
