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
- [ ] **驗證（待 user）**：真機 `GLAS_FA_TIMING=1 GLAS_FA_TIMING_EVERY=1` 跑 5 張，貼 `[export-timing]`，得 decode vs 遞迴 拆分
  → **據此定 M4 主攻方向**（decode 大則 M4 併 leaf 快取；遞迴大則 M4 專攻 per-instance vectorize）。

### M2: giant rect 空間索引 + ROI-independent transform 快取（L2）  [status: planned]

- [ ] 確認 giant 的 rect transform 後 root-coord bbox 只依賴 `(cell, T)`（T 每張固定）→ 在 `walk_roi` 的 giant rect emit
  路徑（`oasis_random.py:2256-2285`）把 `T.apply_to_rects(ext_bb)` 結果**快取於 per-worker（keyed on cell+T）**，每張只重跑 ROI mask。
- [ ] 在快取的 root-coord bbox 上建**空間索引**（sort-by-x + `searchsorted`，或 coarse grid bucket）——ROI-independent、per-worker
  建一次；per-ROI 只查桶取候選 rect，取代 O(10.8M) 全掃。
- [ ] 記憶體護欄：索引 ~O(N) int（sort：86MB / grid：更小），per-worker、**不共享、不進 sidecar**（避免 F29 RAM 雷）；量測確認總量不逼近 free RAM。
- [ ] **byte-identical 護欄**：新 emit 的 rect SET 與現行 `walk_roi` 逐一相同（order-independent，rasterize 不在意順序）；
  加 `tests/test_walk_giant_index.py`。
- [ ] 驗證：`pytest` 綠 + 單機 micro-bench（giant rect emit 536ms → 目標 <100ms）；真機一張 `[export-timing]` 的 `rect` 下降。

### M3: 暖機 footprint 降低（L3，非 F29 共享）  [status: planned]

- [ ] 先量測（M1 數據）確認暖機 60-137s 的組成：giant sidecar np.load ×8 併發 + 每 worker extent build + 首 FOV leaf 解碼 的佔比。
- [ ] `rect_arrays`（`oasis_random.py:371-401`）確認 flat giant 是否 `ext == base`（`rt` 全 -1、無 repetition）——若成立且尚未 alias，
  改 `ext=base`（省一份 345MB/layer float64 + build 時間）；若已 alias 則跳過此步（agent 提示 docstring 稱「most rects extent==base」，需實測）。
- [ ] 評估：M2 的空間索引是否可**取代** per-image transient 的 transformed-bbox 陣列 → 進一步降單 worker giant footprint → 減 8-worker 冷啟併發記憶體壓力（純降用量、不跨 worker 共享）。
- [ ] 驗證：真機暖機期總時間下降、`free_ram` 低點抬升、無新 thrash（HUD worker 列不大片標紅）。

### M4: walk 遞迴主體重構（L4）— 消 2666ms per-instance 遞迴  [status: planned]

- [ ] （前置）依 M1 數據定位：2666ms 中 decode 佔多少、per-instance 遞迴佔多少。
- [ ] 讓 `walk_roi_batched` 對「sbbox + 無 CE」型**可負擔**：目前 `_batched_walk_affordable`（`oasis_random.py:2678`）因 sbbox 直接
  回 False（topo build 的 `load_cell_bbox` 會 full-decode）。改用 **sbbox（decode-free）建 topo / instance 集**，繞開 full-decode，
  讓 batched 的「每 cell 一次、vectorize over instances」取代 3340 次 per-instance `walk()`。
- [ ] （若 decode 也大）併入 leaf 快取策略（L5）：ROI-touched leaf 打包單一 archive 或降 `min_records`，避免每 worker 首觸重解。
- [ ] **byte-identical 護欄（最關鍵）**：batched 路徑的 rect+poly SET 與 `walk_roi` 逐一相同（既有 `walk_roi_batched` 已宣稱
  byte-identical，需對 LTV sbbox 型補測）；保留 `walk_roi` 為 fallback，只有護欄綠才切路。
- [ ] Open question（Q2）：若純 Python decode-free batched 不可行 → 與 user 確認走 native descent（.pyx，需 CI）或止步 M2/M3。
- [ ] 驗證：`pytest` 綠（含新 byte-identical 測）+ 真機 LTV per-image walk 主體大幅下降、總時間對照。

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
