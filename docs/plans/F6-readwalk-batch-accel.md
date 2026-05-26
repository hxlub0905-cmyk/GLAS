# [F6] OAS 讀取與批次 fine-align 加速（mmap + 共享 map + thread-pool 批次）

> **狀態：** in progress（M1–M3 程式碼完成，沙箱已過 numpy-free 等價驗證；numpy/cv2/PyQt6
> 相依的等價測試與實機效能待 user 本地驗收）
> **§8 ID：** [F6]
> **建立：** 2026-05-25
> **負責 branch：** claude/dazzling-cori-5T7XE

---

## Goal & Context

**動機現象：**
1. **OAS 讀取／記憶體**：`OasisStream.__init__`（`oasis_streamer.py:214`）開檔即 `base.read()` 整檔
   slurp 進 RAM。對 **random-access ROI 模式**（只碰少數 cell）這是浪費——數百 MB 檔全進 RAM。
2. **雙重 slurp**：`RandomAccessReader.__init__`（`oasis_random.py:215-218`）建一個持久 `OasisReader`
   （slurp 整檔），緊接 `scan_cell_offsets(path)`（`oasis_streamer.py:2259` **又**新建一個 OasisReader
   → 第二次整檔讀），而 scan 只需 name-table 前綴（讀到第一個 CELL 就 break，:2316）。
3. **批次 fine-align 單執行緒**：`FineAlignAllWorker`（`gds_align_tool.py:1116`，docstring 明寫
   *"Sequential (one reader, no concurrent access)"*）每張影像 `imread → walk ROI → 合成 template
   → matchTemplate` 完全循序，但各影像彼此獨立，是 embarrassingly parallel。

**成功長相：**
- ROI 模式開大檔不再整檔進 RAM（mmap 只 page-in 真正讀到的 cell）；開檔不再 slurp 兩次。
- `Run all` 批次在多核機器上有實質加速（cv2 重活釋放 GIL）。
- **功能逐 byte / 逐數值不變**：用 golden-output 等價測試把「不掉功能」變成可驗證關卡。

**與現有系統關係：** 純內部加速，**不改演算法、不改任何對位不變式（CLAUDE.md §7）**。
mmap 與 thread-pool 都帶 fallback（無 fileno → slurp；cv2 缺 → 既有行為），舊路徑零行為變化。

可重用（探索結論）：`FineAlignAllWorker` 已用 `threading.Event` cancel + 逐張 `result` signal
（key=image_id，順序無關）；`RandomAccessReader` 已有 `_memo/_reach_memo`；offset index
（`by_refnum/by_name/unit/layernames`）是純 dict，可跨 thread 共享。

---

## Q&A Decisions

### Q1: B1 批次平行化模型
**選項：** thread pool / process pool
**選擇：** **thread pool**（每 thread 各自 reader、共享同一份 mmap）
**理由：** cv2.imread/matchTemplate/GaussianBlur 釋放 GIL → 仍有實質加速；改動小、不需 pickle、
不需把 reader 從 path 重建、共享位址空間。風險最低的第一步。若日後 profiling 顯示 GIL-bound
（Python ROI walk 佔比過高），再評估升級 process pool（另開 milestone）。

### Q2: 批次 worker 數量
**選擇：** **自動** = `min(os.cpu_count() or 1, 上限 8)`。不加 UI 控制項、不動持久化 schema。

### Q3: A1 mmap 套用範圍
**選擇：** **只用在 ROI/隨機存取路徑**（`RandomAccessReader` 與 `scan_cell_offsets`）。
整檔 bulk decode（`oasis_store` 全 walk）維持 slurp——它本來就讀完整檔，slurp 的 in-RAM bytes
逐-byte 索引比 mmap 快，不想拖慢；且不動到 ~218 測試的既有讀檔路徑。

---

## Milestones

### M1: A1 — mmap-backed OasisStream（ROI 路徑，bulk 維持 slurp）  [status: done — code + sandbox numpy-free 等價過]

- [x] `OasisStream.__init__(base=None, *, use_mmap=False, shared_buf=None)`：`use_mmap` 且 `base` 有真實
  `fileno()` → `mmap.mmap(fileno, 0, ACCESS_READ)` 當 `_buf`（持有 base 至 close）；decode 沿用
  `buf[pos]`(int)/`buf[a:b]`(bytes)/`len(buf)`。
- [x] **fallback**：mmap 失敗（BytesIO/平台/空檔）→ `except (ValueError, OSError, io.UnsupportedOperation,
  AttributeError)` 退回 `base.read()`，行為與今日相同。
- [x] `close()`：mmap → `_mmap.close()` + 關 base；slurp → `_buf=b""`。`_stack`/`read/tell/seek` 不變。
- [x] `OasisReader` / `scan_cell_offsets` 加 `use_mmap=False`（預設 False，bulk 不變）；`RandomAccessReader`
  內部走 mmap。
- [x] 驗證：py_compile + `TestOasisStreamMmapEquivalence`（read 原語/iter_records/scan index 三者 mmap↔slurp
  相等、BytesIO fallback）**沙箱實跑過**；numpy-gated `load_cell` 幾何相等待本地。

### M2: A2 — 單一 map 共享給 offset-scan 與持久 ROI reader（去雙重 slurp）  [status: done — code + sandbox numpy-free 等價過]

- [x] `OasisStream/OasisReader/scan_cell_offsets(shared_buf=...)`：包外部擁有的 buffer，各自 `_pos`，
  `close()` 只丟自身 ref、不關共享 map。
- [x] `RandomAccessReader.__init__`：建一個 owning mmap `OasisStream`，`_buf` 同時給 persistent reader 與
  `scan_cell_offsets` → 檔案只 map 一次、index 只算一次。
