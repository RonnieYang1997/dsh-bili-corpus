# -*- coding: utf-8 -*-
"""punct_local.py —— 用**本地**标点模型（sherpa-onnx CT-Transformer）给"整片无标点"的长稿加标点。
0 Token：不调用任何 LLM。

契约与 punct_apply.py 完全一致（它是最终闸门，本脚本先自检一遍）：
  · 只许插入标点：去掉「标点(P*) + 空白」后，产出与原文逐字符相同
  · 不新增换行（模型若吐换行，先剔除再校验）
  · 不新增省略号 … / ...
  · 断句确实变好：最长连读 <= 400 字，且比原来至少缩短一半
产出：`_punct/<bvid>/assembled.txt`（punct_apply.py 优先取这个文件）+ 审计用 `local-model.txt`

用法：
  punct_local.py --batch 3 --only BV1yWbzz3Eme --sample 260 --dry-run   # 单篇质量抽检
  punct_local.py --batch 4                                             # 整波产出
"""
import sys, os, re, json, time, argparse, unicodedata
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import paths as _cp

MAIN = _cp.LANDING
STATE = _cp.STATE
PUNCT_DIR = r"C:\Users\Ronnie\Documents\deepseek-harness\default-workspace\_scratch\_punct"
MODEL = os.path.expanduser(r"~\.dsh\speech-to-text\sensevoice\models\punct-ct-transformer\model.onnx")
# 2026-10-12 修：与 punct_queue.py / punct_apply.py 同步——吃掉开头**所有**连续 frontmatter 块（双 frontmatter 畸形稿）
FM = re.compile(r"^(?:\ufeff?---\r?\n.*?\r?\n---\r?\n[ \t]*\r?\n?)+", re.S)


def is_punct(c):
    return unicodedata.category(c).startswith("P")


def strip_ps(t):
    """与 punct_apply.py 一字不差的判据。"""
    return "".join(c for c in t if not is_punct(c) and not c.isspace())


def maxrun(b):
    run = mx = 0
    for ch in b:
        if is_punct(ch) or ch in "\r\n":
            run = 0
        else:
            run += 1
            mx = max(mx, run)
    return mx


def make_punc(threads=4):
    import sherpa_onnx
    cfg = sherpa_onnx.OfflinePunctuationConfig(
        model=sherpa_onnx.OfflinePunctuationModelConfig(
            ct_transformer=MODEL, num_threads=threads, provider="cpu", debug=False))
    return sherpa_onnx.OfflinePunctuation(cfg)


def seg_text(s, size):
    """把整片文本切成 <=size 的段：优先在空白处断开，其次硬切。"""
    if len(s) <= size:
        return [s]
    out, cur = [], 0
    while cur < len(s):
        end = min(cur + size, len(s))
        if end < len(s):
            lo = cur + int(size * 0.6)
            cut = -1
            for k in range(end - 1, lo - 1, -1):
                if s[k].isspace():
                    cut = k + 1
                    break
            if cut > cur:
                end = cut
        out.append(s[cur:end])
        cur = end
    return out


