# [F20] Open OASIS Wizard 化（三層 modal → 單一 QWizard 三頁）

> **狀態：** done (2026-06-05)
> **§8 ID：** [F20]
> **建立：** 2026-06-05
> **負責 branch：** claude/youthful-gates-WLNYJ

---

## Goal & Context

### 為什麼做

UX 評估發現「Open OASIS」是新手第一個踩雷的環節（通關率 ≈ 30%）。現行流程：

1. toolbar 按 `Open OASIS…` → `QFileDialog` 選檔
2. 跳 **LayerFilterDialog**：標題、空輸入框、`Scan layers in file` 按鈕
3. user 按 Scan → progress dialog → 跳 **LayerPickDialog**（多選清單）→ 回原 dialog
4. 按 `Pick root cell…` → 跳 **QInputDialog** 下拉問「Root (top) cell」（無解釋）

三個 modal 連發 + 「root cell」黑話，新手卡在第 4 步。

### 想達成什麼

把這四步合併成 **單一 `QWizard` 三頁**，每頁清楚標示「Step X / 3」+ 為什麼做這步：

- **Page 1 — Pick file**：檔案選擇 + 顯示檔名 / 大小 / S_CELL_OFFSET 索引狀態
- **Page 2 — Pick layers**：Scan 按鈕 + 結果多選清單（內嵌，不再另開）+ 手動輸入 fallback
- **Page 3 — Pick root cell**：cell 名稱清單 + 預選推薦項 + 「root cell 是什麼」一行說明

Wizard `accept()` 後 MainWindow 用同一組值建 `RandomAccessReader` + 套到 UI。

### 跟現有系統的關係

- **取代**：`LayerFilterDialog` + `LayerPickDialog` + 內部 `QInputDialog` root cell prompt
- **新增**：`OpenOasisWizard(QWizard)` + 三個 `QWizardPage` 子類
- **不動**：`LayerScanWorker` / `oasis_random.RandomAccessReader` / `oasis_streamer.scan_cell_offsets`（wizard 內部沿用）
- **不動**：PART/CHIP 選擇仍在右欄（Q1 = 完全分開）

---

## Q&A Decisions

### Q1: PART/CHIP 選擇要不要拉進 wizard？
**選擇：** **完全不動 PART/CHIP，仍在右欄**
**理由：** 部件關係最乾淨；wizard 只處理 OASIS。換 chip 不必重開 wizard。

### Q2: Scan layers 怎麼觸發？
**選擇：** **進 Page 2 顯示說明 + 按鈕觸發**（不自動）
**理由：** 現狀使用者明確知道「接下來要掃描」；自動觸發可能讓誤點檔案的人浪費時間。

---

## Milestones

### M1: OpenOasisWizard 三頁骨架 [status: done 2026-06-05]

純 UI 結構，不接邏輯。

- [x] 新增 `OpenOasisWizard(QWizard)` 三頁：
  - `FilePickPage(QWizardPage)`：QLineEdit + 「Browse…」 → 檔名 / 大小 / 索引狀態 badge
  - `LayerPickPage(QWizardPage)`：說明 + `Scan layers` button + 多選 QListWidget +
    手動輸入 QLineEdit + warning label
  - `RootCellPage(QWizardPage)`：說明 + QComboBox（cell names）+ 推薦理由提示
- [x] 註冊 `registerField` 把每頁的關鍵欄位（`file*`, `layers*`, `root_cell*`）露給
      caller 取用
- [x] `isComplete()` 控制 Next/Finish 按鈕（檔案存在 / 至少 1 個 layer / 有 root cell）
- [x] 視覺：QWizard banner 圖（沿用 GLAS wordmark）、每頁標題寫「Step N — 動作」

### M2: 接 scan 邏輯 + index 狀態檢查 [status: done 2026-06-05]

- [x] FilePickPage：選檔後 `oasis_streamer.scan_cell_offsets` 快速檢查
      `S_CELL_OFFSET` 是否存在；無 → 黃色 warning「需先用 KLayout strict-mode 另存」
      但仍允許 Next（讓 user 看到後面頁面的解釋）
- [x] LayerPickPage：`Scan layers` button → `LayerScanWorker`（同現有），結果填入
      QListWidget（多選 + checkbox）；user 仍可手動 key
- [x] LayerPickPage 結果存到 wizard field（`("layers", List[Tuple[int,int]])`）+
      原始 `scan_result` dict 給 Page 3 取 cell names
- [x] RootCellPage `initializePage()`：從 wizard 取 scan_result 的 `by_refnum` →
      cell name list；default 選含 `top`/`merge` 的；補一行「Root cell = 整個 layout
      的最頂層（通常 chip 整片）；不確定就用預選」

### M3: MainWindow 接線 + 移除舊 dialog [status: done 2026-06-05]

- [x] `_on_open_roi` 大瘦身：開 wizard → accept 後取 `wizard.file_path()` /
      `wizard.layer_keys()` / `wizard.root_cell()` 三個值，後面 RAR 建構 + has_offsets
      檢查 + state 寫入跟現在一樣
- [x] 刪 `LayerFilterDialog` + `LayerPickDialog`（grep 確認沒別處引用）
- [x] guidance Step 1 文字小調：「Open an OASIS — wizard 引導你選檔 / layer / root cell」

### M4: 測試 + 文件 [status: done 2026-06-05]

- [x] 新增 `tests/test_gds_align_f20.py`：
  - FilePickPage 接受存在檔案、拒絕不存在
  - LayerPickPage isComplete 依 layers 數量
  - RootCellPage initializePage 把 by_refnum keys 填入 combobox + 預選含 top
  - MainWindow `_on_open_roi` 整合測（用 monkeypatch 假 wizard accept，驗證 RAR 建構）
- [x] 全套件 pytest 通過（預期 ~656 項）
- [x] 從 §8 移除 [F20]、SESSION_LOG 條目、`README.md` 描述微調

---

## Affected Files

**新增：**
- `tests/test_gds_align_f20.py`

**改動：**
- `glas/app/gds_align_tool.py` — 新增 `OpenOasisWizard` + 三個 page 類；移除
  `LayerFilterDialog` / `LayerPickDialog`；`_on_open_roi` 改寫；guidance 文字
- `CLAUDE.md` §8（移除 F20）
- `README.md`（使用流程「Open OASIS → wizard 三頁」）
- `SESSION_LOG.md`

---

## Risks / Open Questions

- **取消 + 已建 RAR**：wizard 在 Page 3 顯示 cell names 前需要 by_refnum；可以直接從
  `scan_cell_offsets` 結果拿（不必先 build RAR）。RAR 仍在 wizard accept 後才建。
- **無索引檔案**：FilePickPage 允許 Next，但若 has_offsets 失敗（accept 後檢查）走原
  fallback 訊息。
- **多檔案 stress test**：wizard 中斷 / 重開 / 換檔的記憶體釋放路徑要乾淨。

---

## 驗證方式

- [x] 所有 milestone checkbox 已勾
- [x] 手動驗證：toolbar `Open OASIS…` → 三頁 wizard 走通 → SEM 點 defect 可載 ROI
- [x] `pytest tests/` 全綠
- [x] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 在最終 SESSION_LOG 條目註記 `完成 [F20]`
- 從 `CLAUDE.md` §8 移除該任務
- **本檔保留**，作為 design history