- [x] `RandomAccessReader.close()` + `__enter__/__exit__`：先關共享 wrapper、再關 owning map；idempotent。
- [x] 驗證：py_compile + `TestSharedMapEquivalence`（共享 map scan/iter_records 與獨立呼叫相等、關 wrapper
  後 owner map 仍可用、close idempotent）**沙箱實跑過**。

### M3: B1 — thread-pool 批次 fine-align（per-thread 獨立 reader）  [status: done — code 完成，等價測試待本地相依]

- [x] 抽出純函式 `_fine_align_image(job, rar, root, poi_specs, cfg, cancel_is_set)`（無共享可變狀態）。
- [x] `FineAlignAllWorker.run()` 改 `ThreadPoolExecutor(max_workers=_auto_batch_workers())`
  （`min(cpu_count, 8)`）；每 worker thread 經 `threading.local` 用 `rar.clone()` 取私有 reader、
  結束逐一 close。
- [x] **per-thread 獨立 reader（取代原「共享單一 map+index」構想）**：clone 各自 mmap，OS page-cache 共享
  → 不耗 N× RAM；零共享可變狀態 → 結果逐值等於循序。index 重掃（≤8 次 name-table）成本可忽略，換完全
  獨立、無生命週期風險。`RandomAccessReader.clone()` + `_init_wanted`。
- [x] **結果一致性**：signal 由 run() 單一 thread 在 future 完成時 emit（pool thread 只算）；順序無關。
- [x] **cancel**：`threading.Event`，task 起點 + `poi_polys_for_roi(cancel_cb=...)` 讀 `is_set()`；cancel 後
  跳出收集、pool `__exit__` 等 in-flight（快速 bail）、保留已完成結果。
- [x] **cv2 執行緒決策**：**維持 cv2 預設不動**——`setNumThreads` 全域且會影響單張路徑、且可能改變 float
  加總順序而動 score；為保證 golden 等價，不採用任何改變數值的設定。
- [x] 驗證：py_compile + `TestBatchParallelEquivalence`（循序 vs 4-worker pool 每張 tuple 相等）；沙箱無
  numpy/cv2/PyQt6 → 待本地實跑。

### M4: 等價性與效能總驗收  [status: in progress — 等價測試本地全綠（170 passed）；實機效能/GUI/mmap 記憶體驗收待本地]

- [x] golden 測試彙整於 `tests/test_accel_equivalence.py`（mmap↔slurp / 共享map↔獨立scan / 循序↔並行）。
- [x] 本地 `pytest tests/test_accel_equivalence.py tests/test_oasis_random.py tests/test_oasis_streamer.py -v`
  全綠（含 numpy/cv2/PyQt6-gated）。（2026-05-26 user 本地 Python 3.9.7：170 passed）
- [ ] 實機效能、GUI 批次加速、大檔 mmap 記憶體下降由 user 本地驗收。

---

## Affected Files

- `glas/core/oasis_streamer.py`（`OasisStream` mmap + fallback + close、`OasisReader` use_mmap /
  既有-buffer 建構路徑、`scan_cell_offsets` use_mmap）
- `glas/core/oasis_random.py`（`RandomAccessReader` 單次 map 共享、以既有 mmap+index 建 reader 路徑）
- `glas/app/gds_align_tool.py`（`FineAlignAllWorker.run` 改 ThreadPoolExecutor + per-thread reader +
  共享 mmap/index + cancel 傳遞；`_on_run_fine_align_all` 傳 path/config 供 per-thread reader 建立）
- `tests/test_accel_equivalence.py`（新增：mmap↔slurp / 共享map↔獨立scan / 循序↔並行 三組等價）
- `docs/plans/F6-readwalk-batch-accel.md`、`CLAUDE.md`（§8）、`SESSION_LOG.md`

---

## Risks / Open Questions

- **§7 不變式絕不動**：matchTemplate 符號、CE 邊界 early-stop（reachable_bbox 用 `load_cell_bbox`、
  walk 用完整 `load_cell`）、SemViewer 折疊——加速只換「資料來源/排程」，不換演算法。
- **mmap 平台差異**：Windows 下檔案被 map 期間不可被刪改（唯讀、本就不寫，可接受）；空檔 mmap 會
  丟例外 → fallback。fd 需在 reader 生命週期內保持開啟（mmap 釋放時一併關）。
- **共享 mmap 併發讀**：多個 `OasisStream`（各自 `_pos`）讀同一唯讀 mmap 是安全的（不寫）；
  `_memo/_reach_memo` per-reader 不共享，無資料競爭。
- **thread-pool GIL**：Python ROI-walk 受 GIL；若該段佔比過高，加速幅度有限 → 列為日後 process-pool
  升級的判準（本 plan 不做）。
- **cv2 執行緒互搶**：thread pool × cv2 內部多緒可能 oversubscribe；M3 評估後決定是否設 setNumThreads，
  但**不採用任何會改變 score 數值的設定**（避免 golden 測試失準與功能偏移）。

---

## 驗證方式

- [ ] 所有 milestone checkbox 已勾
- [ ] `python3 -m py_compile glas/core/oasis_streamer.py glas/core/oasis_random.py glas/app/gds_align_tool.py`
- [x] `pytest tests/test_oasis_random.py tests/test_oasis_streamer.py tests/test_accel_equivalence.py -v`
      全綠（含三組等價測試）（2026-05-26 本地：170 passed）
- [ ] 手動（user 本地）：開大 OASIS ROI 模式記憶體明顯下降；`Run all` 在多核機批次明顯變快、
      結果與加速前逐張一致；按終止仍即時停。
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 在最終 SESSION_LOG 條目註記 `完成 [F6]`
- 從 `CLAUDE.md` §8 移除該任務
- **本檔保留**，作為 design history
