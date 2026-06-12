# Session Log

> 紀錄原則：每 (日期, 任務) 一條；同天同 task 的多次來回已合併。完整逐 commit 細節見 git history。

---

## [2026-06-12] [F24] M11：cellcache 改用「解碼成本」快取（大檔 walk-bound 修法）

**變更類型：** 效能優化（快取策略）· **狀態：M11 完成** · **Branch：** claude/gifted-lovelace-uvnn8v

**動機（大檔實機數據 + 機器資訊）：** 大檔 LTV 1.8GB / 44,997 cells / **sbbox=89994**（decode-free
prune 有效 → M7 對大檔無用）。batch 480 張 = 28.5 分，**ROI-walk 主導（23.7s/img，85% POI）**，
Boolean 輕（4.2s，P1/P4/M9 白賺）。machine = **4 實體核/8 邏輯**：batch 開 8 worker 擠 4 核 →
per-image perf_counter 計時被排程灌水（先前誤判「2.2×」），真實 4 核吃滿、CPU-bound。瓶頸 = 解碼
大 cell：這些 cell 總 record 多（~65ms 解碼）但過濾後只剩 ~40 rect → `min_records=100k` 永不達標 →
**cellcache 完全沒接住**，每次/每 worker 重解。user 回報**常重跑同一個 .oas** → 持久快取極有價值。

**實作：**
- `cellcache.min_decode_s()`（env `GLAS_CELLCACHE_MIN_DECODE_MS`，預設 10ms）+
  `should_cache(kept_records, decode_s)`：kept≥`min_records()` **或** decode≥`min_decode_s()` 即快取。
  → 解碼貴但過濾後小的 cell 現在也會落地，re-run 從磁碟讀（~ms）。
- `oasis_random.load_cell` 在 `_decode_at` 兩側計 `_decode_s`，改用 `should_cache` 決定 save。
- `cellcache._maybe_evict()` 節流（module 計數，每 512 saves 才 `_evict()` glob 一次）—— 上萬 cell
  快取時避免每次 save 都 glob 整個 dir 的 O(n²)。
- 等價/正確性不變：cellcache 仍以 mtime+size 驗證、毀損當 miss、原子寫；快取的是過濾後 content。

**測試：** `tests/test_cellcache.py::TestDecodeCostTrigger`（5：records 觸發 / decode-cost 觸發 /
關閉 / ms 解析 / evict 節流）+ e2e（合成檔 run2 從磁碟讀回、`walk_roi` 幾何 byte 相同）。
`pytest tests/` **747 passed**。

**影響檔案：** `glas/core/cellcache.py`、`glas/core/oasis_random.py`、`tests/test_cellcache.py`、
`docs/plans/F24-perf-roiwalk-finealign.md`、`SESSION_LOG.md`。

---

## [2026-06-11] [F24] M9 驗證 + M10：batch POI walk/bool 分段診斷

**變更類型：** 效能優化驗證 + 診斷 · **狀態：M10 完成** · **Branch：** claude/gifted-lovelace-uvnn8v

**M9 實測成效（第三次 .txt，E3B/399）：** batch **17.9→11.6 分**（原始 21.5 → −46%）、
POI walk+bool **21.2→13.6s/img（−36%）**、img/s 0.4→0.6。POI 仍 ~99% 瓶頸。

**M10（診斷）：** POI 仍合在一起看不出 walk vs bool 殘餘佔比，故再拆。`_fine_align_image` 用
`walk_acc` box 累計 ROI-walk 時間（`poi_polys_for_roi`/`_and_geometry` 的 `_walk` 計時）；
`_record_timing` 加 `walk_s=`、`_FA_TIMING_ACC` 加 `walk`、`pool_collect_timing` 帶 walk、
`[fa-timing]` 改印 `poi=…(walk=…+bool=…)`；`_on_fa_stage_timing` 記 `batch:walk`+`batch:bool`
（=poi−walk）。發現 cellcache 只存大 cell（E3B 多小 cell ~0.9ms→不進磁碟、各 worker 各自解碼），
推測殘餘 POI 由 ROI-walk 冷解碼主導，待 walk/bool 數據確認。

**測試：** 合成 batch 驗證 walk/bool 拆分（`poi=280(walk=162+bool=118)`）；更新
`test_accel_equivalence.py::test_timing_accumulates` 含 walk。`pytest tests/` **742 passed**。

**影響檔案：** `glas/core/fine_align.py`、`glas/core/perfmon.py`、`glas/app/gds_align_tool.py`、
`tests/test_accel_equivalence.py`、`docs/plans/F24-perf-roiwalk-finealign.md`、`SESSION_LOG.md`。

---

## [2026-06-11] [F24] M9：batch 跨 POI-spec 快取共用（第二次實機數據驅動）

**變更類型：** 效能優化 · **狀態：M9 完成** · **Branch：** claude/gifted-lovelace-uvnn8v

**動機（第二次實機 .txt + P5 分段）：** P1/P4/P5 之後 user 重跑 E3B/399 張：morph 最重的
`(K...)` recipe 2922→1881ms（−36%）、batch 21.5→17.9 分（−17%）。**P5 分段揪出真凶**：
batch per-image CPU 中 **POI walk+bool = 21,159ms（~99%）**、match 僅 90ms（確認 M6 金字塔
不需要）。進一步發現：batch 每張有 3 個 POI 表達式但**彼此不共用快取**（P1 的 per-image 快取原本
每個 spec 各建一份），共用 raw layer A 每張被 walk 3 次、`(A>W:7)` 每張算 3 次——live 的
`_recompute_recipes` 有跨 recipe 共用、batch 漏掉。

**實作：** `poi_polys_for_roi` / `poi_polys_and_geometry_for_roi` 加 `walk_memo`/`raw_geom_memo`/
`eval_cache` kwargs；`_fine_align_image` 對「同一張影像的所有 POI spec」（同一 ROI）共用這三個
快取 → 共用 raw layer 每張只 walk 一次、共用子式只算一次。等價（shapely 幾何不可變）。

**測試：** `tests/test_gds_boolean_cache.py::TestFineAlignCrossSpecShare`（3 spec 共用 layer 17 →
walk 3→1、結果等價）。`pytest tests/` **742 passed**。

**影響檔案：** `glas/core/fine_align.py`、`tests/test_gds_boolean_cache.py`、
`docs/plans/F24-perf-roiwalk-finealign.md`、`SESSION_LOG.md`、`CLAUDE.md`。

---

## [2026-06-11] [F24] M3-M5：實機數據驅動的 Boolean 去重 + morph 加速 + batch 分段診斷

**變更類型：** 效能優化（數據驅動）· **狀態：M3/M4/M5 完成** ·
**Branch：** claude/gifted-lovelace-uvnn8v

**動機（實機數據）：** user 用 HUD 在 E3B 小檔（346MB / 13,276 cells / **sbbox=0** / 4 layer /
~399 張）跑真實工作流回傳 .txt。瓶頸明確：① **Boolean eval 0.8~2.9s/次、在 GUI thread**，且每次
ROI 重載 3 個 recipe 各重算、共用子式 `(A>W:7)` 被算 3 次；② 首次 ROI walk **12.4s 解碼整檔**
（sbbox=0）；③ **Batch 21.5 分 / 399 張**（poi(walk+bool) 主導）。user 選擇實作 P1 + P5 + P4。

**實作：**
- **P5 batch per-stage 診斷：** `fine_align.pool_collect_timing()` 回傳並重置 worker 的
  `_FA_TIMING_ACC`；`FineAlignAllWorker` 加 `stage_timing` signal——process-pool 路徑在 lease 內
  over-submit workers×3 個 collect probe 聚合（每 worker 真值計一次、之後回 0，總和精確），
  in-thread 路徑本地收集；`_on_fa_stage_timing` 把 `batch:read/poi/template/match` 記進 monitor。
  端到端驗證：合成 batch 量到 per-image 分段（poi 主導）。
- **P1 Boolean 共用子式去重：** `gds_boolean.evaluate` 加 `node_cache`+`ref_ids`，以 `_canon_key`
  （leaf 用「解析後 (layer,datatype)/recipe 名」而非字母）memo 每個 AST 子樹結果；
  `resolve_expression` 加 `_eval_cache` 串接（含 nested recipe）。app `_recompute_recipes` 跨 recipe
  共用 `raw_memo`+`eval_cache` → `(A>W:7)` 算 1 次；`fine_align` expression POI 每張共用 per-image
  raw_memo + `_eval_cache`。shapely 幾何不可變、set op 不改輸入，故共用快取值等價。
- **P4 morph 加速：** profile 出 `_dilate_axis` = `unary_union`(53%) + per-edge `Polygon()`(~25%)。
  改成單一 `shapely.polygons` 向量化建 parallelogram + **跳過與掃描向量平行的退化邊**（零面積、
  丟棄為精確；rectilinear 約少一半 pieces）。實測 9000-poly grow **4007ms → 2565ms（−36%）、
  symdiff=0**。與 P1（live recompute 少算 ~2/3 morph）相乘。

**測試：** 新增 `tests/test_gds_boolean_cache.py`（16：共用子樹只算一次 / 不同 layer 不混淆 /
含 morph·diagonal·hole 的 cache 與 no-cache 等價 / 向量化 `_dilate_axis` vs per-edge 參考等價）。
既有 `test_gds_boolean.py`(63) + `test_accel_equivalence.py` 全綠。`pytest tests/` **741 passed**。
未動對位/解碼正確性紅線（§7）。

**未做（候選）：** M6 matchTemplate 金字塔（數據顯示 match 僅 0.3ms/img，不需要）、M7 無 sbbox
檔 bbox sidecar（[F17]，user 本輪未選）、M8 grid analytic clip。

**影響檔案：** `glas/core/gds_boolean.py`、`glas/core/fine_align.py`、`glas/core/perfmon.py`、
`glas/app/gds_align_tool.py`、`tests/test_gds_boolean_cache.py`、
`docs/plans/F24-perf-roiwalk-finealign.md`、`SESSION_LOG.md`、`CLAUDE.md`。

---

## [2026-06-11] [F24] M1：app 內建即時效能監測 HUD（perfmon + perf_panel）

**變更類型：** 效能可觀測性（in-app 即時監測）· **狀態：M1 完成** ·
**Branch：** claude/gifted-lovelace-uvnn8v

**動機（轉向）：** 接續同日 M0 離線 harness，user 指出「離線跑合成檔測不準」——實際工作流是
**載入大 + 小兩組檔 → 載 3~4 layer 做 Boolean → 填 POI → Batch align**，希望**在 UI 操作當下
時時監測**這些步驟的效能。Q&A 收斂：UI 內嵌面板（HUD）+ 可選 **.txt** log 匯出（不做 JSON/CSV），
涵蓋每個步驟，本 session 直接實作。

**實作：**
- **新增 `glas/core/perfmon.py`（Qt-free）：** session 單例 `monitor`。`record(op, ms, **meta)`
  累積 ring buffer（最近 400 筆）+ per-op 聚合（次數/總和/平均/最大/最近）；`set_logfile()` 開
  .txt 即時逐行寫（`format_event` 人類可讀、套 `OP_LABELS` 中文化操作名）；`on_event` callback
  餵 UI；`timed()` context manager。RLock 保護，可從 ROI / batch worker thread 安全呼叫。
  batch per-image 分段在獨立 process、不回主行程，故 monitor 記 batch 整批耗時 + img/s，
  per-stage 仍走既有 `[fa-timing]` console。
- **新增 `glas/app/perf_panel.py`（Qt）：** `PerfPanel` QWidget——上半 per-op 聚合表
  （Operation/last/count/avg/max）、下半逐筆事件 log（QPlainTextEdit）、工具列「Log to .txt…」
  /「Clear」。`_Bridge(QObject)` 的 `pyqtSignal(object)` 把 worker-thread 事件 queued marshal
  回 GUI thread 才更新 widget；`detach()` 切斷 callback + 關 log。bound-signal `emit` 快取成穩定
  參照（每次存取回新 wrapper，detach 身分比對才成立）。
- **`glas/app/gds_align_tool.py`：** import perfmon / PerfPanel；主視窗建 `QDockWidget`
  「Performance monitor」掛底部、dev mode 顯示（`_refresh_dev_ui` 連動）、View 選單加可切
  action；closeEvent `detach()`；補 module 級 `import time`。**五處插樁：** `open`（reader build
  = 開檔 + name-table scan，含 file_mb/cells/sbbox/prune）、`roi`（ROI walk 整批 per-layer 總和，
  含 rects/polys/decoded/cached/prune）、`boolean`（`_eval_expression` 每次評估，含 expr/bindings/
  out_polys）、`template`（POI→模板合成）、`batch`（Run all 整批 wall-clock + img/s + ms/img）。

