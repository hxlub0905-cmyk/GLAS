# [F15] 模擬 GLV 灰階圖 + label ROI 匯出（下游 MMH 區域量測底圖）

> **狀態：** planned
> **§8 ID：** [F15]
> **建立：** 2026-06-03
> **負責 branch：** claude/optimistic-pasteur-31ELv

---

## Goal & Context

**問題來源：** F13 已做 per-image binary GDS mask 輸出，下游 MMH 拿來限縮 blob 偵測範圍。
但 user 進一步釐清下游真正要的不是 binary mask，而是：

1. **一張「已經對齊」的影像當工作底圖** — 形式是**模擬 GLV 灰階圖**（像 fine-align 的
   template：每層 polygon 用各自 `fg_glv` 畫在 `bg_glv` 背景），下游量測就用同一張。
2. **ROI 資訊** — 讓 MMH 知道每個 pixel 屬於哪個 region / layer，以做區域遮罩量測。

**「ROI 以哪種形式交付？」→ user 答「MMH 怎麼讀最快」**（把形式決定權交給我們）。
**決定：pre-rasterized 整數 label 圖**。理由：MMH 量測時只要 `imread` 一張 label PNG，
再 `gray[label==id]` 一個 NumPy boolean index 就拿到該 region 全部像素——**零 polygon
rasterize、零 JSON parse、零 GLV 閾值猜測**。向量 ROI 每次量測都要重 rasterize；用 GLV
值編碼 region 又會被 blur / 邊緣抗鋸齒 / GLV 撞值破壞邊界精度。label 圖是最快且最穩。

**現有可複用元件（探索結論）：**
- `fine_align.render_composite_template(poi_layers, anchor, W, H, nm_per_px, bg_glv,
  blur_sigma_px)` — 已產出模擬 GLV 灰階（cv2.matchTemplate 用的 template）。
- `gds_boolean.make_mask(geom, ...)` — 把 hole-preserving geometry rasterize 成 0/255。
- `overlay_export.export_one_image(...)` — 已是 per-image 匯出單元（raw/overlay/mask），
  跑在 spawn `ProcessPoolExecutor`；一次 ROI walk 同時餵 overlay(rings) 與 mask(geom)。
- `AlignmentExportDialog.selected()` 6-tuple + `_export_overlay_images` orchestrator。

**與現有關係：取代**。gray + label **取代 F13 的 binary mask**（移除 mask 匯出選項），
與 raw/overlay 並存，共用同一次 ROI walk、同一個對齊 anchor、同一個 score-threshold
把關（沿用 F13 Q2 語義）。F13 的 mask 相關 core helper（`make_mask` 等）仍保留供 gray/
label 的 raster 路徑與既有測試使用，只是 UI 不再提供「Export GDS mask」選項。

**成功長相：** export dialog 勾「模擬 GLV 灰階」+「ROI label map」後，每張通過門檻的
影像產出 `<id>_gray.png`（含 blur 的 SEM-like 灰階）與 `<id>_label.png`（uint8，
0=背景、1..N=第 N 個 POI 層，無 blur 邊界精確），manifest 多 `gray_png`/`label_png` 欄
與一份 `label_map`（id→layer 名 + fg_glv），MMH 端 `gray[label==id]` 直接量測。

---

## Q&A Decisions

### Q1: 下游要 binary mask 還是模擬灰階？
**選擇：** 模擬 GLV 灰階圖（像 template）當工作底圖 + label ROI，**直接取代** F13 的
binary mask（移除 mask 匯出選項）。
**理由：** user 明示「GLAS 輸出的是已經對齊的 image，下游會用同一張」，並決定
「gray+label 直接取代 mask（拿掉 mask 選項）」。

### Q2: 「ROI 資訊」以哪種形式交付？
**選項：** GLV 分層編碼 / 向量 ROI 座標 / label 圖 / 由我們判斷。
**選擇：** **整數 label 圖（uint8 PNG，每 pixel 一個 region id）**。
**理由：** user 答「MMH 怎麼讀最快」→ label 圖讀取最快（單次 imread + boolean index），
不必 rasterize 向量、不必靠 GLV 閾值切（會被 blur/抗鋸齒破壞）。

### Q3: gray 與 label 的 region 邊界要不要一致？要不要 blur？
**選擇：** 兩張都由**同一組 per-layer hole-preserving geom mask** 產生：
- `gray`：paint `fg_glv` + 套 `blur_sigma_px`（SEM-like 量測底圖）。
- `label`：paint 整數 id、**不 blur**（region 邊界精確，供 boolean index）。
**理由：** 兩張共用 region 幾何 → MMH 在 gray 上量測、用 label 圈選，邊界一致；label 無
blur 保證 `label==id` 是精確 ROI。（與 matchTemplate 當下用 exterior-ring 的 template 略
有 holes 差異，但 export 不參與 matching，採 hole-preserving 與 mask 語義一致更正確。）

### Q4: gray/label 要不要也吃 score threshold？
**選擇：** 要，沿用 F13 `mask_should_export(refined, thr)`。
**理由：** 只輸出「確實對齊過且分數達標」的影像，下游免 fallback 判斷。

---

## Milestones

### M1: core — render_label_image + per-layer 共用 raster  [status: planned]

