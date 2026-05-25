# Session Log

---

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
