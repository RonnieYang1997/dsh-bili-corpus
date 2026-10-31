# -*- coding: utf-8 -*-
"""smoothing_batch.py — 文意疏通的批量驱动器。

子命令：
  queue    --n 30 --batch 1        取一批待处理 bvid（date 倒序，跳过台账里已完成的）
  snapshot --batch 1               记录本批语料文件的 sha256（**在子代理跑之前**）
  verify   --batch 1               复检本批文件未被改动（有任何改动 rc=1）
  apply    --batch 1               用 apply_smoothing.py --dir 落地（自动带 --drop-bad）
  record   --batch 1               把落地报告写进台账
  status                           台账总览

台账：BASE_DIR/_smoothing_state/ledger.json（语料根之外，R28）
补丁：<STATE>/_patches/batch-<N>/<bvid>.json（子代理唯一允许写的位置）
"""
import sys, os, re, json, argparse, hashlib, subprocess, datetime, shutil
import paths as _cp

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = _cp.CORPUS
MAIN = _cp.MAIN
MIR = _cp.MIRROR
STATE = _cp.STATE
LEDGER = os.path.join(STATE, "ledger.json")
SCRATCH = _cp.PATCHES
APPLIER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apply_smoothing.py")
FM = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n", re.S)
DONE_STATUS = {"done", "empty", "skip", "already"}


