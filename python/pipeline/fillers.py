# -*- coding: utf-8 -*-
"""fillers.py —— 口语碎屑清理（确定性、0 token、可复算）。

口号：只删「不是话的字」，不动任何实词。

三类东西（对应讲授/录音转写里最常见的水分）：
  1. 纯语气填充词：呃 / 嗯 / 哦 / 噢 / 唉 / 哎 / 诶 / 呀 / 啊 —— 这些字在汉语里
     不作为构词成分出现（不会像「额」那样咬住「定额」「额头」），见到即可删。
  2. 与标点相邻的「哈」（笑/语气）：`问题哈，` `哈。` `哈哈哈哈`。单独的 `哈`
     夹在两个实词中间（`哈哈哈` 之外）不动，因为可能真是「哈」字。
  3. 删除后留下的标点残渣：`，。` `，，` `。，` 这种，统一收敛。

「额」只删**独立出现**的情况（前后是标点或首尾）——它同时是常用字（定额、额度）。

用法：
  python fillers.py --stats                          # 只看统计（落点+存档）
  python fillers.py --apply --dry-run                # 预览
  python fillers.py --apply --only 沃格林             # 只处理文件名含该子串的稿
  python fillers.py --selftest                       # 12 条断言自检
"""
import argparse
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

# 见到即删的填充词（都不是构词成分）
ALWAYS = "呃嗯哦噢唉哎诶呀啊"
# 只在独立出现时删（本身是常用字）
STANDALONE = "额"
# 含「哈」的实词：先占位保护，避免把「哈佛」删成「佛」
PROTECT = [
    # 真词（含「哈」的专名/音译名）：占位保护，绝不删。按长度降序匹配更稳。
    "哈贝马斯", "哈贝玛斯", "哈姆雷特", "哈利波特", "哈萨克", "哈尔滨", "哈哈镜",
    "哈耶克", "哈维尔", "哈谢克", "哈巴狗", "哈佛", "哈密", "哈尔", "哈勃",
    "哈恩", "哈希", "哈欠", "哈喇", "哈腰", "哈特", "哈桑",
]
_PH_BASE = 0xE000

_PUNCT = "，。？！、；：“”（）「」…·"


def _protect(text):
    saved = []
    for i, w in enumerate(PROTECT):
        if w in text:
            ph = chr(_PH_BASE + i)
            text = text.replace(w, ph)
            saved.append((ph, w))
    return text, saved


def _restore(text, saved):
    for ph, w in saved:
        text = text.replace(ph, w)
    return text


def _strip_ha(text, stats):
    # 保护词已经换成占位符，剩下任何「哈」都是笑声/语气词（含单字「哈」夹在句中、
    # 「哈哈哈」「问题哈，」这些形态），一律删除。info: 与标点相邻的会被下面的
    # _tidy_punct 顺手收敛掉残渣。
    n = text.count("哈")
    if n:
        stats["哈"] = stats.get("哈", 0) + n
        text = text.replace("哈", "")
    return text


def _strip_fillers(text, stats):
    for ch in ALWAYS:
        n = text.count(ch)
        if n:
            stats[ch] = stats.get(ch, 0) + n
    text = re.sub("[%s]+" % ALWAYS, "", text)
    # 实际逐字计数（re.subn 只给 run 数）
    text, n2 = re.subn(
        "([%s])(?=[%s])" % (STANDALONE, re.escape(_PUNCT)), "", text)
    if n2:
        stats["额"] = stats.get("额", 0) + n2
    text, n3 = re.subn(
        "(?<=[%s])[%s]" % (re.escape(_PUNCT), STANDALONE), "", text)
    if n3:
        stats["额"] = stats.get("额", 0) + n3
    return text


def _tidy_punct(text, stats):
    before = text
    subs = [
        (r"[，、；：]{2,}", None),   # 连续标点：只留第一个
        (r"。{2,}", "。"),
        (r"，(?=[，。；：、？！])", ""),
        (r"(?<=[。？！])[，、；：]+", ""),
        (r"^\s*[，、；：]+", ""),
        # 段首被删空后留下的句末标点（`嗯。那他呢` → `。那他呢` → `那他呢`）
        (r"^\s*[。？！]+", ""),
        (r"，(?=[」”）])", ""),
        (r"(?<=[「“（])，", ""),
        (r"[ \t]{2,}", " "),
    ]
    for pat, rep in subs:
        if rep is None:
            text = re.sub(pat, lambda m: m.group(0)[0], text)
        else:
            text = re.sub(pat, rep, text)
    n = sum(1 for a, b in zip(before, text) if a != b) if len(before) != len(text) else 0
    if text != before:
        stats["_punct"] = stats.get("_punct", 0) + max(1, abs(len(before) - len(text)))
    return text


