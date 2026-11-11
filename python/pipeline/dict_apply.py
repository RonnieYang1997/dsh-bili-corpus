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

# ---------------------------------------------------------------- 内置规则表
# 规则分三层，加载顺序即下面的顺序：
#   1. 插件内置   <plugin>/python/pipeline/rules/curated.tsv
#                 跨语料通用：换个语境也不会咬到正常词（每条都验过上下文）
#   2. 语料级     <plugin>/python/pipeline/rules/corpora/<id>.tsv
#                 按话题核心内容分（malie 马恩文集 / capital 资本论 / dufuzhixin …）
#   3. 讲授级     <STATE>/dict/*.tsv
#                 单讲专属、用完即弃；同名键以这里的为准（后加载 + --merge 先到先得
#                 时请把临时表放前面，或用 --rules 显式指定顺序）
_HERE = os.path.dirname(os.path.abspath(__file__))
RULES_DIR = os.environ.get("DSH_CORPUS_RULES") or os.path.join(_HERE, "rules")


def corpus_id():
    """当前语料 id：优先 env DSH_CORPUS_ID，其次从 <corpus>/<id>/_smoothing_state 反推。"""
    cid = (os.environ.get("DSH_CORPUS_ID") or "").strip()
    if cid:
        return cid
    st = os.path.abspath(_cp.STATE)
    parent = os.path.dirname(st)
    return os.path.basename(parent) if parent else ""


def builtin_rule_paths():
    """插件自带的两层：curated 永远先跑，语料级按当前语料 id 取。"""
    out = []
    cur = os.path.join(RULES_DIR, "curated.tsv")
    if os.path.exists(cur):
        out.append(cur)
    cid = corpus_id()
    if cid:
        per = os.path.join(RULES_DIR, "corpora", "%s.tsv" % cid)
        if os.path.exists(per):
            out.append(per)
    return out


# ---------------------------------------------------------------- 规则装载
def load_rules(paths, merge=False, min_key=1):
    """读若干 TSV → [(key, val, src)]，按长键优先排序；重复键直接报错。"""
    rules, seen, dups = [], {}, []
    per_file = []
    for p in paths:
        if not os.path.exists(p):
            raise SystemExit("!! 规则文件不存在: %s" % p)
        n_here = 0
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
                n_here += 1
        per_file.append((p, n_here))
    if per_file:
        print("规则层：%s" % " · ".join(
            "%s %d条" % (os.path.basename(p), n) for p, n in per_file if n))
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
    """一次都没**命中**过的键（我那条硬判据）。

    注意这里判的是「命中」而不是「键是否出现在正文里」：两者会分叉。
    键逐字在正文里、却被起点更早的长键先吃掉时，它一次都改不到东西，
    按「出现在正文」判会认为它没问题——027 讲就是这么漏过去的。
    所以真跑一遍 apply_text，用命中计数判。
    """
    rx = _build_re(rules)
    fired = {}
    for t in texts:
        _, _, _, counts = apply_text(t, rules, rx)
        for k, n in counts.items():
            fired[k] = fired.get(k, 0) + n
    return [k for k, _, _ in rules if not fired.get(k)]


def diagnose_miss(rules, texts, misses):
    """零命中的两种病要分开说，否则永远查不到病根。

    ① absent：键在这份语料里根本不存在——键抄错了，或讲者没这么说过。
    ② covered：键逐字在正文里，却被**起点更早、终点更晚**的另一条长键先吃掉，
       所以自己一次都没机会命中。交替正则是「最左优先、同起点取最长」，
       左边界更早的长键天然压住右边一切。

    实例（027 讲）：`手口。所以医化这个概念` 被 `经取哲学手口→经济学哲学手稿` 吃掉；
    `第一条，就对对对象现实` 被 `关于范把它提纲第一条→关于费尔巴哈的提纲第一条` 吃掉。
    解药不是加守卫，是把键挪到遮蔽段的右边（如 `。所以医化这个概念`）。
    """
    joined = "\n".join(texts)
    rx = _build_re(rules) if rules else None
    if rx is None:
        return {}, []
    longest = max(len(k) for k, _, _ in rules) if rules else 0
    absent, covered = [], {}
    for k in misses:
        pos = joined.find(k)
        if pos < 0:
            absent.append(k)
            continue
        lo = max(0, pos - longest + 1)
        cover = None
        for m in rx.finditer(joined, lo, pos + len(k)):
            if m.start() < pos and m.end() > pos and m.group(0) != k:
                cover = m.group(0)
                break
        if cover:
            covered.setdefault(cover, []).append(k)
        else:
            absent.append(k)
    return covered, absent


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
    covered, absent = diagnose_miss(rules, texts, miss) if miss else ({}, [])
    if covered:
        print("   ── 被长键遮蔽 %d 条（键在正文里，但左边界更早的长键先吃掉了它；"
              "把键挪到遮蔽段右边即可）：" % sum(len(v) for v in covered.values()))
        for cover, keys in list(covered.items())[:10]:
            print("   遮蔽键 %r 吃掉：" % cover)
            for k in keys[:6]:
                print("      %r" % k)
    if absent:
        print("   ── 正文里根本不存在 %d 条（键抄错了，或讲者没这么说过）：" % len(absent))
        for k in absent[:20]:
            print("      %r" % k)
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
        covered, absent = diagnose_miss(rules, texts, miss)
        if covered:
            print("零命中·被长键遮蔽 %d 条（键在正文里，被左边界更早的长键吃了）："
                  % sum(len(v) for v in covered.values()))
            for cover, keys in list(covered.items())[:10]:
                print("   遮蔽键 %r 吃掉 %d 条：%s"
                      % (cover, len(keys), "、".join(repr(k) for k in keys[:5])))
        if absent:
            print("零命中·正文里不存在 %d 条（键抄错或讲者没说过）：" % len(absent))
            for k in absent[:40]:
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
    out = builtin_rule_paths()
    out.extend(x.strip() for x in env.split(os.pathsep) if x.strip())
    d = os.path.join(_cp.STATE, "dict")
    if os.path.isdir(d):
        out.extend(os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".tsv"))
    if not out:
        raise SystemExit(
            "!! 没有规则文件：内置表目录 %s 不存在，且 %s 下没有 *.tsv。\n"
            "   显式指定：--rules a.tsv[,b.tsv…]" % (RULES_DIR, d))
    return out


