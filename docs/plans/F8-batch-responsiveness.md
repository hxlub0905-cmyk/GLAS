# [F8] Batch Align 反應性與加速：ProcessPool + 節流串流 + 扁平進度條

> **狀態：** planned
> **§8 ID：** [F8]
> **建立：** 2026-05-26
> **負責 branch：** claude/practical-pascal-AtKLm

---

## Goal & Context

user 回報「跑 Batch Align 時 UI 非常非常卡、不敢亂點，而且運算時間很長；進度條太花俏、感覺也吃效能，
想回歸簡單清新版」。

調查（`gds_align_tool.py`）後確認**三個獨立的卡頓來源**，外加一個「運算長」的根因：

1. **串流時 O(N²) 重建整個結果面板（最大元兇、是 bug）**：`_on_fa_result`（`gds_align_tool.py:5621`）
   每收到**一張**結果就 `_refresh_batch_panel()` → 對**全部**影像重算 `fine_align_result_rows`，再
   `BatchResultsPanel.set_rows()`（`:4138`）整表重建（`setRowCount` + 重建 N×6 個 cell +
   `resizeColumnsToContents()`），且 `_rebuild_charts()`（`:4187`）每次把直方圖/散點 widget **刪掉重生**。
   N 張影像 → N 次 ×O(N) 的 GUI 主執行緒重繪 = **O(N²)**，影像越多越卡。
2. **F6 thread-pool 的 GIL 競爭（F6 M3 退化）**：批次開最多 8 條 Python thread，但 OASIS ROI walk
   解碼是純 Python 緊迴圈、會持續握 GIL；8 條飢餓 thread 與 Qt 主執行緒搶 GIL → UI 一頓一頓，且純
   Python 解碼那段並未因多執行緒變快（`gds_align_tool.py:1259`）。
3. **進度條重繪成本**：`_AnimatedBar`（`:716`）每張都漸層 + 雙層發光矩形 + 掃光帶 + 每 120ms
   `advance()` 重繪 + % 字雙描邊，在批次最忙時還在燒主執行緒繪圖。
4. **運算長**：expression POI 每張要 ROI walk + shapely 布林；thread-pool 只平行到 cv2/GEOS（會釋放
   GIL）那段，純 Python 解碼仍序列化。真正吃滿多核要改**多行程**。

**成功長相：** 跑 Batch 時 UI 順暢（可自由捲動/點選）、進度條簡潔扁平、批次在多核機真正變快；且
**每張 fine-align 結果與現況 byte/value 完全相同**（§7 不變式、F6「功能不變」承諾）。

**與現有系統的關係：** 本計畫**修訂 F6 M3**（thread-pool → ProcessPool）與 **F7 M1/M4**（進度條質感
回退為扁平、串流改節流）。F6/F7 既有的演算法、結果值、SemViewer 折疊、CE early-stop、median→δ 符號
**一律不動**。

---

## Q&A Decisions

### Q1: 進度條風格
**選項：** 簡潔扁平版 / 原生 QProgressBar / 保留動畫但調淡
**選擇：** 簡潔扁平版
**理由：** user 明確要「反璞歸真、簡單清新」。單色扁平填充 + 旁邊一個 % 文字，拿掉漸層/發光/掃光/動畫，
重繪成本最低，並套用到全 app（OASIS 載入對話框 + 批次面板）。

### Q2: 批次加速策略
**選項：** 先修 UI 卡頓（保留 thread-pool）/ 改單執行緒 / 改多行程 ProcessPool
**選擇：** ProcessPool
**理由：** user 要「真正變快 + 不卡」。多行程繞過 GIL → 純 Python 解碼也能吃滿多核，且子行程完全碰不到
GUI 的 GIL，主執行緒徹底不被干擾。代價是子行程啟動/序列化開銷（小批次以 in-thread fallback 規避）。

### Q3: 串流結果表更新
**選項：** 節流即時更新 / 只在結束後填表
**選擇：** 節流即時更新
**理由：** 保留「邊跑邊看」體驗但合併更新（QTimer 約 300ms 重繪一次），直方圖/散點只在結束時重建。把
O(N²) 降為 O(N × 有限次數)。

---

## Milestones

> 每個 milestone 以「一個 session 可完成」為粒度切。

### M1: 扁平進度條（拿掉漸層/發光/掃光/動畫）  [status: done — code 完成，GUI 待本地驗收]

- [x] `_AnimatedBar.paintEvent` 改為：軌道（圓角單色 `_TRACK`）+ 單色扁平填充（`_FILL` 一色，無漸層、
  無發光矩形、無掃光帶）；determinate 時填充寬 = `frac×w`。bar 高度 20→14（更纖細清新）。
