# [F25] EXPORT 單一路徑單一按鈕：融合對位+匯出（ROI 只走一次）

> **狀態：** planned（DRAFT — 待 user 核准 + 確認 D1–D3）
> **§8 ID：** [F25]
> **建立：** 2026-06-30
> **負責 branch：** claude/project-perf-optimization-86i8yt

---

## Goal & Context

**動機（來自效能審查 + user 指示）：** 目前 F24「Export all」流程把每張影像的 ROI **解碼兩次**——
先 `FineAlignAllWorker`（對位 pass：walk ROI → 合成 template → `matchTemplate`），再
`OverlayExportWorker`（匯出 pass：**再 walk 同一個 ROI** → rasterize gray/label/overlay → 寫 PNG）。
ROI walk 是純 Python、GIL-bound 的最重步驟，等於整個資料集付兩次。審查列為「輸出」最大的單一槓桿
（端到端「Export all」約 1.3–1.7×，密集 FOV + 重 Boolean 時可達 ~2×）。

User 進一步要求：**EXPORT 只留一個路徑與一個按鈕**。

**成功長相：**
- 操作員手動 Run 幾張確認對位後，按**一個** Export 鈕；每張被選影像的 ROI **只解一次**，同一個 per-image
  task 內完成「（必要時）對位 + rasterize + 寫所有產物」。
- 已手動跑過、存在 `self._refined` 的影像沿用其對位值（不重算 `matchTemplate`），但仍用那次 walk 來
  rasterize 匯出產物——最終 refined 值與產物與現況 **byte-identical**（決定論）。
- 移除重複的 export 入口，UI 上 export 只剩一個按鈕。

**與現有系統關係：** 取代 F24 的「fine-align pass → export pass」雙 worker 串接，融合為單一 worker /
單一 per-image 計算路徑。沿用 F23 常駐 pool、F14 process-pool 模型、上一批 quick win 落地的
`prebuilt_index` 注入與 `render_gray_and_label_from_geoms` 共用 raster。

---

## Q&A Decisions（AskUserQuestion 工具當下不可用 → 以下為「建議值」，開工前需 user 確認）

### D1: 「一個按鈕」的範圍  **【待確認】**
**選項：** A 保留單張 Run + 1 個 Export 鈕 / B 只留 1 個 Export 鈕（連單張 Run 也移除）/ C 三鈕都留只統一路徑
**建議：** **A**
**理由：** 符合 user 先前描述的工作流（「手動 Run 3-4 張確認對位 → 整包匯出」）。保留 FineAlign 面板
單張「Run fine align」做目視抽查；移除工具列「Export Alignment…」(`_align_btn`)；FineAlign 面板
「Export all…」改名「Export…」成**唯一** export 入口。

### D2: 融合方式（ROI 只走一次）  **【待確認】**
**選項：** A 融合成單一 worker（per-image task 內 align+rasterize+寫檔）/ B 保留兩 pass + 幾何 sidecar
**建議：** **A**
**理由：** user 字面要「一個路徑」。A 是真正的單一 pass、最大加速、架構最簡（不需跨 process 序列化
已解析幾何）。代價：匯出選項（格式 / 產物 / 輸出資料夾）必須在「開跑前」選定（見 D3）。

### D3: 匯出選項時機  **【待確認】**
**選項：** A 保留單一選項對話框、改在開跑前出現 / B 簡化成固定全產出（不問）
**建議：** **A**
**理由：** 維持現有彈性（勾選 raw/overlay/gray/label、選影像、選 CSV/JSON、選輸出資料夾），只是把
對話框從「對位跑完後」前移到「開跑前」。這是 D2-A 的必然結果（worker 開跑時就要知道要寫什麼）。

### D4（內部、不需 user 決策，記錄於此）
- **in-thread fallback 保留：** 小批 / raw-only / 無 cv2 仍走 in-thread（對使用者透明，非使用者可見的
  「路徑」）。「一個路徑」指對位+匯出融合，不是移除這個內部 fallback。
