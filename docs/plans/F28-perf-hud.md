# [F28] 程式內即時效能監控 HUD / Log 視窗（分類彩色、涵蓋全部事件）

> **狀態：** planned
> **§8 ID：** [F28]
> **建立：** 2026-07-03
> **負責 branch：** claude/code-review-handoff-65xwf4（PR #18）

---

## Goal & Context

**問題觀察：** F27 一連串效能除錯全靠 `debug.bat` 的終端輸出（`[roi]` / `[export-timing]` /
`[export] ramping …` …）。訊息雖豐富但：(1) 在 console 快速捲動、難即時看趨勢；(2) export
worker 是**獨立行程**，per-worker 計時只 print 掉、UI 拿不到；(3) 無分類彩色、不易一眼分辨。
user 要求把除錯體驗升級成**程式內可開關的、精美、分類彩色、資訊詳盡的即時監控視窗**，取代盯終端機。

**成功長相：** 主視窗內一個可開關（View 選單）的 dock 面板，即時顯示：
- **頂部總覽列：** ramp 狀態 `R→W`、吞吐 `img/s`、可用 RAM、已完成 `N/total`、目前階段。
- **per-worker / per-op 聚合表：** 每個 worker(pid) 或操作類別的 最近/次數/平均/最大 耗時，即時更新。
- **分類彩色事件 log：** 每筆事件依**類別上色**（ROI/Export/Decode/Boolean/Align/Cache/Warn…），
  可篩選類別、可暫停、可存 `.txt`。異常（decode/emit 尖峰＝thrash、錯誤）標紅。
- **即時 worker 監控：** 哪個 worker 正在跑哪張、已跑幾秒（主程式 `run_ramped` 本就知道，免跨行程 IPC）；
  每張完成時補上 decode/emit/bool/mat 明細。

**與現有系統關係：** **復用並取代** open PR #15（`perfmon.py` + `perf_panel.py`，已建好 Qt-free 事件
收集器 + HUD dock 骨架，但只量主行程操作、沒有 worker 資料）。本案把它接上 F27 的 worker 計時 +
ramp/RAM 狀態，並做分類彩色美化。**console 輸出保留**為 headless / log 檔 fallback（不移除、不再加強）。

---

## Q&A Decisions

### Q1: 顯示在哪？
**選項：** 甲 UI dock HUD ／ 乙 終端即時儀表板 ／ 丙 兩者
**選擇：** 甲（UI dock HUD）。**理由：** user 要「像時時監控那樣」的精美視窗、程式內開關；終端不再是重點
（保留為 fallback，不再加強）。

### Q2: 監控範圍？
**選項：** A 只 export ／ B 也含互動 ／ C 全部
**選擇：** C（全部事件皆可記錄）。**理由：** user 明示「任何事件都可以記錄，但可以分類、不同事件不同顏色」。

### Q3: 即時 worker 監控如何取得資料（避免脆弱的跨行程 IPC）？
**選項：** (a) 主程式 in-flight 追蹤 + 完成時回傳明細（無 IPC）／ (b) worker 經 `multiprocessing.Queue`
即時串流（含 mid-image 心跳）
**選擇：** 先做 (a)（M4），(b) 列為可選 M5。**理由：** 主程式的 `run_ramped` 已掌握「哪張在哪個 worker、
起訖時間」，能即時顯示 worker 忙碌狀態 + 完成明細，**零跨行程風險**、涵蓋 95% 需求；mid-image 心跳
（大 cell 解到第幾筆）才需要 Queue，留待確認值不值得。

### Q4: console 輸出怎麼辦？
**選擇：** 保留現況（`perfmon` 加**可選 console sink**，讓事件同時能印到終端），**不移除**既有 print。
**理由：** headless / cron / 貼 log 分析仍需要；全面改寫每個 print 風險高、收益低。UI 成為主要豐富視圖即可。

### Q5: 開關與 gating？（user 定案）
**選擇：** HUD 為**獨立 top-level 視窗**（非 dock，可移到第二螢幕邊看邊操作）；**預設開啟**（app 啟動即開）；
主視窗**上方 toolbar/header 一個 toggle 鈕**可關/開，視窗自身的 X 也是隱藏（toggle 同步狀態）；狀態存
`QSettings`（記住上次開/關）。**不 dev-mode gate**（一律可用）。
**理由：** user 明示「預設開啟、可在 UI 上方關掉、log HUD 為獨立視窗」。獨立視窗最適合「邊監控邊操作 / 丟到副螢幕」。

