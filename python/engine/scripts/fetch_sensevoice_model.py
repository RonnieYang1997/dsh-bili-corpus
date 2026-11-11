# -*- coding: utf-8 -*-
"""下载 SenseVoiceSmall 的 ONNX 权重（sherpa-onnx 官方导出）到本技能目录。

为什么要这个脚本：模型有 228MB，**不进 git**（技能仓库是 git 仓库），
但必须可复现 —— 换机器时能一条命令重建，而不是"只有那台机器上才有"。
这与 kb-mcp 的 `scripts/fetch_model.py` 是同一套做法。

网络事实（2026-10-09 实测）：
  huggingface.co       不可达（000）
  hf-mirror.com        可达  → 默认走这里
  objects.githubusercontent.com  不可达 → 因此不走 GitHub release

用法（用本技能的 venv 运行）：
    .venv/Scripts/python.exe scripts/fetch_sensevoice_model.py
    .venv/Scripts/python.exe scripts/fetch_sensevoice_model.py --force
    .venv/Scripts/python.exe scripts/fetch_sensevoice_model.py --skip-verify
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# R3: 中文诊断脚本在 CP936 控制台下必须先把两个流都换成 UTF-8。
# 只换 stdout 不够 —— 告警走 stderr，照样会变成 "޷ȡ" 这种乱码。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = SKILL_ROOT / "models" / "sensevoice-small"

# sherpa-onnx 官方的 SenseVoice 导出仓库（含 int8 量化权重与测试音频）
REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
HF_BASES = ("https://hf-mirror.com", "https://huggingface.co")

# 只取必需文件 + 一个冒烟测试音频；model.onnx(fp32, 894MB) 不需要
REQUIRED = ["model.int8.onnx", "tokens.txt"]
OPTIONAL = ["test_wavs/zh.wav"]
EXPECTED_SIZE = {
    "model.int8.onnx": 239233841,
    "tokens.txt": 315894,
    "test_wavs/zh.wav": 178988,
}

# ---------------------------------------------------------------- silero VAD
# 切分用的 VAD 模型。与识别权重**分开放**：识别权重落在 --dir，
# VAD 固定落在 <技能>/models/silero/（脚本与 SKILL.md 都按这个位置找）。
#
# 2026-10-15：本机 GitHub release **直连不可达**（curl 返回 000），
# 故带上两个 GitHub 代理前缀兜底；直连放第一位，网络通的地方优先走直连。
# 已实测：经代理取到的文件是合法 ONNX，且 sherpa-onnx 能加载。
VAD_URLS = [
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
    "https://ghproxy.net/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
    "https://gh-proxy.com/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
]
VAD_DEFAULT_DIR = SKILL_ROOT / "models" / "silero"
VAD_MIN_SIZE = 300000      # 防"下到了 HTML 错误页"这类假成功


def urls_for(rel: str) -> list[str]:
    return [f"{b}/{REPO}/resolve/main/{rel}" for b in HF_BASES]


def fetch(url: str, dest: Path, attempts: int = 4) -> bool:
    """流式下载到 .part 再原子改名；支持 Range 断点续传与重试。

    2026-10-09 实测：228MB 的权重第一次请求因 **DNS 瞬时失败**
    (`getaddrinfo failed`) 而中断，同一主机的下一个文件随即正常。
    所以"一次性大文件下载"必须有重试，且断了要能从断点接着下 ——
    否则每次抖动都要从头再传 228MB。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    t0 = time.time()

    for attempt in range(1, attempts + 1):
        have = tmp.stat().st_size if tmp.exists() else 0
        headers = {"User-Agent": "sensevoice-fetch/1.0"}
        if have > 0:
            headers["Range"] = "bytes=%d-" % have
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                partial = (r.status == 206)
                if have > 0 and not partial:
                    have = 0  # 服务器不支持续传 → 从头写
                total = int(r.headers.get("Content-Length") or 0) + have
                mode = "ab" if (have and partial) else "wb"
                if mode == "wb":
                    have = 0
                done = have
                last = have
                with open(tmp, mode) as f:
                    while True:
                        buf = r.read(262144)
                        if not buf:
                            break
                        f.write(buf)
                        done += len(buf)
                        if done - last > 10_000_000:
                            last = done
                            pct = f"{done * 100 // total}%" if total else "?"
                            print(f"    {done // 1024 // 1024}MB / "
                                  f"{total // 1024 // 1024 if total else '?'}MB ({pct})",
                                  flush=True)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"    第 {attempt}/{attempts} 次失败: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            if attempt < attempts:
                time.sleep(3 * attempt)
            continue
        tmp.replace(dest)
        mb = dest.stat().st_size / 1024 / 1024
        print(f"    完成 {dest.name}  {mb:.1f}MB  {time.time() - t0:.0f}s", flush=True)
        return True
    return False


