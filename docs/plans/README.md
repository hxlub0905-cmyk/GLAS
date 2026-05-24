# GLAS Plans

本目錄存放跨 session 的功能 / 重構企劃書。每份 plan 對應 `CLAUDE.md` §8 任務清單中的一個 `[Fxx]`（Feature / Refactor）或大型 `[Bxx]` (Bug)。

## 命名規則

```
<feature-id>-<slug>.md
```

- `feature-id`：對應 §8 任務 ID，例如 `F1`、`F2`、`B5`
- `slug`：kebab-case 短描述，3-5 個字以內

範例：

```
F1-gds-import.md
F2-recipe-sqlite-migration.md
B12-klarf-windows-path.md
```

## 撰寫流程（強制）

依 `CLAUDE.md` §11「規劃流程」執行：

1. **不可直接動工**。先用 Explore agent 探索相關區域
2. 用 `AskUserQuestion`（選擇題 + 「Other」自由輸入）與使用者來回確認關鍵岔路
3. 討論收斂後才以 `_template.md` 為起點產生新 plan
4. plan 採 **Milestone + checkbox** 結構
5. user 核准後才開工
6. 每個 milestone 完成 → 在 plan 中勾掉對應 checkbox + 在 `SESSION_LOG.md` 補條目

## 範本

新 plan 一律從 [`_template.md`](./_template.md) 複製起步。

## Plan 生命週期

| 狀態 | §8 表現 | plan 檔位置 |
|---|---|---|
| 待辦 | `- [F1] ... — see @docs/plans/F1-xxx.md — 待辦` | 已存在 |
| 進行中 | `- [F1] ... — see @docs/plans/F1-xxx.md — 進行中 (M2/5)` | 持續更新 checkbox |
| 完成 | 從 §8 刪除該條 | **保留 plan 檔**，作為 GDS 等大型 feature 的歷史設計紀錄 |

> 已完成的 plan 不刪 — 是有價值的 design history。`§8` 從清單移除即可，git history + plan 檔本身會留紀錄。
