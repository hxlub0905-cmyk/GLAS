#!/usr/bin/env python3
# GLAS 單檔純文字打包 — authored 2026-07-30 (ADEPT) · ported 2026-08-17 · F32 2026-10-14 對齊 d4t 現行格式.
"""把整個 repo 打成**一個 .py 檔**，那個檔案自己解得開。

什麼時候需要這個
----------------
實測遇到的情況：公司政策**擋掉 `.zip` 這個類別**（不只是 GitHub 的
`codeload.github.com`，連從別的來源下載同一包 zip 也擋），而且 proxy 也不讓
Python 逐檔抓。能過的只剩「一個純文字檔」：在瀏覽器打開 raw、全選複製、
存成檔案、執行。

所以這支產出的是**一個 .py 檔**：最前面是讀得懂的英文解包程式，後面的資料區
每個檔案各自壓縮後寫成 base85（**整份純 ASCII**）。

格式為什麼長這樣（三個性質，缺一不可；每一個都是 d4t 那邊踩出來的）
--------------------------------------------------------------------
1. **純 ASCII。** 公司機拿程式碼的方式是「瀏覽器複製 → 記事本存檔」，而中文
   Windows 的記事本會存成 ANSI（cp950）。包裡只要有一個中文字，存出來就是 Big5
   位元組，Python 用 UTF-8 讀就死在 ``SyntaxError: Non-UTF-8 code starting with
   '\\xe5'``（d4t 2026-09-03 在公司機上真的撞到）。GLAS 2026-08-17 那版的解包程式
   檔頭與訊息**就是中文** —— 同一顆地雷，只是還沒踩到。所以解包程式整段改成英文，
   資料區用 base85（字母表全 ASCII、沒有空白引號反斜線），並且有測試**把包用 cp950
   存一次再解**。
2. **git delta 壓得動。** 整包壓成一個 lzma 流的話，改一支模組會讓整份 base64
   從頭到尾變樣，git 只能每個 commit 完整存一份（d4t 實測 bundle 歷史佔 pack 98%）。
   逐檔壓的話只有那一個檔案的那一段會變。
3. **夠小。** 不壓縮的純文字在瀏覽器上全選複製會卡到不能用。逐檔 raw LZMA2 +
   base85 比逐檔 xz + base64 再小 7%（拿掉每檔的 xz 外殼 —— 它的 CRC 跟我們自己驗
   的 git blob SHA 重複；base85 每 4 個位元組寫 5 個字，base64 是 3 寫 4）。

怎麼用
------
    python tools/make_text_bundle.py            # 產 GLAS_bundle.py（預設逐檔壓縮）
    python tools/make_text_bundle.py --out X.py

拿到 `GLAS_bundle.py` 的人：

    python GLAS_bundle.py                      # 解到 .\\GLAS\\
    python GLAS_bundle.py --dest D:\\tools
    python GLAS_bundle.py --list               # 只列出裡面有什麼，不寫檔

舊格式
------
解包程式**三種編碼都認得**：``#ENC lzma2+b85/file``（現行）、``#ENC lzma+base64``
（2026-08-17 ～ 10-14 的整包壓縮）、``#ENC text``（純文字一行一行 + ``#X`` base64
記錄給二進位檔）。每一包都帶著自己的解包程式，所以已經搬進公司機的舊包照樣解得開；
這裡留著舊編碼只是讓測試能證明這件事。

每個檔案仍然帶 **git blob SHA-1**，解開時逐檔驗 —— 傳輸途中被動到的話要當場
講出來，而不是讓使用者拿到一份安靜壞掉的程式碼。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import lzma
import os
import subprocess
import sys
from typing import List, Optional, Tuple

BUNDLE_DIR = "bundle"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_filelist                       # noqa: E402  （tools/ 裡的同伴）

#: 不進包的目錄 —— **跟 FILELIST 用同一份定義**（`make_filelist.EXCLUDE_DIRS`）。
#: 兩邊分家的下場是：清單說有這個檔案、包裡沒有，於是分批解包的「還缺幾個」
#: 永遠到不了 0。
EXCLUDE_DIRS = make_filelist.EXCLUDE_DIRS

#: 資料區的分隔行。解包時找的是**整行剛好等於它**的那一行 —— 而資料區的每一行
#: 都被加了 '#'，所以就算某個檔案的內容裡出現這個字串（這支自己就在 repo 裡），
#: 也永遠不會剛好相等。**純 ASCII**（理由見模組說明第 1 點）。舊的包帶著它自己
#: 那一行，所以改這個字串**不會**讓已經搬進公司機的舊包失效。
SENTINEL = "# ==== GLAS-BUNDLE-DATA ==== data below, do not edit ===="

#: 解包程式（放在產出檔案的最前面）。它自己也是這份 bundle 的一部分，所以刻意
#: 寫短、只用標準函式庫、而且看得完 —— 使用者要能在跑之前先讀一遍。
#: ⚠ **整段必須是純 ASCII**（含註解與訊息）—— `tests/test_bundle_tools.py`
#: 看的是產出來的東西，不是這裡寫了什麼。
EXTRACTOR = '''#!/usr/bin/env python3
# GLAS single-file text bundle (produced by tools/make_text_bundle.py).
#
# THIS FILE IS DELIBERATELY PURE ASCII - every byte is < 128. Do not put any
# non-ASCII character in here, not in a comment and not in a message.
# Reason: the company machine gets this file by copying it out of a browser
# and saving it with Notepad, and Notepad on a Chinese Windows can write ANSI
# (cp950) instead of UTF-8. Any CJK byte then comes back mangled and Python
# refuses the whole file with
#   SyntaxError: Non-UTF-8 code starting with '\\\\xe5' ... no encoding declared
# An all-ASCII file cannot be damaged that way, whatever encoding is chosen.
# The payload below is base85 for the same reason.
#
# BUILD %(build)s
# (the build id is the blob SHA of tools/FILELIST.txt, so it names the exact
#  set of file contents inside; `python main.py --version` prints the same id
#  once unpacked, which is how you tell which build a machine is running.)
"""The whole GLAS repo lives inside this one file, as plain ASCII text.

