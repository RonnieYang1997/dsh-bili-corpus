---
name: bilibili-transcribe
description: >
  Download audio from Bilibili (B站) videos and transcribe to Chinese text with
  **local SenseVoice** (sherpa-onnx, CPU; no online API, no size limit). Handles
  DASH M4A download via WBI-signed API, **gain normalisation** (quiet tracks make
  the model silently drop whole sentences), VAD segmentation for long videos, and
  transcript storage to the knowledge base. Also carries the transcript
  **calibration** method and its tool (`scripts/calibrate.py`: rules TSV in →
  calibrated text + change ledger + review list out) plus the corpus encoding and
  write-path rules (R1-R44).
  Triggers: user asks to transcribe B站/Bilibili videos, process BV-number
  audio, create transcripts, generate text from B站 video, or **校订/校准/校对**
  an existing transcript.
---

# B站视频转文字稿 (Bilibili Video Transcriber)

## Overview

Download B站 video audio (DASH M4A format), transcribe with **local SenseVoice**
(`sherpa-onnx` on CPU — no network, no API key, no file-size limit), and save the
full transcript as a memory file (`dufu-{bvid}.md`) in the project knowledge base.

> **2026-10-09 迁移**：在线 Whisper API 已**完全丢弃** —— 端点、密钥、multipart
> 上传与 25MB 分片逻辑全部移除。迁移理由与实测对照见 **R30**；执行环境约定见 **R29**
> （一律 Git Bash，不用 PowerShell）。

## 快速开始（只问两件事）

```bash
bash scripts/setup.sh            # 一次性自检：解释器 / 模型 / ffmpeg（会复用本机已有的，不重复安装）
python scripts/transcribe.py     # 唯一入口
```

跑起来之后，它**只问你两件事**：

1. **要转哪些视频？** —— 粘 BV 号、整条链接、或一段含多个链接的文字都行
2. **稿子放哪个目录？** —— 默认 `./transcripts`

其余全自动：WBI 签名取流 → 下载 DASH M4A → 本地 SenseVoice 识别 → 写 frontmatter
→ 落盘（CRLF、无 BOM）→ **逐篇校验** → 汇总报告。

**cookie 不用你操心**：先**匿名试跑**（B站公开视频低清档位无需登录）；只有当某个视频
真的因"需要登录/风控/取不到"失败时，才向你索取一次，并存到用户级位置
（`%APPDATA%\bilibili-transcribe\cookies.txt`，**不进 git**）。见 R31。

也可以完全非交互：

```bash
python scripts/transcribe.py --urls "https://www.bilibili.com/video/BV1xx411c7mD" --out ./out
python scripts/transcribe.py --bvids BV1xx411c7mD BV1yy411c7mE --out ./out -y
python scripts/transcribe.py --from-file list.txt --out ./out -y     # list.txt 里是链接/BV 号
python scripts/transcribe.py --profile dufu --bvids BV1xx411c7mD     # 独夫之心语料布局
```

> **只提供函数、不做编排**的旧用法（`download_audio` / `transcribe_long_audio` /
> `save_transcript` / `process_video`）仍然可用，见下面的 Step-by-Step。

## Prerequisites

**先跑 `bash scripts/setup.sh`** —— 四项自检，缺什么明确说缺什么，然后按需补齐。
`--check` 只报告不安装。

**本技能是完全自给自足的**：装完以后，ASR 解释器、模型、ffmpeg 全部在技能目录内，
**不依赖任何外部应用**（2026-10-15 之前它复用 AionUi 会话目录里的 venv/模型/ffmpeg，
用户要求彻底切断，现已全部改为自带）。

| 资源 | 技能内的位置 | 怎么来 |
|---|---|---|
| ASR 解释器 | `.venv/Scripts/python.exe`（sherpa-onnx + numpy） | `setup.sh` 建 venv 并 pip 装 |
| 识别模型 | `models/sensevoice-small/`（`model.int8.onnx` 228MB + `tokens.txt`） | `scripts/fetch_sensevoice_model.py` |
| VAD | `models/silero/silero_vad.onnx`（0.6MB） | 同上（同一脚本一起抓） |
| ffmpeg | `bin/ffmpeg.exe`（161MB） | `scripts/fetch_ffmpeg.py` |

**这三样都 `.gitignore` 了，不进 git**（`.venv/`、`models/`、`bin/`）：它们是
"可由脚本重建的运行时"，不是源码。仓库里只放重建脚本，`bash scripts/setup.sh`
一把装齐。理由不只是"大"—— `bin/` 里的 ffmpeg.exe 有 **161MB**，超过 GitHub
单文件 100MB 的硬限，根本推不上去；就算推得上去，也不该让每次 clone 都拖几百 MB。

**没有 ffprobe**：2026-10-15 起不再需要它。它原先只干一件事 —— 报音频时长，
而 `ffmpeg -i <file>` 本来就把 `Duration:` 打在 stderr 上（见 `ffprobe_duration`）。
去掉它让自带运行时从 **323MB 降到 161MB**，下载量减半。

**ffmpeg 被杀软"内容级封锁"时的应急换件（2026-10-20 实测踩过）**：
`bin/ffmpeg.exe`（BtbN 构建，161MB）在批量转录中途突然变成**连读都被拒绝**——
`Get-Item` 仍报 161MB、`icacls` 显示用户 FULL、Defender 查不到任何威胁记录，
但 `[IO.File]::OpenRead` 和 CreateProcess 一律 `Access is denied`；
于是 `_find_ffmpeg()` 逐个候选都验不过，退回裸 `'ffmpeg'`，转录报
`FileNotFoundError: [WinError 2]`（**报错完全指不到真凶**）。

判据：把**同目录的 python.exe** 复制进 `bin/` 能跑，而 ffmpeg.exe 复制到
`%LOCALAPPDATA%`、`%TEMP%` 都不能跑 ⇒ 封锁跟**内容**走，不跟路径走。

换件（1 分钟；**不要重跑 `fetch_ffmpeg.py`**，它取回的是同一份被封锁的二进制，
下完 191MB 仍然验不过）：

```bash
.venv/Scripts/python.exe -m pip install -q imageio-ffmpeg
cp .venv/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-*.exe bin/ffmpeg.exe
```

imageio 带的是 gyan.dev essentials 构建（7.1，83.6MB），哈希不同即可正常执行；
`_find_ffmpeg()` 会自动重新解析到它，**无需改任何代码**。验证解析结果不是裸 `ffmpeg`：

```bash
.venv/Scripts/python.exe -c "import sys;sys.path.insert(0,'scripts');import bilibili_transcribe as bt;print(bt.FFMPEG)"
```

**下载源顺序是按实测带宽定的（2026-10-15，本机同一时段）**，别随手改回去：

| 用途 | 首选 | 实测 | 次选 | 实测 |
|---|---|---|---|---|
| pip | 清华镜像 | **15.8 MB/s** | pypi.org | 11 KB/s |
| ffmpeg | `gh-proxy.com` 转发 BtbN | **773 KB/s** | gyan.dev 直连 | 16 KB/s |
| 模型 | `hf-mirror.com` | 36 KB/s | huggingface.co | 常连不上 |

先试慢的那个不是"稳妥"，是白等：官方源那次直接把 numpy 拖到超时
（报 `from versions: none`），gyan.dev 按 16 KB/s 下 109MB 要约 2.5 小时。

- **本地兜底**（可选，非必需）：`~/.dsh/speech-to-text/sensevoice/models/` 下有同款模型。
  `~/.dsh` 是 DSH 自己的目录，可用；它只是**兜底**，技能自带的路径优先。
- **B站 cookie**：**可选**。公开视频低清档位匿名可取流；需要时才向你索取（R31）。
- **命令通道**（2026-10-27 DSH 更新后）：用 `ops_bash` —— 宿主把命令串**直接**交给 bash，
  中间不再经过 PowerShell；`pwsh` 只留给 Windows 原生 cmdlet（注册表、服务）。
  底层仍是 `C:\Program Files\Git\bin\bash.exe`，详见 R29。
- 采集器本身**只用标准库**，不引第三方依赖。
- ~~Whisper API key / endpoint~~ — **已废弃，并已从脚本与文档中全部删除**

## Workflow Decision Tree