### Q6: M5（worker mid-image 心跳）做不做？
**選擇：** 先不做（user「聽建議」）。本輪交付 **M1–M4**；M5 標記 deferred，日後需要再評估。

---

## Milestones

> 粒度：每個 milestone 約一個 session。M1–M4 交付核心價值，M5 可選，M6 收尾。

### M1: 事件匯流排地基（復用 perfmon，Qt-free）  [status: done 2026-07-03]

- [x] 取回並強化 `glas/core/perfmon.py`（`PerfMonitor` 單例 / `record` / `timed` / ring buffer /
  聚合 / `on_event` / `.txt` sink）。
- [x] `PerfEvent` 增 `category`（預設取 op `:` 前綴）與 `level`（info/warn/error）欄。
- [x] 定義類別集合 `CATEGORIES`（顏色對照放 app 端 `perf_panel.CATEGORY_COLORS`）。
- [x] 加可選 console sink（`monitor.echo_console=True` → record 也用 `devlog` 上色印一行）。
- [x] 驗證：新 `tests/test_perfmon.py`（16：聚合/ring/callback/timed/category/level/console/logfile/thread-safe）全綠。

### M2: HUD 視窗 UI（復用 perf_panel + 精美化 + 分類彩色 + 獨立視窗）  [status: done 2026-07-03]

- [x] `glas/app/perf_panel.py` → `PerfWindow`（獨立 top-level 視窗，`Qt.Window`、parent 為主視窗非 modal、
  可自由移動/丟副螢幕），**深色監控台配色**（刻意與奶油色主 app 區隔、彩色 log 更醒目）。
- [x] 版面：頂部**總覽列** KPI tiles（phase/ramp/throughput/ram/progress，`update_overview`/`push_summary` 餵）
  ＋中段**聚合表**（op 名依分類色）＋下段**分類彩色 log**。
- [x] **分類彩色**：`CATEGORY_COLORS`（13 類，深底可辨）；warn/error 標琥珀/紅底 + `⚠` flag。
  **類別篩選 chips**（點選 rebuild log）＋**暫停**（暫停時 log 凍結、resume rebuild）。
- [x] `_Bridge`（event + summary 兩 signal，QueuedConnection）：任何 thread 安全 marshal 回 GUI。
- [x] **預設開啟**：MainWindow 建 `PerfWindow`、View 選單「Performance monitor」toggle（Ctrl+Shift+P）；
  視窗 X 只隱藏（`closed` 同步 toggle）；可見狀態存 `QSettings`；MainWindow closeEvent `shutdown()`（真關+detach）。
- [x] 驗證：新 `tests/test_perf_panel.py`（10：wiring/事件→表列+log/分類色/篩選/暫停/總覽/warn色/clear/close隱藏/shutdown）；
  截圖確認外觀精美；全套 **838 passed**。

### M3: 插樁主行程事件（互動側，涵蓋 Q2「全部」）  [status: done 2026-07-03]

- [x] 在既有計時點呼叫 `perfmon.monitor.record`（全在 app 端主行程 callback，**core 保持與 perf 系統解耦**）：
  **open+index**（reader built）、**scan layers**（`_on_scan`/`_on_scan_finished`）、**ROI walk**（`_on_roi_finished`
  逐 layer decode/geom/rects，慢層標 warn）、**boolean eval**（`_recompute_recipes`）、**align**（單張 `_on_run_fine_align`）。
- [x] 用既有 stats（per_layer / perf_counter）記錄，不新增量測、不動 core。
- [x] 驗證：全套 838 passed（後續 M4 再 +5）；截圖含互動類別事件上色。（poi/template/coordinate-jump 屬次要，暫略。）

### M4: export worker 即時監控（批次側，user 痛點）  [status: done 2026-07-03]

- [x] **per-image 計時由 orchestrator 量測**（`ExportWorker._run_process_pool` 的 `_submit`/`_on_result` 記
  submit→complete wall time）——**免動 `align_and_export_one_image`（保持 byte-identical）**、免 run_ramped 回呼。
- [x] `_afe_pool_task` 僅多回傳 `os.getpid()`（3-tuple），讓 orchestrator 依 **worker pid 分組**（`worker:<pid>` op）；
  `_run_in_thread` 用主 pid 同樣記錄。`test_export_fused` 只需改該一處解包（fa/row 斷言不變 → byte-identity 保住）。
- [x] **thrash 警示**：per-image ≥ 30s → `level=warn`（HUD 標紅）。**總覽 KPI**：`perfmon.set_summary` 推
  phase/ramp `R→W`/throughput img/s/free RAM/progress（每張刷新，RAM 即時）。