Why this shape: company policy blocks the .zip category outright, and the
proxy will not let Python fetch files one by one -- the only thing that gets
through is a single text file you can see in a browser and copy.

    python %(name)s              # unpack into .\\\\GLAS\\\\
    python %(name)s --dest D:\\\\tools
    python %(name)s --list       # just show what is inside, write nothing

Every file carries its git blob SHA-1 and is verified before it lands, so a
copy that got truncated or rewritten in transit says so instead of leaving
you with quietly broken code.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

SENTINEL = "%(sentinel)s"
PART, N_PARTS = %(part)d, %(n_parts)d   # which batch / how many (1/1 = single)
TOTAL = %(total)d                       # files in the whole repo, not this batch


def blob_sha(data: bytes) -> str:
    """How git computes a blob SHA: "blob <length>\\\\0" + content."""
    h = hashlib.sha1()
    h.update(b"blob %%d\\0" %% len(data))
    h.update(data)
    return h.hexdigest()


def entries(lines, per_file):
    """Walk the data area, yielding (sha, path, content bytes).

    Record kinds:
      "#F" with per_file  -- base85 of raw-LZMA2 bytes on "#B" lines (current)
      "#F" without        -- the file's own text, one line per line, each
                             prefixed with "#" (legacy text form)
      "#X"                -- base64 lines (legacy text form, binary files)
    """
    i = 0
    while i < len(lines):
        line = lines[i]
        kind = line[:3]
        if kind not in ("#F ", "#X "):
            i += 1
            continue
        _, sha, count, path = line.split(" ", 3)
        n = int(count)
        chunk = lines[i + 1:i + 1 + n]
        if kind == "#F " and per_file:
            import base64
            import lzma
            from urllib.parse import unquote
            # Paths are percent-encoded in this form so that a non-ASCII
            # file name (GLAS has one) cannot break the all-ASCII promise.
            path = unquote(path)
            body = "".join(ln[2:] for ln in chunk if ln.startswith("#B"))
            raw_lzma2 = [{"id": lzma.FILTER_LZMA2, "preset": %(preset)d}]
            yield sha, path, lzma.decompress(base64.b85decode(body),
                                             format=lzma.FORMAT_RAW,
                                             filters=raw_lzma2)
        elif kind == "#X ":
            import base64
            body = [ln[1:] if ln[:1] == "#" else ln for ln in chunk]
            yield sha, path, base64.b64decode("".join(body))
        else:
            # Every data line carries a leading '#' so the whole file stays
            # valid Python. Strip it back off here.
            body = [ln[1:] if ln[:1] == "#" else ln for ln in chunk]
            yield sha, path, "\\n".join(body).encode("utf-8")
        i += 1 + n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Unpack the GLAS text bundle.")
    ap.add_argument("--dest", default="GLAS",
                    help="folder to unpack into (default .\\\\GLAS)")
    ap.add_argument("--list", action="store_true",
                    help="only list the contents, write nothing")
    a = ap.parse_args(argv)

    # Read ourselves in text mode: Python turns CRLF into LF, so the file still
    # unpacks after its line endings were changed in transit (the format counts
    # lines, not bytes, for exactly that reason). errors="replace" so a copy
    # that was saved as ANSI still gets as far as the data area.
    with open(os.path.abspath(__file__), "r", encoding="utf-8",
              errors="replace") as f:
        lines = f.read().split("\\n")
    try:
        start = lines.index(SENTINEL) + 1
    except ValueError:
        print("FAILED: no data section found -- this file was truncated, or")
        print("        it is not a complete bundle. Copy it again.")
        return 2

    data = lines[start:]
    # The line right AFTER the separator declares the encoding. A fixed
    # position, not a pattern scan: "a line starting with #B is base64" would
    # be bitten by the content itself, because every data line gets a '#'
    # prefix, so any source line starting with B (BUNDLE_DIR = ...) becomes
    # "#B...".
    enc = data[0].strip() if data else ""
    data = data[1:]
    per_file = False
    if enc == "#ENC lzma2+b85/file":
        per_file = True
    elif enc == "#ENC lzma+base64":
        # Legacy whole-archive form: one lzma stream for everything. Still
        # read here so bundles already carried into the fab keep working.
        b64 = [ln[2:] for ln in data if ln.startswith("#B")]
        import base64
        import lzma
        try:
            raw = lzma.decompress(base64.b64decode("".join(b64)))
        except Exception as exc:
            print("FAILED: cannot decode the data section: %%s" %% exc)
            print("        This file was truncated or altered while being")
            print("        copied. Copy it again, and do NOT open it in an")
            print("        editor and re-save it.")
            return 2
        data = raw.decode("utf-8").split("\\n")

    try:
        items = list(entries(data, per_file))
    except Exception as exc:
        print("FAILED: cannot decode the data section: %%s" %% exc)
        print("        This file was truncated or altered while being copied.")
        print("        Copy it again, and do NOT open it in an editor and")
        print("        re-save it.")
        return 2
    if not items:
        print("FAILED: the data section is empty -- this file was truncated.")
        return 2
    print("This bundle holds %%d files." %% len(items))
    if a.list:
        for _sha, path, data in items:
            print("  %%8d  %%s" %% (len(data), path))
        return 0

    dest = os.path.abspath(a.dest)
    print("Unpacking into: %%s" %% dest)
    bad, done = [], 0
    for sha, path, data in items:
        if blob_sha(data) != sha:
            bad.append(path)
            continue
        full = os.path.join(dest, path.replace("/", os.sep))
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        tmp = full + ".tmp"                    # atomic: never leave half a file
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, full)
        done += 1

    if bad:
        print("")
        print("FAILED: %%d files do not match their own SHA:" %% len(bad))
        for path in bad[:12]:
            print("    %%s" %% path)
        print("")
        print("  This file was altered in transit (an editor re-saving it or a")
        print("  mail filter rewriting it both do this). Get a fresh copy and")
        print("  do not re-save it from an editor. This code is incomplete --")
        print("  do not use it.")
        return 1

    print("OK: %%d files unpacked, every SHA matches." %% done)

    # When the bundle is split we must say how many are still missing --
    # otherwise the user cannot tell whether they are done pasting. The count
    # comes from tools/FILELIST.txt (always in batch 1), not from this batch.
    listing = os.path.join(dest, "tools", "FILELIST.txt")
    have_listing = os.path.isfile(listing)
    missing = []
    if have_listing:
        with open(listing, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    rel = line.split(" ", 1)[1]
                    if not os.path.isfile(os.path.join(dest,
                                                       rel.replace("/", os.sep))):
                        missing.append(rel)

    if N_PARTS > 1 and not have_listing:
        # No file list yet, so "how many are missing" cannot be computed.
        # NEVER print "next step" here -- that reads as if the whole repo had
        # arrived.
        print("")
        print("This is batch %%d of %%d; the repo has %%d files -- NOT all here yet."
              %% (PART, N_PARTS, TOTAL))
        print("Paste and run the other batches too (any order, re-running is safe).")
        print("Batch 1 carries the file list; after it, each batch says what is left.")
        return 0

    if missing:
        print("")
        print("This is batch %%d of %%d. The repo is still missing %%d files --"
              %% (PART, N_PARTS, len(missing)))
        print("they are in the other batches. Paste and run those too (any")
        print("order, re-running is safe). Missing for example:")
        for rel in missing[:6]:
            print("    %%s" %% rel)
        return 0

    print("")
    print("Next:")
    print("  cd %%s" %% dest)
    print("  pip install -r requirements.txt   # PyQt6 / numpy / opencv / shapely")
    print("  python main.py                    # start GLAS")
    print("  python main.py --version          # which build this is")
    print("")
    print("  If the dependencies will not install, see the install section of")
    print("  README.md. tifffile is only needed for multi-page patch TIFFs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def blob_sha(data: bytes) -> str:
    h = hashlib.sha1()                                # noqa: S324 — git 的格式
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def collect(root: str = "") -> List[Tuple[str, bytes]]:
    """``git ls-files`` 的每個檔案（路徑, 位元組）。

    逐檔壓縮 + base85 裝得下任何位元組，所以二進位檔（GLAS 有一份 PDF 參考卡）
    與 CRLF 檔都直接收，不像舊的純文字形式要特判。
    """
    root = root or repo_root()
    # ``-z``：這個 repo 有中文檔名，git 預設會把非 ASCII 路徑加引號並跳脫，
    # 那種字串當路徑開檔一定失敗（實測 `GLAS_快速參考卡.pdf`）。
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True,
                         stdout=subprocess.PIPE).stdout.decode("utf-8")
    items: List[Tuple[str, bytes]] = []
    for rel in sorted(p for p in out.split("\0") if p.strip()):
        if any(rel.startswith(d + "/") for d in EXCLUDE_DIRS):
            # `bundle/`：產出物自己不進包裡 —— 不然每打一次包，repo 就多一份
            # 上一次的包，而且是指數成長。
            continue
        with open(os.path.join(root, rel.replace("/", os.sep)), "rb") as f:
            items.append((rel, f.read()))
    return items


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _is_binary(data: bytes) -> bool:
    """這個位元組串裝不裝得進「一行一行的純文字」（舊 ``#ENC text`` 形式用）。

    兩個條件是分開的理由：CR 會被文字模式讀成 LF（或跟 LF 併成一個），
    所以行數對得上但**內容變了**，SHA 當場就對不上；非 UTF-8 則是連讀都讀不出來。
    """
    return b"\r" in data or not _is_utf8(data)


def _slice(items: List[Tuple[str, bytes]], limit: int
           ) -> List[List[Tuple[str, bytes]]]:
    """依大小切成幾批。``limit`` 是每批的內容上限（位元組）。

    ``tools/FILELIST.txt`` 固定放在第一批：後面每一批解完都用它回報「還缺幾個」，
    所以它必須先到。GLAS 的搬運走 raw 複製、沒有 1 MB 限制，所以**預設不分批**；
    這個留給「一次複製整包會卡」的情況（`--split`）。
    """
    first = [it for it in items if it[0] == "tools/FILELIST.txt"]
    rest = [it for it in items if it[0] != "tools/FILELIST.txt"]
    out: List[List[Tuple[str, bytes]]] = [list(first)]
    size = sum(len(d) for _r, d in first)
    for rel, data in rest:
        if size + len(data) > limit and out[-1]:
            out.append([])
            size = 0
        out[-1].append((rel, data))
        size += len(data)
    return out


#: 資料行一行多長（base85／base64 都是）。太長的行在 GitHub 上要橫向捲，看起來像壞掉的檔案。
_DATA_WIDTH = 120

#: 逐檔壓縮用的 lzma 等級。**解包程式裡寫的是同一個數**（`%(preset)d` 填進去）——
#: raw LZMA2 沒有檔頭，解的那一端要自己知道參數，兩邊分家就整包解不開。
#: 6 而不是 9：9 大概只小 1%，而逐檔壓幾百次的時間差很有感。
_LZMA_PRESET = 6
_RAW_LZMA2 = [{"id": lzma.FILTER_LZMA2, "preset": _LZMA_PRESET}]


def _data_lines(items: List[Tuple[str, bytes]]) -> List[str]:
    """資料區（**舊**純文字形式；整包壓縮版壓的也是這一段）。

    兩種記錄：``#F`` 是純文字（一行一行躺著），``#X`` 是 base64（裝不進行導向
    格式的檔案，例如 PDF）。2026-10-14 起出貨不用這一種（它帶中文，記事本存成
    cp950 就壞）；留著是讓測試證明解包程式仍認得舊包。
    """
    out: List[str] = []
    for rel, data in items:
        if _is_binary(data):
            b64 = base64.b64encode(data).decode("ascii")
            body = [b64[i:i + _DATA_WIDTH]
                    for i in range(0, len(b64), _DATA_WIDTH)] or [""]
            out.append("#X %s %d %s" % (blob_sha(data), len(body), rel))
            out.extend("#" + line for line in body)
            continue
        body = data.decode("utf-8").split("\n")
        out.append("#F %s %d %s" % (blob_sha(data), len(body), rel))
        # **每一行都要變成註解。** Python 在跑任何東西之前會先編譯整個檔案，
        # 所以資料區不能是裸的文字 —— 不然它會去解析別的檔案的內容然後語法錯誤。
        # 加一個 '#' 比塞進三引號字串安全：檔案內容裡本來就可能有三個引號。
        out.extend("#" + line for line in body)
    return out


def _data_lines_per_file(items: List[Tuple[str, bytes]]) -> List[str]:
    """資料區（**逐檔 raw LZMA2 + base85**）—— 現行出貨格式。

    一個檔案一段：``#F <sha> <幾行> <路徑>`` 之後接 ``#B<base85>`` 幾行。
    三個性質（純 ASCII / git delta 壓得動 / 夠小）的理由見模組說明。
    ⚠ raw LZMA2 **沒有檔頭**，解的那一端要自己知道參數 —— 所以解包程式裡的
    等級是從 ``_LZMA_PRESET`` 填進去的，不是另外寫一個數字。
    ⚠ **路徑 percent-encode**（GLAS 特有）：d4t 沒有中文檔名，GLAS 有
    （`GLAS_快速參考卡.pdf`）—— 光把內容 base85 還不夠，檔頭那一行的路徑也會把
    中文帶進包裡，記事本存成 cp950 就把那個路徑弄壞（內容 SHA 對、檔名錯）。
    ASCII 路徑經 quote 之後一個字都不變，所以舊包的路徑解起來也相同。
    """
    from urllib.parse import quote
    out: List[str] = []
    for rel, data in items:
        enc = base64.b85encode(lzma.compress(data, format=lzma.FORMAT_RAW,
                                             filters=_RAW_LZMA2)).decode("ascii")
        chunks = [enc[i:i + _DATA_WIDTH] for i in range(0, len(enc), _DATA_WIDTH)]
        out.append("#F %s %d %s" % (blob_sha(data), len(chunks),
                                    quote(rel, safe="/")))
        out.extend("#B" + c for c in chunks)
    return out


def build_id(items: List[Tuple[str, bytes]]) -> str:
    """這一包的身分：``tools/FILELIST.txt`` 的 blob SHA 前 12 碼，加上打包日期。

    為什麼不是 git hash：包是在 commit **之前**產的（``git add -A && release.py
    && git add -A``），所以它裝不下包含它自己的那個 commit 的 hash；而清單的
    SHA 由**檔案內容**決定，兩台機器算出來一樣，跟 git 在不在無關
    （`glas/core/buildinfo.py` 在解開的那一端算同一個數）。
    清單不在 items 裡（測試餵任意檔案）就寫 ``unknown``。日期是資訊，不進比對。
    """
    import datetime
    sha = "unknown"
    for path, data in items:
        if path == make_filelist.MANIFEST:
            sha = make_filelist.blob_sha(data)[:12]
            break
    return "%s %s" % (sha, datetime.date.today().isoformat())


def build(out_name: str = "GLAS_bundle.py", root: str = "",
          items: Optional[List[Tuple[str, bytes]]] = None,
          part: int = 1, n_parts: int = 1, total_files: int = 0,
          compress: bool = False, legacy_text: bool = False) -> str:
    """整個包的文字。預設（``compress=False``）＝逐檔 raw LZMA2 + base85。

    ``compress=True`` 產舊的整包 lzma+base64、``legacy_text=True`` 產舊的純文字
    形式 —— 兩者只為了測「解包程式仍認得舊包」，出貨不用（`release.py` 的
    ast 測試守著）。
    """
    items = collect(root) if items is None else items
    parts = [EXTRACTOR % {"name": out_name, "sentinel": SENTINEL,
                          "part": part, "n_parts": n_parts,
                          "total": total_files or len(items),
                          "build": build_id(items),
                          "preset": _LZMA_PRESET}, SENTINEL]
    if not compress and not legacy_text:
        parts.append("#ENC lzma2+b85/file")
        parts.extend(_data_lines_per_file(items))
        return "\n".join(parts) + "\n"
    body = _data_lines(items)
    if compress:
        parts.append("#ENC lzma+base64")
        blob = base64.b64encode(lzma.compress(
            "\n".join(body).encode("utf-8"), preset=9)).decode("ascii")
        parts.extend("#B" + blob[i:i + _DATA_WIDTH]
                     for i in range(0, len(blob), _DATA_WIDTH))
        return "\n".join(parts) + "\n"
    parts.append("#ENC text")
    parts.extend(body)
    return "\n".join(parts) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Pack the repo into one plain-text self-extracting .py")
    ap.add_argument("--out", default="GLAS_bundle.py",
                    help="輸出檔名（分批時會變成 ..._part1of6.py）")
    ap.add_argument("--compress", action="store_true",
                    help=("舊格式：整包壓成**一個** lzma 流（lzma + base64）。改一個"
                          "檔案整份就變樣，git 的 pack 壓不動 —— 2026-10-14 起出貨"
                          "不用它。解包程式仍認得。"))
    ap.add_argument("--split", type=int, default=0, metavar="KB",
                    help=("每批最多幾 KB（0 = 不分批）。每一批的編法跟整包一樣"
                          "（逐檔壓縮），只是把一次大複製切小；400 是安全值。"))
    a = ap.parse_args(argv)

    items = collect()
    # **預設是一個檔案。** GLAS 的搬運是直接複製 raw（user 2026-08-17 確認），
    # 跟檔案多大無關，所以不自動分批：要分批才給 `--split`。
    groups = _slice(items, a.split * 1024) if a.split else [items]
    out_dir = os.path.dirname(os.path.abspath(a.out))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    stem, ext = os.path.splitext(a.out)
    n_parts = len(groups)

    for i, group in enumerate(groups, 1):
        name = a.out if n_parts == 1 else "%s_part%dof%d%s" % (stem, i, n_parts, ext)
        text = build(os.path.basename(name), items=group, part=i,
                     n_parts=n_parts, total_files=len(items),
                     compress=a.compress)
        tmp = name + ".tmp"                           # atomic
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, name)
        print("%s：%d 個檔案、%.0f KB"
              % (name, len(group), len(text.encode("utf-8")) / 1024))
    if n_parts > 1:
        print("\n共 %d 批、%d 個檔案。每一批都可以單獨執行，順序不重要，"
              "重複執行也沒關係。" % (n_parts, len(items)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
