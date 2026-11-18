#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""patch_lint.py —— 补丁自检（R47）。在 apply 之前跑，一次把"会被丢弃"的原因说清楚。

背景：apply_smoothing.py 的闸门是对的（它必须拦），但它只能在 apply 时逐条报错，
写补丁的人要 dry-run 七八轮才能把丢弃数降到 0。本工具把同一套判据前置成静态检查，
并把**改法**直接写进输出：改 kind、拆成两条、加 all、给 find 补左邻字。

用法：
    patch_lint.py --doc <稿件路径> [补丁.json | --dir 补丁目录] [--json]

判据（与 apply_smoothing.py 保持一致）：
  1. find 必须能在正文里定位（字面优先，退而用"全角/半角无关"比较）。
  2. 非 stutter 的 kind 最多删 2 个字。
  3. stutter：删 1~8 字、改 ≤2 字、插入的字符必须全是标点/空白。
  4. 同一 find 多处命中必须写 occurrence 或 all。
  5. 自食：replace 里含 find，跑起来会越改越长。
  6. 顺序：补丁按 find 长度降序执行；若某条的 find 被更长的补丁改没了，就是顺序/拆链问题。
"""
import os
import re
import io
import sys
import json
import difflib
import argparse
import unicodedata

KINDS = {"homophone", "name", "punct", "stutter", "number", "other"}
_FW2HW = str.maketrans({
    "，": ",", "。": ".", "？": "?", "！": "!", "；": ";",
    "：": ":", "、": ",", "（": "(", "）": ")", "“": '"', "”": '"',
    "‘": "'", "’": "'", "《": "<", "》": ">",
})


def norm(s):
    return s.translate(_FW2HW)


def read_doc(path):
    raw = io.open(path, "r", encoding="utf-8", newline="").read()
    if raw.startswith("---"):
        i = raw.find("\n---", 3)
        if i >= 0:
            j = raw.find("\n", i + 4)
            if j > 0:
                return raw[j + 1:]
    return raw


def find_positions(body, f):
    """字面命中 -> 全角容错命中。返回 (positions, tolerant)。"""
    out, i = [], body.find(f)
    while i >= 0:
        out.append(i)
        i = body.find(f, i + 1)
    if out:
        return out, False
    fn = norm(f)
    if fn == f or not f:
        return [], False
    bn = norm(body)
    i = bn.find(fn)
    while i >= 0:
        out.append(i)
        i = bn.find(fn, i + 1)
    return out, bool(out)


def is_punct_only(s):
    return all(unicodedata.category(c).startswith("P") or c.isspace() for c in s)


def _noncontent(c):
    return c.isspace() or unicodedata.category(c).startswith("P")


def lint_one(p, body, applied_so_far):
    """返回 (level, message)。level: ok / warn / error。"""
    f = p.get("find") or ""
    r = p.get("replace")
    kind = p.get("kind") or ""
    if r is None:
        r = ""
    if not f:
        return "error", "find 为空"
    if kind not in KINDS:
        return "error", "kind=%r 不合法（可选：%s）" % (kind, "/".join(sorted(KINDS)))

    pos, tolerant = find_positions(body, f)
    if applied_so_far is not None and not pos:
        pos2, tol2 = find_positions(applied_so_far, f)
        if pos2:
            return ("warn", "本条的 find 只在「前序补丁改完之后」才出现 —— apply 按 find 长度降序执行，"
                            "请把这一条拆成链式两步：前一步 find 更长、后一步 find 更短")
    if not pos:
        # 已经是改后文本？那叫冗余，不算错
        if r and r in body:
            return "warn", "find 已不存在但 replace 已在正文里（冗余，apply 会跳过并记 redundant）"
        return "error", "find 在正文里找不到（含全角容错）；若正文里是另一处写法，请先看上下文再写"

    cnt = len(pos)
    occ = p.get("occurrence")
    if cnt > 1 and occ is None and not p.get("all"):
        return "error", ("find 命中 %d 处且未指定 occurrence/all —— 加 \"occurrence\": 1"
                         "（第几处，从 1 数）或 \"all\": true" % cnt)
    if occ is not None and (occ < 1 or occ > cnt):
        return "error", "occurrence=%s 越界（本 find 命中 %d 处）" % (occ, cnt)

    shorten = len(f) - len(r)
    if shorten > 2 and kind != "stutter":
        return "error", ("kind=%s 却删掉 %d 个字 —— 要么改成 \"kind\": \"stutter\"（纯删除/重复字），"
                         "要么拆成两条：先做等长替换 a→b，再单独做纯删除" % (kind, shorten))
    if kind == "stutter":
        sm = difflib.SequenceMatcher(None, f, r)
        dele = ins = sub = 0
        ins_text = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "delete":
                dele += i2 - i1
            elif tag == "insert":
                ins += j2 - j1
                ins_text.append(r[j1:j2])
            elif tag == "replace":
                sub += max(i2 - i1, j2 - j1)
        if not (1 <= dele <= 8) or sub > 2 or ins_text and not is_punct_only("".join(ins_text)):
            return "error", ("stutter 判据不满足（删 %d 增 %d 改 %d 字）—— 只允许删 1~8 字 + 最多改 2 字 + "
                             "插入只能是标点。改法：先 \"kind\": \"name\" 做等长替换，再 \"kind\": \"stutter\" 删重复"
                             % (dele, ins, sub))

    if r and f in r:
        return "error", "自食：replace 里含 find（跑起来会越改越长）—— 给 find 加左邻字（如 。xxx）或改 replace 措辞"
    if tolerant:
        return "warn", "靠「全角/半角等价」才命中 —— 能落地，但建议把 find 的标点写成与正文一致"
    return "ok", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("patch", nargs="?", help="补丁 json")
    ap.add_argument("--dir", help="补丁目录")
    ap.add_argument("--doc", required=True, help="稿件路径（读正文）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    files = []
    if a.dir:
        files = [os.path.join(a.dir, n) for n in sorted(os.listdir(a.dir)) if n.endswith(".json")]
    elif a.patch:
        files = [a.patch]
    else:
        ap.error("要么给补丁 json，要么给 --dir")

    body = read_doc(a.doc)
    report = {"doc": a.doc, "files": [], "ok": 0, "warn": 0, "error": 0}
    for fn in files:
        spec = json.load(io.open(fn, "r", encoding="utf-8"))
        if "patches" not in spec and len(spec) == 1:
            spec = list(spec.values())[0]
        patches = spec.get("patches") or []
        ordered = sorted(patches, key=lambda p: -len(p.get("find") or ""))
        sim = body
        items = []
        for idx, p in enumerate(ordered):
            level, msg = lint_one(p, body, sim)
            if level != "error":
                f = p.get("find") or ""
                r = p.get("replace") or ""
                if sim.count(f) == 1:
                    sim = sim.replace(f, r, 1)
                elif sim.count(f) > 1:
                    if p.get("all"):
                        sim = sim.replace(f, r)
                    elif p.get("occurrence"):
                        parts = sim.split(f)
                        k = p["occurrence"]
                        sim = f.join(parts[:k]) + r + f.join(parts[k:])
                else:
                    pos, _ = find_positions(sim, f)
                    for t in sorted(pos, reverse=True):
                        sim = sim[:t] + r + sim[t + len(f):]
            items.append({"i": idx, "find": (p.get("find") or "")[:60],
                          "replace": (p.get("replace") or "")[:60],
                          "kind": p.get("kind"), "level": level, "msg": msg})
            report[level] += 1
        report["files"].append({"file": os.path.basename(fn), "count": len(patches), "items": items})

    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for f in report["files"]:
            print("== %s  %d 条" % (f["file"], f["count"]))
            for it in f["items"]:
                if it["level"] == "ok":
                    continue
                mark = "!!" if it["level"] == "error" else " ~"
                print("%s #%d [%s] %r -> %r" % (mark, it["i"], it["kind"], it["find"], it["replace"]))
                print("     %s" % it["msg"])
        print("---- ok %d / warn %d / error %d" % (report["ok"], report["warn"], report["error"]))
    return 1 if report["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