**測試：** `tests/test_perfmon.py`（13：record/聚合/順序/meta/ring buffer/disabled/clear/callback
（含例外不外傳）/timed/.txt log 開關與替換與壞路徑/format）+ `tests/test_perf_panel.py`（5：事件上
聚合表 + log、多 op 聚合、clear、晚建面板 prime 既有狀態、detach 停 callback；Qt offscreen gated）。
headless 主視窗整合手測：dev mode 切換 → dock 顯示/隱藏、事件入表、選單 action 連動皆正確。
`pytest tests/` **725 passed**（707 + 18）。

**影響檔案：** 新增 `glas/core/perfmon.py`、`glas/app/perf_panel.py`、`tests/test_perfmon.py`、
`tests/test_perf_panel.py`；改 `glas/app/gds_align_tool.py`、`docs/plans/F24-perf-roiwalk-finealign.md`、
`SESSION_LOG.md`、`CLAUDE.md`。

---

## [2026-06-11] [F24] M0：ROI walk + 批次 fine-align 離線效能 harness

**變更類型：** 效能量測工具 + 分析企劃 · **狀態：進行中（M0 完成）** ·
**Branch：** claude/gifted-lovelace-uvnn8v

**動機：** user 要求做效能分析與測試、之後提改善方案。Q&A 收斂聚焦 **ROI walk 隨機存取 +
批次 fine-align**；因 repo 無大型 OASIS 樣本檔、瓶頸只在 production 實機顯現，決定
「我寫量測腳本、user 在實機跑、回傳 log」再據以排優先序。產出設定為「分析報告 + 改善 plan」。

**實作（M0）：**
- 新增 `tools/bench/bench_roiwalk_finealign.py`：
  turnkey harness，最少只要一個 OASIS 路徑（root / layer / ROI 全自動推導），印五區段 +
  可直接複製回傳的摘要：[1] reader build（name-table scan + **S_BOUNDING_BOX 覆蓋率**=剪枝
  關鍵訊號）、[2] resolve（root/layer/ROI）、[3] ROI walk cold+warm + 可選 cProfile（dump
  `RoiWalkStats` 全欄位：cells_decoded/cached、instances_materialized vs visited、max_array_k、
  t_place/t_rect/t_poly）、[4] fine-align per-stage（walk / rasterize+blur / matchTemplate
  真實幾何分段）、[5] batch parallel（真實 process pool 端到端吞吐 + 對序列加速比）。`--json`
  另存機器可讀結果。
- 新增 `tools/bench/_make_sample_oasis.py`：產帶 S_CELL_OFFSET 的合成 OASIS（root TOP +
  N leaf），僅供 harness 自我驗證（非代表性樣本）。修正一處 PLACEMENT info-byte（應為 0xF0
  = C+N+X+Y，原誤寫 0xC0 只宣告 refnum 卻多塞 x/y → 解碼器 desync）。
- 新增 `docs/plans/F24-perf-roiwalk-finealign.md`：分析報告 + 改善 plan（靜態分析假說 +
  量測方法 + data-gated milestones M1–M5 候選池：matchTemplate 金字塔 / expr 同層 walk 去重 /
  批次首波熱 cell 預解碼 / regular-grid analytic clip 補強）。

**測試：** harness 在合成 OASIS 上五區段全綠（reader build / walk cold 7.9ms warm 2.1ms /
fine-align 分段 / 4-worker pool 2.2× 加速 / cProfile / --json 皆驗證）。本 session 另為跑
測試而在環境裝齊 numpy/cv2(headless)/shapely/pytest/PyQt6 + Qt 系統庫；`pytest tests/`
707 passed。**未動 production 程式碼**（M2+ 待 user 實機 log 定案）。

**影響檔案：** 新增 `tools/bench/bench_roiwalk_finealign.py`、`tools/bench/_make_sample_oasis.py`、
`docs/plans/F24-perf-roiwalk-finealign.md`；改 `SESSION_LOG.md`、`CLAUDE.md`（§8 註冊 [F24]）。

---

## [2026-06-07] [doc-sync] README / CLAUDE.md 文件整理

**變更類型：** 文件同步 · **狀態：完成**

**動機：** README 與 CLAUDE.md 的測試計數、目錄結構、功能描述停在舊版（F21/F23/devlog
完成前），與現況不符。

**修改內容：**
- **README.md**：測試計數 `~218` → `~707`；Features 補充 F21 PART/CHIP catalog + Wizard +
  Welcome dialog、F23 batch pool 常駐/預熱加速、dev mode 終端機着色 + 分段計時儀表。
- **CLAUDE.md §3**：測試計數 `~218` → `~707`。
- **CLAUDE.md §4 目錄**：補 `fine_align.py`（F8/F14/F23 batch pool）、`parts_catalog.py`
  （F21 catalog model）、`devlog.py`（終端機着色）；`gds_layer_cache.py` 備註 v4/v5；
  tests 計數更新。
- **CLAUDE.md §5.2**：§並行模型補 F23 `_BatchPool` 說明。

**影響檔案：** `README.md`、`CLAUDE.md`、`SESSION_LOG.md`。
**Branch：** claude/loving-brown-XwlrJ

---

## [2026-06-07] [devlog] dev-mode 終端機輸出上色分類 + 主控台編碼防呆

**變更類型：** DX / 可讀性 · **狀態：完成**（同 branch claude/f23-batch-align-startup-accel）

**動機現象：** dev mode 會把多類診斷訊息噴到終端機（`[roi]` reader/load、`[fa-timing]` 批次計時、
`[jump]` 座標換算、`[gds-align]` 模式 banner），user 反映「一大片資訊」難讀、希望上色分類。

**實作：**
- 新增 `glas/core/devlog.py`（Qt-free、純 stdlib）：依類別給 `[tag]` 上色（roi=cyan、fa-timing=
  magenta、jump=yellow、gds-align=green），`paint()` / `dim()` 輔助。**安全偵測**是否支援色彩：
  `NO_COLOR`/`GLAS_NO_COLOR` → 純文字（opt-out）；`PYCHARM_HOSTED` → 上色（PyCharm console 吃 ANSI
  但非 TTY）；真 TTY → 上色並一次性開 Windows VT（ENABLE_VIRTUAL_TERMINAL_PROCESSING）；其餘（導檔/
  dumb）→ 純文字，確保不把 `\033[` 漏進 log。spawn worker 可如一般 core 模組 import。
- 把 `oasis_random._dbg/_trace`、`fine_align._record_timing`、app 的 `[roi]×4 / [jump] / [gds-align]×2`
  前綴改用 `devlog.tag(...)`。`[fa-timing]` 另把 poi/match 中較大者 **bold** 標出，瓶頸一眼可見；pid/n 用 dim。
- **主控台編碼防呆：** 既有診斷訊息含 `·`、`µm`、`──` 等非 ASCII，在 cp950（繁中 Windows）裸終端機會
  `UnicodeEncodeError` 中斷 print。`main()` 與 worker `_pool_init` 把 stdout/stderr `reconfigure(encoding=
  "utf-8", errors="replace")`，PyCharm（本就 UTF-8）為 no-op。

**測試：** `test_devlog.py`（9 項：TTY 上色 / 非 TTY 純文字 / NO_COLOR / GLAS_NO_COLOR / PYCHARM_HOSTED /
未知類別 / paint / dim）。`pytest tests/` 707 passed（含先前修好的 cellcache，0 fail）。

**影響檔案：** 新增 `glas/core/devlog.py` · `tests/test_devlog.py`；改 `glas/core/oasis_random.py`、
`glas/core/fine_align.py`、`glas/app/gds_align_tool.py`。

---

## [2026-06-07] [batch-perf] Batch run 途中加速：raw POI 跳過被丟棄的 unary_union + 分段計時儀表

**變更類型：** 效能優化 + 診斷工具 · **狀態：完成**（接續 F23、同 branch
claude/f23-batch-align-startup-accel）

**動機現象：** F23 解決「批次啟動延遲」後，user 要求探查「batch run **途中**」的加速點。實測各
per-image 階段（worker 內單執行緒）：matchTemplate 3ms(512²)/14ms(1024²)/62ms(2048²)、rasterize+blur
可忽略、**matchTemplate 多執行緒無加速**（故 cv2 pin 單執行緒零損失）；shapely `unary_union` 隨形狀數
陡升（200→5ms、1k→34ms、5k→258ms、20k→1095ms）。

**頭號發現 + 修復：** `poi_polys_for_roi`（batch fine-align 唯一入口、只用 polys 做模板）對 **raw POI**
仍呼叫 `poi_polys_and_geometry_for_roi`，後者算了一個 `unary_union` 卻被 `[0]` 丟掉——密集 FOV 每張白燒
30ms~1s。修：raw POI 直接回傳 `_walk_roi_polys` 結果、不算 geometry（模板 rasterize 對重疊 polys 冪等、
不需 union）；expression POI 仍走全路徑（boolean 需 geometry）。

**分段計時儀表（診斷）：** `_fine_align_image` 加 4 段 perf_counter（read / poi(walk+bool) / template /
match）。**綁定既有 dev mode**：`_apply_fa_timing()`（`__init__` 依持久化 dev_mode 套用、`_set_dev_mode`
切換時套用並 `batch_pool.shutdown()` 讓 worker 重生繼承新 env）設 `fine_align._FA_TIMING`（in-thread 路徑）
+ `GLAS_FA_TIMING` env（spawn worker 繼承）——**免手設環境變數，開 dev mode 就有**，輸出印到 console。
每個 worker 第 1 張即印一行（小批<worker 數也看得到，n==1 含冷 walk）、之後每 `GLAS_FA_TIMING_EVERY`
（預設 25）張印一次平均。off 時僅 4 個 perf_counter（奈秒級）、production 路徑不受影響。供在**真實檔案**上量
walk+shapely vs match 佔比、據以決定後續 #2(expr 同層去重)/#3(matchTemplate 金字塔)。

**順手修 [Bxx] cellcache 測試 Windows bug：** `test_save_load_and_invalidate` 在 `RandomAccessReader`
開著 mmap 時 `src.write_bytes(...)` 重寫來源檔，Windows mmap 鎖檔 → `OSError 22`（POSIX 不鎖故只在 Windows
壞、user 實機 + clean tree 皆 fail）。修：重寫前先 `rar.close()` 釋放 mmap。`pytest tests/` 由 697+1fail
→ **698 全綠**。

**其餘脈絡：** ROI walk 已有 per-worker memo + `cellcache` 磁碟 sidecar（大 cell 跨 worker/session 重用）+
S_BOUNDING_BOX 免解碼剪枝；殘留僅「批次第一波 K worker 同時撞同一大 cell」、跨 process 不易治、列為低優先。

**測試：** `TestRawPoiSkipsUnion`（raw 路徑不呼叫 `polys_to_geometry`、polys 與全路徑 `[0]` 完全相等）+
timing 累加測試；env-gated 儀表實機驗證會印。`pytest tests/` 697 passed（唯一 fail 為既有 Windows 暫存
路徑問題、與本案無關）。

**影響檔案：** `glas/core/fine_align.py`、`glas/app/gds_align_tool.py`、
`tests/test_accel_equivalence.py`、`tests/test_cellcache.py`。

---

## [2026-06-07] [F23] Batch Align 啟動延遲加速：注入索引 + 常駐/預熱 process pool

**變更類型：** 效能優化（並行模型）· **狀態：完成 [F23]**（M1+M2）·
**Branch：** claude/f23-batch-align-startup-accel

**動機現象：** user 回報每次按 **Batch Align → Run all** 之前都有一段明顯啟動延遲、UI 像卡住。
追碼 + 實測（spawn pool k=4→0.28s、k=8→0.38s、k=20→0.87s）確認延遲來自
`FineAlignAllWorker._run_process_pool` 每次都**重新** spawn 一個 process pool、用完即 `shutdown`，
成本兩塊：(1) K 個直譯器冷啟 + 重 import numpy/cv2/shapely/oasis；(2) 每個 worker 各自重跑一次
`scan_cell_offsets` 重掃 name table——而主行程 `self._rar` 早已建好這份索引（純 dict 可 pickle）。