```
User provides BV number
  │
  ├─ 充电专属 (charging video)? → 无法转录
  │     → 记入 charging_videos.json，后续采集直接过滤，不要每天重试 (R10)
  │
  ├─ Is DASH available? (check playurl API with fnval=16)
  │   ├─ YES → download DASH M4A audio → full audio obtained
  │   └─ NO  → report video has no DASH stream (FLV-only, limited to 300s preview)
  │              → SKIP: manual processing needed
  │
  ├─ 音频多大都直接交给本地 SenseVoice（**没有 25MB 上限，不再分片**）
  │     ├─ 先增益归一（RMS → −18 dBFS，峰值限幅）：便宜、几乎无害 (R37)
  │     └─ 长音频由 CLI 内部用 silero VAD 切成 ≤20s 语音段后逐段识别；
  │        切口落在静音处，并补回被削掉的起始音节（R30 的两个坑）
  │
  ├─ 识别完先量几个数，别先读稿 (R38)
  │     ├─ 段/分钟 < 5        → VAD 漏检，自动改能量切分重跑 (R43，已内置)
  │     ├─ 字/分 < 120        → 同上；仍低则查电平 (R37)
  │     ├─ 空段率 > 15%       → 查音频质量/噪声
  │     └─ 都正常             → 收货
  │
  └─ Save transcript → `memory/dufu-{bvid}.md`
      Format: YAML frontmatter + full text transcript

手上已有一份转写稿（不是要转新的）？
  │
  ├─ 内容要核对 → 走 §7：写规则 TSV → `calibrate.py` → 校订稿 + 台账 + 待复核 (R41/R44)
  └─ 只是文件不合规 → 走 §6：`validate_transcript.py`
```

## Step-by-Step Usage

### 1. Download Audio

```python
from scripts.bilibili_transcribe import download_audio, select_page

m4a_path, info = download_audio("BV1xxyyzz1122", output_dir="_transcribe_temp")
# Returns: (path_to_m4a, {"title": "...", "duration_sec": 3600,
#                         "bvid": "...", "date": "2025-06-15",
#                         "page": 1, "page_count": 14, "part": "1"})
#   date       = 视频 **发布日** (pubdate)，不是转录日。见 R6。
#   page_count > 1 时文件名自动带页码（BVxxx-p01.m4a），且 duration_sec
#   是**本分 P** 的时长而不是全片之和。见 R42。
#   多分 P 课程：download_audio(bvid, page=3) 取第 3 讲。
```

- Uses WBI-signed B站 playurl API (`fnval=16` for DASH)
- Requires cookie in `cookies.txt` or COOKIE env var
- Falls back to `fnval=4048` or `fnval=1` if DASH unavailable
- Downloads with Range header for resume support

### 2. Split Audio (if needed)

```python
from scripts.bilibili_transcribe import split_audio

chunks = split_audio(m4a_path, max_minutes=20)
# Returns: ["path/chunk_000.m4a", "path/chunk_001.m4a", ...]
```

Splitting strategies (tried in order):
1. **fMP4 sidx-based split**: Parse segment index, split at fragment boundaries
2. **Binary split**: Calculate bytes-per-second, split at ~20-min byte positions
3. **AAC frame rebuild**: Extract AAC frames, rebuild valid M4A containers

### 3. Transcribe

```python
from scripts.bilibili_transcribe import transcribe_file

text = transcribe_file("path/to/audio.m4a", language="zh")
# Returns: full transcription string
```

- 由 `scripts/sensevoice_asr.py` 子进程执行（stdout 只出正文，诊断走 stderr）
- 长音频用 `transcribe_long_audio()`：与 `transcribe_file()` 等价，**不再分片**
- **识别前做增益归一化**（RMS → −18 dBFS，带峰值限幅，见 R37）。
  但这不是全部：低电平时**先怀疑 VAD 漏检**（R43），本 CLI 会自动改能量切分重跑。
- 语言默认 `zh`；`use_itn=True`，数字/单位自动规整；CPU int8，实测 RTF≈0.1
- 失败即抛异常（CLI 退出码：2=缺模型 3=缺输入 4=ffmpeg 失败 5=空结果），
  **绝不返回空串冒充成功**
- 覆盖 ~30s 模型窗口：超出 20s 的语音段会被强制切开（`enforce_max_segment`）
- 结果里带 `chars_per_min` / `empty_segments` / `segments` / `gain` /
  `auto_energy_retry`，**先看这几个数再看正文**（R38）

### 4. Save Transcript

```python
from scripts.bilibili_transcribe import save_transcript

save_transcript("BV1xxyyzz1122", "Full transcript text here...")
# Creates: memory/dufu-BV1xxyyzz1122.md
```

### 5. Full Pipeline

```python
from scripts.bilibili_transcribe import process_video

result = process_video("BV1xxyyzz1122")
# Result: {"bvid": "...", "path": "memory/...md", "duration": 3600,
#          "text_length": 5000, "cached": False, "page": None, "part": ""}

# 参数：
#   name_template="dufu-{bvid}.md"   文件名模板（默认 "{bvid}.md"，通用）
#   keep_audio=True                  **不删**临时音频（拆"先下载、后识别"两班跑时必需）
#   min_chars=3000                   正文短于该值且时长 >120s 时重转（短视频按 120s 豁免）
#   page=3                           多分 P 课程的**第 3 讲**（1 起）；文件名自动变
#                                    `BVxxx-p03.md`，不传则只转第 1 讲（见 R42）
```

`save_transcript(bvid, text, video_info, output_dir, name_template=None)` 里没有
`page` 参数 —— 页码从 `video_info["page"]/["page_count"]` 推出来，
`transcript_path(output_dir, bvid, name_template, page=2)` 可单独用来算路径。

### 6. 校验（落盘后必做）

```bash
python scripts/validate_transcript.py --dir ./out --min-chars 500 --json report.json
```

`transcribe.py` 已经在每篇落盘后**自动**调它；单独跑用于事后复查或检查历史目录。

**为什么不用 `corpus_lint.py`**：那是语料库守卫，`--root` 会调 `assert_corpus_root()`
（要求目录里有上千篇 `dufu-BV*.md` 自证身份，见 R20），因此**对一个新的输出目录跑不了**。
`validate_transcript.py` 把同一套判据抽出来，做成可对任意目录跑的一步。

| 级别 | 判据 |
|------|------|
| **FAIL** | UTF-8 BOM；frontmatter 缺失/未闭合/被掏空（`---␊␊---`）；正文为空；U+FFFD；私用区字符（PUA）；乱码字面量（`鏃ユ湡` 等 9 条，与 `corpus_lint.py` 同源）；正文疑似 API 错误载荷；**混行**（CRLF/LF 混杂）；bvid 与文件名不符 |
| **WARN** | 正文短于 `--min-chars`；`date` 为空；`title`/`bvid`/`source` 为空；`duration` 为 0；`date` 格式可疑 |
| **FAIL（特例）** | `date` 是占位符（`1970-01-01` 等）—— 这是**缺陷**不是"缺信息"，见 R6 |

退出码：0 = 无 FAIL；1 = 有 FAIL；2 = 用法错误。纯 LF / 纯 CRLF **都合法**（R26 修正过
"必须 CRLF"的误解），只有混行才算缺陷。

**实测校准**（2026-10-15，独夫之心 3760 篇）：FAIL 仅 1 篇 —— `MEMORY.md`（它不是转录稿，
没有 frontmatter，抓得对）；WARN 分布与已知基线吻合（415 个空 bvid、347 个空 title
= 那批早期导入文件）。**零误报**。

### 7. 校订（手上已有一份转写稿时）

落盘校验管的是"文件是否合规"，管不了"内容是否认对了字"。这两件事要分开做。

校订分三层：**定点层**（要上下文才敢定的，如 `他落网历史还是在德国梦态里面 → 诺瓦利斯还是在德国浪漫派里面`）、
**词典层**（同一个走音的多种写法，如 `斯明诺纳/斯频诺上/斯宾诺少 → 斯宾诺莎`）、
**机械层**（叠字、全角标点、多余空格）。方法见
[`references/transcript-calibration.md`](references/transcript-calibration.md)。

**但顺序不用你操心** —— `scripts/calibrate.py` 把所有规则编成一条正则一遍扫完，
长键自动优先。你要做的只有一件事：**写规则表**。

#### 7.1 一条命令出三件东西

```bash
python scripts/calibrate.py 稿子.md \
    --rules 规则.tsv --out-dir ./校订 --name 输出名 \
    --review 待复核.md          # 可省
```

| 产物 | 内容 |
|------|------|
| `<name>.md` | 校订稿。frontmatter 一字不动，只改正文 |
| `<name>_改动台账.md` | 每处改动逐条可查：**遍 / 行 / 列 / 原 / 改 / 依据 / 上下文** |
| `<name>_待复核.md` | 你手写的疑点 + 自动检查（收敛遍数、零命中规则） |

先 `--check` 空跑看数再落盘；`--strict` 把"有规则一次都没命中"当失败（单篇校订时用）。
`--sample 10` 随机抽 10 处改动打到屏幕上（原/改/上下文/依据），批量校订时逐篇抽检用。
`--json rep.json` 把 `passes / changes / never_fired / hit_keys` 写成 JSON，供上层断言
（批量流程要求"本讲专属键零命中"与"passes ≤ 2"，见参考文档第十二节）。
`--collapse-dup` 折叠结巴叠字（默认关，只动"绝不可能合法叠用"的字）。