def punctuate(punc, text, size, min_size=40):
    """返回 (加标点后的文本, 统计)。逐段跑模型；不合格的段回退原样。"""
    st = {"seg": 0, "rejected": 0, "fallback": 0, "secs": 0.0}
    pieces = seg_text(text, size)
    st["seg"] = len(pieces)
    outs = []
    for p in pieces:
        if not p.strip() or len(strip_ps(p)) < 3:
            outs.append(p)
            continue
        t0 = time.time()
        try:
            o = punc.add_punctuation(p)
        except Exception:
            o = None
        st["secs"] += time.time() - t0
        ok = False
        if o:
            o = o.replace("\r", "").replace("\n", "")      # 不许新增换行
            o = o.strip()
            if strip_ps(o) == strip_ps(p) and o.count("…") <= p.count("…"):
                ok = True
        if ok:
            outs.append(o)
        else:
            if len(p) > min_size * 2:            # 段太长导致改写字符：折半重试一次
                mid = len(p) // 2
                got, good = [], True
                for half in (p[:mid], p[mid:]):
                    try:
                        h = punc.add_punctuation(half).replace("\r", "").replace("\n", "").strip()
                    except Exception:
                        good = False; break
                    if strip_ps(h) != strip_ps(half):
                        good = False; break
                    got.append(h)
                if good:
                    st["fallback"] += 1
                    outs.append("".join(got))
                    continue
            st["rejected"] += 1
            outs.append(p)
    return "".join(outs), st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, required=True)
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 篇（试点用）")
    ap.add_argument("--only", default="", help="只处理 bvid 含该子串的篇（单篇调试）")
    ap.add_argument("--sample", type=int, default=0, help="打印中段原文/产出对照的字数（质量抽检）")
    ap.add_argument("--seg", type=int, default=800, help="单段最大字数（默认 800）")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(MODEL):
        print("!! 标点模型缺失: %s" % MODEL); return 2
    q = json.load(open(os.path.join(STATE, "punct-queue-batch-%d.json" % a.batch), encoding="utf-8"))
    led = json.load(open(os.path.join(STATE, "ledger.json"), encoding="utf-8"))
    done = {k for k, v in (led.get("punct") or {}).items() if v.get("status") == "done"}

    print("加载本地标点模型 %s" % os.path.basename(MODEL))
    t0 = time.time()
    punc = make_punc(a.threads)
    print("  就绪 %.1fs" % (time.time() - t0))

    todo = [x for x in q if x["bvid"] not in done]
    if a.only:
        todo = [x for x in todo if a.only in x["bvid"]]
    if a.limit:
        todo = todo[:a.limit]
    print("待处理 %d 篇（队列 %d，已落地 %d）" % (len(todo), len(q), len([x for x in q if x["bvid"] in done])))

    report, tot_seg, tot_rej, tot_fb = [], 0, 0, 0
    t_all = time.time()
    for x in todo:
        b = x["bvid"]
        p = _cp.doc_path(b, MAIN)
        raw = open(p, "rb").read()
        t = raw.decode("utf-8-sig")
        m = FM.match(t)
        if not m:
            print("  BAD  %s frontmatter 解析失败" % b); continue
        body = t[m.end():]
        body_cmp = body.rstrip("\r\n")
        mr_b = maxrun(body_cmp)
        out, st = punctuate(punc, body_cmp, a.seg)
        mr_o = maxrun(out)
        tot_seg += st["seg"]; tot_rej += st["rejected"]; tot_fb += st["fallback"]
        growth = len(out) / max(1, len(body_cmp))
        ok = (strip_ps(out) == strip_ps(body_cmp) and mr_o <= 400
              and (mr_b == 0 or mr_o <= mr_b * 0.5) and growth <= 1.6
              and "…" not in out and "<|" not in out and "\ufffd" not in out
              and "\n" not in out and "\r" not in out)
        if ok and not a.dry_run:
            d = os.path.join(PUNCT_DIR, b)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "assembled.txt"), "wb") as f:
                f.write(out.encode("utf-8"))
            with open(os.path.join(d, "local-model.txt"), "wb") as f:
                f.write(out.encode("utf-8"))
        report.append({"bvid": b, "ok": ok, "maxrun": [mr_b, mr_o], "chars": [len(body_cmp), len(out)],
                       "seg": st["seg"], "rejected": st["rejected"], "fallback": st["fallback"],
                       "secs": round(st["secs"], 1)})
        print("  %-4s %s 连读 %5d -> %-4d  长度 %5d -> %-5d  段 %2d(退%2d)  %.1fs%s"
              % ("OK" if ok else "BAD", b, mr_b, mr_o, len(body_cmp), len(out),
                 st["seg"], st["rejected"], st["secs"], " [dry]" if a.dry_run else ""))
        if a.sample:
            i = len(body_cmp) // 2
            probe = body_cmp[i:i + 36]
            j = out.find(probe) if probe else -1
            print("     原文  : %s" % body_cmp[i:i + a.sample].replace("\r", "").replace("\n", " / "))
            print("     加标点: %s" % (out[j:j + a.sample + 40] if j >= 0 else out[i:i + a.sample]).replace("\r", "").replace("\n", " / "))

    json.dump(report, open(os.path.join(STATE, "report-punct-local-%d.json" % a.batch), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    okn = len([r for r in report if r["ok"]])
    print("小结: 合格 %d / %d 篇；段 %d（退回 %d，折半救回 %d）；总耗时 %.1fs%s"
          % (okn, len(report), tot_seg, tot_rej, tot_fb, time.time() - t_all, "（dry）" if a.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