**修復實作：**
- **M1 注入既有索引：** `RandomAccessReader.__init__` 加 `prebuilt_index=` kwarg（有值即跳過
  `scan_cell_offsets`，mmap/OasisReader 照建以供幾何解碼）；加 `index_snapshot()`；`clone()` 轉發索引。
  `_pool_init` 加 `prebuilt_index`，`_run_process_pool` 的 initargs 帶 `rar.index_snapshot()`。
  消掉 K× 重掃（隨 cell 數放大、第一次跑就受益）。
- **M2 常駐/預熱 pool：** 新增 `fine_align._BatchPool`（session 單例 `batch_pool`，RLock 保護），
  key=`(path, wanted, dtype, bbox, workers)`；`get()` 同 key 重用、異 key 關舊建新；`ensure_warm()`
  背景預熱、對同 key 冪等。`_pool_init` 只建 reader、`root/poi_specs/cfg` 改 per-task 隨
  `_pool_task` 傳（本就已 pickle，故 POI/半徑變更仍重用暖 pool）。`_run_process_pool` 改用
  `batch_pool.get()` 且不再 per-batch shutdown。GUI：`_maybe_prewarm_batch_pool()` 於
  `_on_pois_changed` / KLARF / folder 載入時背景預熱（門檻 SEM>2、`_prewarming` 防重入）；
  `closeEvent` + 開新 OASIS 時 `shutdown()`。
- **M2 idle-timeout（記憶體控管）：** 暖 pool 整 session 佔 K 份 mmap+索引，故加 idle auto-release
  —— `_BatchPool(idle_timeout=)` 預設 300s，閒置（無批次在跑）逾時自動釋放 worker。安全：批次以
  `lease()`（busy refcount）持有，timer 只在 `_inuse==0` 武裝、acquire 取消、fire 時再驗 `_inuse==0`，
  **絕不在批次中途殺 worker**。

**測試：** `TestPrebuiltIndex`（注入 vs 重掃等價：by_refnum/by_name/unit/layernames/sbbox/
offset_flag 全等、ROI 幾何一致、clone 共用索引）；`TestBatchPoolManager`（fake executor：重用/重建/
shutdown/warm 冪等）；更新 `test_accel_equivalence` 的 `_pool_init/_pool_task` 新簽名；real-spawn smoke
驗證暖 pool 結果 == 順序；idle-timeout 以 fake-executor 確定性測試（閒置釋放 / 批次中不釋放 / lease 重新武裝 /
短 timer 端到端）+ real-spawn smoke（lease==順序、重用、自動釋放）。`pytest tests/` 695 passed
（唯一 fail 為既有 Windows 暫存路徑問題、與本案無關）。

**影響檔案：** `glas/core/oasis_random.py`、`glas/core/fine_align.py`、`glas/app/gds_align_tool.py`、
`tests/test_oasis_random.py`、`tests/test_accel_equivalence.py`、`docs/plans/F23-batch-align-startup-accel.md`。

---

## [2026-06-05] [PR #11 UX polish] 實機回饋驅動的 30+ 條細節整理（U/S/T 三輪 + Minimap 移除 + Codex review fix）

**變更類型：** UX 細節 batch（多輪 user 實機回饋累積、本日連續推進，已合併）·
**狀態：完成 · PR #11 累計 16 commits**

**背景：** F21（PART/CHIP catalog）+ F20（Wizard）+ F22（Welcome）三大改造完成後，user
實機跑 + 截圖回饋出一系列細節問題。30+ 條全部處理掉。下列依**主題**分組（非時序）。

### Round 1 — U1-U12（右欄 / toolbar / 視覺整理）

- **U1** Wizard 開啟時暫時隱藏 guidance 條，避免「toolbar Open OASIS…」黃條跟 wizard
  重複（`_on_open_roi` try/finally）。
- **U2** `+ Expression…` 按鈕在 LayerPanel 沒 doc 時 `setEnabled(False)`。
- **U3** Toolbar 移除 Load Cache / Export Cache，改進 File menu。
- **U4** nm/px spinbox 在 auto 勾起時 `setSpecialValueText("auto (FOV ÷ image px)")`
  ，視覺從「0.0000 nm/px」變「auto …」。
- **U5** Chip notes 不再單獨一行（撞到 FOV `(custom)` italic）；改塞進 chip-corner
  badge tooltip。
- **U6** SemPanel 頂端「SEM」panelTitle 拿掉（重複 Load SEM… 按鈕）。
- **U7** PART/CHIP block 與 AlignmentDeltaPanel 之間加 1 px 分隔線。
- **U8** LayerPanel 底 hint 拿掉；改 18×18 `?` QToolButton + hover tooltip。
- **U9** 載完 OASIS 後 `setWindowTitle("GLAS — <file>")`；cache 路徑同步。
- **U10** Status bar 啟動文案 `ready · OASIS streamer (built-in)` →
  `ready — open an OASIS to begin`。
- **U11** Fine Align POI placeholder 改成明確指引「click the POI button on a layer
  in the LAYERS column」。
- **U12** Goto µm placeholder `x, y` → `e.g. 12345, 6789`。

**順帶兩個前置 bug fix：**
- `33f7726` 右欄寬度 300 → 320 px：fix scrollbar 把 spinbox/combo ↑↓ ▼ 按鈕裁掉
- `17531db` Copy δ 按鈕改為 Lucide icon + accent border + 「Copied ✓」transient label
  flip（取代之前看不懂的 ⧉ unicode 神祕方塊）

### Round 2 — S2/S7/S11/S12（工作流順手度 + dev mode 隱性）

- **S2** Minimap 從互斥 view mode 拆成獨立 overlay toggle：`_VIEW_MODES` 縮為
  `("sem", "gds")`，toolbar 新增 `_mini_btn` 不進 view group，shortcut `M` 切 minimap、
  `G` 仍 cycle SEM↔GDS。
- **S7** AlignmentDeltaPanel 初始 `setEnabled(False)`；`set_images` 收到 images
  才啟用，沒 SEM 時整個 panel 灰化。
- **S11** PartChipPanel 空 catalog 改成警告卡（橘框 / 米底 / 棕字），文案
  **不洩漏 dev mode 入口**。
- **S12** 新增 `_SemViewerCTA` + `SemViewer.load_sem_requested` signal；viewer 空狀態
  下中央顯示橘色「Load SEM…」CTA；點下開右欄 Load SEM 同一 split menu。
  - 同步 fix `a1c040f`：menu 改錨在 CTA 按鈕底下（之前錨在右欄按鈕，下拉跑去右上）。

**Dev mode 文案清理：** WelcomeDialog slide 3 + PartChipPanel 空狀態都不再提
「Help → About, click icon 5×」—— user 強調 dev mode 必須保持隱性彩蛋。

### Round 2.5 — Minimap 整批移除（dead feature）

S2 拆出獨立 toggle 後，user 表示 Minimap 在實機**根本沒用**（GDS view mode 已能顯示
所有 defect 位置）。整批清除：
- `MiniMap` class（~93 行）、toolbar `_mini_btn`、`M` shortcut、
  `MainWindow.minimap` / `_set_minimap_visible`、batch workspace 保留邏輯、
  `_refresh_overview_defects` 內 `minimap.set_data(...)` 呼叫、
  `SemViewer._corner_overlay` / `set_corner_overlay` / `_reposition_overlay`
  整套（minimap 是唯一 consumer）。淨減 153 行。
- 新增 `test_minimap_gone` defensive guard 防誤回退。

### Round 3 — T1-T8（toolbar gating + 狀態列 + Wizard polish）

- **T1 按鈕 gating：** 新增 `MainWindow._refresh_action_states()`，從 `_update_guidance`
  尾巴呼叫。Toolbar `VIEW MODE GDS` / `Fit` / `Goto µm + Goto` / `Export Alignment` /
  `Export OASIS` + 右欄 `Load GDS ROI here` 按條件 enable/disable。
- **T2 狀態列 transient revert：** 新增 `_status_state(msg)` / `_status_transient(msg,
  ms=4500)` helper。state 訊息存 `_status_state_msg`、transient 訊息 4.5 秒後自動 revert。
  - **State**（永久）：KLARF / Folder / ROI / cache 載入
  - **Transient**（自動清）：origin δ set/cleared/nudged、cache / OASIS / alignment
    exported、catalog saved、deleted expression
- **T3 LAYERS hint：** 空狀態加第二行「or  File → Load cache…」（U3 移走 Load Cache 後
  的補救）。
- **T4 GLAS wordmark 搬家：** Toolbar 左側 wordmark 移除（與 window title 重複），改在
  status bar 左下角顯示「GLAS v1.0.0」橘色橫排。新增 `GLAS_VERSION = "1.0.0"` 常數，
  About dialog 共用。
- **T5 Wizard 記目錄：** `_FilePickPage._on_browse` 用 `QSettings("GLAS","GLAS")` key
  `"wizard/last_oas_dir"` 存上次目錄。
- **T6 Wizard subtitle 對比：** 新增 helper `_wizard_subtitle(html)` 把 subtitle 包進
  `<span style="color:{_TK_TEXT_PRI}; font-size:12px;">`，3 頁都套；Qt 預設灰得幾乎看
  不見的問題解決。
- **T7 Load GDS ROI 文案：** 已載過 ROI（`_doc.entries` 非空 + `_current_sem`）後按鈕
  文字改成「Reload GDS ROI ▶」；未載入維持「Load GDS ROI here ▶」。
- **T8 Wizard Next/Finish accent：** `OpenOasisWizard.__init__` 末尾抓 `button(NextButton)`
  / `button(FinishButton)` 套 accent QSS（橘底白字 / hover 深橘 / disabled 淡米 +
  灰字），視覺權重 Next >> Cancel。

### PR #11 Codex review fix（review 留言）

`PartChipPanel.set_from_meta` v5 catalog-match 分支：reselect 完 chip 後比對 cached
vs `chip.fov_w_nm/fov_h_nm/nm_per_px`；若 FOV 差 >0.5 nm 或 nm_per_px 差 >1e-6 →
自動勾 Custom override + 填入 cached values。修正「Custom override 開啟下匯出的 cache
重載後 silently 回到 catalog 預設、對位偏掉」的 bug。

**Wizard 視覺 fix（`264b529`）：** QWizard 預設 `ModernStyle` 有空白 banner 區，改成
`ClassicStyle` 去掉「stray white block」。

**測試演進：** 606 baseline → 643（F21）→ 665（F20+F22）→ 667（U/S/Minimap）→ 668
（S2 minimap independence）→ 672（T1-T3）→ 677（T5-T8）。期間根據 UI 改動同步重寫
~10 個既有測試（廢棄 Coord Setup / Minimap / FOV badge 等）。

**影響檔案：** `glas/app/gds_align_tool.py`（主戰場，~1500 行淨變動）、
`glas/app/icons/copy.svg`(新)、`glas/app/icons/glas_wordmark.svg`(廢棄 — 仍保留)、
`tests/test_gds_align_f20.py` / `f21.py` / `f22.py` / `m4a.py` / `m5.py` / `m6.py` /
`m6_6.py` / `m7.py`（廣泛更新）、`conftest.py`（welcome QSettings pre-set 防 deadlock）、
`README.md`、`CLAUDE.md`、`SESSION_LOG.md`。 **Branch：** claude/youthful-gates-WLNYJ ·
**PR：** #11 · **Commits：** `33f7726` `17531db` `a139107` `37e6b0d` `a1c040f`
`e7ff174` `45e450a` `9ca60f4` `ca63b4c` 等 16 commits（含 F21 fix + Codex review fix）

---

## [2026-06-05] [F20 + F22] Open OASIS Wizard 化 + First-run welcome dialog

**變更類型：** UX 補完（取代三層 modal cascade + 首次啟動 onboarding） ·  **狀態：完成**

**動機：** F21 把右欄改造完後，剩下兩個 onboarding 缺口：(1) Open OASIS 仍是三層
modal 串接（LayerFilterDialog → LayerPickDialog → QInputDialog root cell，新手卡在
「root cell」黑話）；(2) 第一次打開 app 完全沒人說明這 app 在做什麼。

**F20 OpenOasisWizard（取代 3 dialog）：** 新增 `OpenOasisWizard(QWizard)` 三頁——
`_FilePickPage`（檔案選擇 + 顯示檔名 / 大小 / S_CELL_OFFSET 索引狀態 inline check 用
`oasis_streamer.scan_cell_offsets`）+ `_LayerPickPage`（Scan layers 按鈕 → 內嵌
QListWidget 多選 + 手動輸入 fallback）+ `_RootCellPage`（QComboBox + 推薦預選含
`top`/`merge` 的名稱 + 解釋一行）。`isComplete()` 控制 Next 按鈕。MainWindow
`_on_open_roi` 大瘦身：跑 wizard → accept 後取 file_path / layer_keys / root_cell。
**刪掉 `LayerFilterDialog` + `LayerPickDialog`（共 ~277 行）**。

