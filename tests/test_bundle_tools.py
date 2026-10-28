"""Tests for the offline transfer tooling (``tools/make_filelist.py`` +
``tools/make_text_bundle.py`` + ``tools/release.py``; format ported from d4t).

The bundle exists because the company machine can't take a ``.zip`` and the
proxy won't let Python fetch files one by one — a single text ``.py`` is the
only thing that gets through. The ways that silently fails, all pinned here:

* **``tools/FILELIST.txt`` rots.** Someone adds a file and doesn't regenerate
  the list. A split bundle then reports "complete" while that file is missing
  on the target machine, because the completeness check reads this list.
* **A non-ASCII byte in the bundle.** The operator copies the file out of a
  browser and saves it with Notepad; a Chinese Windows saves ANSI (cp950), and
  Python then refuses the whole file with ``SyntaxError: Non-UTF-8 code``
  (d4t hit this on 2026-09-03). So the bundle must be pure ASCII, and a copy
  saved as cp950 / with a BOM / with CRLF must still unpack.
* **The round trip isn't byte-exact**, or a tampered copy lands anyway.
* **The shipped form regresses** to the whole-archive compression that makes
  git store the full bundle on every commit.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, str(_ROOT / "glas" / "core"))

import make_filelist       # noqa: E402
import make_text_bundle    # noqa: E402
import release             # noqa: E402
import buildinfo           # noqa: E402


def _git_available() -> bool:
    try:
        subprocess.run(["git", "rev-parse", "--git-dir"], cwd=str(_ROOT),
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


pytestmark = pytest.mark.skipif(
    not _git_available(), reason="needs a git checkout (both tools use git ls-files)")


def _run_bundle(script: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(script), *args],
                          capture_output=True, text=True, timeout=300)


class TestFileList:

    def test_matches_the_repo(self):
        """Regenerating the list must produce what is committed. If this fails,
        run ``python tools/make_filelist.py`` — a stale list makes a split
        bundle claim it is complete when it isn't."""
        path = _ROOT / make_filelist.MANIFEST
        assert path.is_file(), "tools/FILELIST.txt is missing — run make_filelist.py"
        want = "\n".join(make_filelist.build_lines(str(_ROOT))) + "\n"
        assert path.read_text(encoding="utf-8") == want

    def test_excludes_the_bundle_output(self):
        """The bundle is a *copy* of the repo, not part of it. Listing it would
        make every rebuild pack the previous build — growth on growth."""
        assert not any(rel.startswith("bundle/")
                       for rel in make_filelist.tracked_files(str(_ROOT)))

    def test_shas_are_git_blob_shas(self):
        readme = (_ROOT / "README.md").read_bytes()
        out = subprocess.run(["git", "hash-object", "README.md"], cwd=str(_ROOT),
                             check=True, stdout=subprocess.PIPE)
        assert make_filelist.blob_sha(readme) == out.stdout.decode().strip()


