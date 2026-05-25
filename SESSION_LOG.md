# Session Log

---

## [2026-05-25] [F4] 方向性 W/H morphology + coordinate setup 版面溢出修復

**變更類型：** 功能（語意變更）+ bug fix

**動機現象：** (1) `A > W:10` 看起來把高度也加大 —— 原本 morph 是**等向** buffer，W/H 只是
標籤、`>`/`<` 為 grow/shrink 全方向。user 要 W/H 變成**方向性**（W=寬/X、H=高/Y）。
(2) Coordinate Setup 輸入第一個值後整個面板暴寬、超出 UI。

**修復實作：**
- **方向性 W/H（`gds_boolean.py`）**：`> W:n`=只長寬、`> H:n`=只長高、`< W:n`/`< H:n`=各軸縮，
  每邊各 ±n nm（總 ±2n）。新增 `_dilate_axis`（與軸線段的 Minkowski sum：geom + 平移副本 +
  各邊掃成平行四邊形 → 對任意多邊形精確）與 `_morph_axis`（grow 用 dilate；shrink 用補集-膨脹-
  補集 erosion，需 fov_bbox）。`evaluate` 的 Morph 分支改呼叫 `_morph_axis`；parser 限制軸
  標籤僅 W/H（大小寫不拘）否則報錯。更新 `tests/test_gds_boolean.py` morph 測試（方向性
  面積 + bounds、shrink 缺 fov 報錯、非法軸標籤報錯）。對話框運算子鈕擴成 `>W: >H: <W: <H:`。
- **版面溢出（`gds_align_tool.py`）**：`CoordinateSetupPanel` 的 `_corner_lbl`/`_origin_lbl`
  無 word-wrap，輸入值後標籤文字變長（含千分位 + nm/µm），單行 QLabel 撐寬固定 300px 的 SEM
  面板而溢出。兩個 label 加 `setWordWrap(True)`。

**測試：** `py_compile` 三檔過。sandbox 無 numpy/shapely/PyQt6 → 未跑 pytest / GUI，待 user
本地 `pytest tests/test_gds_boolean.py -v` + 驗 GUI（W/H 方向、coordinate setup 不再溢出）。

