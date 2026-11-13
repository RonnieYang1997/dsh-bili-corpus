# -*- coding: utf-8 -*-
"""抓更新清单：调 B 站空间接口 → 过滤出"该转录的新视频"。（P3：从 _scratch/nightly/candidates.py 搬进插件）

**2026-10-15 从 AionUi 的 daily_scan.py 里搬出来的。**
此前夜间流水线靠 `subprocess` 调 AionUi 目录下的 `daily_scan.py --dry-run` 来拿候选，
而那个脚本又长又重（抓清单 + 转录 + 写 MEMORY.md + 镜像 + 触发索引全在里面），
其中转录/镜像/索引在夜间流水线里本来就有各自的专门步骤（nightly_transcribe.py），
真正需要复用的只有"抓清单 + 过滤"这几十行。所以这里只搬这一段，
用仓库里那份 skill 的 `wbi_sign/get_wbi_key`，不依赖 AionUi 的任何文件。

过滤口径（与原脚本一致）：
  · 只取 7 天内发布的
  · 已有语料的（新旧任一主库里有 dufu-<bvid>.md）排除
  · 游戏类（标题命中 GAMING_KEYWORDS）排除，并记进 gaming 缓存
  · 充电专属（历史记进 charging 缓存）排除
  · **带货/广告/碎片排除**（2026-10-20 加，见 `ad_reason()`）：
    标题含明确卖货措辞，或「商品词 + ≤5 分钟」，或总时长 < 20 秒。
    背景：UP 主会发自家商品讲解（原创外套/绵羊毛衫/纯银胸针/钥匙扣），
    这类稿子已一次性清理过 25 篇，见 `_scratch/delete_ads.py`。

用法：
  python candidates.py            # 打印候选（bvid/日期/标题）
  python candidates.py --json     # 打印 JSON（给 nightly_download.py 用）
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pipeline"))
import paths as _pp  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 路径与插件同源（DSH_CORPUS_* 环境变量优先）；旧 nightly_common 的常量在这里就地等价声明。
CORPUS = os.path.dirname(_pp.STATE)
MAIN = _pp.LANDING
MIRROR = _pp.ARCHIVE
MAIN_ROOTS = list(_pp.MAIN_ROOTS)
SKILL_SCRIPTS = os.path.dirname(os.path.abspath(__file__))


def doc_exists_anywhere(bvid):
    """任一主库里已有该讲就算已转写过。

    bvid 语料看 `dufu-<bvid>.md`；人工编号语料（docPattern）文件名不含 BV 号，
    改查 `<STATE>/docmap.json` 里 host 归档时写下的 {bvid: 文件名}。
    """
    name = _pp.doc_name(bvid)
    if any(os.path.exists(os.path.join(r, name)) for r in MAIN_ROOTS):
        return True
    dm = os.path.join(_pp.STATE, "docmap.json")
    if os.path.exists(dm):
        try:
            with open(dm, "r", encoding="utf-8") as f:
                fn = (json.load(f) or {}).get(bvid)
        except Exception:
            fn = None
        if fn and any(os.path.exists(os.path.join(r, fn)) for r in MAIN_ROOTS):
            return True
    return False


def log_line(msg):
    sys.stderr.write(str(msg) + "\n")
    sys.stderr.flush()


from bilibili_transcribe import get_wbi_key, wbi_sign, COOKIE, SSL_CTX  # noqa: E402

MID = 10850097
GAMING_KEYWORDS = ['黑神话悟空', '黑神话', '打游戏', '勇闯', '游戏实况', '游戏直播', '电竞']

# ── 用户不收录的主题（2026-11-12 加，用户口径：「NBA 和 LOL 的信息我不关注，不要收录」） ──
# 只认**标题里明确指向 NBA / 英雄联盟赛事**的词，宁漏勿误杀。
# ⚠️ 千万别加 'LOL'：UP 主的「LOL之日报」「LOL之艾欧尼亚」等是**时政节目**用游戏地图做比喻，
#    不是英雄联盟内容，加进去会把时政日报全挡掉。
TOPIC_KEYWORDS = ['NBA', '湖人', '勇士', '库里', '篮球', 'LPL', 'LCK',
                  '英雄联盟', 'S赛', 'MSI', '全球总决赛']
WINDOW_DAYS = 7

# ── 带货/广告过滤（2026-10-20 加） ─────────────────────────────────────
# 背景：UP 主会发**自家商品讲解**（原创外套/绵羊毛衫/纯银胸针/钥匙扣/鼠标垫），
# 以及极短的碎片。这些进了语料就是垃圾——2026-10-20 一次性删掉了 25 篇
# （23 条纯商品广告 + 2 条别的频道串进来的求赞模板），见 `_scratch/delete_ads.py`。
#
# 判定只用**候选阶段就能拿到的信息**（标题 + 时长），因为此时还没下载音频。
# 拿不到正文，所以宁可漏也别误杀：只认**明确的卖货措辞**或「商品词 + 短视频」。
AD_TITLE_STRONG = [
    '已上架', '橱窗里有售', '橱窗', '定制款', '定制版', '预售开始',
    '讲解来啦', '讲解一下我们的', '必备外套', '送人收藏', '已开售',
    '小黄车', '拍一发', '到手价',
    # 2026-10-20 回归补的：
    # 「品质胜过一切」（卖男装，标题无商品词）
    # 「…原创鼠标垫与青年军定制钥匙扣发售」（20 分钟，超出短视频线）
    # 「【独夫之心观天下】视频已打包，欢迎围观！」（别的频道串进来的求赞模板）
    '品质胜过', '欢迎围观', '视频已打包',
    # 2026-10-20 用户确认「卖书也是广告」后补的。只取**精确**措辞：
    # 「签售会」「新书发售」这种不取 —— 它们大量出现在日报标题里
    # （`2024.8.29日报（新书发售、…）`、`问答环节（…新书签售会）`），
    # 那些是真内容，加了会误杀。
    '新书出版', '地图册讲解', '带书',
]
# 商品名词（用于「商品词 + 卖货修饰」和「商品词 + 商品属性」两条规则）
AD_TITLE_PRODUCT = [
    '外套', '夹克', '毛衫', '毛衣', '短袖', 'T恤', '卫衣', '胸针', '吊坠',
    '钥匙扣', '鼠标垫', '披肩', '围巾', '帆布包', '马克杯',
]
# 商品词旁边出现这些词，无论多长都算带货（直播讲解自家周边会长达几十分钟）
# '独夫之心' 也算：标题里带频道名 + 商品词 = 自家周边（如「独夫之心莫代尔…圆领短袖」28 分钟）
AD_TITLE_SELL = ['原创', '定制', '发售', '讲解', '上新', '预售', '新品',
                 '独夫之心', '必备', '包邮', '秒杀', '试衣']
# 商品属性词：商品详情式标题的典型特征（「抗风速干抗皱…外套」「含绵羊毛混纺夹克」）。
# 命中 ≥2 个就基本可以断定这条标题在描述一件商品，而不是在讲一件事。
AD_TITLE_ATTR = [
    '面料', '拉链', '垫肩', '版型', '尺码', '领口', '袖口', '下摆', '内里',
    '透气', '抗皱', '防风', '速干', '加绒', '保暖', '显瘦', '修身', '弹力',
    '克重', '剪裁', '绵羊毛', '羊毛', '羊绒', '莫代尔', '纯银', '925',
    '精纺', '混纺', '抗菌', '免烫', '亲肤', '垂感', '走线', '机洗', '水洗',
    '三合一', '盘玩', '不易皱', '不易变形', '好打理',
]
# 商品类视频极少超过这个时长；超过就不按广告处理（长的是直播/闲聊，可能有真内容）
AD_MAX_SECONDS = 300
# 短到不可能是正经稿子（碎片/贴片）。实测那两条别的频道串进来的都是 11s/23s，
# 而 23s 的「SpaceX计划上市」是真新闻，所以阈值取 20s。
MIN_SECONDS = 20


def _parse_length(v):
    """把 vlist 的 length（"3:41" / "1:02:33" / 数字）转成秒；失败返回 -1。"""
    raw = v.get('length')
    if raw is None:
        return -1
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip()
    if not s:
        return -1
    try:
        parts = [int(x) for x in s.split(':')]
    except Exception:
        return -1
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def _book_only_title(title):
    """标题是不是**只有一个书名**（《…》之外几乎没内容）。"""
    rest = re.sub(r'《[^》]*》', '', title)
    rest = re.sub(r'[\s:：\-—|｜,，.。!！?？]', '', rest)
    return bool(re.search(r'《[^》]*》', title)) and len(rest) <= 4


def ad_reason(v):
    """这条是不是带货/广告/碎片？是则返回原因字符串，否则 None。

    **判定以标题为主**（2026-10-20 用户口径）：标题能说清的就不看时长，
    否则容易误杀「正经的短新闻」（`SpaceX计划上市` 23 秒、`U23亚洲杯四强`）。
    时长只用于两条与"广告"无关的兜底：
      · 短于 20 秒 —— 碎片/贴片，不可能是正经稿子
      · 只有一个书名且很短 —— 单本书的推销
    """
    title = (v.get('title') or '')
    sec = _parse_length(v)

    for kw in AD_TITLE_STRONG:
        if kw in title:
            return "标题含卖货措辞「%s」" % kw

    prod = next((kw for kw in AD_TITLE_PRODUCT if kw in title), None)
    if prod:
        for kw in AD_TITLE_SELL:
            if kw in title:
                return "标题含商品词「%s」+「%s」" % (prod, kw)
        attrs = [kw for kw in AD_TITLE_ATTR if kw in title]
        if len(attrs) >= 2:
            return "标题含商品词「%s」+ %d 个商品属性词（%s）" % (
                prod, len(attrs), "、".join(attrs[:3]))

    if 0 <= sec < MIN_SECONDS:
        return "时长 %ds < %ds（碎片/贴片）" % (sec, MIN_SECONDS)
    if _book_only_title(title) and 0 <= sec <= AD_MAX_SECONDS:
        return "标题只有书名《…》且时长 %ds ≤ %ds（单本书推销）" % (sec, AD_MAX_SECONDS)
    return None

# User-Agent 轮换表。**桌面浏览器 UA 必须排第一**：
# 2026-10-17 实测（同一 WBI key、同样匿名、相隔 3 秒各打一次 page 1）：
#   UA = BiliApp/1.0.0 android              -> code=-352（风控，0 条）
#   UA = Mozilla/5.0 (Windows NT 10.0...)   -> code=0（50 条）
# 原 daily_scan.py 用的是 App UA，在 2026-09 还能跑（当时实测 250/250 成功），
# 但 B站后来改了风控，App UA 现在直接 -352。UA 会随 B站策略变，
# 所以这里做成"逐个试、谁通行就用谁"，而不是写死一个。
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'BiliApp/1.0.0 android',
]

CHARGING_CACHE = os.path.join(CORPUS, "charging_videos.json")
GAMING_CACHE = os.path.join(CORPUS, "gaming_videos.json")
# 这两个 state 文件历史上就镜像进仓库（语料根的杂散文件白名单里有它们）
CHARGING_IN_REPO = os.path.join(MIRROR, "charging_videos.json")
GAMING_IN_REPO = os.path.join(MIRROR, "gaming_videos.json")
# 手工排除：短剧/切片/不属语料范围的，加了就永不再列（与 charging/gaming 同格式）
EXCLUDE_CACHE = os.path.join(CORPUS, "excluded_videos.json")
EXCLUDE_IN_REPO = os.path.join(MIRROR, "excluded_videos.json")


def _load_bvids(path):
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return set()
    if isinstance(data, dict):
        return set(data.get("bvid") or [])
    if isinstance(data, list):
        out = set()
        for it in data:
            out.add(it if isinstance(it, str) else (it or {}).get("bvid", ""))
        return {b for b in out if b}
    return set()


def _save_bvids(path, bvids):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"bvid": sorted(bvids), "updated": datetime.now().isoformat()},
                  f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def fetch_all_videos(pages=5, per_page=50):
    """抓该 UP 主全部投稿。WBI key **只取一次并复用**（见下方注释）。"""
    # FIX (2026-09-23, 从 daily_scan.py 继承)：WBI mixin key 每次运行只取一次。
    # 原实现把 get_wbi_key() 放在重试循环里，请求数翻倍，且 nav 被限流时会返回
    # 过期 key，导致每次重试都 -352（风控）。实测：缓存 key → 250/250 条成功，
    # 旧写法只有 50/250。
    mixin_key = None
    for nav_attempt in range(8):
        try:
            k = get_wbi_key()
            if k and len(k) >= 32:
                mixin_key = k
                break
        except Exception as e:
            log_line("  WBI key 第 %d 次失败：%s" % (nav_attempt + 1, e))
        time.sleep(4 + nav_attempt * 3)
    if not mixin_key:
        raise RuntimeError("8 次尝试仍拿不到 WBI key")
    log_line("  WBI key 已缓存（%s...）" % mixin_key[:8])

    all_videos = []
    preferred = None
    for pn in range(1, pages + 1):
        data = None
        for attempt in range(8):
            params = {'mid': MID, 'ps': per_page, 'pn': pn,
                      'order': 'pubdate', 'platform': 'android'}
            signed = wbi_sign(params, mixin_key)
            qs = '&'.join('%s=%s' % (k, signed[k]) for k in sorted(signed.keys()))
            req = urllib.request.Request(
                'https://api.bilibili.com/x/space/wbi/arc/search?' + qs)
            # 上一页通过的 UA 放前面，但**其余 UA 照样要试**：
            # 2026-10-17 实测 B站风控在两种 UA 之间来回跳（同一分钟内
            # 桌面 UA 报 412、App UA 报 -352，下一次又反过来），
            # 所以"锁定一个 UA"反而会把自己锁死。这里只是排个优先级。
            uas = ([preferred] if preferred else []) + \
                  [u for u in USER_AGENTS if u != preferred]
            data = None
            err = None
            for ua in uas:
                req.remove_header('User-Agent')
                req.add_header('User-Agent', ua)
                req.add_header('Referer', 'https://www.bilibili.com/')
                if COOKIE:
                    req.add_header('Cookie', COOKIE)
                try:
                    with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as resp:
                        data = json.loads(resp.read().decode())
                except urllib.error.HTTPError as e:
                    # **这一条是 2026-10-17 补的**：原来 urlopen 抛 HTTPError（例如
                    # 412 Precondition Failed）会直接冲出整个尝试循环 —— 不重试、
                    # 不换 UA，整页当场判死。实测 21:00 班就是这样连挂三轮的。
                    err = "HTTP %s" % e.code
                    log_line("  第 %d 页 UA=%s：%s" % (pn, ua[:28], err))
                    continue
                except Exception as e:
                    err = "%s: %s" % (type(e).__name__, e)
                    log_line("  第 %d 页 UA=%s：%s" % (pn, ua[:28], err))
                    continue
                if data.get('code') == 0:
                    preferred = ua
                    break
                err = "code=%s" % data.get('code')
                log_line("  第 %d 页 UA=%s：%s" % (pn, ua[:28], err))
                data = None
            if data is not None and data.get('code') == 0:
                break
            wait = 3 + attempt * 3
            log_line("  第 %d 页第 %d 次失败（%s），%ds 后重试" % (pn, attempt + 1, err, wait))
            time.sleep(wait)
        if not data or data.get('code') != 0:
            log_line("  第 %d 页失败：%s" % (pn, err or '无响应'))
            break
        vlist = data.get('data', {}).get('list', {}).get('vlist', [])
        all_videos.extend(vlist)
        log_line("  第 %d 页：%d 条" % (pn, len(vlist)))
        if len(vlist) < per_page:
            break
        time.sleep(2)
    log_line("  共抓到 %d 条投稿" % len(all_videos))
    return all_videos


def has_cookie():
    """有没有可用的 B站凭据（登录 cookie）。结果缓存，只解析一次。

    为什么这个判断重要：**充电专属视频在有 cookie 时是能取的**。
    2026-10-17 实测 —— 同一条 `BV1vBJX6dE4q`（upower 独占）：
      · 无 cookie → `ChargingVideoError`（拿不到流）
      · 带 cookie → 正常下载 2.9MB
    所以"充电专属"不是永久不可取，只是**匿名取不到**。缓存里那些
    bvid 只该在**没有 cookie**时跳过；一旦配了 cookie 就要重新尝试，
    否则会白白漏掉一批本来能转的稿子。
    """
    global _COOKIE_STATE
    if _COOKIE_STATE is None:
        # 插件内嵌引擎把 cookie 直接写在 bilibili_transcribe.COOKIE 里（没有
        # bili_credentials 这个外部模块）；旧 skill 目录才靠它读凭据文件。
        # 先试外部模块，缺了就退回内嵌 COOKIE——判定口径不变：有凭据才重试充电专属。
        _COOKIE_STATE = False
        try:
            if SKILL_SCRIPTS not in sys.path:
                sys.path.insert(0, SKILL_SCRIPTS)
            from bili_credentials import resolve_cookie  # type: ignore
            _COOKIE_STATE = bool(resolve_cookie(quiet=True))
        except Exception:
            try:
                _COOKIE_STATE = bool(str(COOKIE or "").strip())
            except Exception as e:
                log_line("  读取 B站凭据失败（按无凭据处理）：%s" % e)
                _COOKIE_STATE = False
    return _COOKIE_STATE


_COOKIE_STATE = None


def pick_new(videos, days=None):
    """过滤出该转录的新视频。返回 (new_videos, gaming_skipped)。"""
    charging = _load_bvids(CHARGING_CACHE) | _load_bvids(CHARGING_IN_REPO)
    gaming = _load_bvids(GAMING_CACHE) | _load_bvids(GAMING_IN_REPO)
    excluded = _load_bvids(EXCLUDE_CACHE) | _load_bvids(EXCLUDE_IN_REPO)
    # 有凭据时不再按"充电专属"跳过（见 has_cookie 注释）
    skip_charging = not has_cookie()
    if not skip_charging and charging:
        log_line("  已配置 B站凭据：充电专属缓存里的 %d 篇将重新尝试" % len(charging))
    cutoff = datetime.now() - timedelta(days=int(days or WINDOW_DAYS))
    new_videos, skipped_gaming = [], 0
    skipped_ad = []
    skipped_topic = []
    for v in videos:
        try:
            created = datetime.fromtimestamp(v['created'])
        except Exception:
            continue
        if created <= cutoff:
            continue
        bvid = v.get('bvid') or ''
        if not bvid:
            continue
        # 已有语料（新旧任一主库）→ 不是新的
        if doc_exists_anywhere(bvid):
            continue
        if bvid in gaming:
            continue
        if bvid in excluded:      # 手工排除清单（短剧/切片等）
            continue
        if skip_charging and bvid in charging:
            continue
        title = v.get('title') or ''
        if any(kw in title for kw in GAMING_KEYWORDS):
            gaming.add(bvid)
            skipped_gaming += 1
            continue
        hit = next((kw for kw in TOPIC_KEYWORDS if kw in title), None)
        if hit:
            excluded.add(bvid)      # 记进手工排除清单，跨窗口也不会再回来
            skipped_topic.append((bvid, title, hit))
            continue
        why = ad_reason(v)
        if why:
            skipped_ad.append((bvid, title, why))
            continue
        new_videos.append(v)
    if skipped_ad:
        log_line("  跳过带货/广告/碎片 %d 篇：" % len(skipped_ad))
        for bvid, title, why in skipped_ad:
            log_line("    %s  %s  ← %s" % (bvid, title[:36], why))
    if skipped_topic:
        log_line("  跳过不收录主题（NBA/英雄联盟）%d 篇：" % len(skipped_topic))
        for bvid, title, kw in skipped_topic:
            log_line("    %s  %s  ← %s" % (bvid, title[:36], kw))
    # 缓存回写（语料根 + 仓库镜像）
    _save_bvids(CHARGING_CACHE, charging)
    _save_bvids(GAMING_CACHE, gaming)
    _save_bvids(EXCLUDE_CACHE, excluded)
    for src, dst in ((CHARGING_CACHE, CHARGING_IN_REPO), (GAMING_CACHE, GAMING_IN_REPO),
                     (EXCLUDE_CACHE, EXCLUDE_IN_REPO)):
        try:
            if os.path.isdir(MIRROR):
                with open(src, "rb") as f1, open(dst, "wb") as f2:
                    f2.write(f1.read())
        except Exception as e:
            log_line("  缓存镜像失败（不影响主流程）：%s" % e)
    return new_videos, skipped_gaming


def add_charging_bvids(bvids):
    """把充电专属视频记进缓存（语料根 + 仓库镜像）。返回累计条数。

    下载班抓到 `ChargingVideoError` 时调用：这类视频是永久拿不到流的，
    记下来才不会每晚重抓、每晚失败。`pick_new` 读同一个缓存做过滤。
    """
    cur = _load_bvids(CHARGING_CACHE) | _load_bvids(CHARGING_IN_REPO)
    cur |= set(bvids)
    _save_bvids(CHARGING_CACHE, cur)
    try:
        if os.path.isdir(MIRROR):
            with open(CHARGING_CACHE, "rb") as f1, open(CHARGING_IN_REPO, "wb") as f2:
                f2.write(f1.read())
    except Exception as e:
        log_line("  充电缓存镜像失败（不影响主流程）：%s" % e)
    return len(cur)


def to_items(new_videos):
    """转成 nightly_download.py 用的元组列表：(bvid, date, time, title)。"""
    out = []
    for v in new_videos:
        d = datetime.fromtimestamp(v['created'])
        out.append((v.get('bvid'), d.strftime('%Y-%m-%d'), d.strftime('%H:%M'),
                    (v.get('title') or '').strip()))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON（给上层调用）")
    ap.add_argument("--pages", type=int, default=5)
    ap.add_argument("--days", type=int, default=WINDOW_DAYS)
    a = ap.parse_args()

    videos = fetch_all_videos(pages=a.pages)
    new_videos, skipped = pick_new(videos, a.days)
    log_line("  7 天内新视频 %d 篇（跳过游戏 %d 篇）" % (len(new_videos), skipped))
    items = to_items(new_videos)

    if a.json:
        print(json.dumps({"count": len(items), "items": items}, ensure_ascii=False))
    else:
        for b, d, tm, ti in items:
            print("  %s  %s %s  %s" % (b, d, tm, ti))
    return 0


if __name__ == "__main__":
    sys.exit(main())
