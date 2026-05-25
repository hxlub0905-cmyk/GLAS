# [F5] Fine align 診斷 + 工作流：殘差疊圖 + 批次總覽 + setup 持久化 + cancel/ETA + overlay 匯出

> **狀態：** planned（待核准）
> **§8 ID：** [F5]
> **建立：** 2026-05-25（2026-05-25 擴充範圍；2026-05-25 再納入 setup 持久化 / cancel 修復 / overlay 匯出 / 命名）
> **負責 branch：** claude/sharp-lamport-YIk3z

---

## Goal & Context

F3 把 fine align 做成「多 POI 合成樣板 → 單次 `cv2.matchTemplate` → 單一偏移＋分數」，
但只有狀態文字與影像列 badge，**看不到貼合度、也看不出批次裡哪裡系統性對不準**。F5 只
**新增「看結果 / 找誤差」的視圖與一個校準動作**，不改 fine align 演算法本身
（matchTemplate / 符號 / SemViewer 折疊不變式皆不動）。

可重用（探索結論）：`_render_gds_preview(anchor,W,H,nm_per_px)`→RGB、
`render_composite_template`→樣板灰階、`fine_align_one`→`(dx,dy,score,used_r)`、
`_refined_offset(img)`、`_coarse_gds(img)` / `_current_image_gds()`（before/after anchor）、
`self._refined`（{image_id:(dx,dy,score)}）、`self._sem_images`＋`sem_panel` 選取/badge、
`alignment_rows`（匯出）。缺口：SemViewer 只會畫到螢幕（需自寫「輪廓疊到 SEM→RGB ndarray」
helper）；無常駐結果表；單張路徑現在丟掉 `used_r`。

---

## Q&A Decisions

### Q1: Fine align 先加強方向
**選擇：** 結果可視化 / 診斷（非旋轉縮放、非子像素多階、非邊緣比對）。

### Q2: 納入哪些
**選擇（user 授權由我建議 + 明選 C5）：**
- M1 殘差疊圖、M2 批次結果表 — 原 plan 兩項。
- C1 殘差散點圖、C5 score 直方圖 — 批次健康度 / 系統誤差視覺化。
- C2 中位殘差 → origin δ 一鍵套用 — 看完能修的閉環。
- C3 狀態/失敗原因、C4 顯示 used search radius — 讓表格可讀、可信。

**未選（暫不做）：** 信心指標 PSR / peak sharpness、常駐結果面板、
旋轉/縮放 fine align、score 熱圖。如之後要再開 milestone。

### Q3: 工作流擴充（2026-05-25 user 回饋）
1. **命名（#1）**：`BG grey` → `Background GL`、每個 POI 列的 `FG` → `Foreground GL`（**兩者都改**）。
2. **切換 DID 重設很煩（#2）**：現況切 defect 走 `_on_roi_finished → set_document(新doc)`，每次用全新
   `LayerEntry` 重建列 → **POI 勾選 / 可見性 / 顏色遺失**（recipe / offset δ / FG-BG 參數本來就有保留）。
   **決策：重套設定、user 手動按一次** —— 記住 fine-align setup（哪些層當 POI、可見性、各 POI FG），
   新 doc 載入時自動重套，user 只需按一次 Run fine align（不自動跑）。
3. **batch cancel 跑不停 + 要 ETA + 預覽已完成（#3）**：根因 = worker 在 `run()` 緊迴圈，`cancel()` 是
   跨執行緒 **queued slot**，迴圈不回 event loop 永遠不執行。**決策：改 `threading.Event` 直接旗標**，
   進度對話框顯示 ETA（已用時間 × 剩餘比例）+ 即時看已完成結果。
4. **overlay+image 匯出（#4）**：**決策：可勾選 raw / 對位後 overlay PNG / 兩者，附 manifest**
   （image_id ↔ 檔名 ↔ dx/dy/score）供 MMH 用 image_id join。複用 M1 的 `overlay_outlines_on_sem`。

---

## Milestones

### M1: 殘差疊圖（Preview 彈窗 before/after）  [status: planned]

- [ ] 新 helper `overlay_outlines_on_sem(sem_gray, entries, anchor, nm_per_px)`：把可見層
  （或合成樣板）的**輪廓**以層色畫在 SEM 灰階轉 RGB 上，回傳 `(H,W,3) uint8`。不碰
  SemViewer 螢幕繪製，獨立 raster（cv2.polylines / 純 numpy）。
