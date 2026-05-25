# [F3] 多 POI Fine Align ＋ 版面/POI 鈕/Layer 名稱 UI 優化

> **狀態：** code done 2026-05-25（GUI 互動驗收待 user 本地）
> **§8 ID：** [F3]
> **建立：** 2026-05-25
> **負責 branch：** claude/compassionate-dijkstra-84Gjd

---

## Goal & Context

使用者一次提出 6 項需求（4 改動 + 2 問題）。問題已於對話回答（FG/BG grey 意義、
minimap 與 SEM mode 差異）。本 plan 涵蓋 4 項程式改動，全部集中在
`glas/app/gds_align_tool.py`（必要時 `collapsible.py` / core 取 LAYERNAME）：

1. **版面擠迫/裁切**：維持側欄固定寬（user 指定不改成可拖拉），只修「擠在一起」與
   Coordinate Setup 展開時欄位/按鈕被裁切、對話框最小寬把視窗撐破的問題。
2. **POI 選擇鈕不明顯**：左側 `_LayerRow` 的 18×16 白底「P」鈕在面板上幾乎看不到；
   放大、改寫「POI」字樣、選中時明顯高亮。
3. **Layer 列顯示名字＋數字**：raw layer 目前只有 `L17/D0 · 42`；改用 OASIS
   LAYERNAME 解析到的名稱顯示成 `METAL1 (L17/D0) · 42`，無名稱者退回原格式。
4. **Fine Align 優化 → 多 POI**：目前只支援單一 POI。改為可選多個 POI layer，
   每個 POI 有自己的 FG gray，合成成**一張**類 SEM 樣板（共用 BG/blur/radius/
   threshold）後跑單次 `matchTemplate`；並提供彈出視窗並排 **SEM / GDS / Template**
   做視覺分析。對位結果仍是單一合成偏移（匯出 schema 不變）。

成功樣子：任何視窗大小下 UI 不破版；左側每層看得到名稱＋POI 鈕；可勾多個 POI、
各設 FG gray，按 Run 出單一分數＋偏移，並能彈窗看三圖比對。

與現有系統關係：**延伸** M4b 單 POI 流程，不是取代——單 POI 是多 POI 的 n=1 特例。

---

## Q&A Decisions

### Q1: 響應式版面做法
**選項：** 側欄可拖拉+下限 / 維持固定寬只修擠迫
**選擇：** 維持固定寬只修擠迫
**理由：** user 偏好固定佈局；問題在內容溢出/裁切與對話框最小寬，非欄寬本身。

### Q2: Layer 名稱來源
**選項：** OASIS LAYERNAME / 手動命名 / 只排版數字
**選擇：** OASIS LAYERNAME
**理由：** 檔案本身已帶 LAYERNAME 記錄（streamer record 11/12 已解析），免使用者手動。

### Q3: POI 鈕怎麼變明顯
**選擇：** 放大、改寫「POI」字樣、選中明顯高亮（user 反映目前全白看不到）。

### Q4: 多 POI 組合方式
**選擇：** 合成成一張樣板（每層自己的 FG gray、共用 BG gray），跑單次 matchTemplate。
**理由：** 最接近真實複雜 SEM、單一偏移結果、最好視覺化。

### Q5: 哪些參數每 POI 獨立
**選擇：** 僅 **FG gray** 每 POI；BG gray / blur σ / search radius / score threshold 全局。

### Q6: 樣板視覺化
**選擇：** 彈出視窗，並排 3 張 **SEM / GDS / Template**。

### Q7: 對位結果匯出
**選擇：** 單一合成偏移（mmh-gds-alignment-v1 schema 不變）。

---

## Milestones

> 由低風險、彼此獨立的版面/名稱修正先做，再做多 POI 核心，最後視覺化彈窗。
> 環境限制（見 Risks）：sandbox 無 PyQt6/numpy/cv2，無法跑 GUI；以 `py_compile`
> ＋核心純函式單元測試驗證，互動驗收交由 user 本地。

### M1: 版面擠迫 / Coordinate Setup 裁切修正  [status: done 2026-05-25]

- [x] `CoordinateSetupPanel` 展開時欄位/按鈕被裁切：`CollapsibleSection` body layout
  加 `setSizeConstraint(SetMinimumSize)`，展開後不再被下方 list 擠壓裁切，scroll 改為捲動。
- [x] 對話框最小寬（LayerFilterDialog 540、ExpressionLayerDialog 420、
  AlignmentExportDialog 360）以 `_capped_min_width()` 夾到螢幕可用寬，避免小視窗被撐破。
- [x] `MainWindow.setMinimumSize(min(940,avw), min(600,avh))`（capped 到螢幕），避免縮到破版。
- [x] density：由最小尺寸避免過度壓縮達成；不盲調 spacing（無法互動驗證）。
- [ ] 驗證：py_compile 通過；offscreen 多種視窗尺寸 render-grab 與 user 本地拖放縮放待確認。

### M2: Layer 列顯示 LAYERNAME 名稱 + 數字  [status: done 2026-05-25]

- [x] core：`scan_cell_offsets` 同一輪收集 LAYERNAME（record 11/12）為
  `layernames: [(name, layer_iv, datatype_iv)]` 一併回傳。
- [x] `oasis_random`：`resolve_layer_name()` 純函式（INF 區間、最具體者優先）＋
  `RandomAccessReader.layer_display_name(layer, datatype)`。