#### 7.2 规则表（TSV，三列）

```
# 原走音 <TAB> 改后 <TAB> 依据
诸子	朱子	全篇 24 处，上下文均为朱熹
以礼	仪礼	三礼之一，全篇 18 处
偶与诗书者气势	偶语诗书者弃市	《史记·秦始皇本纪》"有敢偶语诗书者弃市"
```

**依据一列不能省**。它进台账，是唯一能让别人复核的东西；写不出依据的规则，多半是在猜。

#### 7.3 五条纪律（每条都踩过）

1. **空转守卫**：键是正确词的子串时（`拉克利特 → 赫拉克利特`），另加一行
   `赫拉克利特<TAB>赫拉克利特`。改后=原，长键先赢，正确形态就被保护住了。
2. **零命中要回看**：工具会列出一次都没命中的规则 —— 键打错了，或者它被更长的规则遮蔽了
   （都不影响结果，但都值得看一眼）。
3. **收敛遍数 > 1 是信号**：键里含着另一条规则的**产出**（`曾森→曾参` 与 `曾参和有弱→…`），
   同一遍匹配不到，工具会自动再跑。结果不受影响，但键该写得更直接。
4. **不收敛直接报错**：规则自己喂自己会每遍多一个字，8 遍封顶即报错并点出是哪条键。
5. **别用同一个模型交叉印证**：实测 0 条可用建议。

#### 7.4 自测

```bash
python scripts/calibrate.py --selftest     # 12 项，不需要模型与 ffmpeg
```

#### 7.5 一次实测（照抄这个规模）

杨立华《四书精读》第一课，25,191 字：**221 条规则 → 307 处改动 + 1,341 处标点**，
2 遍收敛，零命中 0 条。产出校订稿 / 台账 / 待复核三件，台账逐条带依据。

#### 7.6 长稿：超过约 4.5 万字必须拆半

一讲 49,000 字（024 讲实测）时，"一个人通读全篇写一张表"会连续失败——连文件都建不出来。
拆成 `partA`（前半）`partB`（后半）两班并行，各自每次只读 6,000 字、读完立刻 `>>` 追加，
再用 `scripts/merge_rules.py` 按键去重合并（`calibrate.py` 遇重复键直接退出）：

```bash
python scripts/merge_rules.py 稿子.md calib/<bv>_partA.tsv calib/<bv>_partB.tsv \
    --base calib/base.tsv -o calib/<bv>_extra.tsv          # 报每份表的覆盖区间
python scripts/merge_rules.py --selftest                   # 11 项，不需要真稿子
```

它同时是**验收工具**：覆盖率 = 该表最远的键在正文的结束位置 ÷ 正文长度。
负责后半段的人报 60% 就是没读完；负责前半段的人报 100% 就是读越界了。
分身的"我读完了""已完成"都不作数——**看文件、量覆盖率、跑 `--check`**。
完整判据见参考文档 §12.7 / §12.8（含"分身失联但活儿已干完"与"分身擅自改了稿子"两种状态怎么认）。

## API Reference

### B站 Playurl API

```
GET https://api.bilibili.com/x/player/wbi/playurl
  ?bvid={bvid}&cid={cid}&fnval=16&qn=0&fourk=1
  + w_rid (MD5 of params+wts+wbi_key) + wts (unix timestamp)
```

- WBI signing uses 64-index scramble table (stored in code)
- Cookie format: `SESSDATA=...; bili_jct=...; buvid3=...`
- DASH response: `data.dash.audio[]` array → pick highest bandwidth → `base_url`

### 本地语音识别（SenseVoice / sherpa-onnx）

`scripts/sensevoice_asr.py` —— 一个 CLI，把 ffmpeg 解出的 16k 单声道浮点样本
按 silero VAD 切成 ≤20s 的语音段，逐段送 SenseVoice 识别，再清理标签、拼接成文。

```
python scripts/sensevoice_asr.py <audio> [--language zh] [--threads 4]
    [--model-dir DIR] [--vad-model FILE] [--segment-seconds 20] [--pad-seconds 0.25]
    [--target-db -18] [--no-normalize] [--json-out FILE] [--debug-raw] [--quiet]
```

- **契约**：stdout 只放转录正文；诊断一律 stderr；退出码 0/2/3/4/5
- 模型发现顺序：`$SENSEVOICE_MODEL_DIR` → **技能内** `models/sensevoice-small` → `models/sensevoice-onnx` → `~/.dsh/.../sensevoice-onnx`（兜底）
- VAD 发现顺序：`$SENSEVOICE_VAD_MODEL` → **技能内** `models/silero/silero_vad.onnx` → `~/.dsh/.../silero/silero_vad.onnx`（兜底）
  技能自带的排在 `~/.dsh` 之前，是为了让技能能独立存活（2026-10-15 改）
- 无 VAD 模型时退回能量法切分（`--energy-db`），仍能工作
- **增益归一化默认开启**（`--target-db` 默认 −18 dBFS，带峰值限幅，见 R37）；
  `--no-normalize` 只用于排查。`--json-out` 里含 `chars_per_min` / `empty_segments` / `gain`
- 覆盖 `VAD.max_speech_duration` **不生效**的问题：自建硬上限 + 静音边距（R30）
- ~~Whisper API~~ — 已删除；本地引擎无体积上限、无需凭据、无 429/502
- Rate limits: moderate (retry with 5s backoff on 429)

## Key Technical Details

### WBI Signing

B站的 WBI 签名使用一个 64 元素的混合密钥表，对参数进行 MD5 签名:

```python
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
```

### fMP4 Structure (B站 DASH M4A)

```
ftyp (file type: isom/iso2/mp41)
moov
  └─ mvex (movie extends)
       └─ trex (track extends defaults)
sidx (segment index: maps time→byte position)
[moof (movie fragment)
  └─ traf (track fragment)
       └─ trun (track run: AAC frame offsets in mdat)
 mdat (media data: raw AAC frames)] × N segments
```

### AAC Codec Details

- B站 uses AAC-LC, typically 44100 Hz stereo
- AAC frames are ~1024 samples each
- Frame boundaries determined by parsing ADTS-style sync words or MP4 trun entries
- Raw AAC in MP4 containers = no ADTS headers (elementary stream)

### Memory File Format

```markdown
---
title: "视频标题"
bvid: "BV1xxyyzz1122"
date: "2025-06-15"      # 必须是 pubdate（发布日），不是转录日  → R6
duration: 3600
source: "bilibili"
part: "3"               # 仅多分 P 视频才有：第几个分 P（R42）
---
[完整文字稿内容...]
```

多分 P 视频的 `title` 是**整条视频**的标题（14 讲全都一样），所以必须补 `part`
才能从 frontmatter 分出这是第几讲；单分 P 视频**不写**这个字段，既有语料不动。

**落盘契约（违反会直接污染知识库）：**

- 文件必须以 `---` 为第一个字节（frontmatter 不得缺失） → R7
- 正文里**不得出现任何程序生成的中文标签行**（`日期：` / `BVID：` / `时长：`…）
  → R4。元数据一律进 frontmatter。
- 写文件一律 `encoding="utf-8"` **不带 BOM** → R1
- `date` 取不到时写空串 + 告警，**绝不静默填今天** → R6

## Known Limitations

1. **FLV-only videos**: Some older B站 videos have no DASH stream. API returns only
   300-second preview. These need manual processing or alternative download methods.
2. **本地 ASR 首次加载慢**：模型 228MB，首次载入约 2.5s，之后同一进程内复用。
3. **长音频不再分片**，但**单段**必须 ≤20s 才安全：SenseVoice 的有效窗口约 30s，
   超过会被静默截断 —— CLI 已强制切开，改动这段逻辑时务必保留该上限（R30）。
4. **纯 CPU 推理**：本机无 NVIDIA GPU，走 int8 + 4 线程，约 10 倍实时。
   长视频耗时可观，但远比在线端点稳定（无 429/502/体积上限）。

## Troubleshooting

