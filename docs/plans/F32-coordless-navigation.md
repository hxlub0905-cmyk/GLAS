# [F32] 無圖 / 無 KLARF / 純 GDS 三種缺料模式都能「移過去並顯示」+ bundle 對齊 d4t 現行格式

> **狀態：** in progress — M1–M6 code 完成（2026-10-14），待 user 真機驗收
> **§8 ID：** [F32]
> **建立：** 2026-10-14
> **負責 branch：** claude/epic-goodall-guq8mb

---

## Goal & Context

GLAS 今天的導航鏈是「KLARF 給 XREL/YREL → 點影像 → `klarf_to_gds` → ROI walk → SemViewer 畫 overlay」，
**每一環都假設兩樣東西同時在**：(1) 影像檔在磁碟上、(2) 座標來自 KLARF。user 實際有三種缺料情境：

| 模式 | 有 GDS | 有 KLARF | 有影像檔 | 今天的行為 |
|---|---|---|---|---|
| (a) | ✓ | ✓ | ✗ | 無檔名的 defect 被 `load_klarf` 直接 `continue` 丟掉（0 張）；有檔名但檔不在時雖能跳位，但 `SemViewer` 沒 pixmap 就**什麼都不畫**（`_compute_geometry` 回 None → 只畫 empty state），overlay / 拖動 / 縮放 / HUD 全失效，auto nm/px 也回 0 |
| (b) | ✓ | ✗ | ✓ | `load_folder` 的影像 `xrel/yrel=None`，沒有任何地方能設；所有跳位（`_jump_to_image` / `_current_image_gds` / `_coarse_gds` / fine-align / overview / fit）都 `has_coords` 擋掉 |
| (c) | ✓ | ✗ | ✗ | 工具列 Goto 只驅動預設**隱藏**的 `GdsCanvas`、不設 `_current_sem` → `_update_overlay` 清掉 overlay，SemViewer 維持空白 |

目標：三種模式都能「鍵入 / 由 KLARF 取得座標 → 跳到對應 GDS 位置 → 在 SemViewer 看到 layout」，
操作與有圖有 KLARF 時**一致**（同一個 viewer、同一套拖動 / 縮放 / Set Offset）。
硬依賴只拆兩個點：**SemViewer 無 pixmap 時畫虛擬底圖**、**座標可以不來自 KLARF 檔**；其餘路徑不動
（§7 的 `klarf_to_gds` 方向、overlay sign、fold invariant 全部沿用）。

另一半（user 同場要求）：**GLAS 的搬運包升級成 d4t 現行格式**。GLAS 的 `bundle/GLAS_bundle.py` 是 2026-08-17
從 ADEPT 移植的版本：解包程式檔頭 / 訊息是**中文**、資料區是整包 lzma+base64。d4t 之後踩到兩件事並修掉
（AGENTS.md §2 記錄完整）：
1. **2026-09-03**：公司機拿包的方式是「瀏覽器複製 → 記事本存檔」，中文 Windows 記事本存成 ANSI（cp950），
   包裡只要有一個非 ASCII 字元，Python 就 `SyntaxError: Non-UTF-8 code`。GLAS 現在的解包程式**就有中文**。
2. 整包壓成一個流 → 改一行整份 base64 變樣 → git 每個 commit 存完整一份（d4t 實測 bundle 歷史佔 pack 98%）。
   改成**逐檔** raw LZMA2 + base85（`#ENC lzma2+b85/file`，2026-10-06 起），每次 commit 只多那一檔的那一段。

---

## Q&A Decisions

### Q1: 模式 (a)(c) 沒有 SEM 影像時，「移過去並顯示」的畫面怎麼呈現？
**選項：** A. SemViewer 畫虛擬底圖 / B. 自動切到 GDS canvas 檢視 / C. 兩者都要
**選擇：** A
**理由：** 有圖沒圖操作一致（同一個 viewer、拖動 / 縮放 / HUD / Set Offset 全部照常）。虛擬底圖尺寸由
FOV + nm/px 推出，`native_size()` 回虛擬尺寸 → 既有 `_effective_nm_per_px` auto 路徑（FOV 寬 / 像素寬）
不用改就自洽。GDS canvas 檢視仍可手動切（G 鍵），不自動切。

