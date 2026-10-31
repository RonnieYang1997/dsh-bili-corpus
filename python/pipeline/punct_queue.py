# -*- coding: utf-8 -*-
"""punct_queue.py --n N --batch N [--min-run 2000] [--all]
挑选"最长无标点连读"超阈值的稿子（最差优先），写队列 + 快照（守卫用）。

台账：ledger["punct"][bvid] = {status, ...}，状态 done 的不再重复派单。
"""
import sys, os, re, json, hashlib, argparse, datetime
import paths as _cp
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
MAIN = _cp.MAIN
MIR = _cp.MIRROR
STATE = _cp.STATE
LED = os.path.join(STATE, "ledger.json")
# 2026-10-12 修：原正则只吃**第一个** frontmatter 块。畸形稿（第一个块只有 type/warning，
# 紧跟一个 \ufeff--- 的第二块才是 title/bvid/date）的第二个块会被当成"正文" → maxrun 被
# YAML 抬高、模型去给 YAML 加标点 → punct_apply 的 strip_ps 校验因 \ufeff 不一致而整篇拒。
# 现在吃掉开头**所有**连续 frontmatter 块（块间允许空行与 BOM）。
FM = re.compile(r"^(?:\ufeff?---\r?\n.*?\r?\n---\r?\n[ \t]*\r?\n?)+", re.S)
PUNCT = "，。？！、：；—…「」《》（）,.?!:;-\u2014"
ap = argparse.ArgumentParser()
ap.add_argument("--n", type=int, default=8)
ap.add_argument("--batch", type=int, required=True)
ap.add_argument("--min-run", type=int, default=2000)
ap.add_argument("--all", action="store_true", help="连已完成标点恢复的也重扫")
ap.add_argument("--max-chars", type=int, default=0, help="只看正文不超过这么多字的稿子（试点用）")
ap.add_argument("--exclude-batch", type=int, action="append", default=[],
                help="跳过某批次已排队的稿子（可重复传：--exclude-batch 3 --exclude-batch 4）")
a = ap.parse_args()
led = json.load(open(LED, encoding="utf-8"))
led.setdefault("punct", {})

def body_of(p):
    t = open(p, "rb").read().decode("utf-8-sig")
    m = FM.match(t)
    return t, (m.end() if m else 0)

def maxrun(b):
    run = mx = 0
    for ch in b:
        if ch in PUNCT or ch in "\r\n":
            run = 0
        else:
            run += 1
            mx = max(mx, run)
    return mx

todo = []
skip = set()
for eb in a.exclude_batch:
    ef = os.path.join(STATE, "punct-queue-batch-%d.json" % eb)
    if os.path.exists(ef):
        got = {x["bvid"] for x in json.load(open(ef, encoding="utf-8"))}
        skip |= got
        print("排除批次 %d 的 %d 篇" % (eb, len(got)))
for f in sorted(_cp.iter_names()):
    if not (f.startswith(_cp.DOC_PREFIX) and f.endswith(".md")):
        continue
    b = f[5:-3]
    if b in skip:
        continue
    if not a.all and led["punct"].get(b, {}).get("status") == "done":
        continue
    t, off = body_of(_cp.doc_by_name(f))
    mr = maxrun(t[off:])
    if mr > a.min_run and (not a.max_chars or len(t) - off <= a.max_chars):
        todo.append({"bvid": b, "maxrun": mr, "chars": len(t) - off})
todo.sort(key=lambda x: -x["maxrun"])
batch = todo[:a.n]
qd = os.path.join(STATE, "punct-queue-batch-%d.json" % a.batch)
ROOT = r"C:\Users\Ronnie\Documents\deepseek-harness\default-workspace\_scratch\_punct"
os.makedirs(ROOT, exist_ok=True)
for x in batch:
    # 每篇一个**私有目录**：代理只在这里写，避免它们互相"清理"对方产出（已踩坑）
    d = os.path.join(ROOT, x["bvid"])
    os.makedirs(d, exist_ok=True)
    x["dir"] = d
    x["outfile"] = os.path.join(d, "part01.txt")
    x["path"] = _cp.doc(x["bvid"])
json.dump(batch, open(qd, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
snap = {}
for x in batch:
    n = "%s%s.md" % (_cp.DOC_PREFIX, x["bvid"])
    snap[x["bvid"]] = {"main": hashlib.sha256(open(_cp.doc_by_name(n), "rb").read()).hexdigest(),
                       "mirror": hashlib.sha256(open(os.path.join(MIR, n), "rb").read()).hexdigest()}
sf = os.path.join(STATE, "snap-punct-%d.json" % a.batch)
json.dump(snap, open(sf, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("标点恢复批次 %d：全库超阈值 %d 篇，本批 %d 篇（最长连读 %d .. %d 字）"
      % (a.batch, len(todo), len(batch), batch[0]["maxrun"] if batch else 0, batch[-1]["maxrun"] if batch else 0))
print("队列 -> %s\n快照 -> %s" % (qd, sf))
for x in batch:
    print("   %s  连读 %5d 字  %5d 字  -> %s" % (x["bvid"], x["maxrun"], x["chars"], x["dir"]))
print("\nPUNCT_JOBS_JSON: " + json.dumps([{"bvid": x["bvid"], "path": x["path"], "dir": x["dir"],
                                          "maxrun": x["maxrun"]} for x in batch], ensure_ascii=False))