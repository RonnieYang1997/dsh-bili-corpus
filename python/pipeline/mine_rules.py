# -*- coding: utf-8 -*-
"""mine_rules.py —— 从逐讲规则表挖「语料级底表」（rules/corpora/<id>.tsv）。

为什么单独有这个脚本
-------------------
逐讲表（讲授级）是讲完就没用的；但同一套合集里反复出现的那批走音是**语料级**的，
值得沉淀成一张随插件走的表。口径必须保守：宁可漏掉几条，也不能让一条短键去咬正确词
（`磅→镑` 会把「20磅重的棉花」改掉、`资本价→资本家` 会咬「资本价值」）。

聚合口径
--------
  * 同一 (键, 值) 至少在 N 讲独立出现（默认 2；89 讲的大语料建议 3）
  * 键长 ≥ 4（单双字/三字键跨讲复用太容易误伤）
  * 键与值互不为子串（自食 / 反向自食都会让 apply 不收敛或改坏）
  * 同一键出现多种落点 → 判为歧义，整条丢弃（不猜）
  * curated.tsv 已覆盖的键不再重复收（否则两层叠加会报重复键）

用法
----
    python mine_rules.py --src ../../_mle/calib --corpus malie --min-lectures 3
    python mine_rules.py --src ../../_cap/calib --corpus capital --min-lectures 2 --dry-run
"""
import argparse
import collections
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
RULES_DIR = os.environ.get("DSH_CORPUS_RULES") or os.path.join(_HERE, "rules")
CORPORA_DIR = os.path.join(RULES_DIR, "corpora")


def curated_keys():
    p = os.path.join(RULES_DIR, "curated.tsv")
    keys = set()
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8-sig") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    keys.add(ln.split("\t")[0].strip())
    return keys


def collect(src):
    """逐讲表 → {(键, 值): {"lectures": set, "times": n}}；base.tsv（语料级）不参与。"""
    pairs = collections.defaultdict(lambda: {"lectures": set(), "times": 0})
    for fn in sorted(os.listdir(src)):
        if not fn.endswith(".tsv") or fn == "base.tsv":
            continue
        lec = fn.split("_")[0]
        with open(os.path.join(src, fn), "r", encoding="utf-8-sig", errors="replace") as f:
            for ln in f:
                ln = ln.rstrip("\r\n")
                if not ln.strip() or ln.lstrip().startswith("#"):
                    continue
                parts = ln.split("\t")
                if len(parts) < 2:
                    continue
                k, v = parts[0].strip(), parts[1].strip()
                if not k or not v or k == v:
                    continue
                e = pairs[(k, v)]
                e["lectures"].add(lec)
                e["times"] += 1
    return pairs


def build(src, min_lectures, min_key):
    pairs = collect(src)
    skip = curated_keys()
    by_key = collections.defaultdict(list)
    for (k, v), e in pairs.items():
        if k in skip or len(k) < min_key:
            continue
        if len(e["lectures"]) < min_lectures:
            continue
        if k in v or v in k:
            continue
        by_key[k].append((len(e["lectures"]), e["times"], v))
    rows, ambiguous = [], 0
    for k, cand in by_key.items():
        cand.sort(reverse=True)
        if len(cand) > 1 and cand[0][0] == cand[1][0] and cand[0][2] != cand[1][2]:
            ambiguous += 1
            continue
        nlec, times, v = cand[0]
        rows.append((k, v, "%d 讲 %d 次" % (nlec, times)))
    rows.sort(key=lambda r: (-len(r[0]), r[0]))
    return rows, ambiguous


def main():
    ap = argparse.ArgumentParser(description="从逐讲规则表挖语料级底表")
    ap.add_argument("--src", required=True, help="逐讲规则表所在目录（*.tsv）")
    ap.add_argument("--corpus", required=True, help="语料 id（决定 rules/corpora/<id>.tsv）")
    ap.add_argument("--min-lectures", type=int, default=2, help="至少在 N 讲独立出现（默认 2）")
    ap.add_argument("--min-key", type=int, default=4, help="键长下限（默认 4）")
    ap.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件")
    a = ap.parse_args()

    if not os.path.isdir(a.src):
        raise SystemExit("!! --src 不是目录: %s" % a.src)
    rows, ambiguous = build(a.src, a.min_lectures, a.min_key)
    print("挖出 %d 条（丢歧义 %d 条；来源 %s，门槛 ≥%d 讲 / 键长 ≥%d）"
          % (len(rows), ambiguous, a.src, a.min_lectures, a.min_key))
    for k, v, why in rows[:10]:
        print("   %-12s → %-12s %s" % (k, v, why))
    if a.dry_run:
        print("（dry-run，未写文件）")
        return 0

    os.makedirs(CORPORA_DIR, exist_ok=True)
    out = os.path.join(CORPORA_DIR, "%s.tsv" % a.corpus)
    lines = [
        "# 语料级底表（插件内置）· %s" % a.corpus,
        "# 由 mine_rules.py 从逐讲表聚合：同一键值在 ≥%d 讲独立出现才收；键长 ≥%d；"
        % (a.min_lectures, a.min_key),
        "# 键与值互不为子串；同键多落点判歧义整条丢弃；curated.tsv 已有的键不重复收。",
        "# 用法：corpus_dict 不带 rules 时自动叠加（curated → 本表 → <STATE>/dict/*.tsv）。",
        "",
    ]
    lines.extend("%s\t%s\t%s" % (k, v, why) for k, v, why in rows)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print("已写入 %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
