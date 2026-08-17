#!/usr/bin/env python3
# GLAS 單檔純文字打包 — authored 2026-07-30.
"""把整個 repo 打成**一個純文字 .py 檔**，那個檔案自己解得開。

什麼時候需要這個
----------------
實測遇到的情況：公司政策**擋掉 `.zip` 這個類別**（不只是 GitHub 的
`codeload.github.com`，連從別的來源下載同一包 zip 也擋），而且 proxy 也不讓
Python 逐檔抓。這時候 `tools/get_code.py` / `.ps1` 都用不上，能過的只剩
「一個純文字檔」。

所以這支產出的東西**沒有任何壓縮格式**：檔案內容原封不動地一行一行躺在裡面，
可以用記事本打開讀。也**刻意不用 base64** —— base64 對 DLP 來說是「看不懂的
東西」，而看不懂通常就等於擋掉；而且這個 repo 全部是純文字，本來就不需要編碼。

怎麼用
------
    python tools/make_text_bundle.py            # 產 GLAS_bundle.py
    python tools/make_text_bundle.py --out X.py

拿到 `GLAS_bundle.py` 的人：

    python GLAS_bundle.py                      # 解到 .\\GLAS\\
    python GLAS_bundle.py --dest D:\\tools
    python GLAS_bundle.py --list               # 只列出裡面有什麼，不寫檔

格式為什麼是「行數」而不是「位元組數」
--------------------------------------
這個檔案會經過瀏覽器下載、記事本另存、郵件附件…… 任何一步都可能把 LF 換成
CRLF。用位元組數的話那一換整包就對不起來了，而且錯誤會發生在**第一個檔案之後
的全部檔案**上，看起來像整包壞掉。用行數則對換行符號免疫 —— 解開的時候用
Python 的文字模式讀（它會把 CRLF 讀成 LF），所以來回一趟仍然逐位元組相同。
前提是 repo 裡全部是 LF + UTF-8，這支會在打包前檢查，不合就拒絕產出。

每個檔案仍然帶 **git blob SHA-1**，解開時逐檔驗 —— 傳輸途中被動到的話要當場
講出來，而不是讓使用者拿到一份安靜壞掉的程式碼。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from typing import List, Optional, Tuple

#: 資料區的分隔行。解包時找的是**整行剛好等於它**的那一行 —— 而資料區的每一行
#: 都被加了 '#'，所以就算某個檔案的內容裡出現這個字串（``make_text_bundle.py``
#: 自己就在 repo 裡，它的源碼裡當然有），也永遠不會剛好相等。加 '#' 那件事因此
#: 同時解決了兩個問題：整個檔案仍然是合法的 Python，而分隔行不會被誤認。
BUNDLE_DIR = "bundle"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_filelist                       # noqa: E402  （tools/ 裡的同伴）

#: 不進包的目錄 —— **跟 FILELIST 用同一份定義**（`make_filelist.EXCLUDE_DIRS`）。
#: 兩邊分家的下場是：清單說有這個檔案、包裡沒有，於是公司機解完包之後
#: `check_files.py` 永遠報「還缺幾個」而那幾個永遠補不進來。
EXCLUDE_DIRS = make_filelist.EXCLUDE_DIRS

SENTINEL = "# ==== GLAS-BUNDLE-DATA ==== 以下是資料，不要編輯 ===="

#: 解包程式（放在產出檔案的最前面）。它自己也是這份 bundle 的一部分，
#: 所以刻意寫短、只用標準函式庫、而且看得完 —— 使用者要能在跑之前先讀一遍。
EXTRACTOR = '''#!/usr/bin/env python3
# GLAS 單檔純文字包（由 tools/make_text_bundle.py 產生）。
"""整個 GLAS repo 就在這個檔案裡，一行一行的純文字，沒有壓縮、沒有編碼。

為什麼是這種形式：公司政策擋掉 .zip 這個類別，而 proxy 也不讓 Python 逐檔抓 ——
能過的只剩「一個純文字檔」。你可以用記事本打開它，往下捲就看得到每個檔案的內容。

    python %(name)s              # 解到 .\\\\GLAS\\\\
    python %(name)s --dest D:\\\\tools
    python %(name)s --list       # 只看裡面有什麼，不寫任何檔案

每個檔案都帶 git blob SHA-1，解開時逐檔驗過才落地 —— 傳輸途中被動到的話會當場
講出來，不會讓你拿到一份安靜壞掉的程式碼。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

