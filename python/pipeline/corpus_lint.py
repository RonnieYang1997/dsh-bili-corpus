#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
corpus_lint.py -- encoding / structure / metadata guard for the 独夫之心 knowledge base.

WHY THIS EXISTS
---------------
A past pipeline (PowerShell 5.1 + CP936 console) silently corrupted the corpus:
  * mojibake Chinese literals   ("日期：" -> "鏃ユ湡锛?")
  * private-use-area characters (U+E000..U+F8FF) from mis-decoded GBK
  * a UTF-8 BOM on 2839 files
  * 828 junk body headers, 855 garbled "## 语音转录" subheadings
  * 90 files with no frontmatter opener, 14 API-error stub bodies
See the repair package.  All of the above were repaired on 2026-09-23.

A SECOND, LARGER-REPAIR STILL LEFT DEFECTS BEHIND (found 2026-09-27):
  * one frontmatter emptied outright (`---\\n\\n---`)            -> R17
  * metadata exercised only on the BODY, never on the frontmatter -> R18
  * 连麦/问答 transcripts missing `type: dialogue` / a usable warning -> R19
  * placeholder metadata (title "", date 1970-01-01, duration "")  -> R14

THE INDEX IS A FILE TOO (found 2026-10-05):
  * MEMORY.md had 36 index lines without the "- " bullet (invisible entries),
    23 duplicate names, one `[link](link.md)` line, and 18 transcript files
    that no index line mentioned at all                         -> R27

ENCODING CORRECTNESS DOES NOT IMPLY FILE CORRECTNESS.  This guard therefore checks
bytes AND metadata AND cross-root identity AND index completeness.

BASELINE
--------
Known-and-accepted exceptions live in corpus_lint_baseline.json.  As of 2026-10-05 the
58 entries are 56 (28 placeholder files whose metadata the Bilibili API confirms is
unrecoverable, counted once per root) + 2 (one file carrying a single lost character,
once per root).  The 14 API-error stub entries were dropped once every one of those
bodies had been re-transcribed or replaced by an honest placeholder.
Anything NOT in the baseline is a NEW REGRESSION -> exit 1.

    python corpus_lint.py                     # check against baseline
    python corpus_lint.py --all               # show known issues too
    python corpus_lint.py --update-baseline   # accept current state (use with care)

Other scripts MUST resolve the corpus root through assert_corpus_root() (R20).

EXIT CODE
    0 = no new defects        1 = new defects found        2 = usage error
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# R3 + R16: a CP936 (GBK) console dies with UnicodeEncodeError on U+FFFD / PUA
# output, which would make the guard crash exactly when it has something to say.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
# P2a：默认只判「唯一校准源」（产物存档）；落点只是临时落脚。
DEFAULT_ROOTS = [
    os.environ.get("DSH_CORPUS_LINT_ROOT")
    or os.environ.get("DSH_CORPUS_ARCHIVE")
    or r"C:\Users\Ronnie\Documents\GitHub\dufuzhixin\memory",
]
DEFAULT_BASELINE = Path(os.environ.get("DSH_CORPUS_LINT_BASELINE") or (HERE / "corpus_lint_baseline.json"))

PUA        = re.compile("[\ue000-\uf8ff]")
REPL       = "\ufffd"
RE_JUNK_A  = re.compile(r"@\{page=.*\}\.date\)")
RE_JUNK_B  = re.compile(r"^BVID\S{1,3}bvid\s*$")
RE_JUNK_H  = re.compile(r"^##\s+.*\u7487")
RE_INLINE  = re.compile(r"^(日期|BVID)\s*[：:]", re.M)
MOJIBAKE   = ["鏃ユ湡", "锛", "瀛楃", "杞綍", "璇煶", "绠€", "疄浣", "鍒嗕韩", "鑻忚仈"]
DEAD_MARKS = ("upstream_error", "Request Entity Too Large", "502 Bad Gateway",
              "Bad gateway", '"code":-')

