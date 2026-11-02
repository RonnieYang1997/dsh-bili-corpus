# -*- coding: utf-8 -*-
"""dict_apply.py —— 确定性词典通道（P4）。

为什么有这条通道
----------------
LLM 逐篇出补丁的通道（smoothing_batch.py）成本高、不可复算、同一错误会反复犯。
人工校订 89 讲 + 20 讲攒下来的规则表是**确定性**的：键→值、依据、长键优先、
跑到不动点。把它做成第一条通道，LLM 只负责它覆盖不到的新走音。

规则文件格式（TSV，三列，`#` 开头为注释）
----------------------------------------
    走音形态<TAB>正确形态<TAB>依据

判定纪律（从实战里来的四条）
--------------------------
1. **长键优先**：同一位置能匹配更长的键就先吃长的，否则短键会把长键的头部吃掉，
   长键永远不触发（"专属键零命中" 的根因）。
2. **不许自食**：键必须是值的真子串之外的东西（`本家为自己的利润而斗争` →
   `资本家为自己的利润而斗争` 会把每遍多改一个字），否则永不收敛。解药是把
   键前移一个真实左邻字（`。本家…`），不是加空转守卫。
3. **不许重复键**：同一键出现两次说明表里有两个来源打架，直接报错，不猜。
4. **跑到不动点**：单遍替换后还有新命中就再来一遍，上限 8 遍；到上限仍变 = 表坏了。

用法
----
    python dict_apply.py check  --rules a.tsv[,b.tsv...]
    python dict_apply.py verify --rules ... [--sample 3]
    python dict_apply.py apply  --rules ... [--only 001,002] [--dry-run]

env 与其它 pipeline 脚本一致（paths.py）：默认处理整个语料（落点 ∪ 存档）。
"""
import argparse
import json
import os
import re
import shutil
import sys
import datetime

import paths as _cp

MAX_PASSES = 8


# ---------------------------------------------------------------- 规则装载
def load_rules(paths, merge=False, min_key=1):
    """读若干 TSV → [(key, val, src)]，按长键优先排序；重复键直接报错。"""
    rules, seen, dups = [], {}, []
    for p in paths:
        if not os.path.exists(p):
            raise SystemExit("!! 规则文件不存在: %s" % p)
        with open(p, "r", encoding="utf-8-sig") as f:
            for ln, line in enumerate(f, 1):
                line = line.rstrip("\r\n")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    raise SystemExit("!! %s:%d 少于两列（要用 TAB 分隔）: %r" % (p, ln, line))
                key, val = parts[0], parts[1]
                if not key:
                    raise SystemExit("!! %s:%d 键是空的" % (p, ln))
                if key == val:
                    continue  # 恒等规则没有意义，静默丢弃
                if key in seen:
                    dups.append((key, seen[key], "%s:%d" % (p, ln)))
                    continue
                seen[key] = "%s:%d" % (p, ln)
                rules.append((key, val, parts[2] if len(parts) > 2 else ""))
    if dups:
        if merge:
            print("合并去重：%d 条重复键保留首次出现（跨讲累积表正常现象）" % len(dups))
        else:
            print("!! 重复键 %d 个（同一形态出现两次，说明两张表在打架）：" % len(dups))
            for k, first, again in dups[:20]:
                print("   %r  首次 %s  再次 %s" % (k, first, again))
            print("   要按「先到先得」合并请加 --merge")
            raise SystemExit(2)
    rules.sort(key=lambda r: -len(r[0]))  # 长键优先
    if min_key > 1:
        before = len(rules)
        rules = [r for r in rules if len(r[0]) >= min_key]
        print("按 --min-key %d 过滤掉 %d 条短键（短键跨讲复用极易误伤正确词）"
              % (min_key, before - len(rules)))
    return rules


def _self_eating(rules):
    """键含于自身值 → 每遍都会再长出一截，永不收敛。"""
    bad = []
    for k, v, src in rules:
        if k in v:
            bad.append((k, v, src))
    return bad


def _build_re(rules):
    return re.compile("|".join(re.escape(k) for k, _, _ in rules))


