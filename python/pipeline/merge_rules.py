#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""merge_rules.py —— 把多份（分半/分段由不同人写出的）校订规则表合并成一张。

为什么需要它：一讲超过约 4.5 万字时，"一个人通读全篇再交表"经常交不出来
（读过一半上下文就满了），得拆成 partA/partB 两班并行。两班各自只看到半篇，
必然写出**重复键**（同一个人名两边都收），而 `calibrate.py` 遇到重复键会直接退出。
这个脚本按键去重（保留先出现的），顺手做三件体检：

  1. 丢掉"稿子里根本不存在"的键（写错了、或读的是别的一讲）；
  2. 丢掉与底表撞键的条目（底表已经在管，重复写会让 calibrate 报错）；
  3. 报**覆盖区间**：每份表的键在正文里最远命中到第几个字。这个数比"我读完了"
     这句话可靠得多 —— 负责后半篇的人，覆盖率就该接近 100%；反过来负责前半篇的
     人如果也报到 100%，说明他越界读了别人的部分（或两边撞车）。

用法：
    python scripts/merge_rules.py 稿子.md partA.tsv partB.tsv -o <bv>_extra.tsv
    python scripts/merge_rules.py 稿子.md a.tsv b.tsv --base base.tsv --json rep.json
    python scripts/merge_rules.py --selftest

合并后照常用 calibrate.py 跑 `--check`；本脚本不看规则对不对，只看表是否完整、无冲突。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


def read_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return text.strip()


def read_rules(path: Path):
    """返回 (行列表, 坏行行号列表)。坏行 = 不是三列。"""
    rows, bad = [], []
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) != 3:
            bad.append(no)
            continue
        rows.append([c.strip() for c in cols])
    return rows, bad


def read_keys(path: Path):
    rows, _ = read_rules(path)
    return {r[0] for r in rows}


def merge(body: str, parts, base_keys=frozenset()):
    seen = set(base_keys)
    out, dropped, coverage = [], [], []
    for path, rows in parts:
        hits = []
        kept = 0
        for key, value, why in rows:
            if key in seen:
                dropped.append((path.name, key, "重复" if key not in base_keys else "撞底表"))
                continue
            pos = body.rfind(key)
            if pos < 0:
                dropped.append((path.name, key, "稿中不存在"))
                continue
            seen.add(key)
            kept += 1
            hits.append((pos, pos + len(key)))
            out.append("\t".join([key, value, why]))
        span = "%d–%d" % (min(h[0] for h in hits), max(h[1] for h in hits)) if hits else "无命中"
        pct = 100.0 * max(h[1] for h in hits) / len(body) if hits else 0.0
        coverage.append({"part": path.name, "rows": len(rows), "kept": kept,
                         "span": span, "coverage_pct": round(pct, 1)})
    return out, dropped, coverage