**影響檔案：** `glas/core/gds_boolean.py`、`glas/app/gds_align_tool.py`、
`tests/test_gds_boolean.py`、`CLAUDE.md`、`docs/plans/F4-boolean-enhance.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

---

## [2026-05-25] [F4] 修復 edit 閃退 + 對話框內嵌預覽

**變更類型：** bug fix + 功能

**動機現象：** (1) 編輯 expression layer 時偶發閃退（終端機有 Error）。(2) 預覽新 Boolean
layer 要回主視窗才看得到，且 modal 對話框擋住主畫面 canvas。

**修復實作：**
- **閃退根因**：edit/delete 由 `_LayerRow` 的按鈕點擊 / 雙擊 signal 觸發，handler 內同步
  開 modal 對話框（`exec()`）；對話框關閉後 `_recompute_recipes()` → `set_document()` 會
  刪掉那個 row widget，待 `exec()` 返回時控制流回到「已被刪除的 C++ row 物件」的事件
  handler → use-after-free，PyQt6 直接 abort。改為 `_on_edit_recipe`/`_on_delete_recipe`/
  `_on_add_expression` 一律用 `QTimer.singleShot(0, …)` 延遲，等 row 的 handler 完全 unwind
  後再開對話框，避免在 row 事件處理中刪除自身。
- **內嵌預覽**：`ExpressionLayerDialog` 新增 `_ExprPreview` 迷你 canvas（fit-to-view），按
  Preview 直接在對話框內渲染結果（filled highlight）疊在綁定的 raw layer（細外框）上，
  對話框不關、不再動主視窗 doc/canvas；確認無誤再按 **Save**（OK 鈕改名 Save）儲存。
  `_preview_expression` 改回傳 `(ok, msg, data)` 且**不再 mutate 主 doc**（移除原本塞臨時層
  + cancel 時 recompute 的迂迴）。

**測試：** `py_compile` 過。sandbox 無 PyQt6 → GUI 待 user 本地驗（編輯不再閃退、Preview 在
對話框內顯示、Save 才存）。

**影響檔案：** `glas/app/gds_align_tool.py`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

---

## [2026-05-25] [F4] 實作：Boolean 強化（食譜化重算 + 巢狀 + 編輯 + 對話框重設計）

**變更類型：** 功能（新功能 + 重構）

**動機現象：** synthetic（Boolean 表達式）layer 只算一次、載新 ROI（跳 defect）就遺失、
無法編輯、binding 只能綁 raw layer（無法巢狀）。

**修復實作：**
- **引擎（`gds_boolean.py`）**：新增 `normalize_binding`（舊 `(layer,datatype)` →
  `("raw",l,d)`、支援 `("ref",name)`）、`recipe_dependency_order`（拓樸排序 + 循環/未知
  ref 偵測，純函式）、`resolve_expression`（raw/recipe provider 抽象 + 遞迴解析巢狀 ref +
  cache memoize + 循環防護）。core 維持無 Qt。
- **app（`gds_align_tool.py`）**：MainWindow 新增 `self._recipes` 作 synthetic 層唯一事實
  來源；`_recompute_recipes()` 在 `_on_roi_finished`（每次載 ROI/跳 defect 的 FOV）與
  cache 還原時自動重算所有 recipe → synthetic 層跟著 defect 走。`_eval_expression` 改用
  `resolve_expression`（display 路徑）；`poi_polys_for_roi`（F3 batch）同步支援巢狀 +
  recipe 快照。`_LayerRow` 加編輯/刪除按鈕（雙擊=編輯），刪除被其他 recipe 引用時擋下。
  `ExpressionLayerDialog` 重設計：layer/synthetic chip + 運算子按鈕插入 token、即時語法
  檢查（disable OK + inline 錯誤）、binding 下拉含 raw + ref、編輯預填。cache sidecar
  改由 recipe 序列化/還原（tagged binding，含舊格式遷移）；開新 OASIS(ROI)/載新 cache 會
  清掉前一檔的 recipe，ROI reload 則保留。
- **tests**：`tests/test_gds_boolean.py` 加 `normalize_binding` / `recipe_dependency_order`
  / `resolve_expression`（巢狀、循環、未知 ref、舊格式）測試。

**測試：** `py_compile` 三檔皆過。**sandbox 無 numpy/shapely → 未跑 pytest；GUI 互動未驗。**
待 user 本地 `pytest tests/test_gds_boolean.py -v` + GUI 驗收（定義 L0/L1 巢狀、跳 defect
自動重算、編輯連動、刪除、新對話框）。

**影響檔案：** `glas/core/gds_boolean.py`、`glas/app/gds_align_tool.py`、
`tests/test_gds_boolean.py`、`docs/plans/F4-boolean-enhance.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

---

## [2026-05-25] [F4] 規劃：Boolean 強化（食譜化重算 + 巢狀 + 編輯 + 對話框重設計）

**變更類型：** 文件（plan，尚未動工）

**內容：** user 調整優先序：F4 改做 Boolean 強化、原 fine align 診斷改排 F5。經探索確認現況
（synthetic layer 只算一次、ROI reload 即遺失、無法編輯、無法巢狀），Q&A 收斂為四項：
食譜化每 FOV 自動重算、巢狀引用 synthetic、編輯/刪除、表達式對話框完整重設計。
新增 `docs/plans/F4-boolean-enhance.md`（3 milestone）；§8 更新 [F4] 指向新 plan、
[F5] = fine align 診斷。**待 user 核准後才開工。**

**影響檔案：** `docs/plans/F4-boolean-enhance.md`、`docs/plans/F5-finealign-diagnostics.md`
（renumber）、`CLAUDE.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

## [2026-05-25] [F4] 規劃：Fine align 診斷（殘差疊圖 + 批次結果總覽）

**變更類型：** 文件（plan，尚未動工）

**內容：** 經 Q&A 收斂 fine align 強化方向＝結果可視化/診斷，具體交付兩項：殘差疊圖
overlay（對位前/後輪廓畫在 SEM）與批次結果總覽（可排序/篩選/點列跳轉的表格）。
新增 `docs/plans/F4-finealign-diagnostics.md`（2 milestone），於 CLAUDE.md §8 註冊 [F4]。
**待 user 核准後才開工。**

**影響檔案：** `docs/plans/F4-finealign-diagnostics.md`、`CLAUDE.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

