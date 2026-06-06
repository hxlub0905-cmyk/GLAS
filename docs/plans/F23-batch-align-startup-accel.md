# [F23] Batch Align 啟動延遲加速（注入索引 + 常駐/預熱 process pool）

> **狀態：** done (2026-06-07)
> **§8 ID：** [F23]
> **建立：** 2026-06-06
> **負責 branch：** claude/f23-batch-align-startup-accel

---

## Goal & Context

**現象（user 回報）：** 每次按 **Batch Align → Run all** 之後，第一張影像真正開始算
之前都有一段明顯「啟動時間」，UI 像卡住。

**根因（已實測 / 追碼確認）：** `FineAlignAllWorker._run_process_pool()`
（[gds_align_tool.py:1501](../../glas/app/gds_align_tool.py)）每次都**重新**建立一個 spawn-based
`ProcessPoolExecutor`，用完即 `shutdown(wait=True)` 拆掉。啟動成本由兩塊組成、每次按都重付：

1. **Spawn K 個直譯器 + 重 import numpy/cv2/shapely/oasis 鏈。** Windows 無 fork，
   每 worker 冷啟。實測本機：k=4 → 0.28s、k=8 → 0.38s、k=20（每核一個）→ 0.87s。
   這是與 OASIS 檔無關的「地板」成本。
2. **每個 worker 各自重跑一次 `scan_cell_offsets`。** `_pool_init`
   （[fine_align.py:463](../../glas/core/fine_align.py)）在每個 worker 重建
   `RandomAccessReader`，其 `__init__`（[oasis_random.py:599](../../glas/core/oasis_random.py)）
   會把整個 **name table** 重掃一遍建 `by_refnum / by_name / layernames / S_BOUNDING_BOX` 索引。
   **但主行程的 `self._rar` 早已把這份索引建好**（全是純 dict、可 pickle），現在卻讓 K 個
   worker 各自從零再掃一次同一張表 —— 隨檔案 cell 數放大的純浪費，大檔通常比 (1) 還顯著。

**成功長相：**
- 重複按 Run all（同檔同 POI）時，啟動近乎零等待（pool 已常駐 / 已預熱）。
- 即使第一次跑 / pool 需重建，也省掉 K× name-table 重掃（注入索引）。
- 結果**位元/數值完全不變**（§7 不變式：per-image 工作與順序無關）。

**與現有系統關係：** 延伸 F8（batch 改 process pool）+ F14（worker 數 / cv2 單執行緒）+
F6（mmap 一次 / clone reader）。不改對位數學、不改 per-image 演算法，只改「reader 怎麼進
worker」與「pool 生命週期」。

---

## Q&A Decisions

### Q1: 啟動加速採哪個方向？
**選項：** 注入既有索引 / 常駐·預熱 pool / 降低 worker 數預設 / 只改 UX 進度提示
**選擇：** **注入既有索引 + 常駐·預熱 pool**（兩者並做）
**理由：** 注入索引消掉與檔案複雜度成正比的重掃浪費、且第一次跑就受益、零行為風險；
常駐/預熱 pool 把 spawn 地板成本在第二次以後攤掉、並可在背景預熱隱藏首次延遲。兩者正交、可疊加。

---

## Milestones

> 粒度：每個 milestone 一個 session 可完成。M1 風險最低、先做；M2 依賴 M1 的 reader 建構路徑。

### M1: 注入既有索引，worker 跳過 `scan_cell_offsets`  [status: done 2026-06-06]

把主行程 `self._rar` 已建好的索引 dict 透過 initargs 傳進 worker，worker 重建 reader 時跳過重掃。

- [x] `RandomAccessReader.__init__` 新增 `prebuilt_index: Optional[dict] = None` kwarg：
      有值時用它取代 `oas.scan_cell_offsets(path, shared_buf=shared)`（其餘 mmap / OasisReader
      建構不變，因為 worker 仍需 mmap 做幾何解碼）。
- [x] `RandomAccessReader` 提供 `index_snapshot() -> dict`：回傳與 `scan_cell_offsets` 同形狀
      的 dict。實作：`__init__` 保留 `self._idx = idx` 參照（注入路徑下 `self._idx = prebuilt_index`）。
      `clone()` 也改成轉發 `self._idx`（in-thread fallback 的 clone 同樣免重掃）。
- [x] `fine_align._pool_init` 簽名加 `prebuilt_index`，傳入 `RandomAccessReader(...)`。
- [x] `_run_process_pool` 的 `initargs` 加 `rar.index_snapshot()`。
- [x] **驗證：** `TestPrebuiltIndex`（test_oasis_random.py）：注入索引 reader 與重掃 reader 的
      `by_refnum/by_name/unit/layernames/sbbox/offset_flag/offsets_via` 全等、ROI 幾何一致、
      sbbox 一致、clone 共用同一 index 物件。+ smoke：snapshot 過 pickle 邊界後 `_pool_init` 正常解碼。
- [x] **驗證：** `pytest tests/test_oasis_random.py`（41）+ `-k "fine_align or batch or pool"`（17）全綠。

### M2: 常駐 / 背景預熱 process pool  [status: done 2026-06-07]

讓 pool 跨批次存活，並在 OASIS+POI 就緒時於背景預先 spawn，隱藏首次延遲。

