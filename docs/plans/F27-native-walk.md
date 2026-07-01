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

### M1：native `apply_to_rects_d4` + `roi_overlap_mask`  [status: 待核准]

> profile 的最大純數值熱點（~40%）。小陣列（多為 (1,4)/(K,4)）被呼叫 6 萬次/walk，成本是 numpy dispatch
> overhead，不是計算——native C loop 直接消。

- [ ] `oasis_fastdecode`：`transform_rects_d4(rects(N,4), m00,m01,m10,m11, tx,ty) -> (N,4)`（2-corner、floor/ceil、
      D4 精確）+ `roi_overlap_mask(boxes(N,4), roi) -> uint8(N)`。純 memoryview + C loop，無 numpy dispatch。
- [ ] `oasis_walker.Transform.apply_to_rects` / `oasis_random._roi_overlap_mask` gated 取用（native 可用走 C，否則
      現有 numpy）。VERSION bump。
- [ ] 護欄：新 `test_native_walk`：native vs numpy 對隨機 D4 M + rects byte-identical；全 `test_oasis_*` 雙路徑綠。
- [ ] **量測**：合成寬重複樹 walk_roi native vs pure；目標消掉那 40% → walk ~1.5–1.7×。CI 出 v5 `.pyd`/`.b64`，
      user 下載重量 export（`[export-timing]` 的 walk 應同比例降）。
- [ ] 決策點：達標 → M2；未達（Python 遞迴框架/其他主導）→ 重估 M3 或改批次化。

### M2：native per-cell inner（rect emit + placement prune 數值段）  [status: planned]

- [ ] 把 1589–1618（rect emit）+ 1707–1771（placement corner-transform / clip / mask / composed-M/t）的數值段
      整併成一個 native call（輸入攤平 arrays，輸出 emit rects + 要遞迴的 (child, M, t) 清單）；遞迴框架留 Python。
- [ ] `_clip_grid_offsets` 的 regular-grid analytic clip 也在 C（arbitrary-list fallback 留 Python）。
- [ ] gated + byte-identical；量測後定 M3。

### M3：native 子樹 walk（攤平 cell graph）  [status: planned / 高風險]

- [ ] 把 memo 好的 cell graph（per-cell rect arrays + placement matrices + reachable bbox）攤平成 C 結構，整個
      ROI 子樹遍歷在一次 native call（消 walk 遞迴的 Python overhead 2.7s）。最大收益、最高風險（要 port 剪枝
      不變式、repetition、cycle 偵測，且與 §7「CE 邊界 early-stop / reachable_bbox」不變式對齊）。
- [ ] 全 `test_oasis_*` 雙路徑綠 + 真檔 byte-identical 抽樣。

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
