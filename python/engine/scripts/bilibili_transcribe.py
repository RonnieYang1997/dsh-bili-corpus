#!/usr/bin/env python3
"""
Bilibili (B站) Video Transcriber
Download audio from Bilibili videos via WBI-signed DASH API,
transcribe to Chinese text with **local SenseVoice (sherpa-onnx, CPU)**,
and save as memory files.
Requires: Python 3.8+, standard library only. ffmpeg for audio splitting.

2026-10-09 迁移：**在线 Whisper API 已丢弃**，改为本机 SenseVoice。
  识别跑在技能自己的 .venv 里（scripts/sensevoice_asr.py），
  本模块把它当"外部可执行文件"调用（与 FFMPEG 同样处理），因此仍然只用标准库。
  模型取自用户已备好的 DSH 插件目录（~/.dsh/speech-to-text/...），不重复下载。
"""

import os, sys, json, hashlib, urllib.parse, urllib.request, urllib.error, ssl, time, re, struct, shutil, subprocess, tempfile

# ============================================================
# Configuration
# ============================================================

# 凭据（2026-10-15 整改）：**源码里绝不存放 cookie**。
# 这里曾在第 21 行硬编码一整条真实账号 cookie（SESSDATA + bili_jct），
# 等于把账号凭据写进源码随 git 走。现已抽到 bili_credentials.py：
#   查找顺序 = 显式参数 → 环境变量 → ./cookies.txt → <skill>/cookies.txt
#              → ~/.config/bilibili-transcribe/cookies.txt（推荐）
# 空串是**合法状态**：B站公开视频的低清档位无需登录，匿名即可取流。
# 调用方若拿到 -101/-404/-352，再向用户索取 cookie 并 set_cookie()。
try:
    from bili_credentials import (resolve_cookie as _resolve_cookie,
                                  cookie_hint, validate_cookie)
except ImportError:  # 直接以脚本方式运行（scripts/ 未在 sys.path 上）
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from bili_credentials import (resolve_cookie as _resolve_cookie,
                                  cookie_hint, validate_cookie)

COOKIE = _resolve_cookie(quiet=True)


def set_cookie(cookie=None, cookie_file=None, quiet=True):
    """覆盖本次会话使用的 cookie（返回生效值）。空串 = 匿名。"""
    global COOKIE
    COOKIE = _resolve_cookie(explicit=cookie, cookie_file=cookie_file, quiet=quiet)
    return COOKIE


# 2026-10-09：在线 Whisper 的端点与密钥已**全部移除**（连同 multipart 上传代码）。
# 现役识别引擎 = 本机 SenseVoice，见文件末尾的 "Local ASR" 一节。

class ChargingVideoError(Exception):
    """Raised when a video is charging-exclusive and only a preview is available."""
    pass

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'

# SSL context (ignore cert errors for some CDN endpoints)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# ============================================================
# FFmpeg path
# ============================================================

def _bili_home():
    """运行时自备目录：venv / ffmpeg / （可选）模型。

    默认 `~/.dsh/bili-corpus`，`DSH_BILI_HOME`（或 `BILI_HOME`）可覆盖。
    2026-11-10：引擎从独立技能目录搬进插件后，**不再从任何技能目录借用运行时**——
    解释器、ffmpeg 都放这里，模型优先用 DSH 的 speech-to-text 目录（兜底）。
    """
    env = os.environ.get('DSH_BILI_HOME') or os.environ.get('BILI_HOME')
    if env:
        return env
    return os.path.join(os.path.expanduser('~'), '.dsh', 'bili-corpus')


