# [F7] 批次對位工作區：Batch view-mode + inline 進度/即時結果表 + 進度條質感升級

> **狀態：** planned（待核准）
> **§8 ID：** [F7]
> **建立：** 2026-05-25
> **負責 branch：** claude/dazzling-cori-5T7XE

---

## Goal & Context

**動機現象：** 使用者的理想流程是 *Load OASIS → KLARF → coordinate/coarse → SEM↔GDS align →
在已對位影像上寫 boolean 加 layer → 單張 fine-align 測試幾張 → 跑 Batch 輸出 template+offset*。
其中 1–5 是**單張 / setup 導向**，第 6 步是**多張 / review 導向**——這個相位切換正適合給批次一個
專屬畫面。目前批次結果在 modal-ish 的 `FineAlignResultsDialog`（另一個視窗，與主畫布競爭空間、
無法邊看表邊看 overlay），進度在 modal `LoadProgressDialog`（會擋操作），且使用者覺得進度條
「不夠質感」。

**成功長相：**
- 批次有專屬 **「Batch」view-mode**（沿用現有 segmented 切換）：中央左=結果表+直方圖+散點、
  右=SEM overlay；點表格一列 → 右側 overlay **當場換到那張**，不離開 batch 模式、可連續 review。
- 批次跑的時候 **inline 進度**（該 view 頂部進度條+ETA）+ 結果**隨完成 streaming 填進表**，
  不再彈 modal 視窗。
- 進度條 **質感升級**（漸層填充 + 軟發光 + 條內 % 數字 + 更圓潤），全 app 一致（OASIS 載入也受益）。

**與現有系統關係：** 純 UI 重新安置 + 視覺升級。**不動 F6 的批次運算**（每張 `(dx,dy,score,
used_r,status)` 與現在逐值相同）、不動 fine-align 演算法、不動 §7 不變式。複用既有純函式
`fine_align_result_rows` / `score_histogram` / `residual_median` 與 `_ScoreHistogram` /
`_ResidualScatter` / `_AnimatedBar`。

可重用（探索結論）：view-mode = `_VIEW_MODES` + `QButtonGroup` + `_set_view_mode`（4827）切
`_center_split`（4487：目前 [canvas, sem_viewer]）；結果視圖 = `FineAlignResultsDialog`（3985）；
進度 = `_AnimatedBar`（716）in `LoadProgressDialog`（789）；批次接線 = `_on_run_fine_align_all`
（5360 附近）→ `_on_fa_progress/_on_fa_result/_on_fa_finished/_on_fa_cancelled` + `_open_fa_results`。

---

## Q&A Decisions

### Q1: 批次結果畫面位置
**選擇：** **第四個 view-mode「Batch」**（segmented 列加一顆；中央分割 左=結果 / 右=SEM overlay）。
**理由：** 最貼合現有 view-mode 心智模型、改動最小（重用 `_set_view_mode` + `_center_split`），且
保住「點列即見 overlay」的 review 迴圈（dock 多一層視窗管理、獨立 dialog 失去同視窗併看）。

### Q2: 批次進度呈現
**選擇：** **Inline 進度 + live 表**——批次跳進 Batch view，頂部 inline 進度條+ETA，結果隨完成
streaming 進表，不彈 modal。**OASIS 載入 / overlay 匯出仍用原 modal `LoadProgressDialog`**（那裡
無表可串）。

### Q3: 進度條質感
**選擇：** **漸層 + 發光 + 條內 %**（橘→深橘漸層、軟外發光、determinate 時條內白色 % 數字、更高更
圓潤、保留掃光帶動態）。與現有色系一致；升級 `_AnimatedBar` → 全 app 進度條同步變精緻。

---

## Milestones

### M1: `_AnimatedBar` 質感升級（漸層 + 發光 + 條內 %）  [status: planned]

- [ ] `_AnimatedBar.paintEvent`：填充改 `QLinearGradient`（`#e89a4a` → `#d06f22`）、軌道更柔、
  圓角加大、bar 加高（14→18px）；填充外緣加一層半透明軟發光（外擴 rounded rect / 低 alpha）。
- [ ] determinate 模式條內置中畫白色 `NN%`（`set_fraction` 已知 frac 時）；indeterminate 不顯示 %。
- [ ] 保留 `set_fraction` / `set_indeterminate` / `advance` API 與掃光帶動態，**不改任何呼叫端**。
- [ ] 驗證：py_compile；GUI 外觀（漸層/發光/% 字/動態）user 本地驗收。

### M2: 抽出可重用 `BatchResultsPanel`（QWidget，含 inline 進度條）  [status: planned]

- [ ] 新 `BatchResultsPanel(QWidget)`：把 `FineAlignResultsDialog` 的「summary + only-low 篩選 +
  sortable 表 + `_ScoreHistogram` + `_ResidualScatter` + Apply-median 鈕」內容搬進來；
  對外 `set_rows(rows, threshold)` 重填、signals `image_activated(str)` / `apply_median_requested(float,float)`。
- [ ] 頂部加 **inline 進度區**：`_AnimatedBar` + 狀態 label（done/total · % · Elapsed · ETA）+ Cancel 鈕，
  `start_progress()` / `set_progress(done,total,image_id)` / `end_progress(status_text)` 控制顯示；
  signal `cancel_requested()`。閒置時進度區隱藏、只顯示結果。