## [2026-05-25] [F3] 後續：toolbar 不裁切、layer name catch-all 修正、移除透明度 slider

**變更類型：** UI 修正 + bug fix（PR #3 後續 user 回饋 1/2/3）

**動機/現象：**
1. 視窗非最大化時中間 toolbar 按鈕（Open OASIS / Load Cache / Export Cache…）文字被裁切。
2. 讀 OASIS LAYERNAME 時所有 layer 都顯示同一個名字（NW）。
3. Layers 列的透明度搖桿沒實際用途，要移除。

**修復：**
1. `_build_toolbar` 結尾把每顆按鈕 `setMinimumWidth(sizeHint().width())`（在設粗體後），
   並新增 `_wrap_toolbar()` 用橫向 `QScrollArea`（v-scroll off、h-scroll as-needed、
   高度 = bar + scrollbar extent）包住，窄視窗改為橫向捲動而非裁字。
2. `resolve_layer_name` 改為「最具體（最窄 layer 區間，其次 datatype 區間）優先」，
   並跳過 `(0, INF)` 全層 catch-all（placeholder 名稱不再蓋到每一層）。
   注意：LAYERNAME 表若在檔尾（scan_cell_offsets 於首個 CELL 即停）仍可能收不到，
   屆時退回 L/D；若仍有問題需後續加讀檔尾 name table。
3. `_LayerRow` 移除 opacity slider/`_pct`/`_on_opacity`（`LayerEntry.opacity` 保留，
   渲染用預設值）；移除未用的 `QSlider` import；hint 文字更新。

**測試：** py_compile 全通過；更新 `test_oasis_random.py::TestResolveLayerName`（catch-all
跳過、不蓋其他層）、移除 `test_gds_align_m6.py::test_slider_sets_opacity_and_emits`。
sandbox 無 PyQt6/numpy/cv2，toolbar 捲動/透明度移除等 GUI 行為待 user 本地驗收。

**影響檔案：** `glas/app/gds_align_tool.py`、`glas/core/oasis_random.py`、
`tests/test_oasis_random.py`、`tests/test_gds_align_m6.py`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

## [2026-05-25] [F3] 修正：多 POI 選取以 row 狀態重建，避免 ndarray __eq__ 報錯

**變更類型：** Bug fix（PR #3 review，P1）

**動機/現象：** `LayerEntry` 是含 NumPy 陣列（polygons/bboxes）的 dataclass，
`_on_poi_toggled` 用 `entry not in self._poi_entries` / `.remove(entry)` 會觸發
dataclass `__eq__` 對陣列比較，實際 ROI 資料下選/取消第二個 POI 會丟
`ValueError: truth value of an array ... is ambiguous`，破壞多 POI 互動。

**修復（`glas/app/gds_align_tool.py`）：** 移除多餘且有 bug 的 append/remove 區塊，
直接由各 row 的 `poi_btn.isChecked()` 以 panel 順序重建 `_poi_entries`（原本下方
本就有此重建，append/remove 為冗餘）。不再對 LayerEntry 做相等比較。

**測試：** py_compile 通過；既有 `test_gds_align_m4b.py::test_multi_select_and_run_enabled`
覆蓋多選 toggle 路徑。

