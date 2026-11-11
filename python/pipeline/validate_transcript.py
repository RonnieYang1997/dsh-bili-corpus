#!/usr/bin/env python3
"""
转录稿校验器 —— 可对**任意输出目录**跑（不绑定独夫之心语料布局）。

为什么需要它：`corpus_lint.py` 是语料库守卫，`--root` 会调 `assert_corpus_root()`
（要求目录里有上千篇 `dufu-BV*.md`），因此**对一个新的输出目录跑不了**。
本脚本把同一套判据抽出来，做成"落盘后立刻自检"的一步。

判据来源与 `corpus_lint.py` 保持一致（PUA 正则、乱码字面量、BOM、U+FFFD），
并补齐转录场景特有的：frontmatter 缺失/未闭合、正文为空、
API 错误载荷被当成转录稿、占位日期、混行。

用法：
  python validate_transcript.py --dir ./out
  python validate_transcript.py --dir ./out --min-chars 500 --json report.json
  python validate_transcript.py f1.md f2.md

退出码：0 = 全部通过（允许 WARN）；1 = 存在 FAIL；2 = 用法错误。
"""

import argparse
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --- 判据（与 corpus_lint.py 同源） ---------------------------------------
PUA = re.compile("[\ue000-\uf8ff]")
MOJIBAKE = ["鏃ユ湡", "锛", "瀛楃", "杞綍", "璇煶", "绠€", "疄浣", "鍒嗕韩", "鑻忚仈"]
FM_OPEN = re.compile(r"^---\r?\n")
FM_ANY = re.compile(r"^(?:\ufeff?---\r?\n.*?\r?\n---\r?\n[ \t]*\r?\n?)", re.S)
PLACEHOLDER_DATES = {"1970-01-01", "0000-00-00", "None", "null"}

# 端点用 HTTP 200 回错误载荷的历史事故（R21）
API_ERR = re.compile(r'^\s*\{\s*"(error|code|message)"\s*:', re.S)
API_ERR_HINT = ("too_large", "Request Entity Too Large", "Gateway", "rate limit")


def _fail(rule, detail=""):
    return ("FAIL", rule, detail)


def _warn(rule, detail=""):
    return ("WARN", rule, detail)


def parse_frontmatter(text):
    """返回 (fm_dict_or_None, body, body_start_index)。"""
    m = FM_ANY.match(text)
    if not m:
        return None, text, 0
    block = m.group(0)
    inner = re.sub(r"^\ufeff?---\r?\n", "", block, count=1)
    inner = re.sub(r"\r?\n---\r?\n[ \t]*\r?\n?$", "", inner, count=1)
    fm = {}
    for line in inner.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm, text[m.end():], m.end()


def check_file(path, min_chars=1):
    """返回 (issues, info)。issues 是 (级别, 规则, 细节) 列表。"""
    issues = []
    info = {"path": path, "bytes": 0, "chars": 0, "bvid": "", "title": "",
            "date": "", "duration": "", "eol": "", "cached": False}

    try:
        raw = open(path, "rb").read()
    except OSError as e:
        return [_fail("无法读取", str(e))], info
    info["bytes"] = len(raw)

    # BOM（R1/R7）
    if raw.startswith(b"\xef\xbb\xbf"):
        issues.append(_fail("UTF-8 BOM 存在", "文件必须以 '---' 为第一个字节"))

    text = raw.decode("utf-8", "replace")
    info["chars"] = len(text)

    # 换行：混行是明确的缺陷；纯 LF / 纯 CRLF 都合法（R26 修正了"必须 CRLF"的误解）
    n_lf = text.count("\n")
    n_crlf = text.count("\r\n")
    if n_lf == 0:
        info["eol"] = "none"
    elif n_crlf == n_lf:
        info["eol"] = "crlf"
    elif n_crlf == 0:
        info["eol"] = "lf"
    else:
        info["eol"] = "mixed"
        issues.append(_fail("混行", "CRLF %d 行 / LF 共 %d 行" % (n_crlf, n_lf)))

    # frontmatter（R7/R17）
    if not FM_OPEN.match(text.lstrip("\ufeff")):
        issues.append(_fail("frontmatter 缺失", "文件第一个字节必须是 '---'"))
        fm, body = None, text
    else:
        fm, body, _ = parse_frontmatter(text)
        if fm is None:
            issues.append(_fail("frontmatter 未闭合", "找不到收尾的 '---'"))
        elif not fm:
            # `---␊␊---`：整块被掏空（R17 的事故形态）
            issues.append(_fail("frontmatter 为空", "疑似被'解析→重组'掏空"))

    if fm:
        info["bvid"] = fm.get("bvid", "")
        info["title"] = fm.get("title", "")
        info["date"] = fm.get("date", "")
        info["duration"] = fm.get("duration", "")

        if not fm.get("title"):
            issues.append(_warn("title 为空", ""))
        if not fm.get("bvid"):
            issues.append(_warn("bvid 为空", ""))
        # R6/R14：date 必须是 pubdate；空是"诚实"，占位符才是缺陷
        if info["date"] in PLACEHOLDER_DATES:
            issues.append(_fail("date 是占位符", info["date"]))
        elif not info["date"]:
            issues.append(_warn("date 为空", "取不到 pubdate 时应留空（R6），但值得回源补"))
        elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", info["date"]):
            issues.append(_warn("date 格式可疑", info["date"]))
        if str(info["duration"]).strip() in ("", "0"):
            issues.append(_warn("duration 为 0", ""))
        if not fm.get("source"):
            issues.append(_warn("source 缺失", ""))

        # bvid 与文件名是否对得上（只在文件名里能提取到 BV 号时比）
        m = re.search(r"(BV[0-9A-Za-z]{10})", os.path.basename(path))
        if m and info["bvid"] and m.group(1) != info["bvid"]:
            issues.append(_fail("bvid 与文件名不符",
                                "文件名 %s / frontmatter %s" % (m.group(1), info["bvid"])))

    # 正文
    body_stripped = body.strip()
    if not body_stripped:
        issues.append(_fail("正文为空", ""))
    elif len(body_stripped) < min_chars:
        issues.append(_warn("正文过短", "%d 字 < 阈值 %d" % (len(body_stripped), min_chars)))
    info["chars"] = len(body_stripped)

    # 编码类缺陷
    if "\ufffd" in text:
        issues.append(_fail("U+FFFD 替换字符", "解码已损坏"))
    if PUA.search(text):
        issues.append(_fail("私用区字符 (PUA)", "GBK 误解码残留"))
    for pat in MOJIBAKE:
        if pat in text:
            issues.append(_fail("乱码字面量", repr(pat)))
            break

    # R21：错误载荷绝不能当成转录稿
    if API_ERR.match(body_stripped) and len(body_stripped) < 2000:
        issues.append(_fail("正文疑似 API 错误载荷", body_stripped[:60]))
    else:
        head = body_stripped[:400]
        if any(h in head for h in API_ERR_HINT) and len(body_stripped) < 500:
            issues.append(_warn("正文疑似端点报错文本", head[:60]))

    return issues, info


