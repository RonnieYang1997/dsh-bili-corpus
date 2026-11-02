# 每日流水线（插件模式 · 2026-10-31 起）

> 旧模式（21:00 下载班 + 00:00 无头 dsh 班 + `RUNBOOK-correction.md` + `_scratch/nightly/*.sh`）**已废弃**。
> 现在**全部在本 DSH 会话里用本插件自己的工具完成**，不依赖无头进程、不依赖 `_scratch` 脚本。

## 一条命令级流程

| 步 | 动作 | 工具 |
| --- | --- | --- |
| 1 | 发现新视频（只看清单，不下载） | `bili_discover`（默认 7 天 / 250 条投稿） |
| 2 | 逐个转写（下载 + SenseVoice + 落盘 + 归档 + 索引） | `bili_transcribe {target, wait:true}` |
| 3 | 跑门禁（双库一致 / MEMORY.md / BOM / 规则自检） | `corpus_gates` |
| 4 | 疏文（LLM 出补丁 → verify → apply → record → 门禁） | `corpus_batch {kind:'error'}` 循环 |
| 5 | 收尾核对 | `corpus_status` + `corpus_gates` |
| 6 | 回报一行 | `NIGHTLY-RESULT: ...` |

### 1. 发现

```
bili_discover {days: 7, pages: 5}
→ {ok, count, candidates:[{bvid, date, time, title}]}
```

过滤口径（与历史一致，代码在 `python/engine/discover.py`）：
7 天内发布 · 已有 `dufu-<bvid>.md` 的排除 · 游戏切片排除（`gaming_videos.json`）·
充电专属按 `charging_videos.json` 与「有无 cookie」决定是否重试 · 带货广告/碎片排除（标题措辞 + 商品词 + 时长）。

**手工排除清单**：语料根下的 `excluded_videos.json`（与 `charging_videos.json` / `gaming_videos.json` 同格式，
`{"bvid":[...],"updated":"..."}`，仓库侧 `memory/` 里有一份镜像）。加了就**永不再列**，用于短剧、切片、不属语料范围的内容。
当前已排除 9 集「魂穿泽圣」1 分半短剧；另把 1 条 82 秒游戏切片写进 `gaming_videos.json`。

抓取用 WBI `x/space/wbi/arc/search`，桌面 UA 优先、两种 UA 轮换、每页最多 8 次重试（-352 风控是常态）。

### 2. 转写

```
bili_transcribe {target: "BV1xxxxxxxx", wait: true}   # 单并发，长视频 10~20 分钟
bili_jobs {id: "bj-..."}                               # 不想阻塞时轮询
```

产物：`~/.dsh/corpus/dufuzhixin/memory/dufu-<bvid>.md`（落点）+ 归档仓 `~/Documents/GitHub/dufuzhixin/memory/`，
两处 `MEMORY.md` 自动补行，随后触发 kb-mcp 增量索引。**不需要**再手工跑镜像 / 索引 / 删音频。

### 3~4. 疏文

```
corpus_gates                     # 先确认基线绿
corpus_batch {kind:'error'}      # 一批 = queue → snapshot → 逐篇 LLM → verify → apply(--drop-bad --snap) → record → 门禁
corpus_status                    # 看 error.docsRemaining，直到 0
```

- 新稿自动进队列；`docsRemaining` 为 0 就是当天疏完。
- 一次只允许一个批次；门禁失败会拒绝开下一批。
- 补丁目录：`~/.dsh/corpus/dufuzhixin/_smoothing_state/_patches/batch-N/`。

### 5. 收尾

```
corpus_gates     # pass=true 才算过（lint rc=0 + RESULT: PASS + SELFTEST PASS）
corpus_status    # docsDone / docsRemaining / lastGate / paused
```

### 6. 回报

末行固定：

```
NIGHTLY-RESULT: ok discovered=N transcribed=N batches=N patches=N pending=0 gates=PASS
```

