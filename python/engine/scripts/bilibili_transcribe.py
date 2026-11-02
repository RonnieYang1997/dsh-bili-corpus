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

_DEFAULT_COOKIE = ""  # 不在仓库里存 cookie：从 BILI_COOKIE 环境变量或 ~/.dsh/bili-cookie.txt 读取


COOKIE_FILE = "C:\\Users\\Ronnie/.dsh/bili-cookie.txt"


def _load_cookie():
    """Cookie 取法：环境变量 BILI_COOKIE → ~/.dsh/bili-cookie.txt → 下方内置兜底。

    换 cookie 只要覆盖文件（或设环境变量）即可，不用改代码。
    """
    v = str(os.environ.get("BILI_COOKIE") or "").strip()
    if v:
        return v
    try:
        if os.path.isfile("C:\\Users\\Ronnie/.dsh/bili-cookie.txt"):
            t = open("C:\\Users\\Ronnie/.dsh/bili-cookie.txt", encoding="utf-8-sig").read().strip()
            if t:
                return t
    except Exception:
        pass
    return _DEFAULT_COOKIE


COOKIE = _load_cookie()

# 2026-10-09：在线 Whisper 的端点与密钥已**全部移除**（连同 multipart 上传代码）。
# 现役识别引擎 = 本机 SenseVoice，见文件末尾的 "Local ASR" 一节。
# 顺带的好处：这个文件里不再存放任何第三方 API 凭据。

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

def _find_ffmpeg():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for candidate in [
        # 本机已备好的一份（P1 起 ffmpeg/ffprobe 都搬到 DSH 家目录，不再依赖会话目录）
        os.path.join(os.path.expanduser('~'), '.dsh', 'speech-to-text', 'ffmpeg', 'ffmpeg.exe'),
        os.path.join(script_dir, '..', '..', '..', '..', 'ffmpeg.exe'),
        os.path.join(script_dir, '..', 'ffmpeg.exe'),
        os.path.join(os.getcwd(), 'ffmpeg.exe'),
        'ffmpeg',
    ]:
        if shutil.which(candidate) or os.path.isfile(candidate):
            return candidate
    return 'ffmpeg'

FFMPEG = _find_ffmpeg()


def _ffprobe_path():
    """ffprobe 与 ffmpeg 同目录同基名。

    注意：**只能换基名，不能对整串做 replace**——本机目录就叫 `ffmpeg`
    （`~/.dsh/speech-to-text/ffmpeg/ffmpeg.exe`），整串替换会把目录也改成
    `ffprobe`，得到不存在的 `.../ffprobe/ffprobe.exe`，子进程直接
    `FileNotFoundError [WinError 2]`（2026-11-02 长稿转写失败的根因）。
    """
    head, base = os.path.split(FFMPEG)
    probe = base.replace('ffmpeg', 'ffprobe')
    cand = os.path.join(head, probe) if head else probe
    if os.path.isfile(cand) or (not head and shutil.which(cand)):
        return cand
    return shutil.which('ffprobe') or cand


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


def ffprobe_duration(file_path, timeout=60):
    """Duration in seconds via ffprobe (pipe-free)."""
    probe = _ffprobe_path()
    out = run_capture([probe, "-v", "error", "-show_entries", "format=duration",
                       "-of", "default=noprint_wrappers=1:nokey=1", file_path],
                      timeout=timeout, tag="ffprobe " + os.path.basename(file_path))
    return float(out.strip())

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
        # 416 = 断点续传的 Range 越界。最常见的原因不是"文件坏了"，而是
        # 上一次进程被杀时**音频其实已经下完**，本地文件是完整的，服务端却
        # 认为 bytes=<size>- 越界。这时不能重试 Range，必须删掉本地文件从 0 重下。
        if e.code == 416 and existing_size > 0:
            print(f'  Range 越界（本地已有 {existing_size} 字节，多半是上次下完没记账），删除后整段重下')
            try:
                os.remove(output_path)
            except OSError:
                pass
            headers.pop('Range', None)
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=600, context=SSL_CTX)
            existing_size = 0
        else:
            raise

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

def download_audio(bvid, output_dir='_transcribe_temp'):
    """Download DASH M4A audio for a video. Returns (m4a_path, info_dict).
    Raises ChargingVideoError if the downloaded file is a truncated charging preview."""
    os.makedirs(output_dir, exist_ok=True)

    info = get_video_info(bvid)
    cid = info['cid']
    title = info['title']
    duration = info.get('duration', 0)
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
        m4a_path = os.path.join(output_dir, f'{bvid}.m4a')
        download_file(audio_url, m4a_path, referer=f'https://www.bilibili.com/video/{bvid}')
        if not duration:
            duration = dur_from_api
        
        # Check if the downloaded file is a charging preview (truncated)
        actual_dur = get_audio_duration(m4a_path)
        if duration > 0 and actual_dur < duration * 0.2:
            raise ChargingVideoError(
                f"Charging video detected: API reports {duration}s but downloaded only {actual_dur:.0f}s"
            )
        
        return m4a_path, {'title': title, 'duration_sec': duration, 'bvid': bvid, 'date': date_str}
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
            return m4a_path, {'title': title, 'duration_sec': real_dur, 'bvid': bvid, 'date': date_str}
        raise e