def collect(paths, glob_pat="*.md"):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if glob_pat == "*" or (name.endswith(glob_pat.lstrip("*"))):
                    fp = os.path.join(p, name)
                    if os.path.isfile(fp):
                        files.append(fp)
        elif os.path.isfile(p):
            files.append(p)
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description="转录稿校验器（可对任意目录跑）")
    ap.add_argument("paths", nargs="*", help="文件或目录（默认：当前目录）")
    ap.add_argument("--dir", action="append", default=None, help="输出目录（可重复）")
    ap.add_argument("--glob", default="*.md", help="目录内匹配（默认 *.md）")
    ap.add_argument("--min-chars", type=int, default=1,
                    help="正文少于该字数记 WARN（默认 1，即只查空稿）")
    ap.add_argument("--json", default=None, help="把结果写成 JSON 报告")
    ap.add_argument("--quiet", action="store_true", help="只打印汇总")
    args = ap.parse_args(argv)

    targets = list(args.paths or []) + list(args.dir or [])
    if not targets:
        targets = [os.getcwd()]
    files = collect(targets, args.glob)
    if not files:
        print("没有匹配到文件：%s" % ", ".join(targets))
        return 2

    results, n_fail, n_warn = [], 0, 0
    for fp in files:
        issues, info = check_file(fp, args.min_chars)
        f = [i for i in issues if i[0] == "FAIL"]
        w = [i for i in issues if i[0] == "WARN"]
        n_fail += len(f)
        n_warn += len(w)
        verdict = "FAIL" if f else ("WARN" if w else "PASS")
        results.append({"file": fp, "verdict": verdict, "info": info,
                        "fail": [{"rule": r, "detail": d} for _, r, d in f],
                        "warn": [{"rule": r, "detail": d} for _, r, d in w]})
        if not args.quiet:
            print("%-6s %-46s %6d 字  %-4s  %s"
                  % (verdict, os.path.basename(fp), info["chars"], info["eol"],
                     "; ".join(r for _, r, _ in f) or "; ".join(r for _, r, _ in w)))

    n_pass = sum(1 for r in results if r["verdict"] == "PASS")
    n_warn_f = sum(1 for r in results if r["verdict"] == "WARN")
    n_fail_f = sum(1 for r in results if r["verdict"] == "FAIL")

    print()
    print("校验 %d 篇：PASS %d / WARN %d / FAIL %d（FAIL 项 %d，WARN 项 %d）"
          % (len(results), n_pass, n_warn_f, n_fail_f, n_fail, n_warn))
    if n_fail_f:
        print("失败明细：")
        for r in results:
            if r["verdict"] == "FAIL":
                for it in r["fail"]:
                    print("  %s :: %s %s" % (os.path.basename(r["file"]), it["rule"], it["detail"]))

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"total": len(results), "pass": n_pass, "warn": n_warn_f,
                       "fail": n_fail_f, "results": results}, f,
                      ensure_ascii=False, indent=2)
        print("报告 -> %s" % args.json)

    return 1 if n_fail_f else 0


if __name__ == "__main__":
    sys.exit(main())