# ---------------------------------------------------------------- 应用
def apply_text(text, rules, rx=None):
    """跑到不动点；返回 (新文本, 改动次数, 遍数, 每键命中数)。"""
    rx = rx or _build_re(rules)
    table = {k: v for k, v, _ in rules}
    counts = {}
    total = 0
    for n in range(1, MAX_PASSES + 1):
        def sub(m):
            counts[m.group(0)] = counts.get(m.group(0), 0) + 1
            return table[m.group(0)]
        new = rx.sub(sub, text)
        if new == text:
            return text, total, n - 1, counts
        total += sum(1 for _ in rx.finditer(text))
        text = new
    return text, total, MAX_PASSES, counts


def zero_hit(rules, texts):
    """从没在任何一篇里命中过的键（我那条硬判据）。"""
    joined = "\n".join(texts)
    return [k for k, _, _ in rules if k not in joined]


# ---------------------------------------------------------------- 语料
def corpus_docs(only=None):
    out = []
    for p in _cp.iter_docs():
        name = os.path.basename(p)
        sid = _cp.stem(name)
        if only and sid not in only and not any(sid.startswith(o) for o in only):
            continue
        out.append(p)
    return out


def read_text(p):
    with open(p, "rb") as f:
        raw = f.read()
    return raw.decode("utf-8-sig")


def write_text(p, s):
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(s)


# ---------------------------------------------------------------- 子命令
def cmd_check(a):
    rules = load_rules(split_rules(a.rules), getattr(a, 'merge', False), getattr(a, 'min_key', 1))
    print("规则 %d 条（长键优先已排序）" % len(rules))
    short = [(k, s) for k, v, s in rules if len(k) < 2]
    if short:
        print("**注意**：%d 条键只有 1 个字，极易误伤正确词：" % len(short))
        for k, s in short[:10]:
            print("   %r  %s" % (k, s))
    bad = _self_eating(rules)
    if bad:
        print("!! 自食规则 %d 条（键含于值，永不收敛）：" % len(bad))
        for k, v, s in bad[:20]:
            print("   %r -> %r  %s" % (k, v, s))
        return 1
    print("自食：无")
    texts = []
    for p in corpus_docs():
        try:
            texts.append(read_text(p))
        except Exception:
            pass
    miss = zero_hit(rules, texts)
    print("全库零命中 %d 条%s" % (len(miss), "" if not miss else "："))
    for k in miss[:30]:
        print("   %r" % k)
    if miss and not a.strict_miss:
        print("（零命中只作提示：跨讲累积的底表必然有大半在本语料用不上；"
              "要当硬判据请加 --strict-miss）")
    # 硬判据：自食必挂；零命中只有在 --strict-miss 下才算失败
    return 1 if (bad or (a.strict_miss and miss)) else 0


def cmd_verify(a):
    rules = load_rules(split_rules(a.rules), getattr(a, 'merge', False), getattr(a, 'min_key', 1))
    rx = _build_re(rules)
    texts = []
    per_doc = []
    for p in corpus_docs():
        t = read_text(p)
        texts.append(t)
        n = sum(1 for _ in rx.finditer(t))
        if n:
            per_doc.append((os.path.basename(p), n))
    miss = zero_hit(rules, texts)
    hit = len(rules) - len(miss)
    print("语料 %d 篇 | 规则 %d 条 | 有命中 %d 条（%.0f%%）| 零命中 %d 条"
          % (len(texts), len(rules), hit, 100.0 * hit / max(1, len(rules)), len(miss)))
    tot = sum(n for _, n in per_doc)
    print("全库改动点 %d 处" % tot)
    for name, n in sorted(per_doc, key=lambda x: -x[1])[:a.sample]:
        print("   %5d  %s" % (n, name))
    if miss:
        print("零命中（大概率键抄错了，或已被更长的键吃掉）：")
        for k in miss[:40]:
            print("   %r" % k)
    return 0


def contexts(text, rules, rx, n=3, width=14):
    """改前上下文样本（人眼复核用）：键在句中的样子比键本身更说明问题。"""
    out = []
    for m in rx.finditer(text):
        k = m.group(0)
        a, b = max(0, m.start() - width), min(len(text), m.end() + width)
        out.append((k, text[a:m.start()] + "【" + k + "】" + text[m.end():b]))
        if len(out) >= n:
            break
    return out