- **alignment CSV/JSON：** 由 orchestrator 在批次結束後從收集到的 refined 結果組裝（無 ROI walk，cheap）。
- **walk 條件：** 每張影像只在「需要對位」或「有要求影像產物」時才 walk；純 CSV/JSON 匯出且影像已對位 →
  完全不 walk（保留現有便宜路徑）。
- **batch 子集 re-run（`_on_rerun_requested`）：** 屬「對位 re-run」非 export，沿用同一 worker（export
  旗標關閉）；保留。

---

## 現況盤點（實作前的事實基礎）

**Export 相關按鈕（3 個入口）**
- 工具列 `_align_btn`「Export Alignment…」→ `_on_export_alignment`（直接開 `AlignmentExportDialog`）。
- FineAlign 面板 `_export_all_btn`「Export all…」→ `_on_export_all`（補跑未對位 → 完成後 `_on_export_alignment`）。
- FineAlign 面板 `_run_btn`「Run fine align」→ `_on_run_fine_align`（單張，非 export）。

**計算路徑（2 條）**
- Path A `FineAlignAllWorker`（`gds_align_tool.py:1451`+）→ `fine_align._fine_align_image`（walk→template→match）。
- Path B `OverlayExportWorker`（`gds_align_tool.py:1576`+）→ `overlay_export.export_one_image`（walk→rasterize→PNG）。
- `_on_export_all`（`:7100`）設 `_export_after_fa` → `_launch_fa`（Path A）→ `_on_fa_finished`（`:7268`）
  `QTimer.singleShot(0, _on_export_alignment)` → 開 dialog → `_export_overlay_images`（Path B）。
- 兩條都各有「process pool / in-thread」子分支，且各自重建 reader。

**關鍵不變式（§7，融合不得破壞）**
- per-image task 為純函式 → parallel == sequential（`overlay_export.py:98-100`、`fine_align.py:522-527`）。
- KLARF↔GDS / SemViewer fold / fine-align 修正量符號（M4）。
- F15 gray↔label 像素一致（同 anchor / FOV-corner / `_fov_min_corner` y_min raise）。
- score-threshold gate 決定 gray/label 是否輸出。

---

## Milestones

> 每個 milestone 以「一個 session 可完成」為粒度切。

### M1: 核心融合計算（Qt-free，決定論）  [status: done 2026-06-30]

- [x] 新增 `overlay_export.align_and_export_one_image(job, rar, root, poi_colored, cfg, out_dir, flags, score_thr, cancel_cb)`：
      每個 POI walk **一次** → `polys`（template + overlay）、`geom`（gray/label）；
      `job` 帶 `prior_refined`：None → 合成 composite template + `matchTemplate` 算 refined；非 None → 沿用。
      接著於 `anchor=coarse+refined` rasterize 要求的產物、寫 PNG、回 `(fa_result|None, manifest_row)`。
      （置於 `overlay_export` 而非 `fine_align`，避開 overlay→fine_align 既有單向 import 的循環。）
- [x] walk gating：無對位需求且未要求影像產物 → 跳過 walk（純 CSV 影像）；`need_geom` 才算 raw POI 的 union（保 F23 fast path）。
- [x] 統一 pool task：`overlay_export._afe_pool_task` 重用 **F23 常駐 pool**（reader 由 `fine_align.pool_reader()`
      取得、context 隨 task），export 不再自建冷 pool；新增 `fine_align.pool_reader()` accessor。
- [x] 驗證（`tests/test_export_fused.py`，5 項，無需真 GUI）：
      - refined 與舊 `_fine_align_image` 對同一輸入 **逐值相等**；
      - manifest row + 五產物 PNG 與舊 `export_one_image` **byte-identical**（fresh + reused，`_build_two_cell`）；
      - 已對位 + 無產物 → spy 斷言**完全不 walk**；missing-file 路徑；`_afe_pool_task` 取用共享 reader。

### M2: App 單一 worker + 單一 handler  [status: planned]