| Issue | Likely Cause | Solution |
|-------|-------------|----------|
| "No DASH audio" | Video has no DASH stream | Try fnval=4048, or skip video |
| `SenseVoice CLI 缺失` / `未找到 SenseVoice 解释器` | 技能 `.venv` 或脚本不在 | 检查 `scripts/.venv/Scripts/python.exe`；见 Prerequisites |
| 退出码 2（缺模型） | 插件模型目录被移动/删除 | 用 `scripts/fetch_sensevoice_model.py` 重建，或设 `$SENSEVOICE_MODEL_DIR` |
| 转录开头少几个字 | VAD 削掉了起始音节 | **R30**：`--pad-seconds`（默认 0.25）补静音余量 |
| 长音频后半段内容缺失 | 单段超过 ~30s 模型窗口被截断 | **R30**：`enforce_max_segment()` 默认 20s，不要调大 |
| `couldn't create signal pipe, Win32 error 5` | 受限沙箱下 MSYS2 建不了命名管道 | **R29**：Git Bash 只能在完全访问下运行；`git.exe` 不受影响 |
| 命令里的 `\|` / `$1` 行为诡异 | 只在走 `pwsh` 通道时发生（外层 PowerShell 抢先解析） | **R29**：改用 `ops_bash` 直通；不得已时才落盘成 `.sh` |
| `HTTPError: HTTP Error 416: Requested Range Not Satisfiable` | 断点续传发的 `Range: bytes=<本地大小>-` 越界：本地那份音频其实**已经下完**（上次被杀时写全了），却仍按"半截"接着要 | **R45**：别重试 Range，删掉本地音频整份重下；脚本已内置自愈（416 且本地非空 → 删文件、去 Range 头、重发一次） |
| "Cookie expired" | SESSDATA invalid | Update cookies.txt |
| "wbi sign error" | API parameters changed | Check mixin key and sign algorithm |
| 成功率突然暴跌 (−352 风控) | `get_wbi_key()` 写在重试循环里，每次重试都打 nav 接口 | **R8**：钥匙只取一次并缓存 |
| 历史视频 frontmatter 日期全是今天 | 未使用 pubdate，兜底成当天 | **R6**：`download_audio()` 返回 `date` |
| 转录稿出现 `鏃ユ湡` / `##璇` 之类乱码 | PowerShell CP936 内联中文写盘 | **R1/R2/R4** + 跑 `scripts/corpus_lint.py` |
| 300s preview only | FLV-only video | These can't be fully processed without alternative approach |
| 文件头变成 `---␊␊---`（frontmatter 被掏空） | 脚本"解析→重组"式写盘 | **R17**：改成原地字符级 splice + fence 外字节断言 |
| 校验"通过"但两库其实不一样 | 只比了正文，没比 frontmatter | **R18**：整文件 sha256 |
| 标题含"问答"却没有 `type: dialogue` / `warning` | 元数据契约缺失或弱化版提示语 | **R19**：补齐完整版提示语 |
| `warning` 里出现 `联麦` | 错别字 | **R19**：改回 `连麦` |
| 脚本扫出 0 个目标，可目录里明明有 | 根目录常量写错，命中了旧快照目录 | **R20**：`assert_corpus_root()` |
| 批量脚本报"改了 type"但文件没变 | 日志记的是意图而非事实 | **R19**：回读取证 |
| 一批文件 `date: 1970-01-01` / `title: ""` | 当年元数据没取到，写了占位符 | 回源 view API 补真值；取不到就留空，别瞎填 |
| 转录文件正文是 `{"error":{...}}` | 端点用 HTTP 200 回了错误载荷，被当成转录稿 | **R21**：读回校验 + 超大改切分 + 落盘兜底 |
| 转录报 "Request Entity Too Large" | 分片超过端点的真实上限（不是 25MB） | **R21**：把该分片按 1/4 时长再切 |
| 批量转录卡在同一视频几十分钟 | `-ss` 在 `-i` 后面（每片从头解码）+ Windows 上 `subprocess.run(timeout=)` 收尾阻塞 | **R22**：输入侧 seek + 无管道 `Popen`/`wait` |
| 出现 0 字节分片 | ffmpeg 被杀在半路，转录会静默变空 | **R22**：切分后校验非空 |

**最终验收用调用方的工具，不是自己的 lint**：kb-mcp 在
`C:\Users\Ronnie\Documents\GitHub\kb-mcp`，源指向镜像根；在它的目录内跑
`& ".\.venv\Scripts\python.exe" -m kb verify`（期望 `bom=0 / gbk_lines=0 /
no_front_matter=0 / dead=0`）。提交 git 后需重跑 `kb index` 刷新索引。

## 分片与本地 ASR 选型（补充）

### fMP4 Binary Split (The Proven Approach)

> **2026-10-09 起基本用不上了**：本地 SenseVoice **没有体积上限**，长音频不再分片，
> 由 CLI 内部用 silero VAD 切成 ≤20s 语音段（R30）。`split_audio()` 保留作历史参考，
> 只在需要"把长音频切成人能听的片段"时才用；下面第 4 条里的 Whisper 已不存在。

B站 DASH M4A is fMP4 (fragmented MP4). The reliable split approach:

1. Parse `sidx` (Segment Index) to find fragment boundaries
2. Split at fragment boundaries: `ftyp + moov + segments[n:m]`
3. **Critical**: For non-first chunks, reset the first `moof→traf→tfdt`'s `base_media_decode_time` to 0
4. Each chunk is playable standalone → Whisper accepts them natively

### 本地 ASR 选型（2026-10-09 定）

| 组件 | 取值 | 说明 |
|------|------|------|
| 识别模型 | `sensevoice-onnx/model.int8.onnx`（228MB） | SenseVoiceSmall，中英日韩粤，CPU int8 |
| VAD | `silero/silero_vad.onnx`（1.8MB） | 切分用；`max_speech_duration` 不可信，见 R30 |
| 运行时 | `sherpa-onnx 1.13.8` + `numpy 2.5.3` | 注意该版本**已不依赖 numpy**，需手动安装 |
| 参数 | `language=zh`, `use_itn=True`, `threads=4` | **实测 RTF≈0.10（约 10 倍实时）**，见下 |

**实测标定（2026-10-15，技能自带运行时）**：686 秒音频端到端（下载 → ffmpeg 转码 →
VAD 切分 → SenseVoice 识别 → 校验）用了 **68.8 秒**，即 **RTF≈0.10**。
排时长预算按 **0.1 秒/秒音频** 估（10 分钟视频约 1 分钟）。

同一天早些时候在**旧运行时**（AionUi 目录里那份 skill 副本 + 它自带的旧 ffmpeg）
上量到的是 **180.5 秒 / RTF≈0.26**。两次跑的是同一视频、同一模型、同一参数，
差别只在运行时 —— 所以"约 10 倍实时"这个数是对的，之前那次偏慢是旧运行时的开销。
两条记录都留着：换运行时前后差 2.6 倍，本身是个值得记住的数。

**与在线 Whisper 的实测对照**（同一视频 `BV19nA8zwEoU`，213s，两稿均 1572/1579 字）：
SenseVoice 带完整标点，且专名明显更准 ——「实事求是」「限制调控」「修复信用」
「公立教育」四处 Whisper 全错、SenseVoice 全对（详见 R30）。

---

## 编码与落盘规范（必读 · 防复发）

> 2026-09-23 曾因编码问题污染知识库 **2961 个文件**（828 处正文乱码表头、
> 855 处乱码小标题、2839 处 BOM、90 处 frontmatter 缺失）。
> 完整复盘与处置规范见 **[`references/encoding-and-pitfalls.md`](references/encoding-and-pitfalls.md)**。

**不可违背的红线（第一轮 4 条 + 第二轮 4 条）：**

1. **R1/R2｜中文落盘只走 Python，且显式 `encoding="utf-8"`（无 BOM）。**
   禁止用 PowerShell `Set-Content` / `Out-File` / `>` 写中文文件——
   Windows PowerShell 5.1 默认 CP936/UTF-16LE，会把 UTF-8 中文写坏成
   `鏃ユ湡锛?` 这类乱码并产生私用区字符（PUA, U+E000–U+F8FF）。
2. **R4/R6/R7｜正文零中文表头，元数据全进 frontmatter，`date` 必须是 pubdate，
   文件第一个字节必须是 `---`。**
3. **R8｜取 WBI key / token 必须在重试循环之外，一次取好缓存复用。**
   在循环里反复请求 nav 接口会触发 B站 `-352` 随机风控（实测成功率 250/250 → 50/250）。
4. **R10｜充电专属视频与 API 错误页要沉淀缓存/baseline，不要每天重试。**

**另有四条来自第二轮排查（详见 reference 第七节）：**

5. **R11｜模块里绝不能出现"同名后置 shim"。** Python 取最后一次定义——
   曾在文件末尾追加 `def init_logging(): pass` / `def get_wbi_key(): return get_mixin_key()`
   两个"兼容层"，结果**同时废掉了日志配置和 WBI key 缓存**（R8 的修复形同虚设）。
6. **R12｜凭据类兜底值只有两种合法形态：正确值，或显式异常。**
   曾用假 mixin key 兜底，导致签名静默错误、被误判成"随机风控"。