- [ ] `fine_align.render_label_image(poi_layers_ids, anchor, W, H, nm_per_px)`：
      `poi_layers_ids` = `[(geom_or_polys, label_id), ...]`，bg=0、無 blur，後層覆前層，
      回 uint8 ndarray。複用 `rasterize_layer` + `_fit_mask`（或 `make_mask` per layer）。
- [ ] 抽出/確認 gray 與 label 共用「per-layer geom→mask」的 raster 路徑，確保兩張同網格。
- [ ] 驗證：`tests/test_export_perf.py` 加 label 決定論 / id 指派 / 後層覆蓋 / 無 blur
      （邊界只有 0 與 id、無中間值）/ hole 保留 測試；模組仍 Qt-free。

### M2: overlay_export — export_one_image 加 gray / label 輸出  [status: planned]

- [ ] poi 入參擴成帶 fg_glv（`[(spec, color, fg_glv), ...]`；label id = enumerate+1）；
      cfg 加 `bg_glv` / `blur_sigma_px`。
- [ ] `export_one_image(...)` 新增 `export_gray` / `export_label` 旗標：用既有那次 ROI
      walk 拿到的 per-layer `geom`，產 `<id>_gray.png`（fg_glv+blur）與 `<id>_label.png`
      （id、no blur），anchor/nm_per_px 對齊既有 mask 路徑（含 y_min 1-px 慣例）。
- [ ] gating 沿用 `fine_align.mask_should_export(refined, thr)`。
- [ ] manifest row 加 `gray_png` / `label_png`；`OVERLAY_MANIFEST_COLS` 同步擴欄。
- [ ] `_export_pool_init` / `_export_pool_task` initargs 帶上新旗標。
- [ ] 驗證：export_one_image 在 raw-only / 有 POI / 未達門檻 各情境的寫檔正確；純函式決定論。

### M3: app UI + manifest label_map + worker 串接  [status: planned]

- [ ] `AlignmentExportDialog`：**移除**「Export GDS mask」checkbox，改為
      「Export simulated GLV grayscale (.png)」與「Export ROI label map (.png)」兩
      checkbox，沿用同一 score-threshold 區塊（threshold/count 標籤改綁 gray/label）；
      `selected()` 回 `(fmt, ids, export_raw, export_overlay, export_gray, export_label,
      score_threshold)`（mask 欄移除）。
- [ ] `OverlayExportWorker` 建構子以 `export_gray` / `export_label` 取代 `export_mask`；
      `_write_manifest` 在 JSON 加 `label_map = [{id, layer, fg_glv}, ...]`（schema bump）。
- [ ] `_export_overlay_images`：建 `[(spec, color, fg_glv)]` poi 清單、cfg 補 `bg_glv`/
      `blur_sigma_px`、組 `label_map`（由 `_poi_entries` 層名 + `poi_fgs()`），透傳 worker。
- [ ] 更新呼叫 `selected()` 的 m5 測試（已用 `*_` 解包，確認相容）。
- [ ] 驗證：`pytest tests/test_export_perf.py tests/test_gds_align_m5.py -v`；
      `py_compile`；（user 端實機：勾兩框匯出 → 檢查 gray/label/ manifest）。

### M4: 文件  [status: planned]

- [ ] README 匯出章節補 gray + label 輸出與 MMH 讀法（`gray[label==id]`）。
- [ ] CLAUDE.md §5.2 對位流程末段補一句新輸出；§8 完成後移除 [F15]。
- [ ] 本 plan 各 checkbox 勾完、SESSION_LOG 補條目。

---

## Affected Files

- `glas/core/fine_align.py`（`render_label_image` + `OVERLAY_MANIFEST_COLS`）
- `glas/core/overlay_export.py`（`export_one_image` + pool init/task）
- `glas/app/gds_align_tool.py`（`AlignmentExportDialog` / `OverlayExportWorker` /
  `_export_overlay_images` / `_write_manifest`）
- `tests/test_export_perf.py`（+ 可能 `tests/test_gds_align_m5.py`）
- `README.md`、`CLAUDE.md`、`SESSION_LOG.md`、本 plan 檔

---

## Risks / Open Questions

- **gray fill 用 geom(holes) vs alignment template 用 rings 的差異**：export 不參與
  matching，採 hole-preserving 與 mask 一致更正確；已於 Q3 決定，README 註明。
- **label id 上限**：uint8 → 最多 255 層 POI（實務遠夠）；超過則需 uint16，暫不處理。
- **記憶體/吞吐**：gray/label 各多一次 per-layer raster；沿用 F14 process pool，影響有限。
- **移除 mask 選項的相容性**：F13 的 mask UI 拿掉後，舊測試 `test_export_perf.py` /
  `test_gds_align_m5.py` 中針對 `export_mask` 的部分需同步調整（保留 `make_mask` core 測試）。

---

## 驗證方式

- [ ] 所有 milestone checkbox 已勾
- [ ] `pytest tests/test_export_perf.py tests/test_gds_align_m5.py -v` 通過
- [ ] `python3 -m py_compile` 三個改動的 core/app 檔
- [ ] 手動：開 OASIS(ROI)+選 POI+batch fine-align → export dialog 勾 gray+label →
      檢查每張達標影像有 `<id>_gray.png`/`<id>_label.png`、manifest 有欄與 label_map、
      `gray[label==1]` 圈到該層
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 最終 SESSION_LOG 條目註記 `完成 [F15]`
- 從 `CLAUDE.md` §8 移除 [F15]
- **本檔保留**作為 design history
