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

## 未定（必須真檔確認，不可猜——§7 漏幾何風險）

`S_BOUNDING_BOX` 的 5 個 value 確切語意。**假設**（SEMI P39 §31）：
`[flag(uint), x(int), y(int), w(uint), h(uint)]` → bbox =
`(x, y, x+w, y+h)`，cell-local grid units。**待 M1 真檔數值驗證**：
- value 個數是否真為 5、型別順序是否如上；
- 座標是否 cell-local grid（與 `reachable_bbox` 同 frame）；
- `flag` 語意（空 cell？）。

## Milestones

- [x] **M1 收集 + 診斷（純加法，零剪枝改動）**
  - [x] `oasis_streamer.scan_cell_offsets` 兩路徑：PROPERTY 迴圈加收
    `S_BOUNDING_BOX`，回傳 `bbox_by_refnum` / `bbox_by_name`（raw 5-int list）。
  - [x] `RandomAccessReader` 載入該 map（`self._sbbox_by_*`）+ accessor
    `std_bbox(cell_id)`（用上述**假設**格式 → grid bbox 或 None）。
  - [x] app `_on_open_roi`：開檔印 map 大小；Pick root 後印 root 的 raw values +
    換算 bbox(nm)，供 user 對照 KLayout 該 cell bbox 確認格式。
  - [ ] **user 驗收**：開檔回報 root 的 `S_BOUNDING_BOX raw=...` 數值。
- [ ] **M2 接上剪枝（格式確認後）**
  - `reachable_bbox` / `_reachable_bbox`：cid 命中 `std_bbox` 時**直接回傳**，
    不 `load_cell_bbox`、不遞迴。未命中 → 既有 CE / full-decode fallback（不退化）。
  - DEBUG cross-check：對 walk 實際 full-decode 的 cell，驗
    `std_bbox(cid) ⊇ own-geometry bbox`，違反則警告（抓格式/語意錯）。
- [ ] **M3 測試**
  - 合成帶 `S_BOUNDING_BOX`（header + tail offset_flag）OASIS：驗 map 收集、
    `std_bbox` 換算、`reachable_bbox` 短路命中、未命中 fallback、walk_roi 正確性。
- [ ] **M4 真檔驗收**：`R8_OD_to_VC_NEW.oas` GUI 開 ROI → walk 秒級 + 幾何正確。

## 不變式 / 風險

- 命中才短路、未命中走既有路徑 → **不退化**（§7）。
- 格式偏小 → 漏幾何（危險）；M1 真檔確認 + M2 DEBUG `⊇` cross-check 雙重防護。
- offset_flag 0/1 兩路徑都要收（KLayout strict 是 1）。
</content>
</invoke>