### Q2: 手動 key 座標用哪一種系統？
**選項：** A. GDS µm（layout 座標）/ B. KLARF die-corner nm（XREL/YREL）/ C. 兩種都要（下拉切換）
**選擇：** C
**理由：** 純 GDS 時 GDS µm 直覺（與工具列 Goto、klayout Ctrl+G 一致）；「手上有 KLARF 數字但沒檔案」時
key XREL/YREL 比自己換算安全。**內部統一存成 die-corner nm**（`SemImage.xrel/yrel`）走既有路徑：GDS µm 輸入
經 `gds_to_klarf`（`klarf_to_gds` 的反函數，**新增**、不碰原函式）用目前 chip_corner 反推。
推論：keyed 影像之後跟 KLARF 影像一樣受 origin δ / refined 影響 → 拖動 + Set Offset 的 fold 不變式對它們成立。

### Q3: 模式 (b) 座標怎麼綁到影像？
**選項：** A. 選中影像後逐張 key / B. 逐張 key + CSV 批次匯入 / C. 單一全域「目前位置」
**選擇：** A
**理由：** 最小閉環；CSV 匯入列為 backlog（Risks 區）。key 完立刻跳位、列表 "no coords" 徽章消失、
可參與 fine-align / Export（`has_coords` 變 True 自然納入，不另開路徑）。

### Q4: bundle.py 的範圍？
**選項：** A. 把 GLAS 的 bundle 升級成 d4t 現行格式 / B. 幫其他 repo 新建 / C. 兩者
**選擇：** A
**理由：** d4t 已有 `bundle/d4t_bundle.py`（CLAUDE.md 明寫「不要再造第二個 bundle」），且比 GLAS 的新；
GLAS 那份有 cp950 地雷（見上）。其他 repo 不在本次範圍。

---

## 設計摘要

### 資料層（`sem_loader`）
- `SemImage` 新增 `coord_source: str = ""`：`"klarf"`（載入時設）/ `"manual-gds"` / `"manual-klarf"`，
  供列表徽章與日後 manifest 使用；預設 `""` 不影響既有 dataclass 比較。
- `load_klarf` rSEM 路徑：**有座標但無檔名**的 defect 不再丟掉 → `filename=""`、`file_path=None`
  （`exists` False、`has_coords` True）。既無檔名也無座標的才丟（沒有任何可做的事）。
- EBI 路徑：`IMAGECOUNT 0` 但有座標的 defect 同樣保留（`pages=()`）。
- **coords-only 後備**：rSEM 路徑產 0 張、但 `klarf_doc` 讀得動（典型：1.2 flat KLARF 沒 TIFF）→ 以
  `klarf_doc` 的 DEFECTID/XREL/YREL 產無檔 `SemImage`（單位換算同 F31：1.2 ×1000）。**只在 rSEM 給 0 張時
  接手**，rSEM 有結果就一個位元組都不變（F31 的「新路徑只接舊路徑服務不到的檔」原則）。
- `load_folder` 不變。

### 座標層（`gds_fov`）
- 新增 `gds_to_klarf(gx, gy, chip_corner_x, chip_corner_y, *, flip_y=False)`：`klarf_to_gds` 的反函數，
  round-trip 測試護欄。**`klarf_to_gds` 本身一行不改**（§7）。

### 顯示層（`SemViewer`）
- 新增 `set_blank(width_px, height_px, caption)`：`_pixmap=None`、`_blank_size=(w,h)`。
  `_compute_geometry` / `native_size` / `_apply_zoom` 在無 pixmap 時改讀 `_blank_size`；`paintEvent` 畫深色
  底板（代替 `drawPixmap`）+ 中央淡字「no SEM image — layout only」，overlay / 十字 / HUD / 說明列照常。
  `set_image` 清掉 `_blank_size`（有圖就不是 blank）。CTA 按鈕在 blank 模式隱藏（右欄 Load SEM… 仍可用）。
