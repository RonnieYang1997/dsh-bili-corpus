#!/usr/bin/env python3
"""校订转写稿：规则表进 → 校订稿 + 改动台账 + 待复核 出。

设计上只做三件事，但每件都对应一次真实事故（见 SKILL.md R41 与
references/transcript-calibration.md 第五、六、七节）：

  1. **单遍交替匹配**（不是"A 规则跑完再跑 B 规则"）。
     逐条顺序替换是自我破坏的根源：规则 A 产生的新文本会被规则 B 再次命中。
     这里把所有规则编成一条正则 `(长键|短键|…)`，一遍扫完，天然做到
     "最长优先"，也就不需要操心规则之间的先后。

  2. **跑到不动点**。单遍交替挡不住一类**线序依赖**：键里含着另一条规则的
     输出时（`曾森→曾参` 与 `曾参和有弱→曾参和有若`），第一遍匹配不到。
     所以循环到不再变化（上限 8 遍），`passes > 1` 会报出来。
     反过来，规则自己喂自己（`拉克利特→赫拉克利特` 又命中已改好的
     `赫拉克利特`）会每遍多长一个字，永远不收敛——上限一到即报错并指出键名。
     解药是空转守卫：`赫拉克利特\t赫拉克利特`（改后=原，最长优先先命中它）。

  3. **台账可复核**。每条改动记下 行号 / 原 / 改 / 依据 / 上下文。
     没有台账的校订稿无法被检验，也就无法被信任。

规则表格式（TSV，UTF-8，`#` 开头是注释）：

    原走音<TAB>改后<TAB>依据
    轮字<TAB>论字<TAB>本段在讲"论"读 lún
    编程的<TAB>编成的<TAB>上下文是成书年代

改后留空表示**删掉**这些字（只允许在 `--allow-delete` 下使用）。

用法：

    python scripts/calibrate.py 稿子.md --rules 规则.tsv --out-dir 输出目录 --name 我的稿
    python scripts/calibrate.py --selftest          # 跑内置 6 个用例
"""

import argparse
import re
import sys
from bisect import bisect_right

__all__ = ["load_rules", "apply_rules", "calibrate", "main"]

FM_RE = re.compile(r"^---\r?\n.*?\r?\n---\r?\n", re.S)

# 机械层：全角标点 → 半角（用户既定体例：, ? ! ; :）。句号、顿号、引号不动。
FULL2HALF = {"，": ",", "？": "?", "！": "!", "；": ";", "：": ":"}

# `--collapse-dup` 只收"绝不可能合法叠用"的字。慢/常/天/人/看/想 这类
# 都能合法重叠，一律不许动 —— 宁可漏修，不可错改。
DUP_SAFE = set("呃啊嗯哦呀嘛我就也都这那是在和不了很会要你他她它们")


class Rule:
    __slots__ = ("key", "value", "why", "order")

    def __init__(self, key, value, why, order):
        self.key = key
        self.value = value
        self.why = why
        self.order = order


def load_rules(paths, allow_delete=False):
    """读入一个或多个 TSV。返回 {原: Rule}，重复键直接报错而不是悄悄覆盖。"""
    rules = {}
    order = 0
    for path in paths:
        with open(path, "r", encoding="utf-8-sig") as f:
            for lineno, raw in enumerate(f, 1):
                line = raw.rstrip("\n").rstrip("\r")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    raise SystemExit("%s:%d 需要至少两列（原<TAB>改）：%r"
                                     % (path, lineno, line[:60]))
                key = parts[0]
                value = parts[1]
                why = parts[2] if len(parts) > 2 else ""
                if not key:
                    raise SystemExit("%s:%d 原列为空" % (path, lineno))
                if not value and not allow_delete:
                    raise SystemExit("%s:%d 改列为空（删除类改动需 --allow-delete）：%r"
                                     % (path, lineno, key))
                if key in rules:
                    raise SystemExit("%s:%d 规则重复：%r（先出现于第 %d 条）"
                                     % (path, lineno, key, rules[key].order))
                rules[key] = Rule(key, value, why, order)
                order += 1
    if not rules:
        raise SystemExit("规则表是空的")
    return rules