7. **R13｜两库一致性要逐文件哈希校验；裁决取"内容更完整"的一方，
   并把另一方的元数据抢救过来、原文归档，而不是直接丢。**
8. **R14｜`date` 只信 API `pubdate`（北京时间）。**
   同一日期下挂着几十个文件 = 批量运行日，不是发布日；取不到就留空并记录，绝不猜。
   另：改写文件**必须保留原换行符**（本库是 CRLF）。

**第三轮（2026-09-27）新增：元数据与路径卫生——编码对了不等于文件对了。**

9. **R17｜绝不"重装"frontmatter：只做原地字符级替换。**
   曾有文件 frontmatter 被整块掏空（`---␊␊---`，正文完好），因为脚本把 frontmatter
   当对象解析后**重新拼接**写回。正确做法：`open(p, newline="")` 读原文 →
   只替换目标字段的字符区间 → 写回前断言 **fence 之外一个字节都没变**。
10. **R18｜两库一致要按**整文件**哈希比，不能只比正文。**
    旧校验只比 body，导致 frontmatter 坏掉照样"通过"。另需校验"只存在于一个根"
    的文件必须在白名单内（`no-git-operations.md` 只放主库）。
11. **R19｜`连麦/问答` 视频必须同时有 `type: dialogue` 与含"独夫之心"的 `warning`。**
    原先写作"采集器不写这两个字段，属人工富化约定，必须由守卫兜住"——**这条注释本身
    就是第四轮的事故记录**：只加守卫、不进写入路径，结果是每次采集都复发一次，
    守卫只能事后抓（见 R23）。现已由 `save_transcript()` 自动写入；
    禁止 `联麦` 错别字；弱化版提示语（只提"建议结合原视频收听"）必须升级。
    **日志只记事实**：写完后回读取证，不要记"打算做什么"。
    **已知边界（2026-09-30 记录）**：触发条件是**标题**正则 `连麦|联麦|问答`
    （标题来自 API），不是内容分析。所以标题里不带这三个词的"问答式片段"
    （例如 `dufu-BV1yCaR65EhR.md`：标题是《房贷贴息政策出台…》，正文却是
    两人一来一往的短对话）**不会被标记**。这是刻意的取舍：正文里作者经常引用
    他人观点，做内容判断会大量误判；宁可按标题保守触发。
    如果你认为某条需要标记，手工加 `type: dialogue` + `warning` 即可，守卫不会反对。
12. **R20｜脚本里的语料根目录必须自证身份。**
    本轮曾把主库常量漏写一层，正好命中一个 2026-07-18 的旧快照目录（19 个 md），
    脚本"安静地"扫了错目录并返回 0 目标。规范：根目录只有一个常量定义；
    写前调 `assert_corpus_root()`（`dufu-BV*.md` 数量 > 1000，**或**存在
    `no-git-operations.md` 标记——该标记只在主库，镜像库靠文件数自证）；
    命中数量异常必须直接中止。

13. **R21｜转录结果无效（空串/错误载荷）绝不许当成转录稿落盘。**
    （2026-10-09 备注：识别已改本机 SenseVoice，不再有"HTTP 200 + 错误 JSON"这种形态；
    但"空结果"依然可能，所以三层防护**全部保留** —— 规则的价值是"绝不落盘无效内容"，
    与它当初在哪一层发现无关。`WhisperPayloadError` 类名保留以兼容调用方。）
    兼容端点会用 **HTTP 200 + `{"error":{...}}`** 回复；旧代码直接写盘，
    造成 **14 个文件正文是报错 JSON**（kb-mcp 判为 `dead`）。
    三层防护：① 读回即 `looks_like_api_error()` 校验；
    ② 命中 `too_large` **改切分**（按 1/4 时长）而不是重试同样字节；
    ③ `save_transcript()` 落盘前兜底拒绝。
    阈值刻意压到 500 字，避免误杀"独夫在评论网站 Bad Gateway"的真转录。

14. **R22｜ffmpeg 子进程必须有硬超时，且 `-ss` 必须在 `-i` 之前。**
    `-i in.m4a -ss 4800 -t 1200 out.m4a` 是"输出侧 seek"——要从头解码 80 分钟再丢掉；
    改成 `-ss 4800 -t 1200 -i in.m4a` 后 600 秒素材切 10 片只要 **4.7 秒**。
    另外 `subprocess.run(timeout=)` 在 Windows 上**收尾会永久阻塞**（孙子进程握着管道句柄），
    必须用 `run_ffmpeg()` / `run_capture()`（`Popen` + `wait`，**不建管道**）。
    实测一次卡死 25 分钟就是这两条同时踩中。切分后还要校验分片**存在且非空**。

15. **R23｜规则写进文档 ≠ 已经生效：元数据契约必须落在**写入路径**上。**
    第四轮事故：R19 在第三轮被写进文档、加进守卫、手工修好了存量文件，
    **却没有一行代码在写入时应用它**。2026-09-28 的日常采集照旧写出一个没有
    `type`/`warning` 的连麦稿，下一次跑守卫就成了 4 条"新回归"。
    守卫只能事后抓、不能事前挡。凡采集器**可能产出**的元数据契约，都必须在
    **唯一写入点** `save_transcript()` 实现（现已有 `RE_QA_TITLE` + `QA_WARNING`）。
    留一个**正向测试**（非连麦稿件不得被误加）：`_test_r19.py` 同类用例。

16. **R24｜frontmatter 与 bvid 是否"还对得上"，离线永远查不出来。**
    `title`/`duration` 是采集当天快照；作者之后改标题或换源（bvid 不变）就会漂移。
    第四轮实测：2025.9.29 日报四集里三集已对不上当前 API（其中一集的 bvid 已变成
    另一条问答视频并被设为充电专属）。**但 45 个随机样本标题一致 43/45**，
    说明这是**上游漂移、局部且正常**，不是采集错配。
    工具：`scripts/audit_metadata_vs_api.py`（只读、按需、发现漂移 exit 1）；
    **不要放进定时循环**，B站会返回 `-352 风控校验失败`。
    纪律：**不要静默改写历史元数据**，要么在正文注明差异，要么先问用户。

17. **R25｜拿不到内容的视频：正文必须是"诚实的占位说明"，不能是错误载荷。**
    14 个 dead 文件里 13 个重转录成功；`dufu-BV1hFnmznExR.md` 是**充电专属**
    （`is_upower_exclusive=true`、`is_upower_play=false`），取不到音频。
    处置：`status: "unavailable"` + 首行即"本条不是转录稿，请勿引用"的占位正文
    + 复核日期与"文件记录 vs 当前 API"差异，并写明**属上游变动而非采集错误**。
    **不要为了好看直接删文件**（删除不可逆），是否删除由用户裁决。

18. **R26｜换行符：先搞清事实，再谈归一化；取值比较必须兼容引号两种写法。**
    ① 写占位文件时只归一化了正文的 `\n`→`\r\n`，frontmatter 留下 **8 个裸 LF**。
    顺手全库普查后发现**"语料是 CRLF-only"这句话本身就是错的**：3714 个 md 里
    **403 个是纯 LF**、0 个混合换行、0 个 BOM（两库一致）。这 403 个不是本轮造成的
    （采集器新写的稿子与本轮重转录的 13 个文件都是 CRLF），是更早的批次。
    **规则修正**：R15 的本意是"编辑时**保持该文件原有风格**"，不是"把全库改成 CRLF"；
    新建文件跟随多数派 CRLF，**不要为整齐批量改写那 403 个文件**（纯噪音 diff）。
    对自己写的文件断言：`text.count("\n") == text.count("\r\n")`（不混行）；
    先统一到 `\n`、**最后一次性**转 `\r\n`。
    ② `RE_TYPE` 曾用 `(\S+)`，只认**不带引号**的 `type: dialogue`，把合法的
    `type: "dialogue"` 判成"缺 type"——与第三轮"只认带引号 `warning`"是同一类
    bug 的反向版本。凡是取 frontmatter 值做等值比较，正则必须同时容纳带引号与
    不带引号（`^type:[ \t]*["\']?(\w+)["\']?[ \t\r]*$`）。
    ③ 附带：**PowerShell 5.1 的 `>` 重定向会写成 UTF-16LE**（Read 工具判定为二进制），
    重定向一律用 `Start-Process -RedirectStandardOutput`（保留原始 UTF-8 字节）。