def _ffmpeg_candidates():
    """候选 ffmpeg 路径（按优先级）。只列，不判断。

    **技能自带优先**（`<技能>/bin/`，由 `fetch_ffmpeg.py` 一次性装好），
    PATH 与系统安装只作最后兜底。

    2026-10-15：不再去 AionUi/DSH 的会话目录里"借用"别人的 ffmpeg.exe。
    那是**运行时依赖别人的安装** —— 对方一删、一搬、一换机器就断，
    而且断得很晚很难查（实测断在"取时长"那步，报 WinError 2，指向还不对）。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    skill = os.path.dirname(here)
    cands = [
        os.environ.get('FFMPEG', ''),
        os.environ.get('SENSEVOICE_FFMPEG', ''),
        # 运行时自备目录（首选，由 fetch_ffmpeg.py 或手动放置）
        os.path.join(_bili_home(), 'bin', 'ffmpeg.exe'),
        os.path.join(_bili_home(), 'ffmpeg.exe'),
        # 引擎自带
        os.path.join(skill, 'bin', 'ffmpeg.exe'),
        os.path.join(skill, 'ffmpeg.exe'),
        os.path.join(here, 'ffmpeg.exe'),
        # 当前工作目录
        os.path.join(os.getcwd(), 'ffmpeg.exe'),
        # 旧布局：脚本往上四层
        os.path.join(here, '..', '..', '..', '..', 'ffmpeg.exe'),
        # 系统级安装（用户自己装的才走这里）
        os.path.join(os.path.expanduser('~'), 'scoop', 'shims', 'ffmpeg.exe'),
        r'C:\ffmpeg\bin\ffmpeg.exe',
        shutil.which('ffmpeg') or '',
    ]
    return [c for c in cands if c]


def _runs(exe):
    """该可执行文件能否真的跑起来（返回码 0）。不用管道（R22）。"""
    try:
        r = subprocess.run([exe, '-version'], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def _find_ffmpeg():
    """找到**能跑**的 ffmpeg。

    2026-10-15 修：原实现只查 4 个位置、也不验证可用性，找不到就退化成裸名
    `'ffmpeg'`；本机 PATH 上没有 ffmpeg（它躺在 AionUi 会话目录里），
    于是下载成功后在"取时长"那一步才炸出 WinError 2，报错还指向别处。
    现在：逐个验证能执行再选定，并覆盖本机已知位置。
    """
    for c in _ffmpeg_candidates():
        if (os.path.isfile(c) or shutil.which(c)) and _runs(c):
            return c
    return 'ffmpeg'


FFMPEG = _find_ffmpeg()


# R22: never let an ffmpeg/ffprobe child stall a whole batch.
# `subprocess.run(..., timeout=)` is NOT enough on Windows: when the deadline passes,
# the follow-up communicate() can block forever on a pipe that a grandchild still holds.
# Observed live: one chunk of a 3-hour video kept ffmpeg spinning for 25+ minutes and the
# whole 14-video job never moved past item 1.  Both helpers below avoid pipes entirely.
def run_ffmpeg(cmd, timeout=300, tag=""):
    """Run ffmpeg with a hard deadline. Raises RuntimeError on timeout/failure."""
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            p.wait(timeout=20)
        except Exception:
            pass
        raise RuntimeError("ffmpeg timed out after %ss (%s)" % (timeout, tag))
    if p.returncode != 0:
        raise RuntimeError("ffmpeg rc=%s (%s)" % (p.returncode, tag))
    return p.returncode


def run_capture(cmd, timeout=60, tag=""):
    """Run a command and capture stdout **without pipes** (safe on Windows)."""
    with tempfile.TemporaryFile() as tf:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=tf,
                             stderr=subprocess.DEVNULL)
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                p.wait(timeout=20)
            except Exception:
                pass
            raise RuntimeError("command timed out after %ss (%s)" % (timeout, tag))
        if p.returncode != 0:
            raise RuntimeError("command rc=%s (%s)" % (p.returncode, tag))
        tf.seek(0)
        return tf.read().decode("utf-8", errors="replace")


def run_capture_stderr(cmd, timeout=60, tag="", ok_rc=(0,)):
    """Run a command and capture **stderr** without pipes (safe on Windows).

    给 `ffmpeg -i` 这类"信息打在 stderr 上、且故意以非 0 退出"的命令用：
    `ffmpeg -i <file>` 没给输出文件，所以返回 1，但 Duration 已经打在 stderr 上了。
    与 run_capture 一样走临时文件而不是管道（Windows 上管道是踩过的坑）。
    """
    with tempfile.TemporaryFile() as tf:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=tf)
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                p.wait(timeout=20)
            except Exception:
                pass
            raise RuntimeError("command timed out after %ss (%s)" % (timeout, tag))
        tf.seek(0)
        out = tf.read().decode("utf-8", errors="replace")
    if p.returncode not in ok_rc:
        raise RuntimeError("command rc=%s (%s)" % (p.returncode, tag))
    return out


RE_FFMPEG_DUR = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")


def ffprobe_duration(file_path, timeout=60):
    """Duration in seconds —— **只用 ffmpeg**，不再需要 ffprobe.exe。

    2026-10-15：ffprobe.exe 有 161MB，而它在这里只干一件事 —— 报时长。
    `ffmpeg -i <file>` 本来就把 `Duration: HH:MM:SS.xx` 打在 stderr 上
    （且因为没给输出文件而返回 1，所以这里容忍 rc=1）。
    去掉 ffprobe 让技能自带运行时的体积**减半**（323MB → 161MB），
    下载量也减半，且少一个要维护的二进制。

    函数名保留：调用点有好几处，改名只会扩大改动面。
    """
    out = run_capture_stderr(
        [FFMPEG, "-hide_banner", "-i", file_path], timeout=timeout,
        tag="ffmpeg -i " + os.path.basename(file_path), ok_rc=(0, 1))
    m = RE_FFMPEG_DUR.search(out)
    if not m:
        raise RuntimeError("ffmpeg 没能报出时长（%s）" % os.path.basename(file_path))
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

# ============================================================
# Logging
# ============================================================

def init_logging(level='INFO'):
    """Setup logging for daily scan and other batch operations."""
    import logging
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S'
    )

# ============================================================
# WBI Signing
# ============================================================

# MIXIN_KEY_ENC_TAB: 64-element index array for deriving mixin key from img_key + sub_key
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52
]

def get_mixin_key():
    """Fetch WBI mixin key from Bilibili nav API. Returns 32-char hex string."""
    try:
        nav_url = 'https://api.bilibili.com/x/web-interface/nav'
        req = urllib.request.Request(nav_url, headers={
            'User-Agent': UA,
            'Referer': 'https://www.bilibili.com/',
            'Origin': 'https://www.bilibili.com',
            'Cookie': COOKIE
        })
        resp = urllib.request.urlopen(req, timeout=15, context=SSL_CTX)
        data = json.loads(resp.read().decode('utf-8'))
        wbi_img = data['data']['wbi_img']
        img_url = wbi_img['img_url']
        sub_url = wbi_img['sub_url']

        img_key = img_url.rsplit('/', 1)[-1].split('.')[0]
        sub_key = sub_url.rsplit('/', 1)[-1].split('.')[0]
        combined = img_key + sub_key

        key_chars = []
        for idx in MIXIN_KEY_ENC_TAB:
            if idx < len(combined):
                key_chars.append(combined[idx])
        return ''.join(key_chars)[:32]
    except Exception as e:
        # NEVER return a made-up key.  A wrong mixin key silently produces invalid
        # w_rid signatures; the API then answers with -352 / "请求错误", which looks
        # exactly like random rate-limiting and hides the real cause.  Fail loudly
        # so the caller can back off and retry.   (R8 / R9)
        raise RuntimeError('failed to fetch WBI mixin key from nav API: %s' % e)

# Global WBI cache
_WBI_KEY = None

def get_wbi_key():
    """Get WBI key with caching."""
    global _WBI_KEY
    if _WBI_KEY is None:
        _WBI_KEY = get_mixin_key()
    return _WBI_KEY

def wbi_sign(params, mixin_key=None):
    """Sign a parameter dict with WBI MD5 signature. Adds w_rid and wts."""
    if mixin_key is None:
        mixin_key = get_wbi_key()
    wts = int(time.time())
    params = dict(params)  # don't mutate original
    params['wts'] = str(wts)
    # Sort keys alphabetically
    sorted_keys = sorted(params.keys())
    query = '&'.join(f'{k}={params[k]}' for k in sorted_keys)
    sign_str = query + mixin_key
    params['w_rid'] = hashlib.md5(sign_str.encode()).hexdigest()
    return params

# ============================================================
# Bilibili API helpers
# ============================================================

def api_request(url, params=None, referer='https://www.bilibili.com/'):
    """Make an API request with proper headers and WBI signing if needed."""
    if params is None:
        params = {}
    if 'wbi/arc/search' in url or 'wbi/playurl' in url:
        params = wbi_sign(params)

    if params:
        query = '&'.join(f'{k}={params[k]}' for k in sorted(params.keys()))
        full_url = f'{url}?{query}' if '?' not in url else f'{url}&{query}'
    else:
        full_url = url

    req = urllib.request.Request(full_url, headers={
        'User-Agent': UA,
        'Referer': referer,
        'Origin': 'https://www.bilibili.com',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Cookie': COOKIE
    })
    resp = urllib.request.urlopen(req, timeout=15, context=SSL_CTX)
    return json.loads(resp.read().decode('utf-8'))

def get_video_info(bvid):
    """Get video metadata (title, cid, duration, pic, etc.)."""
    url = 'https://api.bilibili.com/x/web-interface/view'
    data = api_request(url, {'bvid': bvid})
    if data.get('code') != 0:
        raise Exception(f"get_video_info failed: {data.get('message')}")
    return data['data']

def get_audio_url(bvid, cid):
    """Get DASH audio URL for a video. Tries fnval=4048 first, then 16."""
    for fnval in [4048, 16]:
        try:
            data = api_request(
                'https://api.bilibili.com/x/player/wbi/playurl',
                {'bvid': bvid, 'cid': cid, 'fnval': str(fnval), 'qn': '0', 'fourk': '1'},
                referer=f'https://www.bilibili.com/video/{bvid}'
            )
            if data.get('code') != 0:
                continue

            dash = data.get('data', {}).get('dash')
            if dash:
                audios = dash.get('audio', [])
                if audios:
                    # Pick highest quality audio
                    best = max(audios, key=lambda a: a.get('bandwidth', 0))
                    return best['base_url'], data['data'].get('timelength', 0) // 1000

            # Try from durl fallback
            durl = data.get('data', {}).get('durl', [])
            if durl:
                best = max(durl, key=lambda d: d.get('size', 0))
                return best['url'], data['data'].get('timelength', 0) // 1000

        except Exception:
            continue

    raise Exception(f"No audio URL found for BV={bvid} cid={cid}")

# ============================================================
# Audio Download
# ============================================================

def download_file(url, output_path, referer='https://www.bilibili.com/'):
    """Download a file with proper headers and resume support."""
    headers = {
        'User-Agent': UA,
        'Referer': referer,
        'Origin': 'https://www.bilibili.com',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Cookie': COOKIE
    }

    existing_size = 0
    if os.path.exists(output_path):
        existing_size = os.path.getsize(output_path)
        if existing_size > 0:
            headers['Range'] = f'bytes={existing_size}-'

    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=600, context=SSL_CTX)
    except urllib.error.HTTPError as e:
        # 416 = 本地文件已有长度 >= 服务端长度，即上次被杀时其实已经下完。
        # 旧行为直接抛错，一整讲报废（082/088 实测）；新行为：本地文件可能只是
        # 长度凑巧相等但内容损坏，所以删掉重下一次，而不是断言它有效。
        if e.code != 416 or existing_size == 0:
            raise
        os.remove(output_path)
        headers.pop('Range', None)
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=600, context=SSL_CTX)
        existing_size = 0

    mode = 'ab' if existing_size > 0 else 'wb'
    with open(output_path, mode) as f:
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            f.write(chunk)

    return output_path

def download_flv_extract_audio(bvid, cid, output_dir):
    """For copyright videos: download FLV preview and extract AAC audio."""
    try:
        data = api_request(
            'https://api.bilibili.com/x/player/wbi/playurl',
            {'bvid': bvid, 'cid': cid, 'fnval': '1', 'qn': '0'},
            referer=f'https://www.bilibili.com/video/{bvid}'
        )
        durl = data.get('data', {}).get('durl', [])
        if not durl:
            return None

        flv_url = durl[0]['url']
        flv_path = os.path.join(output_dir, f'{bvid}_preview.flv')

        download_file(flv_url, flv_path, referer=f'https://www.bilibili.com/video/{bvid}')

        # Extract audio with ffmpeg
        m4a_path = os.path.join(output_dir, f'{bvid}_preview.m4a')
        run_ffmpeg([FFMPEG, '-y', '-nostdin', '-loglevel', 'error', '-i', flv_path,
                    '-vn', '-acodec', 'aac', m4a_path],
                   timeout=120, tag='extract preview audio ' + bvid)
        os.remove(flv_path)
        return m4a_path
    except Exception:
        return None

def select_page(info, page=1):
    """从 view 接口的数据里挑出**某一分 P** 的 (cid, 时长, 分P标题, 分P总数)。

    为什么必须挑：多分 P 视频（讲座、课程、连续剧）的 `duration` 是**所有分 P 之和**，
    而顶层 `cid` 只是第 1 个分 P。直接拿顶层那两个字段配着用，就会得到
    "时长 23.7 小时、只下到 1.6 小时"的假象 —— 实测把一个 14 讲课程的第 1 讲
    误判成 ChargingVideoError（充电专属预览）。见 R42。
    """
    pages = info.get("pages") or []
    count = len(pages) or 1
    try:
        page = int(page or 1)
    except (TypeError, ValueError):
        page = 1
    if page < 1 or page > count:
        raise ValueError("分 P 超出范围：page=%s，共 %d 个分 P" % (page, count))
    if pages:
        pg = pages[page - 1]
        return (pg.get("cid") or info.get("cid"),
                pg.get("duration") or info.get("duration", 0),
                pg.get("part") or "",
                count)
    return info.get("cid"), info.get("duration", 0), "", 1


def download_audio(bvid, output_dir='_transcribe_temp', page=1):
    """Download DASH M4A audio for a video. Returns (m4a_path, info_dict).

    `page` 是**从 1 开始**的分 P 序号（多分 P 视频必需，见 select_page）。
    Raises ChargingVideoError if the downloaded file is a truncated charging preview."""
    os.makedirs(output_dir, exist_ok=True)

    info = get_video_info(bvid)
    cid, duration, part, page_count = select_page(info, page)
    title = info['title']
    # frontmatter "date" must be the video's PUBLISH date, never the day the
    # transcription happened to run.  Falling back to today's date silently
    # writes wrong metadata on any back-fill / batch run.
    _pubdate = info.get('pubdate', 0)
    try:
        date_str = time.strftime('%Y-%m-%d', time.localtime(int(_pubdate))) if _pubdate else ''
    except (TypeError, ValueError, OSError):
        date_str = ''

    # Early detection: charging-exclusive (UPOWER) video without play access.
    # is_upower_exclusive=True marks 充电专属 videos; is_upower_play=False means
    # the current account has no charging access, so only a preview is served.
    if info.get('is_upower_exclusive') and not info.get('is_upower_play'):
        raise ChargingVideoError(
            f"Charging video (upower exclusive, no play access): {title} ({duration}s)"
        )

    try:
        audio_url, dur_from_api = get_audio_url(bvid, cid)
        # 多分 P 的文件名必须带页码，否则转第 2 个分 P 会覆盖第 1 个
        _name = f'{bvid}.m4a' if page_count == 1 else f'{bvid}-p{int(page):02d}.m4a'
        m4a_path = os.path.join(output_dir, _name)
        download_file(audio_url, m4a_path, referer=f'https://www.bilibili.com/video/{bvid}')
        if not duration:
            duration = dur_from_api

        # Check if the downloaded file is a charging preview (truncated).
        # 这里比的是**本分 P 的时长**，不是整条视频的总时长（R42）。
        actual_dur = get_audio_duration(m4a_path)
        if duration > 0 and actual_dur < duration * 0.2:
            raise ChargingVideoError(
                f"Charging video detected: API reports {duration}s but downloaded only {actual_dur:.0f}s"
            )

        return m4a_path, {'title': title, 'duration_sec': duration, 'bvid': bvid,
                          'date': date_str, 'page': int(page),
                          'page_count': page_count, 'part': part}
    except ChargingVideoError:
        raise
    except Exception as e:
        # Fallback: try copyright FLV approach
        m4a_path = download_flv_extract_audio(bvid, cid, output_dir)
        if m4a_path:
            # Report the ACTUAL length of the file we got, not an arbitrary
            # 180s cap (the old min(duration,180) wrote wrong metadata).
            try:
                real_dur = get_audio_duration(m4a_path)
            except Exception:
                real_dur = duration
            if not real_dur:
                real_dur = duration
            # keep the frontmatter value integral: ffprobe returns a float
            # (e.g. 181.323991), which looks out of place next to the API's ints.
            try:
                real_dur = int(round(float(real_dur)))
            except (TypeError, ValueError):
                real_dur = int(duration or 0)
            return m4a_path, {'title': title, 'duration_sec': real_dur, 'bvid': bvid,
                              'date': date_str, 'page': int(page),
                              'page_count': page_count, 'part': part}
        raise e

# ============================================================
# Audio Splitting
# ============================================================

def get_audio_duration(file_path):
    """Get audio duration in seconds（走 ffmpeg，不再需要 ffprobe，见上）。"""
    return ffprobe_duration(file_path)

def split_audio(m4a_path, max_seconds=1200):
    """Split a long M4A into chunks of max_seconds each using ffmpeg.
    Re-encodes at 64k to keep each chunk well under Whisper's 25MB limit.
    Returns list of chunk file paths."""
    duration = get_audio_duration(m4a_path)
    max_sec = max_seconds
    if duration <= max_sec:
        return [m4a_path]

    base_dir = os.path.dirname(m4a_path)
    base_name = os.path.splitext(os.path.basename(m4a_path))[0]
    chunks = []

    num_chunks = int(duration / max_sec) + (1 if duration % max_sec > 0 else 0)
    for i in range(num_chunks):
        start = i * max_sec
        chunk_path = os.path.join(base_dir, f'{base_name}_chunk_{i:03d}.m4a')
        # R22: -ss MUST precede -i.  Placed after -i, ffmpeg decodes the file from the
        # beginning and throws the audio away: on a 3-hour video the 9th chunk would have
        # to decode 160 minutes first.  Input-side seeking is instant.
        run_ffmpeg([
            FFMPEG, '-y', '-nostdin', '-loglevel', 'error',
            '-ss', str(start), '-t', str(max_sec),
            '-i', m4a_path,
            '-vn', '-acodec', 'aac', '-b:a', '64k',
            chunk_path
        ], timeout=300, tag="split chunk %d/%d of %s" % (i + 1, num_chunks, base_name))
        if not os.path.exists(chunk_path) or os.path.getsize(chunk_path) == 0:
            raise RuntimeError("empty chunk produced: %s" % chunk_path)
        chunks.append(chunk_path)

    return chunks

# ============================================================
# Local ASR (SenseVoice) + 转录结果守卫
# ============================================================

# R21: an OpenAI-compatible endpoint may answer HTTP 200 with an *error payload*:
#   {"error":{"type":"upstream_error","message":"...Request Entity Too Large..."}}
# The old code returned that string as the transcript, so 14 corpus files ended up
# holding error JSON instead of speech (kb-mcp reports them as "dead").  Never let an
# error payload pass as a transcript: verify, then either split smaller or raise.
RE_ERROR_PAYLOAD = re.compile(r'^\s*\{.*?"(error|message)"\s*:', re.S)
API_ERROR_MARKS = (
    "upstream_error", "invalid_request_error", "request_too_large",
    "request entity too large", "payload too large", "bad gateway",
    "service unavailable", "internal server error", "gateway time-out",
    "invalid_api_key", "rate_limit_exceeded",
)

# R19: 连麦/问答 metadata contract.  `联麦` is a common typo for `连麦` and the
# guard rejects it, so it must never appear in the text we generate either.
RE_QA_TITLE = re.compile(r"连麦|联麦|问答")
QA_WARNING = (
    "此视频为连麦/问答形式，包含提问者或连麦对象的观点。"
    "请勿将提问者/连麦对象的观点直接当作独夫之心的观点。"
    "应通过上下文理解独夫之心对相关观点的真实立场（支持、反驳或补充）。"
    "引用时以独夫之心本人的表述为准。"
)


class WhisperPayloadError(Exception):
    """转录结果无效：空串，或（历史上）在线端点回的错误载荷。

    2026-10-09 起识别改在本机跑，不会再出现"HTTP 200 + 错误 JSON"这种情况；
    但"空结果"依然可能（音频没人声、解码失败），所以这个守卫与 save_transcript
    里的最后一道防线都保留 —— R21 的价值在于**绝不让无效内容落盘**，
    而不是它当初是在哪一层发现的。类名保持兼容，不改调用方。
    """

    def __init__(self, text):
        super().__init__("transcript rejected (empty or error payload): %s" % (text or "")[:200])
        self.text = text or ""
        low = self.text.lower()
        self.too_large = ("too_large" in low or "too large" in low
                          or "413" in low or "entity" in low and "large" in low)


def looks_like_api_error(text):
    """True when `text` is an API error payload rather than transcribed speech.

    Deliberately conservative: a long transcript may legitimately mention
    "Bad Gateway" when 独夫之心 is talking about websites, so marks only count
    for short bodies, and a JSON error signature is required for anything longer.
    """
    t = (text or "").strip()
    if not t:
        return True
    if RE_ERROR_PAYLOAD.match(t) and len(t) < 4000:
        return True
    # Real error bodies are tiny (the 14 corpus stubs are 170-226 chars).  Keeping the
    # mark-only path to very short bodies stops "...they often hit Bad Gateway..." in a
    # genuine transcript from being thrown away.
    if len(t) < 500 and any(m in t.lower() for m in API_ERROR_MARKS):
        return True
    return False


def _skill_dir():
    """引擎自带资源目录（插件内 = `python/engine`）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sensevoice_cli():
    return os.path.join(_skill_dir(), 'scripts', 'sensevoice_asr.py')


