# GLAS UI 修正 Prompt

## 背景

請先閱讀 `glas/app/gds_align_tool.py` 了解現有結構，
然後依序修正以下五個 UI 問題。
每項修改完請跑 `python3 -m py_compile glas/app/gds_align_tool.py` 確認語法正確。

---

## 問題 1：右欄 Coordinate Setup 預設改為收起

**現況：** `SemPanel.__init__()` 裡 `_wrap_section("Coordinate Setup", ..., collapsed=False)`，
導致六個 µm 欄位一開始就全部展開，把 image list 擠到看不見。

**修正：** 把 `collapsed=False` 改成 `collapsed=True`。

```python
# 找到這行（約在 SemPanel.__init__ 內）：
self._coord_section = self._wrap_section(
    "Coordinate Setup", self.coord_setup, collapsed=False)

# 改成：
self._coord_section = self._wrap_section(
    "Coordinate Setup", self.coord_setup, collapsed=True)
```

同時，`_maybe_collapse_coord_setup()` 原本在第一次 jump 後才收起，
現在預設已收起，所以這個 method 改成「第一次展開後才不再自動干預」——
邏輯不需要改，但確認 `_coord_collapsed_once` 初始值為 `True`：

```python
# 在 MainWindow.__init__() 內，找到：
# （若沒有這行則新增）
self._coord_collapsed_once = True
```

---

## 問題 2：左欄 LAYERS empty state 加視覺引導

**現況：** `LayerPanel._show_empty_hint()` 只有一行小字
`"Open an OASIS to list layers."`，視覺重量太輕。

**修正：** 替換 `_show_empty_hint()` 方法，加入圖示 + 主文 + 次文三層結構：

```python
def _show_empty_hint(self) -> None:
    self.list.clear()

    # 圖示行
    icon_item = QListWidgetItem()
    icon_item.setFlags(Qt.ItemFlag.NoItemFlags)
    icon_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    self.list.addItem(icon_item)

    # 主文
    title_item = QListWidgetItem("Open an OASIS")
    title_item.setFlags(Qt.ItemFlag.NoItemFlags)
    title_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    title_item.setForeground(QColor(_TK_TEXT_SEC))
    font = title_item.font()
    font.setPixelSize(_FS_LABEL)
    font.setBold(True)
    title_item.setFont(font)
    self.list.addItem(title_item)

    # 次文
    hint_item = QListWidgetItem("toolbar → Open OASIS…")
    hint_item.setFlags(Qt.ItemFlag.NoItemFlags)
    hint_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    hint_item.setForeground(QColor(_TK_TEXT_HINT))
    font2 = hint_item.font()
    font2.setPixelSize(_FS_CAPTION)
    hint_item.setFont(font2)
    self.list.addItem(hint_item)
```

---

## 問題 3：Set Offset / Clear Offset 移入右欄 SEM panel

**現況：** 這兩個按鈕放在中央視圖下方（`center_layout.addWidget(offset_w)`），
跟整體 layout 關係不清楚，使用者不知道這是給 SEM overlay 用的。

**修正：** 把 offset row 從中央移到右欄 `SemPanel` 內，
放在 image list 下方、Load GDS ROI 按鈕上方。

**步驟 A — 從 `MainWindow.__init__()` 移除原本的 offset_w：**

找到並刪除以下程式碼（約 8 行）：
```python
offset_row = QHBoxLayout()
offset_row.setContentsMargins(8, 4, 8, 4)
self._set_offset_btn = QPushButton("Set Offset")
self._set_offset_btn.setToolTip(...)
self._set_offset_btn.clicked.connect(self._on_set_offset)
self._clear_offset_btn = QPushButton("Clear Offset")
self._clear_offset_btn.setToolTip(...)
self._clear_offset_btn.clicked.connect(self._on_clear_offset)
offset_row.addWidget(self._set_offset_btn)
offset_row.addWidget(self._clear_offset_btn)
offset_row.addStretch(1)
offset_w = QWidget(center)
offset_w.setLayout(offset_row)
center_layout.addWidget(offset_w)
```

