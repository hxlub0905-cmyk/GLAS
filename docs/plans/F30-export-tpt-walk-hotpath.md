# [F30] LTV export TPT 優化：walk 熱路徑（giant 空間索引 + 遞迴重構）

> **狀態：** planned（待 user 核准開工）
> **§8 ID：** [F30]
> **建立：** 2026-07-07
> **負責 branch：** claude/code-review-handoff-65xwf4

---

## Goal & Context

**觀察（F29 撤案後真機 LTV + Explore agent 熱路徑分析）：** LTV（1750MB、44997 cells、有 S_BOUNDING_BOX、一顆
10.8M-rect flat merge cell `_2_gri_yank_top`）export 在 8 workers 下 **196 張 ~7 分鐘、不卡**。瓶頸不是記憶體（F29 已證實
撤掉 SHM 反而更順），而是 **walk 熱路徑本身**。

**熱路徑事實（agent 確認，修正先前臆測）：** 這型檔（大 + sbbox + 無 CE + giant）走的是
`oasis_random.walk_roi`（遞迴 + sbbox 剪枝），**不是** `walk_roi_batched`（被 `_batched_walk_affordable` 的 sbbox gate
擋掉，`oasis_random.py:2697`）。暖機後一張 ~5s 的分解：

| 區塊 | 時間 | 性質 |
|---|---|---|
| 遞迴 + 解碼（黑盒）| **~2666ms** | 3340 個 FOV 內 repetition instance 各一次 `walk()` + 解 ~210 個 leaf cell。**最大塊、decode vs 遞迴 未拆分** |
| raster（3 PNG）| 680ms | |
| boolean union | 657ms | |
| **giant rect emit** | **536ms** | `T.apply_to_rects(ext_bb)` 對 10.8M bbox transform + ROI mask，**每張重算** |
| placement emit | 209ms | |
| matchTemplate / read | ~250ms | |

**目標：** 把暖機後 per-image 從 ~5s 壓到 ~2-3s（walk 主體 vectorize + giant 幾何空間索引），並降暖機期每 worker 冷啟
記憶體 footprint（不重蹈 F29——**不共享、只降單 worker 用量**）。成功 = 真機 LTV 8-worker 總時間明顯下降、byte-identical、
無新 thrash。

**兩個關鍵洞察（agent）：**
1. 那 2666ms 黑盒（decode vs 遞迴）只差**一行診斷**（`stats.t_decode` 已在量、只是沒印進 `[export-timing]`）就能拆開——
   **這決定最大塊該攻 decode 還是遞迴**。→ M1 先做。
2. giant 的 `T.apply_to_rects` 對 10.8M bbox **每張重算，但 T 每張都一樣**（giant 由 root 固定路徑到達）→ transform 後座標
   **與 ROI 無關**，只有最後的 ROI mask 需 per-image。→ M2 快取 + 空間索引。

**與現有關係：** 延伸 walk_roi / walk_roi_batched（不取代，加快取 + 讓 batched 對 sbbox 可負擔）；保留 F27 M7n ramp、
M7j giant 預解、F28 HUD。**與 F29 的分野：** F29 想「跨 worker 共享記憶體」（撤案）；F30 是「減少每張重算 + 降單 worker
footprint」，不碰跨行程共享。

---

## Q&A Decisions

### Q1: TPT 優化節奏？
**選項：** 分階段先穩後險（量測 + giant 安全加速，再依數據攻最大塊）／ 積極（一次把遞迴重構 L4 也排進做完）／ 最小（只做 L2）
**選擇：** **積極**（user 明示）——L1+L2+L3 之外，把 L4 遞迴重構也排進 plan。
**理由：** user 要顯著壓 TPT，願意承擔 L4 的工程量與風險。**但仍在 plan 內分階段**（M1 量測 → M2/M3 安全 → M4 大改），
每 milestone 獨立可驗、真機可回歸，先做安全的建立 byte-identical 護欄再上 L4。

### Q2:（待 M4 開工前確認）L4 若純 Python decode-free batched 不可行，走 native（.pyx，需 CI）還是止步於 L2/L3？
**現況：** 保留為 open question，M4 kickoff 時依 M1 數據 + 可行性再決定，不預先綁死。

---

## Milestones

> 分階段：M1 量測（極快、決定主戰場）→ M2/M3 安全自足加速（建立 walk-level byte-identical 護欄）→ M4 遞迴重構（大、險）
> → M5 整體真機驗收。每個 milestone 可獨立 ship + 真機回歸。

