# [F4] Boolean 表達式引擎強化：食譜化重算 + 巢狀 + 編輯 + 對話框重設計

> **狀態：** implemented（待 user 本地 GUI/測試驗收）
> **§8 ID：** [F4]
> **建立：** 2026-05-25
> **負責 branch：** claude/compassionate-dijkstra-84Gjd（PR #3）

---

## Goal & Context

現況（探索結果）：synthetic（Boolean 表達式）layer 只在「+ Expression…」對話框按下時，
對**當前 viewport FOV** 算一次幾何後塞進 doc；之後：

- **載入新 ROI（跳到別的 defect）→ 整個 doc 被換掉，synthetic layer 直接遺失**，不會
  對新 FOV 重算（只有 cache 載入時 `_restore_expr_sidecar` 會重算）。
- **無法編輯**已建立的 synthetic layer（只能重建或被 ROI reload 清掉）。
- **無法巢狀**：binding 下拉只列 raw layer（`raw_layer_keys()` 濾掉 synthetic），
  `_eval_expression` 只用 `(layer,datatype)` 查 raw entry。
- `W:`/`H:` 其實都是等向 buffer（本 plan 暫不改語意，user 未選此項）。

**目標（Q&A 收斂）：**

1. **食譜化 + 每 FOV 自動重算**：把使用者定義的 Boolean layer 當「recipe」記住，跳到新
   defect / 載新 ROI 時自動對該 FOV **即時重算**（只算該 ROI、不算整顆 GDS），並隨
   cache 存檔。
2. **編輯 / 刪除**既有 synthetic layer（左側列雙擊或按鈕 → 預填對話框重開；可刪）。
3. **巢狀引用**：binding 可指向另一個 synthetic layer（名稱），eval 遞迴解析（含循環偵測）。
4. **表達式對話框重設計**：點層/運算子按鈕插入 token、即時語法檢查 + inline 錯誤、即時預覽。

成功樣子：定義一次 `L0 = [(A>W:5)&B]<H:5`、`L1 = L0 - C`，之後每點一個 defect，左側
L0/L1 都自動對該 FOV 重算顯示；可雙擊改 L0 表達式、L1 立即跟著更新；對話框好用、即時報錯。

與現有系統關係：**延伸**既有 `gds_boolean` 引擎（parser/AST/evaluate 不需大改）與 synthetic
layer 機制，主要是補「持久化重算 + 巢狀解析 + 編輯 + UI」。

---

## Q&A Decisions

### Q1: Fine align vs Boolean 先做
**選擇：** 先做 Boolean（F4）；fine align 診斷改排 F5。

### Q2: 持久/重算模型
**選擇：** 食譜化，每 FOV 自動重算（不算整顆 GDS），隨 cache 存檔。

### Q3: 巢狀引用
**選擇：** 要（binding 可指向 synthetic layer），eval 遞迴解析。

### Q4: UI 幅度
**選擇：** 完整重設計表達式對話框（點層/運算子插入、即時語法檢查 + 預覽），並含編輯/刪除。

---

## Milestones

> 先做引擎/資料層（可純函式測試、低 GUI 依賴），再做持久化重算，最後做 UI 重設計。

### M1: Recipe store + 巢狀 binding 解析（引擎/資料層）  [status: done]

- [x] binding 改為 tagged 形式以支援巢狀：raw = `("raw", layer, datatype)`、synthetic =
  `("ref", name)`（取代現有純 `(layer,datatype)`）。提供 from/to-dict 與舊格式遷移
  （舊 `(layer,datatype)` 視為 `("raw",...)`），cache sidecar 同步。
- [x] 抽出單一 `resolve_expr_geometry(expr, bindings, raw_geom_provider, recipes,
  fov_bbox, _visiting)`：遞迴把 `("ref", name)` 解析成該 recipe 的幾何（memoize +
  循環偵測，循環丟 `BooleanExprError`）。display 與 F3 POI batch 共用它。
- [x] `MainWindow` 增 `self._recipes`（有序 `{name: {expr_text, bindings}}`），為唯一
  事實來源；synthetic LayerEntry 由 recipe 衍生。
- [x] 驗證：`resolve_expr_geometry` 純函式測試（巢狀 2 層、循環偵測、缺 binding、
  raw 不存在時回空）；binding 遷移測試。

### M2: 每 FOV / ROI 自動重算 + 編輯 / 刪除  [status: done]

- [x] ROI 載入（`_on_roi_finished`）與跳 defect（`_jump_to_image` / 新 ROI 幾何就緒時）
  後，依相依順序（topological，因巢狀）對當前 FOV 重算每個 recipe 並（重）插入 synthetic
  LayerEntry。raw binding 在此 ROI 沒載到 → 該層算空 + status 提示，不崩。