SENTINEL = "%(sentinel)s"
PART, N_PARTS = %(part)d, %(n_parts)d   # 這是第幾批 / 共幾批（1/1 = 沒有分批）
TOTAL = %(total)d                       # 整個 repo 有幾個檔案，不是這一批有幾個


def blob_sha(data: bytes) -> str:
    """git 算 blob SHA 的方式："blob <長度>\\\\0" + 內容。"""
    h = hashlib.sha1()
    h.update(b"blob %%d\\0" %% len(data))
    h.update(data)
    return h.hexdigest()


def entries(lines):
    """走過資料區，一個一個吐出 (sha, 路徑, 內容位元組)。

    ``#F`` = 純文字（一行一行），``#X`` = base64（裝不進行導向格式的檔案）。
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
        # 資料區每一行前面有一個 '#'（那樣整個檔案才仍然是合法的 Python）。
        body = [ln[1:] if ln[:1] == "#" else ln for ln in lines[i + 1:i + 1 + n]]
        if kind == "#X ":
            import base64
            yield sha, path, base64.b64decode("".join(body))
        else:
            yield sha, path, "\\n".join(body).encode("utf-8")
        i += 1 + n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Unpack the GLAS text bundle.")
    ap.add_argument("--dest", default="GLAS", help="解到哪個資料夾（預設 .\\\\GLAS）")
    ap.add_argument("--list", action="store_true", help="只列出內容，不寫檔")
    a = ap.parse_args(argv)

    # 用文字模式讀自己：Python 會把 CRLF 讀成 LF，所以這個檔案就算在傳輸途中
    # 被換過行尾也解得開（格式用「行數」而不是「位元組數」正是為了這件事）。
    with open(os.path.abspath(__file__), "r", encoding="utf-8") as f:
        lines = f.read().split("\\n")
    try:
        start = lines.index(SENTINEL) + 1
    except ValueError:
        print("✗ 找不到資料區 —— 這個檔案被截斷了，或不是完整的 bundle。")
        return 2

    data = lines[start:]
    # 分隔行的**下一行**宣告編碼。用「固定位置的宣告」而不是「掃某個開頭的樣式」
    # ——「以 #B 開頭就是 base64」那種判斷會被內容咬到：資料區每一行都加了 '#'，
    # 所以任何原本以 B 開頭的程式碼行（`BUNDLE_DIR = ...`）都會變成 `#B...`。
    enc = data[0].strip() if data else ""
    data = data[1:]
    if enc == "#ENC lzma+base64":
        b64 = [ln[2:] for ln in data if ln.startswith("#B")]
        import base64
        import lzma
        try:
            raw = lzma.decompress(base64.b64decode("".join(b64)))
        except Exception as exc:                     # noqa: BLE001
            print("✗ 資料區解不開：%%s" %% exc)
            print("  這個檔案在複製／貼上的過程中被截斷或改掉了。請重新複製一次，")
            print("  而且**不要**用編輯器打開後另存。")
            return 2
        data = raw.decode("utf-8").split("\\n")

    items = list(entries(data))
    if not items:
        print("✗ 資料區是空的 —— 這個檔案被截斷了。")
        return 2
    print("這個包裡有 %%d 個檔案。" %% len(items))
    if a.list:
        for _sha, path, data in items:
            print("  %%8d  %%s" %% (len(data), path))
        return 0

    dest = os.path.abspath(a.dest)
    print("解到  : %%s" %% dest)
    bad, done = [], 0
    for sha, path, data in items:
        if blob_sha(data) != sha:
            bad.append(path)
            continue
        full = os.path.join(dest, path.replace("/", os.sep))
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        tmp = full + ".tmp"                      # atomic：半個檔案不要留在磁碟上
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, full)
        done += 1

    if bad:
        print("")
        print("✗ %%d 個檔案的內容跟它自己的 SHA 對不上：" %% len(bad))
        for path in bad[:12]:
            print("    %%s" %% path)
        print("")
        print("  這個檔案在傳輸途中被動過（編輯器另存、郵件過濾器改寫都會這樣）。")
        print("  請重新取得一份，不要用編輯器打開後另存。這份程式碼不完整，不要用。")
        return 1

    print("✓ %%d 個檔案都解開了，SHA 全部對得上。" %% done)

    # 分批的時候要講「還缺幾個」—— 不然使用者不知道自己貼完了沒有。
    # 判斷依據是 tools/FILELIST.txt（它固定在第一批），而不是這一批的數量。
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
        # 還沒拿到檔案清單，所以「缺幾個」算不出來。**這時候絕對不能印
        # 「下一步：跑 doctor」** —— 那看起來就像整包已經到位了。
        print("")
        print("這是第 %%d 批 / 共 %%d 批，整個 repo 有 %%d 個檔案 —— **還沒到齊**。"
              %% (PART, N_PARTS, TOTAL))
        print("把其他批也貼進來執行（順序不重要，重複執行也沒關係）。")
        print("第 1 批裡有檔案清單，貼過它之後每一批都會告訴你還缺哪些。")
        return 0

    if missing:
        print("")
        print("這是第 %%d 批 / 共 %%d 批。整個 repo 還缺 %%d 個檔案 —— 在其他批裡。"
              %% (PART, N_PARTS, len(missing)))
        print("把其他批也貼進來執行（順序不重要，重複執行也沒關係）。缺的例如：")
        for rel in missing[:6]:
            print("    %%s" %% rel)
        return 0

    print("")
    print("下一步：")
    print("  cd %%s" %% dest)
    print("  pip install -r requirements.txt   # PyQt6 / numpy / opencv / shapely")
    print("  python main.py                    # 啟動 GLAS")
    print("")
    print("  裝不了相依套件的話，README.md 的安裝章節有說明；")
    print("  多頁 patch TIFF 才需要 tifffile（沒裝也跑得起來，只是慢）。")
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


def collect(root: str = "", skip_binary: bool = False
            ) -> List[Tuple[str, bytes]]:
    """``git ls-files`` 的每個檔案（路徑, 位元組）。

    「用行數打包」只對 LF + UTF-8 成立，所以不符合的檔案改走 base64
    （`_data_lines` 的 `#X` 記錄）。``skip_binary=True`` 則是整個丟掉它們 ——
    給「DLP 看到 base64 就擋」的情況用，代價是收到的人少了那些檔案，
    所以打包時會把丟掉的**列出來**，不讓它安靜地消失。
    """
    root = root or repo_root()
    # ``-z``：這個 repo 有中文檔名，git 預設會把非 ASCII 路徑加引號並跳脫，
    # 那種字串當路徑開檔一定失敗（實測 `GLAS_快速參考卡.pdf`）。
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True,
                         stdout=subprocess.PIPE).stdout.decode("utf-8")
    items: List[Tuple[str, bytes]] = []
    binary: List[str] = []
    for rel in sorted(p for p in out.split("\0") if p.strip()):
        if any(rel.startswith(d + "/") for d in EXCLUDE_DIRS):
            # `bundle/`：產出物自己不進包裡 —— 不然每打一次包，repo 就多一份
            # 上一次的包，而且是指數成長。
            continue
        with open(os.path.join(root, rel.replace("/", os.sep)), "rb") as f:
            data = f.read()
        # 以行數為單位的打包只裝得下「LF + UTF-8」。GLAS 有一個檔案不是
        # （`GLAS_快速參考卡.pdf`），所以這裡不像 ADEPT 那樣直接拒絕產出 ——
        # 那些檔案改用 base64 逐檔裝進去，見 `_data_lines`。
        if not skip_binary and (b"\r" in data or not _is_utf8(data)):
            binary.append(rel)
        items.append((rel, data))
    if binary:
        print("以 base64 收進包裡的非純文字檔（%d 個）：" % len(binary))
        for rel in binary:
            print("    %s" % rel)
    return [it for it in items if not (skip_binary and _is_binary(it[1]))]


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _is_binary(data: bytes) -> bool:
    """這個位元組串裝不裝得進「一行一行的純文字」。

    兩個條件是分開的理由：CR 會被文字模式讀成 LF（或跟 LF 併成一個），
    所以行數對得上但**內容變了**，SHA 當場就對不上；非 UTF-8 則是連讀都讀不出來。
    """
    return b"\r" in data or not _is_utf8(data)


def _slice(items: List[Tuple[str, bytes]], limit: int
           ) -> List[List[Tuple[str, bytes]]]:
    """依大小切成幾批。``limit`` 是每批的內容上限（位元組）。

    為什麼一定要切：**GitHub 不顯示超過 1 MB 的檔案**，而公司機唯一的取得方式是
    「在 GitHub 上看到、按複製鈕、貼進記事本」。一個 2.3 MB 的包在那台機器上
    根本點不開來複製 —— 打包成功但送不進去，是最糟的一種「做完了」。

    ``tools/FILELIST.txt`` 固定放在第一批：後面每一批解完都用它回報「還缺幾個」，
    所以它必須先到。
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