# ---------------------------------------------------------------- R14/R17/R19 --
RE_FM_KEY   = lambda k: re.compile(r'^%s:[ \t]*"?(.*?)"?[ \t\r]*$' % k, re.M)
RE_TITLE    = RE_FM_KEY("title")
RE_ALTID    = re.compile(r'^(?:name|description):[ \t]*"?(.*?)"?[ \t\r]*$', re.M)
RE_TYPE     = re.compile(r'^type:[ \t]*["\']?(\w+)["\']?[ \t\r]*$', re.M)
# NB: the corpus contains BOTH quoted and unquoted type/warning values (two
# accepted wordings, several frontmatter generations).  A quoted-only regex
# silently reported 79 false "missing warning"; the same bug hit RE_TYPE, which
# used (\S+) and therefore rejected a perfectly valid `type: "dialogue"`.
RE_WARNING  = RE_FM_KEY("warning")
RE_QA_TITLE = re.compile(r"连麦|联麦|问答")
TRANSCRIPT  = re.compile(r"^dufu-BV")
PLACEHOLDER_DATES = {"1970-01-01", "1970-01-01T00:00:00"}

# ------------------------------------------------------------------- R27 -------
# MEMORY.md is the corpus index.  It is the ONE file every session reads first, and
# until 2026-10-05 nothing validated it: an ad-hoc appender had written 36 lines
# without the leading "- " bullet (so the entry was invisible), 23 duplicate names,
# one Markdown-link line, and 18 transcript files were never indexed at all.
# Two accepted line forms only:
#     - dufu-BVxxxxxxxxx
#     - dufu-BVxxxxxxxxx.md: 视频标题
RE_IDX_OK  = re.compile(r"^- (dufu-BV[A-Za-z0-9]+)(?:\.md)?(?=[ \t]*[:]|[ \t]*$)")
RE_IDX_ANY = re.compile(r"^[-*]?[ \t]*\[?(dufu-BV[A-Za-z0-9]+)")

# ------------------------------------------------------------------- R18 -------
CROSS_KEY = "<two-root>"
MAIN_ONLY_WHITELIST   = {"no-git-operations.md"}
MIRROR_ONLY_WHITELIST = set()

# ------------------------------------------------------------------- R28 -------
# A corpus root is a DATA directory: one .md per transcript, the index, and nothing
# else.  Repair/audit scratch ("_verify.txt", "dufu-*_wc.txt", "MEMORY.md.bak") belongs
# in the workdir.  2026-10-08 found 17 such files sitting in the MAIN root only: the
# guard never saw them (its file glob is *.md) and the mirror never had them, so the
# two-root diff could not report them either.
STRAY_OK = {                        # basename -> roots where it is a legitimate resident
    "no-git-operations.md": {"main"},      # main-only marker, see R20
    "charging_videos.json": {"mirror"},    # state file written by daily_scan.py
    "gaming_videos.json":   {"mirror"},
}


def lint_stray(root, tag):
    """R28: only corpus .md (plus the tiny state-file whitelist) may sit in a root."""
    for p in sorted(root.iterdir()):
        if not p.is_file():
            continue
        if p.suffix == ".md" and p.name not in STRAY_OK:
            continue
        allowed = STRAY_OK.get(p.name)
        if allowed and tag in allowed:
            continue
        yield "语料根目录存在杂散文件", p.name


def assert_corpus_root(root, min_transcripts=1000):
    """R20: refuse to touch a directory that is not obviously the corpus.

    A mis-typed root once resolved to a 2026-07-18 snapshot holding 19 files; the
    script scanned it happily and wrote nothing.  Never let that happen silently.

    Plausibility = enough transcripts OR the no-git-operations.md marker (the marker
    exists only in the main root, so it cannot be required for the mirror).
    """
    root = Path(root)
    if not root.is_dir():
        raise SystemExit("R20: corpus root does not exist: %s" % root)
    n = sum(1 for _ in root.glob("dufu-BV*.md"))
    marker = (root / "no-git-operations.md").exists()
    if n < min_transcripts and not marker:
        raise SystemExit(
            "R20: %s does not look like the corpus (%d dufu-BV*.md, marker=%s). "
            "Real path is probably under aionrs\\projects\\C--Users-...\\memory"
            % (root, n, marker))
    return root


def frontmatter(text):
    """Text between the --- fences, or None when there is no well-formed block."""
    if not text.startswith("---"):
        return None
    try:
        i = text.index("\n", 3)
        j = text.index("\n---", i)
    except ValueError:
        return None
    return text[i + 1:j]


def fmval(fm, rx):
    m = rx.search(fm) if fm else None
    return m.group(1).strip() if m else None


