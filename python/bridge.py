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
                   --lang zh [--force] [--skill-dir "<外部技能目录，可选>"]

stdout 协议：每行一个 JSON
  {"type":"stage","stage":"engine|download|asr|save","msg":"..."}
  {"type":"result","ok":true,"bvid":"...","path":"...","duration":123,"textLength":4567,"cached":false}
  {"type":"result","ok":false,"error":"CHARGING|PAYLOAD|ASR|DOWNLOAD|UNKNOWN","message":"..."}
退出码：0 成功；2 业务失败（stdout 里已有 result）；1 环境错误。
"""

import argparse
import json
import os
import shutil
import sys
import traceback


def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bvid", required=True)
    ap.add_argument("--out", default=os.getcwd(), help="转写产物目录（默认当前目录）；--propose 用不到")
    ap.add_argument("--temp", default=None, help="音频下载/切片临时目录")
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--force", action="store_true", help="忽略已存在的转写稿，强制重转")
    ap.add_argument("--skill-dir", default=None, help="bilibili-transcribe 技能根目录")
    ap.add_argument("--name", default=None, help="产物文件名（含 .md）；缺省 dufu-<bvid>.md")
    ap.add_argument("--propose", action="store_true", help="只取视频元数据（标题/时长），不下载不转写")
    args = ap.parse_args()

    # ── 引擎解析（P1：内置引擎优先，外部技能目录只作覆盖/兜底）────────────
    # 内置：<插件>/python/engine/scripts，随插件分发，不依赖 AionUi 会话临时目录。
    # 顺序：DSH_BILI_ENGINE_DIR（显式）→ 内置 → --skill-dir/DSH_BILI_SKILL_DIR。
    here = os.path.dirname(os.path.abspath(__file__))
    builtin_scripts = os.path.join(here, "engine", "scripts")
    explicit_engine = os.environ.get("DSH_BILI_ENGINE_DIR", "")
    skill_dir = args.skill_dir or os.environ.get("DSH_BILI_SKILL_DIR") or ""
    external_scripts = os.path.join(skill_dir, "scripts") if skill_dir else ""

    scripts_dir, engine_kind = "", "missing"
    for cand, kind in ((explicit_engine, "external"), (builtin_scripts, "builtin"), (external_scripts, "external")):
        if cand and os.path.isfile(os.path.join(cand, "bilibili_transcribe.py")):
            scripts_dir, engine_kind = cand, kind
            break
    if not scripts_dir:
        emit({"type": "result", "ok": False, "error": "ENV",
              "message": "找不到转写引擎：内置 %s 不存在，也没提供 --skill-dir / DSH_BILI_ENGINE_DIR" % builtin_scripts})
        return 1

    # 内置引擎不带 venv：识别子进程用哪个解释器由这里显式指定（缺省 = 当前解释器）。
    os.environ.setdefault("SENSEVOICE_PYTHON", sys.executable)
    emit({"type": "stage", "stage": "engine", "msg": "引擎：%s（%s）" % (engine_kind, scripts_dir)})
    sys.path.insert(0, scripts_dir)

    try:
        import bilibili_transcribe as bt  # noqa: E402
    except Exception as e:  # 依赖缺失（venv 不在、ffmpeg 不在）时给出可诊断的信息
        emit({"type": "result", "ok": False, "error": "ENV",
              "message": "import bilibili_transcribe 失败（%s）：%s" % (scripts_dir, e)})
        traceback.print_exc()
        return 1

    # P3：--propose 只问元数据（给「标题式命名」确认用），不碰模型/ffmpeg/音频。
    if args.propose:
        try:
            info = bt.get_video_info(args.bvid)
            emit({"type": "result", "ok": True, "propose": True, "bvid": args.bvid,
                  "title": info.get("title", ""), "date": info.get("date", ""),
                  "duration": info.get("duration_sec", 0), "engine": engine_kind})
            return 0
        except Exception as e:
            emit({"type": "result", "ok": False, "error": "META", "message": str(e)[:500]})
            return 2

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
    # ffmpeg 解析顺序：env（插件注入）→ ~/.dsh/speech-to-text/ffmpeg（本机已备）→ 插件 engine/bin → PATH。
    # 不再相对「技能目录」上溯——那个会话目录随时会被清掉。
    def resolve_ffmpeg():
        home = os.path.expanduser("~")
        for c in (os.environ.get("SENSEVOICE_FFMPEG", ""),
                  os.environ.get("DSH_BILI_FFMPEG", ""),
                  os.path.join(home, ".dsh", "speech-to-text", "ffmpeg", "ffmpeg.exe"),
                  os.path.join(here, "engine", "bin", "ffmpeg.exe")):
            if c and os.path.isfile(c):
                return c
        return shutil.which("ffmpeg") or ""

    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        emit({"type": "result", "ok": False, "error": "ENV",
              "message": "找不到 ffmpeg（放 ~/.dsh/speech-to-text/ffmpeg/ffmpeg.exe 或设 DSH_BILI_FFMPEG）"})
        return 1
    bt.FFMPEG = ffmpeg
    os.environ["SENSEVOICE_FFMPEG"] = ffmpeg  # ASR 子进程（sensevoice_asr.py）也靠这个找 ffmpeg
    ffprobe = ffmpeg.replace("ffmpeg.exe", "ffprobe.exe")
    if not os.path.isfile(ffprobe) and not shutil.which("ffprobe"):
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
    wrap("save_transcript", "save", "写 %s" % (args.name or "dufu-<bvid>.md"))

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    temp_dir = os.path.abspath(args.temp) if args.temp else os.path.join(out_dir, "_transcribe_temp")
    os.makedirs(temp_dir, exist_ok=True)

    # --force：删掉旧稿，让 process_video 重新走全流程（它自己有 <3000 字的缓存判定）
    target = os.path.join(out_dir, args.name or ("dufu-%s.md" % args.bvid))
    if args.force and os.path.exists(target):
        try:
            os.remove(target)
            emit({"type": "stage", "stage": "reset", "msg": "已删除旧稿，强制重转"})
        except OSError as e:
            emit({"type": "result", "ok": False, "error": "RESET", "message": str(e)})
            return 2

    try:
        result = bt.process_video(args.bvid, output_dir=out_dir, temp_dir=temp_dir, name=args.name)
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
            "engine": engine_kind,
            "name": os.path.basename(path),
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