class TestBundleRoundTrip:

    #: A handful of real repo files, chosen to cover what the format has to
    #: survive: CJK text, a long docstring-heavy module, and the one binary.
    SAMPLE = ("README.md", "CLAUDE.md", "glas/core/tiff_index.py",
              "GLAS_快速參考卡.pdf")

    def _items(self):
        items = []
        for rel in self.SAMPLE:
            p = _ROOT / rel
            if p.is_file():
                items.append((rel, p.read_bytes()))
        assert items, "no sample files found"
        return items

    def _extract(self, tmp_path, text, name="B.py"):
        script = tmp_path / name
        script.write_text(text, encoding="utf-8", newline="\n")
        dest = tmp_path / "out"
        rc = _run_bundle(script, "--dest", str(dest))
        assert rc.returncode == 0, rc.stdout + rc.stderr
        return dest

    @pytest.mark.parametrize("form", ["per-file", "whole-lzma", "text"])
    def test_files_survive_byte_for_byte(self, tmp_path, form):
        """The shipped form and both legacy forms (bundles already carried
        into the fab keep their own extractor, but the current extractor must
        read them too) all round-trip every byte, binary included."""
        items = self._items()
        text = make_text_bundle.build("B.py", items=items,
                                      compress=(form == "whole-lzma"),
                                      legacy_text=(form == "text"))
        dest = self._extract(tmp_path, text)
        for rel, data in items:
            assert (dest / rel).read_bytes() == data, rel

    def test_shipped_form_is_per_file_lzma2_b85(self):
        text = make_text_bundle.build("B.py", items=self._items())
        lines = text.split("\n")
        i = lines.index(make_text_bundle.SENTINEL)
        assert lines[i + 1] == "#ENC lzma2+b85/file"
        assert any(ln.startswith("#B") for ln in lines[i + 2:])

    def test_legacy_text_form_routes_binaries_to_base64(self):
        """Pinned so the legacy decoder keeps a test: in the old text form only
        files that cannot be stored as lines used base64."""
        items = self._items()
        lines = make_text_bundle._data_lines(items)
        text_recs = [l for l in lines if l.startswith("#F ")]
        b64_recs = [l for l in lines if l.startswith("#X ")]
        assert any(r.endswith("README.md") for r in text_recs)
        assert all(r.endswith(".pdf") for r in b64_recs)

    def test_crlf_in_transit_does_not_break_it(self, tmp_path):
        """The whole reason the format counts lines: a bundle mailed around or
        re-saved by an editor comes back with CRLF endings."""
        items = self._items()
        text = make_text_bundle.build("B.py", items=items)
        script = tmp_path / "B.py"
        script.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        dest = tmp_path / "out"
        rc = _run_bundle(script, "--dest", str(dest))
        assert rc.returncode == 0, rc.stdout + rc.stderr
        for rel, data in items:
            assert (dest / rel).read_bytes() == data, rel

    def test_tampered_text_record_is_reported_not_written(self, tmp_path):
        """A file altered in transit must be named, not silently delivered
        (legacy text form, where a byte can be flipped in the clear)."""
        items = [("a.txt", b"hello\nworld\n")]
        text = make_text_bundle.build("B.py", items=items, legacy_text=True)
        text = text.replace("#hello", "#hell0")
        script = tmp_path / "B.py"
        script.write_text(text, encoding="utf-8", newline="\n")
        dest = tmp_path / "out"
        rc = _run_bundle(script, "--dest", str(dest))
        assert rc.returncode == 1
        assert "a.txt" in rc.stdout
        assert not (dest / "a.txt").exists()

    def test_tampered_base85_record_lands_nothing(self, tmp_path):
        """Same thing in the shipped encoding. Swapping one base85 character
        for another valid one does NOT make ``b85decode`` fail — the lzma or
        SHA stage has to catch it, and nothing may be written either way
        (the extractor decodes everything before it writes anything)."""
        body = b"print('hi')\n" * 50
        text = make_text_bundle.build("B.py", items=[("glas/x.py", body)])
        lines = text.split("\n")
        i = next(k for k, ln in enumerate(lines) if ln.startswith("#B"))
        mid = len(lines[i]) // 2
        swap = "0" if lines[i][mid] != "0" else "1"
        lines[i] = lines[i][:mid] + swap + lines[i][mid + 1:]
        for name, content, ok in (("good", text, True),
                                  ("bad", "\n".join(lines), False)):
            script = tmp_path / f"{name}.py"
            script.write_text(content, encoding="utf-8", newline="\n")
            dest = tmp_path / name
            rc = _run_bundle(script, "--dest", str(dest))
            assert (rc.returncode == 0) is ok, rc.stdout
            assert (dest / "glas" / "x.py").exists() is ok
            if ok:
                assert (dest / "glas" / "x.py").read_bytes() == body

    def test_list_writes_nothing(self, tmp_path):
        items = self._items()
        text = make_text_bundle.build("B.py", items=items)
        script = tmp_path / "B.py"
        script.write_text(text, encoding="utf-8", newline="\n")
        dest = tmp_path / "out"
        rc = _run_bundle(script, "--list", "--dest", str(dest))
        assert rc.returncode == 0
        assert not dest.exists()
        assert "README.md" in rc.stdout

    def test_the_whole_repo_is_packable(self):
        """Every tracked file goes in — the per-file encoding takes any bytes,
        so a real build can never quietly drop one."""
        items = make_text_bundle.collect(str(_ROOT))
        tracked = set(make_filelist.tracked_files(str(_ROOT)))
        tracked.add(make_filelist.MANIFEST)
        assert {rel for rel, _d in items} == tracked


class TestPureAscii:
    """The cp950 guard (d4t 2026-09-03): the operator saves the bundle with
    Notepad on a Chinese Windows, which may write ANSI. A pure-ASCII file
    cannot be damaged by that, whatever encoding is chosen."""

    def test_the_bundle_is_pure_ascii(self):
        """Looks at the *output*, not the source: a non-ASCII character can
        sneak in anywhere (header, SENTINEL, one error message)."""
        text = make_text_bundle.build("GLAS_bundle.py", str(_ROOT))
        bad = [(i, ln) for i, ln in enumerate(text.split("\n"), 1)
               if any(ord(c) > 127 for c in ln)]
        assert not bad, (
            "%d line(s) with non-ASCII, first at line %d:\n  %s\n"
            "Notepad on a Chinese Windows would save this as cp950 and Python "
            "would refuse the whole file." % (len(bad), bad[0][0], bad[0][1][:120]))

    @pytest.mark.parametrize("enc,newline", [("cp950", "\r\n"),
                                             ("utf-8-sig", "\r\n"),
                                             ("utf-8", "\n")])
    def test_a_bundle_saved_by_notepad_still_unpacks(self, tmp_path, enc, newline):
        """Save the bundle the three ways Notepad can (ANSI, UTF-8 with BOM,
        plain) and unpack it. The previous test says "no non-ASCII"; this one
        says "so it survives" — only this one still catches the day someone
        adds a Chinese character and loosens the other."""
        items = [("glas/x.py", b"print('hi')\n"), ("docs/中文.md", "中文內容\n".encode("utf-8"))]
        text = make_text_bundle.build("B.py", items=items)
        script = tmp_path / "B.py"
        with open(script, "w", encoding=enc, newline=newline) as f:
            f.write(text)
        dest = tmp_path / "out"
        rc = _run_bundle(script, "--dest", str(dest))
        assert rc.returncode == 0, rc.stdout + rc.stderr
        for rel, data in items:
            assert (dest / rel).read_bytes() == data, rel