**F22 WelcomeDialog（首次啟動 5 slide）：** 新增 `WelcomeDialog(QDialog)` + 模組常數
`_WELCOME_SLIDES`：QStackedWidget 切換 + Prev / Next / `Got it ✓` + ● ○ 進度點 +
`[ ] Don't show again`（預設 on）。Help menu 加 `Show welcome…` action。MainWindow
`showEvent` 首次觸發時 `QSettings("welcome_shown_v1")` 未設 → `QTimer.singleShot(0,
self._show_welcome_dialog)` 在主視窗繪製完後彈出。

**測試環境 hook（重要）：** `MainWindow.showEvent` 自動跳 welcome 用 `dlg.exec()`，在
test 環境 `processEvents()` 一旦呼叫就死鎖。conftest.py 啟動時預先 `QSettings("GLAS",
"GLAS").setValue("welcome_shown_v1", True)` 讓 test 表現得像「曾經看過」的 user；
welcome 本身在 `test_gds_align_f22.py` 直接 instantiate 驗證。

**測試：** 643 → **665 passed**（+22）。新增 `tests/test_gds_align_f20.py` 11 項 +
`tests/test_gds_align_f22.py` 11 項。

**影響檔案：** `docs/plans/F20-open-oasis-wizard.md`(新)、`docs/plans/F22-first-run-
welcome.md`(新)、`glas/app/gds_align_tool.py`、`conftest.py`、
`tests/test_gds_align_f20.py`(新)、`tests/test_gds_align_f22.py`(新)、`README.md`、
`CLAUDE.md` §8（移除 F20 / F22）、`SESSION_LOG.md`。

---

## [2026-06-05] [F21] PART/CHIP catalog 取代 Coordinate Setup + Origin δ UI 升級

**變更類型：** UX 重大改造（取代 right-column setup form + δ 升級為常駐區塊 + 移除 fine
tune dx/dy + dev-mode catalog editor + cache schema v4→v5） ·  **狀態：完成（M1–M6
全部完工）**

**動機：** 評估「真小白 + 無手冊」走完 GLAS 全流程的通關率 ≈ 0.4%，最大殺手是
Coordinate Setup（Step 3 通過率僅 ~10%）—— RFL 術語、6 欄手填、無自動帶入、預設折疊。
user 提出：fab 工程師熟悉「PART 碼 + CHIP」心智模型（同 PART/CHIP 座標永遠相同），
應該預先 key 進 catalog → user 只下拉選擇。同步發現 Origin δ vs Fine tune dx/dy
是同件事兩個表現，要簡化。

**Q&A 收斂（共 7 題）：** catalog 存 `glas/data/parts.json` 隨 repo 出貨（Q1）；
OASIS 與 PART/CHIP 解耦、user 自挑 .oas（Q2）；cache 兩者都存、快照為主+id 追溯（Q3）；
未知 CHIP 完全擋住（Q4）；FOV catalog 預設 + UI Custom override，預設 1500 nm（Q5）；
**Fine tune dx/dy 完全移除**——UI + state + 加總邏輯全清（Q6）；Origin δ 升級為
**永久可見的常駐區塊**、大字級 X/Y + Set/Clear/copy（Q7）。

**6 milestones：**
- **M1** catalog data model：新增 `glas/core/parts_catalog.py`（無 Qt；`ChipSpec` /
  `PartSpec` dataclass + `to_dict/from_dict` round-trip、`chip_corner_nm()` 沿用
  `(DieX − GDS_off) × 1000`、`load/save_catalog` atomic、`DEFAULT_FOV_NM = 1500.0`、
  `CATALOG_SCHEMA = "glas-parts-v1"`、`CatalogError`）+ 種子 `glas/data/parts.json`
  （EXAMPLE_PART / C1+C2 placeholder）+ 26 項單元測試全綠。
- **M2** 右欄重構為 PartChipPanel：移除舊 `CoordinateSetupPanel`（~220 行）；新
  `PartChipPanel` PART/CHIP `QComboBox` 下拉、即時 chip-corner / FOV badge、
  `Custom override` checkbox 展開 FOV/scale spinbox、`values()` 新 dict 加
  `part_id` / `chip_id` 移除 `fine_dx` / `fine_dy`。MainWindow 同步移除
  `_fine_dx`/`_fine_dy` state + 8 處 `+ self._fine_dx +` 加總。
- **M3** AlignmentDeltaPanel 常駐：新獨立 `QFrame`，大字級 monospace X / Y、
  `Set Offset` / `Clear` / 後改 `Copy δ to clipboard` 按鈕，emit
  `set_requested` / `clear_requested`。所有 `coord_setup.set_origin(...)` call site
  改走 `alignment_delta.set_values(...)`。
- **M4** dev-mode CatalogEditorDialog：左 PART/CHIP 樹（Add/Remove，命名重複擋下）+
  右 form + atomic Save 寫回 `glas/data/parts.json`。PartChipPanel 加 `⚙ Edit catalog…`
  按鈕（`set_dev_mode(on)` 控制顯示）。
- **M5** cache schema v4 → v5：`LayerCacheMeta` 加 `part_id` / `chip_id`
  (`Optional[str] = None`)、`SCHEMA_VERSION = 5`、`_LOADABLE_SCHEMAS = {4, 5}` 容忍 v4。
  v5+catalog 命中 → 直接 reselect 下拉；v4 / 不在 catalog → 進「legacy snapshot」模式。
- **M6** 文件收尾：README / CLAUDE §5.2 / §7 新增不變式 / §8 移除 [F21]。

**測試：** ~606 baseline → **643 passed**（+37）。新增 `tests/test_gds_align_f21.py`
23 項 + `tests/test_parts_catalog.py` 26 項。重寫廢棄測試：`test_gds_align_m4a` 改驗
AlignmentDeltaPanel、`test_gds_align_m5` coarse_gds 改用 `_origin_dx/dy`、
`test_gds_align_m6_6` 改用 catalog seed、`test_gds_align_m7` 刪除 minimap 相關 +
TestBatch1CoordBadge（功能已移除）、`test_gds_layer_cache` v4 仍能載入 + future schema
rejected。

**影響檔案：** `docs/plans/F21-part-chip-catalog.md`(新)、`glas/core/parts_catalog.py`(新)、
`glas/core/gds_layer_cache.py`、`glas/data/parts.json`(新)、`glas/app/gds_align_tool.py`、
`tests/test_parts_catalog.py`(新)、`tests/test_gds_align_f21.py`(新)、
`tests/test_gds_align_m4a.py` / `m5.py` / `m6_6.py` / `m7.py`、
`tests/test_gds_layer_cache.py`、`README.md`、`CLAUDE.md`、`SESSION_LOG.md`。
**Branch：** claude/youthful-gates-WLNYJ

---

## [2026-06-04] [F16 + F16-B + F18/F19] Load GDS ROI 大檔加速：S_BOUNDING_BOX 剪枝 → 解碼快取 → 換 ROI 秒級

**變更類型：** 重大效能（多 milestone，本日一連串來回，已合併）·  **狀態：完成**

**問題：** LTV（1.75GB OASIS）載入單一 defect 的 ROI 要 ~7 分鐘。逐層定位瓶頸並修復（完整逐步診斷見 git history 與
`docs/plans/F16-sbbox-roi-prune.md` / `F16-B-cell-decode-cache.md`）。

**根因與修復（依發現順序）：**
1. **無 per-cell bbox → ROI 首載需全 chip 解碼** → [F16] 用 name-table **S_BOUNDING_BOX**（per-cell 完整、含 placement 的 bbox）
   做 `reachable_bbox`，免解幾何剪枝（`oasis_random.sbbox_for` + reader 建 sbbox map）。
2. **巨大 repetition 阵列被全展開**（placement 1600 萬 instance / 幾何 CMG 阵列）→ **解析子網格裁剪** `_clip_grid_offsets`
   （type 1/2/3/8 規則格點：把橫跨全 chip、ROI 落在內部的阵列只展開 ROI 附近幾顆；下游精確 mask，結果與全展開逐筆相等）。
3. **per-record 逐筆 numpy 開銷**（百萬筆 × `apply_to_rects` 單盒）→ walk 剪枝**向量化**（placement gather 批次 einsum + 單次
   `apply_to_rects`/mask；rect/poly 同理）。
4. **真正大頭：單一橫跨全 chip 的 flat merge cell `44995`（880 萬 rect + 150 萬 placement + 48 萬 poly ≈ 1080 萬筆）首解 ~292s**
   → **[F16-B] 解碼磁碟快取**（per-user sidecar）：
   - **M1** `Placement`→NamedTuple + decode 內圈精簡；**M2** `CellContent` 欄狀雙後端（`_rcol`/`_pcol`）；
     **M3/M4** 新模組 `cellcache.py`（欄狀 `.npz` + 稀疏 rr、mtime/size+schema 驗證、原子寫、毀損當 miss、env 開關/門檻）接進 `load_cell`。
   - **M6** placement gather per-cell 快取（ROI/T 無關）→ **換 ROI 秒級**；**M7** gather 持久化成 prep sidecar + `_feat` DEBUG gate
     → batch 每 worker / 每 session 第一個 ROI 跳過 gather。
   - **[F18]** lazy placement（cache 存 SoA、int target/kind、用到 survivor 才建）→ cache 載入 placement 段 ~10s→~0.3s；
     **[F19]** sidecar LRU 自動清理（`_evict`/`clear()`/`GLAS_CELLCACHE_MAX_MB`）。
   - geometry extent 向量化（`_ext_from_columnar`，規則型 numpy 一次算、10/11 逐筆）；`_ext_cache` 跨 ROI 重用。
5. **診斷/UX：** 永遠顯示遙測 → `--debug` 分層（L1 精簡每層摘要 / `--trace`=L2 深度 per-cell）；ROI 進度畫面精確化（逐層 +
   已載/快取命中數）；有 sbbox 時跳過 --debug 的 CE 檢查（省首載 ~53s）；濾掉無害 Qt 警告（setPointSize / setGeometry）。

**實測（5–7min → ）：** 換 ROI/defect **~9–12s**（原 ~2min）；重開 app 第一個 ROI **~42s**（decode 283→14s 走快取、
place 14→0.8s 走 prep、geom 29→13s 向量化）；同檔換不同 ROI 全程秒級；batch 每 worker 暖機亦走快取。

**撤案：** M5（ROI 過濾解碼）— 對「看多 defect」工作流更差 + 與 cell 快取相斥。**未做（CP 值不足）：** 結構化 repetition 參數
（可再砍 geom build ~9s→~1s）。

**測試：** ~218 → **606 項全綠**（新增 cellcache round-trip / walk decode-vs-cache bit-identical / 子網格裁剪 D4 superset /
ext 向量化對照 repetition_extent / lazy placement / LRU eviction 等）。

**影響檔案：** `glas/core/oasis_random.py`、`glas/core/cellcache.py`(新)、`glas/app/gds_align_tool.py`、
`tests/test_oasis_random.py`、`tests/test_cellcache.py`(新)、`docs/plans/F16-sbbox-roi-prune.md`、
`docs/plans/F16-B-cell-decode-cache.md`、`CLAUDE.md`、`SESSION_LOG.md`。**Branch：** claude/friendly-franklin-9uZqU。

---

## [2026-06-04] [F12 tune] 放寬 layer 抽樣預算（漏 layer 修正）+ GLAS_SCAN_* env 覆寫

**變更類型：** 調參（core 預設值）+ env 覆寫 + 終端機顯示 + 測試 ·  **狀態：完成，待 user 實機確認覆蓋率**

**現象：** user 回報 strict-mode 修好後，scan 出來的 layer「少一點點」——bounded 抽樣的預期取捨（早停 +
每顆 cell 記錄上限 + max_cells 上限漏掉只出現在少數/深層 cell 的 layer）。

**修法（`oasis_random.enumerate_layers`）：** 預設預算大幅放寬（仍有時間上限保證會結束）：`_SCAN_DEFAULTS`
= max_cells 64→512、max_records_per_cell 2000→8000、stop_after_no_new 16→128、time_budget 15→30s。
參數改 `None` sentinel + `_scan_param()` 解析優先序 **explicit arg > `GLAS_SCAN_<NAME>` env > 預設**，
讓覆蓋率可無痛再調（如 `GLAS_SCAN_MAX_CELLS` / `GLAS_SCAN_STOP_AFTER_NO_NEW` / `GLAS_SCAN_TIME_BUDGET_S`
/ `GLAS_SCAN_MAX_RECORDS_PER_CELL`）。cache params 指紋含解析後的 bounds → 改預算自動失效重掃。diag 加
`sample_bounds`；app 終端機在 sampled 時印出 bounds + 「不完整可用 GLAS_SCAN_* 放寬」提示。

