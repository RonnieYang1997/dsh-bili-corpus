# -*- coding: utf-8 -*-
"""punct_apply.py --batch N [--dry-run]
标点恢复的落地闸门：**只允许插入标点，其余字符逐字符不变**。

逐篇校验（任一不满足就拒绝写入该篇）：
  1) 两库当前逐字节一致
  2) 代理产出文件存在
  3) 去掉"标点+空白"后，产出与原文**完全相同**（非标点非空白字符一个都不能增删改）
  4) 未引入省略号 … / ...（铁律：不许出现省略号）
  5) 无 BOM、无 <| 残渣、无 U+FFFD
  6) 不新增裸 LF（写入统一 CRLF）
  7) 断句确实变好：最长无标点连读 <= 400 字，且比原来至少缩短一半
  8) 长度增幅合理（≤60%）
写入：原件归档 _asr_raw（若缺）→ 两库 tmp+replace → 校验两库 sha 一致 → 记台账 ledger["punct"]
"""
import sys, os, re, json, hashlib, shutil, argparse, datetime
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import paths as _cp
MAIN = _cp.MAIN
MIR = _cp.MIRROR
STATE = _cp.STATE
ARCH = _cp.ASR_RAW
LED = os.path.join(STATE, "ledger.json")
# 2026-10-12 修：与 punct_queue.py / punct_local.py 同步——吃掉开头**所有**连续 frontmatter 块
FM = re.compile(r"^(?:\ufeff?---\r?\n.*?\r?\n---\r?\n[ \t]*\r?\n?)+", re.S)
# 标点判定改用 Unicode 类别（所有 P* 类字符都算标点）：
# 之前用固定字符集，漏了中文引号 “”（U+201C/201D），把代理合法插入的引号误判成"改动了非标点字符"，
# 导致整批被拒。教训：判"是不是标点"必须用字符类别，不能靠枚举。
import unicodedata
def is_punct(c):
    return unicodedata.category(c).startswith("P")

def sha(b):
    return hashlib.sha256(b).hexdigest()[:12]

def strip_ps(t):
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

ap = argparse.ArgumentParser()
ap.add_argument("--batch", type=int, required=True)
ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--force", action="store_true",
                help="重落模式：跳过\"断句变好/改善不足\"两项（重写同一篇时会失败），内容类闸门全部保留")
