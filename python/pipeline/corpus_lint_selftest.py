#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Negative self-test for corpus_lint.py.

A guard nobody has ever seen fail is not a guard.  This builds throw-away fixtures in a
temp directory and asserts that every rule class is actually detected:

    R17  frontmatter 内容为空 / 缺少起始 ---
    R17  UTF-8 BOM / PUA / U+FFFD / mojibake / 正文失效 stubs
    R14  date 占位符 1970-01-01
    R19  连麦问答 缺 type: dialogue / 缺 warning / warning 错别字 联麦 / 缺观点归属
    R18  两库内容不一致 / 仅存在于主库 / 仅存在于镜像库
    R20  assert_corpus_root() refuses an implausible root
    R27  MEMORY.md 索引: 缺 bullet / 链接式 / 重复条目 / 缺条目 / 指向不存在的文件
    R28  语料根目录存在杂散文件（修复脚本的 .txt/.bak 不许留在根目录）

Run:  python corpus_lint_selftest.py     -> exit 0 = the guard works
"""
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("cl", HERE / "corpus_lint.py")
cl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cl)

GOOD_FM = ('---\r\ntitle: "测试视频"\r\nbvid: "BV1TESTONLY"\r\ndate: "2026-01-02"\r\n'
           'duration: 600\r\nsource: "bilibili"\r\n---\r\n\r\n正文内容。\r\n')

CASES = [
    ("fm-empty.md", '---\r\n\r\n---\r\n\r\n正文\r\n', "frontmatter 内容为空"),
    ("fm-noopen.md", 'title: "缺起始横线"\r\n\r\n正文\r\n', "frontmatter 缺少起始 ---"),
    ("bom.md", '\ufeff' + GOOD_FM, "UTF-8 BOM 存在"),
    ("pua.md", GOOD_FM.replace("正文内容", "正文\ue123内容"), "私用区字符 (PUA)"),
    ("repl.md", GOOD_FM.replace("正文内容", "正文\ufffd内容"), "U+FFFD 替换字符"),
    ("mojibake.md", GOOD_FM.replace("正文内容", "鏃ユ湡锛氭祴璇"),
     "乱码特征串"),
    ("dead.md", GOOD_FM + "\r\nupstream_error\r\n", "正文失效 (API 错误页)"),
    ("junkhead.md", GOOD_FM + "\r\n@{page=1}.date)\r\n", "乱码表头 @{page=...}"),
    ("inline.md", GOOD_FM + "\r\n日期：2026\r\n", "正文内写死中文表头"),
    ("mixed-eol.md", GOOD_FM.rstrip("\r\n").replace("\r\n", "\n") + "\r\n",
     "CRLF/LF 混用"),
    ("date1970.md", GOOD_FM.replace('"2026-01-02"', '"1970-01-01"'),
     "date 为占位符 1970-01-01"),
    ("qa-notype.md",
     GOOD_FM.replace("测试视频", "舰长问答").replace(
         "source:", 'type: monologue\r\nsource:'),
     "连麦/问答 缺 type: dialogue"),
    ("qa-nowarn.md", GOOD_FM.replace("测试视频", "连麦专场"), "连麦/问答 缺 warning"),
    ("qa-typo.md",
     GOOD_FM.replace("测试视频", "连麦专场").replace(
         "source:", 'type: dialogue\r\nwarning: "此视频为联麦/问答形式，请勿混淆。"\r\nsource:'),
     "warning 错别字 联麦"),
    ("qa-weak.md",
     GOOD_FM.replace("测试视频", "问答环节").replace(
         "source:", 'type: dialogue\r\nwarning: "此视频为连麦/问答形式，建议结合原视频收听。"\r\nsource:'),
     "warning 缺观点归属说明"),
    ("placeholder-meta.md",
     GOOD_FM.replace('"测试视频"', '""').replace('"2026-01-02"', '""').replace(
         "duration: 600", 'duration: ""'),
     "元数据为空占位 (title/date/duration)"),
]


def main():
    tmp = Path(tempfile.mkdtemp(prefix="corpus_lint_selftest_"))
    try:
        a, b = tmp / "main", tmp / "mirror"
        a.mkdir()
        b.mkdir()
        for i, (name, body, _) in enumerate(CASES, 1):
            # real transcript naming (dufu-BV<bvid>.md) so the R14/R17/R19 rules apply
            fn = "dufu-BV1TEST%05d.md" % i  # BV + 10 位字母数字
            (a / fn).write_text(body, encoding="utf-8", newline="")
            (b / fn).write_text(body, encoding="utf-8", newline="")
        # R18 fixtures
        (a / "dufu-diverge.md").write_text(GOOD_FM, encoding="utf-8", newline="")
        (b / "dufu-diverge.md").write_text(GOOD_FM.replace("600", "601"),
                                           encoding="utf-8", newline="")
        (a / "dufu-mainonly.md").write_text(GOOD_FM, encoding="utf-8", newline="")
        (b / "dufu-mirroronly.md").write_text(GOOD_FM, encoding="utf-8", newline="")

        # R27 fixtures: an index that lost a bullet, used a link, duped a name,
        # and forgot a file (while pointing at one that does not exist)
        idx = ("- dufu-BV1TEST00001\r\n"
               "dufu-BV1TEST00002\r\n"
               "- [dufu-BV1TEST00003](dufu-BV1TEST00003.md) - 链接式\r\n"
               "- dufu-BV1TEST00004\r\n"
               "- dufu-BV1TEST00004\r\n"
               "- dufu-BV1GHOST00001\r\n")
        for r in (a, b):
            (r / "MEMORY.md").write_text(idx, encoding="utf-8", newline="")

        # R28 fixtures: repair scratch left behind inside a corpus root
        (a / "_semantic_audit.txt").write_text("scratch", encoding="utf-8", newline="")
        (b / "MEMORY.md.bak").write_text("scratch", encoding="utf-8", newline="")

        found, _ = cl.scan([a, b])
        found.update(cl.scan_cross([a, b]))
        kinds = set(k[2] for k in found)

        failures = []
        for name, _, want in CASES:
            if want not in kinds:
                failures.append("rule not triggered: %s (want %r)" % (name, want))
        for want in ("两库内容不一致", "仅存在于主库", "仅存在于镜像库"):
            if want not in kinds:
                failures.append("R18 rule not triggered: %r" % want)
        for want in ("MEMORY.md 索引行格式错误 (缺 bullet/链接式)",
                     "MEMORY.md 索引重复条目",
                     "MEMORY.md 索引缺条目",
                     "MEMORY.md 索引指向不存在的文件"):
            if want not in kinds:
                failures.append("R27 rule not triggered: %r" % want)
        if "语料根目录存在杂散文件" not in kinds:
            failures.append("R28 rule not triggered: %r" % "语料根目录存在杂散文件")

        # R20: a directory that is not the corpus must be refused
        try:
            cl.assert_corpus_root(a)
            failures.append("R20 assert_corpus_root() accepted a fake root")
        except SystemExit:
            pass

        print("fixtures checked : %d transcript files x 2 roots + MEMORY.md x 2"
              % (len(CASES) + 3))
        print("rules triggered  : %d distinct" % len(kinds))
        for k in sorted(kinds):
            print("   - %s" % k)
        if failures:
            print("\nSELFTEST FAIL")
            for f in failures:
                print("   ! " + f)
            return 1
        print("\nSELFTEST PASS - every rule class is detected")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())