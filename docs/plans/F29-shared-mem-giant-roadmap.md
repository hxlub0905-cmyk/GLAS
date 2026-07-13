# [F29] LTV giant-cell 共享記憶體 + 效能/體驗 roadmap

> **狀態：** ⛔ **撤案（2026-07-07）** — M1 全實作 + 上 PR #19，但真機 LTV 驗收失敗後 revert。保留為 design history。
> **撤案原因：** `[export-timing]` 顯示 worker 實算僅 ~5s/張，但牆鐘 ~35s/張 → 每張 ~30s 空檔（per-task SHM descriptor
> 傳遞 + Windows pagefile-backed `shared_memory` 在低 free RAM〔11→7.2→4.4GB〕下 paging）。SHM 只共享 coords、未共享每
> worker ~GB 的 extent cache，**對 2-worker 比原本「每 worker np.load」更糟**。真正瓶頸是 flat giant 的 per-ROI 幾何
> （另需 spatial index，與記憶體重複無關）。詳見 SESSION_LOG 2026-07-07。
> **§8 ID：** [F29]（已移入 Backlog 標撤案）
> **建立：** 2026-07-06　**撤案：** 2026-07-07
> **負責 branch：** claude/code-review-handoff-65xwf4（PR #19，已關）

---

## Goal & Context

**觀察（真檔 log 分析）：** LTV（1750MB，單一 974MB 巨大 flat merge cell `_2_gri_yank_top`）export 時，
**每個 pool worker 各自 `np.load` 一份 974MB giant** → 8 份 = 7.8GB + 冷解暫態尖峰（2–3×）→ 峰值 15–20GB >
機器 12GB free → **OS paging → 整台電腦卡頓 + 第一波每張 88–132s**。ramp（M7n）把最壞從 510s 降到 132s（~4×），
但沒根治。對照 E3B（無 giant、階層密集葉）：CPU-bound、8 worker 全速、2.1 img/s、電腦不卡。
**根因不是檔案大小，是「單一 cell 記憶體 footprint × worker 數 > RAM」。**

**目標（M1）：** 把那顆 giant **載入一次到 `multiprocessing.shared_memory`，8 個 worker 共用同一份唯讀**
（1× 實體，不是 8×）→ thrash 消失、電腦不卡、LTV 可像 E3B 全速。估 LTV export **319s → ~110s（~3×）**。

**與現有關係：** 延伸 F27（M7k offset 快取、M7n ramp）與 F16-B（cellcache）。共享記憶體是 cellcache 逐 worker
`np.load` 的替代**捷徑**（僅對 giant），其餘 cell 照舊。ramp（M7n）在 SHM 生效後對 giant 變成不需要（M1.5）。

**可行性（已由 agent 審核，見 SESSION_LOG / 對話）：** ✅ walk 全程把 giant 陣列當**唯讀**（無 in-place 寫入
→ 唯讀 SHM 無 CoW 風險）；大陣列連續純 dtype 扁平（可 SHM-back）；注入點與發布點清楚。⚠️ caveat：`from_cache_arrays`
衍生的 dense `rr`/`names`（object，不能進 SHM）每 worker 重建（~70–150MB）；F23 常駐 pool 的 SHM 生命週期。

---

## Q&A Decisions

### Q1: 先做哪個優化？
**選項：** #1 共享記憶體 giant ／ #2 旋鈕（workers 硬上限 / factor）／ #3 更聰明 ramp
**選擇：** **#1 共享記憶體**（user 明示），並把「APP 進步空間」roadmap 一起排入 schedule。
**理由：** #1 是唯一能把 LTV 做到 E3B 等級（無 thrash、電腦不卡）的正解；旋鈕/ramp 只緩解。

### Q2: 唯讀共享記憶體可行嗎（walk 會不會就地改 giant 陣列）？
**選項：** 唯讀共享 ／ copy-on-write ／ 不可行
**選擇：** **唯讀共享**。**理由：** feasibility 審核確認 walk 全程唯讀（`Transform` frozen、emit 全 `.copy()`/新配置），
無任何對 `_rcol`/`_pcol`/`_pl_soa` 的 in-place 寫入。→ 單一實體唯讀副本可安全給所有 worker。

