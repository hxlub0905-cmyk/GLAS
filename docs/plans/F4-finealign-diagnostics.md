# [F4] Fine align 診斷：殘差疊圖 + 批次結果總覽

> **狀態：** planned
> **§8 ID：** [F4]
> **建立：** 2026-05-25
> **負責 branch：** claude/compassionate-dijkstra-84Gjd（PR #3）

---

## Goal & Context

F3 把 fine align 做成「多 POI 合成樣板 → 單次 matchTemplate → 單一偏移＋分數」，但
user 回饋功能仍太陽春，且最想要的是**結果可視化/診斷**。經 Q&A 收斂為兩個具體交付：

1. **殘差疊圖（overlay）** — 對位前 vs 對位後，把 GDS/樣板輪廓畫在 SEM 上，直接用眼睛
   確認幾何有沒有貼合。延伸現有 `TemplatePreviewDialog`。
2. **批次結果總覽** — `Run all` 後以可排序表格列出每張影像的 score＋偏移，能依 score
   排序/篩出低分項、點列跳到該影像，快速找出對不準的影像。

與現有系統關係：**延伸** F3，不改 fine align 演算法本身（matchTemplate / 符號不變），
只增加「看結果」的視圖。

---

## Q&A Decisions

### Q1: Fine align 先加強方向
**選擇：** 結果可視化/診斷（不是旋轉/縮放、不是子像素多階、不是邊緣比對）。

### Q2: 診斷視圖包含哪些
**選擇：** 殘差疊圖 (overlay) ＋ 批次結果總覽。
**未選（暫不做）：** Score 熱圖＋峰值、信心指標（peak sharpness / PSR）。

---

## Milestones

### M1: 殘差疊圖（preview 彈窗加 before/after overlay）  [status: planned]

- [ ] 新增 helper：給定 anchor，把可見 GDS 各層（或合成樣板）的**輪廓**（cv2 邊緣或
  polygon 外框）以層色畫在 SEM 灰階轉 RGB 之上，回傳 RGB ndarray。
- [ ] `TemplatePreviewDialog` 由 SEM/GDS/Template 三圖，擴充為含
  「對位前（coarse anchor）」與「對位後（coarse + `_refined` 偏移）」兩張 overlay，
  讓 user 直接比較貼合度（保留 Template pane 供參考）。
- [ ] `_on_preview_template` 準備 before/after 兩組 anchor 與 overlay 後傳入。
- [ ] 驗證：py_compile；overlay helper 以小 mask 純函式測試（邊緣像素落點正確）；
  彈窗實際顯示待 user 本地。

### M2: 批次結果總覽彈窗  [status: planned]

- [ ] 新增 `FineAlignResultsDialog`：`QTableWidget`（image_id / score / dx / dy / 狀態），
  可依欄排序，score 依 threshold 上色（綠/黃/紅），提供「只看低於 threshold」勾選。
- [ ] `Run all` 完成（`_on_fa_finished`）後彈出（或加 toolbar/panel「Results…」按鈕重開）；
  資料取自 `self._refined` 與 `self._sem_images`。
- [ ] 點列 → 選取並跳到該影像（重用 `_on_sem_image_selected` / list 選取路徑）。
- [ ] 驗證：py_compile；以假 `_refined` 建表的純邏輯測試（排序/上色/篩選）；
  互動跳轉待 user 本地。

---

## Affected Files

- `glas/app/gds_align_tool.py`（`TemplatePreviewDialog` 擴充、新 overlay helper、
  `FineAlignResultsDialog`、`_on_preview_template` / `_on_fa_finished` 接線）
- `tests/test_gds_align_m4b.py` 或新測試檔（overlay helper / 結果表純邏輯）

---

## Risks / Open Questions

- **環境**：sandbox 無 PyQt6/numpy/cv2，無法跑 GUI；以 py_compile＋純函式測試把關，
  overlay 與表格互動由 user 本地驗收。
- §7 不變式：fine-align 符號、SemViewer 折疊不可動；本 plan 只新增視圖、不改對位數學。
- overlay 的「對位後」anchor 需與實際 jump 後 overlay 一致（沿用 coarse + `_refined`）。
- 未選的 score 熱圖 / 信心指標留待後續，如 user 之後要再開 milestone。

---

## 驗證方式

- [ ] 所有 milestone checkbox 已勾
- [ ] `python3 -m py_compile glas/app/gds_align_tool.py`
- [ ] 新增 overlay / 結果表純函式測試通過
- [ ] 手動（user 本地）：單張 Preview 看 before/after 貼合；Run all 後總覽表排序/篩選/點列跳轉
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 最終 SESSION_LOG 條目註記 `完成 [F4]`
- 從 `CLAUDE.md` §8 移除 [F4]
- 本檔保留作 design history