def build_pattern(rules):
    """一条正则，长键优先；等长时按文件里的先后（Python 交替即如此）。"""
    keys = sorted(rules, key=lambda k: (-len(k), rules[k].order))
    return re.compile("|".join(re.escape(k) for k in keys))


def apply_rules(body, rules, pattern, log=None):
    """单遍替换。返回 (新文本, [(偏移, 原, 改), …])。"""
    hits = []

    def repl(m):
        key = m.group(0)
        hits.append((m.start(), key, rules[key].value))
        if log is not None:
            log[key] = log.get(key, 0) + 1
        return rules[key].value

    return pattern.sub(repl, body), hits


def collapse_dups(text, log):
    """折叠 ASR 结巴（`我我`→`我`）。只动 DUP_SAFE 里的字，且循环到稳定
    —— 否则 `我我我` 一遍只变 `我我`，第二遍还会改，破坏幂等。"""
    if not DUP_SAFE:
        return text
    # 注意必须是**字符类** `[我你]`，不能写成 `(我|你|…)` 那种字面串：
    # 后者会去匹配"我你他"这个整体再要求重复一次，永远不命中。
    chars = "".join(re.escape(c) for c in sorted(DUP_SAFE))
    dup = re.compile("([%s])\\1+" % chars)
    total = 0
    while True:
        text, n = dup.subn(r"\1", text)
        total += n
        if n == 0:
            break
    if total and log is not None:
        log["<叠字折叠>"] = total
    return text


def mechanical(text, log, collapse=False):
    n = 0
    for full, half in FULL2HALF.items():
        c = text.count(full)
        if c:
            text = text.replace(full, half)
            n += c
    if n and log is not None:
        log["<全角标点→半角>"] = n
    # 多余空格：行首行尾与连续空格（正文里没有需要保留空格的场合）
    text2 = re.sub(r"[ \t]{2,}", " ", text)
    text2 = re.sub(r"[ \t]+(\r?\n)", r"\1", text2)
    if text2 != text:
        if log is not None:
            log["<多余空格>"] = 1
        text = text2
    if collapse:
        text = collapse_dups(text, log)
    return text


def line_starts(text):
    starts = [0]
    for m in re.finditer(r"\r?\n", text):
        starts.append(m.end())
    return starts


def context_of(line, start, end, span=12):
    a = max(0, start - span)
    b = min(len(line), end + span)
    left = "…" if a > 0 else ""
    right = "…" if b < len(line) else ""
    return "%s%s%s" % (left, line[a:b].replace("\t", " "), right)


def calibrate(body, rules, collapse=False, max_passes=8):
    """跑到不动点（通常是 1 遍），并自检。

    为什么不是严格"只跑一遍"：单遍交替虽然避免了"A 规则污染 B 规则"，
    但存在**线序依赖**——键里含着另一条规则的**输出**时，同一遍就匹配不到。
    实例：`曾森→曾参` 和 `曾参和有弱→曾参和有若`，正文里是"曾森和有弱"，
    第一遍只能改前一句，把键拼好之后第二遍才命中。实测就踩到这一条。
    所以这里跑到不再变化为止，上限 8 遍；`passes > 1` 会明确报出来，
    因为那说明规则表里有依赖关系，值得回头把键写得更直接（如 `和有弱`）。

    反过来，规则自己喂自己（`拉克利特→赫拉克利特` 又命中已改好的
    `赫拉克利特`）会每遍多长一个字，永远不收敛——上限一到就报错，
    并指出是哪条键在反复命中。这比"静默跑一遍"安全得多。
    """
    pattern = build_pattern(rules)
    cur = body
    passes = 0
    hits_log = []          # [(遍号, 该遍输入文本, 偏移, 原, 改)]
    log = {}               # 跨遍累计：每条键总命中次数
    while True:
        passes += 1
        new, hits = apply_rules(cur, rules, pattern, log)
        new = mechanical(new, log, collapse=collapse)
        if new == cur:
            passes -= 1    # 最后一遍什么都没改，不计
            break
        for off, key, value in hits:
            hits_log.append((passes, cur, off, key, value))
        cur = new
        if passes >= max_passes:
            hot = [k for k in rules if log.get(k, 0) > 1]
            raise SystemExit(
                "规则在 %d 遍内没有收敛，最可能是某条键在吃自己产出的文本：%s\n"
                "　（典型：`拉克利特→赫拉克利特` 会命中已改好的「赫拉克利特」，"
                "每遍多一个字）\n　解药：另加一条空转守卫 `赫拉克利特\\t赫拉克利特`，"
                "或把键写得只可能命中错误形态。"
                % (max_passes, "、".join(hot[:6]) or "（无法定位，见 --check 输出）"))

    ledger = []
    for pass_no, text, off, key, value in hits_log:
        starts = line_starts(text)
        lines = text.splitlines()
        ln = bisect_right(starts, off)
        line = lines[ln - 1] if 0 < ln <= len(lines) else ""
        col = off - starts[ln - 1]
        ledger.append({
            "pass": pass_no, "line": ln, "col": col, "key": key,
            "value": rules[key].value, "why": rules[key].why,
            "ctx": context_of(line, col, col + len(key)),
        })
    return cur, {
        "hits": hits_log, "ledger": ledger, "log": log,
        "passes": passes, "converged": True,
        "never_fired": [k for k in rules if log.get(k, 0) == 0],
        "mech_fired": {k: v for k, v in log.items() if k.startswith("<")},
        "collapse": collapse,
    }


