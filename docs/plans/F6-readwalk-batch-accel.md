# [F6] OAS 讀取與批次 fine-align 加速（mmap + 共享 map + thread-pool 批次）

> **狀態：** planned（待核准）
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

### M1: A1 — mmap-backed OasisStream（ROI 路徑，bulk 維持 slurp）  [status: planned]

- [ ] `OasisStream.__init__(base, *, use_mmap: bool = False)`：當 `use_mmap` 且 `base` 有真實
  `fileno()` → `mmap.mmap(fileno, 0, access=ACCESS_READ)` 當 `self._buf`，保留 fd（存 `self._fd`），
  decode 完全沿用 `buf[pos]`(回 int) / `buf[a:b]`(回 bytes) / `len(buf)`（mmap 三者語意同 bytes）。
- [ ] **fallback**：`mmap` 失敗（BytesIO 無 fileno、平台不支援、空檔）→ `except (ValueError, OSError,
  io.UnsupportedOperation)` 退回 `base.read()`，行為與今日完全相同（測試的 BytesIO 路徑不受影響）。
- [ ] `close()`：若 `_buf` 是 mmap → `_buf.close()` + 關 fd；否則維持 `_buf = b""`。`_stack`（CBLOCK
  解壓後的 bytes）與 `read/tell/seek/clear_substreams` 不變。
- [ ] `OasisReader.__init__` 加 `use_mmap: bool = False`，傳給 `OasisStream`；**預設 False（bulk 不變）**。
- [ ] 驗證：py_compile；新增等價測試——同一份合成 OASIS（沿用 `test_oasis_random.py` 既有 builder）
  分別以 `use_mmap=True/False` decode，斷言 `iter_records` 序列與 `RandomAccessReader.load_cell`
  的 rects/polys ndarray **完全相等**；BytesIO 仍走 slurp fallback。

### M2: A2 — 單一 mmap 共享給 offset-scan 與持久 ROI reader（去雙重 map）  [status: planned]

- [ ] `scan_cell_offsets(path, *, use_mmap=False)` 走 mmap（讀 name-table 前綴即 break，本就便宜）。
- [ ] `RandomAccessReader.__init__` 改為**檔案只 map 一次**：建立一個 mmap-backed `OasisStream`，
  讓「offset-scan pass」與「持久 geometry reader」共用同一個 mmap 物件（各自獨立 `OasisStream`／
  `_pos` 游標；mmap 唯讀、多 OasisStream 併讀安全）。新增 `OasisReader` 接「既有 buffer/mmap」的
  建構路徑（不重開、不重 map）。offset index 只算一次。
- [ ] 驗證：py_compile；斷言「共享 map 後算出的 `by_refnum/by_name/unit/layernames` 與獨立呼叫
  `scan_cell_offsets` 結果**完全相等**」；`RandomAccessReader` 既有測試全綠（行為不變）；
  以小檔確認檔案 handle/map 數量降為 1（可用 errors 清單為空 + load 結果一致間接驗證）。

### M3: B1 — thread-pool 批次 fine-align（per-thread reader 共享 map）  [status: planned]

- [ ] 批次驅動改用 `concurrent.futures.ThreadPoolExecutor(max_workers=auto)`（auto 見 Q2）。
  `FineAlignAllWorker` 仍是 QThread 進入點；`run()` 內部 submit 每張影像為一個 task。
- [ ] **per-thread reader**：GUI thread 先算好 offset index（一次），各 worker thread 經
  `threading.local` 建/重用一個 `RandomAccessReader`，**共享同一 mmap + 共享 offset index**
  （新增「以既有 mmap + 既有 index 建 reader」路徑，跳過重掃）。每 reader 各自 `_memo/_reach_memo`。
- [ ] **結果一致性**：每張影像 task 回傳 `(image_id, dx, dy, score, used_r, status)`，future 完成即
  emit `result`（順序無關，存 `_refined[image_id]`，最終狀態與循序版相同）。`progress` 計已完成數。
- [ ] **cancel**：沿用 `threading.Event`；每個 task 開頭檢查 `is_set()` 直接 return，並把
  `cancel_cb=event.is_set` 傳進 `poi_polys_for_roi`（中斷進行中的 walk）。cancel 後 pool 收尾、
  保留已完成結果（與 F5 M5 行為一致）。
- [ ] **cv2 過度訂閱**：評估在批次期間 `cv2.setNumThreads()` 的取捨（thread pool + cv2 內部多緒可能
  互搶）；若無明顯助益則維持預設、不動，並記錄結論。**不納入會改變數值結果的設定**。
- [ ] 驗證：py_compile；**golden 等價測試**——同一批 job 跑「舊循序版」vs「thread-pool 版」，斷言
  **每張影像的 (dx,dy,score,used_r,status) 完全相等**（worker 數 1 與 N 都測）。

### M4: 等價性與效能總驗收  [status: planned]

- [ ] 彙整 golden-output 測試成 `tests/test_accel_equivalence.py`：mmap↔slurp 幾何相等、
  共享 map↔獨立 scan 的 index 相等、循序↔並行的批次結果相等。
- [ ] （sandbox 無 numpy/cv2/PyQt6）py_compile 全過 + 純邏輯測試；**實機效能與 GUI 批次加速、
  大檔 mmap 記憶體下降由 user 本地驗收**（plan 末「驗證方式」）。

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
- [ ] `pytest tests/test_oasis_random.py tests/test_oasis_streamer.py tests/test_accel_equivalence.py -v`
      全綠（含三組等價測試）
- [ ] 手動（user 本地）：開大 OASIS ROI 模式記憶體明顯下降；`Run all` 在多核機批次明顯變快、
      結果與加速前逐張一致；按終止仍即時停。
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 在最終 SESSION_LOG 條目註記 `完成 [F6]`
- 從 `CLAUDE.md` §8 移除該任務
- **本檔保留**，作為 design history
