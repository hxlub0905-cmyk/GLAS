# [F27] Native ROI-walk 熱路徑加速

> **狀態：** in progress（M0 ✓ down payment / M1 待核准）
> **§8 ID：** [F27]
> **建立：** 2026-07-01
> **負責 branch：** claude/project-perf-optimization-86i8yt
> **前置：** F26（native ext 交付管道 `oasis_fastdecode` + build/unpack 已驗證）

---

## Goal & Context

user 真實痛點＝整包 KLARF（~190 顆 defect）批次 export，實測 ~12–15 min。`[export-timing]` +
`[walk: place/rect/poly]` 定案：

- **瓶頸＝ROI walk 遍歷**，非解碼（後續顆 `cells_decoded≈0`、`reach_new=0`）、非對位（`match<0.3s`）、非
  rasterize（`raster<1s`）。
- 每顆 walk `cellvisits=1.7萬~5.4萬`（≈`instances`）——E3B 是 CMP D2DB：**少數 unique device cell、天量重複
  placement**，walk 逐 instance 遞迴、重複轉換同一份幾何。
- walk 30s 內部：`place`（剪枝）+`rect`+`poly` 僅 ~20%，**殘差 80% ＝遞迴下降的 per-instance 數值運算**。
- worker 數守恆：8→4 每顆快 2× 但並行度砍半，總時間反而略增（8 worker throughput 最好，別下調）。

**cProfile（合成 2 萬 instance 寬重複樹，warm）：** `apply_to_rects`（2.65s tottime）+ `_roi_overlap_mask`
（1.46s）＝**~40%**，且純數值（無 Python 物件）；`walk` 遞迴框架本身 2.7s（碰 CellContent/memo，難 native）。

**策略：** 分階段把 walk 的純數值熱點搬進 native（`oasis_fastdecode`，F26 管道），遞迴框架先留 Python；每階段
**gated + 與純 Python byte-identical**（walk_roi 的 rects/polys 輸出逐位比對），先量再進下一步。

---

## Milestones

### M0：apply_to_rects 2-corner（D4）—— 零風險 down payment  [status: done 2026-07-01]

- [x] walk 的 Transform 全是 D4（`from_placement` 拒非 quarter-turn），rect bbox 由 2 對角點即精確 → 4-corner
      改 2-corner，省一半 corner build + matmul + reduce。純 numpy、無 native。
- [x] 合成寬重複樹：2440ms → 2228ms（**~9%**）；`test_oasis_walker`（0/90/180/270+flip+mag+composed）+
      `test_oasis_random` 全綠（byte-identical）。

### M1：native `apply_to_rects_d4` + `roi_overlap_mask`  [status: done 2026-07-01（合成 2.29×）]

> profile 的最大純數值熱點（~40%）。小陣列（多為 (1,4)/(K,4)）被呼叫 6 萬次/walk，成本是 numpy dispatch
> overhead，不是計算——native C loop 直接消。

- [x] `oasis_fastdecode`（VERSION 4→5）：`transform_rects_d4(rects(N,4), m00,m01,m10,m11, tx,ty) -> (N,4)`（2-corner、
      floor/ceil、D4 精確）+ `roi_overlap_mask(boxes(N,4), r0,r1,r2,r3) -> bool(N)`。純 memoryview + C loop、
      `libc.math` floor/ceil、無 numpy dispatch。selftest 加 90° smoke。
- [x] `oasis_walker.Transform.apply_to_rects`（`_FASTW`，VERSION≥5）/ `oasis_random._roi_overlap_mask`（`_FASTW`）
      gated 取用（native 且 float64 C-contig 才走 C，否則現有 numpy）。
- [x] 護欄：新 `test_native_walk`（transform/mask 對全 D4×flip×mag byte-identical + walk_roi native-on vs off 全 ROI
      與 tight ROI 逐位相同）；全 `test_oasis_*`+cellcache+export_fused 147 passed（native ON）。