def lint_frontmatter(text, name):
    """R14 / R17 / R19 checks -- yields (kind, detail)."""
    fm = frontmatter(text)
    if fm is None:
        return
    if name.startswith("dufu-") and not fm.strip():
        yield "frontmatter 内容为空", ""
        return
    if not TRANSCRIPT.match(name):
        return

    title = fmval(fm, RE_TITLE)
    alt = fmval(fm, RE_ALTID)
    date = fmval(fm, RE_FM_KEY("date"))
    dur = fmval(fm, RE_FM_KEY("duration"))

    # R17: a transcript with no identifying text at all
    if not (title or alt):
        yield "元数据为空占位 (title/date/duration)", \
              "title=%r date=%r dur=%r" % (title, date, dur)

    # R14: never a guessed/placeholder date
    if date in PLACEHOLDER_DATES:
        yield "date 为占位符 1970-01-01", ""

    # R19: 连麦/问答 metadata contract
    ident = " ".join(x for x in (title, alt) if x)
    if RE_QA_TITLE.search(ident):
        w = fmval(fm, RE_WARNING)
        if fmval(fm, RE_TYPE) != "dialogue":
            yield "连麦/问答 缺 type: dialogue", (fmval(fm, RE_TYPE) or "<none>")
        if not w:
            yield "连麦/问答 缺 warning", ""
        else:
            if "联麦" in w:
                yield "warning 错别字 联麦", ""
            if "独夫之心" not in w:
                yield "warning 缺观点归属说明", w[:40]


def lint_file(p: Path, rel: str):
    """Yield (kind, detail) for every defect found in one file."""
    raw = p.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        yield "UTF-8 BOM 存在", ""
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        yield "非法 UTF-8 字节", str(e)[:50]
        return
    if REPL in text:
        yield "U+FFFD 替换字符", ""
    if PUA.search(text):
        yield "私用区字符 (PUA)", ""
    for m in MOJIBAKE:
        if m in text:
            yield "乱码特征串", repr(m)
            break
    if RE_JUNK_A.search(text):
        yield "乱码表头 @{page=...}", ""
    if RE_JUNK_B.search(text):
        yield "乱码表头 BVID..bvid", ""
    if RE_JUNK_H.search(text):
        yield "乱码小标题 ##璇", ""
    if RE_INLINE.search(text):
        yield "正文内写死中文表头", ""
    if not text.startswith("---"):
        first = text.splitlines()[0] if text.splitlines() else ""
        if re.match(r"(name|title|description|type|bvid|date|duration|source|"
                    r"status|warning|tags|category)\s*:", first):
            yield "frontmatter 缺少起始 ---", ""
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    if crlf and lf:
        yield "CRLF/LF 混用", "crlf=%d lf=%d" % (crlf, lf)
    # "dead body" only makes sense for transcripts: notes ABOUT an API error
    # (e.g. this round's memory file quoting the payload) are legitimate.
    if TRANSCRIPT.match(rel):
        for mk in DEAD_MARKS:
            if mk in text:
                yield "正文失效 (API 错误页)", mk
                break
    for kind, detail in lint_frontmatter(text, rel):
        yield kind, detail


def lint_memory_index(root):
    """R27: MEMORY.md must stay a machine-readable, complete index of the transcripts.

    Catches the four drift modes seen in the wild:
      * a line that lost its leading "- " bullet (or used the [link](link.md) form)
        -> the entry still shows in the file but is invisible to index consumers
      * the same bvid indexed twice
      * a dufu-BV*.md file that no index line mentions
      * an index line whose file does not exist
    Lines that merely *mention* a bvid inside prose are ignored.
    """
    p = root / "MEMORY.md"
    if not p.exists():
        return
    try:
        text = p.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return
    names = []
    for i, line in enumerate(text.splitlines(), 1):
        if "dufu-BV" not in line:
            continue
        m = RE_IDX_OK.match(line)
        if m:
            names.append(m.group(1))
            continue
        if RE_IDX_ANY.match(line):
            yield ("MEMORY.md 索引行格式错误 (缺 bullet/链接式)",
                   "line %d: %s" % (i, line[:40]))
    seen, dup = set(), set()
    for n in names:
        if n in seen:
            dup.add(n)
        seen.add(n)
    for n in sorted(dup):
        yield "MEMORY.md 索引重复条目", n
    on_disk = {q.stem for q in root.glob("dufu-BV*.md")}
    for n in sorted(on_disk - seen):
        yield "MEMORY.md 索引缺条目", n
    for n in sorted(seen - on_disk):
        yield "MEMORY.md 索引指向不存在的文件", n