### M1: 量測 — 拆開 2666ms 黑盒（L1）  [status: code done · 待 user 真機數據]

- [x] `overlay_export.align_and_export_one_image` 的 `[export-timing]` 加印 `decode`（reader 新增 `_walk_tdecode_total`
  累加 `stats.t_decode`；overlay_export 快照差量後印於 `[walk: … decode=…]`）——把 decode 從「遞迴+decode」黑盒分離。
- [x] 純 Python、不改運算邏輯、不需 CI；`walk − (place+rect+poly+decode) ≈ 純 per-instance 遞迴`。59 tests 綠、`_walk_tdecode_total` 流通已驗。
- [x] **驗證（user 真機，2026-07-07）**：拿到 `decode=` 拆分。**結論：遞迴大、decode 小。**
  - **暖機後穩定張**（batch 主體）：`img 1477` total=6430 walk=5358 → **decode=231、rect=617、place=257 → 遞迴（walk−place−rect−poly−decode）≈4253ms**；
    `img 1512` total=8330 walk=7127 → decode=545、rect=1051、place=714 → **遞迴≈4817ms**。decode 僅 ~5-8%。
  - **冷第一張/worker**：`img 1469/1467/1480` total~68s、**decode=23.9-25.6s**（giant extent 冷建 + ~470 cell 冷解）—— 這是 warmup（M3），非穩定張。
  - **定案：穩定張 = per-instance 遞迴主導（~4.2-4.8s）→ 走 L4；decode 小 → L5 leaf 快取剔除。** 真正冗餘：leaf 被放 K 次 → K 次
    `walk()` 各重 emit 同一 leaf 幾何（`mat instances≈197k`）。

### M2: giant rect emit（L2）  [status: transform-cache 試作後撤回 · 降級/重評]

**試作 + 撤回（2026-07-07）：** 先做了「transform 快取」版（`CellContent._trect_cache` + `rect_ext_transformed`，把
`T.apply_to_rects(ext_bb)` 結果 memo 於 cell+T），byte-identical 護欄綠（`test_walk_giant_index.py`）。但**撤回**，兩個真相：
1. **記憶體雷（F29 教訓重演）：** 快取的 tb = 10.8M×4×8B = **346MB/worker × 8 = +2.8GB 常駐**。真機 free_ram 已到 9.9GB
   （giant per-worker footprint 已用 ~6.4-8.8GB）→ +2.8GB 極可能重新 paging，正是剛撤掉 F29 的失敗。**違反 F30 自訂「降 footprint 不增」原則。**
2. **收益比想像小且更糊：** 這裡 env 無 native `.pyd`，bench（719ms/img）**灌水**；真機 `apply_to_rects` 是 native（10.8M ~150ms）。
   且 giant 的 survivor loop 只 ~2150 iter（小）；`[export-timing]` 的 `mat: arrays=11413 instances=205849` 顯示 rect=536ms 的
   **大頭其實是 leaf cell 的 repetition 展開**，不是 giant transform。→ 攻 giant transform 的性價比低。

**重評方向（不急、低優先）：**
- 記憶體中性的小改：flat giant 無 repetition 時 `tb[keep]` 即輸出，省掉 survivor loop + 第二次 transform（~10-30ms，byte-identical，零記憶體）——但收益太小，暫不做。
- 真正要吃 10.8M transform+mask（~200ms）得靠**記憶體精簡的空間索引**（grid-on-local ~84MB，不存 tb），但要解 long-thin rect 的多格 span（複雜），且只佔 rect 的一部分。
- **結論：M2 降為低優先**；rect=536ms 的大頭（leaf repetition + native transform）性價比不如 M4 的 2666ms。**先看 M1 真機數據再決定要不要回頭做 M2。**

### M3: 暖機 footprint 降低（L3，非 F29 共享）  [status: planned]

- [ ] 先量測（M1 數據）確認暖機 60-137s 的組成：giant sidecar np.load ×8 併發 + 每 worker extent build + 首 FOV leaf 解碼 的佔比。
- [ ] `rect_arrays`（`oasis_random.py:371-401`）確認 flat giant 是否 `ext == base`（`rt` 全 -1、無 repetition）——若成立且尚未 alias，
  改 `ext=base`（省一份 345MB/layer float64 + build 時間）；若已 alias 則跳過此步（agent 提示 docstring 稱「most rects extent==base」，需實測）。