a = ap.parse_args()
batch = json.load(open(os.path.join(STATE, "punct-queue-batch-%d.json" % a.batch), encoding="utf-8"))
led = json.load(open(LED, encoding="utf-8"))
led.setdefault("punct", {})
n_ok = n_bad = 0
report = []
for x in batch:
    b = x["bvid"]
    n = "%s%s.md" % (_cp.DOC_PREFIX, b)
    if not a.force and led["punct"].get(b, {}).get("status") == "done":
        print("  ==   %s 已落地过（跳过）" % b)
        continue
    pm, pr = _cp.doc_by_name(n), os.path.join(MIR, n)
    res = {"bvid": b, "ok": False, "errors": []}
    raw_m, raw_r = open(pm, "rb").read(), open(pr, "rb").read()
    if raw_m != raw_r:
        res["errors"].append("两库不一致"); n_bad += 1; report.append(res); print("  BAD  %s 两库不一致" % b); continue
    # 取件：**永远优先用 punct_assemble.py 拼装并验证过覆盖全文的 assembled.txt**
    # （踩过的坑：代理会分块写 part01..NN；若直接用 part01.txt，就只落地了第一块）
    alt = os.path.join(x.get("dir") or os.path.dirname(x["outfile"]), "assembled.txt")
    if os.path.exists(alt):
        x["outfile"] = alt
    elif os.path.isdir(x.get("dir") or "") and any(
            f.startswith("in-part") for f in os.listdir(x["dir"])):
        # 分块作业还没齐：此时 part01.txt 只是第一块，拿它比全文必然"差异"——直接报准原因
        res["errors"].append("分块未齐（缺 assembled.txt）")
        n_bad += 1; report.append(res); print("  ~~   %s 分块未齐（%d 块已出）"
            % (b, len([f for f in os.listdir(x["dir"]) if f.startswith("part") and f.endswith(".txt")])))
        continue
    if not os.path.exists(x["outfile"]):
        res["errors"].append("代理产出文件缺失"); n_bad += 1; report.append(res); print("  --   %s 无产出" % b); continue
    t = raw_m.decode("utf-8-sig")
    m = FM.match(t)
    if not m:
        res["errors"].append("frontmatter 解析失败"); n_bad += 1; report.append(res); print("  BAD  %s fm" % b); continue
    head, body = t[:m.end()], t[m.end():]
    out = open(x["outfile"], "rb").read().decode("utf-8-sig")
    if out.startswith("\ufeff"):
        out = out[1:]
    # 换行策略：**一个字节都不新增**。沿用原文正文自己的风格与结尾状态。
    # （踩过的坑：有些稿 frontmatter 是裸 LF、正文没有任何换行；无条件补一个 CRLF 会让文件
    #   变成 CRLF/LF 混用，被 corpus_lint 判为新缺陷。）
    body_cmp = body.rstrip("\r\n")
    trailing = "\r\n" if body.endswith("\r\n") else ("\n" if body.endswith("\n") else "")
    flat = out.replace("\r\n", "\n").replace("\r", "\n")
    if "\r\n" in body:
        out = flat.replace("\n", "\r\n")
    elif "\n" in body:
        out = flat
    else:
        out = flat.replace("\n", "")          # 原文正文没有换行，产出也不该有
    out = out.rstrip("\r\n")
    if strip_ps(out) != strip_ps(body_cmp):
        d = strip_ps(out); e = strip_ps(body_cmp)
        i = next((k for k in range(min(len(d), len(e))) if d[k] != e[k]), min(len(d), len(e)))
        res["errors"].append("非标点字符被改动，首个差异在第 %d 字符: 原 %r / 新 %r"
                             % (i, e[max(0, i - 15):i + 15], d[max(0, i - 15):i + 15]))
    for mark in ("…", "..."):
        if out.count(mark) > body_cmp.count(mark):
            res["errors"].append("新增省略号 %r" % mark)
    if "<|" in out or "\ufffd" in out:
        res["errors"].append("残渣")
    if out[:3] == "\ufeff":
        res["errors"].append("BOM")
    mr_o, mr_b = maxrun(out), maxrun(body_cmp)
    if not a.force:
        if mr_o > 400:
            res["errors"].append("断句仍不足：最长连读 %d 字" % mr_o)
        if mr_b and mr_o > mr_b * 0.5:
            res["errors"].append("改善不足：连读 %d -> %d 字" % (mr_b, mr_o))
    if len(out) > len(body_cmp) * 1.6:
        res["errors"].append("长度增幅异常 %d -> %d" % (len(body_cmp), len(out)))
    if res["errors"]:
        n_bad += 1; report.append(res)
        print("  BAD  %s %s" % (b, res["errors"][0][:90]))
        continue
    new = (head + out + trailing).encode("utf-8")
    if not a.dry_run:
        ap_ = os.path.join(ARCH, n)
        if not os.path.exists(ap_):
            shutil.copy2(pm, ap_)
        for p in (pm, pr):
            tmp = p + ".punct-tmp"
            with open(tmp, "wb") as fh:
                fh.write(new)
            os.replace(tmp, p)
        if open(pm, "rb").read() != open(pr, "rb").read():
            res["errors"].append("写入后两库不一致"); n_bad += 1; report.append(res)
            print("  BAD  %s 写入后不一致" % b); continue
    res.update(ok=True, chars_before=len(body_cmp), chars_after=len(out),
               maxrun_before=mr_b, maxrun_after=mr_o,
               sha_before=sha(raw_m), sha_after=sha(new))
    n_ok += 1
    report.append(res)
    if not a.dry_run:
        led["punct"][b] = {"status": "done", "batch": a.batch, "chars": [len(body_cmp), len(out)],
                           "maxrun": [mr_b, mr_o], "sha": [sha(raw_m), sha(new)],
                           "at": datetime.datetime.now().isoformat(timespec="seconds")}
    print("  OK   %s 连读 %5d -> %-4d 字  长度 %5d -> %-5d  %s%s"
          % (b, mr_b, mr_o, len(body_cmp), len(out), sha(raw_m), " [dry]" if a.dry_run else ""))
if not a.dry_run:
    json.dump(led, open(LED, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
json.dump(report, open(os.path.join(STATE, "report-punct-%d.json" % a.batch), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print("小结: 成功 %d 篇，拒绝 %d 篇%s" % (n_ok, n_bad, "（dry）" if a.dry_run else ""))