19. **R27｜`MEMORY.md` 也是文件：行式索引必须机器可读、去重、覆盖率 100%。**
    第五轮（2026-10-05）查明索引自身有四类缺陷：**36 行**丢了行首 `- `（条目对读取方
    不可见）、**1 行**写成 `- [dufu-BVx](dufu-BVx.md) - 标题`（名字解析不出来）、
    **23 组**同名重复、**18 个**磁盘上存在的转录稿在索引里**根本没有条目**
    （3733 行 / 唯一名字 3709 / 文件 3728 → 覆盖率仅 99.5%）。
    根因与 R23 同源：`daily_scan.py` 只写 `- dufu-BVxxx`，历次修复脚本各写各的格式
    （`- x.md:`、`- x.md: 标题`、链接式），有的还漏掉 `- `，而**没有任何检查覆盖索引**。
    索引行只认两种形式：
    ```
    - dufu-BVxxxxxxxxx
    - dufu-BVxxxxxxxxx.md: 视频标题
    ```
    守卫新增 `lint_memory_index()`（四类 kind：格式错误 / 重复条目 / 缺条目 /
    指向不存在的文件）。**任何往索引里插行的脚本必须幂等、必须先查重、写完立刻跑
    `corpus_lint.py --all`。** 修复工具：`_fix_memory_index.py`（幂等，留 `.bak`，
    去重时把标题升级到保留行以免丢信息）。

20. **R28｜语料根目录只放数据：不许留修复脚本的产物。**
    `_verify.txt`、`dufu-*_wc.txt`、`MEMORY.md.bak` 这类中间产物必须放工作目录
    （如 `_repair_YYYYMMDD_scratch/`）。2026-10-08 在**主库**发现 17 个这样的文件：
    守卫看不见（文件匹配是 `*.md`）、镜像库里也没有（两库 diff 也报不出来），
    等于长期游离在监管之外。守卫新增 `lint_stray()`，白名单只有
    `no-git-operations.md`（主库专有）、`charging_videos.json` / `gaming_videos.json`
    （镜像库专有，`daily_scan.py` 的状态文件）；发现即 `语料根目录存在杂散文件`，`exit 1`。

21. **R29｜执行命令一律走 Git Bash，不用 PowerShell（2026-10-09 用户明确要求）。**
    运行环境是 `C:\Program Files\Git\bin\bash.exe`。非平凡命令**先落盘成 `.sh` 再调用**，
    不要把 `|` `>` `;` `$` `&` 内联进外层命令 —— PowerShell 会**抢先解析**它们。
    实测两次：`... | tee /c/...` 被当成 `Tee-Object` 报 `C:\c\...` 不存在；
    `awk '{print $1}'` 里的 `$1` 被吃掉导致 awk 报语法错。两次都"看起来像脚本坏了"。
    **前置条件**：MSYS2 启动必须创建命名管道，受限沙箱下会拒绝 ——
    `bash.exe: fatal error - couldn't create signal pipe, Win32 error 5`（0xC0000142），
    即 **Git Bash 只能在完全访问下运行**；`git.exe` 是原生程序，不受影响。
    语料库同步记录：`memory/shell-git-bash-only.md`。

    **2026-10-27 DSH 更新后的更正（已实测）**：命令通道改成 `ops_bash` —— 宿主把命令串
    直接交给 bash，中间**没有 PowerShell**。于是上面前两个症状在当前环境里不复现：
    `|`、`>`、`;`、`&&`、`$1`、heredoc、中文全部正常，
    `printf '%s\n' "中文" > x.md` 写出的是 UTF-8（`file` 判 `UTF-8 text`，无 BOM）。
    因此 **"非平凡命令先落盘成 `.sh`"降级为兜底**，只在被迫走 `pwsh` 通道时才需要。
    PowerShell 那一侧的老坑**没变**，也已重新实测：`"中文" > f.md` 写出的仍是
    **UTF-16LE（BOM `FF FE`）** —— 所以**写中文文件一律走 bash**，这条不许放宽。
    命名管道那条前置条件仍然成立（受限沙箱下 MSYS2 起不来），完全访问下无碍。

22. **R30｜识别改用本机 SenseVoice，在线 Whisper API 已丢弃（2026-10-09）。**
    构成：`scripts/sensevoice_asr.py`（CLI，stdout 只出正文）+ 技能内 `.venv`
    （sherpa-onnx + numpy）+ 模型取自**用户已备好的 DSH 插件**
    `~/.dsh/speech-to-text/sensevoice/models/` —— **不要再下载一遍**（曾因此重复拉 228MB）。
    `bilibili_transcribe.py` 把它当"外部可执行文件"调用（与 ffmpeg 同样处理），
    因此采集器仍然只用标准库；`transcribe_long_audio()` **不再按 25MB 分片**。
    迁移理由（同一视频实测对照）：标点完整、专名更准，且无凭据/无体积上限/无 429。
    **两个必须记住的坑**：
    ① `VAD.max_speech_duration` **不生效** —— 213s 音频只返回 **1 段**，
       不强制切分就会被 SenseVoice 的 ~30s 窗口**静默截断**；
       故 CLI 自建 `enforce_max_segment()`（默认 20s）。
    ② VAD 的 `SpeechSegment.start` **已削掉起始音节** —— 同一句话 Whisper 转出
       「没钱人就爹妈带着玩」，切片后只剩「些人就爹妈带着玩」；
       故 `pad_segments()` 给每段前后补静音余量（默认 0.25s，用相邻边界夹紧，
       实测**零重复**）。
    另：`sherpa-onnx 1.13.8` **不再依赖 numpy**（需手动装）且**没有 `read_wave`**
    （用标准库 `wave` + numpy 自行读取）。
    ③ VAD 的环形缓冲必须**按音频长度给足**：固定给 60s 时，对「几乎没有静音」的讲话
       （独夫之心的视频基本如此），VAD 会把整段攒在缓冲里，原生层便自行扩容并往
       stderr 打 `circular-buffer.cc: Overflow! ...`；它虽写着 `No data loss!`，
       但那是**原生层绕过 Python 日志**的输出，会把 `--quiet` 的干净 stderr 弄脏。
       现按长度一次性给足（上限 2 小时）；float32 每样本 4 字节，1 小时音频缓冲约 230MB。

**frontmatter 世代提醒**：语料含至少 5 代 frontmatter（A: `name/description/type`；
B/C/D/E 逐渐演化为现行 `title/bvid/date/duration/source`），
**不要为"整齐"批量重写 3600+ 文件**；守卫只校验"非空 + 身份键存在 + R19 契约 +
两库整文件一致"，新文件一律用现行世代。

**批量写盘后必须跑回归守卫（守卫本身也有自检）：**

```bash
python scripts/corpus_lint.py             # 退出码 0 = 无新增缺陷，1 = 回归
python scripts/corpus_lint.py --all       # 连已知可接受的例外一起列出
python scripts/corpus_lint_selftest.py    # 负向自检：24 类规则必须全部触发
python scripts/calibrate.py --selftest     # 校订工具自检：12 项（含"不响的检查"反例，R44）
python scripts/audit_metadata_vs_api.py   # 回源核对 frontmatter↔bvid（需联网，按需跑，R24）
python scripts/audit_metadata_vs_api.py --bv BV1xxxxxxxxx --show-ok   # 只查指定视频
```

**日常采集后的固定动作**：跑 `daily_scan.py` → 跑 `corpus_lint.py`。
只要新采集的 连麦/问答 稿件触发了 R19，说明 R23 的写入路径实现被改坏了。

第三轮起守卫覆盖三层，第五轮再加索引层与根目录层，共 24 类规则：

| 层 | 规则 | 说明 |
|----|------|------|
| 字节层 | BOM / PUA / `U+FFFD` / 乱码特征串 / 乱码表头 / CRLF-LF 混用 / 正文内中文表头 / API 错误页 | 第一、二轮的历史缺陷 |
| 元数据层 | `frontmatter 内容为空`、`缺少起始 ---`、`date 占位符 1970-01-01`、`元数据为空占位`、`连麦/问答 缺 type: dialogue`、`缺 warning`、`warning 错别字 联麦`、`warning 缺观点归属说明` | 第三轮新增（R14/R17/R19） |
| 两库层 | `两库内容不一致`（整文件 sha256）、`仅存在于主库/镜像库`（白名单外） | 第三轮新增（R18） |
| 索引层 | `MEMORY.md 索引行格式错误`（缺 bullet / 链接式）、`索引重复条目`、`索引缺条目`、`索引指向不存在的文件` | 第五轮新增（R27） |
| 根目录层 | `语料根目录存在杂散文件`（白名单之外的非 `.md` / 非语料文件） | 第五轮新增（R28） |

