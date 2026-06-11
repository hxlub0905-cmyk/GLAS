# [F24] ROI walk + 批次 fine-align 效能分析與改善

> **狀態：** in progress（量測 harness 完成；待 user 回傳實機 log 後填數據、定優先序）
> **§8 ID：** [F24]
> **建立：** 2026-06-11
> **負責 branch：** claude/gifted-lovelace-uvnn8v

---

## Goal & Context

**動機：** user 要求對 GLAS 做一次效能分析與測試，之後提出改善方案。經 Q&A 收斂，
聚焦兩條互相串接的主路徑：

1. **ROI walk 隨機存取**（`oasis_random.walk_roi`）—— 點一張 SEM 影像時、把 FOV 對應的
   layout 幾何從大檔載進來。
2. **批次 fine-align**（`fine_align._fine_align_image` + `_BatchPool` process pool）——
   「Run all」把整批影像的 per-image 對位算完；其 per-image 工作的第一階段就是 ROI walk，
   所以兩者是同一條熱路徑的近端 / 遠端。

**現況脈絡（已做過的效能工作）：**

- **M1.13** parser 系列：lazy repetition（`decode_repetition` 51%→15%）、walker 向量化
  （114s→2.4s）、fast-consumer + byte-reader（store.run 77.7s→36.2s）。
- **F16** S_BOUNDING_BOX 免解碼 ROI 剪枝（`reachable_bbox` 用 name-table sbbox，跳過幾何解碼）。
- **F16-B** `cellcache` 大 cell 解碼結果 sidecar 快取（跨 worker / session 重用）。
- **F23** batch pool：注入既有索引（省 K× 重掃 name table）+ 常駐 / 預熱 process pool。
- **batch-perf** raw POI 跳過被丟棄的 `unary_union`（密集 FOV 每張省 30ms–1s）+ `GLAS_FA_TIMING`
  分段計時儀表。

**這次的成功長相：** 不是再憑空猜。先有一支能在 **production 大檔 + 實機**上量測這兩條路徑的
turnkey harness，跑出 cold/warm ROI walk、per-stage fine-align、平行吞吐的**真實數字**，
再據此把改善項排序、逐一驗證前後對比。

**這次產出：** 本分析報告 + 改善 plan（不直接動 production 程式碼，待數據定案再開 milestone）。

---

## Q&A Decisions

### Q1: 聚焦哪個子系統？
**選項：** OASIS 大檔解析 / ROI walk 隨機存取 / 批次 fine-align / Boolean 引擎 + 匯出
**選擇：** ROI walk 隨機存取 + 批次 fine-align
**理由：** OASIS 解析（M1.13）已大幅優化過；user 日常痛點在「點圖載入」與「Run all」的等待。

### Q2: repo 無大型 OASIS 樣本檔，如何實測？
**選項：** 建合成基準 harness / user 提供真實檔路徑 / 純靜態分析
**選擇：** 「你寫腳本，我在實機跑、回傳 log」
**理由：** 真正的瓶頸只在 production 大檔 + 真實 cell 階層 / repetition 密度下才顯現；
合成檔無法代表，純靜態分析無數據佐證。→ 交付一支在 user 實機跑的量測腳本。

### Q3: 產出程度？
**選項：** 分析報告 + 改善 plan / 報告 + 直接實作優化
**選擇：** 分析報告 + 改善 plan
**理由：** 先看清全貌（實機數據）再決定做哪些，避免在沒數據下盲改。

---

## 量測方法（Methodology）

> **2026-06-11 轉向：** user 指出「離線跑合成檔測不準」——實際工作流是**載入兩組（大 +
> 小）檔 → 載 3~4 layer 做 Boolean → 填 POI → Batch align**，希望**在 UI 操作當下時時監測**。
> 故主要量測手段改為 **app 內建即時效能監測（HUD）**；下方合成 harness 退為次要（CI / 離線
> 重現用）。

### 主要：app 內建即時效能監測 HUD（M1，已完成）

