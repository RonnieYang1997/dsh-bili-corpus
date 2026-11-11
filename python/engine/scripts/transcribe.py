#!/usr/bin/env python3
"""
B站视频 → 本地转录 → 落盘 → 校验（统一入口）

**只问两件事**：
  1. 要转哪些视频？（BV 号 / 完整链接 / 含 BV 号的文本文件都行）
  2. 稿子放哪个目录？
其余全部自动：取流、下载、本地 SenseVoice 识别、写 frontmatter、落盘、逐篇校验。

cookie 策略（无需用户操心）：
  · **先不带 cookie 试跑** —— B站公开视频的低清档位匿名即可取流；
  · 只有当某个视频真的因为"需要登录/风控/找不到"失败时，才向你索取一次，
    并存到用户级位置（%APPDATA%\\bilibili-transcribe\\cookies.txt，不进 git）。

示例：
  python transcribe.py                                   # 全交互
  python transcribe.py --urls "https://www.bilibili.com/video/BV1xx411c7mD" --out ./out
  python transcribe.py --bvids BV1xx411c7mD BV1yy411c7mE --out ./out -y
  python transcribe.py --profile dufu --bvids BV1xx411c7mD      # 独夫之心语料布局
"""

import argparse
import json
import os
import re
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
# 插件布局：引擎在 python/engine/scripts，校验器在 python/pipeline（同一仓库，跨目录导入）
_PIPELINE = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "pipeline")
if os.path.isfile(os.path.join(_PIPELINE, "validate_transcript.py")) and _PIPELINE not in sys.path:
    sys.path.insert(0, _PIPELINE)

import bilibili_transcribe as bt          # noqa: E402
import bili_credentials as cred            # noqa: E402
import validate_transcript as vt           # noqa: E402

BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")

# 需要 cookie 才能继续的信号（错误码或报错文本）
AUTH_SIGNALS = ("-101", "-404", "-403", "-352", "62002", "账号未登录",
                "风控", "not logged", "forbidden", "playurl 失败")

DEFAULT_OUT = os.path.join(os.getcwd(), "transcripts")


def eprint(*a):
    sys.stderr.write(" ".join(str(x) for x in a) + "\n")


# ---------------------------------------------------------------- 输入解析

def extract_bvids(items):
    """从任意文本（BV 号 / 链接 / 整段话）里抽出 BV 号，去重且保序。"""
    out, seen = [], set()
    for it in items:
        for b in BV_RE.findall(it or ""):
            if b not in seen:
                seen.add(b)
                out.append(b)
    return out


def read_list_file(path):
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            return f.read()
    except OSError as e:
        eprint("[warn] 读不了清单文件 %s：%s" % (path, e))
        return ""


