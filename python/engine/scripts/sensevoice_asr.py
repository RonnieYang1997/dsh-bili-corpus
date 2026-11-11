# -*- coding: utf-8 -*-
"""本地 SenseVoice 语音识别 CLI（sherpa-onnx + SenseVoiceSmall int8，纯 CPU）。

为什么要"独立进程 + CLI"，而不是在采集进程里 import：
  采集器跑在 `python-embedded`（嵌入式 3.12，**没有 pip**，见 daily_scan.py），
  技能自身的契约又是 "Python 3.8+ / standard library only"。
  所以模型依赖只能落在本技能自己的 `.venv` 里，由这个 CLI 承载，
  父进程把它当"外部可执行文件"调用 —— 与既有 FFMPEG 的做法一致。

模型从哪来（重要，2026-10-09 更正）：
  用户已经在本机的 **DSH 插件**里备好了同一份模型，**不要再下载**：
      ~/.dsh/speech-to-text/sensevoice/models/sensevoice-onnx/{model.int8.onnx,tokens.txt}
      ~/.dsh/speech-to-text/sensevoice/models/silero/silero_vad.onnx
  查找顺序：$SENSEVOICE_MODEL_DIR → 插件目录 → 技能自带 models/。
  技能自带的那份只作为"换机器时用 fetch_sensevoice_model.py 重建"的兜底。

长音频怎么办：SenseVoice 是非流式、短窗模型，**不能整段 2 小时直接喂**。
  这里一次性 ffmpeg 解码成 16k 单声道，再用 **silero VAD** 切分成 ≤20s 的语音段
  （切口落在静音，既不丢字也不重复；silero 模型同样用插件里那份）。
  没有 VAD 模型时退化为"逐帧能量 + 静音游程"的切分，保证仍有输出。

契约（父进程依赖）：
  * stdout  **只**输出转录正文（UTF-8）；
  * stderr  进度/耗时/告警；
  * 退出码   0 成功；2 模型缺失；3 输入缺失；4 ffmpeg 失败；5 识别为空。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

# R3: 中文诊断脚本在 CP936 控制台下必须把**两个流**都换成 UTF-8
# （只换 stdout 的话，走 stderr 的告警会变成 "޷ȡ" 这种乱码）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
PLUGIN_ROOT = Path.home() / ".dsh" / "speech-to-text" / "sensevoice" / "models"

# 模型查找顺序：环境变量 → **技能自带** → DSH 插件目录（兜底）。
# 2026-10-15：技能自带提到前面 —— 技能要能独立存活，不能默认指望
# `~/.dsh` 下那份别的插件的模型一直在（见 fetch_sensevoice_model.py）。
MODEL_DIR_CANDIDATES = [
    os.environ.get("SENSEVOICE_MODEL_DIR", ""),
    str(SKILL_ROOT / "models" / "sensevoice-small"),
    str(SKILL_ROOT / "models" / "sensevoice-onnx"),
    str(PLUGIN_ROOT / "sensevoice-onnx"),
]
VAD_CANDIDATES = [
    os.environ.get("SENSEVOICE_VAD_MODEL", ""),
    str(SKILL_ROOT / "models" / "silero" / "silero_vad.onnx"),
    str(PLUGIN_ROOT / "silero" / "silero_vad.onnx"),
]

# SenseVoice 的富文本标签（<|zh|><|NEUTRAL|><|Speech|><|woitn|>）与事件符号，
# 一旦漏进语料库就是 3741 个文件级别的污染 —— 必须防御性剥掉（R1 那次事故的教训）。
RE_SV_TAG = re.compile(r"<\|[^|>]*\|>")
RE_BRACKET_EVENT = re.compile(
    r"[\[\(](laughter|applause|cough|sneeze|breath|noise|music|"
    r"笑|笑声|掌声|咳嗽|喷嚏|呼吸|噪音|音乐)[\]\)]", re.I)
# 常见 emoji / 象形符号区段：SenseVoice 情感标签偶有渲染成 emoji 的情况
RE_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]")


def log(msg: str) -> None:
    """进度/诊断通道。`--quiet` 会把它整体关掉。"""
    print(msg, file=sys.stderr, flush=True)


def err(msg: str) -> None:
    """错误通道：**不受 `--quiet` 影响**。

    失败必须留下原因，否则父进程只看到一个退出码（"rc=4: " 后面什么都没有），
    排查时等于没有信息 —— 与 `daily_scan.py` 那次"日志撒谎"是同一类问题。
    """
    print(msg, file=sys.stderr, flush=True)


def clean_text(text: str) -> str:
    """剥标签/事件符号/emoji，规整空白。"""
    if not text:
        return ""
    t = RE_SV_TAG.sub("", text)
    t = RE_BRACKET_EVENT.sub("", t)
    t = RE_EMOJI.sub("", t)
    t = t.replace("\u200b", "")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\s*\n\s*", "\n", t)
    return t.strip()


def first_existing(cands, kind: str) -> str:
    for c in cands:
        if c and os.path.exists(c):
            log("[sensevoice] %s: %s" % (kind, c))
            return c
    return ""


def find_model_dir(explicit: str | None) -> str:
    if explicit:
        return explicit if os.path.exists(explicit) else ""
    return first_existing(MODEL_DIR_CANDIDATES, "模型目录")


def find_vad_model(explicit: str | None) -> str:
    if explicit:
        return explicit if os.path.exists(explicit) else ""
    return first_existing(VAD_CANDIDATES, "VAD 模型")


def _runs(exe: str) -> bool:
    """该可执行文件能否真的跑起来。不用管道（R22：Windows 上管道收尾会卡死）。"""
    try:
        r = subprocess.run([exe, "-version"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def find_ffmpeg(explicit: str | None) -> str:
    """找到**能跑**的 ffmpeg。

    **技能自带优先**（`<技能>/bin/`，见 `fetch_ffmpeg.py`）。

    2026-10-15 两处修正：
    ① 原实现只查 PATH + 旧的四层布局，且不验证可用性 —— 本机 PATH 上没有 ffmpeg，
       于是识别在"解码音频"这一步才失败，报错只说"找不到 ffmpeg: ffmpeg"；
    ② 删掉了去 AionUi/DSH 会话目录里借用别人 ffmpeg.exe 的逻辑 ——
       那是运行时依赖别人的安装，对方一删/一搬就断。
    现在覆盖技能自带 + 系统安装，并逐个验证能执行；父进程也会用 --ffmpeg 显式传入。
    """
    if explicit:
        return explicit
    cands = [
        os.environ.get("SENSEVOICE_FFMPEG", ""),
        os.environ.get("FFMPEG", ""),
        # 技能自带（首选）
        str(SKILL_ROOT / "bin" / "ffmpeg.exe"),
        str(SKILL_ROOT / "ffmpeg.exe"),
        str(SCRIPT_DIR / "ffmpeg.exe"),
        # 旧约定：BASE_DIR/ffmpeg.exe（脚本往上四层）
        str(SCRIPT_DIR.parent.parent.parent.parent / "ffmpeg.exe"),
        # 系统级安装（用户自己装的才走这里）
        str(Path.home() / "scoop" / "shims" / "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        shutil.which("ffmpeg") or "",
    ]
    for c in cands:
        if c and (shutil.which(c) or os.path.isfile(c)) and _runs(c):
            return c
    return "ffmpeg"


def decode_to_wav(ffmpeg: str, src: str, dst: str, timeout: int = 1800) -> None:
    """ffmpeg → 16kHz / mono / s16le WAV。无管道（R22：Windows 上管道会卡死收尾）。"""
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-i", src, "-vn", "-ac", "1", "-ar", "16000",
           "-acodec", "pcm_s16le", "-f", "wav", dst]
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
        raise RuntimeError("ffmpeg 解码超时 (%ss): %s" % (timeout, os.path.basename(src)))
    if p.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 1024:
        raise RuntimeError("ffmpeg 解码失败 rc=%s: %s" % (p.returncode, os.path.basename(src)))


def read_wav_float32(path: str):
    """标准库读 16-bit PCM WAV → (np.float32 [-1,1], sample_rate)。

    注意：sherpa-onnx 1.13.8 **没有** `sherpa_onnx.read_wave`（实测 hasattr=False），
    网上示例里的这个调用在本版本会直接 AttributeError。
    """
    import numpy as np
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        nch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise RuntimeError("只支持 16-bit PCM WAV，得到 sampwidth=%d" % sw)
    a = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    if nch > 1:
        a = a.reshape(-1, nch).mean(axis=1)
    return a, sr


# ---------------------------------------------------------------- 切分

def segments_via_vad(vad, samples, window: int):
    """用 silero VAD 流式切分，返回 [(start, end), ...]（样本下标）。"""
    step = max(window, 512)
    for i in range(0, len(samples), step):
        vad.accept_waveform(samples[i:i + step])
    vad.flush()
    segs = []
    while not vad.empty():
        seg = vad.front
        start = int(seg.start)
        segs.append((start, start + len(seg.samples)))
        vad.pop()
    return segs


def segments_via_energy(samples, sr: int, target_sec: float = 20.0,
                        min_silence: float = 0.30, energy_db: float = -35.0):
    """无 VAD 时的兜底：逐帧能量 + 静音游程，切口取静音中心。"""
    import numpy as np
    total = len(samples)
    if total == 0:
        return []
    frame_ms = 20
    frame = max(1, int(sr * frame_ms / 1000))
    n = total // frame
    if n == 0 or total <= target_sec * sr:
        return [(0, total)]
    frames = samples[:n * frame].reshape(n, frame).astype("float64")
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
    mask = (20.0 * np.log10(rms + 1e-12)) > energy_db

    min_run = max(1, int(min_silence * 1000 / frame_ms))
    cands = []
    i = 0
    while i < n:
        if not mask[i]:
            j = i
            while j < n and not mask[j]:
                j += 1
            if (j - i) >= min_run:
                cands.append((i + j) // 2)
            i = j
        else:
            i += 1

    target_frames = max(1, int(target_sec * 1000 / frame_ms))
    segs, start = [], 0
    while True:
        if n - start <= target_frames:
            segs.append((start * frame, total))
            break
        ideal = start + target_frames
        cut = None
        for c in cands:
            if start < c <= ideal:
                cut = c
            elif c > ideal:
                break
        if cut is None:
            cut = ideal
        segs.append((start * frame, min(cut * frame, total)))
        start = cut
    merged = []
    for s, e in segs:
        if merged and (e - s) < int(0.5 * sr):
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return merged


def pad_segments(segs, total: int, sr: int, pad_sec: float):
    """给每段前后补一点静音余量，并用相邻边界夹紧（绝不重叠）。

    为什么必须补：实测 VAD 的 `SpeechSegment.start` 已经被削到"确认是语音"的位置，
    起始音节会被吃掉 —— 同一段音频 Whisper 转出「没钱人就爹妈带着玩」，
    VAD 切片后 SenseVoice 只得到「些人就爹妈带着玩」，**开头两个字永久丢失**。
    因为 VAD 两段之间至少有 min_silence_duration(0.5s) 的静音，
    前后各补 0.25s 仍在静音内，不会把邻段的字带进来。
    """
    pad = int(pad_sec * sr)
    if pad <= 0 or not segs:
        return segs
    bounds = [(max(0, s - pad), min(total, e + pad)) for s, e in segs]
    for i in range(len(bounds) - 1):
        if bounds[i][1] > bounds[i + 1][0]:
            bounds[i] = (bounds[i][0], bounds[i + 1][0])
    return [(s, e) for s, e in bounds if e > s]


def _quietest_sample(samples, lo: int, hi: int, sr: int, frame_ms: int = 20) -> int:
    """在 [lo, hi) 里找能量最低的一帧，返回其中心样本下标（用于切口）。"""
    import numpy as np
    frame = max(1, int(sr * frame_ms / 1000))
    lo = max(0, lo)
    hi = min(len(samples), hi)
    if hi - lo < frame * 2:
        return (lo + hi) // 2
    n = (hi - lo) // frame
    frames = samples[lo:lo + n * frame].reshape(n, frame).astype("float64")
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
    k = int(np.argmin(rms))
    return lo + k * frame + frame // 2


def enforce_max_segment(segs, samples, sr: int, cap_sec: float):
    """强制单段不超过 cap_sec。

    为什么必须自己做：实测 sherpa-onnx 1.13.8 的 `silero_vad.max_speech_duration`
    **并不会**把一段连续语音硬切开 —— 38s 的连续人声仍然只返回 1 段。
    而 SenseVoice 是 ~30s 短窗非流式模型，超长输入会被静默截断（正文莫名丢内容，
    却完全看不出错）。所以在这里兜住：超限的段在"最安静的一帧"处再切。
    """
    cap = int(cap_sec * sr)
    if cap <= 0:
        return segs
    out = []
    for s, e in segs:
        cur = s
        while (e - cur) > cap:
            hard = cur + cap
            lo = cur + cap // 2          # 只在后半段找切口，避免切出碎片
            cut = _quietest_sample(samples, lo, hard, sr)
            if cut <= cur or cut >= e:
                cut = hard
            out.append((cur, cut))
            cur = cut
        if e > cur:
            out.append((cur, e))
    return out


def segment_stats(segs, sr: int) -> dict:
    if not segs:
        return {"n": 0, "min_s": 0, "med_s": 0, "max_s": 0}
    d = sorted((e - s) / sr for s, e in segs)
    return {"n": len(d), "min_s": round(d[0], 1),
            "med_s": round(d[len(d) // 2], 1), "max_s": round(d[-1], 1)}


# ---------------------------------------------------------------- 增益

def rms_dbfs(samples) -> float:
    """整体 RMS 电平（dBFS）。静音返回 -120，避免 log(0)。"""
    import numpy as np
    if not len(samples):
        return -120.0
    rms = float(np.sqrt(np.mean(samples.astype("float64") ** 2)))
    # 下限钳到 -120 dBFS：数字静音在数学上是 -inf，直接返回 -inf 会让
    # 日志和 JSON 都写不出来，也会让下游的算术变成 nan。
    return max(-120.0, 20.0 * math.log10(max(rms, 1e-9)))


def normalize_gain(samples, target_db: float = -18.0, peak_ceiling: float = 0.98):
    """把整段音频的 RMS 拉到 target_db，同时按峰值限幅，返回 (samples, info)。

    为什么必须做（2026-10 实测，一次 29 讲的课程录音）：

    ① **没有 VAD、按固定窗口切块**时，录轻的音轨会被整句丢弃：
       同一段 20 分钟素材，固定 15s 分块

           原样       80 块  63 非空  17 空  1626 字   81 字/分
           归一化后   80 块  80 非空   0 空  4159 字  208 字/分

       整讲（99 分钟）归一化后空块从 78 降到 1，正文从 8,925 字涨到 18,933 字。

    ② **但带 VAD 的本技能管线上，主要瓶颈不是电平而是 VAD 门限**。
       同一段 20 分钟音频（−27.7 dBFS）：

           VAD  切分 + 不归一 →  36 段 /  626 字 /  31 字/分
           VAD  切分 + 归一   →  94 段 / 1513 字 /  76 字/分
           能量 切分 + 不归一 →  67 段 / 4366 字 / 218 字/分   ← 只换切分就够了
           能量 切分 + 归一   →  74 段 / 4337 字 / 217 字/分

       归一化把 VAD 从"几乎全聋"抬到"半聋"，而换用能量切分直接回到正常密度
       （见 transcribe_one 里的自动抢救与 R43）。

    ③ 电平本来健康的音轨几乎不受影响（实测 −26 dBFS 的 3 分钟切片
       163 → 158 字/分，在噪声范围内）。

    结论：归一化是**便宜且几乎无害的第一步**，值得无条件做；但它不是低电平
    问题的全部答案，别把它当成"做了就没事了"。真正的杠杆在切分。

    第二个作用：让 `--energy-db` 这个**绝对**阈值重新有意义。无 VAD 的兜底切分
    按 dBFS 绝对值判静音，录音偏轻时会把正常语音也判成静音（gain 归一后阈值
    才对应它声称的 -35 dBFS）。所以归一化必须在切分**之前**做。
    """
    import numpy as np
    x = samples.astype("float64")
    before = rms_dbfs(x)
    peak = float(np.abs(x).max()) if len(x) else 0.0
    if peak <= 0.0:
        return samples, {"gain": 1.0, "before_db": before, "after_db": before,
                         "peak": 0.0, "clipped": False}
    goal = 10.0 ** (target_db / 20.0)
    rms = float(np.sqrt(np.mean(x ** 2))) or 1e-9
    gain = goal / rms
    # 限幅：宁可少提一点增益，也不要把波峰削平（削波会制造新的识别错误）
    limit = peak_ceiling / peak
    clipped = gain > limit
    gain = min(gain, limit)
    out = np.clip(x * gain, -1.0, 1.0).astype("float32")
    return out, {"gain": round(gain, 4), "before_db": round(before, 1),
                 "after_db": round(rms_dbfs(out), 1),
                 "peak": round(peak, 4), "clipped": clipped}


# ---------------------------------------------------------------- 引擎

class Engine:
    """模型只加载一次（R8 的同源纪律：贵的东西不要每条都重来）。"""

    def __init__(self, model_dir: str, vad_model: str, threads: int, use_itn: bool,
                 language: str, segment_seconds: float, vad_chunk: int):
        import sherpa_onnx
        md = Path(model_dir)
        model = md / "model.int8.onnx"
        tokens = md / "tokens.txt"
        if not model.exists():
            alt = md / "model.onnx"          # 只放了 fp32 权重的环境
            if alt.exists():
                model = alt
            else:
                raise FileNotFoundError("找不到模型权重: %s" % model)
        if not tokens.exists():
            raise FileNotFoundError("找不到 tokens.txt: %s" % tokens)

        t0 = time.time()
        self.rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model), tokens=str(tokens),
            num_threads=threads, use_itn=use_itn, language=language, debug=False,
        )
        log("[sensevoice] 识别模型加载 %.1fs  (%s, threads=%d, itn=%s, lang=%s)"
            % (time.time() - t0, model.name, threads, use_itn, language))

        self.vad = None
        self.vad_window = 512
        if vad_model:
            try:
                self.vad_cfg = self._make_vad_config(sherpa_onnx, vad_model,
                                                     segment_seconds, threads)
                self.vad_window = int(self.vad_cfg.silero_vad.window_size)
                self.vad_chunk = max(self.vad_window, int(vad_chunk or self.vad_window * 16))
                self.vad = self._new_vad(sherpa_onnx)   # 构造一次以尽早暴露配置错误
                log("[sensevoice] VAD 就绪（silero, 段上限 %.0fs, chunk=%d）"
                    % (segment_seconds, self.vad_chunk))
            except Exception as e:
                err("[sensevoice] VAD 不可用，退化为能量切分: %s: %s"
                    % (type(e).__name__, e))
                self.vad = None

    @staticmethod
    def _make_vad_config(sherpa_onnx, vad_model: str, segment_seconds: float, threads: int):
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = vad_model
        cfg.silero_vad.threshold = 0.5
        cfg.silero_vad.min_silence_duration = 0.5
        cfg.silero_vad.min_speech_duration = 0.25
        cfg.silero_vad.max_speech_duration = float(segment_seconds)
        cfg.silero_vad.window_size = 512
        cfg.sample_rate = 16000
        cfg.num_threads = threads
        return cfg

    def _new_vad(self, sherpa_onnx, seconds: float = 0.0):
        """VAD 实例内部有状态 —— 每个文件都必须用全新的一个。

        `buffer_size_in_seconds` 必须**按音频长度给足**。给固定 60s 时，
        对"几乎没有静音"的讲话（独夫之心的视频基本如此）VAD 会把整段攒在环形缓冲里，
        原生层于是自己扩容并往 stderr 打 `circular-buffer.cc: Overflow! ...`——
        它写着 "No data loss!"，但那是**原生层绕过 Python 日志**的输出，
        会把 `--quiet` 的干净 stderr 弄脏，而且扩容是"先复制再翻倍"，代价不可控。
        这里按长度一次性给足（上限 2 小时），既没有告警也没有隐式扩容。
        内存提醒：float32 每样本 4 字节，1 小时音频的缓冲约 230MB。
        """
        buf = int(min(max(60.0, float(seconds) + 10.0), 7200.0))
        return sherpa_onnx.VoiceActivityDetector(self.vad_cfg, buffer_size_in_seconds=buf)

    def split(self, samples, sr: int, force_energy: bool = False):
        if force_energy:
            return segments_via_energy(samples, sr), "energy"
        if self.vad is not None:
            import sherpa_onnx
            vad = self._new_vad(sherpa_onnx, len(samples) / float(sr or 16000))
            total = len(samples)
            for i in range(0, total, self.vad_chunk):
                vad.accept_waveform(samples[i:i + self.vad_chunk])
            vad.flush()
            segs = []
            while not vad.empty():
                seg = vad.front
                start = int(seg.start)
                end = min(start + len(seg.samples), total)
                if end > start:
                    segs.append((start, end))
                vad.pop()
            if segs:
                return segs, "vad"
            err("[sensevoice] VAD 未检出语音段，退化为能量切分")
        return segments_via_energy(samples, sr), "energy"

    def split_capped(self, samples, sr: int, cap_sec: float, pad_sec: float,
                     force_energy: bool = False):
        """切分 →（VAD 时）补边距 → 强制单段上限；返回 (segs, 来源, 统计)。"""
        segs, how = self.split(samples, sr, force_energy=force_energy)
        if how == "vad":
            segs = pad_segments(segs, len(samples), sr, pad_sec)
        before = len(segs)
        segs = enforce_max_segment(segs, samples, sr, cap_sec)
        if len(segs) != before:
            log("[sensevoice] 长段强制切分: %d -> %d 段（上限 %.0fs）"
                % (before, len(segs), cap_sec))
        return segs, how, segment_stats(segs, sr)

    def transcribe(self, samples, sr: int, seg) -> str:
        s = self.rec.create_stream()
        s.accept_waveform(sr, samples[seg[0]:seg[1]])
        self.rec.decode_stream(s)
        return s.result.text or ""


def _transcribe_segments(engine, samples, sr, segs, args, quiet: bool = False):
    """逐段识别，返回 (parts, raws, 空段数, 说话人标签样本)。

    说话人标签留证用：语音转写平台给的分轨常常是错的（同一个人会在半句中间换编号），
    留着是为了事后能证明"错在哪一层"，不是为了使用。
    """
    parts, raws, speaker_hits = [], [], []
    empty = 0
    for k, seg in enumerate(segs):
        t = engine.transcribe(samples, sr, seg)
        if args.debug_raw:
            raws.append(t)
        c = clean_text(t)
        if c:
            parts.append(c)
        else:
            empty += 1
        if args.debug_raw and re.search(r"说话人|speaker", t):
            speaker_hits.append(t[:80])
        if not quiet and (k + 1) % 20 == 0:
            log("[sensevoice]   已识别 %d/%d 段" % (k + 1, len(segs)))
    return parts, raws, empty, speaker_hits


def transcribe_one(engine: Engine, ffmpeg: str, src: str, tmpdir: str,
                   args) -> dict:
    if not os.path.isfile(src):
        raise FileNotFoundError(src)
    wav = os.path.join(tmpdir, os.path.splitext(os.path.basename(src))[0] + ".16k.wav")
    t0 = time.time()
    decode_to_wav(ffmpeg, src, wav)
    samples, sr = read_wav_float32(wav)
    dur = len(samples) / sr if sr else 0.0

    # 增益归一化必须在切分之前：既决定模型会不会整句丢字，也决定
    # --energy-db 这个绝对阈值是否对得上它声称的电平（见 normalize_gain）。
    ginfo = None
    if not getattr(args, "no_normalize", False):
        target = getattr(args, "target_db", -18.0)
        samples, ginfo = normalize_gain(samples, target_db=target)
        log("[sensevoice] %s  增益 x%.2f (%.1f → %.1f dBFS)%s"
            % (os.path.basename(src), ginfo["gain"], ginfo["before_db"],
               ginfo["after_db"], "  [峰值限幅]" if ginfo["clipped"] else ""))

    segs, how, sstat = engine.split_capped(samples, sr, args.segment_seconds,
                                          args.pad_seconds)
    log("[sensevoice] %s  时长 %.1fs  切分=%s  段数=%d (最短%.1fs/中位%.1fs/最长%.1fs)"
        % (os.path.basename(src), dur, how, sstat["n"],
           sstat["min_s"], sstat["med_s"], sstat["max_s"]))

    parts, raws, empty, speaker_hits = _transcribe_segments(
        engine, samples, sr, segs, args)

    text = clean_text("".join(parts))
    mins = dur / 60.0 if dur else 0.0
    cpm = round(len(text) / mins, 1) if mins else 0.0

    # ---- VAD 漏检的自动抢救 ----------------------------------------------
    # 实测（2026-10，一段 −27.7 dBFS 的讲座录音，20 分钟）：
    #     VAD  切分 + 不归一 →  36 段 /  626 字 /  31 字/分
    #     VAD  切分 + 归一   →  94 段 / 1513 字 /  76 字/分
    #     能量 切分 + 不归一 →  67 段 / 4366 字 / 218 字/分   ← 只换切分就够了
    #     能量 切分 + 归一   →  74 段 / 4337 字 / 217 字/分
    # 结论：**低电平时真正拖后腿的是 silero VAD 的门限，不是识别模型。**
    # 归一化只能把 VAD 从"几乎全聋"抬到"半聋"，换成能量切分则直接回到正常密度。
    # 所以不指望用户调参：检测到"段数异常少"，就自动用能量切分重跑一遍，取字多者。
    retried = None
    if (how == "vad" and dur > 120 and mins
            and (len(segs) < 4.0 * mins or cpm < 120)
            and not getattr(args, "no_auto_energy", False)):
        err("[sensevoice] 警告：VAD 只切出 %d 段（%.1f 段/分钟）、密度 %.0f 字/分，"
            "疑似低电平漏检；改用能量切分重跑 …"
            % (len(segs), len(segs) / mins, cpm))
        segs2, how2, sstat2 = engine.split_capped(
            samples, sr, args.segment_seconds, args.pad_seconds, force_energy=True)
        parts2, raws2, empty2, hits2 = _transcribe_segments(
            engine, samples, sr, segs2, args, quiet=True)
        text2 = clean_text("".join(parts2))
        cpm2 = round(len(text2) / mins, 1) if mins else 0.0
        retried = {"split": how2, "segments": len(segs2),
                   "chars": len(text2), "chars_per_min": cpm2}
        if len(text2) > len(text):
            log("[sensevoice]   采用能量切分：%d → %d 字（%.0f → %.0f 字/分）"
                % (len(text), len(text2), cpm, cpm2))
            segs, how, sstat = segs2, how2, sstat2
            parts, raws, empty, speaker_hits = parts2, raws2, empty2, hits2
            text, cpm = text2, cpm2
        else:
            log("[sensevoice]   能量切分没有更好（%d 字），保留 VAD 结果" % len(text2))

    # 密度是"模型有没有整句丢字"的唯一廉价指示：中文讲课正常 190–260 字/分。
    # 短视频必须豁免（与 R34 的缓存判定同一个 120s 阈值）：片头/通知/纯音乐
    # 本来就只该出几个字，拿密度去要求它是误报（R33 已记过同一类教训）。
    if dur > 120 and segs and empty > len(segs) * 0.15:
        err("[sensevoice] 警告：%s 有 %d/%d 段识别为空（%.0f%%），"
            "正文密度仅 %.0f 字/分。可能是音频偏轻/噪声。"
            % (os.path.basename(src), empty, len(segs),
               100.0 * empty / len(segs), cpm))
    elif dur > 120 and cpm < 120:
        err("[sensevoice] 警告：%s 正文密度仅 %.0f 字/分（中文讲课正常 190–260），"
            "请核对音频质量（R38）" % (os.path.basename(src), cpm))
    if not args.keep_temp:
        try:
            os.remove(wav)
        except OSError:
            pass
    return {
        "file": src,
        "seconds": round(dur, 2),
        "segments": len(segs),
        "empty_segments": empty,
        "chars_per_min": cpm,
        "gain": ginfo,
        "auto_energy_retry": retried,
        "speaker_tags_seen": speaker_hits,
        "split": how,
        "seg_stats": sstat,
        "chars": len(text),
        "elapsed": round(time.time() - t0, 2),
        "text": text,
        "raw": raws,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="本地 SenseVoice 转录（sherpa-onnx, CPU）")
    ap.add_argument("files", nargs="+", help="音频文件（m4a/wav/mp3/flac 均可）")
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--vad-model", default=None)
    ap.add_argument("--ffmpeg", default=None)
    ap.add_argument("--language", default="zh", help="zh / auto / en / ja / ko / yue")
    ap.add_argument("--threads", type=int, default=max(2, (os.cpu_count() or 4) // 2))
    ap.add_argument("--no-itn", action="store_true", help="关闭逆文本归一化")
    ap.add_argument("--segment-seconds", type=float, default=20.0,
                    help="单段上限（VAD 的 max_speech_duration）")
    ap.add_argument("--pad-seconds", type=float, default=0.25,
                    help="VAD 段前后补的静音余量，找回被削掉的起始音节")
    ap.add_argument("--vad-chunk", type=int, default=0, help="喂给 VAD 的分块大小，0=自动")
    ap.add_argument("--energy-db", type=float, default=-35.0)
    ap.add_argument("--target-db", type=float, default=-18.0,
                    help="增益归一的 RMS 目标电平（dBFS），默认 -18")
    ap.add_argument("--no-normalize", action="store_true",
                    help="关闭增益归一（默认开启，几乎总是应该让它开着）")
    ap.add_argument("--no-auto-energy", action="store_true",
                    help="关闭\"VAD 漏检时自动改用能量切分\"的抢救（默认开启）")
    ap.add_argument("--json-out", default=None, help="结果（含计时/raw）写 JSON")
    ap.add_argument("--keep-temp", action="store_true")
    ap.add_argument("--debug-raw", action="store_true",
                    help="把未清洗的原始输出写进 JSON 的 raw 字段（查标签用）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.quiet:
        global log
        log = lambda *a, **k: None  # noqa: E731

    for f in args.files:
        if not os.path.isfile(f):
            err("[sensevoice] 输入不存在: %s" % f)
            return 3

    ffmpeg = find_ffmpeg(args.ffmpeg)
    if not (shutil.which(ffmpeg) or os.path.isfile(ffmpeg)):
        err("[sensevoice] 找不到 ffmpeg: %s" % ffmpeg)
        return 4

    model_dir = find_model_dir(args.model_dir)
    if not model_dir:
        err("[sensevoice] 未找到模型目录。查找过：")
        for c in MODEL_DIR_CANDIDATES:
            err("    %s" % (c or "(env 未设置)"))
        err("            可用 scripts/fetch_sensevoice_model.py 重建")
        return 2
    vad_model = find_vad_model(args.vad_model)

    try:
        engine = Engine(model_dir, vad_model, args.threads, not args.no_itn,
                        args.language, args.segment_seconds, args.vad_chunk)
    except FileNotFoundError as e:
        err("[sensevoice] 模型缺失：%s" % e)
        return 2
    except Exception as e:
        err("[sensevoice] 模型加载失败: %s: %s" % (type(e).__name__, e))
        return 2

    tmpdir = tempfile.mkdtemp(prefix="sensevoice_")
    results = []
    try:
        for f in args.files:
            try:
                results.append(transcribe_one(engine, ffmpeg, f, tmpdir, args))
            except Exception as e:
                err("[sensevoice] 失败 %s: %s: %s"
                    % (os.path.basename(f), type(e).__name__, e))
                return 5
    finally:
        if not args.keep_temp:
            shutil.rmtree(tmpdir, ignore_errors=True)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    texts = [r["text"] for r in results]
    if not any(texts):
        err("[sensevoice] 识别结果为空（音频无语音？）")
        return 5

    sys.stdout.write("\n".join(texts))
    sys.stdout.flush()
    total_s = sum(r["seconds"] for r in results)
    total_e = sum(r["elapsed"] for r in results)
    total_c = sum(r["chars"] for r in results)
    mins = total_s / 60.0 if total_s else 0
    log("[sensevoice] 完成：音频 %.1fs / 用时 %.1fs / RTF %.3f / %d 字 "
        "(%.0f 字/分) / 空段 %d/%d / 切分=%s"
        % (total_s, total_e, (total_e / total_s) if total_s else 0, total_c,
           (total_c / mins) if mins else 0,
           sum(r["empty_segments"] for r in results),
           sum(r["segments"] for r in results), results[0]["split"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())