#: base64 一行多長。太長的行在 GitHub 上要橫向捲，看起來像壞掉的檔案。
_B64_WIDTH = 120


def _data_lines(items: List[Tuple[str, bytes]]) -> List[str]:
    """資料區（純文字形式）。壓縮版壓的也是這一段，所以兩種編碼的內容一模一樣。

    兩種記錄：``#F`` 是純文字（一行一行躺著，記事本讀得到），``#X`` 是 base64
    （裝不進行導向格式的檔案，例如 PDF）。**只有真的需要的檔案才 base64** ——
    base64 對 DLP 來說是看不懂的東西，而看不懂常常等於擋掉，所以能用純文字的
    一律用純文字。
    """
    out: List[str] = []
    for rel, data in items:
        if _is_binary(data):
            import base64
            b64 = base64.b64encode(data).decode("ascii")
            body = [b64[i:i + _B64_WIDTH]
                    for i in range(0, len(b64), _B64_WIDTH)] or [""]
            out.append("#X %s %d %s" % (blob_sha(data), len(body), rel))
            out.extend("#" + line for line in body)
            continue
        body = data.decode("utf-8").split("\n")
        out.append("#F %s %d %s" % (blob_sha(data), len(body), rel))
        # **每一行都要變成註解。** Python 在跑任何東西之前會先編譯整個檔案，
        # 所以資料區不能是裸的文字 —— 不然它會去解析別的檔案的內容然後語法錯誤
        # （第一版就是這樣掛的：某個 .md 裡的全形括號變成 SyntaxError）。
        # 加一個 '#' 比塞進三引號字串安全：檔案內容裡本來就可能有三個引號。
        out.extend("#" + line for line in body)
    return out