- [x] **量測**：合成 2 萬 instance 寬重複樹 walk_roi：pure 2729ms → native 1190ms = **2.29×**（優於預期 1.5–1.7×，
      因 native 連小陣列 dispatch 一起消）。walk 是 export ~90% → 端到端 ~2×（12min→~6–7min）待 user 真檔驗。
- [ ] CI 出 v5 `.pyd`/`.b64` → user 下載重量一次 export（`[export-timing]` 的 walk 應同比例降）。
- [ ] 決策點：真檔達標 → 收工或評估 M2/M3；真檔遞迴框架主導 → M3。

### M2：native per-cell inner（rect emit + placement prune 數值段）  [status: planned]

- [ ] 把 1589–1618（rect emit）+ 1707–1771（placement corner-transform / clip / mask / composed-M/t）的數值段
      整併成一個 native call（輸入攤平 arrays，輸出 emit rects + 要遞迴的 (child, M, t) 清單）；遞迴框架留 Python。
- [ ] `_clip_grid_offsets` 的 regular-grid analytic clip 也在 C（arbitrary-list fallback 留 Python）。
- [ ] gated + byte-identical；量測後定 M3。

### M3：native 子樹 walk（攤平 cell graph）  [status: in progress — user 核准 2026-07-01]

> **真檔 M1 結果驅動：** M1 真檔 walk 30s→22s（1.35×，非合成 2.29×），因真檔殘差 **94%**＝walk 遞迴框架本身
> （每顆 2 萬次遞迴：`walk()` 呼叫 + `_clip_grid_offsets` + `composed_M/t` + `load_cell` memo + `Transform` 建構），
> native M1 只碰得到純數值熱點。要砍 94% 必須把整個遞迴下降搬 C。目標 walk ~3× → total 12min→~5min。
>
> **策略：攤平 + C stack-walk + Python fallback 邊界。** memo 好的 cell graph（ROI-independent）攤平成 CSR C 陣列
> （一次性 per reader），native 用 explicit stack 做 DFS；native 只吃「能吃的」情形，碰到不支援的（poly、arbitrary
> repetition、非 D4、name-ref target）就把該子樹交回 Python walk（byte-identical 由 fallback 保證）。每階段先量再進。

- [x] **M3a：可行性 spike（先量天花板）** ✓ 2026-07-01：`oasis_fastdecode.walk_rects_native(rect_coords/off,
      pl_target/M/t/off, reach_bbox, root, roi, max_depth)` — C explicit-stack DFS（rect emit：2-corner transform +
      floor/ceil + exact roi mask；placement prune：compose + child reach-bbox transform + mask；depth-bound）。合成
      `_build_hierarchy`（2 萬 no-rep instance、single leaf rect）：**rect set byte-identical**（native 20000 == python
      20000），**walk_roi 1225ms → native kernel 1ms = 1378×**（排除一次性 flatten）。**決策：≥5× 大幅通過 → 完整 M3
      GO。** 端到端會低不少（flatten 分攤 + 真檔 rep/poly 部分 fallback + 前波 reachable_bbox sweep），但遠勝 M1。
- [x] **M3b：接進 export 路徑（gated）** ✓ 2026-07-01（合成端到端 95.6×）：`flatten_cell_graph`（DFS 收集 +
      native-able 偵測：poly/rect-rep/placement-rep/非D4/name-ref → None）+ `_flatten_cached`（memo，ROI-independent，
      跨 defect/worker 共享）+ `walk_roi_fast`（native-able 走 `walk_rects_native`、否則 fall through 到純 Python
      `walk_roi`）。**關鍵：native gate 放 `walk_roi_fast`（fine_align._walk_roi_polys 改呼叫它），`walk_roi` 本身
      維持純 Python 不動** → 所有 walk_roi stats/prep-cache 測試不受影響。VERSION 5→6（`_FASTWALK` gate）。護欄：
      `test_native_walk` +5（walk_roi_fast native vs Python：full/tight/empty ROI byte-identical rect set + native-able
      True/False 偵測 + rep 檔 fallback 展開正確）；全 `tests/` **810 passed**（native ON）+ native-absent fallback 綠。
      **量測**：合成 2 萬 instance 樹、50 次 walk 共享一 reader（含首次 flatten）：python 61431ms → native 643ms =
      **95.6×**（12.9 ms/walk）。真檔端到端待 CI v6 + user 量（rep/poly 顆走 Python fallback，涵蓋率決定實際降幅）。