- [ ] 評估：M2 的空間索引是否可**取代** per-image transient 的 transformed-bbox 陣列 → 進一步降單 worker giant footprint → 減 8-worker 冷啟併發記憶體壓力（純降用量、不跨 worker 共享）。
- [ ] 驗證：真機暖機期總時間下降、`free_ram` 低點抬升、無新 thrash（HUD worker 列不大片標紅）。

### M4: walk 遞迴主體重構（L4）— 消 ~4.2-4.8s per-instance 遞迴  [status: 第一刀「contained leaf 快取路」已 ship（護欄綠）· 待真機驗收 · 跨 parent DAG 為硬問題留 (ii)]

**已 ship（2026-07-08）— contained leaf fast-path（`_WALK_LEAF_BATCH`）：** 在 `walk_roi` 的 placement descent（:2491）
把「target 是純幾何 leaf（`placement_count()==0`）且 **surviving instance > 1**（`len(sel) > 1`）」的 `for k in sel: walk(...)`
K× 遞迴，換成**一次** `_emit_leaf_segment`（:2681）——plain-rects/no-poly 走 vectorized `_emit_plain_rects_seg`、有 repeated
rect / polygon 則落回逐 instance `_emit_cell_geom`。這正打 LTV 的 dense repetition array（`mat maxk=6938`：一個 placement 6938
instance 從 6938 次 `walk()` → 1 次 vectorized emit）。**不碰 topo/DAG**，故繞開下方 (ii) 的硬問題。
- **byte-identical 護欄**（`test_walk_leaf_batch.py`，5 tests）：`_WALK_LEAF_BATCH` ON vs OFF 在 dense array / 多 distinct
  placement / sbbox prune / repeated-rect 落回 / size-cap 落回全 byte-identical。全套 852 綠。
- **F29 記憶體雷雙重防護**：(1) `len(sel) > 1` gate —— giant flat cell 只被放一次（`sel==1`）→ 永不進快取路 → 不 cache coords；
  (2) `_LEAF_PLAIN_MAX_RECTS = 1_000_000` cap —— `rect_plain_coords` 對超過的 cell **在 materialize float64 前**回 None（防某檔把
  百萬-rect flat cell 當 repetition array 放）→ 落回逐 instance emit、零額外快取。**giant 的 345MB coords copy（F29 的錯）不會發生。**
- **範疇界定：** 這只 collapse「單一 placement 的 repetition array」（K 個同 pitch instance）。「leaf 被 K 個 **distinct** placement
  放」（cross-parent 冗餘，`mat instances≈205849` 的另一半）仍是下方 (ii) 的 DAG 問題，本刀刻意不碰。→ **部分 M4 win，待真機量 walk 降幅。**

**先前 de-risk（2026-07-07）：**
- ✅ **sbbox byte-identity 護欄已建**（`test_walk_batched.py::test_batched_matches_walk_roi_sbbox_hierarchy`）：leaf 放 20×20 grid + sbbox
  設真實 extent，`walk_roi` vs `walk_roi_batched` 在 all/tight/strip/empty ROI 全 byte-identical（含 sbbox prune）。**→ batched 機制在 sbbox 資料上正確性已證，M4 只剩「可負擔」問題，非正確性。**
- 🔍 **讀 `walk_roi_batched` 處理迴圈（:2791-2885）發現：** 它的 emit/傳遞**本身已 ROI-pruned**（`xf` dict 只有被 ROI-reaching segment 打到的 cell 才處理，其餘 `continue`）。**唯一貴的是 whole-graph `order`（topo）build**（:2761-2790）—— 它 `load_cell_bbox` 解**每一個** reachable cell（LTV 無 CE → full-decode，giant 10.8M 還不吃 sidecar），且 `load_cell` memo **無 eviction → 全 44996 cell 幾何常駐**（記憶體）。

**「可負擔 topo」的兩條路（核心設計岔路）：**
- **(i) whole-graph topo + sidecar-aware**：讓 topo build 對大 cell 走 `load_cell`（giant 走 sidecar 便宜）。**但仍解+持有全 44996 cell** →
  decode 時間 + 記憶體雙重 regression（recursive walk 整批只碰 ~5000 unique cell；whole-graph 解 44996 = 4-9× 更多 + 全持有）→ **F29 鄰域記憶體雷**。**否決**（違反 F30 原則）。
