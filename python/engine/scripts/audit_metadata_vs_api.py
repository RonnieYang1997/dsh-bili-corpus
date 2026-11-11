#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_metadata_vs_api.py -- speaker: 独夫之心 -- READ-ONLY

Answers one question the offline guard (corpus_lint.py) cannot:
    does each file's frontmatter still describe the video that its bvid
    actually is, according to Bilibili?

Why this exists
---------------
R24 (2026-09-27): a 5-file cluster proved that a frontmatter can be perfectly
well-formed, pass every offline rule, and still be *about a different video*.
The file keeps whatever title/duration the collector saw on the day it ran.
The uploader can afterwards rename a video or 换源 (swap the source file), which
changes title and/or duration under the same bvid.  Nothing offline can see that.

Observed 2026-09-27 on the 2025.9.29日报 four-part series: three of the four
episodes no longer matched their bvid (one had been renamed to a 问答环节 video
and flipped to 充电专属).  Meanwhile a 45-file random sample showed ~0 systemic
mis-pairing -- so this is upstream drift, not a collector bug.  Drift is normal
and benign; the point is to *know* about it instead of discovering it by accident.

This tool is therefore an on-demand auditor, NOT a gate: it needs the network and
Bilibili rate-limits aggressively (code -352 风控校验失败).  Run it now and then,
or after a bulk import, never in a tight loop.

Usage
-----
    python audit_metadata_vs_api.py                 # random sample of 40
    python audit_metadata_vs_api.py --n 120 --seed 7
    python audit_metadata_vs_api.py --bv BV1hFnmznExR BV1zAnqzBEWB
    python audit_metadata_vs_api.py --all --sleep 2 --json drift.json

Exit codes
    0 = no drift found        1 = drift found        2 = usage / network error