def scan(roots):
    """-> (dict {("rootkey","filename","kind"): detail}, {root: file count})"""
    found = {}
    per_root = {}
    for root in roots:
        # R20/R28: the main root is the one carrying the marker
        tag = "main" if (root / "no-git-operations.md").exists() else "mirror"
        cnt = 0
        for p in sorted(root.glob("*.md")):
            cnt += 1
            for kind, detail in lint_file(p, p.name):
                found[(str(root), p.name, kind)] = detail
        for kind, detail in lint_memory_index(root):          # R27
            found[(str(root), "MEMORY.md", kind)] = detail
        for kind, detail in lint_stray(root, tag):            # R28
            found[(str(root), "<root>", kind)] = detail
        per_root[str(root)] = cnt
    return found, per_root


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def scan_cross(roots):
    """R18: the two roots must be byte-identical file by file."""
    found = {}
    if len(roots) < 2:
        return found
    a, b = roots[0], roots[1]
    na = {p.name: p for p in a.glob("*.md")}
    nb = {p.name: p for p in b.glob("*.md")}
    for n in sorted(set(na) | set(nb)):
        if n in na and n not in nb:
            if n not in MAIN_ONLY_WHITELIST:
                found[(CROSS_KEY, n, "仅存在于主库")] = str(a)
        elif n in nb and n not in na:
            if n not in MIRROR_ONLY_WHITELIST:
                found[(CROSS_KEY, n, "仅存在于镜像库")] = str(b)
        elif _sha(na[n]) != _sha(nb[n]):
            found[(CROSS_KEY, n, "两库内容不一致")] = "整文件 sha256 不同"
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", default=None)
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    ap.add_argument("--all", action="store_true", help="show known issues too")
    ap.add_argument("--update-baseline", action="store_true")
    a = ap.parse_args()

    roots = [Path(r) for r in (a.root or DEFAULT_ROOTS)]
    roots = [r for r in roots if r.exists()]
    if not roots:
        print("no such root", file=sys.stderr)
        return 2
    # R20: a mis-resolved root silently looks "clean" because it contains few files
    for r in roots:
        n_t = sum(1 for _ in r.glob("dufu-BV*.md"))
        if n_t < 1000 and not (r / "no-git-operations.md").exists():
            print("WARNING R20: %s does not look like the corpus (%d dufu-BV*.md). "
                  "Did the path lose a level?" % (r, n_t), file=sys.stderr)

    found, per_root = scan(roots)
    cross = scan_cross(roots)
    found.update(cross)

    if a.update_baseline:
        data = {"note": "known/accepted defects; anything else is a regression",
                "entries": sorted("|".join(k) + "|" + v for k, v in found.items())}
        Path(a.baseline).write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        print("baseline written: %d entries -> %s" % (len(found), a.baseline))
        return 0

    baseline = set()
    bp = Path(a.baseline)
    if bp.exists():
        try:
            baseline = set(json.loads(bp.read_text(encoding="utf-8")).get("entries", []))
        except Exception as e:
            print("warning: bad baseline (%s); treating as empty" % e, file=sys.stderr)

    new = {}
    known = {}
    for k, v in found.items():
        line = "|".join(k) + "|" + v
        (known if line in baseline else new).setdefault(k[2], []).append((k[1], v))

    for root in sorted(per_root):
        print("=" * 70)
        print("ROOT: %s" % root)
        print("  md files : %d" % per_root[root])
    print("=" * 70)
    compared = 0
    if len(roots) >= 2:
        na = set(p.name for p in roots[0].glob("*.md"))
        nb = set(p.name for p in roots[1].glob("*.md"))
        compared = len(na & nb)
    print("two-root consistency: %d shared file(s) compared, %d finding(s)"
          % (compared, len(cross)))

    def dump(d, title):
        tot = sum(len(v) for v in d.values())
        print("-" * 70)
        print("%s  (%d)" % (title, tot))
        for kind in sorted(d):
            print("  %-26s %d" % (kind, len(d[kind])))
            for fn, detail in d[kind][:6]:
                print("       - %s %s" % (fn, detail))
            if len(d[kind]) > 6:
                print("       ... +%d more" % (len(d[kind]) - 6))

    print("=" * 70)
    if new:
        dump(new, "NEW REGRESSIONS -> FAIL")
    else:
        print("NEW REGRESSIONS: none")
    if a.all or known:
        dump(known, "known/baselined (accepted)")
    print("=" * 70)
    if new:
        print("RESULT: FAIL - %d new defect(s). Investigate before continuing."
              % sum(len(v) for v in new.values()))
        return 1
    print("RESULT: PASS - no new defects.")
    return 0


if __name__ == "__main__":
    sys.exit(main())