- 虛擬尺寸由 MainWindow 決定：手動 nm/px 時 `w=fov_w/nm, h=fov_h/nm`；否則寬 1024、高依 FOV 長寬比。
  → `_effective_nm_per_px` 的 auto 路徑（`fov_w / native_w`）自動對上，不需特例。

### 應用層（`MainWindow` / `SemPanel`）
- `_show_sem_or_blank(img)`：`set_image` 後若 viewer 無 pixmap 且 `img.has_coords` → `set_blank(...)`。
  `_on_sem_image_selected` / `_on_align_page_changed` / `_on_coord_changed`（FOV 變→虛擬尺寸變）走它。
- **虛擬目標**：純 GDS 的 Goto 產生一個不在 `_sem_images` 裡的 `SemImage(image_id="goto", file_path=None,
  coord_source="manual-gds")` 當 `_current_sem` → `_current_image_gds` / `_update_overlay` / ROI load 全走
  既有路徑；不進列表 → overview 點、Export、batch 自然排除。
- 工具列 Goto（`_on_goto_gds`）保留 ±25 µm canvas 行為，**加上**設虛擬目標 + blank viewer，所以在預設 SEM 檢視
  也看得到 layout。
- 新右欄區塊 `CoordinateKeyPanel`（放在 ALIGNMENT δ 下、影像列表上）：單位下拉〔GDS µm｜KLARF nm (XREL/YREL)〕、
  X / Y 輸入、「Apply to selected image」、「Go (no image)」。選中影像時以所選單位**回填**目前座標（兼讀出）。
  Apply → 寫回 `img.xrel/yrel`（die-corner nm）+ `coord_source` → 列表徽章更新 → 同 `_on_sem_image_selected`
  跳位 + ROI。Go → 虛擬目標（FOV 視窗 ROI）。
- 列表徽章：`no coords`（既有）之外新增 `no image`（有座標、檔不在）與 `keyed`（手動座標）。
- `_update_guidance` Step 2 文案補「或在右欄 key 座標直接看 layout」；`_current_sem` 為虛擬目標時不卡在 Step 2。
  `_refresh_action_states`：Load GDS ROI 在 `_rar` 存在且（有座標影像 **或** 目前為虛擬目標）時啟用；
  CoordinateKeyPanel 的 Apply 需有選中影像、Go 需 `has_doc`。
- 小修：`_on_load_folder` 清 `_klarf_path`（folder 載入後 manifest 不該帶上一個 KLARF 路徑）。

### bundle（`tools/`）
- `make_text_bundle.py`：EXTRACTOR 全 ASCII 英文 + `BUILD <sha12> <date>` 檔頭；預設 `#ENC lzma2+b85/file`
  （逐檔 raw LZMA2 preset 6 + base85，`#F <sha> <n> <path>` + `#B…`）；解包程式仍認得舊的
  `#ENC lzma+base64`（整包）與 `#ENC text`（含 `#X` base64 記錄）→ 已搬進公司機的舊包照樣解。
  `collect()` 保留 GLAS 的 `git ls-files -z`（中文檔名）與「二進位不拒絕」（逐檔壓縮下 PDF 不需特判）。
  `_slice`/`--split` 保留；拿掉 `--no-binary`（全 base85 之後沒有「只有 PDF 是 base64」的區別）。
- `release.py`：`build(..., compress=False)`（ast 測試守著）；`_bundle_entries` 認新格式；size 報告改 MB、
  不印百分比（d4t：「745% 讀起來就是警報」）。
- `glas/core/buildinfo.py`：`build_id()` = `tools/FILELIST.txt` blob SHA 前 12 碼（與包檔頭同一個數）；
  status bar 品牌 chip 的 tooltip 帶 `build xxxx`，`python main.py --version` 印同一行 → 公司機能回答
  「我這台跑的是哪一版」。