def cmd_apply(a):
    rules = load_rules(split_rules(a.rules), getattr(a, 'merge', False), getattr(a, 'min_key', 1))
    bad = _self_eating(rules)
    if bad and not a.force:
        print("!! 拒绝落地：有 %d 条自食规则（--force 可强行，但你会看到永不收敛）" % len(bad))
        for k, v, s in bad[:10]:
            print("   %r -> %r  %s" % (k, v, s))
        return 2
    rx = _build_re(rules)
    only = [x.strip() for x in a.only.split(",")] if a.only else None
    docs = corpus_docs(only)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bk = os.path.join(_cp.STATE, "_dict_backup", stamp)
    total_changed = total_pts = 0
    touched = []
    worst_pass = 0
    for p in docs:
        src = read_text(p)
        new, pts, passes, counts = apply_text(src, rules, rx)
        worst_pass = max(worst_pass, passes)
        if new == src:
            continue
        total_changed += 1
        total_pts += sum(counts.values())
        touched.append({"doc": os.path.basename(p), "points": sum(counts.values()),
                        "passes": passes,
                        "top": sorted(counts.items(), key=lambda x: -x[1])[:5]})
        if not a.dry_run:
            os.makedirs(bk, exist_ok=True)
            shutil.copy2(p, os.path.join(bk, os.path.basename(p)))
            write_text(p, new)
    print("%s：%d 篇有改动，共 %d 处，最多 %d 遍跑到不动点"
          % ("试跑" if a.dry_run else "已落地", total_changed, total_pts, worst_pass))
    if not a.dry_run and total_changed:
        print("改前备份：%s" % bk)
    for x in sorted(touched, key=lambda y: -y["points"])[:a.sample]:
        print("   %5d 处 / %d 遍  %s" % (x["points"], x["passes"], x["doc"]))
        for k, n in x["top"]:
            print("        %3d×  %r" % (n, k))
    if a.context > 0 and touched:
        print("改前上下文抽样（键在【】里，先看这个再决定要不要落地）：")
        want = {x["doc"] for x in sorted(touched, key=lambda y: -y["points"])[:min(3, len(touched))]}
        shown = 0
        for p in docs:
            if os.path.basename(p) not in want:
                continue
            for k, ctx in contexts(read_text(p), rules, rx, n=a.context):
                print("   %r\n      …%s…" % (k, ctx))
                shown += 1
            if shown >= a.context * 3:
                break
    if worst_pass >= MAX_PASSES:
        print("!! 有稿件跑满 %d 遍仍未收敛，说明表里有互相喂食的规则" % MAX_PASSES)
        return 3
    return 0


def split_rules(s):
    if s:
        out = []
        for x in s.split(","):
            x = x.strip()
            if not x:
                continue
            if os.path.isdir(x):  # 目录 → 里面所有 *.tsv（跨讲累积表常用）
                out.extend(os.path.join(x, f) for f in sorted(os.listdir(x)) if f.endswith(".tsv"))
            else:
                out.append(x)
        return out
    env = os.environ.get("DSH_CORPUS_DICT") or ""
    paths = [x.strip() for x in env.split(os.pathsep) if x.strip()]
    if not paths:
        d = os.path.join(_cp.STATE, "dict")
        if os.path.isdir(d):
            paths = [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".tsv")]
    if not paths:
        raise SystemExit("!! 没有规则文件：--rules a.tsv 或 DSH_CORPUS_DICT，或 <STATE>/dict/*.tsv")
    return paths


def main():
    ap = argparse.ArgumentParser(description="确定性词典通道")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("check", cmd_check), ("verify", cmd_verify), ("apply", cmd_apply)):
        sp = sub.add_parser(name)
        sp.add_argument("--rules", default="", help="TSV 路径（逗号分隔；缺省 <STATE>/dict/*.tsv）")
        sp.add_argument("--merge", action="store_true",
                        help="重复键先到先得（跨讲累积表一起喂时用）")
        sp.add_argument("--min-key", type=int, default=1,
                        help="只用长度 ≥ N 的键（跨讲合并时建议 3，滤掉会误伤的单双字短键）")
        if name == "check":
            sp.add_argument("--strict-miss", action="store_true",
                            help="零命中即失败（单讲专属表用；跨讲底表别加）")
        if name != "check":
            sp.add_argument("--sample", type=int, default=10)
        if name == "apply":
            sp.add_argument("--only", default="", help="只处理这些稿件 id（逗号分隔，前缀匹配）")
            sp.add_argument("--dry-run", action="store_true")
            sp.add_argument("--force", action="store_true")
            sp.add_argument("--context", type=int, default=3,
                            help="每篇打印几条改前上下文（0=不打印）")
        sp.set_defaults(func=fn)
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