**只有 baseline 之外的新缺陷才报警**，避免告警疲劳；
新增例外必须显式 `--update-baseline` 才生效。
当前 baseline = **58 条**（第三轮曾为 86 条）：1 处单字丢失（U+FFFD，主/镜像各 1）
+ **28 个早期导入文件的元数据不可恢复**（B站 view API 对 26 个 bvid 返回
"啥都木有"、2 个返回"请求错误"，属伪造/失效 bvid 的旧批次：正文仅 580–1200 字、
传统汉字、同一段文字被复制到 15 个不同 bvid，`title/date/duration` 为占位符）
—— 已按 R14 把 `1970-01-01` 改成空值，**内容保留待用户裁决是否删除**。
**第三轮登记的 14 个"API 错误页存根"已全部清零**：13 个重新转录成功，
1 个充电专属改为诚实占位（R25），因此 baseline 从 86 降到 58。

`assert_corpus_root()` 也可被其它脚本 `import` 复用（R20）。

**命令输出通道注意（R29）**：命令一律由 Git Bash 执行；诊断脚本若打印中文，
首行必须加 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`
（stderr 同办），否则 CP936 控制台遇到 `U+FFFD` 会直接崩溃。

**日志绝不许撒谎（2026-10-09 修）**：`daily_scan.py` 的 `log()` 原用裸 `print()`，
某标题含 `U+2011` 时抛 `UnicodeEncodeError`('gbk' can't encode '\u2011')，
异常被"每条视频"的 `except` 捕获 —— **已经成功落盘的视频被记成 `ERROR`**。
现已两道防线：模块级 `reconfigure` 两个流 + `log()` 内 `except UnicodeEncodeError` 降级写。
判断采集是否成功**以磁盘上的文件为准**，不要只信日志里的 OK/ERROR。

---

## 第五轮（2026-10-15）：统一入口、凭据外置、校验内建

本轮起因：把技能从"独夫之心项目专用"改成**通用**，并补上一直缺的两块 ——
面向用户的**单一入口**与落盘后的**校验**。同时修掉三个真缺陷。

19. **R31｜凭据绝不进源码。** `bilibili_transcribe.py` 第 21 行曾硬编码一整条真实账号
    cookie（`SESSDATA` + `bili_jct`），等于把账号凭据写进源码随 git 走 —— 而同一文件
    第 25 行还写着"这个文件里不再存放任何第三方 API 凭据"，讽刺且危险。
    现已抽到 `bili_credentials.py`，查找顺序：显式参数 → `BILI_COOKIE` 环境变量 →
    `./cookies.txt` → `<skill>/cookies.txt` → `%APPDATA%\bilibili-transcribe\cookies.txt`（推荐）。
    写法容错：原始 cookie 串 / 单值 `SESSDATA=…` / 裸 SESSDATA 值 / 插件导出的 JSON 都认。
    **空 cookie 是合法状态**：公开视频低清档位匿名可取流，所以先匿名试跑，
    只在真失败（`-101/-404/-403/-352/62002`）时才问用户，并用 nav 接口校验后落盘。
    `.gitignore` 另加了 `cookies.txt` / `**/cookies.txt` 兜底。

    **2026-10 审计补记（这条规则第一次并没有真的生效）**：把凭据抽到
    `bili_credentials.py` 之后，同目录还留着 `bilibili_transcribe.py.bak_cookie`
    —— 就是那份硬编码 cookie 的备份，**21,661 字节，内含真实 `SESSDATA` +
    `bili_jct` + `DedeUserID`，已被 `git add` 进 `0528094` 并推到 `origin/main`**。
    规则写进文档、`.gitignore` 也加了，但**没人去查"旧的那份还在不在"**。
    现已删除该文件，`.gitignore` 补 `*.bak_cookie` / `*.bak_*`。
    教训：**改凭据处理方式时，必须同时清掉旧文件并扫描全仓库**
    （`git ls-files | xargs grep -l "SESSDATA="`），否则"修好了"只是修好了新代码。
    已泄露的 cookie 只能靠**重新登录使其失效**，删文件不等于止损；
    若仓库已推送到远端，还需重写历史（`git filter-repo`）后才算干净。

20. **R32｜ffmpeg 必须"找到能跑的"，并把结果显式传给子进程。**
    原 `_find_ffmpeg()` 只查 4 个位置、不验证可用性，找不到就退化成裸名 `'ffmpeg'`。
    本机 PATH 上**没有** ffmpeg（它随附在 DSH/AionUi 会话目录里），后果是：
    下载成功 → 在"取时长"那步才炸 `WinError 2: 系统找不到指定的文件`，报错还指向别处。
    更隐蔽的是**两边各有一套查找逻辑**：父进程已解析到正确路径，子进程
    `sensevoice_asr.py` 却报 `找不到 ffmpeg: ffmpeg`。现在：
    ① 两边都覆盖本机已知位置（会话目录、`~/.dsh`、scoop、`C:\ffmpeg`）并**逐个验证能执行**；
    ② `_run_sensevoice()` 用 `--ffmpeg <已解析路径>` 显式传给子进程。
    教训：**"路径存在"不等于"能跑"**，跨进程传递的路径要么显式传、要么两边同源。

21. **R33｜无语音/纯音乐片段：极短文本是**正确**结果，不要拿它去比 Whisper 的长文本。**
    实测 `BV16J21YSEjK`（16 秒）：SenseVoice 给「我。」，而语料里当年 Whisper 的稿是
    「请不吝点赞 订阅 转发 打赏支持**明镜与点点**栏目」。音频分析（RMS −17~−21dB 全程、
    仅末尾 0.69s 静音）证明**这段是纯音乐/片头，没有人声** —— 所以
    **Whisper 幻觉了一整句**（连频道都串成了"明镜与点点"，是经典幻觉特征），
    SenseVoice 的短输出才是对的。调 VAD 参数（pad 0.25→2.0s、energy-db −35→−50）结果不变，
    进一步排除了"切太狠"。
    **判定空稿要看音频能量，不要看文本长度**；遇到"某稿比旧稿短很多"先查音频再下结论。
    这也解释了独夫之心语料里几篇 `state=empty` 的稿子为何正文是"点赞订阅"话术。

22. **R34｜缓存判定不能只看字数。** 原逻辑 `len(body) >= 3000` 才认缓存，
    对片头/通知/广告这类**天然只有几个字**的短视频失效 —— 每次重跑都白重转一遍
    （实测 16 秒片头每次都重转 6.7 秒）。现在：`len >= min_chars`
    **或**（正文非空 **且** frontmatter `duration <= 120`）即认缓存。

23. **R35｜`date` 取不到就留空 —— 代码里曾经不是这样。**
    文档 R6/R14 写着"绝不静默填今天"，但 `save_transcript()` 的实现是
    `date_str = video_info.get('date') or time.strftime('%Y-%m-%d')` ——
    **规则写进文档、却没落在写入路径上**（与 R23 同一类错误）。
    批量回填/补录会把"运行日"当成"发布日"写进历史。现已改为留空 + stderr 告警。

24. **R36｜输出路径与文件名必须参数化，不绑项目。** 原来硬编码
    `memory/dufu-{bvid}.md`。现在 `--out DIR` + `--name-template`（默认 `{bvid}.md`），
    独夫之心用 `--profile dufu`（= `memory/` + `dufu-{bvid}.md` + CRLF）。
    换行显式归一（`BILI_NEWLINE`），不再靠 Windows 的通用换行翻译隐式产生 CRLF ——
    否则同一技能在非 Windows 上会产出 LF，两种产物。
    另：CLI 必须 `allow_abbrev=False` —— `--bvids` 与 `--bvids-file` 同前缀，
    argparse 的默认缩写匹配会把 `--bvids BV1xx` 解析成 `--bvids-file BV1xx`（把 BV 号当文件名）。

**第六轮（2026-10，29 讲 50 小时课程全量转写 + 校订后新增）——
方法论全文见 `references/transcript-calibration.md`。**

25. **R37｜识别前做增益归一化（默认开启），但别把它当唯一的解药。**

    模型对输入电平敏感，录轻的音轨会**整句被丢弃且不报错、退出码 0**。
    但"丢多少"高度取决于**怎么切分**，这一点是被实测纠正过的：

    | 条件（同一段 20 分钟、−27.7 dBFS 讲座录音） | 段数 | 字/分 |
    |---|---|---|
    | VAD 切分 + 不归一 | 36 | **31** |
    | VAD 切分 + 归一 | 94 | **76** |
    | 能量切分 + 不归一 | 67 | **218** |
    | 能量切分 + 归一 | 74 | **217** |

    读法：**低电平时真正拖后腿的是 silero VAD 的门限，不是识别模型。**
    归一化能把 VAD 从"几乎全聋"抬到"半聋"，而换成能量切分直接回到正常密度。
    （另一组固定 15s 分块、无 VAD 的测量里，归一化让同一讲从 90 字/分升到
    191 字/分 —— 那条是"没有 VAD"时的形态，不要套到本技能上。）

    所以：归一化**默认开启**（便宜、几乎无害；健康音轨实测 163→158 字/分，
    属噪声范围），**同时**必须有 R43 的自动抢救兜住 VAD 漏检。

    三条实现纪律：
    ① **限幅优先于达标**（削波会制造新的识别错误，比声音小更糟）；
    ② **全零输入短路**（`log10(0)` 是 `-inf`，会把 JSON 写成 NaN）；
    ③ **日志必须打出增益前后**（`增益 x3.09 (-27.8 → -18.0 dBFS)`）。

    必须在切分**之前**：无 VAD 时 `--energy-db`（默认 −35 dBFS）是**绝对**阈值，
    录音整体在 −28 dBFS 时正常语音一大半"低于 −35"，会被判成静音。

    自带测试：`python scripts/_test_gain.py`（不需要模型和 ffmpeg）。

26. **R38｜交付前先量四个数，再读稿。**

    这几个数是"有没有整句丢字 / 是不是切分出了毛病"的唯一廉价指示，
    读到第一句正文之前就该看：

    | 指标 | 健康值 | 不正常时先查 |
    |---|---|---|
    | 字/分（中文讲课） | **190–260** | 切分方式（R43），再看电平（R37） |
    | 空段率 | **< 2%** | 音频质量/噪声 |
    | 段/分钟（VAD 切分） | **8–20** | **< 5 基本就是 VAD 漏检**（R43） |
    | 结束时间 vs 视频时长 | 差 < 1% | 下载不完整 |

    "段/分钟"这一列是这轮补上的：低电平时字/分会掉，但**段数掉得更早更狠**
    （20 分钟只切出 36 段 = 1.8 段/分钟），而且它能在识别之前就暴露问题。

    现在 `transcribe_one()` 返回 `chars_per_min` / `empty_segments` / `segments` /
    `gain` / `auto_energy_retry`。**不要跳过这一步**：不量密度，你会拿到一份
    读起来通顺、但少了整整一半的稿子。

    告警**只对时长 >120s 的音频生效** —— 片头/通知/纯音乐本来就只该出几个字，
    拿密度去要求它是误报（R33/R34 已记过同一类教训，这里沿用同一个 120s 阈值）。

27. **R39｜分轨（说话人编号）默认不可信，单一讲授者的课一律合并为 `说话人 1`。**

    平台给的分轨会把同一个人的一句话切成三个"说话人"，切换点在句子中间：

    ```
    [说话人1] 那个亚里士多德…
    [说话人2] 出生。
    [说话人1] 他工作，他死了。
    ```

    实测一节课 `说话人 1/2/3` 各 3649/3069/208 次，而这是单人讲授。
    **处置**：统一标签为 `说话人 1`，**逐条保留时间码、不合并句子** ——
    既纠正错误，又不破坏可定位性。不要试图"修正"分轨，也不要按分轨做内容切分。

28. **R40｜分块长度是"时间码粒度 vs 句子完整度"的取舍，没有免费午餐。**

    15 s 块时间码细，但句子容易在块边界被切成两半（相邻两条各带半句）；
    120 s 块牺牲粒度换来完整句子（实测 212–253 字/分、空段 0–1，质量并不更差）。
    如果你不打算合并句子（通常不该合并，见 R41），就选长一点的块。

29. **R41｜转写稿的校订底线：只订正误录，不创作。**

    | 别做 | 为什么 |
    |---|---|
    | 合并相邻短句 | 破坏时间码定位，且你并不知道说话人是否真的停顿 |
    | 改写句子／删从句／补内容 | 这是转写稿不是编辑稿 |
    | 补一个"听起来对"的人名 | 留着错，读者知道有问题；猜错，读者以为是对的 |
    | 相信文件头时间戳 | 实测同一批写着 9:54/10:18/10:19/10:23/10:24，是录音软件的**排课时刻**；B站 `view_points` 为空、`pubdate` 是上传日，真实日期取不到 |
    | 为整齐批量统一标点/换行 | 纯噪音 diff；只对**你自己新写**的文件统一 |

    另三条方法论要点（全文见 `references/transcript-calibration.md`）：
    ① **替换要单遍交替，不要一条条顺序替换** —— 顺序替换会让规则 A 产生的文本被
    规则 B 再吃一遍（典型：短键 `女性批判→理性批判` 把已改好的 `纯粹理性批判`
    反向吃成 `纯粹女性批判`）。把所有规则编成一条正则一遍扫完，长键天然优先；
    剩下的**线序依赖**（键里含另一条规则的产出）靠"跑到不动点"兜住，
    自己喂自己的规则会不收敛，必须直接报错。工具：`scripts/calibrate.py`；
    ② **校验必须独立于修复，而且必须可证伪**（见 R44）—— 单遍交替之后，
    "按行判残留"这类检查永远不会响，别把不响的检查当成查过了；
    ③ **别用同一个模型做交叉印证** —— 实测 0 条可用建议，同模型错的地方一样错。
    真正的独立证据只有：音频本身、上下文锚点、外部资料（生卒/书名/他人引述）。

30. **R42｜多分 P 视频：`duration` 是**所有分 P 之和**，`cid` 只是第 1 个分 P。**

    2026-10 端到端实测撞到的真缺陷：拿 `view` 接口顶层的 `duration`（14 讲合计
    23.7 小时）配顶层的 `cid`（第 1 讲 1.6 小时）去校验下载完整性，于是
    `5954s < 85484s × 0.2` 成立 —— **一个完全正常的公开课程被判成"充电专属预览"**，
    直接抛 `ChargingVideoError`，一个分 P 都转不出来。而课程/讲座/连续剧几乎全是多分 P。

    修法（`select_page()`）：分 P 的 `cid`、`duration`、`part` 一律从
    `info["pages"][page-1]` 取，充电校验只和**本分 P 的时长**比。
    连带三条：
    ① `--p N` 选页、`--p all` 逐页转（不给 `--p` 就只转第 1 讲）；
    ② **多分 P 的文件名必须带页码**（`BVxxx-p02.md`），否则 14 讲互相覆盖；
       单分 P 保持 `{bvid}.md` 不变，既有语料命名一个都不动；
    ③ 临时音频也带页码（`BVxxx-p02.m4a`），否则第二讲会覆盖第一讲再被识别。

31. **R43｜低电平时先怀疑 VAD，不是先怀疑模型；检测到漏检要自动改切分重跑。**

    实测（20 分钟、−27.7 dBFS）：silero VAD 只切出 **36 段（1.8 段/分钟）**，
    正文 626 字（31 字/分）——**它把大部分语音判成了静音，而模型本身没坏**。
    同一文件改用能量切分、**不做任何归一**：67 段、4366 字、**218 字/分**。

    这是本轮唯一"反直觉且改方向"的一条：我原本以为低电平是模型的问题、
    归一化是解药；端到端一测才发现**切分才是主要杠杆**，归一化只是把 VAD
    从全聋抬到半聋。所以：

    - 判据用**段/分钟**，不用字/分（段数掉得更早，且与识别无关）；
    - `transcribe_one()` 检测到 `how == "vad" and 段/分钟 < 4 or 字/分 < 120`
      就自动用能量切分重跑一遍，**取字多者**，并在 `--json-out` 的
      `auto_energy_retry` 里留证（`--no-auto-energy` 可关）；
    - 重跑成本约是一次识别的时间，只在该怀疑时才付。

    教训（比这条规则本身更值钱）：**"改了 A 之后指标变好"不等于"A 是原因"。**
    我当初在固定分块管线上测到 90→191 字/分，就断言"增益是主因"；
    换到技能的 VAD 管线上一测，同样的增益只有 31→76，而换切分是 76→217。
    变量没控住，因果就说反了。

32. **R44｜不响的校验等于没有：新写的检查，先造一个反例证明它抓得住。**

    2026-10 做 `scripts/calibrate.py` 时，把上一轮校订里的"按行判残留"
    （`key in line and value not in line`）直接搬了进去。自测一跑发现它**一次都不会触发**：
    单遍交替替换保证正文里每个键都被换掉，压根不存在"残留"这种东西。
    这个检查要是一直留着，我每次都会以为"校订稿已经校验过了"——比起没有校验更糟。

    删掉，换成两个真会响的（都配了反例，`--selftest` 里能看见）：

    | 检查 | 抓什么 | 反例长什么样 |
    |---|---|---|
    | 零命中规则 | 键打错 / 这份稿里没有 / 被更长的规则遮蔽 | 规则表里写 `拉克利特`，正文里一个都没有 |
    | 不收敛 | 规则自己喂自己，每遍多一个字 | `拉克利特→赫拉克利特` 而没有空转守卫 |

    判断标准一句话：**给我一个能让它报警的输入**。给不出来，它就不是校验。