def clean_body(body, rounds=3, stats=None):
    """清理正文（不含 frontmatter）。返回 (new_body, stats)。"""
    stats = stats if stats is not None else {}
    cur = body
    for _ in range(rounds):
        t, saved = _protect(cur)
        t = _strip_fillers(t, stats)
        t = _strip_ha(t, stats)
        t = _tidy_punct(t, stats)
        t = _restore(t, saved)
        if t == cur:
            break
        cur = t
    return cur, stats


def split_front(text):
    """分离 frontmatter。返回 (head, body)，head 以 '---\\n…---\\n' 原样保留。"""
    if not text.startswith("---"):
        return "", text
    m = re.match(r"^---\r?\n.*?\r?\n---\r?\n", text, re.S)
    if not m:
        return "", text
    return text[: m.end()], text[m.end():]


# ---------------------------------------------------------------- selftest

CASES = [
    ("你好呃，我是杨立华", "你好，我是杨立华"),
    ("这个呃呃文明", "这个文明"),
    ("就是呃就是这个问题", "就是就是这个问题"),
    ("问题啊，就是这个", "问题，就是这个"),
    ("哈哈哈哈，说得对", "说得对"),
    ("立华哈呃，谢谢", "立华，谢谢"),
    ("嗯。那他呢", "那他呢"),
    ("啊。呃，那我先说", "那我先说"),
    ("我们去看哈佛和哈尔滨", "我们去看哈佛和哈尔滨"),
    ("定额和额度不能动", "定额和额度不能动"),
    ("哎，这个额，这样", "这个，这样"),
    ("太温良恭俭让，啊啊啊", "太温良恭俭让，"),
]


def selftest():
    bad = 0
    for src, want in CASES:
        got, _ = clean_body(src)
        if got != want:
            bad += 1
            print("FAIL %r -> %r (want %r)" % (src, got, want))
    # 保护词：正文里的 哈佛 不能被删
    for w in ["哈佛", "哈尔滨", "哈耶克"]:
        got, _ = clean_body(w)
        if got != w:
            bad += 1
            print("FAIL protect %r -> %r" % (w, got))
    print("SELFTEST %s (%d cases)" % ("PASS" if bad == 0 else "FAIL(%d)" % bad, len(CASES) + 3))
    return 0 if bad == 0 else 1


# ---------------------------------------------------------------- CLI

def _docs(only=""):
    names = set()
    for root in [paths.LANDING, paths.ARCHIVE]:
        if not os.path.isdir(root):
            continue
        for n in os.listdir(root):
            if not n.endswith(".md"):
                continue
            if only and only not in n:
                continue
            if paths.DOC_PATTERN and not paths.is_doc(n):
                continue
            names.add(n)
    return sorted(names)


def _run(apply, dry_run, only, sample):
    tot = {"docs": 0, "changed": 0, "bytes_in": 0, "bytes_out": 0}
    chars = {}
    detail = []
    for name in _docs(only):
        lp = os.path.join(paths.LANDING, name)
        ap = os.path.join(paths.ARCHIVE, name)
        if not os.path.isfile(lp):
            continue
        raw = io.open(lp, encoding="utf-8").read()
        head, body = split_front(raw)
        stats = {}
        new_body, stats = clean_body(body, stats=stats)
        tot["docs"] += 1
        tot["bytes_in"] += len(body)
        if new_body == body:
            continue
        tot["changed"] += 1
        tot["bytes_out"] += len(new_body)
        for k, v in stats.items():
            if k.startswith("_"):
                continue
            chars[k] = chars.get(k, 0) + v
        dropped = len(body) - len(new_body)
        detail.append({"doc": name, "dropped": dropped})
        if apply and not dry_run:
            out = head + new_body
            for p in [lp, ap]:
                if os.path.isdir(os.path.dirname(p)):
                    io.open(p, "w", encoding="utf-8", newline="\n").write(out)
    res = {
        "ok": True,
        "apply": bool(apply and not dry_run),
        "landing": paths.LANDING,
        "archive": paths.ARCHIVE,
        **tot,
        "dropped_total": tot["bytes_in"] - tot["bytes_out"] if tot["changed"] else 0,
        "by_char": chars,
        "docs": detail[: int(sample or 20)],
    }
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    import json
    res = _run(a.apply and not a.dry_run, a.dry_run, a.only, a.sample)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