**測試：** 新增 `TestScanParams`（預設夠大、explicit>env>default 解析、env 放寬後 enumerate 由 ≤3 → 全 30
layer）。`pytest tests/` **580 passed**。

**影響檔案：** `glas/core/oasis_random.py`、`glas/app/gds_align_tool.py`、`tests/test_oasis_layer_scan.py`、
`README.md`、`docs/plans/F12-no-layername-scan.md`、`SESSION_LOG.md`。 **Branch：** `claude/friendly-franklin-9uZqU`

---

## [2026-06-04] [F12 bugfix] strict-mode（表在檔尾）OASIS 的 S_CELL_OFFSET/LAYERNAME 找不到 + scan 診斷強化

**變更類型：** bug fix（core `scan_cell_offsets` 補 strict-mode table-offset 跟隨）+ 終端機/Diagnose 診斷強化
+ 測試 ·  **狀態：實作完成、全套件 577 passed，待 user 實機驗收**

**現象（user 回報）：** user 用 KLayout 特別「另存」成有 S_CELL_OFFSET 的 `.oas`，但 GLAS「Scan layers」
仍顯示無索引（no-index）。

**根因：** `scan_cell_offsets` 只做 inline 掃描、**遇到第一個 CELL record 就 break**。但 **strict-mode
writer（KLayout）把 name table（CELLNAME+S_CELL_OFFSET / PROPNAME / LAYERNAME）寫在所有 cell 之後**，
其 byte offset 記在 START（offset_flag=0）或固定 256B 的 END（offset_flag=1）。inline 掃描在第一個 CELL
就停 → 永遠掃不到檔尾的表 → 回 `by_refnum={}` / `layernames=[]` → enumerate_layers 判 no-index。已用合成
strict 檔重現（scan 前回空、修後正確）。

**修法（`glas/core/oasis_streamer.py`）：** 把 per-record 處理抽成 `_consume` 閉包供 inline 與
table-follow 共用；inline 掃描時補抓 START 的 `offset_flag` + `table_offsets`。**當 inline 找不到 offsets
或 layernames 時，依 table_offsets 跟隨檔尾表**：先 PROPNAME（讓 PROPERTY refnum 可解）→ CELLNAME
（帶 S_CELL_OFFSET）→ LAYERNAME，各為**有界 seek 到已知 table 區段**（讀到非該表 record 即停，非全檔掃）。
offset_flag=1 時用 `_read_end_table_offsets` 從檔尾 256B END 讀 6 組 offset pair（`_END_RECORD_LEN`，
SEMI P39 §14）。回傳 dict 加 `offset_flag`/`table_offsets`/`offsets_via`（inline|tables|None）供診斷。
**Calibre inline 檔兩表都 inline → 不進 table-follow，零額外成本**；只缺表的檔才觸發（最多檔尾 256B + 表區段）。

**診斷強化（issue「終端機 debug 需更多資訊」）：**
- `oasis_random`：RandomAccessReader 存 `_offset_flag/_offsets_via/_table_offsets`；`enumerate_layers`
  回傳加 `diag` 區塊並 `_dbg` 輸出。
- app `_scan_oas_with_streamer`：scan 後在終端機印多行 `[gds-scan]` 區塊（source / offset_flag /
  offsets_via / table_offsets / cell-offset 數 / LAYERNAME 數 / 找到的 layer），no-index 時附「KLayout
  請開 strict mode」提示。
- `oasis_debug.report_file`（Diagnose OASIS file… 選單）新增「index tables」段：offset_flag、located via、
  table_offsets、S_CELL_OFFSET 筆數、LAYERNAME 筆數 + 對應建議。

**測試：** `tests/test_oasis_layer_scan.py` 新增 `TestStrictEndTables`（offsets-in-START 帶/不帶 LAYERNAME、
offsets-in-END）共 3 筆 + strict 檔 builder。`QT_QPA_PLATFORM=offscreen pytest tests/` **577 passed**
（含 oasis_random/oasis_streamer 無回歸）。py_compile 全過。**手動：待 user 拿真實 KLayout strict 檔實測。**

**影響檔案：** `glas/core/oasis_streamer.py`、`glas/core/oasis_random.py`、`glas/core/oasis_debug.py`、
`glas/app/gds_align_tool.py`、`tests/test_oasis_layer_scan.py`、`SESSION_LOG.md`。
**Branch：** `claude/friendly-franklin-9uZqU`

---

## [2026-06-04] [F12] 無 LAYERNAME 檔的 layer 列舉（bounded 抽樣 + sidecar 快取）M1–M4

**變更類型：** 功能（core 2 新函式 + 新 cache 模組 + app scan 接線）+ 測試 + 文件 ·
**狀態：實作完成、sandbox 全套件 574 passed，待 user 實機驗收**

**動機：** 承本日 F12 重啟規劃。user 反映「很多 OASIS 都是無 LAYERNAME 表這型」（即使 KLayout 轉檔補了
`S_CELL_OFFSET` 仍只有數字 layer，如 17/101、6/0），「Scan layers」列空→被迫手 key。**追加硬限制：檔案
多 GB，全檔 fallback 掃描「根本開不完」，禁止 O(檔案) 全掃。** 兩個取捨題 user 皆「無偏好」→ 由實作定奪。

**實作（plan `docs/plans/F12-no-layername-scan.md` M1–M4）：**
- **M1 core `oasis_random.py`**：`enumerate_layers(path, *, progress_cb, use_cache, max_cells=64,
  max_records_per_cell=2000, time_budget_s=15, stop_after_no_new=16, include_text=True)` →
  `{"layers":[{layer,datatype,name}], "source": "layername"|"sampled"|"no-index"}`。有 LAYERNAME 表走
  原秒級 fast-path（`_layernames_to_layer_dicts`，沿用 scan_cell_offsets 既得的 layernames）；無表則
  `sample_layers`：用 `_sample_offsets` 從 S_CELL_OFFSET 表**均勻抽樣 ≤max_cells 顆 cell**，各 seek 後
  只讀前 max_records_per_cell 筆記錄收 RECTANGLE/POLYGON/PATH/TRAPEZOID(+VR/VL)/CTRAPEZOID/CIRCLE
  的 (layer,datatype) + TEXT 的 (text_layer,text_type)，連續 stop_after_no_new 顆無新 layer 或超時即
  早停。**無 S_CELL_OFFSET → 回 source="no-index" 空清單，不退化成全掃。** 單一 RandomAccessReader
  (wanted_layers=None) 共用 name-table。**完全不碰 §7 隨機存取/walk/early-stop/bbox 熱路徑。**
- **M3 core `layerscan_cache.py`（新模組）**：列舉結果 sidecar JSON 快取，key=(abspath, mtime, size, 掃描
  params 指紋)；存 per-user cache dir（XDG_CACHE_HOME/LOCALAPPDATA/~/.cache，不寫唯讀網路碟旁）；原子寫、
  壞檔/stale 一律當 miss、最壞重抽，不會弄壞 scan。enumerate_layers `use_cache=True` 命中即跳整個 reader。
- **M2 app `gds_align_tool.py`**：`_scan_oas_with_streamer` 改呼叫 `enumerate_layers`，progress_cb 節流
  （0.15s）串流「已抽 K 顆、找到 N layer：…」到 LoadProgressDialog，user 看到要的即可 cancel（沿用既有
  subprocess terminate）。`_on_scan_finished` 改吃 dict：no-index→提示用 KLayout 補索引；sampled→
  `LayerPickDialog` 加 `note` 標明「抽樣可能不全、缺的可手 key」。LayerPickDialog 既有無名稱顯示（只顯數字）。
- **M4 文件**：README features 加「Layer 掃描（含無 LAYERNAME 檔）」、CLAUDE §4 模組表 + §8 F12 更新、plan、本 log。

**測試：** `tests/test_oasis_layer_scan.py`（14 筆，autouse fixture 把 cache 導到 tmp）——抽樣列舉/TEXT
toggle/混合 shape、LAYERNAME fast-path + sentinel 斷言不進抽樣、no-index 不 hang、**bounded 上限與早停
斷言**（證明非全掃）、cache 命中跳抽樣/檔變失效/use_cache=False bypass、`_sample_offsets` 分散去重。
`QT_QPA_PLATFORM=offscreen pytest tests/` **574 passed**（560+14）。py_compile 全過。**手動 GUI 待 user 實機。**

**影響檔案：** `glas/core/oasis_random.py`、`glas/core/layerscan_cache.py`(新)、`glas/app/gds_align_tool.py`、
`tests/test_oasis_layer_scan.py`(新)、`README.md`、`CLAUDE.md`、`docs/plans/F12-no-layername-scan.md`、
`SESSION_LOG.md`。 **Branch：** `claude/friendly-franklin-9uZqU`

---

## [2026-06-04] F15 驗收（測試通過）+ [F12] 重啟規劃（範圍重界定 → plan 檔）

**變更類型：** 驗收 + 規劃（新增 plan 檔，無程式碼異動）·  **狀態：F15 測試綠；F12 plan 待 user 核准**

**F15 確認：** 沙箱裝 numpy 2.4.6 / cv2 4.13 / shapely 2.1.2 / PyQt6（+ libEGL1）後
`QT_QPA_PLATFORM=offscreen pytest tests/` **560 passed**，與 2026-06-03 條目宣稱一致；F15 核心測試
（`render_label_image` id/背景、後層覆前層保 holes、gray↔label 邊界一致、無 POI 略過路徑）全綠。
程式 + 測試層面驗收通過，僅手動 GUI 端到端待 user 實機。

**F12 重啟（依 §2.3 探索 + Q&A → plan）：** 用 Explore agent 摸清隨機存取/bbox/scan 路徑後與 user
Q&A，**範圍大幅收斂**——user 接受 KLayout 轉檔補 `S_CELL_OFFSET`（隨機存取 + bbox 由 KLayout 解決，
GLAS 端不自建 bbox 索引，避開 2026-05-28 撤案的根本效能卡點）。真正痛點只剩「這類檔無 `LAYERNAME`
表 → Scan layers 列空、強制手 key 數字 layer」。Plan 核心：`oasis_streamer.enumerate_layers` 在無
LAYERNAME 表時 fallback 掃幾何記錄列舉 distinct (layer/datatype)，餵既有 `LayerPickDialog`（已支援
無名稱條目，只顯示數字）。不碰 §7 任何不變式。Plan 存 `docs/plans/F12-no-layername-scan.md`，**待
user 核准才開工**。

**測試：** 純文件 + 驗收，無程式碼異動。

**影響檔案：** `docs/plans/F12-no-layername-scan.md`（新）、`SESSION_LOG.md`。
**Branch：** `claude/friendly-franklin-9uZqU`

---

## [2026-06-03] 完成 [F15] 模擬 GLV 灰階 + label ROI 匯出（取代 F13 binary mask）

**變更類型：** 功能（core 2 函式 + export pipeline + app dialog/worker）+ 測試 + 文件 ·
**狀態：實作完成、sandbox pytest 全綠，待 user 本地驗收**

**動機：** user 釐清下游 MMH 真正要的不是 F13 的 binary mask，而是 (1) 一張「已對齊」的
**模擬 GLV 灰階圖**（像 fine-align template）當量測底圖、(2) **ROI 資訊**。問「ROI 怎麼讀
最快」→ 決定用 **整數 label 圖**（單次 imread + `gray[label==id]` boolean index，零
rasterize/JSON/閾值）。user 進一步決定 **gray+label 直接取代 mask（拿掉 mask 選項）**。

**實作：**
- **M1 core**：`fine_align.render_label_image`（per-layer geom→`make_mask`→paint 整數 id、
  bg=0、無 blur、後層覆前層）+ `render_grayscale_from_geoms`（同組 geom→paint fg_glv + 一次
  blur，hole-preserving 版的 `render_composite_template`）；共用 `_fov_min_corner`（沿用 F13
  mask 的 y_min 1-px raise，§7）→ gray/label 像素網格一致。`OVERLAY_MANIFEST_COLS` 的
  `mask_png` 換成 `gray_png`/`label_png`。
