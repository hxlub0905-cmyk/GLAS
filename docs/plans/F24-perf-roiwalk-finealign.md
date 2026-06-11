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

### ✅ M2 實機數據（2026-06-11，E3B 小檔：346MB / 13,276 cells / sbbox=0 / 4 layer / ~399 張 batch）

| 操作 | 實測 | 判讀 |
|---|---|---|
| Open + index | 180 ms | 不是問題 |
| ROI walk（首次/cold） | **12,395 ms** · decoded=13,330（≈全檔） | **sbbox=0 → reachable_bbox 解碼整檔**（[F17] 假說證實） |
| ROI walk（warm） | 1,000~2,500 ms · decoded 0~50 | reach_memo 暖後只解少數新 cell；殘 1~2.5s 為 4-layer 遍歷+emit |
| **Boolean eval** | **800~2,900 ms/次**（GUI thread！） | morph（`W:n` grow/shrink）× 數千 poly 為主成本；隨 out_polys 陡升 |
| Boolean 重複算 | 每次 ROI 重載 3 recipe 各重算；`(A>W:7)` 子式被算 3× | **共用子式無 memo** → 去重可省 ~1/3+ |
| Template build | 30~45 ms | 可忽略 |
| **Batch align** | **1,293,328 ms / 399 張 = 3,241 ms/張、0.3 img/s** | 頭號痛點；每張 worker 重跑 walk+boolean，boolean×399 為主因 |

**結論（依數據定的優先序）：**
1. **Boolean 是雙重痛點** —— 互動時凍 UI（0.8~2.9s × 3 recipe／重載），且乘 399 撐起整個 batch。
   → 共用子式去重（P1）+ morph 本身加速（P4）一次打中 live 與 batch。
2. **sbbox=0 → 首次 walk 解碼整檔 12.4s** —— 大檔更痛、每個 batch worker 各付一次。
   → 一次性 per-cell bbox sweep + 磁碟 sidecar（P3 = [F17]），跨 session / 跨 worker 重用。
3. **Boolean 在 GUI thread** → 互動凍結。→ 移到 worker thread（P2，純體感修復）。
4. **batch per-stage 缺口** → 加 batch per-image 計時回傳（P5），確認 boolean vs walk 佔比。

### ✅ M2b 第二次實機數據（2026-06-11，P1+P4+P5 之後，同 E3B / 399 張）

| 項目 | 改善前 | 改善後 | 變化 |
|---|---|---|---|
| Boolean `(K>W:10<W:10)-(A>W:7)`（morph 最重） | 最高 2,922 ms | 最高 1,881 ms | **−36%**（P4 morph） |
| live recompute（3 recipe 總和，估） | ~4,500 ms | ~2,800 ms | **−35%** |
| **Batch align** | 1,293,328 ms（21.5 分） | 1,073,929 ms（17.9 分） | **−17%** |

**P5 揪出 batch 真凶（per-image CPU，399 張平均）：** read=67ms · **POI walk+bool=21,159ms** ·
template=95ms · match=90ms。→ **match 完全不是瓶頸（確認 M6 金字塔不需要）**；POI(walk+bool) 佔
~99%。21,159ms × 399 ÷ ~8 worker ÷ 60 ≈ 17.9 分，與總時間吻合。

**新發現 → M9：** batch 每張有 **3 個 POI 表達式，彼此不共用快取**（P1 的 per-image 快取原本是
「每個 spec 各建一份」），故共用 raw layer A **每張被 walk 3 次**、`(A>W:7)` 每張算 3 次——
live 的 `_recompute_recipes` 有跨 recipe 共用、batch 漏掉。→ M9 把快取共用到「同一張影像的所有
POI spec」。

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

### M3: P5 batch per-stage 診斷  [status: done 2026-06-11]

- [x] `fine_align.pool_collect_timing()`：回傳並重置 worker 的 `_FA_TIMING_ACC`
- [x] `FineAlignAllWorker` 加 `stage_timing` signal；process-pool 路徑 over-submit
      workers×3 個 collect probe 聚合（每 worker 真值計一次、其餘 0，總和精確）；
      in-thread 路徑本地收集