**影響檔案：** `glas/app/gds_align_tool.py`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`（PR #3）

## [2026-05-25] [F3] M3–M5：多 POI fine align（合成樣板）＋ POI 鈕／預覽彈窗

**變更類型：** 功能（fine align 多 POI / UI）

**動機/現象：** 原 fine align 僅支援單一 POI，真實半導體 SEM 影像含多層結構。改為可選
多個 POI layer，各自輸入 FG gray，合成一張類 SEM 樣板做單次 matchTemplate，並能彈窗
並排 SEM/GDS/Template 做視覺分析。

**修復/實作（`glas/app/gds_align_tool.py`）：**
- 核心：新增 `render_composite_template(poi_layers,...)`（各層 mask 以各自 fg 疊到共用 bg、
  一次 blur）；`render_poi_template` 改為 n=1 thin wrapper（行為不變）。
- `LayerPanel`：POI 改多選，`poi_changed`→`pois_changed(list)`，`_on_poi_toggled` 去互斥、
  以 panel 順序重組；`_LayerRow` POI 鈕放大改「POI」＋`_POI_BTN_QSS`（解決全白看不到）。
- `FineAlignPanel`：移除單一 FG，改 `_poi_box` 每 POI 一列（名稱＋FG spin），`set_pois()`
  保留既有值，新增 `poi_fgs()`；BG/blur/radius/threshold 維持全局；加「Preview template…」。
- `MainWindow`：`_on_pois_changed` / `_poi_layers` / `_build_template` / `_coarse_anchor` /
  `_poi_specs` 全多 POI；`FineAlignAllWorker` 改吃 `poi_specs=[(spec,fg)]`；匯出 `poi_layer`
  多層串接；新增 `TemplatePreviewDialog` + `_on_preview_template` + `_render_gds_preview`。

**測試：** py_compile 全通過；更新 `test_gds_align_m4b.py`（多選、`_poi_specs`、worker
建構子、composite）、`test_gds_align_m7.py`（`set_pois`）。sandbox 無 PyQt6/numpy/cv2，
GUI／matchTemplate 互動驗收待 user 本地。

**影響檔案：** `glas/app/gds_align_tool.py`、`tests/test_gds_align_m4b.py`、
`tests/test_gds_align_m7.py`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`

## [2026-05-25] [F3] M1+M2：版面裁切/最小尺寸修正＋OASIS 圖層名稱顯示

**變更類型：** 功能（UI 修正 + 圖層名稱）

**動機/現象：** (M1) 視窗縮放時版面擠迫、Coordinate Setup 展開時欄位/按鈕被裁切、
對話框最小寬可能撐破小螢幕。(M2) 左側 layer 只顯示 `L17/D0`，user 想看名稱。

**修復/實作：**
- M1（`collapsible.py`）：`CollapsibleSection` body layout 加
  `setSizeConstraint(SetMinimumSize)`，展開段落不再被下方 list 擠壓裁切。
- M1（`gds_align_tool.py`）：新增 `_screen_avail()` / `_capped_min_width()`，三個對話框
  最小寬夾到螢幕；`MainWindow.setMinimumSize(min(940,avw),min(600,avh))`。
- M2（`oasis_streamer.py`）：`scan_cell_offsets` 同輪收集 LAYERNAME → `layernames`。
- M2（`oasis_random.py`）：`resolve_layer_name()` 純函式 + `RandomAccessReader.layer_display_name()`。
- M2（`gds_align_tool.py`）：`LayerEntry.display_name`（display-only，不入 LayerKey identity）；
  `_roi_entry` 填名；`_LayerRow` 顯示 `NAME (L17/D0) · n`，無名稱退回 `L17/D0 · n`。

**測試：** py_compile 全通過；新增 `tests/test_oasis_random.py::TestResolveLayerName` 5 項
（純函式，已於 sandbox 以等價邏輯驗過）。GUI 版面修正待 user 本地確認。

