#!/usr/bin/env bash
# bilibili-transcribe 运行时自检 / 一键就绪（Git Bash 专用，见 docs/skill-reference.md R29）
#
# 技能是**自给自足**的：解释器、模型、ffmpeg 都装在技能自己的目录里，
# 不依赖 DSH/AionUi 等任何外部应用的私有目录（2026-10-15 起）。
#
#   解释器   <技能>/.venv                      sherpa-onnx + numpy
#   识别模型 <技能>/models/sensevoice-small/   model.int8.onnx + tokens.txt
#   VAD      <技能>/models/silero/             silero_vad.onnx
#   ffmpeg   <技能>/bin/                       ffmpeg.exe
#
# 用法：
#   bash setup.sh          # 自检 + 缺什么补什么
#   bash setup.sh --check  # 只自检，不改动任何东西
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 2026-11-10：运行时不再放在技能目录，统一放 DSH_BILI_HOME（默认 ~/.dsh/bili-corpus）。
#   .venv / bin/ffmpeg.exe / （可选）models/
SKILL="${DSH_BILI_HOME:-$HOME/.dsh/bili-corpus}"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

PY="$SKILL/.venv/Scripts/python.exe"
MODEL="${SENSEVOICE_MODEL_DIR:-$HOME/.dsh/speech-to-text/sensevoice/models/sensevoice-onnx}"
VAD="${SENSEVOICE_VAD_MODEL:-$HOME/.dsh/speech-to-text/sensevoice/models/silero/silero_vad.onnx}"
BIN="$SKILL/bin"
BASE_PY=""

ok()   { printf '  [ok]   %s\n' "$1"; }
miss() { printf '  [缺]   %s\n' "$1"; }
info() { printf '  ..     %s\n' "$1"; }

echo "================================================================"
echo "bilibili-transcribe 运行时自检（插件内置引擎）"
echo "================================================================"
echo "运行时目录: $SKILL"
echo

# ---------------------------------------------------------------- 0. 基础 python
find_base_python() {
  local c
  for c in "${BASE_PYTHON:-}" python3 python py; do
    [ -n "$c" ] || continue
    if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys;assert sys.version_info>=(3,8)" >/dev/null 2>&1; then
      echo "$c"; return 0
    fi
  done
  for c in "$LOCALAPPDATA/Programs/Python/Python312/python.exe" \
           "$LOCALAPPDATA/Programs/Python/Python311/python.exe" \
           "/c/Python312/python.exe" "/c/Python311/python.exe"; do
    [ -f "$c" ] && { echo "$c"; return 0; }
  done
  return 1
}

# ---------------------------------------------------------------- 1. 解释器
echo "[1/4] ASR 解释器（需能 import sherpa_onnx）"
if [ -f "$PY" ] && "$PY" -c "import sherpa_onnx" >/dev/null 2>&1; then
  ok "$PY"
else
  if [ -f "$PY" ]; then
    miss "$PY 存在但不能 import sherpa_onnx"
  else
    miss "技能自带 venv 尚未创建（$PY）"
  fi
  if [ "$CHECK_ONLY" = "1" ]; then
    info "（--check：不安装）"
  else
    BASE_PY="$(find_base_python || true)"
    if [ -z "$BASE_PY" ]; then
      miss "找不到可用于建 venv 的 python（设 BASE_PYTHON 指定）"
    else
      info "用 $BASE_PY 建 venv ..."
      "$BASE_PY" -m venv "$SKILL/.venv" || miss "venv 创建失败"
      if [ -f "$PY" ]; then
        info "安装 sherpa-onnx numpy（首次约 60MB）..."
        "$PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1
        # 源顺序：**清华镜像优先**。2026-10-15 实测（本机，同一时段）：
        #   pypi.tuna.tsinghua.edu.cn  15.8 MB/s
        #   pypi.org                   11 KB/s   ← 慢 1400 倍，先试它等于白等
        # 先试官方源那版曾经直接失败（numpy 报 "from versions: none"），
        # 就是因为慢到超时。所以这里把快的放前面。
        if ! "$PY" -m pip install --quiet -i https://pypi.tuna.tsinghua.edu.cn/simple \
             sherpa-onnx numpy; then
          info "清华镜像失败，改用官方源重试 ..."
          "$PY" -m pip install --quiet sherpa-onnx numpy || miss "pip 安装失败，请手动执行"
        fi
        "$PY" -c "import sherpa_onnx" >/dev/null 2>&1 \
          && ok "$PY（已可 import sherpa_onnx）" \
          || miss "装完了但仍 import 不了，请检查上面的 pip 输出"
      fi
    fi
  fi
fi
echo

# ---------------------------------------------------------------- 2. 模型 + VAD
echo "[2/4] 识别模型 + silero VAD"
NEED_MODEL=0
if [ -f "$MODEL/model.int8.onnx" ] && [ -f "$MODEL/tokens.txt" ]; then
  ok "识别模型 $MODEL"
else
  miss "识别模型缺失（$MODEL）"
  NEED_MODEL=1
fi
if [ -f "$VAD" ]; then
  ok "VAD $VAD"
else
  miss "VAD 缺失（$VAD）"
  NEED_MODEL=1
fi
if [ "$NEED_MODEL" = "1" ] && [ "$CHECK_ONLY" != "1" ]; then
  if [ -f "$PY" ]; then
    info "运行 fetch_sensevoice_model.py ..."
    "$PY" "$HERE/fetch_sensevoice_model.py" || miss "模型获取失败"
  else
    info "解释器还没就绪，跳过（先把第 1 步补上）"
  fi
fi
echo

# ---------------------------------------------------------------- 3. ffmpeg
# 2026-10-15：不再需要 ffprobe（时长改由 `ffmpeg -i` 自己报，见 ffprobe_duration），
# 所以这里只查 ffmpeg.exe 一个文件 —— 体积从 323MB 降到 161MB。
echo "[3/4] ffmpeg（技能自带）"
if [ -f "$BIN/ffmpeg.exe" ] && "$BIN/ffmpeg.exe" -version >/dev/null 2>&1; then
  ok "$BIN/ffmpeg.exe"
else
  miss "技能自带 ffmpeg 尚未安装"
  if [ "$CHECK_ONLY" = "1" ]; then
    info "（--check：不下载）"
  else
    PYBIN=""
    [ -f "$PY" ] && PYBIN="$PY"
    [ -z "$PYBIN" ] && PYBIN="$(find_base_python || true)"
    if [ -z "$PYBIN" ]; then
      miss "没有可用的 python 来运行 fetch_ffmpeg.py"
    else
      info "下载静态 ffmpeg（约 190MB 压缩包，解出 ffmpeg.exe 161MB）..."
      "$PYBIN" "$HERE/fetch_ffmpeg.py" || miss "ffmpeg 获取失败"
    fi
  fi
fi
echo

# ---------------------------------------------------------------- 4. 汇总
echo "[4/4] 结论"
READY=1
[ -f "$PY" ] && "$PY" -c "import sherpa_onnx" >/dev/null 2>&1 || READY=0
[ -f "$MODEL/model.int8.onnx" ] || READY=0
[ -f "$BIN/ffmpeg.exe" ] || READY=0
if [ "$READY" = "1" ]; then
  echo "  就绪。可以直接用："
  echo "    python scripts/transcribe.py"
  echo
  echo "  注：VAD 缺失时也能跑（会退化成整段处理），但切分质量下降。"
else
  echo "  尚未就绪，见上面 [缺] 行。"
fi
echo "================================================================"
[ "$READY" = "1" ]