- [x] `_on_fa_stage_timing` 記 `batch:read/poi/template/match` 四事件進 monitor（HUD/.txt）
- [x] 端到端驗證：合成 batch 跑出 per-image 分段（poi(walk+bool) 主導）

### M4: P1 Boolean 共用子式去重  [status: done 2026-06-11]

- [x] `gds_boolean.evaluate` 加 `node_cache`+`ref_ids`：以 `_canon_key`（leaf 用解析後
      layer 身分）memo 每個 AST 子樹結果；`resolve_expression` 加 `_eval_cache` 串接
- [x] app `_recompute_recipes`：跨 recipe 共用 `raw_memo` + `eval_cache`
      → `(A>W:7)` 算 1 次而非 3 次；`fine_align` expression POI 每張共用 per-image cache
- [x] 驗證：`tests/test_gds_boolean_cache.py`（16）—— 共用子樹只算一次、不同 layer 不混、
      含 morph/diagonal/hole 的結果與無 cache 等價

### M5: P4 morph 加速  [status: done 2026-06-11]

- [x] profile 確認 `_dilate_axis` 成本 = `unary_union`(53%) + per-edge `Polygon()`(~25%)
- [x] 向量化 parallelogram（單一 `shapely.polygons`）+ 跳過與掃描向量平行的退化邊
      （rectilinear 約少一半 pieces）→ **9000-poly grow 4007ms → 2565ms（−36%）、symdiff=0 等價**
- [x] 驗證：上述 cache 測試含 morph 等價；既有 63 項 boolean 測試全綠

### M9: batch 跨 POI-spec 快取共用  [status: done 2026-06-11]

- [x] `poi_polys_for_roi` / `poi_polys_and_geometry_for_roi` 加
      `walk_memo`/`raw_geom_memo`/`eval_cache` kwargs
- [x] `_fine_align_image` 對「同一張影像的所有 POI spec」共用這三個快取（同一 ROI）
      → 共用 raw layer 每張只 walk 一次、共用子式只算一次
- [x] 驗證：3 spec 共用 layer 17 → walk 3 次降為 1 次、結果等價
      （`tests/test_gds_boolean_cache.py::TestFineAlignCrossSpecShare`）
- [ ] 待 user 回傳新 .txt 確認 batch POI(walk+bool) 降幅

### M6（候選 · 未做）: matchTemplate 金字塔  [status: planned · 若 match 主導]

- 實機數據顯示 batch 是 **poi(walk+bool) 主導**、match 微不足道（0.3ms/img），故**暫不需要**。
- 若日後大檔 + 大影像使 match 變重再做：coarse-to-fine 粗搜 + 原解析度精修。

### M7（候選 · 未做）: 無 S_BOUNDING_BOX 檔 per-cell bbox sidecar（[F17]）  [status: planned]

- 實機證實首次 ROI walk 因 sbbox=0 解碼整檔 12.4s；大檔 + 每個 batch worker 更痛。
- 一次性 bbox sweep + 磁碟 sidecar，跨 session / worker 重用。user 本輪未選，列候選。

### M8（候選 · 未做）: regular-grid analytic sub-grid clip 補強  [status: planned]

- 待數據顯示 `instances_materialized ≫ visited` 再做。

---

## Affected Files

- `tools/bench/bench_roiwalk_finealign.py`、`tools/bench/_make_sample_oasis.py`（新增，M0）
- `glas/core/perfmon.py`（新增，M1：Qt-free 監測核心；M3 加 batch:* OP_LABELS）
- `glas/app/perf_panel.py`（新增，M1：HUD 面板）
- `glas/app/gds_align_tool.py`（M1 掛 dock + 五處插樁；M3 stage_timing；M4 共用 cache）
- `glas/core/gds_boolean.py`（M4 node_cache/_eval_cache；M5 向量化 `_dilate_axis`）
- `glas/core/fine_align.py`（M3 `pool_collect_timing`；M4 expression POI per-image cache）
- `tests/test_perfmon.py`、`tests/test_perf_panel.py`、`tests/test_gds_boolean_cache.py`（新增）
- 後續候選：`tests/test_accel_equivalence.py`（若做 M6 金字塔）、`oasis_random.py`（M7/M8）

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
