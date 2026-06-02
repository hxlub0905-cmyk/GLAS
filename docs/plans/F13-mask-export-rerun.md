# [F13] Per-image GDS mask 批次輸出 + low-score re-run

> **狀態：** in progress (2026-06-02)
> **§8 ID：** [F13]
> **建立：** 2026-06-02
> **負責 branch：** claude/optimistic-pasteur-31ELv

---

## Goal & Context

**為什麼做：** 下游工具 MMH 需要 per-image 的 GDS mask 來限縮 blob 偵測範圍，
解決純 gray-level 定位失效的問題。GLAS 是**唯一**能產生這個 mask 的工具
（Boolean 引擎合成 layer + SEM↔GDS fine-align 得到精準 anchor）。但目前 GLAS：

- 缺乏批次 mask 輸出能力，下游無法消費對位結果；
- batch fine-align 跑完後，若有 low-score 的圖，使用者只能**重跑全部一萬張**才能調參再對，
  無法針對 low-score / 選取的少數圖重跑，非常浪費時間。

**成功長相：** 使用者 `Run all` → 在 BatchResultsPanel 對 low-score 圖調參 `re-run`
→ 反覆直到所有圖 score ≥ 0.8 → 在 Export 對話框勾 `Export GDS mask`、設 threshold
→ 輸出一個資料夾：每張 `{image_id}_mask.png`（uint8）+ `mask_manifest`（沿用 overlay manifest，新增 `mask_png` 欄）
→ MMH 設定 `gds_mask_dir` 指向該資料夾即可直接信任套用（每張輸出的 mask 都是 score ≥ threshold 的可信結果）。

**與現有系統關係：** 延伸現有 batch fine-align（`FineAlignAllWorker` + `BatchResultsPanel`）
與 overlay 匯出（`OverlayExportWorker` + `AlignmentExportDialog`）；不取代，並存擴充。

---

## Q&A Decisions

### Q1: Re-run 結果覆蓋策略？
**選項：** A 永遠覆蓋 / B 永遠不覆蓋（只新增） / C 只有新 score > 舊 score 才覆蓋
**選擇：** C
**理由：** 保證每次 re-run 只會讓結果變好，不會因為調壞參數把原本好的結果蓋掉。

### Q2: 沒跑 fine-align 或 score < threshold 的圖要不要輸出 mask？
**選擇：** 不輸出。
**理由：** GLAS 負責品質把關，輸出的每張 mask 都是可信的；MMH 不需要 fallback 邏輯，
看到資料夾裡有該 image_id 的 mask 就能無條件套用。

### Q3: Export mask 的 UI 入口？
**選擇：** 跟現有 Export Overlay 合併成同一個 dialog（`AlignmentExportDialog`），
新增 `Export GDS mask (.png)` checkbox + `Score threshold` spinbox（預設 0.8，range 0.0–1.0，step 0.05）
+ 即時顯示預計輸出張數（jobs 裡 score ≥ threshold 的圖數）。
**理由：** mask 與 overlay 共用同一次 ROI walk 與 anchor，合併入口避免重複走查、UI 一致。

### Q4: mask 輸出使用哪個函式？
**選擇：** `glas/core/gds_boolean.py` 的 `make_mask()`，已存在不需新增。
**注意（探索修正）：** 實際簽章為
`make_mask(geom, *, width_px, height_px, x_min_nm, y_min_nm, nm_per_px, invert_y=True, fill=255)`，
吃**單一 geometry**（非草稿假設的 `make_mask(polys, anchor, W, H, nm_per_px)`）。
→ M2 需把各 POI polys `unary_union` 成一個 geom 後傳入，並由 anchor 換算 FOV 左下角 `x_min_nm/y_min_nm`。

### Q5: Re-run UI 放在哪裡？
**選擇：** `BatchResultsPanel` 內，results table 下方新增 Re-run 參數區塊。
**理由：** 使用者看著 table 的 low-score 結果，就地調參重跑，動線最短。

---

## Milestones

> 每個 milestone 以「一個 session 可完成」為粒度切。

### M1: BatchResultsPanel Re-run 介面  [status: done (code; pytest 待 user)]

- [x] `BatchResultsPanel` results table 下方新增 Re-run 參數區塊（沿用 FineAlignPanel 既有參數名）：
  - [x] Search radius (nm) — `search_radius_nm`（預設帶入現有值，可覆蓋）
  - [x] Background GL — `bg_glv`
  - [x] Blur σ (px) — `blur_sigma_px`
  - [x] Per-POI FG GL — `fg_glv`（繼承現有設定，可覆蓋）
- [x] 新增「Re-run low-score」按鈕：重跑所有 `status == "low-score"` 的圖
- [x] 新增「Re-run selected」按鈕：重跑 table 勾選的圖（table 改支援多選 / checkable）
- [x] Re-run 透過既有 `FineAlignAllWorker`（ProcessPoolExecutor）跑**子集** jobs
- [x] 覆蓋規則（Q1=C）：新 score > 舊 score 才更新 `MainWindow._refined[image_id]`
      與對應 `BatchResultsPanel._rows` 列（兩處同步）