- 測試移植：純 ASCII、cp950/BOM/CRLF 存一次再解、出貨不壓縮（ast）、repo 內那份是 per-file 形式、
  base85 記錄被改一個字 → 一個檔都不落地。

---

## Milestones

### M1: 資料層 — KLARF 無圖保留 + 反向座標函式  [status: done]

- [x] `gds_fov.gds_to_klarf` + round-trip 測試（含 `flip_y`）
- [x] `SemImage.coord_source`；`load_klarf` / `_load_klarf_ebi` 設 `"klarf"`
- [x] rSEM 路徑：有座標無檔名 → 保留（`file_path=None`）；無座標無檔名 → 丟
- [x] EBI 路徑：`IMAGECOUNT 0` 有座標 → 保留
- [x] coords-only 後備（rSEM 0 張 + `klarf_doc` 讀得動）
- [x] 測試：改 `test_defects_without_image_skipped` 為新語意 + 新增無檔保留 / 後備 / rSEM 不變三組
- [x] 驗證：`pytest tests/test_sem_loader.py tests/test_gds_fov*.py -v`

### M2: SemViewer 虛擬底圖  [status: done]

- [x] `set_blank()` / `_blank_size`；`_compute_geometry` / `native_size` / `_apply_zoom` 無 pixmap 走虛擬尺寸
- [x] `paintEvent` blank 分支（深色底板 + 淡字），overlay / HUD / 十字照常；CTA 隱藏
- [x] `set_image` 清 `_blank_size`
- [x] 測試（offscreen）：blank 後 `_compute_geometry` 非 None、`native_size` 回虛擬尺寸、`grab()` 後 `_view_to_world`
  可用、fold 不變式在 blank 下成立、`set_image(有圖)` 清掉 blank
- [x] 驗證：`pytest tests/test_gds_align_m4a.py tests/test_f32_coordless.py -v`

### M3: MainWindow 接線 — 無圖影像 + 虛擬目標 + Goto  [status: done]

- [x] `_virtual_canvas_size()` + `_show_sem_or_blank(img)`；接進 `_on_sem_image_selected` / `_on_align_page_changed` /
  `_on_coord_changed`
- [x] `_goto_virtual(gx, gy, *, half)`：虛擬 `SemImage` 當 `_current_sem`；`_on_goto_gds` 改走它（保留 ±25 µm canvas）
- [x] `_update_guidance` / `_refresh_action_states` 對虛擬目標與無圖影像的放行
- [x] `_on_load_folder` 清 `_klarf_path`
- [x] 列表徽章：`no image` / `keyed`
- [x] 測試：選無檔有座標影像 → viewer blank + `_current_image_gds()` 非 None；Goto → `_current_sem` 虛擬、位置正確、
  不進 `_sem_images`；guidance 不卡 Step 2
- [x] 驗證：`pytest tests/test_f32_coordless.py tests/test_gds_align_m7.py tests/test_perf_quickwins.py -v`

### M4: CoordinateKeyPanel（逐張 key）  [status: done]

- [x] 右欄新 widget：單位下拉 / X Y 輸入 / Apply / Go；選中影像時回填
- [x] `MainWindow._on_apply_keyed_coord`：寫 `xrel/yrel`（die-corner nm）+ `coord_source` → 徽章 → 跳位 + ROI
- [x] `MainWindow._on_goto_keyed`：→ `_goto_virtual`（FOV 視窗）
- [x] 測試：folder 影像 Apply（兩種單位）→ `has_coords`、`_current_image_gds()` 等於輸入（GDS µm 單位下與 δ 的關係）、
  Export 候選納入（`fine_align` 的 `has_coords` 過濾）；Go 等同 M3 虛擬目標
- [x] 驗證：`pytest tests/test_f32_coordless.py -v` + 全套

### M5: bundle 對齊 d4t 現行格式  [status: done]