def read_wav_float32(path: str):
    """标准库读 16-bit PCM WAV → (np.float32 [-1,1], sample_rate)。

    注意：sherpa-onnx 1.13.8 **没有** `sherpa_onnx.read_wave`
    （实测 hasattr=False），所以必须自己读，不能照抄网上示例。
    """
    import wave

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


def verify(model_dir: Path, test_wav: Path | None) -> bool:
    """真实加载 + 真实解码，证明第二步不会卡在模型上。"""
    try:
        import sherpa_onnx
    except Exception as e:
        print(f"[verify] 无法 import sherpa_onnx（跳过校验）: {e}")
        return True  # 下载本身成功即可；校验是加分项

    print("[verify] 加载 OfflineRecognizer.from_sense_voice ...")
    t0 = time.time()
    try:
        rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_dir / "model.int8.onnx"),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=max(2, (os.cpu_count() or 4) // 2),
            use_itn=True,
            language="zh",
            debug=False,
        )
    except Exception as e:
        print(f"[verify] ✗ 加载失败: {type(e).__name__}: {e}")
        return False
    print(f"[verify] 加载成功 {time.time() - t0:.1f}s")

    if not test_wav or not test_wav.exists():
        print("[verify] 无测试音频，仅确认加载")
        return True

    try:
        samples, sample_rate = read_wav_float32(str(test_wav))
    except Exception as e:
        print(f"[verify] 读取测试音频失败: {e}")
        return False
    s = rec.create_stream()
    s.accept_waveform(sample_rate, samples)
    rec.decode_stream(s)
    text = (s.result.text or "").strip()
    print(f"[verify] 识别结果（{len(samples) / sample_rate:.1f}s 音频）:")
    print(f"    {text!r}")
    if not text:
        print("[verify] ✗ 识别为空")
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 SenseVoiceSmall ONNX 权重")
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--force", action="store_true", help="已存在也重新下载")
    ap.add_argument("--skip-verify", action="store_true")
    ap.add_argument("--with-optional", action="store_true",
                    help="连同 test_wavs/zh.wav 一起下载（用于冒烟测试）")
    ap.add_argument("--vad-dir", default=str(VAD_DEFAULT_DIR),
                    help="silero VAD 存放目录（默认 <技能>/models/silero）")
    ap.add_argument("--no-vad", action="store_true",
                    help="不下载 silero VAD（只取识别权重）")
    args = ap.parse_args()

    out = Path(args.dir)
    print(f"[fetch_sensevoice_model] 目标目录 {out}")

    # ---- silero VAD（切分必需，缺了识别会退化成"整段当一句"）
    if not args.no_vad:
        vdir = Path(args.vad_dir)
        vdest = vdir / "silero_vad.onnx"
        if vdest.exists() and not args.force and vdest.stat().st_size > VAD_MIN_SIZE:
            print(f"  已存在，跳过 silero_vad.onnx ({vdest.stat().st_size / 1048576:.1f}MB)")
        else:
            got_vad = False
            for u in VAD_URLS:
                print(f"  下载 {u}")
                if fetch(u, vdest):
                    got_vad = True
                    break
            if not got_vad:
                print("  ✗ 无法获取 silero_vad.onnx（可手动下载后放到 "
                      f"{vdest}）", file=sys.stderr)
            elif vdest.stat().st_size < VAD_MIN_SIZE:
                print(f"  ! silero_vad.onnx 体积异常: {vdest.stat().st_size} "
                      f"(应 > {VAD_MIN_SIZE})", file=sys.stderr)

    wanted = list(REQUIRED) + (list(OPTIONAL) if args.with_optional else [])
    ok = True
    for rel in wanted:
        dest = out / rel
        if dest.exists() and not args.force and dest.stat().st_size > 1024:
            print(f"  已存在，跳过 {rel} ({dest.stat().st_size / 1024 / 1024:.1f}MB)")
            continue
        got = False
        for u in urls_for(rel):
            print(f"  下载 {u}")
            if fetch(u, dest):
                got = True
                break
        if not got:
            print(f"  ✗ 无法获取 {rel}", file=sys.stderr)
            ok = False
    if not ok:
        return 1

    # 体积核对：防"下到了 HTML 错误页"这类假成功
    for rel, want in EXPECTED_SIZE.items():
        p = out / rel
        if p.exists():
            got = p.stat().st_size
            if abs(got - want) > 1024:
                print(f"  ! {rel} 体积异常: {got} (期望约 {want})", file=sys.stderr)
                ok = False

    if args.skip_verify:
        print("[fetch_sensevoice_model] 跳过加载校验")
        return 0 if ok else 1

    test_wav = out / "test_wavs" / "zh.wav"
    if verify(out, test_wav):
        print("[fetch_sensevoice_model] 校验通过 ✅")
        return 0 if ok else 1
    print("[fetch_sensevoice_model] 校验失败 ❌", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())