- [x] app：`LayerEntry.display_name`（display-only，不入 LayerKey identity，避免破壞
  `find()` 查找）；`_roi_entry` 從 reader 填名；`_LayerRow` 顯示 `NAME (L17/D0) · n`，
  無名稱退回 `L17/D0 · n`，並加 tooltip。
- [x] 驗證：`TestResolveLayerName` 5 項純函式測試；py_compile 通過。
       （注意：display_name 目前只在 ROI 載入路徑填；全檔/cache 路徑暫退回 L/D。）

### M3: 多 POI fine align 核心（合成樣板）  [status: done 2026-05-25]

- [x] `LayerPanel`：POI 改多選；`poi_changed(object)` → `pois_changed(object)`（帶 list），
  `_on_poi_toggled` 移除互斥、以 panel 順序重組 `_poi_entries` 後 emit。
- [x] 資料模型：per-POI FG gray 由 `FineAlignPanel` 以 `{key: QSpinBox}` 保存
  （key = `LayerKey.key()`）；MainWindow 以 `poi_fgs()` 取用。
- [x] `render_composite_template(poi_layers, anchor, W, H, npp, bg, blur)`：各 POI mask
  以各自 fg 疊到共用 bg、最後一次 blur；`render_poi_template` 改為 n=1 thin wrapper。
- [x] `_on_run_fine_align`（`_build_template`/`_coarse_anchor`）、`FineAlignAllWorker`
  （吃 `poi_specs=[(spec,fg)]`）、`_poi_specs()` 全改多 POI。`poi_polys_for_roi` 維持單 spec、被逐一呼叫。
- [x] 匯出：`poi_layer = "; ".join(各層名稱)`（schema 不變）。
- [x] 驗證：py_compile；更新 m4b（多選/specs/worker 建構子）。n=1 合成等同舊 template。

### M4: POI 鈕明顯化 + 多選 UI  [status: done 2026-05-25]

- [x] `_LayerRow` POI 鈕：18×16「P」→ 34×18「POI」＋`_POI_BTN_QSS`（未選白底橘框、
  選中橘底白字），tooltip 改多選語意。
- [x] `FineAlignPanel`：移除單一 `_fg`，改 `_poi_box` 動態每層一列（層名＋FG spin），
  `set_pois(items)` 保留既有 fg 值；BG/blur/radius/threshold 全局；新增 `poi_fgs()`。
- [x] `_on_pois_changed` 接 list；有 ≥1 POI 時展開 Fine Align section。
- [x] 驗證：m4b `test_multi_select_and_run_enabled` / `TestPoiSpecs`；m7 `set_pois`。

### M5: 合成樣板視覺化彈窗  [status: done 2026-05-25]

- [x] `TemplatePreviewDialog`：並排 SEM / GDS / Template 三圖（`_gray_to_pixmap` /
  `_rgb_to_pixmap`），副標顯示 image_id＋POI 名稱＋score。
- [x] `FineAlignPanel` 加「Preview template…」按鈕（`preview_requested`）；
  MainWindow `_on_preview_template` 組三圖（`_render_gds_preview` 畫可見層）後 `exec()`。
- [x] 驗證：py_compile；彈窗實際顯示待 user 本地（無 Qt/cv2 環境）。

---

## Affected Files

- `glas/app/gds_align_tool.py`（主要：LayerPanel/_LayerRow、FineAlignPanel、
  CoordinateSetupPanel、SemPanel、MainWindow、make_template/render_*、
  FineAlignAllWorker、新 TemplatePreviewDialog）
- `glas/app/collapsible.py`（M1：展開高度/捲動回報，如需要）
- `glas/core/oasis_random.py` 或載入路徑（M2：帶出 LAYERNAME map）
- `tests/test_gds_align_m4b.py` / `test_gds_align_m7.py` / 新測試檔（POI/template/label）

---

## Risks / Open Questions

- **環境**：本 sandbox 無 PyQt6 / numpy / cv2，無法啟動 GUI 或跑完整 218+ 測試；
  以 `py_compile` ＋ 可純跑的核心單元測試驗證，GUI 互動驗收（版面、POI 鈕、彈窗）
  必須由 user 本地確認。
- **§7 不變式**：fine-align 修正量符號（M4 overlay sign）、SemViewer 折疊不變式不可動；
  合成樣板只是把單 mask 換成多層疊圖，matchTemplate/符號邏輯不變。
- LAYERNAME 在 ROI 隨機存取路徑是否完整可得需於 M2 確認（streamer 全掃時有；
  random reader 走 name table，需驗證 LAYERNAME 是否被索引）。
- 多 POI 互斥邏輯移除後，既有依賴單一 `_poi_entry` 的測試需同步更新。

---

## 驗證方式

- [ ] 所有 milestone checkbox 已勾
- [ ] `python3 -m py_compile glas/app/gds_align_tool.py glas/app/collapsible.py`
- [ ] `pytest tests/test_gds_align_m4b.py tests/test_gds_align_m7.py -v`（環境允許時）
- [ ] 新增的 template/label/POI 純函式測試通過
- [ ] 手動（user 本地）：縮放視窗不破版、Coordinate Setup 展開不裁切、左側看得到
  層名＋POI 鈕、勾多 POI 各設 FG、Run 出單一分數、Preview 三圖彈窗
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 最終 SESSION_LOG 條目註記 `完成 [F3]`
- 從 `CLAUDE.md` §8 移除 [F3]
- 本檔保留作 design history