def ask(question, default=None, allow_empty=False):
    """交互提问。非交互环境（无 tty）直接返回默认值。"""
    if not sys.stdin.isatty():
        return default or ""
    suffix = " [%s]" % default if default else ""
    while True:
        try:
            ans = input("%s%s: " % (question, suffix)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return default or ""
        if ans:
            return ans
        if default or allow_empty:
            return default or ""


# ---------------------------------------------------------------- cookie

def ensure_cookie(cookie=None, cookie_file=None, interactive=True, quiet=False):
    """尽力拿到可用 cookie；拿不到就返回空串（匿名试跑）。"""
    c = cred.resolve_cookie(explicit=cookie, cookie_file=cookie_file, quiet=quiet)
    if not c:
        if not quiet:
            print("  cookie：未提供 → 先匿名试跑（公开视频低清档位无需登录）")
        return ""
    v = cred.validate_cookie(c)
    if v["ok"]:
        if not quiet:
            print("  cookie：有效（已登录 %s，mid=%s）" % (v["uname"], v["mid"]))
        return c
    eprint("[warn] cookie 无效：%s" % v["reason"])
    if interactive and sys.stdin.isatty():
        return prompt_for_cookie(quiet=quiet)
    return ""


def prompt_for_cookie(quiet=False):
    """向用户索取 cookie 并落盘（用户级、不进 git）。"""
    print()
    print(cred.cookie_hint())
    print()
    raw = ask("请粘贴 cookie（或 cookie 文件路径；直接回车＝跳过）", allow_empty=True)
    if not raw:
        eprint("[warn] 未提供 cookie，继续匿名试跑")
        return ""
    if os.path.isfile(raw):
        c = cred.resolve_cookie(cookie_file=raw)
    else:
        c = cred.normalize_cookie(raw)
    if not c:
        eprint("[warn] 没解析出 cookie，继续匿名试跑")
        return ""
    v = cred.validate_cookie(c)
    if not v["ok"]:
        eprint("[warn] 这个 cookie 校验没过：%s（仍然先试跑）" % v["reason"])
        return c
    path = cred.save_cookie(c)
    print("  cookie 已保存到 %s（已登录 %s）" % (path, v["uname"]))
    return c


def needs_cookie(exc_text):
    t = str(exc_text)
    return any(s in t for s in AUTH_SIGNALS)


# ---------------------------------------------------------------- 主流程

def transcribe_one(bvid, out_dir, temp_dir, name_template, min_chars, keep_audio):
    """单篇：下载 → 本地识别 → 落盘 → 校验。返回结果 dict。"""
    t0 = time.time()
def transcribe_one(bvid, out_dir, temp_dir, name_template, min_chars, keep_audio,
                   page=None):
    t0 = time.time()
    rec = {"bvid": bvid, "ok": False, "path": "", "chars": 0,
           "duration": 0, "cached": False, "seconds": 0.0,
           "error": "", "verify": "", "page": page}
    try:
        res = bt.process_video(bvid, output_dir=out_dir, temp_dir=temp_dir,
                               name_template=name_template, keep_audio=keep_audio,
                               min_chars=min_chars, page=page)
    except bt.ChargingVideoError as e:
        rec["error"] = "充电专属/取不到完整音频：%s" % e
        rec["seconds"] = round(time.time() - t0, 1)
        return rec
    except Exception as e:
        rec["error"] = "%s: %s" % (type(e).__name__, e)
        rec["seconds"] = round(time.time() - t0, 1)
        return rec

    rec.update(ok=True, path=res.get("path", ""), chars=res.get("text_length", 0),
               duration=res.get("duration", 0), cached=bool(res.get("cached")),
               page=res.get("page"))
    rec["part"] = res.get("part", "")
    # 落盘后立刻自检（校验器的 FAIL 才算失败；WARN 只记录）
    if rec["path"] and os.path.isfile(rec["path"]):
        issues, info = vt.check_file(rec["path"], min_chars)
        fails = [i for i in issues if i[0] == "FAIL"]
        warns = [i for i in issues if i[0] == "WARN"]
        rec["verify"] = "FAIL" if fails else ("WARN" if warns else "PASS")
        if fails:
            rec["ok"] = False
            rec["error"] = "校验未通过：" + "; ".join("%s %s" % (r, d) for _, r, d in fails)
        elif warns:
            rec["verify_detail"] = [{"rule": r, "detail": d} for _, r, d in warns]
    rec["seconds"] = round(time.time() - t0, 1)
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="B站视频 → 本地转录 → 落盘 → 校验（只问「转哪些」「存哪里」）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # 关掉前缀缩写：`--bvids` 与 `--bvids-file` 同前缀，argparse 默认的
        # 缩写匹配会把 `--bvids BV1xx` 解析成 `--bvids-file BV1xx`（把 BV 号当文件名）。
        allow_abbrev=False)
    ap.add_argument("bvids", nargs="*", help="BV 号或完整链接（可多个）")
    # 与位置参数同名，故显式指定 dest，避免两者互相覆盖
    ap.add_argument("--bvids", nargs="*", default=None, dest="bvids_opt",
                    help="同上（显式写法）")
    ap.add_argument("--urls", nargs="*", default=None, help="同上（显式写法）")
    ap.add_argument("--from-file", default=None, help="含 BV 号的文本文件（一行一个或整段）")
    ap.add_argument("--out", default=None, help="稿子输出目录（默认 ./transcripts）")
    ap.add_argument("--name-template", default=None,
                    help="文件名模板，默认 {bvid}.md；独夫之心用 dufu-{bvid}.md")
    ap.add_argument("--profile", choices=["dufu"], default=None,
                    help="预设：dufu = 独夫之心语料布局（memory/ + dufu-{bvid}.md + CRLF）")
    ap.add_argument("--cookie", default=None, help="直接给 cookie 串（SESSDATA=...）")
    ap.add_argument("--cookie-file", default=None, help="cookie 文件路径")
    ap.add_argument("--temp-dir", default=None, help="临时音频目录（默认 <out>/.tmp）")
    ap.add_argument("--min-chars", type=int, default=3000,
                    help="正文短于该字数就重转（默认 3000，传 0 关闭缓存判定）")
    ap.add_argument("--keep-audio", action="store_true", help="保留临时音频")
    ap.add_argument("--dry-run", action="store_true", help="只列出将处理的视频")
    ap.add_argument("--p", default=None, dest="page",
                    help="分 P 序号（1 起）；`all` = 全部分 P。多分 P 的讲座/课程必给，"
                         "否则只会转第 1 讲")
    ap.add_argument("--json", default=None, help="把结果写成 JSON 报告")
    ap.add_argument("-y", "--yes", action="store_true", help="不提问，全用默认值")
    args = ap.parse_args(argv)

    print("=" * 68)
    print("B站视频 → 本地转录（SenseVoice / sherpa-onnx，纯 CPU，无需联网识别）")
    print("=" * 68)

    # ---- 预设
    name_template = args.name_template
    out_dir = args.out
    if args.profile == "dufu":
        name_template = name_template or "dufu-{bvid}.md"
        out_dir = out_dir or os.path.join(os.getcwd(), "memory")
        os.environ.setdefault("BILI_NEWLINE", "crlf")

    # ---- 问 1：转哪些视频
    items = list(args.bvids or []) + list(args.bvids_opt or []) + list(args.urls or [])
    if args.from_file:
        items.append(read_list_file(args.from_file))
    bvids = extract_bvids(items)

    if not bvids and not args.yes:
        print()
        print("要转哪些视频？（粘贴 BV 号或完整链接，多个用空格/逗号/换行分隔）")
        raw = ask("视频", allow_empty=True)
        bvids = extract_bvids([raw])
        if not bvids and raw and os.path.isfile(raw.strip()):
            bvids = extract_bvids([read_list_file(raw.strip())])

    if not bvids:
        eprint("没有解析出任何 BV 号。用法示例：")
        eprint('  python transcribe.py --urls "https://www.bilibili.com/video/BV1xx411c7mD"')
        return 2

    # ---- 问 2：存哪里
    if not out_dir:
        out_dir = DEFAULT_OUT
        if not args.yes:
            print()
            out_dir = ask("稿子放哪个目录？", default=DEFAULT_OUT) or DEFAULT_OUT
    out_dir = os.path.abspath(out_dir)
    temp_dir = args.temp_dir or os.path.join(out_dir, ".tmp")
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)

    # ---- 展开分 P：多分 P 视频（讲座/课程）必须按页处理，否则只会转第 1 讲
    jobs = []          # [(bvid, page 或 None)]
    want = (args.page or "").strip()
    if not want:
        jobs = [(b, None) for b in bvids]
    elif want.lower() == "all":
        for b in bvids:
            try:
                _info = bt.get_video_info(b)
                _cid, _dur, _part, _n = bt.select_page(_info, 1)
            except Exception as e:
                eprint("[warn] %s 取不到分 P 列表，跳过：%s" % (b, e))
                continue
            if _n > 1:
                print("[info] %s 共 %d 个分 P，逐页处理" % (b, _n))
            jobs += [(b, i) for i in range(1, _n + 1)]
    else:
        try:
            pg = int(want)
        except ValueError:
            eprint("--p 只接受正整数或 all，得到：%s" % want)
            return 2
        jobs = [(b, pg) for b in bvids]

    if not jobs:
        eprint("没有可处理的视频。")
        return 2

    print()
    print("待转 %d 篇：" % len(jobs))
    for b, pg in jobs:
        print("  - %s%s" % (b, "" if pg is None else "  第 %d 个分 P" % pg))
    print("输出目录：%s" % out_dir)
    print("文件名模板：%s" % (name_template or "{bvid}.md"))
    if any(pg is not None for _, pg in jobs) and "{p}" not in (name_template or ""):
        print("注意：多分 P 的视频会自动在文件名里补 `-p01` 这类页码，否则各讲会互相覆盖。")

    # ---- 本地 ASR 运行时体检（找不到就明确告知怎么装，不静默降级）
    py = bt._sensevoice_python()
    model = bt._sensevoice_model_dir()
    print()
    if not py:
        eprint("[error] 找不到可用的 ASR 解释器（需要能 import sherpa_onnx 的 python）。")
        eprint("        跑一次 scripts/setup.sh 即可（本机若已有同类技能的 .venv，会自动复用）。")
        return 3
    print("ASR 解释器：%s" % py)
    print("识别模型：%s" % (model or "(未找到！)"))
    if not model:
        eprint("[error] 找不到 SenseVoice 模型目录。用 scripts/fetch_sensevoice_model.py 获取。")
        return 3

    if args.dry_run:
        print()
        print("--dry-run：不下载、不识别。")
        return 0

    # ---- cookie：先匿名，失败再问
    cookie = ensure_cookie(args.cookie, args.cookie_file,
                           interactive=not args.yes, quiet=args.yes)
    asked_for_cookie = False

    # ---- 逐篇处理
    results = []
    print()
    print("-" * 68)
    for i, (b, pg) in enumerate(jobs, 1):
        label = b if pg is None else "%s[p%02d]" % (b, pg)
        print("[%d/%d] %s ..." % (i, len(jobs), label), flush=True)
        rec = transcribe_one(b, out_dir, temp_dir, name_template,
                             args.min_chars, args.keep_audio, page=pg)
        # 匿名失败且像是"需要登录"→ 问一次 cookie 再重试本篇
        if (not rec["ok"] and not asked_for_cookie and not args.yes
                and needs_cookie(rec["error"])):
            eprint("[info] 该视频需要登录：%s" % rec["error"][:120])
            c = prompt_for_cookie()
            asked_for_cookie = True
            if c:
                bt.set_cookie(c)
                rec = transcribe_one(b, out_dir, temp_dir, name_template,
                                     args.min_chars, args.keep_audio, page=pg)
        rec["label"] = label
        results.append(rec)
        if rec["ok"]:
            tag = "缓存命中" if rec["cached"] else "完成"
            # 短稿的 WARN 容易被误解成故障：纯音乐/片头本来就没有人声，
            # SenseVoice 给极短文本是**正确**行为（R33），所以这里点明。
            hint = ""
            if rec.get("verify") == "WARN" and rec["chars"] < 200:
                hint = "  ← 正文极短；若原片是纯音乐/片头则属正常（R33）"
            print("      %s：%s 字 / %ss / %s / 校验 %s%s"
                  % (tag, rec["chars"], rec["duration"], "%ss" % rec["seconds"],
                     rec.get("verify") or "-", hint))
        else:
            print("      失败：%s" % rec["error"][:160])
    print("-" * 68)

    # ---- 汇总
    ok = [r for r in results if r["ok"]]
    cached = [r for r in ok if r["cached"]]
    vpass = [r for r in ok if r.get("verify") == "PASS"]
    vwarn = [r for r in ok if r.get("verify") == "WARN"]
    bad = [r for r in results if not r["ok"]]

    print()
    print("成功 %d / 失败 %d（其中缓存命中 %d）｜校验 PASS %d / WARN %d"
          % (len(ok), len(bad), len(cached), len(vpass), len(vwarn)))
    for r in ok:
        print("  OK   %s  %6d 字  %s" % (r.get("label") or r["bvid"], r["chars"],
                                        os.path.basename(r["path"])))
    for r in bad:
        print("  FAIL %s  %s" % (r.get("label") or r["bvid"], r["error"][:140]))
    print("输出目录：%s" % out_dir)

    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"out_dir": out_dir, "total": len(results),
                       "ok": len(ok), "failed": len(bad),
                       "results": results}, f, ensure_ascii=False, indent=2)
        print("报告 -> %s" % args.json)

    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