def sh(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def load_ledger():
    if os.path.exists(LEDGER):
        return json.load(open(LEDGER, "r", encoding="utf-8"))
    return {"batches": {}, "docs": {}}


def save_ledger(led):
    os.makedirs(STATE, exist_ok=True)
    tmp = LEDGER + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(led, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LEDGER)


def batch_dir(n):
    return _cp.batch_dir(n)


def doc_meta(f):
    """从 frontmatter 取 date/title；取不到就返回 None。"""
    t = open(_cp.doc_by_name(f), "rb").read().decode("utf-8-sig")
    m = FM.match(t)
    if not m:
        return None
    head = m.group(1)
    d = re.search(r'^date:\s*"?([\d-]+)"?', head, re.M)
    ti = re.search(r'^title:\s*"(.*)"', head, re.M)
    return {"date": d.group(1) if d else "0000-00-00",
            "title": (ti.group(1) if ti else "")[:40]}


# ---------------------------------------------------------------- queue
def guard_batch_reuse(a, led):
    """开批前守门（2026-10-23 新增，血案见 NEXT.md「坑 3」）。

    01:00 那班曾用 190 号重开，把 10-21 已 record 的 190 批记录整个覆盖掉。
    两道闸：① 台账里已 record 的批号不许复用；② 补丁目录里的遗留 json 自动隔离。
    """
    key = str(a.batch)
    rec = (led.get("batches") or {}).get(key) or {}
    if rec.get("recorded_at") and not getattr(a, "force", False):
        print("**拒绝**：批 %d 已在台账里 record（%s，%d 篇）。"
              % (a.batch, rec.get("recorded_at"), len(rec.get("queued") or [])))
        print("         换一个批号；确需覆盖请加 --force（会丢历史记录）。")
        return False
    pd = batch_dir(a.batch)
    if os.path.isdir(pd):
        stale = [f for f in os.listdir(pd) if f.endswith(".json")]
        if stale and not getattr(a, "force", False):
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            vault = _cp.stale_dir(a.batch, ts)
            os.makedirs(vault, exist_ok=True)
            for f in stale:
                shutil.move(os.path.join(pd, f), os.path.join(vault, f))
            print("已隔离 %d 个遗留补丁 -> %s" % (len(stale), vault))
    return True


def cmd_queue(a):
    led = load_ledger()
    if not guard_batch_reuse(a, led):
        return 3
    todo = []
    pending = set()
    # 在飞批次（已排队但还没 record）的稿子要跳过：否则会把同一个稿子重复排进下一批。
    # 注意只跳"在飞"的，已 record 批次里那些没拿到补丁的失败稿子仍会被重新排队（这正是想要的重试）。
    inflight = set()
    for k, b in led["batches"].items():
        if not b.get("recorded_at"):
            inflight.update(b.get("queued") or [])
    for f in sorted(_cp.iter_names()):
        if not f.startswith(_cp.DOC_PREFIX) or not f.endswith(".md"):
            continue
        bvid = f[len(_cp.DOC_PREFIX):-3]
        if bvid in inflight:
            continue
        if led["docs"].get(bvid, {}).get("status") in DONE_STATUS and bvid not in (
                a.redo.split(",") if a.redo else []):
            continue
        meta = doc_meta(f)
        if meta is None:
            pending.add(bvid)
            continue
        todo.append({"bvid": bvid, "date": meta["date"], "title": meta["title"]})
    todo.sort(key=lambda x: (x["date"], x["bvid"]), reverse=True)
    print("全库 %d 篇待处理（另有 %d 篇 frontmatter 异常未入队）" % (len(todo), len(pending)))
    if a.date_from:
        todo = [x for x in todo if x["date"] >= a.date_from]
        print("按日期下限 %s 过滤后 %d 篇" % (a.date_from, len(todo)))
    batch = todo[:a.n]
    pd = batch_dir(a.batch)
    os.makedirs(pd, exist_ok=True)
    for x in batch:
        x["path"] = _cp.doc(x["bvid"])
        x["patchfile"] = os.path.join(pd, "%s.json" % x["bvid"])
    qf = os.path.join(STATE, "queue-batch-%d.json" % a.batch)
    os.makedirs(STATE, exist_ok=True)
    with open(qf, "w", encoding="utf-8") as f:
        json.dump(batch, f, ensure_ascii=False, indent=2)
    print("批 %d：%d 篇（%s .. %s）  队列 -> %s"
          % (a.batch, len(batch),
             batch[0]["date"] if batch else "-", batch[-1]["date"] if batch else "-", qf))
    print("补丁目录 -> %s" % pd)
    for x in batch[:8]:
        print("   %s  %s  %s" % (x["bvid"], x["date"], x["title"]))
    if len(batch) > 8:
        print("   ...（其余 %d 篇）" % (len(batch) - 8))
    print()
    print("BVIDS_JSON: " + json.dumps([x["bvid"] for x in batch], ensure_ascii=False))
    print()
    led["batches"].setdefault(str(a.batch), {})["queued"] = [x["bvid"] for x in batch]
    led["batches"][str(a.batch)]["at"] = datetime.datetime.now().isoformat(timespec="seconds")
    save_ledger(led)
    return 0


# ---------------------------------------------------------------- snapshot / verify
def cmd_snapshot(a):
    qf = os.path.join(STATE, "queue-batch-%d.json" % a.batch)
    batch = json.load(open(qf, "r", encoding="utf-8"))
    snap = {}
    for x in batch:
        bvid = x["bvid"]
        pm, pr = _cp.doc(bvid), os.path.join(MIR, "%s%s.md" % (_cp.DOC_PREFIX, bvid))
        snap[bvid] = {"main": sh(pm), "mirror": sh(pr)}
    sf = os.path.join(STATE, "snap-batch-%d.json" % a.batch)
    with open(sf, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    print("快照 %d 篇 -> %s" % (len(snap), sf))
    return 0


def cmd_verify(a):
    sf = os.path.join(STATE, "snap-batch-%d.json" % a.batch)
    snap = json.load(open(sf, "r", encoding="utf-8"))
    changed = []
    for bvid, want in snap.items():
        pm, pr = _cp.doc(bvid), os.path.join(MIR, "%s%s.md" % (_cp.DOC_PREFIX, bvid))
        got = {"main": sh(pm), "mirror": sh(pr)}
        if got != want:
            changed.append((bvid, want, got))
    if changed:
        print("!! 有 %d 篇在子代理阶段被改动（绝不允许）:" % len(changed))
        for bvid, want, got in changed:
            print("   %s  原 主%s/镜%s -> 现 主%s/镜%s"
                  % (bvid, want["main"][:10], want["mirror"][:10], got["main"][:10], got["mirror"][:10]))
        return 1
    print("守卫通过：本批 %d 篇语料在子代理阶段一个字节都没被改动" % len(snap))
    return 0


# ---------------------------------------------------------------- apply / record
def cmd_apply(a):
    pd = batch_dir(a.batch)
    files = [f for f in os.listdir(pd) if f.endswith(".json")]
    if not files:
        print("!! 补丁目录是空的: %s" % pd)
        return 1
    rep = os.path.join(STATE, "report-batch-%d.json" % a.batch)
    cmd = [sys.executable, APPLIER, "--dir", pd, "--drop-bad", "--report", rep]
    if a.dry_run:
        cmd.append("--dry-run")
    snap = os.path.join(STATE, "snap-batch-%d.json" % a.batch)
    if getattr(a, "guard", False):
        if not os.path.exists(snap):
            print("!! --guard 要求先 snapshot --batch %d" % a.batch)
            return 1
        cmd += ["--snap", snap]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    sys.stdout.write(r.stdout)
    if r.returncode:
        sys.stderr.write(r.stderr[-1500:])
    return r.returncode


def cmd_record(a):
    rep = json.load(open(os.path.join(STATE, "report-batch-%d.json" % a.batch), "r", encoding="utf-8"))
    led = load_ledger()
    n_ok = n_skip = 0
    for r in rep["reports"]:
        bvid = r["bvid"]
        if r["ok"]:
            st = "done" if r["patches"] else ("already" if r.get("redundant") else "empty")
            n_ok += 1
        else:
            st = "skip"
            n_skip += 1
        old = led["docs"].get(bvid) or {}
        # 不许降级：同一篇会被反复重过（晚到的补丁、放宽后的闸门都会触发重跑）。
        # 重跑时正文里已是改后文本，补丁全被判"冗余"，patches 会变成 0——
        # 若直接覆盖，已记的 done/补丁数就被抹掉了（批 6/11 曾因此被降级）。
        # 规则：只有当新结果"更好"（补丁更多，或由非 done 升为 done）时才覆盖工作量字段。
        new_p = r["patches"] or 0
        old_p = old.get("patches") or 0
        if old.get("status") == "done" and new_p <= old_p:
            st = "done"
            new_p = old_p
        led["docs"][bvid] = {
            "status": st, "batch": old.get("batch", a.batch) if old.get("status") == "done" and new_p == old_p else a.batch,
            "patches": new_p, "dropped": max(len(r.get("dropped") or []), old.get("dropped") or 0),
            "chars": [r.get("chars_before"), r.get("chars_after")],
            "sha": [r.get("sha_before"), r.get("sha_after")],
            "errors": r["errors"], "at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
    upd = {"applied": n_ok, "skipped": n_skip, "dry_run": rep["dry_run"],
           "total_patches": rep["total_patches"], "total_dropped": rep.get("total_dropped", 0),
           "recorded_at": datetime.datetime.now().isoformat(timespec="seconds")}
    if _cp.CORPUS_ID:
        upd["corpus"] = _cp.CORPUS_ID
    led["batches"][str(a.batch)].update(upd)
    save_ledger(led)
    print("台账已更新：批 %d 记 %d 篇（跳过 %d）" % (a.batch, n_ok, n_skip))
    return 0


def cmd_status(a):
    led = load_ledger()
    docs = led["docs"]
    from collections import Counter
    c = Counter(v["status"] for v in docs.values())
    total = len([f for f in _cp.iter_names() if f.startswith(_cp.DOC_PREFIX) and f.endswith(".md")])
    print("语料总量 %d 篇 | 台账已记 %d 篇" % (total, len(docs)))
    print("  已完成 %d | 实为已修好(already) %d | 无问题 %d | 跳过 %d"
          % (c["done"], c["already"], c["empty"], c["skip"]))
    print("  剩余 %d 篇" % (total - sum(c[s] for s in DONE_STATUS)))
    print("  累计补丁 %d 条，丢弃 %d 条"
          % (sum(v["patches"] for v in docs.values()), sum(v["dropped"] for v in docs.values())))
    if led["batches"]:
        print("  批次:")
        for k in sorted(led["batches"], key=int):
            b = led["batches"][k]
            print("    批 %-3s 排队 %-4s 落地 %-4s 跳过 %-3s 补丁 %-5s %s"
                  % (k, len(b.get("queued", [])), b.get("applied", "-"), b.get("skipped", "-"),
                     b.get("total_patches", "-"), b.get("recorded_at", b.get("at", ""))))
    return 0


def main():
    ap_ = argparse.ArgumentParser()
    sub = ap_.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("queue"); p.add_argument("--n", type=int, default=30)
    p.add_argument("--batch", type=int, required=True)
    p.add_argument("--date-from"); p.add_argument("--redo")
    p.add_argument("--force", action="store_true", help="允许复用已 record 的批号（会覆盖历史记录）")
    p.set_defaults(func=cmd_queue)
    for name, fn in (("snapshot", cmd_snapshot), ("verify", cmd_verify), ("apply", cmd_apply),
                     ("record", cmd_record)):
        pp = sub.add_parser(name); pp.add_argument("--batch", type=int, required=True)
        if name == "apply":
            pp.add_argument("--dry-run", action="store_true")
            pp.add_argument("--guard", action="store_true",
                            help="落地时逐篇核对批次快照 sha，不一致就拒绝写入该篇")
        pp.set_defaults(func=fn)
    ps = sub.add_parser("status"); ps.set_defaults(func=cmd_status)
    a = ap_.parse_args()
    sys.exit(a.func(a))


if __name__ == "__main__":
    main()