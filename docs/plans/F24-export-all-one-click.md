# [F24] Export all：一鍵「補跑未跑的 fine-align → 整包匯出」

> **狀態：** done (2026-06-29)
> **§8 ID：** [F24]
> **建立：** 2026-06-29
> **負責 branch：** claude/glas-project-progress-vzu25j

---

## Goal & Context

**動機（user 回報）：** 實際工作流是「手動 Run 個 3-4 張確認對位沒問題 → 整包匯出下游產物」。
現行得先按「Run all images」把每張都跑完、再按「Export alignment」兩步；user 覺得 Run all
這個獨立步驟多餘，想要一鍵把「補跑未跑的 fine-align + 匯出」做完。

**現況關鍵事實（已追碼確認）：**
- `self._refined: dict`（image_id → (dx, dy, score)）是 fine-align 結果的唯一來源；單張 Run
  與 Run all 都 merge 進這裡（`_on_fa_result` 逐張更新、不洗整碗）。
- 匯出（`_on_export_alignment` → `OverlayExportWorker`）讀 `self._refined`：CSV/JSON 對沒跑的
  影像標 `status="not-run"`、欄位空白；**gray/label 產物被 `mask_should_export(refined, thr)`
  gate 擋掉**（refined is None 或分數未過 → 不寫）。故「只跑 3-4 張就整包匯出」目前只會拿到
  那 3-4 張的圖。

**成功長相：**
- FineAlignPanel 上「Run all images」按鈕改成「Export all…」。按下去：
  1. 找出**有座標但還沒在 `_refined` 裡**的影像，只對這些補跑 fine-align（複用已確認的 3-4 張）。
  2. 補跑完成後自動開啟既有的 Export 對話框（影像預選全部），user 選格式/產物/存檔位置。
  3. 若全部都已跑過 → 直接開 Export 對話框、不補跑。
- 每張仍各自有 fine-align 修正（品質不變），只是少按一個獨立步驟。
- 補跑被 cancel 或 fail → **不自動匯出**（避免拿半套結果出圖）。

**與現有系統關係：**
- **取代** Run all 按鈕（user 決定）；保留單張「Run」按鈕（用來確認 3-4 張）與既有
  「Export alignment」入口（純匯出當前 `_refined` 狀態，不補跑）。
- 完全複用 `FineAlignAllWorker`（補跑）+ `_on_export_alignment`（匯出），不改對位數學、
  不改 per-image 演算法、不改 export gate。

---

## Q&A Decisions

### Q1: 匯出要不要保留每張 fine-align？
**選項：** A=coarse-only 整包出圖（放寬 gate）/ B=每張仍 fine-align，只合成一鍵
**選擇：** **B**
**理由：** user 的「Run 3-4 張」是出手前的人工 sanity check，不是宣告 coarse 已夠準；每張殘差
仍要各自 matchTemplate 修正，否則下游 label map 會被殘差靜默拉歪。故只做 UX 合併、品質不變。

### Q2: 已經跑過、確認過的 3-4 張要不要重算？
**選項：** 跳過已跑（只補跑未跑）/ 全部重算
**選擇：** **跳過已跑，只補跑未跑**
**理由：** fine-align 是確定性的，重算結果一樣 → 跳過純粹省時間，且尊重 user 已確認的那幾張。
「未跑」定義：`img.has_coords and img.image_id not in self._refined`。

### Q3: 現有「Run all images」按鈕怎麼處理？
**選項：** 取代成 Export all / 保留 Run all 另加 Export all / 移除 Run all 由 export 流程自動補跑
**選擇：** **取代成「Export all…」**
**理由：** user 明言 Run all 用不到了；取代最乾淨、UI 不長新鈕。單張 Run 保留作 3-4 張確認用。

---

## Milestones

### M1: 補跑子集 helper + 一鍵 Export all 串接  [status: done 2026-06-29]

- [x] `fine_align.py` 新增純函式 `images_needing_fine_align(images, refined) -> list`：
      回傳「有座標且 image_id 不在 refined」的影像（保持 dataset 順序）。對齊既有
      `rerun_image_subset` 的風格、Qt-free、可單元測試。