- [ ] `TemplatePreviewDialog` 由 SEM/GDS/Template 三格擴成含「對位前」（coarse anchor，
  `_coarse_gds` 去掉 refined）與「對位後」（coarse + `_refined`）兩張疊圖。
- [ ] `_on_preview_template` 準備 before/after 兩 anchor + entries 傳入。
- [ ] 驗證：py_compile；helper 以小 mask 純函式測（輪廓像素落點/層色正確）；彈窗顯示待 user。

### M2: 批次結果總覽彈窗（表 + 直方圖 + 散點圖）  [status: planned]

- [ ] 新 `FineAlignResultsDialog`：
  - **表格** `QTableWidget`：image_id / score / dx_nm / dy_nm / **used_radius(C4)** /
    **狀態(C3)**；可依欄排序；score 依 threshold 上色（綠/黃/紅）；勾「只看低於 threshold」。
    狀態 = ok / no-coords / flat（樣板無訊號）/ missing-file / low-score。
  - **Score 直方圖(C5)**：分數分布長條（含 threshold 垂直線），一眼看整體健康度。
  - **殘差散點圖(C1)**：每張影像 (dx,dy) 散點 + 原點十字 + median 標記，分辨系統性偏移 /
    隨機 / 離群。
- [ ] 批次 worker 多回傳狀態碼（讓 C3/C4 有資料）：`_on_fa_result` 收
  `(image_id, dx, dy, score, used_r, status)`；單張路徑也保存 used_r/status。
- [ ] `Run all` 完成（`_on_fa_finished`）後彈出；另加 panel/toolbar「Results…」可重開。
- [ ] 點表格列 / 散點 → 選取並跳到該影像（重用 `_on_sem_image_selected` / list 選取）。
- [ ] 驗證：py_compile；以假 `_refined`＋狀態建表/直方圖分箱/散點 median 的純函式測；
  互動跳轉待 user。

### M3: 中位殘差 → origin δ 一鍵套用（C2）  [status: planned]

- [ ] `FineAlignResultsDialog` 加「Apply median residual to origin δ」鈕：取所有 ok 影像的
  median(dx,dy)，加進 `self._origin_dx/dy`（沿用 Set Offset 的 δ 機制與符號），更新
  Coordinate Setup 顯示、重畫 jump/overlay。
- [ ] 套用後提示「δ += (mx,my) nm；建議重跑 Run all 看殘差是否收斂」；不自動清空既有
  `_refined`（讓 user 比較前後）。
- [ ] 驗證：py_compile；median 計算（含忽略非 ok）純函式測；套用後 δ 數值/符號正確待 user。

### M4: Fine-align setup 持久化 + 命名（#1 #2）  [status: planned]

- [ ] **命名（#1）**：`FineAlignPanel` 的 `BG grey` caption → `Background GL`；`set_pois` 每列的
  `FG` 標籤 → `Foreground GL`（tooltip 同步）。純標籤字串改，不動 `values()` 的 key（`bg_glv` 等）
  避免動到匯出 / cache schema。
- [ ] **setup 記憶（#2）**：MainWindow 新增 `self._fa_setup`（by layer key：是否 POI / 可見 / 顏色 /
  FG），在 user 改 POI 勾選 / 可見性 / FG 時更新；`pois_changed` 與 `set_pois` 路徑寫回。
- [ ] **重套**：`_on_roi_finished` 在 `set_document(doc)` 後，依 `self._fa_setup` 用 layer key 比對
  新 doc 的 entries，重設可見性 / 顏色 / 重勾 POI、並把記住的 FG 餵回 `FineAlignPanel.set_pois`。
  key 不存在於新 ROI 的層則略過（該 defect 沒有該層）。**不自動 run**——只還原設定。
- [ ] 驗證：py_compile；`_fa_setup` 套用邏輯（by-key 比對 / 缺層略過 / FG 還原）純函式測；
  互動（切 DID 後 POI 仍勾、按一次 Run fine align 即可）待 user。

### M5: Batch cancel 即時生效 + ETA + 已完成預覽（#3）  [status: planned]