- [x] perfmon 加 `on_summary`+`set_summary`（跨 thread 推總覽，與 on_event 對稱）；PerfWindow 掛 `on_summary`。
- [x] 驗證：`test_perfmon.py`（+3 set_summary）、`test_perf_panel.py`（+2 summary→overview）、`test_export_fused`
  護欄過；全套 **843 passed**；M4 export-flow 截圖確認 per-worker 列 + thrash 標紅 + 即時 KPI。

### M5:（可選）worker mid-image 即時心跳（Queue 串流）  [status: deferred — user「聽建議」先不做]

- [ ] 經 pool init 傳入 `multiprocessing.Queue`；worker 把 decode 心跳（cell/records/elapsed）+ 階段事件 put 上去。
- [ ] 主行程背景 drainer → `monitor.record` → HUD 顯示「worker 正在解 cell X，第 N 筆」。
- [ ] 驗證：Queue 生命週期（含 F23 常駐 pool、cancel）不洩漏；手動看大 cell 解碼即時進度。
- [ ] **先確認值不值得做**（(a) 已涵蓋大部分；此為錦上添花）。

### M6: 收尾（測試 / 文件 / 護欄）  [status: in progress]

- [x] 面板關閉時 `monitor` overhead ≈ 0（無訂閱者 → record 早退／set_summary no-op）；thread-safety 走 RLock + `_Bridge` queued。
- [x] `CLAUDE.md` §4 補模組（perfmon/perf_panel）+ §8 更新；README 補「即時效能監控 HUD」段 + 測試數。
- [x] **關閉 PR #15**（本案取代之，已留 superseded 說明 comment）。
- [x] 全套 `pytest tests/ -q` 綠（845 passed）。
- [ ] **user 真機驗收**（開 HUD 跑 export 看即時 worker 列 + thrash 標紅 + KPI；配色/版面回饋）— 待 user。

---

## Affected Files

- `glas/core/perfmon.py`（**新/復用自 PR #15**：event bus，加 category/level/console sink）
- `glas/app/perf_panel.py`（**新/復用自 PR #15**：dock HUD，精美化 + 分類彩色 + 篩選 + 總覽列）
- `glas/app/gds_align_tool.py`（View 選單 toggle、掛 dock、ExportWorker/互動插樁、`run_ramped` 事件）
- `glas/core/fine_align.py`（`run_ramped` 事件回呼；per-image timing 回傳）
- `glas/core/overlay_export.py`（`align_and_export_one_image` 回傳 timing；護欄）
- `glas/core/devlog.py`（category→色 對照可共用；console sink）
- `tests/test_perfmon.py`（復用+擴充）、`tests/test_perf_panel.py`（復用+擴充）、`tests/test_export_ram_cap.py`（run_ramped 事件）
- `docs/plans/F28-perf-hud.md`（本檔）、`CLAUDE.md`、`README.md`、`SESSION_LOG.md`

---

## Risks / Open Questions

- **byte-identical 護欄**：M4 動 `align_and_export_one_image` 回傳；務必保 `test_export_fused` 的 fa/row 逐位相同。
- **執行緒/行程安全**：`monitor.record` 已 RLock；worker 事件走「回傳（M4）」或「Queue（M5）」，皆不直接碰 GUI。
- **效能 overhead**：面板關閉時 record 必須極輕（無訂閱者早退）；插樁不得拖慢熱路徑。
- **pool 編排無單元測試**（專案慣例靠真檔 end-to-end）：M4/M5 的即時顯示最終靠 user 真檔驗收。
- **待 user 後續確認**：M5（mid-image 心跳）值不值得做；面板要不要「非 dev-mode 也可見」。
- **範圍不小**：建議先交付 **M1+M2**（地基+可開的彩色 HUD 空殼），再 M3（互動事件）、M4（worker）逐步長出。

---

## 驗證方式

- [ ] 所有 milestone checkbox 已勾（M5 可選）
- [ ] `pytest tests/test_perfmon.py tests/test_perf_panel.py tests/test_export_ram_cap.py tests/test_export_fused.py -v` 通過
- [ ] 手動：程式內 View→Performance monitor 開面板 → 做一次互動載入 + 一次批次 export → 各類別事件即時、
  分類彩色、worker 槽位即時、thrash 標紅；存 `.txt` 可回貼
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 最終 SESSION_LOG 條目註記 `完成 [F28]`；`CLAUDE.md` §8 若登記則移除
- 處理 open PR #15（取代/close）
- 本檔保留為 design history