## 已知坑（都是踩过的）

1. **充电专属**：匿名取不到流，`state=failed stage=CHARGING`。有 cookie 时每日会重试一次（失败 <1 秒，可接受）。
2. **LLM 输出上限**：默认 16000 token（`DSH_CORPUS_MAX_TOKENS` 可调）。低于 4000 时推理 token 会把 JSON 截断 → 整篇 0 补丁（2026-10-31 事故，已加「截断→双倍预算重试 3 次」）。
3. **含数字的专名**（`57奇→武契奇`）：守卫只认 `kind=number`。插件现在会自动把带数字改动的 name 补丁改写成 number（`normalizeDigitKinds`），不会再静默丢弃。
4. **MEMORY.md 双写**：`mem_index_add` 与归档各补一次会重复行；去重写回必须 `encoding="utf-8"`（带 BOM 会让 lint 的索引匹配失效，看起来像「缺条目」）。
5. **批号不可复用**：`smoothing_batch.py queue` 已加守门（已记录的批号直接拒绝，`--force` 才放行），开批时旧补丁目录自动搬进 `_patches/_stale/`。
6. **空批**：`queue` 排到 0 篇也会写台账留一条空批——查「还有没有活」请读 `corpus_status.docsRemaining`，别拿 `corpus_batch` 试。
7. **转录中途被掐**：若 00:00 之后发现 `dufu-*.md` 只落了一部分，别重跑整套；`bili_transcribe` 是幂等的（已有 ≥3000 字同名稿直接秒回 cached），逐个补即可。
8. **ffprobe 路径推导**：`ffprobe_duration()` 曾经用整串 `replace("ffmpeg","ffprobe")` 推路径，而本机目录就叫 `ffmpeg`（`~/.dsh/speech-to-text/ffmpeg/`），目录名也被替换成 `ffprobe` → `FileNotFoundError [WinError 2]`。表现为**长稿整单失败**（29 分钟那篇连挂三次），而短稿因为走 FLV 兜底 + API 时长而「看起来正常」。现已修成 `_ffprobe_path()`（只换 basename + 兜底 `shutil.which`）；同类代码不要再对整串做 replace。

## Cookie（充电专属 / 会员视频）

取法（`python/engine/scripts/bilibili_transcribe.py` 的 `_load_cookie()`，三层优先级）：

1. 环境变量 `BILI_COOKIE`
2. `~/.dsh/bili-cookie.txt`（推荐：换 cookie 只覆盖这个文件，不用改代码）
3. 文件里的 `_DEFAULT_COOKIE` 兜底

换 cookie：把浏览器里整条 `cookie` 原样写进 `~/.dsh/bili-cookie.txt`（一行），重新 `bili_transcribe` 即可。
`discover.py` 的 `has_cookie()` 直接读 `bilibili_transcribe.COOKIE`，所以充电专属那条会自动从「跳过」变成「重试」。

自查一行（应返回 `isLogin True`）：

```bash
python -c "import sys,json,urllib.request; sys.path.insert(0,'.'); import bilibili_transcribe as bt; r=urllib.request.Request('https://api.bilibili.com/x/web-interface/nav', headers={'Cookie':bt.COOKIE,'User-Agent':'Mozilla/5.0'}); d=json.load(urllib.request.urlopen(r,timeout=15)); print('isLogin',d['data']['isLogin'],d['data']['uname'])"
```

## 定时

- DSH 会话内每日 01:00 提醒（`schedule_create` 建的那条）触发本流程。
- 旧的 Windows 计划任务 `\DufuNightlyDownload`（21:00）与 `\DufuNightlyTranscribe`（00:00）**已停用**。
- 想全自动跑批（不用会话）：`DSH_CORPUS_AUTO=1` 打开插件守护循环（每 `DSH_CORPUS_INTERVAL_MS`，默认 15 分钟开一批，只做疏文，不做发现/转写）。