- [ ] 純函式（`fine_align_result_rows` / `score_histogram` / `residual_median`）**不動**，panel 只組裝。
- [ ] 驗證：py_compile；既有 `tests/test_gds_align_f5.py` 純函式測仍綠；面板互動 user 本地驗收。

### M3: 新增「Batch」view-mode + 併入中央分割  [status: planned]

- [ ] segmented 列加 `self._seg_batch`（icon 例如 `recipe`/`layers`）；`_VIEW_MODES` 加 `"batch"`。
- [ ] `_center_split` 插入 `self.batch_panel = BatchResultsPanel(...)`（順序 [batch_panel, canvas,
  sem_viewer]，預設隱藏）。
- [ ] `_set_view_mode` 處理 `"batch"`：顯示 batch_panel、隱藏 canvas、`setSizes` 左(結果)≈55% /
  右(SEM)≈45%；其餘 mode 隱藏 batch_panel（既有 sem/gds/minimap 行為不變）。
- [ ] `batch_panel.image_activated` → 選取該影像 + 更新右側 SEM overlay，**留在 batch 模式**
  （重用 `_on_sem_image_selected` 的選取/jump，但不強制切 view-mode）；`apply_median_requested`
  → 沿用既有 `_on_apply_median_residual`。
- [ ] 驗證：py_compile；切換四種 view-mode（含 batch 左右分割、點列就地換 overlay）user 本地驗收。

### M4: 批次接線改 inline（取代 modal 進度 + 自動切 Batch view）  [status: planned]

- [ ] `_on_run_fine_align_all`：起跑時 `_set_view_mode("batch")` + `batch_panel.start_progress()`，
  **不再開 `LoadProgressDialog`**；worker `progress`→`batch_panel.set_progress`、`result`→更新
  `_refined`/badge **並** `batch_panel.set_rows(fine_align_result_rows(...))`（streaming 重填，
  量大時節流）。
- [ ] cancel：`batch_panel.cancel_requested` 以 DirectConnection 直接 `worker.cancel()`（沿用 F5 M5
  `threading.Event` 即時生效）；cancel/finished 後 `end_progress(...)`、保留已完成結果列、啟用 median。
- [ ] `_open_fa_results`（「Results…」鈕）改為「切到 Batch view + 重填面板」；移除
  `FineAlignResultsDialog` 的使用（內容已併入 panel）。
- [ ] **不動**：批次運算/結果值、median→δ 符號、overlay 匯出與其 modal 進度、OASIS 載入 modal 進度。
- [ ] 驗證：py_compile；端到端（跑 Batch 自動進 batch view、inline 進度/ETA、streaming 表、即時
  cancel、median→δ、點列就地換 overlay）user 本地驗收。

### M5: 收尾驗收  [status: planned]

- [ ] 全部 milestone checkbox 已勾。
- [ ] `python3 -m py_compile glas/app/gds_align_tool.py` + `pytest tests/test_gds_align_f5.py -v`
  （純函式不受影響）user 本地全綠。
- [ ] GUI 端到端 user 本地驗收（上述 M1–M4 互動 + 外觀）。

---

## Affected Files

- `glas/app/gds_align_tool.py`（`_AnimatedBar` 升級；新 `BatchResultsPanel`；`FineAlignResultsDialog`
  內容搬移後移除其使用；segmented `_seg_batch` + `_VIEW_MODES` + `_set_view_mode` 加 batch；
  `_center_split` 插面板；`_on_run_fine_align_all` / `_on_fa_*` / `_open_fa_results` 改 inline 接線）
- `docs/plans/F7-batch-workspace-ui.md`、`CLAUDE.md`（§8）、`SESSION_LOG.md`
- （測試）沿用 `tests/test_gds_align_f5.py` 的純函式測；GUI 部分無法在無 Qt 沙箱測，user 本地驗收。

---

## Risks / Open Questions

- **GUI-heavy、沙箱無 PyQt6/numpy/cv2**：只能 py_compile + 純函式測把關，視圖/互動/外觀全由 user 本地驗收。
- **§7 不變式不動**：fine-align 符號、SemViewer 折疊、CE early-stop 皆不碰；本案只搬 UI、不改運算。
- **中央三分割版面**：batch_panel 插入後，sem/gds/minimap 三模式的可見性與 split 尺寸需逐一確認不退化
  （尤其 minimap 的 corner overlay 仍掛 sem_viewer）。
- **streaming 重填表效能**：每筆 result 重填整表，影像量很大時節流（例如每 N 筆或 100ms 合併）。
- **`FineAlignResultsDialog` 移除**：確認沒有其他入口仍依賴該 dialog（目前僅 `_open_fa_results`）。

---

## 驗證方式

- [ ] 所有 milestone checkbox 已勾
- [ ] `python3 -m py_compile glas/app/gds_align_tool.py`
- [ ] `pytest tests/test_gds_align_f5.py -v`（純函式不受影響）
- [ ] 手動（user 本地）：跑 Batch 自動進「Batch」view、inline 進度條（漸層/發光/%/ETA）、結果
  streaming 進表、即時 cancel 保留部分結果、點列右側 overlay 就地換、median→δ；四種 view-mode 切換
  正常；OASIS 載入仍用原 modal 進度。
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 在最終 SESSION_LOG 條目註記 `完成 [F7]`
- 從 `CLAUDE.md` §8 移除該任務
- **本檔保留**，作為 design history