- [ ] **cancel 修復**：`FineAlignAllWorker` 改用 `threading.Event`（GUI 執行緒 `set()`、worker 迴圈
  與 `cancel_cb` 直接讀 `is_set()`），取代靠 queued slot 設 `self._cancel`。`_on_run_fine_align_all`
  連線改 `cancel_requested → event.set`（DirectConnection 或直接呼叫）。確保按下立刻停（含中斷
  正在進行的 ROI walk，已有 `WalkCancelled` 機制）。
- [x] **ETA + 動態進度條**：`LoadProgressDialog` 加自繪 `_AnimatedBar`（掃光填充，避開 QSS 殺
  chunk 動畫的雷）+ `set_progress(done,total)`；`_on_fa_progress` 依 done/total 與已用時間估剩餘，
  detail 顯示「done/total · NN% · Elapsed m:ss · ETA m:ss」。（2026-05-25 user 直接要求先做）
- [ ] **已完成預覽**：batch 進行中每收一筆 `result` 即更新 `self._refined` + badge（現已如此），
  cancel 後保留已完成結果（不清空）；M2 的 `FineAlignResultsDialog` cancel 後也能開看部分結果。
- [ ] 驗證：py_compile；ETA 估算純函式測（done/total/elapsed → 剩餘秒）；cancel 即時性與
  進度文字待 user。

### M6: Overlay + image 匯出（#4）  [status: planned]

- [ ] `AlignmentExportDialog`（或新分頁）加「影像匯出」選項：勾選 **raw image** / **對位後 overlay
  PNG** / **兩者**；overlay 重用 M1 `overlay_outlines_on_sem`（對位後 anchor = coarse + `_refined`）。
- [ ] 匯出 worker（避免凍 UI）：逐張讀 SEM → 視勾選輸出 `*_raw.png` / `*_overlay.png` 到選定資料夾，
  並寫 **manifest**（CSV/JSON，含 image_id ↔ raw 檔名 ↔ overlay 檔名 ↔ dx/dy/score/status），
  供 MMH 以 image_id join。沿用既有 alignment schema 欄位命名。
- [ ] 驗證：py_compile；manifest 列組裝 + 檔名規則純函式測；實際 PNG 落點 / overlay 對齊待 user。

---

## Affected Files

- `glas/app/gds_align_tool.py`（overlay helper、`TemplatePreviewDialog` 擴充、
  `FineAlignResultsDialog`、批次 worker 回傳狀態 + `threading.Event` cancel + ETA、
  `_on_fa_result/_finished`、Results 按鈕、median→δ 接線、`FineAlignPanel` 命名、
  `_fa_setup` 持久化 + `_on_roi_finished` 重套、`AlignmentExportDialog` 影像匯出 + manifest worker）
- `tests/`（overlay helper / 建表 / 分箱 / 散點 median / status 判定 / `_fa_setup` by-key 套用 /
  ETA 估算 / manifest 列組裝 純邏輯測試）

---

## Risks / Open Questions

- **環境**：sandbox 無 PyQt6/numpy/cv2 → 無法跑 GUI；以 py_compile＋純函式測試把關，
  視圖與互動由 user 本地驗收。
- **§7 不變式**：fine-align 符號、SemViewer 折疊不可動；C2 套用 δ 沿用既有 Set Offset 符號，
  不另立新符號規則。
- **before/after anchor 一致性**：before=`_coarse_gds`（不含 refined）、after=含 `_refined`，
  需與實際 jump/overlay 完全一致。
- **worker 回傳擴充**：多帶 used_r/status 會動到 batch worker 的 signal 形狀，須同步單張路徑
  與既有 `set_score` badge。
- **散點/直方圖繪製**：用輕量 QPainter 自畫（不引入 matplotlib，維持相依精簡）。

---

## 驗證方式

- [ ] 所有 milestone checkbox 已勾
- [ ] `python3 -m py_compile glas/app/gds_align_tool.py`
- [ ] 新增純函式測試通過（overlay 落點 / 建表 / 分箱 / median / status）
- [ ] 手動（user 本地）：單張 Preview 看 before/after 貼合；Run all 後總覽表排序/篩選/點列
  跳轉、看直方圖與散點、按 median→δ 後重跑看殘差收斂
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 最終 SESSION_LOG 條目註記 `完成 [F5]`
- 從 `CLAUDE.md` §8 移除 [F5]
- 本檔保留作 design history