- [x] % 不再畫在 bar 內（兩個呼叫端的 detail label 已顯示 `pct%`，bar 內重複又擁擠）→ 直接移除 in-bar
  文字與雙描邊/clip，bar 純粹一條填充。
- [x] indeterminate 保留**極簡單色滑塊**（ping-pong，無發光/漸層）；`advance()` 維持 API，但 determinate
  模式直接 return 不重繪（批次跑時不再每 tick 燒繪圖）。
- [x] **API 不變**（`set_fraction` / `set_indeterminate` / `advance`），呼叫端零改動 → 全 app 進度條同步扁平。
  順手移除已不再使用的 `QLinearGradient` / `QPainterPath` import。
- [x] 驗證：py_compile 過；GUI 外觀（扁平、無動畫殘留）user 本地驗收。

### M2: 節流串流刷新（修 O(N²)）  [status: done — code 完成，GUI 待本地驗收]

- [x] `_on_fa_result`：只更新 `self._refined` / `self._fa_meta` / badge，**不再每張呼叫**
  `_refresh_batch_panel()`；改為 `self._batch_refresh_timer`（QTimer single-shot、300ms）未啟動時 start →
  一串結果最多 ~3x/sec 重填表。
- [x] `set_rows(rows, threshold, rebuild_charts=True)`：`rebuild_charts=False` 時跳過
  `_rebuild_charts()`（直方圖/散點 widget teardown+rebuild 才是貴的部分）；timer 觸發時傳 False。
- [x] finished/cancelled/failed：`_batch_refresh_timer.stop()` + 最後一次 `_refresh_batch_panel()`
  （預設 rebuild_charts=True → 表 + 圖 + median 鈕）；`_open_fa_results` 與起跑初次刷新亦走完整版；起跑
  先 stop 清掉殘留 timer。
- [x] 驗證：py_compile 過；測試無直接呼叫 `set_rows`（新增參數有預設值、向後相容）；GUI 大批次順暢
  user 本地驗收。

### M3: 抽 Qt-free fine-align 至 core + ProcessPool 批次  [status: done — code 完成，相依/實機待本地]

- [x] **M3a 抽核心**：新增 `glas/core/fine_align.py`（Qt-free，僅 numpy/cv2/gds_boolean/oasis_random），把
  `rasterize_layer`(+`_scanline_fill`)、`make_template`、`_fit_mask`、`render_composite_template`、
  `render_poi_template`、`_parabola_subpx`、`fine_align_one`、`_walk_roi_polys`、`poi_polys_for_roi`、
  `_fine_align_image` 從 `gds_align_tool.py` **逐字搬入**；app 端 `import fine_align` + `from fine_align
  import (...)` 把 10 個名字取回 namespace → 既有呼叫端（render/`_fit_mask`/rasterize 於 5149/5188/5242、
  OverlayExportWorker 的 poi_polys_for_roi）與測試（`gat.X`：m4b + accel）全相容。GUI-only 的
  `overlay_outlines_on_sem`/`_draw_polyline_np` 留在 app（子行程用不到）。
- [x] **M3b ProcessPool 進入點**（`fine_align.py` module-level、spawn 可 re-import）：`_pool_init(path,
  wanted, dtype, bbox_layer, root, poi_specs, cfg)` 在子行程建 `RandomAccessReader`（由**路徑+filter 重建**，
  非傳 live reader）存 module global `_G`；`_pool_task(job)` → `_fine_align_image(..., _never_cancel)`。
- [x] **M3c worker 改線**：`FineAlignAllWorker.run()` 由 `ThreadPoolExecutor` 改 `ProcessPoolExecutor`
  （`mp.get_context("spawn")`），initargs 傳 reader 參數（`str(_path)`/`_init_wanted`/`_dtype`/`_bbox_layer`）
  + root + poi_specs + cfg（皆 picklable）；結果 tuple 在 run() 單一 thread 隨 `as_completed` emit（接 M2 節流）。
- [x] **cancel**：orchestrator `threading.Event`；觸發時 `fut.cancel()` 掉所有未開始 future（`CancelledError`
  略過）、in-flight 跑完即止、**已完成結果保留**；末尾 `ex.shutdown(wait=True)`。粒度=**單張影像邊界**
  （非 F6 逐 node 即時 bail）——見 Risks。小批次走 `_run_in_thread`（仍逐 node cancel）。