- **(ii) ROI-pruned lazy batched（正解、但硬）**：不建 whole-graph topo，用 worklist/chaotic-iteration 只在 ROI-reachable 子圖傳遞 segment、
  最後每 cell emit 一次。避開 decode+記憶體 regression。**但這是真正難的 DAG 演算法**（cell 被多 parent 放置 = DAG，要「所有 parent 先於 child」卻不預建全圖 topo → segment 成長時需重傳遞至 fixpoint）+ 高 byte-identity 風險。多步、需逐段驗。

**現實：M4 核心 (ii) 是多 session 的硬工程。** 已完成的 de-risk（護欄 + 定位「只剩 topo 可負擔性」）是實打實的進度；(ii) 的實作待 user 決定投入。

---
（以下為原 M1 定案 + 可行性分析，保留為脈絡。）

**M1 數據定案（2026-07-07）：穩定張 = 遞迴主導（~4.2-4.8s）、decode 小（~5-8%）→ 選 L4 路徑 (a)「sbbox-pruned 子圖 batch」。**
不是原本那個「whole-graph decode-free batched」（下方證得不可行），而是**只在 walk_roi 已用 sbbox 剪出的 ~210-cell ROI 子圖內**
把「leaf 被放 K 次 → K 次 `walk()` 各重 emit」collapse 成「per-child 一次、vectorize over K 個 translation」（複用
`_emit_plain_rects_seg`）。這**不建 whole-graph topo**（子圖只碰 walk 本來就會 decode 的 cell）→ 繞開下方 Q4 矛盾。
估：穩定張 walk ~5s → ~1.5-2s、per-image ~6s → ~3s、batch ~7min → ~4min。

**可行性結論（Explore agent，2026-07-07）：原 plan 的「decode-free batched via sbbox」（whole-graph topo）對 LTV 定義級不可行。**
- batched 要 topo order（`oasis_random.py:2761-2790`）→ 要 parent→child 邊 = **placements**（:2781）；`sbbox_for`（:943）只給
  一個 bbox tuple、**不含子 cell 邊**。placements 只能靠解碼 record stream 取得。
- LTV 無 CE 層 → `load_cell_bbox` 的早停（:1202）永不觸發 → 對每個 reachable cell **full-decode**（含 giant 的 10.8M
  records，且走獨立 `_bbox_memo`、**不吃 cellcache sidecar**）。OASIS record stream 順序變長 → **無法「只解 placement、跳過幾何」**
  （要到第 N 個 record 必得 parse 前面每一個）。
- 好消息：batched 的**剪枝數學已與 walk_roi 一致**（都走 `reachable_bbox` 的 sbbox-first 分支，:2756/:1119）；擋掉純粹是 topo build 的 decode 成本、非正確性。

**per-instance 遞迴（2666ms）真結構：** `walk()`（:2187）每 FOV instance 進一次；`load_cell` 對 memo 命中**不重解**（:960）——
所以「遞迴」那半 = :2434 的 `for k in sel: walk(...)` 的 K 次函式開銷 + 子 leaf 幾何被放 K 次就重算 K 次（:2256-2309）。
placement gather 已快取（`_place_prep`）。

**替代 L4 路徑（待 M1 數字選路，不預先綁死）：**
- **(a) sbbox-pruned 子圖 batch**：保留 walk_roi 的 sbbox 剪枝（只碰 ~210 cell），把 :2434 的 per-instance K×`walk()` 換成
  per-child segment 累積 + vectorized emit（複用 `_emit_plain_rects_seg` :2579 / `_batch_place_prep` :2610）。**繞開 Q4 矛盾
  （不建 whole-graph topo）**；效益最高、但要把 tree 遞迴重構成 DAG worklist，byte-identical 風險最高。純 Python。
- **(b) placement-aware 輕解碼 + giant 走 sidecar**：讓 topo build 對已快取大 cell 走 `load_cell`（sidecar 命中免重解 giant），
  餵現成 batched。改動集中、prune 已一致 → byte-identical 風險較低；**但「一次性掃 44996 cell」的 warmup 成本是真機未知數（F29 鄰域雷）**。純 Python。
- **(c) native descent（.pyx）**：最高效益（~100×），但需 CI + 破壞 F30「純 Python」現況。
- **(d) 只 vectorize 密集 repetition 那段**（不動 topo）：最安全、但 giant flat 無 repetition → 對 giant 無益，只吃「leaf 被密集重複」。