- 核心 `glas/core/perfmon.py`（Qt-free）：session 單例 `monitor`，`record(op, ms, **meta)`
  累積 ring buffer + per-op 聚合（次數/平均/最大/最近），可開 **.txt log** 即時逐行寫出，
  並透過 `on_event` callback 餵 UI。執行緒安全（RLock），可從 ROI / batch worker thread 呼叫。
- UI `glas/app/perf_panel.py`：QDockWidget HUD（dev mode 顯示、View 選單可切），上半 per-op
  聚合表、下半逐筆事件 log、「Log to .txt…」按鈕。worker thread 事件經 pyqtSignal marshal
  回 GUI thread。
- 插樁點（covered 工作流每一步）：`open`（reader build = 開檔 + name-table scan）、`roi`
  （ROI walk 整批 per-layer 總和）、`boolean`（`_eval_expression` 每次表達式評估）、`template`
  （POI → 模板合成）、`batch`（Run all 整批 wall-clock + img/s）。batch 的 per-image 分段
  （read/poi/template/match）仍走既有 `[fa-timing]` console（worker 在獨立 process）。
- **使用：** 連點 About icon 5 次開 dev mode → 底部出現「Performance monitor」面板 → 照常操作
  （載大小檔、Boolean、POI、Batch align）即時看數字；按「Log to .txt…」把整段工作流寫成
  純文字檔回傳，供後續 M2+ 改善分析。

### 次要：離線合成 harness `tools/bench/bench_roiwalk_finealign.py`

turnkey、最少只要一個 OASIS 路徑（root / layer / ROI 全自動推導）。在 **user 實機 + 真實 .oas**
上跑，印出五區段 + 一段可直接複製回傳的摘要。

```bash
# 最簡：
python tools/bench/bench_roiwalk_finealign.py /path/to/prod.oas

# 建議（指定主 layer、看穩態、開 cold-walk cProfile）：
python tools/bench/bench_roiwalk_finealign.py /path/to/prod.oas \
    --layer 17/0 --fov-um 5 --repeats 3 --images 40 --profile \
    --json bench_out.json
```

**五區段量測什麼：**

| 區段 | 量測 | 關鍵指標（瓶頸訊號） |
|---|---|---|
| [1] reader build | name-table scan 成本 | `S_BOUNDING_BOX cells`：0 → 首次 walk 走「bbox-by-decode」慢路徑 |
| [2] resolve | root / layer / ROI 自動推導 | chip bbox、ROI 大小 |
| [3] ROI walk | cold（首次解碼）+ warm（memo 命中）+ 可選 cProfile | `cells_decoded` vs `cached`、`instances_materialized` vs `visited`、`max_array_k`、`t_place/t_rect/t_poly` |
| [4] fine-align stages | 單張 walk / rasterize+blur / matchTemplate 分段 | 哪一段佔比最高（poi-walk vs match） |
| [5] batch parallel | process pool K worker 端到端吞吐 | `speedup vs seq`（離理想 K× 多遠）、pool warm 成本 |

> 已在合成 OASIS 上 smoke-test 五區段全綠（驗證 harness 邏輯正確；非 production 數據）。
> 合成檔產生器：`tools/bench/_make_sample_oasis.py`（僅供 harness 自我驗證，非代表性樣本）。

---

## 靜態分析（待實機數據佐證 / 推翻）

> 以下為讀碼得到的瓶頸假說與量測訊號的對應；**待 user 回傳 log 後**在各條補真實數字、
> 排優先序。

### A. ROI walk（`oasis_random.walk_roi`）

1. **首次（cold）walk 的剪枝是否「免解碼」是頭號變因。**
   `reachable_bbox()`（oasis_random.py:1309）先試 `rar.sbbox_for(cid)`：name-table 有
   S_BOUNDING_BOX → 直接用、零解碼；**沒有** → fallback `load_cell_bbox()` 解碼幾何求 bbox。
   → harness [1] 的 `S_BOUNDING_BOX cells` 為 0 時，整棵階層第一次都得 decode-by-bbox，
   cold walk 會明顯變慢。**先確認三個常用檔是否都帶 sbbox**（CLAUDE.md §8 [F17] 已標：
   會慢的大檔都帶 sbbox）。若實機檔不帶 → [F17] 的一次性 sweep + sidecar 升優先。

