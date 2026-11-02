# -*- coding: utf-8 -*-
"""apply_smoothing.py — 把「文意疏通」补丁安全地落到语料上（R23：契约在写入路径上）。

设计原则（为什么是"补丁"而不是"重写正文"）
  1. 只做**最小必要替换**：模型给出 find/replace 对，本脚本逐条校验后应用；
     任何一条对不上就整篇放弃（不写盘），绝不半途落盘。
  2. 事实不可造：正文里的阿拉伯数字默认**必须原样保留**，除非该条补丁
     显式声明 kind=number（会在报告里单列出来供人工复核）。
  3. frontmatter 一个字节都不许动（R17）；CRLF/BOM 风格必须保持（R26）。
  4. 原始 ASR 正文**先归档到语料根之外**（R28），保留可回滚的转写稿原件。
  5. 两库必须同时写、逐字节一致（R18）。

用法：
  python apply_smoothing.py patches.json             # 应用
  python apply_smoothing.py patches.json --dry-run    # 只预览，不写盘
  python apply_smoothing.py patches.json --report r.json

patches.json 结构：
{
  "BV1xxxxxxxxx": {
    "patches": [
      {"find": "尊介", "replace": "尊界", "kind": "name", "why": "车型名，搜索确认"},
      {"find": "核光中", "replace": "核光钟", "kind": "name", "why": "术语"}
    ],
    "note": "可选备注"
  }
}
kind 取值：homophone | name | punct | stutter | number | other
"""
import sys, os, re, json, hashlib, shutil, argparse, datetime, difflib
import paths as _cp

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = _cp.CORPUS
MAIN = _cp.MAIN
MIRROR = _cp.MIRROR
ARCHIVE = _cp.ASR_RAW           # 语料根之外（R28）
FM_RE = re.compile(r"^---\r?\n.*?\r?\n---\r?\n", re.S)
KINDS = {"homophone", "name", "punct", "stutter", "number", "other"}
# 正文里出现这些行首表头就是 R1/R3 的违背（中文标签不许进正文）
LABEL_RE = re.compile(r"^(标题|日期|时长|BVID|简介|正文|来源)[:：]")


def sha(b):
    return hashlib.sha256(b).hexdigest()


def split_doc(raw):
    t = raw.decode("utf-8")
    m = FM_RE.match(t)
    if not m:
        return None, None, None
    return t[:m.end()], t[m.end():], ("\r\n" if raw.count(b"\r\n") else "\n")


def numbers(text):
    """所有阿拉伯数字串（含小数/百分号），用于"事实不许变"的粗筛。"""
    return sorted(re.findall(r"\d+(?:\.\d+)?", text))


def letters(text):
    """ASCII 字母多重集。用于区分"术语级改写"与"纯数字替换"。"""
    return sorted(c for c in text if c.isalpha() and ord(c) < 128)


def alnum_token(text):
    """是否含"字母数字紧邻"的词（Q10 / C4 / 8V / IC1）——ASR 把术语听成数字字形的典型特征。"""
    return bool(re.search(r"[A-Za-z]\d|\d[A-Za-z]", text))