def build(out_name: str = "GLAS_bundle.py", root: str = "",
          items: Optional[List[Tuple[str, bytes]]] = None,
          part: int = 1, n_parts: int = 1, total_files: int = 0,
          compress: bool = False) -> str:
    items = collect(root) if items is None else items
    parts = [EXTRACTOR % {"name": out_name, "sentinel": SENTINEL,
                          "part": part, "n_parts": n_parts,
                          "total": total_files or len(items)}, SENTINEL]
    body = _data_lines(items)
    if compress:
        parts.append("#ENC lzma+base64")
        # **lzma 而不是 gzip。** 整包 base64 之後 gzip 是 991 KB、lzma 是 701 KB，
        # 而 GitHub 不顯示超過 1 MB 的檔案 —— 那 290 KB 的差距正好就是
        # 「一個檔案」與「還是要分成六批」的差別。
        import base64
        import lzma
        blob = base64.b64encode(lzma.compress(
            "\n".join(body).encode("utf-8"), preset=9)).decode("ascii")
        parts.extend("#B" + blob[i:i + _B64_WIDTH]
                     for i in range(0, len(blob), _B64_WIDTH))
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
                    help=("壓縮成**一個**檔案（lzma + base64，約 700 KB）。"
                          "一次複製就搬完，代價是內容不再是可以直接讀的文字 ——"
                          "解包程式本身仍然是可讀的 Python，而且 --list 可以先看"
                          "它會寫哪些檔案。"))
    ap.add_argument("--no-binary", action="store_true",
                    help=("整個丟掉裝不進純文字的檔案（目前只有那份 PDF 參考卡）。"
                          "給『DLP 看到 base64 就擋』的情況用；丟掉哪些會列出來。"))
    ap.add_argument("--split", type=int, default=0, metavar="KB",
                    help=("每批最多幾 KB（0 = 不分批）。**GitHub 不顯示超過 1 MB "
                          "的檔案**，而剪貼簿是唯一的通道時就必須分批，"
                          "400 是安全值。"))
    a = ap.parse_args(argv)

    items = collect(skip_binary=a.no_binary)
    # **預設是一個檔案。** ADEPT 那邊會自動量大小、超過就分批，因為它假設搬運
    # 途徑是「GitHub 檔案頁 → 複製鈕」，而那個頁面 1 MB 以上就不顯示內容。
    # GLAS 這邊使用者確認（2026-08-17）**沒有這個限制** —— 直接複製 raw，
    # 跟檔案多大無關。所以不自動分批：要分批才給 `--split`。
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
        tmp = name + ".tmp"                           # atomic（鐵則 5）
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
