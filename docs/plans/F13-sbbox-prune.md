# F13 — S_BOUNDING_BOX 剪枝（KLayout per-cell bbox → walk_roi 秒級）

> 動機：KLayout strict「Save As → OASIS」可把 `Standard properties = Global + per
> cell bounding box` 勾起，為**每顆 cell** 寫一筆 `S_BOUNDING_BOX` 標準屬性（cell 含
> 所有 placement 展開後的整體 bbox，cell-local grid frame）。GLAS 若能讀它，
> `reachable_bbox(cid)` 可**直接回傳**該值——免 CE 邊界層、免遞迴、免全 decode，把
> 無 CE 層大檔（如 `R8_OD_to_VC_NEW.oas`，1.84 GB / 27425 cells）的首次 ROI walk
> 從 10min+ 變秒級。延續 SESSION_LOG 2026-05-29 [B] 診斷的「有 → 實作剪枝」分支。

## 背景事實（已由探索確認）

- `S_CELL_OFFSET` attach 在 **CELLNAME table 的 PROPERTY**（name-table 區，非 CELL
  body）。`scan_cell_offsets` 兩條路徑（header `offset_flag==0` / tail
  `offset_flag==1`）都已在該 PROPERTY 迴圈讀它。`S_BOUNDING_BOX` 照 SEMI P39 並列
  在同處 → **同一迴圈順手收**，不需第二次掃檔。
- `_read_property` + `_read_prop_value` 已支援多 integer value（type 8 unsigned /
  9 signed），`capture_prop_values=True` 下 `payload["values"]` 即 5 個值。
- 剪枝瓶頸：`reachable_bbox(cid)`（oasis_random.py）→ `load_cell_bbox(cid)` 無 CE
  層時 fallback `load_cell`（全 decode）→ 遞迴展開整棵樹 ≈ 全 chip 解碼。

## 格式（已確認，不再是假設）

`S_BOUNDING_BOX` = 5 operand `[flag, left, bottom, width, height]`，cell-local
grid units → bbox = `(x, y, x+w, y+h)`。雙重確認：
1. **KLayout 源碼** `dbOASISWriter.cc`：依序 push `flag`(0x0 或 0x2)、`bbox.left`、
   `bbox.bottom`、`bbox.width`、`bbox.height`。
2. **真檔**（`R8_OD_to_VC_KKKK.oas`，unit=2000 → 0.5 nm/grid，292,883 cells **全部**
   有 S_BOUNDING_BOX）：root `iMerge_Top` raw=`[0,0,0,7460112,2204400]` →
   nm `(0,0,3730056,1102200)` = 3.73mm × 1.10mm，合理 die 尺寸。
- `flag` 非零（KLayout 用 0x2）= 退化/依賴 external cell 的無效 box →
  `std_bbox` 回 None → 該 cell fallback CE/full-decode（不漏幾何）。

## Milestones

- [x] **M1 收集 + 診斷（純加法，零剪枝改動）**
  - [x] `oasis_streamer.scan_cell_offsets` 兩路徑：PROPERTY 迴圈加收
    `S_BOUNDING_BOX`，回傳 `bbox_by_refnum` / `bbox_by_name`（raw 5-int list）。
  - [x] `RandomAccessReader` 載入該 map（`self._sbbox_by_*`）+ accessor
    `std_bbox(cell_id)`（用上述**假設**格式 → grid bbox 或 None）。
  - [x] app `_on_open_roi`：開檔印 map 大小；Pick root 後印 root 的 raw values +
    換算 bbox(nm)，供 user 對照 KLayout 該 cell bbox 確認格式。
  - [x] **user 驗收**：root `iMerge_Top` raw=`[0,0,0,7460112,2204400]` 回報，格式確認。
- [x] **M2 接上剪枝（格式已確認）**
  - [x] `std_bbox` flag 判斷改 `!= 0`（KLayout 用 0x2 標記退化 box）。
  - [x] `reachable_bbox`（walk_roi closure）/ `_reachable_bbox`（method）：cid 命中
    `std_bbox` 時**直接回傳並 memo**，不 `load_cell_bbox`、不遞迴。未命中 → 既有
    CE / full-decode fallback（不退化）。
  - [x] DEBUG cross-check：walk 實際 full-decode 的 cell 驗 `std_bbox(cid) ⊇
    own-geometry bbox`，違反 → `SBBOX-VIOLATION` 警告 + `sbbox_violations` 計數；
    `sbbox_used` 計命中數。
- [x] **M3 測試**
  - [x] `tests/test_oasis_random.py::TestStdBboxPrune`：每 cell 雙 property
    (S_CELL_OFFSET + S_BOUNDING_BOX) 合成檔，驗 map 收集 + `std_bbox` 換算、
    `reachable_bbox` 短路（std_bbox 故意放大→回傳原值證明免遞迴）、flag!=0 fallback
    回幾何值、無 property 時 `has_std_bboxes()` False。（沙箱無 numpy → 待 user 本地跑）
- [x] **M3.5 真檔瓶頸：chip 級 repetition 陣列全展開**（M2 短路雖生效，但 walk 展開
  橫跨整顆 chip 的 type1/2/3 grid 時 materialize 全部 K 個 instance → 卡死）
  - [x] `_candidate_offsets`/`_axis_index_range`：可分離軸對齊 grid + 無旋轉 transform
    下解析裁剪到 ROI 附近子網格（保守超集，下游精確 mask 把關）；其他情況 fallback 全展開。
  - [x] DEBUG 安全網 `CLAMP-MISMATCH` 比對 + `BIG-ARRAY` 計數；20 萬筆隨機暴力驗證超集。
  - [x] `TestRepetitionClamp`（grid 裁剪==完整選集、旋轉/type9 fallback）。
- [ ] **M4 真檔驗收**：`R8_OD_to_VC_KKKK.oas` GUI 開 ROI → walk 秒級 + 幾何正確
  （`--debug` 看 `sbbox_used` 大、`sbbox_violations=0`、`CLAMP-MISMATCH` 不出現）。

## 不變式 / 風險

- 命中才短路、未命中走既有路徑 → **不退化**（§7）。
- 格式偏小 → 漏幾何（危險）；M1 真檔確認 + M2 DEBUG `⊇` cross-check 雙重防護。
- offset_flag 0/1 兩路徑都要收（KLayout strict 是 1）。
</content>
</invoke>