def _selftest():
    """把一个月的踩坑经验钉成断言，改代码时先跑这个。"""
    import tempfile
    ok = fail = 0

    def check(name, got, want):
        nonlocal ok, fail
        if got == want:
            ok += 1
            print("  PASS %s" % name)
        else:
            fail += 1
            print("  FAIL %s：got %r want %r" % (name, got, want))

    def rules_of(pairs):
        rs = [(k, v, "selftest") for k, v in pairs]
        rs.sort(key=lambda r: -len(r[0]))  # 与 load_rules 同序
        return rs

    print("[1] 长键优先：同一起点先吃长的")
    rs = rules_of([("甲甲", "短"), ("甲甲甲", "长")])
    out, _, _, _ = apply_text("甲甲甲", rs)
    check("长键胜出（短键先到会变成「短甲」）", out, "长")

    print("[2] 自食（键含于值）必须被点名")
    check("自食 1 条", len(_self_eating(rules_of([("黑格", "黑格尔")]))), 1)
    # 注意：单纯前移左邻字不一定解毒——`。黑格` 仍然含在 `。黑格尔` 里。
    # 真正解毒的是让键**不再出现在值里**（改字形或加右邻字）。
    check("前移左邻仍自食（键⊂值）",
          len(_self_eating(rules_of([("。黑格", "。黑格尔")]))), 1)
    check("换成不含于值的键才解毒",
          len(_self_eating(rules_of([("黑格法", "黑格尔法")]))), 0)

    print("[3] 跑到不动点：键里含另一条的产出，第二遍才命中")
    rs = rules_of([("曾森", "曾参"), ("曾参和有弱", "曾参和有若")])
    out, _, passes, _ = apply_text("曾森和有弱", rs)
    check("两遍收敛", (out, passes), ("曾参和有若", 2))

    print("[4] 零命中按「命中次数」判，不按「键是否出现在正文里」判")
    rs = rules_of([("经取哲学手口", "经济学哲学手稿"),
                   ("手口。所以医化这个概念", "手稿。所以异化这个概念")])
    text = "1844年经取哲学手口。所以医化这个概念它是来自黑格尔"
    miss = zero_hit(rs, [text])
    check("遮蔽键被判零命中", miss, ["手口。所以医化这个概念"])

    print("[5] 零命中分两类：遮蔽 vs 正文里没有")
    covered, absent = diagnose_miss(rs, [text], miss)
    check("归为被遮蔽", covered, {"经取哲学手口": ["手口。所以医化这个概念"]})
    check("无 absent", absent, [])
    covered2, absent2 = diagnose_miss(rs, [text], ["根本没这串"])
    check("归为 absent", (covered2, absent2), ({}, ["根本没这串"]))
    rs3 = rules_of([("经取哲学手口", "经济学哲学手稿"),
                    ("。所以医化这个概念", "。所以异化这个概念")])
    check("挪到遮蔽段右边后能命中", zero_hit(rs3, [text]), [])

    print("[6] 重复键默认报错，--merge 才先到先得")
    d = tempfile.mkdtemp()
    p = os.path.join(d, "dup.tsv")
    with open(p, "w", encoding="utf-8") as f:
        f.write("甲\t乙\tx\n甲\t丙\ty\n")
    try:
        load_rules([p])
        check("重复键应报错", "没报", "报错")
    except SystemExit:
        check("重复键报错", "报错", "报错")
    check("merge 后保留先到的", load_rules([p], merge=True)[0][1], "乙")
    shutil.rmtree(d, ignore_errors=True)

    print("[7] --min-key 滤短键（短键跨讲最会误伤）")
    rs = load_rules([p], merge=True, min_key=2) if False else None
    d2 = tempfile.mkdtemp()
    q = os.path.join(d2, "k.tsv")
    with open(q, "w", encoding="utf-8") as f:
        f.write("磅\t镑\tx\n二0码\t20码\ty\n")
    check("min_key=2 只剩长键", [r[0] for r in load_rules([q], min_key=2)], ["二0码"])
    shutil.rmtree(d2, ignore_errors=True)

    print("\n%d PASS / %d FAIL" % (ok, fail))
    return 0 if fail == 0 else 1


def main():
    if "--selftest" in sys.argv:
        return _selftest()
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
