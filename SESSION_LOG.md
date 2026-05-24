# Session Log

---

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