"""

import argparse
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# R3/R16: a CP936 console must not die while reporting Chinese findings.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import corpus_lint as L            # noqa: E402
import bilibili_transcribe as BT   # noqa: E402

VIEW_API = "https://api.bilibili.com/x/web-interface/view"
RE_FM_KEY = L.RE_FM_KEY
RATE_LIMIT_CODE = -352             # 风控校验失败


def fm(text, key):
    m = RE_FM_KEY(key).search(text)
    return (m.group(1) or "").strip() if m else ""


def norm_duration(v):
    """'1078' / '1078s' / 1078 -> '1078';  '' / None -> ''  (three generations
    of frontmatter spell duration differently; compare the digits only)."""
    if v is None:
        return ""
    return re.sub(r"\D", "", str(v))


def is_selected(name, text):
    """Skip records that cannot be judged by the API (see the note in the
    docstring of corpus_lint.py): the early-import batch has real-looking bvids
    that no longer resolve, and the API-error stubs were never transcripts."""
    if not fm(text, "bvid"):
        return False
    if fm(text, "date") in L.PLACEHOLDER_DATES:
        return False
    if any(mk in text for mk in L.DEAD_MARKS):
        return False
    return True


def candidates(root):
    rows = []
    for p in sorted(root.glob("dufu-BV*.md")):
        text = p.read_text(encoding="utf-8", errors="replace")
        if is_selected(p.name, text):
            rows.append({
                "file": p.name,
                "bvid": fm(text, "bvid"),
                "title": fm(text, "title"),
                "duration": fm(text, "duration"),
                "date": fm(text, "date"),
            })
    return rows


def view(bvid):
    req = urllib.request.Request(
        VIEW_API + "?bvid=" + bvid,
        headers={"User-Agent": "Mozilla/5.0",
                 "Referer": "https://www.bilibili.com/",
                 "Cookie": BT.COOKIE})
    with urllib.request.urlopen(req, timeout=30, context=BT.SSL_CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=40, help="sample size (default 40)")
    ap.add_argument("--all", action="store_true", help="every eligible file (slow)")
    ap.add_argument("--bv", nargs="*", help="only these bvids")
    ap.add_argument("--seed", type=int, default=20260927)
    ap.add_argument("--sleep", type=float, default=1.6,
                    help="seconds between API calls (default 1.6)")
    ap.add_argument("--show-ok", action="store_true")
    ap.add_argument("--json", help="also write the findings to this file")
    a = ap.parse_args()

    root = L.assert_corpus_root(L.DEFAULT_ROOTS[0])
    pool = candidates(root)
    rows = pool
    if a.bv:
        want = set(a.bv)
        rows = [r for r in pool if r["bvid"] in want]
        missing = want - {r["bvid"] for r in rows}
        if missing:
            print("note: not eligible / not indexed: %s" % sorted(missing))
    elif not a.all:
        random.Random(a.seed).shuffle(rows)
        rows = rows[:a.n]

    print("root            : %s" % root)
    print("eligible files  : %d" % len(pool))
    print("checking        : %d  (sleep %.1fs, ~%.0f s)"
          % (len(rows), a.sleep, len(rows) * a.sleep))
    print()

    drift, gone, rate_limited = [], [], 0
    n_title_ok = n_dur_ok = n_dur_absent = n_exclusive = 0
    for i, row in enumerate(rows, 1):
        try:
            j = view(row["bvid"])
        except (urllib.error.URLError, OSError) as e:
            print("  [%3d] %-16s NETWORK %s" % (i, row["bvid"], e))
            continue
        if j.get("code") == RATE_LIMIT_CODE:
            rate_limited += 1
            print("  [%3d] %-16s 风控 -352 -- stopping early" % (i, row["bvid"]))
            break
        d = j.get("data") or {}
        if not d:
            gone.append(dict(row, api_code=j.get("code"), api_message=j.get("message")))
            print("  [%3d] %-16s GONE code=%s %s"
                  % (i, row["bvid"], j.get("code"), j.get("message")))
            time.sleep(a.sleep)
            continue

        api_title, api_dur = d.get("title") or "", norm_duration(d.get("duration"))
        file_dur = norm_duration(row["duration"])
        title_ok = (row["title"] == api_title)
        dur_state = "absent" if not file_dur else ("ok" if file_dur == api_dur else "drift")
        n_title_ok += title_ok
        n_dur_ok += (dur_state == "ok")
        n_dur_absent += (dur_state == "absent")
        if d.get("is_upower_exclusive"):
            n_exclusive += 1

        if title_ok and dur_state != "drift":
            if a.show_ok:
                print("  [%3d] %-16s ok%s" % (i, row["bvid"],
                                              "  (no duration in file)" if dur_state == "absent" else ""))
        else:
            drift.append({"file": row["file"], "bvid": row["bvid"],
                          "file_title": row["title"], "api_title": api_title,
                          "file_duration": row["duration"], "api_duration": api_dur,
                          "date": row["date"],
                          "upower_exclusive": bool(d.get("is_upower_exclusive"))})
            print("  [%3d] %-16s DRIFT" % (i, row["bvid"]))
            if not title_ok:
                print("        file title: %s" % row["title"])
                print("        api  title: %s" % api_title)
            if dur_state == "drift":
                print("        file dur  : %s   api dur: %s" % (row["duration"], api_dur))
        sys.stdout.flush()
        time.sleep(a.sleep)

    checked = n_title_ok + len(drift)
    print()
    print("=" * 74)
    print("checked=%d  title_ok=%d  duration_ok=%d  duration_absent=%d  drift=%d  gone=%d  风控=%d"
          % (checked, n_title_ok, n_dur_ok, n_dur_absent, len(drift), len(gone), rate_limited))
    print("充电专属 among checked: %d" % n_exclusive)
    if drift:
        print("\nDRIFTED records (frontmatter no longer matches bilibili; the\n"
              "uploader renamed or 换源'd the video after it was collected --\n"
              "this is upstream change, not a collector bug, but the file's\n"
              "title/duration are now stale):")
        for r in drift:
            print("  %s  %s" % (r["file"], r["bvid"]))
            print("      file: %s | %s s" % (r["file_title"], norm_duration(r["file_duration"]) or "-"))
            print("      api : %s | %s s%s" % (r["api_title"], r["api_duration"],
                                               "  [充电专属]" if r["upower_exclusive"] else ""))
    if gone:
        print("\nGONE (bvid no longer resolves):")
        for r in gone:
            print("  %s  %s  code=%s" % (r["file"], r["bvid"], r["api_code"]))

    if a.json:
        Path(a.json).write_text(json.dumps(
            {"checked": checked, "drift": drift, "gone": gone,
             "rate_limited": rate_limited, "generated_by": "audit_metadata_vs_api.py"},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print("\nwrote %s" % a.json)

    if drift or gone:
        return 1
    return 2 if rate_limited and not checked else 0


if __name__ == "__main__":
    sys.exit(main())