class TestBuildId:

    def test_header_carries_the_filelist_sha(self):
        listing = (_ROOT / make_filelist.MANIFEST).read_bytes()
        items = [(make_filelist.MANIFEST, listing), ("a.py", b"x\n")]
        text = make_text_bundle.build("B.py", items=items)
        sha12 = make_filelist.blob_sha(listing)[:12]
        assert f"# BUILD {sha12} " in text

    def test_unpacked_tree_computes_the_same_id(self):
        """``python main.py --version`` on the company machine must print the
        number in the bundle header — that is the whole point of the id."""
        listing = (_ROOT / make_filelist.MANIFEST).read_bytes()
        assert buildinfo.build_id(str(_ROOT)) == make_filelist.blob_sha(listing)[:12]
        assert buildinfo.build_id(str(_ROOT)) in buildinfo.version_line(str(_ROOT))

    def test_non_ascii_paths_are_percent_encoded(self):
        """GLAS has a Chinese file name; the header line carrying it would
        otherwise be the one non-ASCII line in the bundle."""
        text = make_text_bundle.build("B.py", items=[("docs/中文.md", b"x\n")])
        assert "docs/%E4%B8%AD%E6%96%87.md" in text
        assert "中文" not in text

    def test_unknown_when_there_is_no_listing(self, tmp_path):
        assert buildinfo.build_id(str(tmp_path)) == "unknown"

    def test_main_version_flag(self):
        rc = subprocess.run([sys.executable, str(_ROOT / "main.py"), "--version"],
                            capture_output=True, text=True, timeout=120)
        assert rc.returncode == 0, rc.stderr
        assert rc.stdout.strip() == buildinfo.version_line(str(_ROOT))


class TestShippedBundle:
    """The committed ``bundle/GLAS_bundle.py`` is what actually gets copied onto
    a machine that can't clone; it has to stay in step with the repo."""

    def test_the_transfer_files_are_up_to_date(self):
        """**The point of this whole file.** Forgetting to regenerate has no
        symptom here — it shows up later as a missing or stale file on a machine
        that has no other way to get the code. So it fails in the test suite
        instead, and the message is the command that fixes it."""
        problems = release.stale(str(_ROOT))
        assert not problems, (
            "transfer files are stale: " + "; ".join(problems)
            + "  →  run: git add -A && python tools/release.py && git add -A")

    def test_it_is_a_single_file(self):
        """One file, not parts. GitHub's 1 MB display limit doesn't apply — the
        operator copies raw — so splitting would only add copy steps."""
        built = sorted((_ROOT / "bundle").glob("GLAS_bundle*.py"))
        assert [p.name for p in built] == ["GLAS_bundle.py"]

    def test_the_bundle_in_the_repo_uses_the_per_file_form(self):
        """Not just the code path: the file that is actually committed."""
        lines = (_ROOT / "bundle" / "GLAS_bundle.py").read_text(
            encoding="utf-8").split("\n")
        # Line-wise lookup, not split(SENTINEL): the bundle contains
        # make_text_bundle.py itself, whose source has this string too.
        i = lines.index(make_text_bundle.SENTINEL)
        assert lines[i + 1] == "#ENC lzma2+b85/file"

    def test_the_bundle_in_the_repo_is_pure_ascii(self):
        data = (_ROOT / "bundle" / "GLAS_bundle.py").read_bytes()
        assert all(b < 128 for b in data)

    def test_release_ships_the_per_file_form(self):
        """The decision is one boolean in release.py and the easiest thing in
        the repo to "fix back"; its symptom (a fat git pack) takes months. ast,
        not a regex: the call has nested parentheses."""
        src = (_ROOT / "tools" / "release.py").read_text(encoding="utf-8")
        calls = [n for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "build"
                 and getattr(n.func.value, "id", "") == "make_text_bundle"]
        assert calls, "release.py no longer calls make_text_bundle.build?"
        for call in calls:
            kw = {k.arg: k.value for k in call.keywords}
            assert "compress" in kw and kw["compress"].value is False, (
                "release.py ships the whole-archive form again (line %d) — that "
                "is what makes git store the full bundle on every commit."
                % call.lineno)

    def test_size_report_is_informational_only(self):
        """The size line must never grow back into a gate: a warning nobody can
        act on just teaches people to ignore warnings."""
        level, msg = release.bundle_size_report(5 * 1024 * 1024)
        assert level == "ok"
        assert "MB" in msg
        assert "%" not in msg          # a percentage of 1 MB reads as an alarm