- [!] **M3b hotfix（2026-07-01）：flatten 規模上限**。真檔 E3B（13276 cells）攤平整棵 graph 幾何 → 每 worker 數十秒
      whole-chip decode，開跑前卡住無輸出。修：`flatten_cell_graph` 免 decode 預檢 `len(rar._by_refnum) > 4000` → 回
      None（Python fallback）+ iterative DFS + native-able 短路。**現況：大檔走 Python（M1），小檔 native。**
- [x] **M3c（大檔 native 的真正解）：shared / persisted flatten** ✓ 2026-07-01：`walkflatten_cache`（sidecar
      .npz，keyed on file mtime+size + root + layer，共用 cellcache dir，atomic write / mtime 驗證 / 毀損當 miss / 從不
      raise；`NOT_NATIVE` sentinel 持久化「非 native-able」判定）。`flatten_prewarm(rar, root, layer, dt)`：無視互動
      cap（`max_cells=200000` OOM guard）build 全 chip flatten + 存 sidecar。`_flatten_cached`：in-process memo →
      sidecar load → cap-limited build（over-cap **不存**，避免 poison prewarm）。**app（`ExportWorker._run_process_pool`）
      在啟動 pool 前、於 orchestrator 主進程對每個 raw POI layer `flatten_prewarm` 一次**（`[export] prewarming…` log），
      pool worker 各 `np.load` sidecar（OS page cache 共享一份）而非各自 decode 全 chip → 免 race、免卡。護欄：
      `test_native_walk` +1（over-cap 先 Python → prewarm 持久化 → fresh reader sidecar hit 走 native byte-identical）；
      全 `tests/` 812 passed（native ON）。**端到端**：prewarm decode 全 chip 一次（~15-30s）分攤到整批 + 每顆 native
      walk <1ms；真檔降幅待 user 量（poly 層仍 Python fallback）。
- [ ] **M3d：擴充 repetition（regular grid analytic clip 在 C）+ 多 wanted layer**；arbitrary-list rep 仍 Python。
- [ ] **M3d：擴充 POLYGON**（point-list transform + emit 在 C）。arbitrary repetition / 非 D4 / name-ref 永遠 fallback。
- [ ] 每階段 byte-identical（native-on vs off 逐位）+ 真檔抽樣；§7「reachable_bbox 用 load_cell_bbox、walk 用
      load_cell」不變式：攤平只用 memo 好的結果，不改剪枝語意。

---

## 護欄與風險

- §7 不變式：native 不得改 walk 的剪枝語意（reachable_bbox 用 load_cell_bbox、walk 用 load_cell）；byte-identical
  是硬約束。
- 交付：沿用 F26 的 CI build + base64 sidecar + `tools/unpack_fastdecode.py`（user 無編譯器/無 git）。
- 每階段先量再進；M1 若 Amdahl 卡在 Python 遞迴框架，M3 才是解，但風險最高——到那步再決定投入。

---

## Affected Files

- `glas/core/oasis_walker.py`（M0 done；M1 gated apply_to_rects）
- `glas/core/oasis_random.py`（M1 gated `_roi_overlap_mask`；M2/M3 walk inner）
- `glas/core/oasis_fastdecode.pyx`（M1+ native helpers）、`tests/test_native_walk.py`（新）
- `docs/plans/F27-native-walk.md`、`SESSION_LOG.md`、`CLAUDE.md`（§8）