_ASR_PY_CACHE = None


def _asr_candidates():
    """候选 ASR 解释器，按优先级排列（只列路径，不判断可用性）。

    **只认技能自己的 venv**（`<技能>/.venv`，由 `setup.sh` 建）。

    2026-10-15：删掉了"复用 AionUi 会话目录里那份技能的 venv"的逻辑。
    那是**运行时依赖别人的安装**：看着省事（零安装），实则把技能绑死在
    另一个应用的私有目录上 —— 那个应用一卸载、一升级、换台机器，
    技能就"找不到解释器"，而报错完全指不出真正的原因。
    技能要能独立存活：自己的 venv、自己的模型、自己的 ffmpeg。
    """
    here = _skill_dir()
    home = _bili_home()
    cands = [
        os.environ.get('SENSEVOICE_PYTHON', ''),
        # 运行时自备（首选）：~/.dsh/bili-corpus/.venv
        os.path.join(home, '.venv', 'Scripts', 'python.exe'),
        os.path.join(home, '.venv', 'bin', 'python'),
        os.path.join(here, '.venv', 'Scripts', 'python.exe'),
        os.path.join(here, '.venv', 'bin', 'python'),
        # SKILL.md 早期写的是 scripts/.venv —— 两种位置都认，免得文档与代码打架
        os.path.join(here, 'scripts', '.venv', 'Scripts', 'python.exe'),
        os.path.join(here, 'scripts', '.venv', 'bin', 'python'),
    ]
    return [c for c in cands if c]