**硬前提：M1 真機 `decode` vs `walk−(place+rect+poly+decode)`（遞迴）拆分。**
- 遞迴大 → (a)/(d) 對症；
- **decode 大 → 上面全部都不解 decode**，得改攻 **L5 leaf 快取**（見下 M4' / plan 原 L5：`min_records` 或 ROI-touched leaf archive）。
- **沒有這個數字，任何 L4 選路都是盲賭。** → M4 開工前必須先有 M1 數據。

- [ ] byte-identical 護欄缺口（agent 指出）：現有 `test_walk_batched` 的 fixture **只有 S_CELL_OFFSET、無 S_BOUNDING_BOX** →
  不涵蓋 LTV 的 sbbox 路徑；M4 要補一個**帶 sbbox 的合成 fixture**、經 `walk_roi_fast` gate 分流、與 `walk_roi`（golden）逐一比對。
- [ ] 保留 `walk_roi` 為 fallback，只有護欄綠才切路。

### M5: 整體真機驗收 + 收尾  [status: planned]

- [ ] 真機 LTV 8-worker：總時間 before(≈7min) → after 對照；暖機 + 穩定期分別記錄。
- [ ] 全套 `pytest tests/ -q` 綠；byte-identical 護欄全過。
- [ ] `SESSION_LOG.md` 逐 milestone 補條目；`CLAUDE.md` §8 更新 / §5 walk 說明補快取與 batched-sbbox 分支。

---

## Affected Files（預期）

- `glas/core/overlay_export.py`（M1：`[export-timing]` 加 `t_decode`）
- `glas/core/oasis_random.py`（M2：giant transform 快取 + 空間索引於 `walk_roi` rect emit；M3：`rect_arrays` ext=base；
  M4：`_batched_walk_affordable` + `walk_roi_batched` decode-free sbbox 建法）
- `glas/core/oasis_walker.py`（可能：transform / apply_to_rects 介面）
- `glas/core/cellcache.py`（M4 若做 leaf archive）
- `tests/test_walk_giant_index.py`（新，M2 byte-identical + 索引正確性）、`tests/test_oasis_random.py`（M4 batched-sbbox byte-identical）
- 若 M4 走 native → `.pyx`（需 CI 重建 `.pyd`）
- `docs/plans/F30-...md`（本檔）、`SESSION_LOG.md`、`CLAUDE.md`

---

## Risks / Open Questions

- **byte-identical 是硬底線**：L2/L4 產出的 geometry 必須與現行 `walk_roi` 逐一相同（order-independent）。每個 milestone 先立護欄再切路，
  `walk_roi` 永遠保留為 fallback。這是最尖銳的正確性面。
- **L4 decode-free batched 可行性**：sbbox 只給 bbox、不給 placements；batched 的 instance 集建法需繞開 `load_cell_bbox` 的 full-decode。
  是否純 Python 可行是 M4 最大未知（Q2）。若不行，要嘛 native（CI），要嘛止步 M2/M3。
- **記憶體（F30 不重蹈 F29）**：L2 索引 / L3 footprint 一律 **per-worker、不跨行程共享、不進 sidecar 供 8×np.load**；每步量測總量對 free RAM 的比值，
  逼近就縮小索引粒度（grid 桶而非全 sort）。
- **暖機是 8-worker 冷啟併發記憶體壓力（F29 鄰域）**：L3 只做「降單 worker footprint」不做「共享」；預期效益有限但零 F29 風險。
- **診斷先行**：M1 幾乎零成本，**必須先做**——否則 M4 攻錯目標（decode vs 遞迴）。

---

## 驗證方式

- [ ] 所有 milestone checkbox 已勾
- [ ] `pytest tests/ -q` 綠（含新 walk byte-identical 護欄 + `test_export_fused` 仍 byte-identical）
- [ ] **user 真機 LTV 8-worker**：per-image walk 主體 + giant rect 下降、暖機縮短、總時間對照改善、無新 thrash、geometry 產物與改前逐位相同
- [ ] `SESSION_LOG.md` 逐 milestone 補條目

---

## 完成後

- 最終 SESSION_LOG 註記 `完成 [F30]`；§8 對應更新；§5 walk 架構補「giant 空間索引 / batched-sbbox 分支」
- 本檔保留為 design history