def main(argv=None):
    ap = argparse.ArgumentParser(description="合并分半写出的校订规则表")
    ap.add_argument("transcript", nargs="?", help="转写稿 .md")
    ap.add_argument("parts", nargs="*", help="要合并的规则表 .tsv，按给定顺序取先出现的键")
    ap.add_argument("-o", "--out", help="输出 TSV（缺省只体检不写）")
    ap.add_argument("--base", help="底表；与其撞键的条目会被丢掉")
    ap.add_argument("--json", dest="json_path", help="把体检结果写成 JSON")
    ap.add_argument("--min-coverage", type=float, default=90.0,
                    help="覆盖率的合格线（%%，默认 90）；低于此值给出警告")
    ap.add_argument("--selftest", action="store_true", help="自检，不需要真稿子")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if not args.transcript or not args.parts:
        ap.error("需要 稿子.md 与至少一份 part .tsv（或用 --selftest）")

    body = read_body(Path(args.transcript))
    if not body:
        print("！稿子正文为空：%s" % args.transcript, file=sys.stderr)
        return 2

    base_keys = read_keys(Path(args.base)) if args.base else frozenset()
    loaded, broken = [], False
    for p in map(Path, args.parts):
        rows, bad = read_rules(p)
        if bad:
            broken = True
            print("！%s 有 %d 行不是三列（第 %s 行），已跳过"
                  % (p.name, len(bad), "、".join(map(str, bad[:8]))), file=sys.stderr)
        loaded.append((p, rows))

    merged, dropped, coverage = merge(body, loaded, base_keys)

    print("正文 %d 字；合并 %d 条" % (len(body), len(merged)))
    for c in coverage:
        print("  %-28s 读入 %3d 条，采用 %3d 条，命中区间 %-14s 覆盖 %5.1f%%"
              % (c["part"], c["rows"], c["kept"], c["span"], c["coverage_pct"]))
    if dropped:
        kinds = {}
        for _, _, why in dropped:
            kinds.setdefault(why, []).append(_)
        parts_txt = []
        for why, names in kinds.items():
            parts_txt.append("%s %d 条（%s）" % (why, len(names), "、".join(names[:6])))
        print("  丢弃：" + "；".join(parts_txt))
    else:
        print("  丢弃：无")

    low = [c for c in coverage if c["coverage_pct"] < args.min_coverage]
    if low:
        print("！覆盖率低于 %.0f%%：%s\n"
              "   判读：覆盖率 = 该份最远的键在正文的结束位置 / 正文长度。\n"
              "   · 单份通读的讲：低 = 没读到末尾（或读的是别的讲）。\n"
              "   · 一讲拆半的多份：前半份会把「两半都出现」的键（人名、篇名）算到后半去，\n"
              "     覆盖率自然偏低——这时看「命中区间」的上限有没有明显超出自己那半段即可，\n"
              "     超出是正常的，没超出才算真读到位。"
              % (args.min_coverage, "、".join(c["part"] for c in low)), file=sys.stderr)

    if args.out:
        header = ["# 由 merge_rules.py 合并：%s" % Path(args.transcript).name]
        for c in coverage:
            header.append("#   %s：%d 条，覆盖到 %.1f%%" % (c["part"], c["kept"], c["coverage_pct"]))
        Path(args.out).write_text("\n".join(header + merged) + "\n", encoding="utf-8")
        print("已写入 %s（%d 条）" % (args.out, len(merged)))

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps({"transcript": Path(args.transcript).name, "body_chars": len(body),
                        "merged": len(merged), "coverage": coverage,
                        "dropped": [{"part": p, "key": k, "why": w} for p, k, w in dropped]},
                       ensure_ascii=False, indent=2), encoding="utf-8")

    return 1 if broken else 0


def selftest() -> int:
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if detail and not cond else ""))
        ok = ok and cond

    body = "甲乙丙丁黑格尔黑格的马克思甲乙完"
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "t.md").write_text("---\ntitle: x\n---\n" + body, encoding="utf-8")
        (td / "a.tsv").write_text(
            "# 前半\n甲乙\t艾乙\t重复键测试\n黑格的\t黑格尔的\t与底表撞键\n", encoding="utf-8")
        (td / "b.tsv").write_text(
            "甲乙\t艾乙\t与前一份重复\n黑格的\t黑格尔的\t与前一份重复\n"
            "戊己\t戊己\t稿中不存在\n完\t完\t后半独有，且落在末尾\n", encoding="utf-8")
        (td / "base.tsv").write_text("黑格的\t黑格尔的\t底表已管\n", encoding="utf-8")

        b = read_body(td / "t.md")
        check("正文剥掉 frontmatter", b == body, repr(b))

        parts = [(p, read_rules(p)[0]) for p in (td / "a.tsv", td / "b.tsv")]
        merged, dropped, cov = merge(b, parts, read_keys(td / "base.tsv"))
        keys = [l.split("\t")[0] for l in merged]
        why = {}
        for p, k, w in dropped:
            why.setdefault(k, []).append(w)

        check("重复键去重后只剩一条", keys.count("甲乙") == 1, str(keys))
        check("后半独有键被保留", "完" in keys and "黑格的" not in keys, str(keys))
        check("稿中不存在的键被丢", "戊己" not in keys)
        check("与底表撞键的条目被丢（两份各记一次）",
              why.get("黑格的") == ["撞底表", "撞底表"], str(why))
        check("重复被识别", why.get("甲乙") == ["重复"], str(why))
        check("覆盖率按最后一次出现算到 100%", cov[1]["coverage_pct"] == 100.0, str(cov))
        check("前半覆盖率 < 100%（没读到末尾）", cov[0]["coverage_pct"] < 100.0, str(cov))

        rows, bad = read_rules(td / "b.tsv")
        (td / "c.tsv").write_text("坏行\n", encoding="utf-8")
        _, bad2 = read_rules(td / "c.tsv")
        check("坏行被识别", bad2 == [1] and bad == [], str((bad, bad2)))

        out = td / "o.tsv"
        rc = main([str(td / "t.md"), str(td / "a.tsv"), str(td / "b.tsv"),
                   "--base", str(td / "base.tsv"), "-o", str(out)])
        text = out.read_text(encoding="utf-8")
        check("写出文件并带表头", rc == 0 and text.startswith("# 由 merge_rules.py 合并"), text[:30])
        check("写出文件行数正确",
              len([l for l in text.splitlines() if l and not l.startswith("#")]) == 2)

    print("\n%s" % ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