def write_outputs(path_in, out_dir, name, text, rep, review_path=None):
    import os
    os.makedirs(out_dir, exist_ok=True)
    p_md = os.path.join(out_dir, name + ".md")
    p_led = os.path.join(out_dir, name + "_改动台账.md")
    p_rev = os.path.join(out_dir, name + "_待复核.md")

    # 换行风格跟着原稿走：Windows 上混行会让不少编辑器与校验脚本出怪问题
    with open(path_in, "rb") as f:
        raw = f.read()
    crlf = b"\r\n" in raw
    text = text.replace("\r\n", "\n")
    if crlf:
        text = text.replace("\n", "\r\n")
    with open(p_md, "w", encoding="utf-8", newline="") as f:
        f.write(text)

    with open(p_led, "w", encoding="utf-8", newline="") as f:
        f.write(ledger_markdown(name, rep))
    with open(p_rev, "w", encoding="utf-8", newline="") as f:
        f.write(review_markdown(name, rep, review_path))
    return p_md, p_led, p_rev


def ledger_markdown(name, rep):
    rows = rep["ledger"]
    out = ["# 改动台账：%s" % name, ""]
    out.append("共 **%d** 处改动%s。"
               % (len(rows), "（跑了 %d 遍才稳定）" % rep["passes"]
                  if rep["passes"] > 1 else ""))
    if rep["mech_fired"]:
        out.append("")
        out.append("机械层：" + "，".join("%s %d 处" % (k.strip("<>"), v)
                                       for k, v in rep["mech_fired"].items()))
    out += ["", "| # | 遍 | 行 | 列 | 原 | 改 | 依据 | 上下文 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for i, r in enumerate(rows, 1):
        out.append("| %d | %d | %d | %d | %s | %s | %s | %s |"
                   % (i, r["pass"], r["line"], r["col"] + 1, cell(r["key"]),
                      cell(r["value"]), cell(r["why"] or "—"), cell(r["ctx"])))
    out.append("")
    out.append("> 行/列按**正文**起算（不含 frontmatter），且指**该遍输入文本**里的位置；"
               "只有第 1 遍的位置能直接对应原稿。多分 P 或长稿回查音频时，"
               "用「上下文」列定位最可靠。")
    return "\n".join(out) + "\n"


def cell(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def review_markdown(name, rep, review_path):
    out = ["# 待复核：%s" % name, ""]
    if review_path:
        with open(review_path, "r", encoding="utf-8") as f:
            out.append(f.read().rstrip())
            out.append("")
    out += ["## 机械检查", ""]
    out.append("- 收敛：跑到不动点共 **%d 遍**%s"
               % (rep["passes"],
                  "" if rep["passes"] == 1 else
                  "　→ 说明有规则依赖别的规则的产出（线序依赖），结果不受影响，"
                  "但值得把键写得直接些"))
    multi = {}
    for r in rep["ledger"]:
        if r["pass"] > 1:
            multi[r["key"]] = multi.get(r["key"], 0) + 1
    if multi:
        out.append("- 第 2 遍及以后才命中的键：%s"
                   % "、".join("`%s`×%d" % (k, v) for k, v in multi.items()))
    if rep["never_fired"]:
        out.append("- **从未命中的规则 %d 条**。两种解释：① 键打错了或这份稿里本来"
                   "没有这个走音；② 它被某条更长的规则遮蔽了（单遍最长优先）。"
                   "都不用改结果，但值得回看一眼：" % len(rep["never_fired"]))
        for k in rep["never_fired"][:30]:
            out.append("    - `%s`" % k)
        if len(rep["never_fired"]) > 30:
            out.append("    - …另有 %d 条" % (len(rep["never_fired"]) - 30))
    else:
        out.append("- 全部规则都命中过")
    out.append("- 说明：本工具**单遍交替 + 跑到不动点**，正文里不会留下被规则覆盖的键，"
               "所以没有\"残留\"这类报告；真正要人眼盯的是**规则没列出的第三种写法**"
               "（同一个走音换个写法），那要靠通读或按后两字扫全文的频次表。")
    return "\n".join(out) + "\n"


def selftest():
    """内置用例：每条都对应文档里记过的一个陷阱。"""
    import tempfile
    import os
    fails = []
    checks = [0]

    def check(name, got, want):
        checks[0] += 1
        if got != want:
            fails.append("%s\n     got=%r\n    want=%r" % (name, got, want))
            print("  FAIL %s" % name)
        else:
            print("  ok   %s" % name)

    d = tempfile.mkdtemp(prefix="calib-")
    p = os.path.join(d, "r.tsv")
    with open(p, "w", encoding="utf-8") as f:
        f.write("# 陷阱 1：短键 + 长键同时存在\n")
        f.write("女性批判\t理性批判\t短键\n")
        f.write("纯粹女性批判\t纯粹理性批判\t长键应赢\n")
        f.write("# 陷阱 3：键是正确词的子串，靠空转守卫保护\n")
        f.write("拉克利特\t赫拉克利特\t补全前两字\n")
        f.write("赫拉克利特\t赫拉克利特\t空转守卫\n")
        f.write("编程的\t编成的\t同音\n")
    rules = load_rules([p])
    pat = build_pattern(rules)

    # 1. 单遍最长优先：短键不得污染长键
    out, _ = apply_rules("纯粹女性批判", rules, pat)
    check("长键先赢（短键不污染）", out, "纯粹理性批判")

    # 2. 幂等：结果再跑一遍不变
    out2, hits2 = apply_rules(out, rules, pat)
    check("幂等（再跑零改动）", (out2, len(hits2)), ("纯粹理性批判", 0))

    # 3. 空转守卫：正确形态不被短键吃掉
    out3, _ = apply_rules("赫拉克利特", rules, pat)
    check("空转守卫保护正确形态", out3, "赫拉克利特")
    out4, _ = apply_rules("拉克利特", rules, pat)
    check("短键仍能补全错形", out4, "赫拉克利特")

    # 4. 全角标点 → 半角
    log = {}
    check("全角标点转半角", mechanical("你好，是吗？是的！好；行：", log),
          "你好,是吗?是的!好;行:")
    check("句号顿号不动", mechanical("一二、三。", {}), "一二、三。")

    # 5. 叠字折叠要循环到稳定（否则破坏幂等）
    out5 = collapse_dups("我我我你你你他他", {})
    check("叠字折叠到稳定", out5, "我你他")
    check("合法重叠不动", collapse_dups("慢慢来，天天练", {}), "慢慢来，天天练")

    # 6. 零命中规则要报出来（这才是单遍架构下真正会响的检查）
    body = "行一：纯粹女性批判\n行二：编程的时间\n"
    _, rep = calibrate(body, rules)
    check("完全不存在的键被报出", "拉克利特" in rep["never_fired"], True)
    # 被更长规则遮蔽的短键同样算"零命中"—— 这是诚实的语义：
    # 它确实一次都没替换过，只是不用它替换而已。报告里要说清这一点。
    check("被长键遮蔽的短键也在其中", "女性批判" in rep["never_fired"], True)
    check("命中过的规则不报", "编程的" in rep["never_fired"], False)
    check("改动数与台账一致", len(rep["ledger"]), 2)

    if fails:
        print("\n%d 个用例未通过" % len(fails))
        return 1
    print("\n全部通过（%d 项）" % checks[0])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="按规则表校订转写稿，产出 校订稿 + 改动台账 + 待复核",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="转写稿（.md/.txt）")
    ap.add_argument("--rules", action="append", default=None,
                    help="规则表 TSV（可给多个：如 定点.tsv 词典.tsv）")
    ap.add_argument("--out-dir", default=None, help="输出目录（默认与输入同目录）")
    ap.add_argument("--name", default=None, help="输出文件名主干（默认用输入的）")
    ap.add_argument("--review", default=None, help="手写的待复核清单（markdown）")
    ap.add_argument("--collapse-dup", action="store_true",
                    help="折叠 ASR 结巴叠字（只动绝不可能叠用的字）")
    ap.add_argument("--allow-delete", action="store_true", help="允许\"改\"列为空")
    ap.add_argument("--check", action="store_true", help="只报告，不写文件")
    ap.add_argument("--json", dest="json_out", metavar="PATH",
                    help="把结果写成 JSON（passes / 改动数 / 零命中规则清单），供上层断言")
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="随机抽 N 处改动打到屏幕上（人工抽检用，配合 --check 最好）")
    ap.add_argument("--strict", action="store_true", help="有残留即退出码非 0")
    ap.add_argument("--selftest", action="store_true", help="跑内置用例")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.input or not args.rules:
        ap.error("需要 input 与至少一个 --rules（或用 --selftest）")

    with open(args.input, "r", encoding="utf-8") as f:
        raw = f.read()
    m = FM_RE.match(raw)
    head, body = (raw[:m.end()], raw[m.end():]) if m else ("", raw)
    if not body.strip():
        raise SystemExit("正文是空的：%s" % args.input)

    rules = load_rules(args.rules, allow_delete=args.allow_delete)
    new_body, rep = calibrate(body, rules, collapse=args.collapse_dup)

    import os
    name = args.name or os.path.splitext(os.path.basename(args.input))[0]
    out_dir = args.out_dir or os.path.dirname(os.path.abspath(args.input))

    print("规则 %d 条，改动 %d 处（机械层 %d 处），%d 遍收敛"
          % (len(rules), len(rep["ledger"]),
             sum(rep["mech_fired"].values()), rep["passes"]))
    if rep["never_fired"]:
        print("从未命中 %d 条：%s" % (len(rep["never_fired"]),
                                     "、".join(rep["never_fired"][:8])))

    if args.sample and rep["ledger"]:
        import random
        k = min(args.sample, len(rep["ledger"]))
        rows = random.sample(rep["ledger"], k)
        print("\n抽检 %d/%d 处：" % (k, len(rep["ledger"])))
        for r in rows:
            print("  第%d遍 行%d  %s → %s" % (r["pass"], r["line"], r["key"],
                                             r["value"]))
            print("      …%s…" % r["ctx"])
            if r["why"]:
                print("      依据：%s" % r["why"])

    if args.json_out:
        import json as _json
        with open(args.json_out, "w", encoding="utf-8") as f:
            _json.dump({
                "input": args.input,
                "rules": len(rules),
                "passes": rep["passes"],
                "changes": len(rep["ledger"]),
                "mech": sum(rep["mech_fired"].values()),
                "never_fired": rep["never_fired"],
                "hit_keys": sorted({r["key"] for r in rep["ledger"]}),
            }, f, ensure_ascii=False, indent=1)

    if args.check:
        print("--check：不写文件")
        return 0

    pmd, pled, prev = write_outputs(args.input, out_dir, name,
                                    head + new_body, rep, args.review)
    print("校订稿 ：%s" % pmd)
    print("改动台账：%s" % pled)
    print("待复核 ：%s" % prev)
    if args.strict and rep["never_fired"]:
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