# ============================================================
# Audio Splitting
# ============================================================

def get_audio_duration(file_path):
    """Get audio duration in seconds using ffprobe."""
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
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sensevoice_cli():
    return os.path.join(_skill_dir(), 'scripts', 'sensevoice_asr.py')


def _sensevoice_python():
    """承载 sherpa-onnx 的解释器（技能自己的 venv）。

    采集器本身跑在 python-embedded（没有 pip），所以识别必须由这个 venv 承担；
    这里只做"找到它"，找不到就明确报错，绝不静默退化成别的东西。
    """
    for c in (os.environ.get('SENSEVOICE_PYTHON', ''),
              os.path.join(_skill_dir(), '.venv', 'Scripts', 'python.exe'),
              os.path.join(_skill_dir(), '.venv', 'bin', 'python')):
        if c and os.path.isfile(c):
            return c
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
    """模型查找顺序：环境变量 → DSH 插件（用户已备好）→ 技能自带兜底。"""
    return _first_existing([
        os.environ.get('SENSEVOICE_MODEL_DIR', ''),
        os.path.join(_plugin_models(), 'sensevoice-onnx'),
        os.path.join(_skill_dir(), 'models', 'sensevoice-small'),
    ])


def _sensevoice_vad_model():
    return _first_existing([
        os.environ.get('SENSEVOICE_VAD_MODEL', ''),
        os.path.join(_plugin_models(), 'silero', 'silero_vad.onnx'),
        os.path.join(_skill_dir(), 'models', 'silero', 'silero_vad.onnx'),
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

def save_transcript(bvid, text, video_info=None, output_dir=None, name=None):
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
    # Prefer the real publish date; only fall back to today when it is genuinely
    # unknown (e.g. the caller passed a hand-built info dict).
    date_str = video_info.get('date') or time.strftime('%Y-%m-%d')
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

    yaml_header = f"""---
title: "{title}"
bvid: "{bvid}"
date: "{date_str}"
duration: {duration}
source: "bilibili"
{extra}---

"""
    # P3：文件名可由调用方指定（标题式命名）；默认仍是 dufu-<bvid>.md
    filepath = os.path.join(output_dir, name or f'dufu-{bvid}.md')
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(yaml_header)
        f.write(text)

    return filepath

# ============================================================
# Full Pipeline
# ============================================================

def process_video(bvid, output_dir=None, temp_dir=None, name=None):
    """Full pipeline: download audio → transcribe → save transcript."""
    if temp_dir is None:
        temp_dir = os.path.join(os.getcwd(), '_transcribe_temp')
    os.makedirs(temp_dir, exist_ok=True)

    if output_dir is None:
        output_dir = os.path.join(os.getcwd(), 'memory')

    # Check if already transcribed (skip short/truncated files)
    existing = os.path.join(output_dir, name or f'dufu-{bvid}.md')
    if os.path.exists(existing):
        with open(existing, 'r', encoding='utf-8') as f:
            content = f.read()
        # 语料是 CRLF，用 '---\n' 切**切不开**（实际是 '---\r\n'），于是 `text`
        # 退化成整个文件、所谓"正文长度"里混进了 frontmatter，缓存判定偏乐观
        # （正文被截断但含 frontmatter 后仍 >3000 就会被误判为"已完成"）。
        # 改用同时容纳 CRLF/LF 的正则切出正文。
        _m = re.match(r'^---\r?\n.*?\r?\n---\r?\n', content, re.S)
        text_stripped = (content[_m.end():] if _m else content).strip()
        # If transcript is <3000 chars for a known long video, re-transcribe
        # (500 chars = ~1 minute of speech, clearly truncated)
        if len(text_stripped) >= 3000:
            return {
                'bvid': bvid,
                'path': existing,
                'duration': 0,
                'text_length': len(text_stripped),
                'cached': True
            }

    try:
        m4a_path, info = download_audio(bvid, temp_dir)
        text = transcribe_long_audio(m4a_path)
        filepath = save_transcript(bvid, text, info, output_dir, name=name)

        return {
            'bvid': bvid,
            'path': filepath,
            'duration': info.get('duration_sec', 0),
            'text_length': len(text),
            'cached': False
        }
    except ChargingVideoError:
        raise
    finally:
        # Cleanup temp files
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
    if len(sys.argv) < 2:
        print("Usage: python bilibili_transcribe.py <bvid>")
        sys.exit(1)

    bvid = sys.argv[1]
    result = process_video(bvid)
    print(f"Done: {result['bvid']} → {result['path']}")
    print(f"  Duration: {result['duration']}s, Text: {result['text_length']} chars")