- **M2 `overlay_export`**：`export_one_image` 以 `export_gray`/`export_label`/`score_thr`
  取代 `export_mask`/`mask_thr`；poi 入參擴成 `[(spec, color, fg_glv)]`，label id = POI 位置；
  那次 ROI walk 的 per-layer `geom` 同時餵 gray/label；cfg 取 `bg_glv`/`blur_sigma_px`。
  pool init/task 同步。移除已不用的 `gds_boolean` import。
- **M3 app**：`AlignmentExportDialog` 移除「Export GDS mask」改兩 checkbox（grayscale /
  label map），共用 score-threshold 區塊；`selected()` 回 7-tuple。`OverlayExportWorker`
  建構子改 `export_gray`/`export_label`/`score_threshold`/`label_map`；manifest schema
  bump `mmh-gds-overlay-v1`→`v2` 並加 `label_map`（id→層名+fg_glv）。`_export_overlay_images`
  /`_poi_specs_colored`(+fg)/`_export_label_map` 串接。
- **M4 文件**：README 匯出章節、CLAUDE §5.2 + §8、plan 檔、本 log。

**測試：** sandbox 裝 numpy/cv2/shapely/PyQt6 後 `pytest tests/` **560 passed**（含新
`render_label_image`/`render_grayscale_from_geoms` 決定論 + holes + gray↔label 邊界一致；
F13 mask 測試改寫成 gray/label；F5 schema v2；m5 dialog 7-tuple）。`py_compile` 全過。
**手動 GUI 端到端待 user 本地。**

**影響檔案：** `glas/core/fine_align.py`、`glas/core/overlay_export.py`、
`glas/app/gds_align_tool.py`、`tests/test_export_perf.py`、`tests/test_gds_align_f13.py`、
`tests/test_gds_align_f5.py`、`README.md`、`CLAUDE.md`、
`docs/plans/F15-glv-grayscale-roi-export.md`、`SESSION_LOG.md`。
**Branch：** `claude/optimistic-pasteur-31ELv`

---

## [2026-06-03] 結案 F9 / F10 / F13 / F14；撤案 F11（user 驗收 + 決策）

**變更類型：** 任務管理（文件）·  **狀態：完成**

**背景：** user 本地驗收後確認 F9（layout 匯出）、F10（OASIS debug mode）、F13（per-image mask 批次
輸出 + low-score re-run）、F14（batch fine-align / export 加速）皆 OK → 結案；F11（整顆 chip OASIS
匯出）決定不做 → 撤案。

**修法：**
- CLAUDE.md §8「進行中」清空（F9/F10/F13/F14 依規則完成即從清單刪除，紀錄留在 git history + 本 log）。
- F11 移到「待辦 (Backlog)」並標 ~~刪除線~~ + 撤案註記（比照 F12 慣例）；plan 檔
  `docs/plans/F11-whole-chip-export.md` 保留供日後參考。

**測試：** 純文件變更，無程式碼異動。

**影響檔案：** `CLAUDE.md`、`SESSION_LOG.md`。 **Branch：** `claude/optimistic-pasteur-31ELv`

## [2026-06-02] Batch UX：移除 score 直方圖 + 平行結果改回影像順序（user 回饋）

**變更類型：** UX/效能（app）·  **狀態：完成（待 user 實機確認）**

**背景：** user 回饋 (1) batch 結果頁的 score 直方圖沒什麼用、想再快一點；(2) batch align 進度「跳著
跑」（不是 1,2,3…），顯示怪。

**修法：**
- **直方圖**：`BatchResultsPanel._rebuild_charts` 移除 `_ScoreHistogram`，只留殘差散點圖（散點圖對
  「median residual → origin δ」有實際用途）。順帶減少每次串流刷新的圖表 teardown/rebuild 開銷。
  `score_histogram` / `_ScoreHistogram` 保留（仍有單元測試）。
- **跳著跑**：`FineAlignAllWorker._run_process_pool` 與 `OverlayExportWorker._run_process_pool` 改成
  **全部 job 先提交（worker 仍滿載平行），但結果依提交（影像）順序消費**（`for fut in futures` 取代
  `as_completed`），讓表格/overview 由上而下填、manifest 順序穩定。吞吐幾乎不變（僅進度回報序列化）。
  移除未用的 `as_completed` import。

**已釐清（非 bug）：** 先前回報匯出 overlay/mask「沒對齊」——經查為**操作面**：`AlignmentExportDialog`
預設「全部影像勾選」，batch 中途 abort（或部分影像 flat/失敗）後若直接匯出，**未算到 fine-align 的影像
沒有 `_refined` → 退回 coarse-only**，看起來才像沒對齊。只勾「已算完（有 score）」的影像匯出即正確，
與靜態比對結論一致（畫面 `paintEvent`/`_world_to_view` == 匯出 `overlay_outlines_on_sem`，anchor=
coarse+refined、nm_per_px、座標框逐項相同）。mask 因有 score 門檻把關不受影響；overlay PNG 無門檻才會
混入 coarse-only。可選後續防呆：對話框加「Only images with a fine-align result」過濾（暫未做）。

**測試：** `py_compile` 過；in-thread fallback 路徑不變。**待 user `pytest` + 實機。**

**影響檔案：** `glas/app/gds_align_tool.py`、`SESSION_LOG.md`。 **Branch：** `claude/optimistic-pasteur-31ELv`

---

## [2026-06-02] 測試修正：m5 export dialog 簽章 + m4b 子像素容差（user 本地 pytest 暴露）

**變更類型：** 測試修正（純 tests，無功能變更）·  **狀態：完成**

**背景：** user 在本地（Python 3.13 / numpy 2.4.6 / cv2 4.13 / PyQt6 6.11）跑 `pytest tests/` →
553 passed, 4 failed。

**根因 + 修法：**
- **m5 `TestExportDialog`（2 筆）**：F13 把 `AlignmentExportDialog.selected()` 從 4-tuple 擴成
  6-tuple（多 `export_mask` / `mask_threshold`），舊測試仍 `fmt, ids = selected()` → unpack 錯。
  改 `fmt, ids, *_ = d.selected()`。**屬 F13 API 演進未同步測試。**
- **m4b `TestFineAlignOne`（2 筆）**：`fine_align_one` 子像素拋物線殘差 ~9.5e-6（abs=1e-6 過嚴），
  隨 BLAS/cv2/numpy build 浮動，**與 F13/F14 無關**（未動該數學）。容差放寬 1e-6→1e-3 nm（仍遠嚴於
  任何實際對位誤差，sign/整數像素不變式不受影響）。

**測試：** `py_compile` 過；預期 `pytest tests/` 全綠（待 user 重跑確認）。

**影響檔案：** `tests/test_gds_align_m5.py`、`tests/test_gds_align_m4b.py`、`SESSION_LOG.md`。
**Branch：** `claude/optimistic-pasteur-31ELv`

---

## [2026-06-02] [F14] batch align + image/mask export 加速（規劃→M1–M4）

**變更類型：** 效能/重構（新 core 模組 + worker 平行化 + UI）+ 測試 + 文件 ·  **狀態：實作完成，待 user 本地驗收**

**動機：** user 回報 batch align 與 image/mask 匯出在上萬張規模仍太慢。探索定位兩瓶頸：
(1) **export（`OverlayExportWorker`）完全循序**——單 reader、單 thread，每張逐張 ROI walk + 畫
overlay/mask，**完全沒平行化**（而 align 早在 F8 就多進程平行）；(2) **align worker 上限寫死 8**
（cv2 多 thread × 多進程 oversubscription 顧慮），多核機核心閒置。Q&A：兩條都要加速、核心數不確定→
自動偵測+保守 cap+UI 可調、可接受多 reader 記憶體、快取 align 幾何重用列後續選項。

**實作：**
- **M1** 新增 Qt-free `glas/core/overlay_export.py`：把 `overlay_outlines_on_sem` / `_draw_polyline_np`
  / `_safe_name` 從 app 原樣搬入；新增 `export_one_image(...)`（單張 imread + raw/overlay/mask 寫出 +
  回 manifest row，內用 `poi_polys_and_geometry_for_roi` 單 walk 共用），與舊 worker 單張邏輯逐行等價。
  app 改 re-import 這些 helper（preview 仍可用）。
- **M2** `OverlayExportWorker` 改 orchestrator：`_run_in_thread`（小批/raw-only/無 reader fallback）
  + `_run_process_pool`（spawn `ProcessPoolExecutor`、`_export_pool_init/_task`、as_completed 收 row、
  cancel drop futures、依 job 序回 row 讓 manifest 穩定）。
- **M3** `fine_align.batch_worker_count(override, cap=16)`（cap 8→16；UI override 優先）；
  `fine_align._pool_init` 與 `overlay_export._export_pool_init` 內 `cv2.setNumThreads(1)`；
  FineAlignPanel 加「Parallel workers (0=auto)」spinbox（QSettings 持久化），align/export cfg 透傳
  `max_workers`。
- **M4** `tests/test_export_perf.py`（worker 數解析 / export_one_image raw-only+純函式determinism /
  missing-file / 無 POI 不寫 mask / 模組 Qt-free）+ README + CLAUDE §5.2 並行模型。

**測試：** `py_compile` 全過；既有 F5 `OverlayExportWorker._write_manifest` 測試相容（建構子向後相容）。
沙箱無 numpy/PyQt6，**pytest 綠 + 多核實測加速待 user 本地**。

**影響檔案：** `glas/core/overlay_export.py`（新增）、`glas/core/fine_align.py`、
`glas/app/gds_align_tool.py`、`tests/test_export_perf.py`（新增）、`README.md`、`CLAUDE.md`、
`docs/plans/F14-batch-export-perf.md`、`SESSION_LOG.md`。 **Branch：** `claude/optimistic-pasteur-31ELv`

---

## [2026-06-02] [F13] per-image GDS mask 批次輸出 + low-score re-run（規劃→M1–M4）

**變更類型：** 功能（app + core helper + 測試）+ 文件 ·  **狀態：實作完成，待 user 本地驗收**

**動機：** 下游 MMH 需要 per-image GDS mask 限縮 blob 偵測範圍（解 gray-level 定位失效）；
GLAS 是唯一能產 mask 的工具（Boolean + fine-align），但缺 (1) 批次 mask 輸出、(2) batch
fine-align 後針對 low-score 圖調參重跑（現只能重跑全部上萬張）。Q&A：覆蓋規則 Q1=C（新 score >
舊才覆蓋）、mask 不輸出 fallback（GLAS 把關品質，Q2）、UI 併入 export dialog（Q3）、用既有
`make_mask()`（Q4）、re-run UI 放 BatchResultsPanel（Q5）。

**實作：**
- **M1 `BatchResultsPanel` 子集 re-run**：table 下方新增 Re-run 區塊（Search radius / Background
  GL / Blur σ 覆蓋 spin，per-POI FG GL 沿用 Fine Align 面板）+「Re-run low-score」/「Re-run
  selected」鈕（table 改 ExtendedSelection 多選），emit `rerun_requested(ids, overrides)`。
  MainWindow `_on_rerun_requested` 重用 `FineAlignAllWorker` 跑子集（抽 `_launch_fa`），
  `_fa_rerun_mode` 旗標令 `_on_fa_result` 走 `fine_align.rerun_should_overwrite`（Q1=C：只變好）。
- **M2 `OverlayExportWorker`**：`__init__` 加 `export_mask` / `mask_score_threshold`；`run()` 把
  ROI walk 改成 overlay/mask 任一需要就走一次、共用 `entries`；mask 分支用
  `polys_to_geometry`→`make_mask`（FOV 左下角座標與 `overlay_outlines_on_sem` 對齊）→寫
  `{base}_mask.png`，僅 `mask_should_export(refined, thr)` 為真才寫。
- **M3 `AlignmentExportDialog`**：加 `Export GDS mask (.png)` checkbox + Score threshold spin
  （0.8 / 0–1 / 0.05）+ 即時「N image(s) ≥ threshold」label；`selected()` 多回 2 值，呼叫鏈
  （`_on_export_alignment`→`_export_overlay_images`）透傳。
- **core helper（Qt-free，便於單測）**：`fine_align.py` 新增 `OVERLAY_MANIFEST_COLS`（加
  `mask_png`）、`rerun_should_overwrite` / `mask_should_export` / `rerun_image_subset`。

**探索修正：** 草稿誤寫對話框為 `OverlayExportDialog`，實為 `AlignmentExportDialog`；
`make_mask()` 吃**單一 geom**（keyword-only），故 M2 用 `polys_to_geometry` union 後傳入。