- [x] cache 存/讀：recipe 一併序列化，載入時還原並重算（與 `_restore_expr_sidecar` 整合）。
- [x] 左側 synthetic `_LayerRow`：加「編輯」「刪除」（雙擊 = 編輯）；編輯預填表達式 +
  bindings、存檔後更新 recipe 並連動重算所有相依層；刪除移除 recipe + 其 layer。
- [x] 驗證：py_compile；以假 doc/recipes 測「ROI reload 後 synthetic 仍在且重算」、
  「編輯 L0 → L1 連動」、「刪除」純邏輯；互動待 user 本地。

### M3: 表達式對話框重設計（UX）  [status: done]

- [x] 重做 `ExpressionLayerDialog`：可點選 layer chip（raw + 既有 synthetic）與運算子
  按鈕（`& | - ~ > W: < H: ( )`）把 token 插入表達式輸入；name 欄。
- [x] 即時語法檢查：邊打邊 `parse_expression`，錯誤以 inline 訊息標示、disable 確定鈕；
  bindings 由表達式自動推導列出（raw 用下拉、ref 用 synthetic 名）。
- [x] 即時預覽（沿用 `preview_cb`）：在 canvas 畫出當前 FOV 結果。
- [x] 驗證：py_compile；對話框可開/輸入/報錯/預覽（user 本地）；token 插入純邏輯測試。

### M3.1: 修 edit 閃退 + 對話框內嵌預覽（user 回饋）  [status: done]

- [x] edit/delete/add 一律 `QTimer.singleShot(0, …)` 延遲，避免在 row 事件 handler 內開
  modal 又被 `set_document` 刪除 → use-after-free 閃退。
- [x] `ExpressionLayerDialog` 內嵌 `_ExprPreview` 迷你 canvas，Preview 在對話框內渲染、
  不動主 doc；OK 鈕改名 Save。

### M4: 方向性 W/H morphology（user 回饋）  [status: done]

- [x] `> W:n` 只長寬（X）、`> H:n` 只長高（Y）、`< W:n`/`< H:n` 各軸縮，每邊 ±n nm。
  grow = 與軸線段的 Minkowski sum（`_dilate_axis`，對任意多邊形精確）；shrink = 補集-膨脹-
  補集 erosion（`_morph_axis`，需 fov_bbox）。parser 限制 label 僅 W/H。
- [x] 對話框運算子鈕擴成 `> W: / > H: / < W: / < H:` 四顆。
- [x] 驗證：更新 morph 測試（方向性面積/bounds、shrink 需 fov、bad axis label）。

---

## Affected Files

- `glas/core/gds_boolean.py`（如需：循環偵測 helper / 巢狀解析支援，但盡量放 app 層）
- `glas/app/gds_align_tool.py`（recipe store、`resolve_expr_geometry`、ROI/jump 重算接線、
  `ExpressionLayerDialog` 重設計、`_LayerRow` 編輯/刪除、cache 序列化）
- `tests/`（新 `test_gds_boolean_recipes.py` 或併入既有：巢狀/循環/遷移/重算純邏輯）

---

## Risks / Open Questions

- **環境**：sandbox 無 PyQt6/numpy/cv2/shapely → 無法跑 GUI 或 shapely eval；以 py_compile
  ＋不依賴 shapely 的純邏輯測試把關（binding 遷移、循環偵測、相依排序），幾何正確性與
  對話框互動由 user 本地驗收。
- **binding schema 變更**：影響 cache sidecar 與 F3 POI batch（`poi_polys_for_roi` 的
  `("expr",...)` spec）；需遷移舊格式並讓兩條 eval 路徑共用 `resolve_expr_geometry`。
- **效能**：每次跳 defect 對所有 recipe 重算；FOV 內幾何量小應可即時，但 recipe 多 + 巢狀
  深時要留意（memoize 同一次重算內的子結果）。
- **§7 不變式**：不動 OASIS decode / klarf↔gds / fine-align 符號；本 plan 僅 Boolean + UI。
- **待 user 確認**：循環引用時的行為（目前定為報錯）；raw binding 在當前 ROI 缺漏時是否
  要警示或靜默算空（目前定為 status 提示 + 算空）。

---

## 驗證方式

- [x] 所有 milestone checkbox 已勾
- [x] `python3 -m py_compile glas/app/gds_align_tool.py glas/core/gds_boolean.py`（已過）
- [ ] 新增純邏輯測試通過（巢狀/循環/遷移/重算/相依排序）— **sandbox 無 numpy/shapely，待 user
  本地 `pytest tests/test_gds_boolean.py -v`**
- [ ] 手動（user 本地）：定義 L0、L1=巢狀引用 L0；跳多個 defect 看自動重算；編輯 L0 連動；
  刪除；新對話框 token 插入 / 即時報錯 / 預覽
- [x] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 最終 SESSION_LOG 條目註記 `完成 [F4]`
- 從 `CLAUDE.md` §8 移除 [F4]
- 本檔保留作 design history
