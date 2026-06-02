# [F14] Batch fine-align + image/mask export 加速

> **狀態：** in progress (2026-06-02)
> **§8 ID：** [F14]
> **建立：** 2026-06-02
> **負責 branch：** claude/optimistic-pasteur-31ELv

---

## Goal & Context

**為什麼做：** user 回報 batch align 與 image/mask 匯出在 production（上萬張）規模下仍太慢。
探索後定位兩個瓶頸：

1. **Image/mask export（`OverlayExportWorker`）完全循序**——單一 reader、單 thread，每張圖
   逐張做 ROI walk（OASIS 解碼，GIL-bound）+ 畫 overlay/mask。這是最大的一塊，因為它**完全沒有
   平行化**，而 batch align 早在 F8 就多進程平行了。
2. **Batch align 的 worker 上限寫死 8**（`_auto_batch_workers`，cap 8 是為了避免 cv2 內部
   threading 過度訂閱）。多核機器（>8 核）核心閒置；且 cv2 每個 worker 都開多 thread，與多進程
   疊加造成 oversubscription，正是當初 cap 8 的原因。

**成功長相：** export 走 F8 同款多進程架構 → 接近 Nx 加速（N=worker 數）；align/export 兩條路徑
worker 數自動依核心數放大（在 worker 內 `cv2.setNumThreads(1)` 解除 oversubscription 顧慮），
並提供 UI 可調 worker 數（0=auto）。輸出結果與循序版**逐 byte 一致**（§7 無功能變更）。

**與現有系統關係：** 延伸 F8 的 process-pool 架構（`fine_align._pool_init/_pool_task`）到 export
路徑；不改對位/walk 演算法本身，只改**執行排程**與**並行度**。

**Q&A（plan 階段確認）：**
- 兩條路徑都要加速。
- 核心數不確定 → 自動偵測 cpu_count、用保守 cap、並讓 user 在 UI 調整。
- 可接受平行 export 同時開多個 OASIS reader（每 process 各佔一份索引記憶體；mmap 本體靠 OS page
  cache 共享）。

---

## Q&A Decisions

### Q1: 哪條路徑優先？
**選擇：** 兩者都要。
**理由：** export 完全沒平行（最大單點），align 受 cap 8 限制；一次處理兩條，且兩者共用同一套
process-pool + cv2-thread 策略。

### Q2: worker 上限怎麼定（cap 8 是否提高）？
**選擇：** 自動偵測核心數、提高 cap（8→16）、worker 內 `cv2.setNumThreads(1)`，並加 UI override
（0=auto，QSettings 持久化）。
**理由：** user 不確定核心數；自動放大 + 安全上限 + 可手調，兼顧未知環境與大核機器。cv2 單執行緒
讓「多進程 × 多 cv2 thread」的 oversubscription 不再是限制因子。

### Q3: 平行 export 的記憶體（多 reader）？
**選擇：** 可接受。每 worker 各建一份 reader（同 F8 align）。
**理由：** OASIS 檔以 mmap 開，實體頁靠 OS 共享；額外成本主要是 per-process 的 offset 索引，
峰值 ≈ 單 reader × worker 數，user 已確認可接受。

### Q4: 要不要快取 align 階段的 ROI 幾何給 export 重用（避免重複 walk）？
**選擇：** 本期**不做**，列為後續選項。
**理由：** align 與 export 的 POI 可能不同、幾何量大（上萬張×多 polygon）記憶體風險高、生命週期
複雜。平行化已能讓兩條路徑各自接近 Nx，先取低風險高回報的並行化。

---

## Milestones

### M1: 抽出 Qt-free export render 模組  [status: done (code; pytest+實測待 user)]

- [x] 新增 `glas/core/overlay_export.py`（Qt-free，只依賴 numpy/cv2/shapely via gds_boolean）：
  - [x] 把 `overlay_outlines_on_sem` + `_draw_polyline_np` 從 app **原樣搬入**（兩者已是 Qt-free）
  - [x] 新增 `export_one_image(job, rar, root, poi_specs_colored, cfg, out_dir, export_raw,
        export_overlay, export_mask, mask_thr)`：做單張的 imread + raw/overlay/mask 寫出，回傳
        manifest row dict（沿用現有欄位 + `mask_png`）。內部用
        `fine_align.poi_polys_and_geometry_for_roi`（單 walk 共用）+ `gds_boolean.make_mask/
        union_geometries`，與目前 `OverlayExportWorker.run()` 的單張邏輯**逐行等價**
- [x] app 改從 `overlay_export` import `overlay_outlines_on_sem`（preview `_on_preview_template`
      仍可用），移除 app 內重複定義
- [ ] 驗證：`py_compile`；既有 overlay/preview 測試仍綠

