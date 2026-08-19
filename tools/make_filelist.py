#!/usr/bin/env python3
# GLAS 檔案清單產生器 — ported from ADEPT tools/make_filelist.py @ 13153f4.
"""產生 ``tools/FILELIST.txt``：整個 repo 的「該有哪些檔案」對照表。

在**開發機**上跑（需要 git）：

    python tools/make_filelist.py

誰在用這份清單
--------------
``tools/make_text_bundle.py`` 產的搬運包。包太大時會分批（見那支的說明），
而每一批解開之後要能回答「還缺幾個檔案」—— 靠的就是這份清單。它固定放在第
一批裡，所以後面每一批解完都算得出還差什麼。沒有它的話，使用者只會看到
「這批解好了」，卻不知道自己貼完了沒有。

清單裡存的是 **git blob SHA-1**，不是大小或 md5 —— 這樣 ``git hash-object``
可以直接對照，搬運包解檔時的逐檔驗證也用同一個算法。

（ADEPT 那邊這份清單還餵給 ``tools/get_code.py`` 逐檔下載；GLAS 沒有那支，
所以這裡只講搬運包這個用途，不抄一段用不到的理由。）
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from typing import List

#: 清單自己不列在自己裡面（它的 SHA 沒辦法包含自己的 SHA）。
MANIFEST = "tools/FILELIST.txt"

#: 不進搬運包的目錄。**這份清單同時是打包的依據**（`make_text_bundle.py`
#: import 它），所以兩邊永遠一致 —— 兩份各自維護的排除清單一定會分家，
#: 而分家的症狀是「清單說有、包裡沒有」，於是分批解包的『還缺幾個』永遠到不了 0。
#:
#: * `bundle/` —— 產出物是 repo 的**複本**不是內容。列進去的話每打一次包，
#:   repo 就多吃一份上一次的包，指數成長。
EXCLUDE_DIRS = ("bundle",)

HEADER = (
    "# GLAS 檔案清單（每行：git blob SHA-1 + 路徑）。",
    "# 由 tools/make_filelist.py 產生；tools/make_text_bundle.py 用它回報分批進度。",
)


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tracked_files(root: str = "") -> List[str]:
    """``git ls-files``（排除清單自己），排序後回傳。

    用 ``-z``：這個 repo 有中文檔名，而 git 預設會把非 ASCII 路徑加引號並跳脫
    （``"GLAS_\\345\\277\\253..."``），那種字串當路徑開檔一定失敗。
    """
    root = root or repo_root()
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True,
                         stdout=subprocess.PIPE).stdout.decode("utf-8")
    keep = []
    for rel in out.split("\0"):
        rel = rel.strip()
        if not rel or rel == MANIFEST:
            continue
        if any(rel.startswith(d + "/") for d in EXCLUDE_DIRS):
            continue
        keep.append(rel)
    return sorted(keep)


def blob_sha(data: bytes) -> str:
    """git 算 blob SHA 的方式：``"blob <len>\\0" + 內容``。"""
    h = hashlib.sha1()                                # noqa: S324 — git 的格式
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def build_lines(root: str = "") -> List[str]:
    """整份清單的內容（測試拿這個跟磁碟上的檔案對照）。"""
    root = root or repo_root()
    lines = list(HEADER)
    for rel in tracked_files(root):
        with open(os.path.join(root, rel.replace("/", os.sep)), "rb") as f:
            lines.append("%s %s" % (blob_sha(f.read()), rel))
    return lines


def main(argv=None) -> int:
    root = repo_root()
    lines = build_lines(root)
    path = os.path.join(root, MANIFEST.replace("/", os.sep))
    tmp = path + ".tmp"                               # 原子寫（同 layer cache 慣例）
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    print("%s：%d 個檔案" % (MANIFEST, len(lines) - len(HEADER)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