- [x] **小批次 fallback**：`n == 0` 直接 finished(0)；`n <= 2` 或 `workers <= 1` 走 in-thread 循序（clone
  一個 reader、用完 close），規避 spawn 啟動開銷。
- [x] **SEM 影像不跨行程傳**：子行程自己 `cv2.imread(path)`（job 只帶 path）→ 不序列化大圖。
- [x] 驗證：py_compile（fine_align.py + gds_align_tool.py）過；app 對 10 個搬移名字的引用全部解析到 import
  回來的名稱（grep 核對）；見 M4 等價測試。

### M4: 等價性 + 效能驗收  [status: in progress — 測試齊備，本地相依/實機待跑]

- [x] `tests/test_accel_equivalence.py`：新增 `TestProcessPoolEquivalence::test_pool_entry_matches_sequential`
  ——`_pool_init`（由**路徑**重建 reader）+ `_pool_task` 每張 result tuple 與循序 `_fine_align_image` 完全相等
  （含 no-coords / missing-file 狀態）。**在 in-process 跑 pool 進入點**（不真的 spawn——pytest 下 spawn 慢/脆；
  真正的跨行程 transport 是 std-lib，分歧只會來自「由路徑重建 reader + 任務接線」，此測已涵蓋）。
- [x] 既有 `TestBatchParallelEquivalence`（thread-pool）保留：經 `gat._fine_align_image`（已 re-export）仍驗
  搬移後純函式行為不變。
- [ ] 本地 `pytest tests/test_accel_equivalence.py tests/test_oasis_random.py tests/test_oasis_streamer.py
  tests/test_gds_align_f5.py tests/test_gds_align_m4b.py -v` 全綠。
- [ ] 手動（user 本地）：大批次 Run all → UI 順、進度條扁平、結果值與改前逐張一致、多核明顯變快、按終止
  在一張影像時間內停、已完成結果保留。

---

## Affected Files

- `glas/core/fine_align.py`（**新增**，Qt-free fine-align 計算 + ProcessPool 進入點）
- `glas/app/gds_align_tool.py`（`_AnimatedBar` 扁平化、`_on_fa_result` 節流、`BatchResultsPanel` 圖表解耦、
  `FineAlignAllWorker.run()` 改 ProcessPool、移除/搬走計算函式並改 import 取回）
- `tests/test_accel_equivalence.py`（import 路徑 + ProcessPool 等價測試）
- `docs/plans/F8-batch-responsiveness.md`（本檔）、`CLAUDE.md`（§8 註冊/收尾）、`SESSION_LOG.md`

---

## Risks / Open Questions

- **cancel 粒度變鬆**：ProcessPool 無法中斷 in-flight 子行程的單張運算 → 取消粒度從「逐 node 即時」變
  「單張影像邊界」。一般每張很快、影響小；但若單張 ROI 的 Boolean 特別久，取消會等該張跑完。**已與 user
  在 Q2 確認接受多行程方案**；若實測不可接受，退路是 thread-pool + 僅靠節流 UI（M1/M2 已足以解卡）。
- **Windows spawn re-import**：子行程會 re-import worker 所在模組 → 必須是 **Qt-free 的 `fine_align.py`**
  （只拉 numpy/cv2/shapely），**絕不可**讓子行程 import `gds_align_tool`（會拉 PyQt6 + 建 app）。M3a 抽核心
  正是為此。
- **子行程啟動/序列化開銷**：小批次可能比 thread 慢 → 以小批次 in-thread fallback 規避。
- **§7 不變式**：所有計算走同一批**未修改**的純函式（只是換執行載體）→ 結果 byte/value 不變，M4 等價測試把關。
- **mmap 記憶體**：每行程各自 map 同檔，OS 共享實體頁，N 行程不耗 N× RAM（沿用 F6 M1 結論）。

---

## 驗證方式

- [ ] 所有 milestone checkbox 已勾
- [ ] `python3 -m py_compile glas/core/fine_align.py glas/app/gds_align_tool.py`
- [ ] `pytest tests/test_accel_equivalence.py tests/test_oasis_random.py tests/test_oasis_streamer.py
  tests/test_gds_align_f5.py -v` 全綠（含 ProcessPool 等價）
- [ ] 手動（user 本地）：大批次 Run all UI 順暢 + 扁平進度條 + 結果逐張一致 + 多核變快 + 取消即時（張邊界）
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 在最終 SESSION_LOG 條目註記 `完成 [F8]`
- 從 `CLAUDE.md` §8 移除 [F8]（並視驗收狀況收尾 F6/F7）
- **本檔保留**，作為 design history