- [x] FineAlignPanel：`_run_all_btn`→`_export_all_btn`，文案「Export all…」、tooltip 改述「補跑未跑的
      fine-align 後整包匯出」；signal `run_all_requested` → `export_all_requested`；`_update_enabled`
      仍控制其 enable。
- [x] MainWindow 新 handler `_on_export_all()`：
      - `todo = fine_align.images_needing_fine_align(self._sem_images, self._refined)`。
      - `todo` 空：直接 `self._on_export_alignment()`（在 cv2/specs 等 guard 之前 early-return）。
      - `todo` 非空：過 cv2 / specs / rar+roi / fov guard → `self._export_after_fa = True`，對 `todo`
        建 jobs → `_enter_batch_workspace()` + `_refresh_batch_panel()` + `_launch_fa(...)`。
- [x] `_on_fa_finished`：若 `self._export_after_fa` → 清旗標、`QTimer.singleShot(0, _on_export_alignment)`
      （延一個 event-loop tick，讓 batch QThread 收尾後再開 modal）；`_on_fa_cancelled` / `_on_fa_failed`：
      清旗標、**不**匯出。
- [x] `__init__` 初始化 `self._export_after_fa = False`；訊號接線改到 `_on_export_all`。
- [x] 驗證：`pytest tests/` 724 passed。

### M2: 測試 + 文件  [status: done 2026-06-29]

- [x] 新增 `tests/test_gds_align_f24.py`：`images_needing_fine_align`（全未跑 / 部分已跑只回未跑 /
      全已跑回空 / 無座標排除 / 順序保持 / None refined）+ `colorize_label_map`（上色 / bg / 未對應 id /
      全黑變可視）+ `_on_export_all` GUI（無影像不動 / 全已跑直接匯出 / 只補跑未跑 / finished 接匯出 /
      無旗標不匯出 / fail+cancel 清旗標）共 17 項。
- [x] README / CLAUDE.md §5.2 把流程敘述更新為「Export all 一鍵（補跑未跑 + 匯出）」+ label_view 註記。
- [x] `SESSION_LOG.md` 新增條目（§8 未曾登錄 F24，故無需移除）。

---

## Affected Files

- `glas/core/fine_align.py` — 新 `images_needing_fine_align`
- `glas/app/gds_align_tool.py` — FineAlignPanel 按鈕/signal、`_on_export_all`、`_on_fa_finished`
  / `_on_fa_cancelled` / `_on_fa_failed` 旗標、`__init__`、接線
- `tests/test_accel_equivalence.py`（或新檔）— `images_needing_fine_align` 單元測試
- `tests/test_gds_align_f24.py`（新）— Export all 串接 / 旗標 GUI 測試
- `docs/plans/F24-export-all-one-click.md`（本檔）、`README.md`、`CLAUDE.md`、`SESSION_LOG.md`

---

## Risks / Open Questions

- **§7 不變式：** 不改 export gate、不改對位數學；補跑只 merge 未跑的 image，已確認的 `_refined`
  原封不動 → 數值結果與「Run all 後 Export」逐張等價。
- **取消競態：** 補跑進行中 user 按 cancel → `_on_fa_cancelled` 清旗標、不匯出（已涵蓋）。
- **無座標影像：** 永遠無法 fine-align（coarse None），不納入補跑；匯出沿用既有 not-run 行為。
- **Export 對話框仍會出現：** 一鍵指的是「免按 Run all」，格式/產物/存檔位置仍由既有對話框選
  （非靜默匯出，避免覆蓋檔案）。如 user 想連對話框都免，可後續再議。

---

## 驗證方式

- [ ] 所有 milestone checkbox 已勾
- [ ] `pytest tests/ -v` 全綠（特別是 fine_align / gds_align 相關）
- [ ] 手動：載 KLARF + OASIS + POI → 單張 Run 確認 3-4 張 → 按「Export all…」→ 只補跑其餘影像 →
      匯出 CSV/gray/label，確認所有影像都有產物、且先前 3-4 張的 offset 未變
- [ ] 手動：全部已跑過 → 按「Export all…」直接開匯出對話框、不重跑
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 在最終 SESSION_LOG 條目註記 `完成 [F24]`
- 從 `CLAUDE.md` §8 移除 [F24]
- 本檔保留作 design history
