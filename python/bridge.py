#!/usr/bin/env python3
"""dsh-bili-corpus · 转写桥（Python → Node host 的适配层）。

为什么不直接 `python scripts/bilibili_transcribe.py <bvid>`：
  那个入口只认 sys.argv[1]，输出目录写死成「当前工作目录/memory」、临时目录写死成
  「当前工作目录/_transcribe_temp」。插件里需要能指定输出目录、需要结构化进度、
  需要把异常（充电专属、API 错误体）翻译成错误码 —— 所以在这里 import 它，
  调 process_video(bvid, output_dir=..., temp_dir=...)，把它自己的阶段函数包一层，
  每进一个阶段就往 stdout 打一行 JSON（Node 侧逐行读）。

用法（Node 侧唯一约定，参数用 JSON 从 stdin 传也行，这里用 argv 更简单）：
  python bridge.py --bvid BV1xx411c7mD --out "<memory 目录>" --temp "<临时目录>" \
                   --lang zh [--force] [--skill-dir "<bilibili-transcribe 技能目录>"]

stdout 协议：每行一个 JSON
  {"type":"stage","stage":"download|asr|save","msg":"..."}
  {"type":"result","ok":true,"bvid":"...","path":"...","duration":123,"textLength":4567,"cached":false}
  {"type":"result","ok":false,"error":"CHARGING|PAYLOAD|ASR|DOWNLOAD|UNKNOWN","message":"..."}
退出码：0 成功；2 业务失败（stdout 里已有 result）；1 环境错误。
"""

import argparse
import json
import os
import sys
import traceback


def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bvid", required=True)
    ap.add_argument("--out", required=True, help="转写产物目录（dufu-<bvid>.md 写这里）")
    ap.add_argument("--temp", default=None, help="音频下载/切片临时目录")
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--force", action="store_true", help="忽略已存在的转写稿，强制重转")
    ap.add_argument("--skill-dir", default=None, help="bilibili-transcribe 技能根目录")
    args = ap.parse_args()

    skill_dir = args.skill_dir or os.environ.get("DSH_BILI_SKILL_DIR") or ""
    scripts_dir = os.path.join(skill_dir, "scripts")
    if not os.path.isdir(scripts_dir):
        emit({"type": "result", "ok": False, "error": "ENV",
              "message": "找不到技能 scripts 目录：%s（用 --skill-dir 或 DSH_BILI_SKILL_DIR 指定）" % scripts_dir})
        return 1
    sys.path.insert(0, scripts_dir)

    try:
        import bilibili_transcribe as bt  # noqa: E402
    except Exception as e:  # 依赖缺失（venv 不在、ffmpeg 不在）时给出可诊断的信息
        emit({"type": "result", "ok": False, "error": "ENV",
              "message": "import bilibili_transcribe 失败：%s" % e})
        traceback.print_exc()
        return 1

    # ── 预检（缺一个就直接失败，免得跑 20 分钟才发现）
    #     ffprobe 缺失时技能会静默降级成 300s 预览音频 → 长视频被截断却"成功"。
    model_dir = os.environ.get("SENSEVOICE_MODEL_DIR", "")
    if model_dir:
        need = [os.path.join(model_dir, "model.int8.onnx"), os.path.join(model_dir, "tokens.txt")]
        missing = [p for p in need if not os.path.isfile(p)]
        if missing:
            emit({"type": "result", "ok": False, "error": "ENV",
                  "message": "SenseVoice 模型缺失：%s（不要自动下载，按 SKILL.md 用 fetch_sensevoice_model.py）" % ", ".join(missing)})
            return 1
    ffmpeg = os.environ.get("SENSEVOICE_FFMPEG", "")
    if ffmpeg:
        if not os.path.isfile(ffmpeg):
            emit({"type": "result", "ok": False, "error": "ENV", "message": "ffmpeg 不存在：%s" % ffmpeg})
            return 1
        bt.FFMPEG = ffmpeg
        ffprobe = ffmpeg.replace("ffmpeg.exe", "ffprobe.exe")
        if not os.path.isfile(ffprobe):
            emit({"type": "result", "ok": False, "error": "ENV",
                  "message": "ffprobe 不存在：%s（缺它会让下载静默降级成 300s 预览）" % ffprobe})
            return 1

    # 给三个阶段函数套进度上报（原样透传参数/返回值）
    def wrap(name, stage, label):
        orig = getattr(bt, name, None)
        if not callable(orig):
            return

        def wrapper(*a, **kw):
            emit({"type": "stage", "stage": stage, "msg": label})
            return orig(*a, **kw)

        setattr(bt, name, wrapper)

    wrap("download_audio", "download", "下载音频（WBI 签名 DASH）")
    wrap("transcribe_long_audio", "asr", "SenseVoice 本地识别")
    wrap("save_transcript", "save", "写 dufu-<bvid>.md")

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    temp_dir = os.path.abspath(args.temp) if args.temp else os.path.join(out_dir, "_transcribe_temp")
    os.makedirs(temp_dir, exist_ok=True)

    # --force：删掉旧稿，让 process_video 重新走全流程（它自己有 <3000 字的缓存判定）
    target = os.path.join(out_dir, "dufu-%s.md" % args.bvid)
    if args.force and os.path.exists(target):
        try:
            os.remove(target)
            emit({"type": "stage", "stage": "reset", "msg": "已删除旧稿，强制重转"})
        except OSError as e:
            emit({"type": "result", "ok": False, "error": "RESET", "message": str(e)})
            return 2

    try:
        result = bt.process_video(args.bvid, output_dir=out_dir, temp_dir=temp_dir)
        path = result.get("path") or target
        # 别信返回值：落盘校验（非原子写 + 空正文都当失败，防止把半截稿当成功台账）
        if not os.path.isfile(path) or os.path.getsize(path) < 64:
            emit({"type": "result", "ok": False, "error": "EMPTY",
                  "message": "转写结束但落盘校验失败：%s（存在=%s 大小=%s）"
                             % (path, os.path.isfile(path), os.path.getsize(path) if os.path.isfile(path) else -1)})
            return 2
        body = result.get("text_length", 0)
        if not body:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            parts = text.split("---")
            if len(parts) >= 3:
                body = len("".join(parts[2:]).strip())
        payload = {
            "type": "result",
            "ok": True,
            "bvid": result.get("bvid", args.bvid),
            "path": path,
            "duration": result.get("duration", 0),
            "textLength": body,
            "cached": bool(result.get("cached")),
        }
        emit(payload)
        return 0
    except Exception as e:
        name = type(e).__name__
        code = "UNKNOWN"
        if "Charging" in name:
            code = "CHARGING"
        elif "WhisperPayload" in name or "Payload" in name:
            code = "PAYLOAD"
        msg = str(e)
        emit({"type": "result", "ok": False, "error": code, "message": msg[:2000]})
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