2. **regular-grid repetition 的展開是否在做白工。**
   `RoiWalkStats.instances_materialized ≫ instances_visited`（且 `max_array_k` 很大）=
   一個大規則陣列被整個展開、卻只留少數命中 → 解法是 analytic sub-grid clip
   （`_clip_grid_offsets` oasis_random.py:1231 已存在；要看它在實機是否真的咬到）。
   harness [3] 直接印這三個數，一眼判斷。

3. **prune 迴圈的 per-record numpy overhead。**
   `placements_scanned` / `rect_specs_scanned` 很大、但 `instances_materialized` 小 →
   成本在「逐筆掃描 + bbox 比對」而非展開。可考慮把 placement bbox 比對更徹底向量化
   （目前 `_roi_overlap_mask` oasis_random.py:1097 已是整陣列 mask；要看殘留的 per-spec loop）。

4. **warm walk 已便宜（memo + reach_memo 跨 walk 重用）。**
   cold/warm ratio 高 = 成本集中在首次解碼，符合設計；批次的痛點因此落在「每個 worker
   各自付一次 cold」（見 B4）。

### B. 批次 fine-align（`fine_align._fine_align_image` + `_BatchPool`）

per-image 四階段：`read`(cv2.imread) → `poi`(walk+bool) → `template`(rasterize+blur)
→ `match`(matchTemplate)。SESSION_LOG batch-perf 已實測 matchTemplate
3ms(512²)/14ms(1024²)/62ms(2048²)，且多執行緒無加速（故 cv2 pin 單緒）。

1. **poi-walk vs match 誰主導，決定下一步往哪打。** harness [4] 給真實佔比：
   - **match 主導**（影像大 / 幾何稀疏）→ 做 **matchTemplate 金字塔**（coarse-to-fine：
     先在降採樣影像粗搜、再原解析度局部精修），可大砍 2048² 的 62ms。
   - **poi-walk 主導**（密集 FOV / 複雜 Boolean）→ 往 A 區的 walk 優化 + expr 同層去重打。

2. **expression POI 同層重複 walk。** `poi_polys_and_geometry_for_roi`（fine_align.py:392）
   的 `raw_provider` 對每個被綁定的 layer 各 walk 一次；若同一 layer 在表達式出現多次，
   會重複 walk。→ 加一層 per-(layer,datatype) memo（限該張影像 ROI 內）。raw POI 已在
   batch-perf 跳過多餘 union，這條是 expression 路徑的對應優化。

3. **process pool 平行效率。** harness [5] 的 `speedup vs seq`：離 K× 越遠，代表
   IPC / 啟動 / 記憶體頻寬（K 份 mmap + 索引）瓶頸越重。若遠低於 K×：
   - 看 `pool warm`（spawn + import + reader build）佔比；F23 已注入索引省重掃，殘留是
     直譯器冷啟 + numpy/cv2 import。
   - 看單張 match 是否已吃滿單核（cv2 pin 1 緒下，K worker 才線性）。

4. **批次第一波 K worker 同時撞同一大 cell（已知、低優先）。**
   每個 worker 私有 `_memo` → 第一波都各自 decode 同一顆熱 cell。`cellcache` 磁碟 sidecar
   能跨 worker 重用**已落地的**解碼結果，但第一波同時起跑時可能都還沒寫入。
   → 可在 batch 啟動前由主行程預先 `load_cell` 熱 cell（寫入 cellcache），worker 第一波
   直接命中磁碟。harness [5] 的 warm 與 parallel 數字能量出這條的影響面。

---

## Milestones

> **全部 data-gated**：M0 已完成（harness）；M1 待 user 回傳 log 後啟動，依數據決定
> M2+ 做哪幾條、順序為何。

### M0: 離線量測 harness  [status: done 2026-06-11]

- [x] `tools/bench/bench_roiwalk_finealign.py`：五區段 + paste-back 摘要 + `--json`
- [x] `tools/bench/_make_sample_oasis.py`：合成檔供 harness 自我驗證
- [x] 在合成 OASIS 上 smoke-test 五區段全綠

### M1: app 內建即時效能監測 HUD  [status: done 2026-06-11]