def apply_one(bvid, spec, dry_run, drop_bad=False, snap=None, allow_name_digits=False,
              allow_name_digits_strong=False):
    name = _cp.doc_name(bvid)
    pm, pr = _cp.doc_by_name(name), os.path.join(MIRROR, name)
    res = {"bvid": bvid, "ok": False, "patches": 0, "applied": [], "dropped": [],
           "redundant": [], "warnings": [], "errors": []}
    if not os.path.exists(pm):
        res["errors"].append("主库找不到文件")
        return res
    if not os.path.exists(pr):
        res["errors"].append("镜像库找不到文件")
        return res

    raw_main = open(pm, "rb").read()
    raw_mirror = open(pr, "rb").read()
    if sha(raw_main) != sha(raw_mirror):
        res["errors"].append("两库本来就不一致，先修一致性再疏通")
        return res
    # 快照闸门：子代理阶段不准动语料。实测有子代理自己跑去调用本脚本，
    # 而"批次前的 verify"存在时间窗（守卫跑完→落地之间还能被写）。这里逐篇核对，
    # 只要和批次快照不一致就拒绝写入这一篇。
    if snap is not None and bvid in snap:
        want = snap[bvid]
        got = {"main": sha(raw_main), "mirror": sha(raw_mirror)}
        for k in ("main", "mirror"):
            w = (want.get(k) or "")[:12]
            if not w or not got[k].startswith(w):
                res["errors"].append("语料在批次快照之后被改动（%s 库），拒绝写入" % k)
                return res

    head, body, eol = split_doc(raw_main)
    if head is None:
        res["errors"].append("frontmatter 解析失败")
        return res
    if raw_main.startswith(b"\xef\xbb\xbf"):
        res["errors"].append("源文件有 BOM")
        return res
    # 注意：老稿子（Whisper 时代）本来就带少量裸 LF，两库一致、kb verify 干净，
    # 所以这里**不强求纯 CRLF**，只要求"不许新增裸 LF"（见下方校验）。
    bare_before = raw_main.count(b"\n") - raw_main.count(b"\r\n")
    res["bare_lf_before"] = bare_before

    patches = spec.get("patches") or []
    if not patches:
        res["errors"].append("补丁为空")
        return res

    new_body = body
    # 长 find 优先：否则"术语级替换"会把后面更长 find 里的字先改掉，
    # 导致那条补丁凭空"找不到"（实测：萨加族→若开族 先应用，长 find 就失效了）
    patches = sorted(patches, key=lambda x: -len(x.get("find") or ""))
    def reject(msg, patch):
        """单条补丁不合格。--drop-bad 时只丢这一条，否则整篇放弃（默认 fail-closed）。"""
        if drop_bad:
            # 记**完整** find/replace：截断过会让 verify_landed 的"精确匹配 dropped"
            # 失配，把有理由的拒绝误报成"静默未落地"（案底：批 49 / 批 77）。
            res["dropped"].append({"kind": patch.get("kind"), "find": str(patch.get("find")),
                                   "replace": str(patch.get("replace")), "reason": msg})
            return False
        res["errors"].append(msg)
        return True

    for p in patches:
        f, r = p.get("find"), p.get("replace")
        kind = p.get("kind", "other")
        if not f or r is None:
            if reject("补丁缺 find/replace: %r" % (p,), p):
                return res
            continue
        if kind not in KINDS:
            if reject("未知 kind=%r" % kind, p):
                return res
            continue
        if f == r:
            if reject("find==replace（空改动）: %r" % f[:30], p):
                return res
            continue
        # 模型/工具里常出现 \n，而正文是 CRLF；做一次自适应，别因此白白 SKIP
        if "\n" in f and "\r\n" not in f and eol == "\r\n":
            f = f.replace("\n", "\r\n")
        if "\n" in r and "\r\n" not in r and eol == "\r\n":
            r = r.replace("\n", "\r\n")
        # 省略号绝不是"疏通"：实测模型会用 `…` 掩盖它删掉的词
        if "…" in r or "..." in r:
            if reject("replace 含省略号（疑似掩盖删除）: %r" % r[:40], p):
                return res
            continue
        # 删字必须显式声明为 stutter（口吃/冗余），且幅度受限；
        # 否则就是"悄悄删内容"—— 实测有补丁把「优于红旗欧洲团队」删成「优于欧洲团队」
        shorten = len(f) - len(r)
        if shorten > 2 and kind != "stutter":
            if reject("kind=%s 却删掉了 %d 个字（要删请用 stutter）: %r -> %r"
                      % (kind, shorten, f[:30], r[:30]), p):
                return res
            continue
        if kind == "stutter" and shorten > 8:
            if reject("stutter 的删减幅度应为 1~8 字，实际 %d" % shorten, p):
                return res
            continue
        # stutter 只许删"同一小段的重复"：允许纯删除，或删除 + 最多 2 个字顺带修正
        # （实测「这么快快的像」→「这么快得像」既删重复的"快"又改"的→得"，是对的就该放行）
        # 允许"只插入标点"的组合（如「有人说说那」→「有人说，那」：删 1 字 + 插 1 个逗号），
        # 但插入的必须**全是标点**，插任何实词都拒绝（不许增内容）。
        # 标点判定用 Unicode 类别（P*），空白也算"非内容"——否则代理顺手补的一个空格
        # 会把合法的叠词去重（如「哈萨克斯坦哈萨克斯坦」→「哈萨克斯坦 」）判成"插入实词"而误拒。
        import unicodedata
        def _noncontent(c):
            return unicodedata.category(c).startswith("P") or c.isspace()
        if kind == "stutter" and r:
            dele = ins = sub = 0
            ins_chars = []
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
                    None, f, r, autojunk=False).get_opcodes():
                if tag == "delete":
                    dele += i2 - i1
                elif tag == "insert":
                    ins += j2 - j1
                    ins_chars.append(r[j1:j2])
                elif tag == "replace":
                    dele += min(i2 - i1, j2 - j1)
                    sub += max(i2 - i1, j2 - j1)
            ins_ok = all(all(_noncontent(c) for c in s) for s in ins_chars)
            if not (1 <= dele <= 8 and ins_ok and sub <= 2):
                if reject("stutter 疑似改写（删 %d 增 %d 改 %d 字，只允许删 1~8 字 + 最多改 2 字 + 只插标点）: %r -> %r"
                          % (dele, ins, sub, f[:30], r[:30]), p):
                    return res
                continue
        # 数字不许被"顺手"改掉：find/replace 的数字多重集必须一致，除非显式声明 kind=number。
        # 这样整篇的数字守卫就变成兜底，而不会因为一条补丁把整篇的其他改动一起 SKIP 掉。
        if kind != "number" and numbers(f) != numbers(r):
            # 用户裁定（2026-10-10）：kind=name 的**专名修正**可以动数字
            # （ASR 常把术语听成带数字的字形：IC1→ICE、8V→巴威、N+1）。
            # 为防止"借专名之名改事实数字"，额外要求该补丁**同时改动拉丁字母**
            # （即确属术语级改写）；纯数字替换（3000分→4000分）仍然一律拒绝。
            ok_name = kind == "name" and (letters(f) != letters(r)
                                          or alnum_token(f) or alnum_token(r))
            # 用户裁定（2026-10-10）之外的边界：ASR 把带数字的型号整段听成汉字
            # （常见时→长剑10、坚实双座型号→歼10、钢铁雄心寺→钢铁雄心4、台风8维→台风巴威）。
            # 这类**数字凭空出现/消失**的专名修正默认仍然拒绝；只有显式加
            # --allow-name-digits 才放行（保守条件：一侧无数字、另一侧有数字，且长度差 ≤4，
            # 这样"3000分→4000分"这种纯数字替换照样被拒）。
            if not ok_name and allow_name_digits and kind == "name":
                nf, nr = numbers(f), numbers(r)
                if (bool(nf) != bool(nr)) and abs(len(f) - len(r)) <= 4:
                    ok_name = True
                elif allow_name_digits_strong and len(str(p.get("why") or "").strip()) >= 12:
                    # 强力放行（2026-10-13，仅 --allow-name-digits-strong）：用户裁定
                    # "kind=name 的术语级改写可以动数字"。代号类两侧都带数字的修正
                    # （110坦克→100坦克、457→苏-57、05大区→055大驱、8192→81192、
                    # 米格26→米格21、590坦克→59式坦克、突厥5→图95…）无法用 ASCII 代号
                    # 判据区分，因此改用"**代理写明了 ≥12 字依据**"作为闸门：
                    # 只有 why 交代了出处/理由的才放行；纯数字替换若无依据仍被拒。
                    ok_name = True
                else:
                    # 追加放行（2026-10-13，第二版）：术语**代号**改写，逐差异块判断。
                    # 只对"含数字"的差异块设限：该块向两侧扩展到连续 ASCII 代号字符后，
                    # 两侧片段必须只由 [A-Za-z0-9 空格 - .] 组成，且至少一侧含 ASCII 字母。
                    # 不含数字的差异块不受本闸门约束（错字/口吃另有各自闸门）。
                    # 放行：S-100→S-400（俄制防空）、T-141→T-14（阿玛塔）、IC1→ICE，
                    #       甚至"骁龙干掉S-100"→"枭龙干掉S-400"（中英混合一处修）。
                    # 仍拒：3000分→4000分（数字块内无字母）。
                    # （difflib 已在模块顶部导入，此处**不可**再 import：
                    #  函数内 import 会把 difflib 变成局部名，导致本函数前面第 182 行的
                    #  引用报 UnboundLocalError —— 2026-10-13 曾因此让批 91 整批没落地。）
                    CODE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -.")
                    ok_blocks = True
                    saw_code_digit = False
                    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
                            None, f, r, autojunk=False).get_opcodes():
                        if tag == "equal":
                            continue
                        bf, br = f[i1:i2], r[j1:j2]
                        if not any(ch.isdigit() for ch in bf + br):
                            continue
                        a2, b2 = i1, j1
                        while a2 > 0 and b2 > 0 and f[a2 - 1] in CODE and r[b2 - 1] in CODE:
                            a2 -= 1
                            b2 -= 1
                        c2, d2 = i2, j2
                        while c2 < len(f) and d2 < len(r) and f[c2] in CODE and r[d2] in CODE:
                            c2 += 1
                            d2 += 1
                        sf, sr = f[a2:c2], r[b2:d2]
                        if not (sf and sr and set(sf) <= CODE and set(sr) <= CODE
                                and (any(ch.isascii() and ch.isalpha() for ch in sf)
                                     or any(ch.isascii() and ch.isalpha() for ch in sr))):
                            ok_blocks = False
                            break
                        saw_code_digit = True
                    if ok_blocks and saw_code_digit:
                        ok_name = True
            if not ok_name and kind == "stutter" and set(numbers(r)) <= set(numbers(f)):
                # 口吃删除会连带删掉重复片段里的数字
                # （"16年南海对峙16南海对峙以后"→"16年南海对峙以后"、"叫塞班S60塞班S60"→"叫塞班S60"）。
                # stutter 自有更严的闸门（删 1~8 字、最多改 2 字、插入只能是标点），
                # 且此处只允许**数字减少**（子集关系），凭空多出数字仍然拒绝。
                ok_name = True
            if not ok_name:
                if reject("改动涉及数字（%s -> %s）但 kind=%s（数字改动须显式声明 number，"
                          "或 kind=name 且属于术语级改写）: %r"
                          % (numbers(f)[:4], numbers(r)[:4], kind, f[:30]), p):
                    return res
                continue
        # 防止"找得到但会误伤"：默认要求全文唯一
        occ = p.get("occurrence")
        cnt = new_body.count(f)
        if cnt == 0:
            # 已经被别的补丁改掉了？只要改后文本已出现，就算"冗余"而非"坏补丁"
            if r and r in new_body:
                res["redundant"].append({"find": f[:40], "replace": r[:40]})
                continue
            if reject("find 不存在: %r" % f[:40], p):
                return res
            continue
        if cnt > 1 and occ is None and not p.get("all"):
            if reject("find 出现 %d 次且未指定 occurrence/all: %r" % (cnt, f[:40]), p):
                return res
            continue
        if occ is not None:
            parts = new_body.split(f)
            if occ < 1 or occ >= len(parts):
                if reject("occurrence=%s 越界: %r" % (occ, f[:40]), p):
                    return res
                continue
            new_body = f.join(parts[:occ]) + r + f.join(parts[occ:])
        else:
            new_body = new_body.replace(f, r)
        res["applied"].append({"kind": kind, "find": f, "replace": r, "why": p.get("why", "")})

    if not res["applied"]:
        # 全部"冗余"= 这些补丁的内容已经在正文里了（先前被应用过，或本来就对）。
        # 这不是失败，是"无事可做"，要如实记成 already，别记成 skip（skip 会让人以为改坏了）。
        if res.get("redundant"):
            res["ok"] = True
            res["note"] = "全部补丁内容已存在于正文（先前已应用/本就正确）"
            res.update(patches=0, chars_before=len(body), chars_after=len(body),
                       bytes_before=len(raw_main), bytes_after=len(raw_main),
                       sha_before=sha(raw_main)[:12], sha_after=sha(raw_main)[:12])
            return res
        res["errors"].append("没有任何补丁通过校验（全部被丢弃）")
        return res

    # ---- 硬校验 ----
    if not new_body.strip():
        res["errors"].append("疏通后正文为空")
        return res
    if "<|" in new_body:
        res["errors"].append("正文含 SenseVoice 标签残留")
        return res
    for line in new_body.split(eol):
        if LABEL_RE.match(line.strip()):
            res["errors"].append("引入了中文表头: %r" % line[:30])
            return res
    old_nums, new_nums = numbers(body), numbers(new_body)
    if old_nums != new_nums:
        numkinds = {a["kind"] for a in res["applied"]}
        # 与逐条闸门保持一致：kind=name 的术语级改写（IC1→ICE / 8V→巴威 / CC系统部→C4系统部 /
        # 妇媒Q食→辅酶Q10）已获用户授权，可动数字。只放宽逐条闸门、忘了这里，
        # 会让这类补丁把**整篇**的其他修正一起带下水（曾把 BV1HvDVBeE4v 整篇 SKIP）。
        name_digit_ok = any(
            (a["kind"] == "name" and numbers(a["find"]) != numbers(a["replace"])
             and (letters(a["find"]) != letters(a["replace"])
                  or alnum_token(a["find"]) or alnum_token(a["replace"])
                  or (allow_name_digits
                      and bool(numbers(a["find"])) != bool(numbers(a["replace"]))
                      and abs(len(a["find"]) - len(a["replace"])) <= 4)
                  or (allow_name_digits_strong
                      and len(str(a.get("why") or "").strip()) >= 12)))
            # 口吃删除连带删掉重复片段里的数字（"叫塞班S60塞班S60"→"叫塞班S60"）：
            # 这是**独立的**放行分支，不能挂在上面 kind=="name" 的合取里。
            or (a["kind"] == "stutter"
                and set(numbers(a["replace"])) <= set(numbers(a["find"])))
            for a in res["applied"])
        if "number" not in numkinds and not name_digit_ok:
            res["errors"].append("数字被改动但没有任何 kind=number/合规 kind=name 的补丁: %s -> %s"
                                 % (old_nums[:6], new_nums[:6]))
            return res
        res["warnings"].append("数字变化（kind=number/name）: %s -> %s" % (old_nums[:6], new_nums[:6]))
    delta = abs(len(new_body) - len(body)) / max(1, len(body))
    if delta > 0.25:
        res["errors"].append("正文长度变化 %.1f%% 超过 25%%" % (delta * 100))
        return res
    new_raw = (head + new_body).encode("utf-8")
    if new_raw.startswith(b"\xef\xbb\xbf"):
        res["errors"].append("输出带 BOM")
        return res
    if eol == "\r\n":
        bare_after = new_body.count("\n") - new_body.count("\r\n")
        if bare_after > bare_before:
            res["errors"].append("新增了裸 LF: %d -> %d" % (bare_before, bare_after))
            return res

    res.update(ok=True, patches=len(res["applied"]),
               chars_before=len(body), chars_after=len(new_body),
               bytes_before=len(raw_main), bytes_after=len(new_raw),
               sha_before=sha(raw_main)[:12], sha_after=sha(new_raw)[:12])
    if dry_run:
        res["dry_run"] = True
        return res

    # ---- 归档原件（只存第一次，保住最初的转写稿）+ 两库同时写 ----
    os.makedirs(ARCHIVE, exist_ok=True)
    ap = os.path.join(ARCHIVE, name)
    if not os.path.exists(ap):
        shutil.copy2(pm, ap)
    for path in (pm, pr):
        tmp = path + ".tmp_smooth"
        with open(tmp, "wb") as f:
            f.write(new_raw)
        os.replace(tmp, path)
    a, b = sha(open(pm, "rb").read()), sha(open(pr, "rb").read())
    if a != b:
        res["errors"].append("写盘后两库不一致！")
        res["ok"] = False
        return res
    res["written"] = True
    res["archived"] = os.path.exists(ap)
    return res


