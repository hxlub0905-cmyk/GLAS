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

### M1: 扁平進度條（拿掉漸層/發光/掃光/動畫）  [status: planned]

- [ ] `_AnimatedBar.paintEvent` 改為：軌道（圓角單色）+ 單色扁平填充（`_FILL` 一色，無漸層、無發光
  矩形、無掃光帶）；determinate 時填充寬 = `frac×w`。
- [ ] % 顯示改為**單純一行**（置中、單色描邊一次；或移到 bar 外側 label——以最簡為準），不再雙描邊 + clip。
- [ ] indeterminate 模式保留一個**極簡**滑塊（單色、無發光），或直接以靜態「處理中」呈現；`advance()` 維持
  API 但不做高成本重繪。
- [ ] **API 不變**（`set_fraction` / `set_indeterminate` / `advance`），呼叫端（`LoadProgressDialog`、
  `BatchResultsPanel`）零改動 → 全 app 進度條同步變扁平。
- [ ] 驗證：py_compile；GUI 外觀（扁平、% 可讀、無動畫殘留）user 本地驗收。

### M2: 節流串流刷新（修 O(N²)）  [status: planned]

- [ ] `_on_fa_result`：只更新 `self._refined` / `self._fa_meta` / badge，**不再每張呼叫**
  `_refresh_batch_panel()`；改為 `self._batch_refresh_timer`（QTimer，單發、約 300ms）合併觸發一次表更新。
- [ ] `BatchResultsPanel`：表更新與圖重建解耦——新增「只重填表、不動圖」路徑（`set_rows(..., rebuild_charts=False)`
  或等義），`_rebuild_charts()` 只在 `_on_fa_finished` / `_on_fa_cancelled` / `_open_fa_results` 末尾呼叫一次。
- [ ] finished/cancelled/failed：取消待觸發 timer，做**最後一次**完整刷新（表 + 圖 + median 鈕狀態）。
- [ ] 驗證：py_compile；既有 `tests/test_gds_align_f5.py` 純函式測仍綠；GUI 大批次捲動/點選順暢 user 本地驗收。

### M3: 抽 Qt-free fine-align 至 core + ProcessPool 批次  [status: planned]

- [ ] **M3a 抽核心**：新增 `glas/core/fine_align.py`（Qt-free，僅 numpy/cv2/shapely/sibling core），把
  `rasterize_layer`(+其 helper)、`make_template`、`_fit_mask`、`render_composite_template`、
  `render_poi_template`、`_parabola_subpx`、`fine_align_one`、`_walk_roi_polys`、`poi_polys_for_roi`、
  `_fine_align_image` 從 `gds_align_tool.py` 搬入；app 端改 `from fine_align import (...)` 取回（line 5495
  等呼叫端與測試 import 路徑都要相容）。**邏輯一字不改，純搬移。**
- [ ] **M3b ProcessPool 進入點**（在 `fine_align.py`，module-level、可被 spawn re-import）：
  `_pool_init(path, wanted, dtype, bbox_layer, root, poi_specs, cfg)` → 在子行程建一個
  `RandomAccessReader`（由路徑+filter 重建，非傳入 live reader）存到 module global；`_pool_task(job)` →
  呼叫 `_fine_align_image(job, _G_RAR, _G_ROOT, _G_SPECS, _G_CFG, _never_cancel)` 回傳 tuple。
- [ ] **M3c worker 改線**：`FineAlignAllWorker.run()` 由 `ThreadPoolExecutor` 改 `ProcessPoolExecutor`
  （**明確 `mp_context="spawn"`** 跨平台一致），initargs 傳 reader 參數（path/`_init_wanted`/dtype/
  `_bbox_layer`）+ root + poi_specs + cfg（皆 picklable）。結果 tuple 在 run() 單一 thread 隨 `as_completed`
  emit（沿用節流串流）。
- [ ] **cancel**：orchestrator 端 `threading.Event` + `ex.shutdown(wait=False, cancel_futures=True)`（Py3.9+）
  →未開始 task 直接取消、in-flight（≤ workers 張）跑完即止、**已完成結果保留**。粒度為**單張影像邊界**
  （非 F6 之前的逐 node 即時 bail）——見 Risks。
- [ ] **小批次 fallback**：`n` 很小（如 `n <= 2` 或 `n <= workers`）時走 in-thread 循序，規避 spawn 啟動開銷。
- [ ] **SEM 影像不跨行程傳**：子行程自己 `cv2.imread(path)`（job 只帶 path）→ 不序列化大圖。
- [ ] 驗證：py_compile；見 M4 等價測試。

### M4: 等價性 + 效能驗收  [status: planned]

- [ ] `tests/test_accel_equivalence.py`：compute 函式 import 改自 `fine_align`（core）；新增
  `TestProcessPoolEquivalence`（循序 vs ProcessPool 每張 result tuple 完全相等，沿用既有假 SEM/jobs/specs）。
- [ ] 既有 `TestBatchParallelEquivalence`（thread-pool）：保留或改寫為對 `fine_align._fine_align_image` 的
  循序 baseline（確保搬移後純函式行為不變）。
- [ ] 本地 `pytest tests/test_accel_equivalence.py tests/test_oasis_random.py tests/test_oasis_streamer.py
  tests/test_gds_align_f5.py -v` 全綠。
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