def _has_sherpa(py):
    """该解释器能否 import sherpa_onnx。

    不用管道（R22）：Windows 上管道收尾会卡死，改用 DEVNULL + 硬超时。
    """
    try:
        r = subprocess.run([py, '-c', 'import sherpa_onnx'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=90)
        return r.returncode == 0
    except Exception:
        return False


def _sensevoice_python():
    """承载 sherpa-onnx 的解释器。

    采集器本身跑在 python-embedded（没有 pip），所以识别必须由别的解释器承担。
    这里**逐个验证能 import sherpa_onnx**再选定（结果缓存），而不是"路径存在就用"——
    否则一个空壳 venv 会让识别在很后面才失败，报错还指向别处。
    找不到就明确报错，绝不静默退化成别的东西。
    """
    global _ASR_PY_CACHE
    if _ASR_PY_CACHE is not None:
        return _ASR_PY_CACHE
    for c in _asr_candidates():
        if os.path.isfile(c) and _has_sherpa(c):
            _ASR_PY_CACHE = c
            return c
    _ASR_PY_CACHE = ''
    return ''


def _first_existing(paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return ''


def _plugin_models():
    return os.path.join(os.path.expanduser('~'), '.dsh', 'speech-to-text',
                        'sensevoice', 'models')


def _sensevoice_model_dir():
    """模型查找顺序：环境变量 → **技能自带** → DSH 插件目录（兜底）。

    2026-10-15：把技能自带提到前面。技能要能独立存活，不能默认指望
    `~/.dsh` 下那份插件模型一直在（它属于 DSH 的另一个插件，不是本技能的资产）。
    """
    return _first_existing([
        os.environ.get('SENSEVOICE_MODEL_DIR', ''),
        os.path.join(_bili_home(), 'models', 'sensevoice-small'),
        os.path.join(_bili_home(), 'models', 'sensevoice-onnx'),
        os.path.join(_skill_dir(), 'models', 'sensevoice-small'),
        os.path.join(_skill_dir(), 'models', 'sensevoice-onnx'),
        os.path.join(_plugin_models(), 'sensevoice-onnx'),
    ])


def _sensevoice_vad_model():
    return _first_existing([
        os.environ.get('SENSEVOICE_VAD_MODEL', ''),
        os.path.join(_bili_home(), 'models', 'silero', 'silero_vad.onnx'),
        os.path.join(_skill_dir(), 'models', 'silero', 'silero_vad.onnx'),
        os.path.join(_plugin_models(), 'silero', 'silero_vad.onnx'),
    ])


def _run_sensevoice(files, language='zh', timeout=None, tag=''):
    """调用本地 ASR CLI，返回转录正文；任何失败都抛异常，绝不返回空串。

    无管道（R22）：Windows 上父进程用管道收子进程输出，会在收尾时卡死，
    所以这里把 stdout/stderr 都落到临时文件再读回。
    """
    cli = _sensevoice_cli()
    py = _sensevoice_python()
    if not os.path.isfile(cli):
        raise RuntimeError('SenseVoice CLI 缺失: %s' % cli)
    if not py:
        raise RuntimeError(
            '未找到 SenseVoice 解释器。需要技能目录下的 .venv'
            '（见 SKILL.md「本地 ASR」一节）；模型目录=%s'
            % (_sensevoice_model_dir() or '(未找到)'))

    # 超时按音频时长给（实测 RTF≈0.1，留足余量）；ffprobe 拿不到时长就用下限。
    if timeout is None:
        dur = 0
        for f in files:
            try:
                dur = max(dur, get_audio_duration(f))
            except Exception:
                pass
        timeout = max(900, 120 + int(dur))

    model_dir = _sensevoice_model_dir()
    if not model_dir:
        raise RuntimeError('未找到 SenseVoice 模型目录（可用 fetch_sensevoice_model.py 重建）')

    cmd = [py, cli] + list(files) + ['--language', language, '--quiet',
                                     '--model-dir', model_dir]
    # 把本模块**已验证可运行**的 ffmpeg 显式传给子进程。两边各有一套查找逻辑，
    # 不传的话子进程会自己再找一次并可能失败（2026-10-15 实测：
    # 父进程已解析到可用 ffmpeg，子进程却报 "找不到 ffmpeg: ffmpeg"）。
    if FFMPEG and (os.path.isfile(FFMPEG) or shutil.which(FFMPEG)):
        cmd += ['--ffmpeg', FFMPEG]
    vad = _sensevoice_vad_model()
    if vad:
        cmd += ['--vad-model', vad]

    with tempfile.TemporaryFile() as tf, tempfile.TemporaryFile() as ef:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=tf, stderr=ef)
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                p.wait(timeout=20)
            except Exception:
                pass
            raise RuntimeError('SenseVoice 超时 (%ss, %s)' % (timeout, tag))
        tf.seek(0)
        out = tf.read().decode('utf-8', errors='replace')
        ef.seek(0)
        err = ef.read().decode('utf-8', errors='replace')

    if p.returncode != 0:
        raise RuntimeError('SenseVoice CLI rc=%s (%s): %s'
                           % (p.returncode, tag, err.strip()[-400:]))
    text = out.strip()
    # R21 仍然保留：空结果/错误载荷绝不许当成转录稿落盘
    if looks_like_api_error(text):
        raise WhisperPayloadError(text)
    return text


def transcribe_file(file_path, language='zh', model=None):
    """用**本地 SenseVoice** 转录一个音频文件，返回文本。

    `model` 参数只为向后兼容（Whisper 时代的模型名）而保留，现已忽略。
    """
    return _run_sensevoice([file_path], language, tag=os.path.basename(file_path))


def transcribe_long_audio(m4a_path, language='zh', model=None):
    """用**本地 SenseVoice** 转录长音频。

    与 Whisper 时代最大的不同：**不再按 25MB 分片**。
    本地引擎没有体积上限，且 CLI 内部已用 silero VAD 把长音频切成 ≤20s 的语音段
    （切口落在静音、并补回被削掉的起始音节），所以这里就是把整个文件交给它。
    既少了一层"分片-拼接"的复杂度，也消除了分片边界处的断句问题。
    （`split_audio` 仍保留在文件里，供其它脚本/排查复用。）
    """
    return _run_sensevoice([m4a_path], language, tag=os.path.basename(m4a_path))


# ============================================================
# Transcript Saving
# ============================================================

def transcript_path(output_dir, bvid, name_template=None, page=None):
    """按命名模板算出稿件路径。默认 `{bvid}.md`（通用）；
    独夫之心语料用 `--name-template 'dufu-{bvid}.md'`。

    多分 P 视频（讲座/课程）必须给 `page`，否则 14 讲会全写进同一个文件。
    模板里可显式写 `{p}`（展开成 `p01`）；没写就自动在扩展名前补 `-p01`。
    """
    tpl = name_template or os.environ.get("BILI_NAME_TEMPLATE") or "{bvid}.md"
    fields = {"bvid": bvid}
    if page is not None:
        fields["p"] = "p%02d" % int(page)
        fields["page"] = int(page)
        if "{p}" not in tpl and "{page}" not in tpl:
            base, ext = os.path.splitext(tpl)
            tpl = base + "-{p}" + ext
    elif "{p}" in tpl or "{page}" in tpl:
        raise ValueError("命名模板里有 {p}/{page}，但没有指定分 P（--p）")
    return os.path.join(output_dir, tpl.format(**fields))


def _page_for_name(video_info):
    """只有**多分 P** 视频才在文件名里带页码，单分 P 保持 `{bvid}.md` 不变。

    这样既有语料（独夫之心 3760 篇）的命名一个都不动，而课程类视频不会互相覆盖。
    """
    info = video_info or {}
    if (info.get('page_count') or 1) > 1:
        return info.get('page') or 1
    return None


def save_transcript(bvid, text, video_info=None, output_dir=None, name_template=None):
    """Save transcript as a memory file. Returns file path."""
    # R21: the last line of defence - an error payload must never become a transcript
    # file (14 such files had to be cleaned out of the corpus by hand).
    if looks_like_api_error(text):
        raise WhisperPayloadError(text)
    if output_dir is None:
        output_dir = os.path.join(os.getcwd(), 'memory')
    os.makedirs(output_dir, exist_ok=True)

    if video_info is None:
        video_info = {}
    title = video_info.get('title', bvid)
    # R6/R14：`date` 只信 API 的 pubdate。取不到就**留空并告警**，绝不静默填今天。
    # 2026-10-15 修：原实现是 `... or time.strftime('%Y-%m-%d')`，批量回填/补录时
    # 会把"运行日"当成"发布日"写进历史 —— 正是 R6 记载的那类事故。
    date_str = (video_info.get('date') or '').strip()
    if not date_str:
        sys.stderr.write("[warn] %s 取不到 pubdate，date 留空（R6/R14：不得填今天）\n" % bvid)
    duration = video_info.get('duration_sec', 0)

    # R19: a 连麦/问答 video interleaves other people's opinions with the author's,
    # so the metadata has to say whose view is whose - otherwise search will cite
    # the asker as 独夫之心.  Applied here, at the single write path, because R19
    # was originally only a *lint rule*: it was documented and the existing corpus
    # was repaired by hand, but nothing enforced it on new videos, so every new
    # 连麦 collection re-created the defect and the guard only caught it afterwards.
    extra = ''
    if RE_QA_TITLE.search(title or ''):
        # unquoted, matching the existing corpus convention (`type: dialogue`)
        extra = ('type: dialogue\n'
                 'warning: "%s"\n' % QA_WARNING)
    # R42：多分 P 视频的 `title` 是**整条视频**的标题，14 讲全都一样，
    # 光看 frontmatter 分不出这是第几讲。所以多分 P 时补 `part`（B站给的分P名，
    # 课程类通常就是 "1" "2" …）。单分 P 不写，既有语料一个字不动。
    if (video_info.get('page_count') or 1) > 1:
        _part = str(video_info.get('part') or video_info.get('page') or '').strip()
        extra += 'part: "%s"\n' % _part.replace('"', "'")

    yaml_header = f"""---
title: "{title}"
bvid: "{bvid}"
date: "{date_str}"
duration: {duration}
source: "bilibili"
{extra}---

"""
    filepath = transcript_path(output_dir, bvid, name_template,
                               page=_page_for_name(video_info))
    # R26：新建文件跟随多数派 CRLF，且**不混行**。原实现靠 Windows 的
    # 通用换行翻译隐式产生 CRLF，在非 Windows 上会变成 LF（同一技能两种产物）；
    # 这里显式归一，结果与平台无关。
    newline = os.environ.get("BILI_NEWLINE", "crlf").lower()
    body = yaml_header + text
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    if newline != "lf":
        body = body.replace("\n", "\r\n")
    with open(filepath, 'w', encoding='utf-8', newline='') as f:
        f.write(body)

    return filepath

# ============================================================
# Full Pipeline
# ============================================================

def process_video(bvid, output_dir=None, temp_dir=None, name_template=None,
                  keep_audio=False, min_chars=3000, page=None):
    """Full pipeline: download audio → transcribe → save transcript.

    keep_audio=True 时**不删**临时音频（拆"先下载、后识别"两班跑时必需：
    默认行为的 finally 会把音频删掉，第二班就无料可识别）。
    `page` 是 1 起的分 P 序号（多分 P 课程必需）；None = 第 1 个分 P 且不加页码后缀。
    """
    if temp_dir is None:
        temp_dir = os.path.join(os.getcwd(), '_transcribe_temp')
    os.makedirs(temp_dir, exist_ok=True)

    if output_dir is None:
        output_dir = os.path.join(os.getcwd(), 'memory')

    _pg = 1 if page is None else int(page)
    # 缓存判定的路径要先知道"这是不是多分 P"，所以先探一次 info（一次 view 请求）
    _probe = None
    try:
        _probe = get_video_info(bvid)
        _pcount = len((_probe.get('pages') or [])) or 1
        _cid_check, _dur_check, _part_check, _pcount = select_page(_probe, _pg)
    except Exception:
        _pcount = 1
        _dur_check = 0
        _part_check = ''

    def _path_for(pnum):
        return transcript_path(output_dir, bvid, name_template,
                               page=pnum if _pcount > 1 else None)

    # Check if already transcribed (skip short/truncated files)
    existing = _path_for(_pg)
    if os.path.exists(existing):
        with open(existing, 'r', encoding='utf-8') as f:
            content = f.read()
        # 语料是 CRLF，用 '---\n' 切**切不开**（实际是 '---\r\n'），于是 `text`
        # 退化成整个文件、所谓"正文长度"里混进了 frontmatter，缓存判定偏乐观
        # （正文被截断但含 frontmatter 后仍 >3000 就会被误判为"已完成"）。
        # 改用同时容纳 CRLF/LF 的正则切出正文。
        _m = re.match(r'^---\r?\n.*?\r?\n---\r?\n', content, re.S)
        text_stripped = (content[_m.end():] if _m else content).strip()
        # 短视频（片头/通知/广告）正文天然只有几个字，不能拿 3000 字去卡，
        # 否则每次重跑都会白重转一遍（2026-10-15 实测：16 秒的片头每次都重转）。
        _dm = re.search(r'^duration:\s*(\d+)', content, re.M)
        _dur = int(_dm.group(1)) if _dm else 0
        cached_ok = len(text_stripped) >= min_chars or (text_stripped and _dur <= 120)
        if cached_ok:
            return {
                'bvid': bvid,
                'path': existing,
                'duration': _dur,
                'text_length': len(text_stripped),
                'cached': True,
                'page': _pg if _pcount > 1 else None,
            }

    try:
        m4a_path, info = download_audio(bvid, temp_dir, page=_pg)
        text = transcribe_long_audio(m4a_path)
        filepath = save_transcript(bvid, text, info, output_dir, name_template)

        return {
            'bvid': bvid,
            'path': filepath,
            'duration': info.get('duration_sec', 0),
            'text_length': len(text),
            'cached': False,
            'page': info.get('page') if (info.get('page_count') or 1) > 1 else None,
            'part': info.get('part') or '',
        }
    except ChargingVideoError:
        raise
    finally:
        # Cleanup temp files（keep_audio=True 时保留，供第二班识别）
        if not keep_audio:
            try:
                for f in os.listdir(temp_dir):
                    if f.startswith(bvid):
                        os.remove(os.path.join(temp_dir, f))
            except Exception:
                pass

# ============================================================
# Main entry point
# ============================================================

if __name__ == '__main__':
    # 本模块只提供函数。**面向用户的统一入口是 transcribe.py**（下载→本地识别→
    # 落盘→校验一条龙，只问"转哪些视频""存哪里"）。这里保留旧式单篇调用做兜底。
    if len(sys.argv) >= 2 and sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        print("用法（单篇兜底）：python bilibili_transcribe.py <bvid> [输出目录]")
        print("推荐入口：python transcribe.py --help")
        sys.exit(0)

    if len(sys.argv) < 2:
        print("用法（单篇兜底）：python bilibili_transcribe.py <bvid> [输出目录]")
        print("推荐入口：python transcribe.py   ← 只问「转哪些视频」「存哪里」")
        sys.exit(1)

    _bvid = sys.argv[1]
    _out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.getcwd(), 'memory')
    result = process_video(_bvid, output_dir=_out)
    print("Done: %s -> %s" % (result['bvid'], result['path']))
    print("  Duration: %ss, Text: %s chars, cached=%s"
          % (result['duration'], result['text_length'], result['cached']))