def main():
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("patches", nargs="?", help="单个补丁 JSON（{bvid: {patches:[...]}}）")
    ap_.add_argument("--dir", help="补丁目录：每个 *.json 形如 {bvid, patches:[...]}（生产形态）")
    ap_.add_argument("--dry-run", action="store_true")
    ap_.add_argument("--drop-bad", action="store_true",
                     help="单条补丁不合格时只丢这一条（默认整篇放弃）")
    ap_.add_argument("--report")
    ap_.add_argument("--limit", type=int)
    ap_.add_argument("--snap", help="快照 JSON（snap-batch-N.json）：写入前逐篇核对 sha，"
                                    "不一致就拒绝写入（防止子代理在守卫之后偷改语料）")
    ap_.add_argument("--allow-name-digits", action="store_true",
                     help="放行 kind=name 且数字凭空出现/消失的专名修正（默认关闭，"
                          "需用户点头：常见时→长剑10 这类）")
    ap_.add_argument("--allow-name-digits-strong", action="store_true",
                     help="更强放行（仅回收复核用）：kind=name 且补丁 why ≥12 字时，"
                          "连两侧都带数字的型号修正也放行（110坦克→100坦克、457→苏-57、"
                          "8192→81192）。默认关闭；主通道不开。")
    args = ap_.parse_args()
    snap = json.load(open(args.snap, encoding="utf-8-sig")) if args.snap else None

    if args.dir:
        data = {}
        badfiles = []
        for fn in sorted(os.listdir(args.dir)):
            if not fn.endswith(".json"):
                continue
            try:
                # utf-8-sig：子代理偶尔会把补丁 JSON 写成带 BOM 的
                with open(os.path.join(args.dir, fn), "r", encoding="utf-8-sig") as f:
                    one = json.load(f)
            except Exception as e:
                badfiles.append((fn, str(e)[:70]))
                continue
            if not isinstance(one, dict):
                badfiles.append((fn, "顶层不是对象"))
                continue
            bvid = one.get("bvid") or os.path.splitext(fn)[0]
            data[bvid] = {"patches": one.get("patches") or [], "note": one.get("notes", "")}
        print("从目录载入 %d 篇补丁: %s" % (len(data), args.dir))
        if badfiles:
            print("!! 无法解析、已跳过的补丁文件 %d 个:" % len(badfiles))
            for fn, e in badfiles:
                print("   %s: %s" % (fn, e))
    elif args.patches:
        with open(args.patches, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    else:
        ap_.error("需要给出补丁文件或 --dir")
    items = list(data.items())
    if args.limit:
        items = items[:args.limit]

    print("=== apply_smoothing %s ===" % ("(DRY RUN)" if args.dry_run else "(写入)"))
    print("待处理: %d 篇   归档目录: %s\n" % (len(items), ARCHIVE))
    ok = skipped = 0
    total_patches = total_dropped = total_red = 0
    reports = []
    for bvid, spec in items:
        r = apply_one(bvid, spec, args.dry_run, args.drop_bad, snap,
                      getattr(args, "allow_name_digits", False),
                      getattr(args, "allow_name_digits_strong", False))
        reports.append(r)
        if r["ok"]:
            ok += 1
            total_patches += r["patches"]
            total_dropped += len(r["dropped"])
            total_red += len(r.get("redundant") or [])
            print("  OK   %-14s %d 条  %d->%d 字  %s -> %s%s%s%s"
                  % (bvid, r["patches"], r["chars_before"], r["chars_after"],
                     r["sha_before"], r["sha_after"], "  [dry]" if args.dry_run else "",
                     "  丢 %d 条" % len(r["dropped"]) if r["dropped"] else "",
                     "  冗余 %d 条" % len(r["redundant"]) if r.get("redundant") else ""))
            for a in r["applied"]:
                print("        [%-9s] %r -> %r" % (a["kind"], a["find"][:40], a["replace"][:40]))
            for d in r["dropped"]:
                print("        丢[%-9s] %r -> %r  因为 %s"
                      % (d["kind"], d["find"], d["replace"], d["reason"]))
            for w in r["warnings"]:
                print("        WARN %s" % w)
        else:
            skipped += 1
            print("  SKIP %-14s %s" % (bvid, " / ".join(r["errors"])))
    print("\n小结: 成功 %d 篇（%d 条补丁，丢弃 %d 条，冗余 %d 条），跳过 %d 篇"
          % (ok, total_patches, total_dropped, total_red, skipped))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump({"generated": datetime.datetime.now().isoformat(),
                       "dry_run": args.dry_run, "ok": ok, "skipped": skipped,
                       "total_patches": total_patches, "total_dropped": total_dropped,
                       "reports": reports},
                      f, ensure_ascii=False, indent=2)
        print("报告 -> %s" % args.report)


if __name__ == "__main__":
    main()