- [ ] 新 `ExportWorker`（取代 `FineAlignAllWorker` + `OverlayExportWorker`）：驅動 F23 常駐 pool；
      stream `progress` / 每張 `result`(refined) / 收集 `manifest rows`；in-thread fallback（小批 / raw-only / no-cv2）；
      cancel 語意（丟未起跑 future、partial PNG 保留）沿用。
- [ ] 新 `_on_export`（合併 `_on_export_all` + `_on_export_alignment` + `_export_overlay_images`）：
      guard（images / POI / OASIS / FOV）→ **開跑前**開選項對話框（格式 / 產物 / 影像 / 輸出資料夾）→
      對所有被選影像建 jobs（夾帶各自 `prior_refined`）→ launch `ExportWorker` →
      完成後寫 alignment CSV/JSON + 收集到的 overlay manifest。
- [ ] `_refined` 串流更新沿用（手動 Run 過的影像不被覆寫，除非更佳 / rerun 模式）。
- [ ] 驗證：`_on_export` 連線測試（仿 `test_gds_align_f24.py` 的 monkeypatch 風格）：todo 計算、
      jobs 帶 prior_refined、cancel/fail 不寫部分 manifest、純 CSV 路徑不 walk。

### M3: UI 收斂成單一按鈕  [status: planned]

- [ ] 移除工具列 `_align_btn`「Export Alignment…」+ 其 wiring（D1=A）。
- [ ] FineAlign 面板 `_export_all_btn` 文案改「Export…」、tooltip 更新；signal `export_all_requested`→`export_requested`。
- [ ] 保留單張 `_run_btn`「Run fine align」（目視抽查）。
- [ ] 更新 guidance / status / 動作 gating（`_refresh_action_states`）文案，反映「選選項→一鍵跑完」。
- [ ] 驗證：offscreen GUI smoke——按鈕存在性、單一入口、選項對話框於開跑前出現。

### M4: 測試遷移 + 文件  [status: planned]

- [ ] 遷移引用舊 worker / 舊 handler 的測試（`test_export_perf.py`、`test_gds_align_f24.py`、
      `test_gds_align_f5.py`、batch fine-align 相關）。保留 byte-identical 斷言作為融合正確性護欄。
- [ ] `SESSION_LOG.md` 條目（§2.1 格式）；`CLAUDE.md` §5.2 改寫並行模型段（兩 worker → 一 worker）；
      §8 註記 F25；`README.md` 更新匯出流程描述；`docs/plans/F24-export-all-one-click.md` 註記被 F25 取代。
- [ ] 全測試綠（目前 742 → 預期持平或微增）。

---

## Affected Files

- `glas/core/fine_align.py`（新增 `align_and_export_one_image` + 統一 pool init/task；walk-once）
- `glas/core/overlay_export.py`（`export_one_image` 邏輯併入統一計算；保留純 rasterize helper）
- `glas/app/gds_align_tool.py`（`ExportWorker` 取代兩 worker；`_on_export` 合併；移 `_align_btn`；改按鈕文案/signal）
- `tests/test_export_perf.py`、`tests/test_gds_align_f24.py`、`tests/test_gds_align_f5.py`、（新）`tests/test_export_fused.py`
- `CLAUDE.md`（§5.2 / §8）、`SESSION_LOG.md`、`README.md`、`docs/plans/F24-export-all-one-click.md`

---

## 風險 / 回退

- **最大風險＝正確性而非效能：** 融合後 per-image task 必須維持「parallel==sequential、與舊兩函式組合
  byte-identical」。對策：M1 的 byte-identical 護欄測試先行，舊函式保留可被測試比對。
- **測試面廣：** 兩 worker 合一會牽動數個 GUI/匯出測試。對策：分 M 漸進、每步保持全綠。
- **回退：** 純加法式新增核心函式 + 新 worker；UI 收斂為最後一步。若 M3 有疑慮可先只上 M1/M2（路徑融合、
  按鈕暫不動），確認加速與正確性後再收 UI。