- [x] Re-run 完成後 table / histogram / scatter 自動刷新
- [ ] 驗證：手動對 low-score 圖重跑，table 只更新有變好的列；histogram/scatter 跟著更新

### M2: OverlayExportWorker 補 mask 輸出  [status: done (code; pytest 待 user)]

- [x] `__init__` 新增 `export_mask: bool` 與 `mask_score_threshold: float` 參數
- [x] `run()` 重構：ROI walk（`poi_polys_for_roi`）改為「`export_overlay` 或 `export_mask` 任一為真」就執行一次，
      overlay 與 mask **共用同一次 walk 的 `entries`**（不重複呼叫 `poi_polys_for_roi`）
- [x] mask 輸出分支：
  - [x] `refined is None` → 跳過（不輸出 mask）
  - [x] `refined[2] < mask_score_threshold` → 跳過
  - [x] 以上都過 → 把 entries 的 polys `unary_union` 成 geom，由 anchor + `nm_per_px` + W/H 換算
        FOV 左下角 `x_min_nm/y_min_nm`（與 `overlay_outlines_on_sem` 的座標映射一致），
        呼叫 `make_mask(geom, width_px=W, height_px=H, x_min_nm=..., y_min_nm=..., nm_per_px=...)`
        → `cv2.imwrite(out_dir / f"{base}_mask.png", mask)`
  - [x] manifest row 新增 `mask_png` 欄（跳過的留空字串）
- [x] `_COLS` 增 `"mask_png"`
- [ ] 驗證：見 M4 單元測試

### M3: Export Dialog 更新  [status: done (code; pytest 待 user)]

- [x] `AlignmentExportDialog` 新增：
  - [x] `Export GDS mask (.png)` QCheckBox
  - [x] `Score threshold` QDoubleSpinBox（預設 0.8、range 0.0–1.0、step 0.05）
  - [x] 預計輸出張數 QLabel（即時計算 jobs 中 score ≥ threshold 的數量；隨 threshold 變動更新）
- [x] `selected()` 回傳擴充：帶出 `export_mask` 與 `mask_score_threshold`
- [x] 呼叫端建構 `OverlayExportWorker` 時帶入新參數
- [ ] 驗證：勾/不勾 mask、調 threshold，預計張數 label 即時正確

### M4: 測試  [status: done (code; pytest 待 user)]

- [x] `test_rerun_only_improves`：re-run 後 score 更低**不**覆蓋、更高**才**覆蓋
- [x] `test_rerun_selected`：只有選取的 image_id 被重跑
- [x] `test_mask_export_threshold`：score < threshold 不輸出 `mask_png`，≥ threshold 輸出
- [x] `test_mask_export_no_refined`：`refined is None` 的圖不輸出 mask
- [x] `test_manifest_mask_png_col`：manifest CSV 有 `mask_png` 欄位
- [ ] 驗證：`pytest tests/test_gds_align_f13.py -v` 全綠

---

## Affected Files

- `glas/app/gds_align_tool.py`（`BatchResultsPanel`、`OverlayExportWorker`、`AlignmentExportDialog`）
- `glas/core/gds_boolean.py`（**只讀**，`make_mask()` 已存在）
- `tests/test_gds_align_f13.py`（新增）
- `docs/plans/F13-mask-export-rerun.md`（本 plan）

---

## Risks / Open Questions

- **共用 walk 結果的 scope**：`poi_polys_for_roi` 目前只在 `export_overlay` 分支內呼叫；
  改成 overlay/mask 共用時，要確保 `entries`（polys）的計算與生命週期正確，避免只勾 mask 時漏走查、
  或兩者都勾時走兩次。
- **兩條 worker 架構不可混用**：re-run 走 `FineAlignAllWorker`（ProcessPoolExecutor 多進程），
  mask 輸出走 `OverlayExportWorker`（單一 Reader、QThread）。兩者架構不同，re-run 與 export 是兩條獨立路徑。
- **make_mask 座標一致性（待實作驗證）**：mask 的 `x_min_nm/y_min_nm` 換算必須與 overlay
  `overlay_outlines_on_sem` 用的 anchor 映射完全一致，否則 mask 會與 overlay 對不上。
  實作時須核對 anchor=image 中心 vs FOV 左下角的關係（含 invert_y）。
- **覆蓋同步**：score 同時存在 `MainWindow._refined` 與 `BatchResultsPanel._rows`，
  re-run 覆蓋時兩處要一致更新。

---

## 驗證方式

整個 feature 結束時的 end-to-end 驗證：

- [x] 所有 milestone 程式碼 subtask 已完成（`py_compile` 通過）
- [ ] `pytest tests/test_gds_align_f13.py -v` 通過（**待 user 本地**——沙箱無 numpy/PyQt6）
- [ ] 手動：batch `Run all` → 對 low-score 圖調參 `Re-run low-score` / `Re-run selected`
      → 確認只變好不變壞、table/圖刷新 → Export 勾 `Export GDS mask` 設 threshold
      → 檢查資料夾每張 `{image_id}_mask.png` + manifest `mask_png` 欄、只有 score ≥ threshold 才有 mask
- [x] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 在最終 SESSION_LOG 條目註記 `推進 [F13]`
- 從 `CLAUDE.md` §8 移除（feature 全部完成時）
- 本檔保留為 design history
