# [F22] First-run welcome dialog（5 張 ASCII / SVG 示意圖 onboarding）

> **狀態：** done (2026-06-05)
> **§8 ID：** [F22]
> **建立：** 2026-06-05
> **負責 branch：** claude/youthful-gates-WLNYJ

---

## Goal & Context

### 為什麼做

F21 + F20 把右欄 / Open OASIS 大幅簡化後，仍有一個結構性缺口：**第一次打開 GLAS 的人
完全不知道接下來該做什麼**。Guidance 條只給「Step 1 — Open OASIS」一行，但沒有解釋：

- 這 app 到底做什麼？
- 為什麼要選 PART/CHIP？
- δ 是什麼？拖什麼？
- POI 是什麼縮寫？

### 想達成什麼

App 首次啟動跳出 **一個 5 張 slide 的 WelcomeDialog**，用 ASCII / inline SVG 圖示 + 短文
快速說明整個流程：

| Slide | 標題 | 核心 |
|---|---|---|
| 1 | Welcome to GLAS | 「Align GDS layout to SEM images」一句話 + GLAS logo |
| 2 | Step 1 — Open OASIS | toolbar 橘色 `Open OASIS…` icon + wizard 三頁示意 |
| 3 | Step 2 — Pick PART / CHIP | 右欄兩個下拉 + chip-corner / FOV badge 示意 |
| 4 | Step 3 — Drag overlay → Set Offset | SEM + 半透明 GDS overlay + 紅 → 綠 對齊 + δ panel |
| 5 | Step 4 — Run fine align → Export | 批次 + 綠色 score badge + CSV 輸出 |

每張右下角 `Prev` / `Next` 或 `Got it`，左下角 `[ ] Don't show again`。

### 跟現有系統的關係

- **新增**：`WelcomeDialog(QDialog)` + `Help → Show welcome…` menu entry
- **QSettings**：`welcome_shown_v1` flag 持久化「Don't show again」狀態
- **MainWindow `__init__`**：建構完畢後（first showEvent）若 flag false → 跳 dialog
- **不動**：guidance 條 / 主視窗結構 / 既有對話框

---

## Q&A Decisions

### Q3: Slide 內容用文字 + 示意圖還是實際截圖？
**選擇：** **ASCII / inline SVG 示意圖 + 文字**
**理由：** 跨版本不會隨 UI 微調就過期；輕量；說明性足夠。

### Q4: Welcome 該不該隨 app 版本 bump 重新跳出？
**選擇：** **只首次啟動跳、勾「Don't show again」永久關閉**
**理由：** 最不吵；要看可從 Help menu 重開。

---

## Milestones

### M1: WelcomeDialog 元件 + 5 張 slide 內容 [status: done 2026-06-05]

- [x] 新增 `WelcomeDialog(QDialog)`：
  - `QStackedWidget` 5 頁
  - 上方：slide 大標題（`_FS_SECTION_HEAD`）
  - 中間：左邊 inline SVG / ASCII art（150–200 px 高）+ 右邊說明文字（word-wrap）
  - 下方：`[ ] Don't show again` + 進度指示（● ● ○ ○ ○）+ Prev/Next/Got it
- [x] 內容（5 張 slide）：標題 / SVG path / 文字定為 module-level 常數，方便 review
- [x] SVG 圖：用 Lucide 風（已在 `glas/app/icons` 系列；可直接組合
      `folder-open` / `layers` / `target` / `image` / `download` 五個 icon）
- [x] 預設 first slide 顯示，Prev/Next 按鈕依 stack index 啟用 / 禁用
- [x] 「Got it」(末頁) 與 「Skip」(任何頁) 都關 dialog；若 checkbox 勾起 → 寫
      QSettings `welcome_shown_v1 = True`

### M2: MainWindow 整合 + Help menu [status: done 2026-06-05]

- [x] MainWindow `__init__` 後在 `showEvent` 首次觸發時：若 QSettings flag false → 
      `QTimer.singleShot(0, self._show_welcome_dialog)` 確保視窗已渲染完才彈
- [x] 新增 `Help → Show welcome…` action（永遠 enabled、無視 flag 強制顯示）
- [x] About dialog 末尾加一行「First-run welcome can be re-opened via Help → Show
      welcome…」便於使用者找到

### M3: 測試 + 文件 [status: done 2026-06-05]

- [x] 新增 `tests/test_gds_align_f22.py`：
  - WelcomeDialog 啟動時 stack index = 0
  - Next/Prev 按鈕能切頁、邊界禁用
  - 勾 Don't show again + 關閉 → QSettings 寫入
  - Help → Show welcome action 找得到、能呼叫
  - MainWindow `__init__` 在 QSettings = True 時不顯示
- [x] README 加「First-run welcome explains the workflow」一行
- [x] 從 §8 移除 [F22]、SESSION_LOG 條目

---

## Affected Files

**新增：**
- `tests/test_gds_align_f22.py`

**改動：**
- `glas/app/gds_align_tool.py` — `WelcomeDialog` + Help menu action + showEvent hook
- `CLAUDE.md` §8（移除 F22）
- `README.md`
- `SESSION_LOG.md`

---

## Risks / Open Questions

- **CI / offscreen 環境**：測試需 `QT_QPA_PLATFORM=offscreen`，dialog 不能 modal-block；
  測試用 `dialog.show()` + `processEvents` 而不是 `exec()`。
- **QSettings 路徑衝突**：沿用 `QSettings("GLAS", "GLAS")` 跟 F9 dev_mode 共用 namespace。
- **SVG icon 缺檔**：`_qicon()` 已有 fallback；測試需 skip 若 icons 不在。

---

## 驗證方式

- [x] 所有 milestone checkbox 已勾
- [x] 手動驗證：清空 QSettings → 啟動 → dialog 跳出 → 翻完五頁勾 Don't show again →
      Got it → 重啟不再跳；Help → Show welcome 再次叫出
- [x] `pytest tests/` 全綠
- [x] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 在最終 SESSION_LOG 條目註記 `完成 [F22]`
- 從 `CLAUDE.md` §8 移除該任務
- **本檔保留**，作為 design history