**PR#9 review 修正（Codex，2 × P2）：** (1) **Boolean 洞保留**——原 mask 用 `poi_polys_for_roi`
回傳的 exterior-only rings（`geometry_to_polygons` 會丟內洞）重建幾何，subtraction/complement 表
達式的洞會被填實。改新增 `fine_align.poi_polys_and_geometry_for_roi`（單次 walk 同時回 polys[給
overlay] + hole-preserving geom[給 mask]）+ `gds_boolean.union_geometries`，mask 走 geom。
(2) **1px Y 偏移**——`make_mask(invert_y=True)` 用 `(H-1)-(y-y_min)/nm`，但 overlay 與 fine-align
template（`rasterize_layer`）用 `(y_top-y)/nm`（anchor→H/2）；mask 比兩者高一格。改 `y_min` 抬高一
像素（`anchor_y-(H/2-1)*nm`），使 mask 像素與 `rasterize_layer` 完全一致（新測試 array_equal 證明）。

**測試：** `tests/test_gds_align_f13.py`——5 個純邏輯測試（rerun 覆蓋規則 / 子集選取 / mask
threshold / 無 refined / manifest 欄）+ Qt+cv2 gated 整合測試（worker manifest header）+ review 修正
測試（洞保留、mask↔rasterize_layer 像素相等）。`py_compile` 全過；沙箱無 numpy/PyQt6，
**pytest 綠 + GUI 端到端待 user 本地**。

**影響檔案：** `glas/app/gds_align_tool.py`、`glas/core/fine_align.py`、
`tests/test_gds_align_f13.py`（新增）、`docs/plans/F13-mask-export-rerun.md`、`CLAUDE.md`、
`SESSION_LOG.md`。 **Branch：** `claude/optimistic-pasteur-31ELv`

---

## [2026-05-28] [F12] 探索後撤案：無索引表 OASIS 支援（改用 KLayout 轉檔）

**變更類型：** 決策 / 還原（本 session 的 F12 程式碼變更已全數 revert，淨碼變更為 0）

**背景：** user 丟一顆非 Calibre 寫出、3.9GB 的 `R8_OD_to_VC.oas`——**無 `LAYERNAME`、無 `S_CELL_OFFSET`**
兩個索引表。症狀：Scan layers 找不到 layer、ROI random-access 索引 0 cells（F10 診斷卻列得出 layer）。

**做過什麼（後來全砍）：** 開了 F12 plan + 實作 M1–M8——自建 cell offset 索引（`build_cell_index`，後改成
`consume(skip_cblocks=True)` 跳壓縮塊加速）、`RandomAccessReader` fallback、layer 幾何掃描 + 提早停、
index/layer/reach 三種 sidecar cache、worker 化。索引與 layer 掃描可達秒級。

**為何撤案：** 卡在**根本性**效能問題——這類檔無 per-cell bbox（Calibre 靠 CE 邊界層 (108,250) 每顆只讀 1
矩形即得大小；此檔無此層），`walk_roi` 首次載入為了剪枝必須把 root 整棵子樹每顆 cell 全解一遍 ≈ 全 chip
解碼，對 3.9GB 等同數分鐘且 GIL 卡 UI。reach-bbox 持久化只能讓它「一次性」，第一次仍慢。user 決定不值得，
**整批 revert 回 `e7437f1`（F11 M5）**。

**結論 / 替代方案：** 不在 GLAS 原生支援無索引表 OASIS。需要開這類檔時，**先用 KLayout 開→另存 `.oas`**
（KLayout 寫出會帶 cell offset + layer name 表），轉出的檔即可走 GLAS 現有快速路徑，零程式改動。

**影響檔案：** 無（程式碼還原）。**Branch：** `claude/adoring-cannon-oKZKo`

---

## [2026-05-27] [F11] 整顆 chip OASIS 匯出 + GDS 座標可見性（規劃→M1–M5）

**變更類型：** 功能（core + app）+ 文件 ·  **狀態：待 user 本地驗收**

**規劃：** F9 FOV 匯出驗收 OK 後，user 要 (1) 匯出**整顆 chip**（raw + boolean 新 layer，目前只能匯出當前
FOV）、(2) UI 常駐 GDS 座標好填裁剪。Q&A：boolean **全 chip 重算**、座標**常駐讀數 + 裁剪框一鍵帶入兩者都要**。
因顧慮全域 shapely 數百萬中間物件 OOM，M2/M3 改 **tiled + 串流寫出**、tile **自動分格**。

**實作：**
- **M1 GDS 座標可見性**：獨立常駐讀數 `_coord_readout`（粗體）、SemViewer 新增 `cursor_gds` signal
  （SEM/GDS 兩模式都顯示 µm+nm）、`OasisExportDialog` 裁剪區「Use current view / ROI bounds」帶入鈕。
- **M2 `oasis_writer.OasisStreamWriter`**：增量寫（header→`add_polygons` 逐 layer→256-byte END，context
  manager，錯誤不 finalize）；輸出與 `serialize_oasis` **byte 完全一致**。+ `oasis_random.reachable_bbox` /
  `reachable_bbox_nm` 唯讀 accessor（忠實複製 walk_roi closure；§7：**不改 walk/early-stop 熱路徑**）。
- **M2/M3/M4** `layout_export.tile_grid`（chip span 自動分格、覆蓋角落無縫）+ `WholeChipExportWorker`
  （QThread 分 tile：raw `walk_roi`→clip→串流寫；boolean 用 haloed tile〔外擴=最大 morph+1µm〕建 tile-scoped
  raw_provider→`resolve_expression`→clip 回 tile 串流寫，峰值受單 tile 控制解 OOM）+ `OasisExportDialog`
  scope 下拉（Current FOV / Whole chip）。
- **M5** 文件（README / CLAUDE §1·§4）。

**測試：** py_compile 全過；core tile_grid / stream-writer / reachable_bbox 有單元測試。沙箱無 numpy/shapely/
PyQt6 → **pytest 綠 + 整 chip 端到端（worker/GUI/真實檔 KLayout 比對）+ OOM/效能實測待 user 本地**。

**影響檔案：** `glas/core/{oasis_writer,oasis_random,layout_export}.py`、`glas/app/gds_align_tool.py`、
`tests/{test_oasis_writer,test_oasis_random,test_layout_export}.py`、`README.md`、`CLAUDE.md`、
`docs/plans/F11-whole-chip-export.md`。 **Branch：** `claude/adoring-cannon-oKZKo`（PR #7）

---

## [2026-05-26] [F9] OASIS 匯出：raw + Boolean layer 寫出 .oas（規劃→M1–M6→fixes→驗收）

**變更類型：** 功能（新 core 模組 + app UI）+ bug fix + 文件 ·  **狀態：core 驗收過；GUI 端到端待 user**

**規劃：** 原評估建議 GDSII，但 user 要求公司流程統一 .oas + 深度格式評估後改 **OASIS**（validation scheme
可為 0、CBLOCK/modal 選用、encoder 是既有 decoder 的逆、可用自家 reader round-trip 當 oracle）。範圍：raw
layer + Boolean layer 同檔 + GDS 座標裁剪 ROI；匯出入口走**開發者模式 gating**。

**實作：**
- **M1 `oasis_writer.py`（純 stdlib）**：encode 原語（unsigned/signed/real/string/g-delta）為 decode 的逆；
  `serialize_oasis` 輸出最小合規（MAGIC→START unit=1000 offset_flag=0→CELLNAME→CELL→XYABSOLUTE→幾何→END）；
  axis-rect→RECTANGLE(`0x7b`)、其餘→POLYGON(g-delta)。
- **M2 `layout_export.py`（shapely）**：`clip_polygons/clip_layers/export_layers` + `shapely_to_rings`
  （O-holes 決議：只取外環、所見即所得）。
- **M3 app `OasisExportDialog`**（每 layer 輸出 layer/datatype + GDS 裁剪框）+ `_on_export_oasis`。
- **M5 開發者模式**：`_dev_mode`（QSettings 持久化、About icon 點 5 次切換），Export OASIS 按鈕預設隱藏。
- **M6 文件**。

**fixes：** (a) END record 補滿到 **256 bytes**——KLayout 嚴格要求，否則 `too few bytes after END` 拒檔；
自家 reader 在 END 即 return、padding 不被 decode 不受影響。(b) PR#7 review P2：layer/datatype spinbox 上限
65535→2147483647（避免大 layer ID 靜默截斷）。(c) `scripts/make_sample_oas.py`（產 sample_good/broken 測試檔）。

**驗收：** user 本地 `pytest` 45 passed；KLayout 開 256-END 修正後 `sample_good.oas` 三 layer（RECTANGLE/
POLYGON/g-delta POLYGON）**正確渲染**——writer 格式被 KLayout 接受（最大風險解除）。**剩餘：** GUI Export+Debug
端到端需載入 layout，待 user 有 production 資料時測。

**影響檔案：** `glas/core/{oasis_writer,layout_export}.py`、`glas/app/gds_align_tool.py`、
`scripts/make_sample_oas.py`、`tests/{test_oasis_writer,test_layout_export}.py`、`README.md`、`CLAUDE.md`、
`docs/plans/F9-layout-export.md`。 **Branch：** `claude/adoring-cannon-oKZKo`（PR #7）

---

## [2026-05-26] [F10] OASIS debug mode：載入/匯出雙向診斷（實作 + 驗收）

**變更類型：** 功能（新 core 模組 + app UI）·  **狀態：Diagnose 驗收過；GUI Export-debug 端到端待 user**

**動機：** 開發 streamer/writer 常 parse 出錯，希望備診斷模式。Q&A：載入+匯出**兩端都要**、sidecar
`.debug.txt` + app 內可複製對話框**兩種輸出都要**。

**實作：** `oasis_debug.py`（Qt-free）`report_file`——走 streamer 統計 record histogram / per-layer rect+poly /
START unit+offset_flag / cell names；**永不拋例外**，decode 出錯收 hex-context（streamer 內建）+ traceback；
給 `sent_layers` 做送出↔讀回 round-trip 比對。`layout_export.export_layers` 加 `debug` 參數回 `(n, report)`。
app：`DebugReportDialog`（唯讀 monospace + Copy）、`OasisExportDialog` Debug checkbox、File 選單 dev-only
「Diagnose OASIS file…」、載入失敗於 dev mode 自動對該檔產報告→sidecar+可複製框。

**驗收：** Diagnose 對 broken 檔精準捕捉 decode error + hex + traceback；開發者模式開關 OK。

**測試：** `tests/test_oasis_debug.py`（well-formed/round-trip/truncated/缺檔）。**不動（§7）：** 純新增診斷。

**影響檔案：** `glas/core/{oasis_debug,layout_export}.py`、`glas/app/gds_align_tool.py`、
`tests/test_oasis_debug.py`、`README.md`、`CLAUDE.md`、`docs/plans/F10-debug-mode.md`。
**Branch：** `claude/adoring-cannon-oKZKo`（PR #7）

---

## [2026-05-25] [F8] Batch 反應性與加速（規劃→M1–M4）+ F5/F6/F7/F8 收尾

**變更類型：** 功能/效能 + test fix + 任務收尾 ·  **狀態：實機驗收通過、已結案**

**動機：** user 回報 Batch Align 很卡、運算久、進度條花俏。三根因：(a) `_on_fa_result` 每張整表重建 + 圖刪重生
= O(N²) 主執行緒重繪；(b) F6 thread-pool 8 條純 Python 解碼搶 GIL；(c) `_AnimatedBar` 漸層/發光/掃光動畫。

**實作（plan F8）：** M1 進度條扁平化（單色軌道+填充、determinate `advance()` 不重繪）；M2 節流串流
（`_batch_refresh_timer` 300ms 合併刷新、串流時跳過圖表重建，修 O(N²)）；M3 ProcessPool——抽 Qt-free
`glas/core/fine_align.py`（rasterize/template/matchTemplate/ROI-walk 等 10 純函式），`FineAlignAllWorker`
改 `ProcessPoolExecutor`（spawn，worker 由**路徑**重建 reader 避開 Windows 拉 PyQt6；cancel 用 `fut.cancel()`
張邊界粒度；n≤2/單核走 in-thread fallback）；M4 `TestProcessPoolEquivalence`（每張 result 與循序相等）。

**test fix（4 個）：** 1 F8 回歸（`_run_in_thread` 誤用 `clone()`→改直接用 `self._rar`）+ 3 既有過時測試
（expr_spec 4-tuple、outline cv2.LINE_AA 改斷言偏紅、batch_run no-coords 回 status）。

