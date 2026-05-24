# [F<n>] 一句話標題

> **狀態：** planned | in progress | done (YYYY-MM-DD)
> **§8 ID：** [Fxx] / [Bxx]
> **建立：** YYYY-MM-DD
> **負責 branch：** claude/...

---

## Goal & Context

說清楚：
- 為什麼做這件事（解決什麼問題、來自什麼觀察）
- 想達成什麼具體成果（成功長什麼樣子）
- 跟現有系統的關係（是延伸、取代、並存？）

---

## Q&A Decisions

把 plan 階段用 `AskUserQuestion` 與使用者來回確定的關鍵岔路寫下來。每條一段：

### Q1: <問題>
**選項：** A / B / C
**選擇：** B
**理由：** ...

### Q2: ...

---

## Milestones

> 每個 milestone 以「一個 session 可完成」為粒度切。
> 子任務用 `- [ ]` checkbox，完成時改 `- [x]`。

### M1: <milestone 名稱>  [status: planned]

- [ ] subtask 1
- [ ] subtask 2
- [ ] 驗證：<測試 / 手動步驟 / 通過條件>

### M2: <milestone 名稱>  [status: planned]

- [ ] ...

### M3: ...

---

## Affected Files

預期會改動或新增的檔案（隨實作補充）：

- `src/...`
- `tests/...`
- `docs/...`

---

## Risks / Open Questions

- 已知風險：...
- 待 user 後續確認：...
- 外部依賴：...

---

## 驗證方式

整個 feature 結束時的 end-to-end 驗證：

- [ ] 所有 milestone checkbox 已勾
- [ ] `pytest tests/<相關> -v` 通過
- [ ] 手動驗證：<操作步驟>
- [ ] `SESSION_LOG.md` 有對應紀錄

---

## 完成後

- 在最終 SESSION_LOG 條目註記 `完成 [F<n>]`
- 從 `CLAUDE.md` §8 移除該任務
- **本檔保留**，作為 design history