### Q3: 共享哪些、每 worker 還留什麼？
**選擇：** 共享**大的數值陣列**（`coords`/`rt`/`pts`/`off`/`pl__*`，contiguous 純 dtype）；每 worker 從小的
sparse side-table（`rri/rrv/nidx/nval`）重建 dense `rr`/`names`（object，~70–150MB）。**理由：** object 陣列/dict
不能進 SHM；重建成本遠小於 974MB 全複製。win = 7.8GB → ~2GB（足以消 thrash）。

---

## Milestones

> M1 是本案主體（大工程，分子任務）。M2–M5 為 user 要求排入的 roadmap，粒度較粗、依序推進。

### M1: 共享記憶體 giant cell（#1，最大效能槓桿）  [status: planned]

- [ ] **M1.0 可行性 gate** — ✅ 已完成（審核：walk 唯讀、陣列可共享、注入/發布點確認）。
- [ ] **M1.1 `sharedcell` 模組（Qt-free, 純 stdlib+numpy）**：
  - `publish(content) -> descriptor`：把 CellContent 的大數值陣列複製進 `shared_memory` 段，回傳 picklable
    descriptor（per-array `name`/`dtype`/`shape`，keyed by offset）+ 小的 sparse side-table（picklable）。
  - `attach(descriptor) -> CellContent`：以 `np.ndarray(shape, dtype, buffer=shm.buf)` **zero-copy** 掛大陣列，
    每 worker 重建 dense `rr`/`names`，組回 CellContent（與 `from_cache_arrays` 產物 **byte-identical**）。
  - `release(...)`：creator `unlink`、attacher `close`；POSIX/Windows resource_tracker 差異處理。
- [ ] **M1.2 `RandomAccessReader` 掛 `_shared_cells`（offset-keyed）**：`load_cell` 在 `cellcache.load` 前查表，
  命中則 `sharedcell.attach` 取代 `np.load`，memo 於 `_memo[cell_id]`。miss 照舊。
- [ ] **M1.3 orchestrator 發布 + 傳遞**（`ExportWorker._run_process_pool`）：pre-decode giant 後 `sharedcell.publish`，
  descriptor 隨 per-task context（比照 `index_snapshot()`）送每 worker；worker 首用時裝上 `_G["rar"]._shared_cells`。
- [ ] **M1.4 生命週期**：SHM 每 export 建立、跨 `batch_pool.lease`/`run_ramped` 存活、批後 `unlink`；worker `close`；
  descriptor 帶目前 SHM names，**跨批變更時 worker 失效舊 `_memo[gid]` + 重掛**（F23 常駐 pool 的 `_memo` 會殘留）。
- [ ] **M1.5 SHM 生效後 giant 免 ramp**：giant 不再逐 worker 佔記憶體 → `ram_capped_worker_count` 對 SHM giant 視為
  ~0 bytes（或 orchestrator 在 SHM 成功時把 ramp_initial 設回 workers）→ 全速起跑。HUD ramp 事件註明「shared → no ramp」。
- [ ] **M1.6 護欄 + 測試**：`tests/test_sharedcell.py`（publish→attach round-trip **byte-identical** 於
  `from_cache_arrays`；release 無洩漏；跨批 names 變更失效）；`test_export_fused` 仍 byte-identical。
- [ ] **M1.7 驗證**：全套 pytest 綠；**user 真機 LTV 驗收**（無 thrash、電腦不卡、~3× 快、HUD 顯示 shared/no-ramp）。

### M2: 更聰明的 ramp + 即時 RAM 護欄（記憶體安全網）  [status: planned]

- [ ] ramp「只在 worker **暖了**才 +1」（限制同時冷解數），修現在「前 2 個一起完成 → 一次放 4 cold」的 thrash。
- [ ] 即時 RAM 護欄：export 途中週期性讀 `available_ram_bytes()`，逼近上限時**暫緩提交**（in-flight 不再增），回穩再放。
  （對無 giant 但密集/大批、或 SHM 未生效的 fallback 都是安全網。）HUD 已有 RAM KPI，可加「⚠ RAM 逼近」標記。