**步驟 B — 在 `SemPanel.__init__()` 加入 offset row：**

找到 `v.addWidget(self.load_roi_btn)` 這行，在它之前插入：

```python
# Set / Clear Offset — 屬於 SEM 對位流程，放在 image list 下方
offset_row = QHBoxLayout()
offset_row.setSpacing(6)
self.set_offset_btn = QPushButton("Set Offset")
self.set_offset_btn.setToolTip(
    "Fold the current GDS drag into the global origin correction δ.")
self.clear_offset_btn = QPushButton("Clear Offset")
self.clear_offset_btn.setToolTip("Reset the global δ and drag to zero.")
offset_row.addWidget(self.set_offset_btn)
offset_row.addWidget(self.clear_offset_btn)
v.addLayout(offset_row)
```

**步驟 C — 更新 `MainWindow.__init__()` 的 signal 接線：**

把原本接 `self._set_offset_btn` / `self._clear_offset_btn` 的兩行改為：
```python
self.sem_panel.set_offset_btn.clicked.connect(self._on_set_offset)
self.sem_panel.clear_offset_btn.clicked.connect(self._on_clear_offset)
```

**步驟 D — 更新所有 `self._set_offset_btn.setEnabled` /
`self._clear_offset_btn.setEnabled` 的引用（若有的話）改成
`self.sem_panel.set_offset_btn` / `self.sem_panel.clear_offset_btn`。**

---

## 問題 4：Toolbar group label 樣式加強

**現況：** FILE / VIEW MODE / EXPORT 三個 group label 字體太小、
跟按鈕對比不夠，一眼看不出分群結構。

**修正：** 找到 `_build_toolbar()` 內的 `_group()` helper，
把樣式加強：

```python
def _group(text):
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color:{_TK_ACCENT_DK.name()}; font-size:{_FS_MICRO}px; "
        f"font-weight:700; letter-spacing:1px; padding: 0 4px;")
    return lbl
```

同時把 FILE group label 前加一點左邊距，讓 wordmark 和第一個 group 之間有呼吸：

找到 `h.addWidget(_group("FILE"))` 前，加：
```python
h.addSpacing(4)
```

---

## 問題 5：消除 guidance strip 與 empty state 的文字重複

**現況：** guidance strip 說 "Step 1 — Open an OASIS..."，
中央 empty state 又說 "1. Open OASIS → 2. Load SEM → 3. Click a defect"，
兩個地方說同一件事。

**修正：** 把 `SemViewer._draw_empty_state()` 內的三步驟提示文字移除，
改成更簡短的副標題，讓 guidance strip 負責引導：

找到 `SemViewer._draw_empty_state()` 內這一行：
```python
p.drawText(r.adjusted(0, 56, 0, 56), Qt.AlignmentFlag.AlignCenter,
           "1. Open OASIS   →   2. Load SEM   →   3. Click a defect")
```

改成：
```python
p.drawText(r.adjusted(0, 56, 0, 56), Qt.AlignmentFlag.AlignCenter,
           "Follow the steps above to get started")
```

---

## 完成後確認清單

- [ ] `python3 -m py_compile glas/app/gds_align_tool.py` 無錯誤
- [ ] `python main.py` 啟動後右欄 Coordinate Setup 預設收起
- [ ] 左欄 LAYERS 空白時有圖示 + 文字引導
- [ ] Set Offset / Clear Offset 出現在右欄 image list 下方
- [ ] Toolbar FILE / VIEW MODE / EXPORT label 對比明顯
- [ ] 中央 empty state 不再重複三步驟
- [ ] `pytest tests/test_gds_align_m6.py tests/test_gds_align_m7.py -v` 全綠
- [ ] SESSION_LOG.md 補上本次 UI 修正紀錄
