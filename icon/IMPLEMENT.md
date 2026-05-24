# GLAS Branding 實作 Prompt

## 任務說明

將以下五個品牌元素整合進 GLAS 應用程式：

1. **視窗 icon（.ico / taskbar）**
2. **啟動時的 splash icon**
3. **UI 左上角 wordmark**
4. **應用程式名稱改為 GLAS**
5. **Titlebar + About 對話框**

---

## 前置作業：SVG 檔案放置

將以下四個 SVG 檔案複製到 `glas/app/icons/` 目錄：

- `glas_icon_256.svg`  → 主圖示（About 頁、大尺寸用）
- `glas_icon_128.svg`  → Splash screen 用
- `glas_icon_32.svg`   → Titlebar / taskbar 小圖示
- `glas_wordmark.svg`  → UI 左上角 wordmark

---

## 實作項目

### 1. 應用程式名稱 + 視窗 icon

在 `glas/app/gds_align_tool.py` 的 `main()` 函式中：

```python
def main() -> int:
    mp.freeze_support()
    app = QApplication(sys.argv)

    # 設定應用程式名稱
    app.setApplicationName("GLAS")
    app.setApplicationDisplayName("GLAS")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("GLAS")

    # 設定應用程式 icon（所有視窗共用）
    _icon_path = Path(__file__).resolve().parent / "icons" / "glas_icon_256.svg"
    if _icon_path.exists():
        app.setWindowIcon(QIcon(str(_icon_path)))

    if _APP_QSS is not None:
        app.setStyleSheet(_APP_QSS)
    else:
        app.setStyleSheet(_FALLBACK_QSS)

    win = MainWindow()
    win.show()
    return app.exec()
```

---

### 2. MainWindow 標題列 icon + 視窗標題

在 `MainWindow.__init__()` 的最後加入：

```python
# 視窗標題改為 GLAS
self.setWindowTitle("GLAS")

# titlebar icon
_icon_path = Path(__file__).resolve().parent / "icons" / "glas_icon_32.svg"
if _icon_path.exists():
    self.setWindowIcon(QIcon(str(_icon_path)))
```

---

### 3. UI 左上角 wordmark

在 `MainWindow._build_toolbar()` 中，`h = QHBoxLayout(bar)` 後、第一個按鈕前，插入：

```python
# GLAS wordmark（toolbar 最左側）
_wm_path = Path(__file__).resolve().parent / "icons" / "glas_wordmark.svg"
if _wm_path.exists():
    wm_label = QLabel(bar)
    wm_pixmap = QPixmap(str(_wm_path))
    # 縮放到高度 28px，保持比例
    wm_label.setPixmap(
        wm_pixmap.scaledToHeight(28, Qt.TransformationMode.SmoothTransformation)
    )
    wm_label.setContentsMargins(4, 0, 8, 0)
    h.addWidget(wm_label)

    # 分隔線
    div = QFrame(bar)
    div.setFrameShape(QFrame.Shape.VLine)
    div.setStyleSheet(f"color: {_TK_BORDER.name()};")
    h.addWidget(div)
```

---

### 4. About 對話框升級

將 `MainWindow._show_about()` 替換為：

```python
def _show_about(self) -> None:
    dlg = QDialog(self)
    dlg.setWindowTitle("About GLAS")
    dlg.setFixedSize(380, 280)

    v = QVBoxLayout(dlg)
    v.setContentsMargins(32, 28, 32, 24)
    v.setSpacing(0)

    # icon
    _icon_path = Path(__file__).resolve().parent / "icons" / "glas_icon_128.svg"
    if _icon_path.exists():
        icon_lbl = QLabel(dlg)
        pm = QPixmap(str(_icon_path)).scaledToHeight(
            72, Qt.TransformationMode.SmoothTransformation)
        icon_lbl.setPixmap(pm)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(icon_lbl)
        v.addSpacing(12)

    # app name
    name_lbl = QLabel("GLAS", dlg)
    name_lbl.setStyleSheet(
        "font-size: 22px; font-weight: 500; color: #3f3428; letter-spacing: 3px;")
    name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    v.addWidget(name_lbl)

    # subtitle
    sub_lbl = QLabel("GDS-Layout Alignment for SEM", dlg)
    sub_lbl.setStyleSheet(
        "font-size: 10px; color: #8a7660; letter-spacing: 1.5px; margin-top: 2px;")
    sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    v.addWidget(sub_lbl)

    v.addSpacing(16)

    # version + info
    ver_lbl = QLabel("Version 1.0.0", dlg)
    ver_lbl.setStyleSheet("font-size: 12px; color: #9a8878;")
    ver_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    v.addWidget(ver_lbl)

    info_lbl = QLabel(
        "Built-in OASIS streaming parser · No klayout / gdstk dependency", dlg)
    info_lbl.setStyleSheet("font-size: 11px; color: #b0a090;")
    info_lbl.setWordWrap(True)
    info_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    v.addWidget(info_lbl)

    v.addStretch(1)

    # close button
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok, dlg)
    bb.accepted.connect(dlg.accept)
    v.addWidget(bb)

    dlg.exec()
```

---

### 5. 選配：Windows .ico 生成（若需要打包成 .exe）

若要打包 Windows 執行檔，在 `glas/` 根目錄執行：

```bash
pip install cairosvg Pillow --break-system-packages
python - <<'EOF'
from cairosvg import svg2png
from PIL import Image
import io

sizes = [16, 32, 48, 64, 128, 256]
imgs = []
with open("glas/app/icons/glas_icon_256.svg", "rb") as f:
    svg_data = f.read()
for s in sizes:
    png = svg2png(bytestring=svg_data, output_width=s, output_height=s)
    imgs.append(Image.open(io.BytesIO(png)).convert("RGBA"))

imgs[0].save(
    "glas/app/icons/glas.ico",
    format="ICO",
    sizes=[(s, s) for s in sizes],
    append_images=imgs[1:]
)
print("glas.ico generated")
EOF
```

---

## 完成後確認清單

- [ ] `python main.py` 視窗標題顯示 "GLAS"
- [ ] Taskbar / dock 顯示 G 準心圖示
- [ ] Toolbar 左側出現 wordmark
- [ ] Help → About 顯示大圖示 + 版本資訊
- [ ] `python3 -m py_compile glas/app/gds_align_tool.py` 無錯誤
- [ ] SESSION_LOG.md 補上本次變更紀錄