### M2: Export 多進程平行化  [status: done (code; pytest+實測待 user)]

- [x] `overlay_export.py` 加 `_export_pool_init` / `_export_pool_task` + 模組全域 `_GE`
      （鏡像 `fine_align._pool_init/_pool_task`：worker 內重建 reader、cache batch context）
- [x] 重寫 `OverlayExportWorker.run()`：
  - [x] 小批（≤2 或 workers≤1）→ in-thread 跑 `export_one_image`（循序 fallback）
  - [x] 大批 → `ProcessPoolExecutor`（spawn），`as_completed` 收 row、emit progress
  - [x] cancel：drop 未啟動 futures（同 `FineAlignAllWorker._run_process_pool`）
  - [x] 全部完成後在 orchestrator（app）寫 manifest（順序依 image_id 穩定排序，確保與循序版一致）
- [ ] 驗證：M4 整合測試（檔案 + manifest）；循序 vs 平行輸出一致

### M3: worker 數自動放大 + cv2 oversubscription + UI 可調  [status: done (code; pytest+實測待 user)]

- [x] `_auto_batch_workers`：改 `min(cpu_count, 16)`（cap 8→16）；接受 override 參數
- [x] `fine_align._pool_init` 與 `overlay_export._export_pool_init` 內 `cv2.setNumThreads(1)`
      （worker process 限定，主行程不變）
- [x] UI：新增「Parallel workers (0 = auto)」spinbox（FineAlignPanel 或設定區），QSettings 持久化；
      align 與 export 兩條路徑都讀此值（0→auto）
- [ ] 驗證：worker 數解析 helper 單測；多核機手動實測加速

### M4: 測試 + 文件  [status: done (code; pytest+實測待 user)]

- [x] `tests/test_export_perf.py`：
  - [x] `test_worker_count_resolver`：override / auto / cap 邊界
  - [x] `test_export_one_image_raw_only`：coarse=None / raw-only 不需 reader，row 正確 + 純函式
        二次呼叫同結果（平行==循序 by construction）
  - [x] `test_export_one_image_missing_file`：exists=False → missing-file、不寫檔
  - [x] `test_export_one_image_no_poi_no_mask`：要 mask 但無 POI → 不 walk、不寫 mask
  - [x] `test_overlay_export_module_is_qt_free`：模組無 PyQt6 依賴（spawn worker 可 re-import）
  - 說明：score-gate 由 F13 `test_mask_should_export` 覆蓋；真正的 pool 平行↔循序等價走手動驗證
- [x] README / CLAUDE §5.2 補一句並行模型
- [ ] 驗證：`pytest tests/ -v` 通過（**待 user 本地**）

---

## Affected Files

- `glas/core/overlay_export.py`（**新增**：搬入 render helper + `export_one_image` + pool entry）
- `glas/app/gds_align_tool.py`（`OverlayExportWorker` 改 orchestrator、`_auto_batch_workers`、
  import 調整、UI worker 數控制）
- `glas/core/fine_align.py`（`_pool_init` 加 `cv2.setNumThreads(1)`；接受 worker override 由 app 傳）
- `tests/test_export_perf.py`（新增）
- `README.md` / `CLAUDE.md`（並行模型一句）

---

## Risks / Open Questions

- **搬移 `overlay_outlines_on_sem` 的相容性**：preview 與 export 都用它；搬到 core 後 app 必須
  re-import，且行為需逐 byte 一致（純搬移、不改邏輯）。
- **manifest 順序一致性**：平行完成順序非確定 → 收集後須依 image_id（或原 job 序）穩定排序再寫，
  才能與循序版輸出一致、可重現。
- **spawn 成本**：每 worker 重建 reader 有固定成本；小批維持 in-thread fallback 避免得不償失
  （沿用 F8 的 ≤2 判斷）。
- **cv2.setNumThreads(1) 範圍**：只在 worker process 設定；主行程的單張 align/preview 不受影響。
- **後續選項（非本期）**：快取 align ROI 幾何給 export 重用（Q4），可再省一次 walk，但需處理
  POI 差異 + 記憶體。

---

## 驗證方式

- [x] 所有 milestone 程式碼 subtask 完成（`py_compile` 通過）
- [ ] `pytest tests/ -v` 通過（含新 `test_export_perf.py`）（**待 user 本地**——沙箱無 numpy/PyQt6）
- [ ] 手動：多核機跑 Run all + Export，確認 CPU 多核吃滿、時間明顯下降；調整 UI worker 數生效
- [ ] 輸出檔案 + manifest 與循序版一致（抽樣比對）
- [x] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 在最終 SESSION_LOG 條目註記 `完成 [F14]`
- 從 `CLAUDE.md` §8 移除該任務
- 本檔保留為 design history
