# [F10] OASIS debug mode：載入/匯出雙向診斷（可複製報告 + sidecar）

> **狀態：** approved — done, 待本地驗收
> **§8 ID：** [F10]
> **建立：** 2026-05-26
> **負責 branch：** claude/adoring-cannon-oKZKo

---

## Goal & Context

**動機：** 開發 OASIS streamer 時 parse 很常出錯；F9 又新增了 OASIS writer。user 目前手上還沒有
production .oas，希望先備好 debug mode，等拿到資料一出錯就能**第一時間把診斷貼回來**。

**成功長相：** 載入（reader）與匯出（writer）兩端出問題時，都能拿到一份**可複製的純文字診斷報告**，
並自動落成 sidecar 檔，方便整段貼出。

**與現有系統關係：** 純新增診斷層。底層既有設施已具備（streamer 的 `_error_context` hex 視窗 + 游標、
`oasis_random.set_debug`、`--debug`）；F10 把它們在 GUI 以可複製/落檔形式呈現，並掛上 F9 的開發者模式。

---

## Q&A Decisions

### Q1: 針對哪個方向？
**選擇：兩端都要**（載入 production .oas 解析 + OASIS 匯出 writer）。

### Q2: 診斷輸出形式？
**選擇：兩者都要**——落 `.debug.txt` sidecar + app 內可複製對話框。

---

## Milestones

### M1: Core 診斷模組 (`glas/core/oasis_debug.py`，Qt-free)  [status: done]

- [x] `report_file(path, *, sent_layers=None, max_records)`：走 `oasis_streamer` 統計 record histogram、
      per-layer rect/poly 數、START unit/offset_flag、cell names；**永不拋例外**——decode 出錯則把
      streamer 的 hex-context（`OasisFormatError` 內建）+ traceback 收進報告。
- [x] `sent_layers` 給定時做 round-trip 比對（每 layer 送出 vs 讀回形狀數 → OK / MISMATCH）。

### M2: 匯出端接線  [status: done, 待本地驗收]

- [x] `layout_export.export_layers(..., debug=False)` 改回傳 `(layers_written, report|None)`；debug 時回讀
      寫出檔產報告。
- [x] `OasisExportDialog` 加「Debug: re-read + report」checkbox；`_on_export_oasis` 取報告 → 落
      `<檔>.oas.debug.txt` + `DebugReportDialog` 顯示。

### M3: 載入端接線  [status: done, 待本地驗收]

- [x] `_on_diagnose_oasis`：File 選單「Diagnose OASIS file…」（dev-mode 才顯示）→ 選 .oas →
      `report_file` → sidecar + `DebugReportDialog`。
- [x] 載入失敗（`_on_open_roi` 開檔 except、`_on_roi_failed` worker 失敗）在 dev mode 經 `_show_load_error`：
      原錯誤 + **自動對該檔跑 `report_file`** → sidecar + 可複製對話框；非 dev 維持原 `QMessageBox.critical`。

### M4: 共用 UI + 測試 + 文件  [status: done, 待本地驗收]

- [x] `DebugReportDialog`：唯讀 monospace `QPlainTextEdit` + Copy to clipboard + Saved-to 標示。
- [x] `tests/test_oasis_debug.py`（well-formed 報告、round-trip 比對、truncated 捕捉錯誤、缺檔）；
      `tests/test_layout_export.py` 更新 `(n, report)` 回傳 + debug 報告測試。
- [x] `py_compile` 全過；`pytest` + GUI 待 user 本地。

---

## Affected Files

- `glas/core/oasis_debug.py`（新）、`glas/core/layout_export.py`（debug 回傳）、`glas/app/gds_align_tool.py`
- `tests/test_oasis_debug.py`（新）、`tests/test_layout_export.py`
- `README.md` / `CLAUDE.md` / `docs/plans/F10-debug-mode.md` / `SESSION_LOG.md`

---

## Risks / Open Questions

- 載入 worker 目前只 emit `str(exc)`（已含 streamer hex-context，無 traceback）；dev-mode 失敗時改由
  `_show_load_error` 對該檔重跑 `report_file` 補足結構掃描 + 錯誤上下文，足以診斷。
- 大檔診斷掃描有 `max_records` 上限（預設 50 萬）避免卡死。
- GUI / 真實 .oas 驗收待 user（沙箱無 numpy/PyQt6）。

---

## 驗證方式

- [ ] `pytest tests/test_oasis_debug.py tests/test_layout_export.py -v` 綠
- [ ] 手動：開發者模式 → 匯出勾 Debug 看報告 + sidecar；File → Diagnose OASIS file… 看報告；
      故意餵壞/不支援的 .oas 看載入失敗的可複製報告
- [ ] `SESSION_LOG.md` 有紀錄

---

## 完成後

- SESSION_LOG 註記 `完成 [F10]`；從 `CLAUDE.md` §8 移除；本檔保留作 design history。