**影響檔案：** `glas/app/gds_align_tool.py`、`glas/app/collapsible.py`、
`glas/core/oasis_streamer.py`、`glas/core/oasis_random.py`、`tests/test_oasis_random.py`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`

## [2026-05-25] 規劃 [F3]：多 POI Fine Align ＋ UI 優化（plan only，尚未動程式）

**變更類型：** 規劃（plan 文件 + §8 任務註冊）

**動機/現象：** user 一次提出 6 項（4 改動 + 2 問題）。問題已於對話回答：(1) Fine
Align 的 FG/BG grey 是合成樣板的前景/背景灰階；(2) View mode 的 minimap 與 SEM mode
差別在 minimap 只是 SEM 滿版再浮一個 defect 色點小地圖、不畫 OASIS 幾何。4 項改動經
`AskUserQuestion` 收斂：維持側欄固定寬只修擠迫/裁切、Layer 列用 OASIS LAYERNAME 顯示
名稱、POI 鈕放大改「POI」字樣、Fine Align 改多 POI（每 POI 自己 FG gray、合成一張樣板、
彈窗並排 SEM/GDS/Template）。

**實作：** 新增 `docs/plans/F3-multipoi-and-ui.md`（5 milestone：M1 版面/裁切、
M2 LAYERNAME 名稱、M3 多 POI 核心、M4 POI 鈕+多選 UI、M5 視覺化彈窗），於 CLAUDE.md
§8「進行中」註冊 [F3]。**尚未修改任何程式碼**，待 user 核准 plan 後從 M1 開工。

**測試：** 無（純規劃）。

**影響檔案：** `docs/plans/F3-multipoi-and-ui.md`、`CLAUDE.md`、`SESSION_LOG.md`。

**Branch：** `claude/compassionate-dijkstra-84Gjd`

## [2026-05-24] UI batch 1：Load SEM 主色按鈕 / Coord 折疊 badge / image list badge

**變更類型：** 功能（UI / UX）

**動機/現象：** 三項視覺強化：(1) `Load SEM…` 按鈕視覺權重不足，與 `Open OASIS…`
不對等；(2) Coordinate Setup 收起後看不出 FOV 是否已設定；(3) image list 每列無法
一眼看出對位狀態（有無座標 / fine-align 分數）。

**修復/實作：**
- **Fix 1（`gds_align_tool.py`）**：新增 `_LOAD_SEM_BTN_QSS`（橘底白字 + hover 深橘 +
  menu-indicator），`SemPanel` 的 Load SEM 按鈕存為 `self.load_sem_btn` 並套用該 QSS。
  （按鈕實際在 `SemPanel` 而非 toolbar。）
- **Fix 2（`collapsible.py` + `gds_align_tool.py`）**：`CollapsibleSection` header 加
  `self._badge` QLabel（右對齊，no-trailing 路徑也包一層 row）+ `set_badge(text,fg,bg)`
  + `_update_badge_visibility()`（僅在收起且有文字時顯示）；`SemPanel.update_coord_badge()`
  讀 `fov_w_nm`/`fov_w`（皆 nm，/1000→µm）顯示綠色 `FOV W × H` 或琥珀 `not set`，
  於 `__init__` 末 seed 一次、`MainWindow._on_coord_changed` 每次更新。
- **Fix 3（`gds_align_tool.py`）**：新增 `_ImageListDelegate(QStyledItemDelegate)`，在右
  邊距以 UserRole+2/+3/+4 資料畫圓角 badge；`set_images` 對無座標列調暗文字 + 設
  `no coords` 灰 badge；`set_score` 改設分數 badge（綠/琥珀/紅，門檻 `>=t` / `>=0.7t` /
  else），不再 inline 改文字。

**測試：** `py_compile` 兩檔通過；更新 `test_gds_align_m4b.py::test_end_to_end`（改驗 badge
資料角色而非 `[score]` 文字）；`test_gds_align_m7.py` 新增 7 項（accent QSS / coord badge
not-set / set / hidden-when-expanded / no-coords badge / score green / score red）。完整
`pytest tests/` 442 passed。offscreen render-grab 煙霧測試：視窗正常顯示、Load SEM 橘色、
badge 正確。

**影響檔案：** `glas/app/gds_align_tool.py`、`glas/app/collapsible.py`、
`tests/test_gds_align_m7.py`、`tests/test_gds_align_m4b.py`。

**Branch：** `claude/jolly-babbage-8nwED`（PR #2）

## [2026-05-24] LAYERS empty hint 置中微調

**變更類型：** UI 微調

**動機/現象：** `LayerPanel._show_empty_hint()` 的三個 item 用 `AlignCenter`，
改為 `AlignHCenter` 明確水平置中（QListWidget 無 list-wide setAlignment API，
per-item setTextAlignment 即正確機制）。`_group()` 橘色標籤上一輪已完成，本次未動。

**修復/實作（`glas/app/gds_align_tool.py`）：** icon/title/hint 三 item 的
`setTextAlignment` 由 `Qt.AlignmentFlag.AlignCenter` → `AlignHCenter`。

**測試：** `python3 -m py_compile` 通過；`pytest tests/test_gds_align_m6.py
tests/test_gds_align_m7.py` 59 passed。

**影響檔案：** `glas/app/gds_align_tool.py`。

**Branch：** `claude/jolly-babbage-8nwED`（PR #2）

## [2026-05-24] GLAS UI 五項修正（依 docs/glas_ui_fixes.md）

**變更類型：** 功能（UI / UX 微調）

**動機/現象：** 依 `docs/glas_ui_fixes.md` 修正五個 UI 問題：右欄 Coordinate Setup
預設展開把 image list 擠掉、左欄 LAYERS 空白引導視覺太輕、Set/Clear Offset 放在中央
視圖下方定位不清、toolbar group label 對比不足、中央 empty state 與 guidance strip
文字重複。

**修復/實作（`glas/app/gds_align_tool.py`）：**
- 問題1：`SemPanel` 的 Coordinate Setup `_wrap_section(..., collapsed=True)`（原 False）；
  `MainWindow.__init__` 加 `self._coord_collapsed_once = True`，使自動收起邏輯不再干預
  （預設已收起，user 再展開即固定）。
- 問題2：`LayerPanel._show_empty_hint()` 由單行小字改為圖示 + 主文「Open an OASIS」+
  次文「toolbar → Open OASIS…」三層置中結構。
- 問題3：Set/Clear Offset 由中央 `center_layout` 移入 `SemPanel`（image list 下方、
  Load GDS ROI 上方），改名 `self.sem_panel.set_offset_btn/clear_offset_btn`，
  signal 在 `MainWindow.__init__` 重新接線；原 `self._set_offset_btn/_clear_offset_btn`
  區塊整段刪除（無其他 setEnabled 引用）。
- 問題4：`_build_toolbar` 的 `_group()` label 改用 `_TK_ACCENT_DK` 色、letter-spacing
  1px、padding；FILE group 前加 `h.addSpacing(4)`。
- 問題5：`SemViewer._draw_empty_state()` 三步驟提示改為「Follow the steps above to get
  started」，由 guidance strip 負責引導。

**測試：** `python3 -m py_compile` 通過；同步更新 4 個測試的舊行為斷言
（`test_gds_align_m6.py::test_set_document_none_clears`、`test_gds_align_m7.py` 的
`test_initial_collapse_state` / `test_no_collapse_without_valid_fov` / `test_layers_empty_hint`），
`pytest tests/test_gds_align_m6.py tests/test_gds_align_m7.py` 59 passed，
完整 `pytest tests/` 435 passed。

**影響檔案：** `glas/app/gds_align_tool.py`、`tests/test_gds_align_m6.py`、
`tests/test_gds_align_m7.py`。

**Branch：** `claude/jolly-babbage-8nwED`

## [2026-05-24] GLAS 品牌元素整合（icon / wordmark / About）

**變更類型：** 功能（UI / branding）

**動機/現象：** 應用程式仍沿用舊名 "GDS Align Tool"，缺視窗 icon、wordmark 與品牌化
About 對話框。依 `docs/IMPLEMENT.md` 將五項品牌元素整合進 app。

**修復/實作（`glas/app/gds_align_tool.py`）：**
- import：QtGui import 補上 `QIcon`。
- `main()`：設定 `setApplicationName/DisplayName/Version/OrganizationName("GLAS"...)`，
  並以 `icons/glas_icon_256.svg` 設 `app.setWindowIcon`（所有視窗共用）。
- `MainWindow.__init__()`：視窗標題 "GDS Align Tool" → "GLAS"，並以
  `icons/glas_icon_32.svg` 設 titlebar icon。
- `_build_toolbar()`：toolbar 最左側插入 `icons/glas_wordmark.svg` wordmark（高度 28px）
  + VLine 分隔線（沿用既有 `_divider()` helper）。
- `_show_about()`：由 `QMessageBox.information` 升級為自繪 `QDialog`（128 icon + 大字 GLAS
  + subtitle + 版本 + 說明 + OK 按鈕）。
- 四個 SVG（256/128/32/wordmark）此 session 前已置於 `glas/app/icons/`。

**測試：** `python3 -m py_compile glas/app/gds_align_tool.py` 通過；sandbox 無 PyQt6
無法實際啟動 GUI 驗收（taskbar icon / wordmark 顯示 / About 對話框待 user 本地確認）。

**影響檔案：** `glas/app/gds_align_tool.py`。

**Branch：** `claude/determined-einstein-Bfo0G`

## [2026-05-24] GLAS 專案自 MMH 抽離成立

**變更類型：** 專案建立 / 重構（抽離）

**動機/現象：** GDS Align Tool 原藏在 MMH 專案 `tools/` 下（plan F2，M1–M7 全實作）。
其核心能力——大檔 OASIS streaming / random-access 解析、KLARF↔GDS 座標換算、FOV 空間查詢、
即時 Boolean 表達式引擎、SEM↔GDS overlay 對位——不只 MMH 用得到，未來其他專案也想複用。
藏在 MMH 內定位不對，故抽離成獨立 repo **GLAS（GDS-Layout Alignment for SEM）**。

**實作（自 MMH git HEAD 搬移，零行為改動）：**
- **glas/core/（無 Qt 引擎）**：`oasis_streamer` / `oasis_store` / `oasis_walker` / `oasis_random`
  / `gds_fov` / `gds_boolean` / `gds_layer_cache` + 自 MMH `src/core` 複製的 `klarf_parser`。
  core 模組原本即無 src 依賴，零修改。
- **glas/app/（PyQt6 殼）**：`gds_align_tool`（改寫 header：`from src.gui.*` soft import →
  flat `from styles/collapsible/icons`，repo-root path hack → core+app sys.path 設定；
  subprocess streamer import 指向 glas/core）、`sem_loader`（`from src.core.klarf_parser` →
  `from klarf_parser`）+ 自 MMH `src/gui` 複製的 `styles` / `collapsible` / `icons/`（無 src 依賴）。
- **import 慣例**：core/app 以扁平 sys.path 模組互相 bare-import（沿用原 `tools/` 慣例）；
  `main.py` + `conftest.py` 把 `glas/core` 與 `glas/app` 放上 sys.path。
- **規則機制移植**：`CLAUDE.md`（保留 §2 工作規則 / §6 慣例 / §8 任務 / §10 checklist 機制，
  改寫 §1/§4/§5/§7 為 GLAS 實況）、`.claude/settings.json` + `hooks/check_progress.sh` +
  `check_session_log.sh`（SessionStart 訊息改 GLAS，腳本邏輯不變）、`README.md`、本 `SESSION_LOG.md`。
- **design history**：`docs/plans/F2-gds-align-tool.md` + `F2-M1.13-parser-perf.md` + `_template.md`
  搬入保留。
- **tests**：14 個 test 檔（`test_oasis_*` / `test_gds_*` / `test_sem_loader`）+ `fixtures/sample_real.klarf` 搬入。

**測試：** sandbox 無 numpy / cv2 / shapely / PyQt6 / pytest，無法跑完整 suite；已 `py_compile`
全檔通過。完整 `pytest tests/`（~218 項應全綠，證明搬移零行為改動）待有相依的環境執行。

**接續任務：** [F1] 互動驗收（真實 SEM 對位 / 拖動 / fine-align / 批次 / 匯出 / 折疊 UX）—
這些在 MMH 抽離前即標記「待 user 本地驗證」，移到 GLAS 接續。

**影響檔案：** 整個 GLAS repo（新建）。MMH 側對應移除見 MMH SESSION_LOG 同日條目。

**Branch：** （新 repo，待 user 在 GitHub 建立後上傳）