- [x] `make_text_bundle.py`：ASCII EXTRACTOR + BUILD 檔頭 + per-file lzma2/b85 預設 + 舊格式解包相容
- [x] `release.py`：`compress=False`、`_bundle_entries` 新格式、MB 報告
- [x] `glas/core/buildinfo.py` + status chip tooltip + `main.py --version`
- [x] `tests/test_bundle_tools.py`：移植 d4t 四條守門 + 改既有 round-trip / tamper 測試
- [x] README 離線搬運段落更新（格式 / 為什麼純 ASCII）
- [x] `git add -A && python tools/release.py && git add -A` 重產，確認 `test_the_transfer_files_are_up_to_date` 綠
- [x] 驗證：`pytest tests/test_bundle_tools.py -v`；乾淨目錄解包逐檔比對

### M6: 文件 + 收尾  [status: done]

- [x] CLAUDE.md §4（sem_loader 說明 / buildinfo）、§7 加「`gds_to_klarf` 必須是 `klarf_to_gds` 的精確反函數」
- [x] README 功能總覽補三種缺料模式的操作
- [x] SESSION_LOG 條目、§8 註冊 → 完成後移除
- [x] 全套 `pytest tests/` 綠

---

## Affected Files

- `glas/core/gds_fov.py`（+`gds_to_klarf`）、`glas/core/buildinfo.py`（新）
- 實作備註：M5 多了一條 d4t 沒有的事 —— GLAS 有中文檔名，`#F` 檔頭的路徑要 percent-encode 才能整份 ASCII
- `glas/app/sem_loader.py`、`glas/app/gds_align_tool.py`（SemViewer / SemPanel / MainWindow / 新 CoordinateKeyPanel）
- `main.py`（`--version`）
- `tools/make_text_bundle.py`、`tools/release.py`、`tools/FILELIST.txt`、`bundle/GLAS_bundle.py`（重產）
- `tests/test_sem_loader.py`、`tests/test_gds_fov*.py`、`tests/test_f32_coordless.py`（新）、`tests/test_bundle_tools.py`
- `README.md`、`CLAUDE.md`、`SESSION_LOG.md`、本檔

---

## Risks / Open Questions

- **keyed 座標不持久化**：關掉 GLAS 就沒了。若 user 常對同一資料夾重 key，下一步是 sidecar JSON（`<folder>/glas_coords.json`）
  或 CSV 匯入（Q3 的 B 選項）。本次不做，列 backlog。
- **manifest 不帶 `coord_source`**：加欄位要 bump schema v4→v5 並通知 ADEPT 側；keyed 影像目前在 manifest 裡與 KLARF 影像無法區分。
  待 user 決定要不要納入契約。
- **虛擬底圖上的左鍵拖動**：沒有影像可對位，拖動只是移 overlay；不擋（保持操作一致），但 Set Offset 在純 GDS 模式下無意義 —— 文案提示即可。
- **coords-only 後備的誤判**：若某 1.8 rSEM KLARF 的檔名欄解析失敗產 0 張，後備會把它當無圖 KLARF 載入（有座標無檔）——
  比今天的 0 張好，且狀態列會寫「0 with image」讓人看得出來。
- **bundle 格式切換後舊包**：公司機上已解開的舊包不受影響（每包帶自己的解包程式）；repo 歷史裡舊格式的 pack 體積不回收（不改歷史）。

---

## 驗證方式

- [x] 所有 milestone checkbox 已勾
- [x] `pytest tests/ -v` 全綠（1,003 passed, 4 skipped）（含 `test_export_fused` byte-identical 護欄不受影響）
- [ ] 手動：(a) 載一份把影像刪掉的 KLARF → 點 defect → viewer 出現深色底圖 + layout，拖動 / 縮放可用；
  (b) 載資料夾 → 選一張 → key GDS µm → 跳位、徽章變 keyed、Run fine align 可跑；
  (c) 不載 SEM → 工具列 Goto 或右欄 Go → viewer 顯示 layout
- [x] 包：乾淨目錄 `python GLAS_bundle.py` 解開逐位元組比對；`iconv -t cp950` 存一次再 `--list` 仍成功
- [x] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 在最終 SESSION_LOG 條目註記 `完成 [F32]`
- 從 `CLAUDE.md` §8 移除該任務
- **本檔保留**，作為 design history