- [x] `glas/core/perfmon.py`：Qt-free 事件收集器（聚合 + .txt log + callback，thread-safe）
- [x] `glas/app/perf_panel.py`：QDockWidget HUD（聚合表 + 事件 log + .txt 開關），cross-thread 橋接
- [x] 主視窗掛 dock（dev mode 顯示、View 選單可切）、closeEvent detach
- [x] 插樁 open / roi / boolean / template / batch 五類操作
- [x] `tests/test_perfmon.py`（13）+ `tests/test_perf_panel.py`（5）+ 主視窗整合驗證

### M2: 實機量測 + 數據定優先序  [status: planned]

- [ ] user 在 dev mode 下照常操作（大小檔 → 3~4 layer Boolean → POI → Batch align），
      開「Log to .txt…」把整段工作流寫成純文字檔回傳
- [ ] 把數字填回本檔「靜態分析」各條，確認 / 推翻假說
- [ ] 依數據排出後續改善的實作順序（下列為候選池，非承諾全做）

### M3（候選）: matchTemplate 金字塔  [status: planned · 若 match 主導]

- [ ] coarse-to-fine：降採樣粗搜 + 原解析度局部精修，保持結果等價（峰值位置一致）
- [ ] 驗證：`tests/test_accel_equivalence.py` 加金字塔 vs 全解析度 dx/dy/score 等價（容差內）

### M4（候選）: expression POI 同層 walk 去重  [status: planned · 若 boolean/poi 主導]

- [ ] `raw_provider` 內加 per-(layer,datatype) ROI memo，同層只 walk 一次
- [ ] 驗證：同表達式多次引用同層，結果與去重前 byte 等價

### M5（候選）: 批次首波熱 cell 預解碼  [status: planned · 若 batch 首波重複解碼明顯]

- [ ] 主行程在 dispatch 前對 ROI 涉及的熱 cell `load_cell` 一次（寫入 cellcache）
- [ ] 驗證：worker 第一波 cellcache 命中率上升、warm→first-image 延遲下降

### M6（候選）: regular-grid analytic sub-grid clip 補強  [status: planned · 若 inst_mat≫visited]

- [ ] 檢視 `_clip_grid_offsets` 為何沒咬到大陣列；補 analytic 子網格裁剪
- [ ] 驗證：`instances_materialized` 大幅下降、結果幾何等價

---

## Affected Files

- `tools/bench/bench_roiwalk_finealign.py`、`tools/bench/_make_sample_oasis.py`（新增，M0）
- `glas/core/perfmon.py`（新增，M1：Qt-free 監測核心）
- `glas/app/perf_panel.py`（新增，M1：HUD 面板）
- `glas/app/gds_align_tool.py`（M1：掛 dock + View 選單 + 五處插樁）
- `tests/test_perfmon.py`、`tests/test_perf_panel.py`（新增，M1）
- 後續（依 M2 數據定案）：`glas/core/fine_align.py`、`glas/core/oasis_random.py`、
  `tests/test_accel_equivalence.py`

---

## Risks / Open Questions

- **數據依賴：** 所有 M2+ 都等 user 實機 log；在此之前不動 production 程式碼。
- **harness 代表性：** [4]/[5] 的 SEM 影像為合成（match 成本只取決於影像/模板尺寸，故
  match 量測可信；walk/template 用真實幾何故可信）；唯「真實 SEM 內容」對 matchTemplate
  的 score 分佈不影響耗時，量測時間有效。
- **等價性紅線：** 任何 M2+ 優化都是純效能變更，必須維持輸出等價（CLAUDE.md §7：
  KLARF↔GDS sign、SemViewer 折疊不變式、cellcache 鍵）。

---

## 驗證方式

- [ ] M0 harness 在合成檔五區段全綠（已完成）
- [ ] M1 取得實機數據並回填本檔
- [ ] 每個落地的 M2+ 都有 `tests/test_accel_equivalence.py` 等價測試
- [ ] `pytest tests/ -v` 全綠
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 在最終 SESSION_LOG 條目註記 `完成 [F24]`
- 從 `CLAUDE.md` §8 移除該任務
- **本檔保留**，作為 design history