- [x] **拆解 `_pool_init` 的 per-batch 與 per-reader 狀態：** `root / poi_specs / cfg` 從 worker
      globals 移出 → 改成 **per-task 參數**隨 `_pool_task(job, root, specs, cfg)` 傳入；
      `_pool_init(path, wanted, dtype, bbox, prebuilt_index)` 只建 reader（少變）。三者本就已 pickle
      過 spawn 邊界，無新風險。
- [x] 新增 `fine_align._BatchPool`（Qt-free，session 單例 `fine_align.batch_pool`）：
      持有一個 `ProcessPoolExecutor` + key `(path, frozenset(wanted), dtype.__name__, bbox, workers)`。
      `get(...)`：key 相同回傳現有；不同則 `shutdown(wait=False)` 舊的、建新的。`RLock` 保護
      （預熱在背景執行緒、batch worker 在另一執行緒）。
- [x] `_run_process_pool` 改用 `fine_align.batch_pool.get(...)`，**不再** `finally: shutdown`；
      `_pool_task` 加傳 `self._root/_poi_specs/_cfg`；cancel 仍只 drop 未起跑 future（行為不變）。
- [x] **預熱：** `MainWindow._maybe_prewarm_batch_pool()` 在背景 daemon thread 呼叫
      `batch_pool.ensure_warm(...)`（每 worker submit 一個 probe 強制 boot+建 reader）。
      觸發點：`_on_pois_changed`、`_on_load_klarf`、`_on_load_folder`。門檻：reader+POI 就緒、
      SEM 影像 >2（小批走 in-thread 不需 pool）、`_prewarming` 防重入。`ensure_warm` 對同 key 冪等。
- [x] **生命週期收尾：** `MainWindow.closeEvent` → `batch_pool.shutdown()`；開新 OASIS
      （`_on_open_roi` 提交新 reader 處）→ `shutdown()` 釋放舊檔的 worker。
- [x] **記憶體註記 + idle-timeout：** 常駐 pool 整 session 佔 K 份 mmap + K 份索引（已寫進
      `_BatchPool` docstring + SESSION_LOG）。**已實作** idle auto-release：`_BatchPool(idle_timeout=)`
      預設 `_POOL_IDLE_TIMEOUT_S=300s`，閒置（無批次在跑）逾時自動 `shutdown` 釋放 worker。
      安全機制：批次以 `lease()`（busy refcount）持有，timer 只在 `_inuse==0` 時武裝、acquire 時取消，
      故**絕不會在批次跑到一半時殺 worker**；timer 在自身執行緒 fire，`_on_idle_fire` 再驗 `_inuse==0`。
- [x] **驗證：** `TestBatchPoolManager`（fake executor）：同 key 重用、key 變重建+關舊、shutdown
      清空、`ensure_warm` 冪等；idle fire 在閒置時釋放、批次中不釋放、lease 取消後重新武裝 timer、
      短 timer 端到端 fire。+ real-spawn smoke：`lease()` 結果 == 順序、第二批重用暖 pool、閒置逾時自動釋放。
- [x] **驗證：** `pytest tests/`：695 passed（唯一 fail = `test_cellcache::test_save_load_and_invalidate`
      為既有 Windows 暫存路徑問題，clean tree 同樣 fail，與 F23 無關）。

---

## Affected Files

- `glas/core/oasis_random.py` — `RandomAccessReader.__init__`（+`prebuilt_index`）、`index_snapshot()`
- `glas/core/fine_align.py` — `_pool_init` / `_pool_task` 簽名（M1+M2）、（M2）pool 管理器或其 helper
- `glas/app/gds_align_tool.py` — `_run_process_pool`（initargs / 改用管理器）、預熱觸發、close 收尾
- `tests/test_oasis_random*.py` 或新檔 — 注入索引等價性測試
- `docs/plans/F23-batch-align-startup-accel.md`（本檔）、`SESSION_LOG.md`、`CLAUDE.md` §8

---

## Risks / Open Questions

- **§7 不變式：** per-image 工作必須與順序、與「reader 怎麼建」無關。注入索引後 reader 的
  `by_refnum / sbbox` 必須與重掃完全相等 → M1 等價性測試把關。
- **常駐 pool 記憶體：** K 份 mmap + 索引整 session 駐留。大檔 + 高核數需留意；本期以註解 +
  log 揭露，不做 idle-timeout。
- **預熱的取消競態：** POI/檔案在預熱途中變更，需確保舊預熱被取消或其結果被丟棄（key 不符即不用）。
- **pickle 邊界：** `root / poi_specs / cfg` 已證實可 pickle（現行 initargs 已過 spawn）；
  `index_snapshot()` 各欄位皆 dict/list/tuple/int，亦可 pickle。

---

## 驗證方式

- [ ] 所有 milestone checkbox 已勾
- [ ] `pytest tests/ -v` 全綠（特別是 oasis_random / fine_align / batch 相關）
- [ ] 手動：載大檔 → 選 POI → Run all 兩次，第二次啟動近零等待；改 POI 再跑結果正確
- [ ] 手動：批次對位輸出 CSV/JSON 與改動前數值一致（抽樣比對）
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 最終 SESSION_LOG 條目註記 `完成 [F23]`
- 從 `CLAUDE.md` §8 移除 [F23]
- 本檔保留作 design history