### M3: UX / HUD 打磨  [status: planned]

- [ ] export **ETA / 剩餘時間**（由目前吞吐 × 剩張數估）進 HUD 總覽列。
- [ ] 第一波「暖機中 N/ramp」提示（有 giant 冷解時），讓等待可讀。
- [ ] HUD **level 篩選**（只看 warn/error 的一鍵 toggle）——大批時只看問題列。

### M4: 健壯性 — [B01] 中文路徑  [status: planned]

- [ ] 含中文的資料夾/檔名（KLARF / layout / 輸出夾）讀寫（Windows cp950 vs UTF-8）。查 `sem_loader` /
  `cv2.imread`（改 `np.fromfile`+`cv2.imdecode`）/ OASIS 開檔 / 匯出寫檔的路徑編碼。（既有 §8 [B01]，併入本 roadmap。）

### M5: 效能延伸（探索性）  [status: planned]

- [ ] native 化 placement / polygon(type-0/1) 解碼的後續（M7g 已做 type-0/1 point-list；placement 仍 Python）——
  先量真檔占比再決定（M7h 顯示對 LTV 大頭是 rect/placement）。**低優先、需 CI。**

### Backlog（暫不排期）

- dense `rr` 改「accessor 直接查 sparse」以省掉每 worker 重建（M1 的深化）。
- 整片 chip OASIS 匯出（F11 撤案）重啟評估。
- ramp 邏輯整體清理 / 抽成 policy。

---

## Affected Files（預期）

- `glas/core/sharedcell.py`（**新**：publish/attach/release）
- `glas/core/oasis_random.py`（`RandomAccessReader._shared_cells` + `load_cell` 掛 attach）
- `glas/app/gds_align_tool.py`（`ExportWorker._run_process_pool`：publish + 傳 descriptor + 生命週期 + M1.5 ramp）
- `glas/core/overlay_export.py`（`_afe_pool_task` / worker 首用裝 `_shared_cells`）
- `glas/core/fine_align.py`（pool context 帶 descriptor；M2 RAM 護欄）
- `glas/app/perf_panel.py` / `gds_align_tool.py`（M3 ETA/level 篩選）
- `glas/app/sem_loader.py` 等（M4 中文路徑）
- `tests/test_sharedcell.py`（新）、`test_export_fused.py`（護欄）

---

## Risks / Open Questions

- **Windows shared_memory 生命週期**（unlink 無作用、resource_tracker POSIX 過度 unlink 警告）——我在此環境**無法真機測 Windows**，
  M1.7 必須靠 user 真檔驗收；有執行風險，會分子任務小步驗。
- **byte-identical 護欄**：SHM-backed CellContent 必須與 `np.load` 路徑逐位相同（M1.6 測試把關）。
- **F23 常駐 pool 的 `_memo` 殘留**：跨批 SHM 失效是最尖銳的 bug 面（M1.4 專門處理）。
- **win 是 ~4× 不是 8×**（每 worker 仍重建 ~70–150MB dense rr）——足以消 thrash，但非零。深化見 Backlog。
- **排期彈性**：M1 大、風險集中；若 user 想先要「立即可用的緩解」，M2（ramp/RAM 護欄）或旋鈕（workers=4）可先做。

---

## 驗證方式

- [ ] M1 全子任務勾完；`pytest tests/ -q` 綠（含 `test_sharedcell` + `test_export_fused` byte-identical）
- [ ] **user 真機**：LTV export 開 HUD → 無 thrash（worker 列不再大片標紅）、電腦不卡、總時間 ~3× 改善、
  ramp 事件顯示「shared → no ramp」、RAM KPI 平穩
- [ ] `SESSION_LOG.md` 逐 milestone 補條目

---

## 完成後

- 最終 SESSION_LOG 註記 `完成 [F29 Mx]`；§8 對應更新
- 本檔保留為 design history