**收尾：** 驗收後 F6/F7/F8 三 plan 標 done、CLAUDE §8「進行中」清空（plan 留作 design history）。

**測試：** 本地 **206 passed**（含 ProcessPool 等價）。實機：UI 不卡、多核生效、明顯變快、結果正確、進度條扁平 OK。

**不動（§7）：** 批次純函式只搬家、結果不變、fine-align 符號、SemViewer 折疊、CE early-stop、median→δ。
取捨：cancel 粒度由逐 node 改為單張影像邊界。

**影響檔案：** `glas/core/fine_align.py`（新）、`glas/app/gds_align_tool.py`、`tests/{test_accel_equivalence,
test_gds_align_f5,test_gds_align_m4b}.py`、`docs/plans/F5–F8 plans`、`CLAUDE.md`。
**Branch：** `claude/practical-pascal-AtKLm`

---

## [2026-05-25] [F7] Batch 工作區 + inline 進度 + 進度條質感（規劃→M1–M4）

**變更類型：** 功能（UI/UX，運算不變）

**規劃：** 批次結果改第四個 view-mode「Batch」、inline 進度+結果 streaming（取代 modal）、進度條漸層+發光+%。
規劃期間 user 反映 Batch 放 View 排怪 → 改**動作進入 + 返回鈕**。

**實作：** M1 `_AnimatedBar` 質感升級（漸層+軟發光+條內%、加高）；M2 抽 `BatchResultsPanel`（summary/篩選/
排序表/直方圖/散點/median 鈕 + 頂部 inline 進度區）；M3 Batch 工作區（`_center_split`=[結果, SEM]、
enter/exit、點任一 View 鈕離開）；M4 批次接線改 inline（不再 modal、streaming 更新、即時 cancel、點列就地
換 overlay），移除 `FineAlignResultsDialog`。

**測試：** py_compile 過；GUI 待本地。**不動：** F6 批次運算與結果值（§7）。
**注：** 進度條質感後由 F8 回退扁平、串流由 F8 改節流。

**影響檔案：** `glas/app/gds_align_tool.py`、`docs/plans/F7-batch-workspace-ui.md`、`CLAUDE.md`。
**Branch：** `claude/dazzling-cori-5T7XE`

---

## [2026-05-25] [F6] OAS 讀取 + 批次 fine-align 加速（規劃→M1–M3 + PR#5 fix）

**變更類型：** 功能（效能，行為不變）

**規劃：** 功能不變前提下找加速點。Q&A：批次 **thread pool**（per-thread reader + 共享 mmap）、worker
**自動**（cpu_count≤8）、mmap **只用於 ROI/隨機存取路徑**（bulk decode 維持 slurp）。

**實作：** M1 mmap-backed `OasisStream`（+ BytesIO/平台/空檔 fallback slurp）；M2 單一 map 共享
（去 `RandomAccessReader` 雙重 slurp、檔案只 map 一次 + `close()`/context manager）；M3 thread-pool 批次
（抽 `_fine_align_image`、per-thread `clone()` 私有 reader、結果與循序逐值相同、**cv2 設定不動**保 golden）。
**PR#5 review P1：** cancel 後一律 drain 所有 future、保留已完成結果（移除 `break`）。

**測試：** `tests/test_accel_equivalence.py`（mmap↔slurp、共享↔獨立 scan、循序↔4-worker 等價）；沙箱
numpy-free 等價檢查通過。**注：** M3 thread-pool 後由 F8 ProcessPool 取代；M1/M2 mmap 仍在用。

**影響檔案：** `glas/core/{oasis_streamer,oasis_random}.py`、`glas/app/gds_align_tool.py`、
`tests/test_accel_equivalence.py`、`docs/plans/F6-readwalk-batch-accel.md`。
**Branch：** `claude/dazzling-cori-5T7XE`（PR #5）

---

## [2026-05-25] [F5] Fine-align 診斷 + 工作流（規劃→M1–M6 + PR#4 fix）

**變更類型：** 功能 + bug fix

**規劃：** fine-align 結果可視化/診斷；多次擴充收斂 6 milestone。

**實作：** M1 `overlay_outlines_on_sem` + TemplatePreviewDialog（before/after 5 格）；M2 `FineAlignResultsDialog`
（排序表/篩選 + `_ScoreHistogram` + `_ResidualScatter`，result signal 擴 6-tuple，每張回狀態 ok/no-coords/
missing-file/no-scale/flat）；M3 中位殘差→origin δ 一鍵套用；M4 setup 快照/還原（切 DID 不丟 POI/可見性/
顏色）+ 命名（Background/Foreground GL）；M5 cancel 改 `threading.Event`（DirectConnection 即時）+ `_AnimatedBar`
ETA；M6 `AlignmentExportDialog` raw/overlay PNG + manifest（schema `mmh-gds-overlay-v1`）。
**PR#4 review P1：** 非 ok 狀態清掉舊 refined offset（`_refined.pop` + `clear_score`）。

**測試：** `tests/test_gds_align_f5.py`；沙箱無 PyQt6/numpy/cv2 → GUI 待本地。

**影響檔案：** `glas/app/gds_align_tool.py`、`tests/test_gds_align_f5.py`、
`docs/plans/F5-finealign-diagnostics.md`、`CLAUDE.md`。 **Branch：** `claude/sharp-lamport-YIk3z`（PR #4）

---

## [2026-05-25] [F4] Boolean 強化（規劃→實作→閃退/預覽→方向性 W/H→結案）

**變更類型：** 功能（新功能 + 重構）+ bug fix ·  **狀態：F4 + F1 已驗收結案**

**規劃：** synthetic layer 只算一次、ROI reload 即遺失、無法編輯/巢狀。收斂：食譜化每 FOV 自動重算、巢狀
引用、編輯/刪除、表達式對話框重設計。

**實作：** 引擎 `normalize_binding` / `recipe_dependency_order`（拓樸排序+循環/未知 ref 偵測）/
`resolve_expression`（巢狀 ref + memoize + 循環防護）；app `_recipes` 唯一事實源、`_recompute_recipes` 每次
載 ROI 自動重算（synthetic 跟著 defect 走）、`_LayerRow` 編輯/刪除、`ExpressionLayerDialog` 重設計（token
按鈕 + 即時語法檢查 + binding 含 ref + 內嵌預覽）、cache 改 recipe 序列化（含舊格式遷移）。

**bug fix：** edit 閃退根因 = row handler 內同步 `exec()` 對話框→關閉後 row widget 被刪→use-after-free；
改 `QTimer.singleShot(0,…)` 延遲開窗。內嵌 `_ExprPreview` 不再 mutate 主 doc、OK→Save。

**方向性 W/H morphology：** 原為等向 buffer（W/H 只是標籤）；改 W=X 軸、H=Y 軸、`>`grow/`<`shrink，每邊 ±n nm。
`_dilate_axis`（與軸線段 Minkowski sum，對任意多邊形精確）+ `_morph_axis`（shrink=補集-膨脹-補集 erosion，
需 fov_bbox）；對話框運算子鈕 `>W: >H: <W: <H:`。+ `CoordinateSetupPanel` label `setWordWrap`（修面板溢出）。

**結案：** user 本地驗收 F4（含 boolean、KLayout 可開）+ F1（互動：對位/拖動/fine-align/批次/匯出/折疊）皆 OK；
§8 移除 F4/F1。

**測試：** `tests/test_gds_boolean.py`（binding/拓樸/resolve/morph 方向性）；沙箱無 numpy/shapely → 待本地。

**影響檔案：** `glas/core/gds_boolean.py`、`glas/app/gds_align_tool.py`、`tests/test_gds_boolean.py`、
`docs/plans/F4-boolean-enhance.md`、`CLAUDE.md`。 **Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

---

## [2026-05-25] [F3] 多 POI Fine Align + UI 優化（規劃→M1–M5 + 後續修正）

**變更類型：** 功能（fine align 多 POI / UI）+ bug fix

**規劃：** user 提 6 項；收斂為版面/裁切修正、Layer 用 LAYERNAME 顯示名稱、POI 鈕放大、Fine Align 改**多 POI**
（各自 FG gray、合成一張樣板做單次 matchTemplate、彈窗並排 SEM/GDS/Template）。

**實作：** M1 版面/裁切（`CollapsibleSection` SetMinimumSize、對話框最小寬夾螢幕）；M2 LAYERNAME 名稱
（`scan_cell_offsets` 收集 layernames、`resolve_layer_name`、`LayerEntry.display_name`）；M3 多 POI 核心
（`render_composite_template`）；M4 POI 多選 UI（POI 鈕放大）；M5 `TemplatePreviewDialog`。

**後續修正：** toolbar 窄視窗改橫向 `QScrollArea` 捲動不裁字；`resolve_layer_name` 改「最具體優先 + 跳過
(0,INF) catch-all」（修所有 layer 同名）；移除無用 opacity slider；多 POI 選取改以 row `isChecked()` 重建
（修 `ndarray __eq__` ValueError，PR#3 P1）。

**測試：** `tests/{test_oasis_random,test_gds_align_m4b,test_gds_align_m7}.py`；GUI 待本地。

**影響檔案：** `glas/app/{gds_align_tool,collapsible}.py`、`glas/core/{oasis_streamer,oasis_random}.py`、
`tests/*`。 **Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

---

## [2026-05-24] UI / branding 整合（抽離後首輪 UI 微調）

**變更類型：** 功能（UI / branding）

**內容（合併本日多筆）：**
- **品牌整合**：視窗 icon / toolbar wordmark、自繪 About 對話框、app 改名 "GDS Align Tool"→**GLAS**
  （setApplicationName 等）。
- **依 `docs/glas_ui_fixes.md` 五項**：Coordinate Setup 預設收起、LAYERS 空白引導改三層置中、Set/Clear
  Offset 移入 `SemPanel`、toolbar group label 對比、empty state 文案。
- **UI batch 1**：Load SEM 主色按鈕、`CollapsibleSection` 折疊 badge（FOV 已設/未設）、image list 對位狀態
  badge（`_ImageListDelegate`：no-coords / score 綠琥珀紅）。
- LAYERS empty hint 置中微調。

**測試：** py_compile 過；`pytest tests/` 435→**442 passed**（含 offscreen render-grab 煙霧測試）。

**影響檔案：** `glas/app/{gds_align_tool,collapsible}.py`、`tests/{test_gds_align_m6,test_gds_align_m7,
test_gds_align_m4b}.py`。 **Branch：** `claude/{determined-einstein-Bfo0G, jolly-babbage-8nwED}`（PR #2）

---

## [2026-05-24] GLAS 專案自 MMH 抽離成立

**變更類型：** 專案建立 / 重構（抽離）

**動機：** GDS Align Tool 原藏在 MMH 專案 `tools/`（plan F2，M1–M7 全實作）。其核心能力（大檔 OASIS
streaming/random-access、KLARF↔GDS 換算、FOV 查詢、Boolean 引擎、SEM↔GDS 對位）可跨專案複用，故抽離成
獨立 repo **GLAS（GDS-Layout Alignment for SEM）**。

**實作（自 MMH git HEAD 搬移，零行為改動）：**
- **glas/core/（無 Qt）**：`oasis_streamer/oasis_store/oasis_walker/oasis_random/gds_fov/gds_boolean/
  gds_layer_cache` + 自 MMH 複製的 `klarf_parser`（core 原本即無 src 依賴，零修改）。
- **glas/app/（PyQt6）**：`gds_align_tool`（改 header import：`from src.*` → flat sys.path）、`sem_loader`
  + 複製 `styles/collapsible/icons`。
- **import 慣例**：core/app 以扁平 sys.path 模組互相 bare-import；`main.py` + `conftest.py` 設 path。
- **規則機制移植**：`CLAUDE.md`（§2 規則 / §6 慣例 / §8 任務 / §10 checklist；§1/§4/§5/§7 改寫成 GLAS）、
  `.claude/settings.json` + hooks、`README.md`、本 `SESSION_LOG.md`、`docs/plans/F2*` design history、
  14 個 test 檔 + `fixtures/sample_real.klarf`。

**測試：** sandbox 無 numpy/cv2/shapely/PyQt6/pytest → 僅 py_compile 全過；完整 `pytest tests/`（~218 項
應全綠，證零行為改動）待有相依環境執行。

**接續任務：** [F1] 互動驗收（自 MMH 抽離前即「待 user 本地驗證」，移到 GLAS 接續 → 已於 2026-05-25 隨 F4 結案）。

**影響檔案：** 整個 GLAS repo（新建）。 **Branch：** （新 repo）
