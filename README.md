# @dsh-external/dsh-bili-corpus

B 站视频转写 + 语料「文意疏通」一体化驾驶舱。一个插件、一块面板、两条产线：

```
 转写线    BVID / 视频链接 / 整段分享文案
             │  bili_transcribe（单并发队列）
             ▼
           python/bridge.py ──import──▶ python/engine/scripts（内置转写引擎，P1）
             │  (进程内调用，不产生新会话)
             ▼
           WBI 签名下载 DASH → 本地 SenseVoice → 落点 memory/dufu-<bvid>.md
             │  成功后自动归档进产物存档（git 仓）并补 MEMORY.md 索引
             ▼

 清洗线    dufu-*.md
             │  corpus_batch（错字通道 24 篇/批，LLM 逐篇出补丁）
             ▼
           逐篇 LLM → verify（专名校验）→ python/pipeline/apply_smoothing.py --drop-bad --snap
             │  punct 通道走原生本地模型 punct_local.py + punct_apply.py（0 token）
             ▼
           记录台账 → 门禁（python/pipeline/corpus_lint.py + selftest）→ 通过才允许下一批
```

面板（`conversation.view`，标签「B站语料」）上半区是转写，下半区是清洗进度。

## 安装 / 构建 / 注入

本机没有 DSH 源码 checkout（只有 app.asar），所以构建链**自包含**，不依赖 `DSH_CHECKOUT`：

```bash
cd C:/Users/Ronnie/Documents/GitHub/dsh-bili-corpus
bash scripts/build.sh          # tsc → lib/index.js；tsdown → lib/client.js
```

依赖已装在本地 `node_modules`（typescript / tsdown / @types/node）。首次安装时 peerDependencies
必须全部标 `optional: true`，否则 pnpm 会去 registry 拉 `@deepseek-ai/dsh-type-meta` 而 404。

注入（在装了 dsh-super-injector 的会话里）：

```
dev_inject_plugin C:\Users\Ronnie\Documents\GitHub\dsh-bili-corpus
```

改代码后：`bash scripts/build.sh` → `dev_reload_package dsh-bili-corpus` → 浏览器 **Ctrl+Shift+R**
（client bundle 带 `cache-control: immutable`，不强刷会吃旧缓存）。

旧的 `dsh-corpus-cockpit` 已卸载：本插件用同名 `corpus_*` 工具接管，状态/台账路径不变，
因此 180 批历史与门禁状态都继承下来。

## 工具（10 个）

| 工具 | 作用 |
| --- | --- |
| `bili_transcribe {target, force?, wait?, corpus?, propose?, name?}` | 入队一个转写任务。target 可为 BVID、链接或整段分享文案；`force` 忽略缓存重转；`wait=true` 最多阻塞 30 分钟；`corpus` 指定语料；`propose=true` 只取标题给建议名；`name` 指定产物文件名（标题式语料必需）。返回 jobId，或任务结束时的终态。 |
| `bili_jobs {id?, limit?, clear?}` | 任务队列摘要 / 单任务明细 / 清掉已结束记录。 |
| `corpus_status` | 清洗线全量状态：落点 / 存档两处文件数、错字与标点进度、最近一批、在跑批次、暂停、门禁、token。 |
| `corpus_gates` | 跑门禁（对「产物存档」单根跑 lint 带 baseline + selftest），返回 `nextBatchAllowed`。 |
| `corpus_batch {kind:'error'\|'punct', size?, batch?, dryRun?}` | 开一批。同一时刻只允许一个批次；门禁红则拒绝。 |
| `corpus_pause {reason?}` / `corpus_resume` | 暂停写 `_smoothing_state/cockpit-paused.flag`；恢复只删 flag，**不会**自动开批。 |
| `corpus_mirror_sync {dryRun?}` | 把落点里的稿补进「产物存档」（内容不同才覆盖、缺的补、存档多出的只报告）。默认 dryRun=true。 |
| `corpus_hydrate {dryRun?, limit?}` | 反向补齐：把存档里有、落点里没有的稿复制回落点（旧稿要在落点开批前先补齐）。默认 dryRun=true 只报告。 |
| `corpus_use {id?}` | 不带 id = 列出全部语料与当前生效语料；带 id = 切换（写 `~/.dsh/corpus/active-corpus.json`，重载插件后保持）。批次/转写在跑时拒绝。 |

## HTTP 端点

前缀 `/dsh-bili-corpus/api`：

- `GET  /status?corpus=<id>` — 面板数据（清洗状态 + `transcribe` 队列摘要与引擎自检）；缺 `corpus` = 当前生效语料
- `GET  /corpora` — 语料清单（id/路径/当前生效）
- `POST /corpus` — `{id}` 切换当前语料（批次/转写在跑时 409）
- `GET  /jobs` — 转写队列
- `POST /transcribe` — `{target|bvid|url, force?}` 入队
- `POST /pause` / `POST /resume` — 清洗闸门

## 状态与产物路径

三处目录的分工（与插件内 `python/pipeline/paths.py` 对齐；旧的 `_scratch/` 只是历史脚本目录）：

| 角色 | 路径 | 说明 |
| --- | --- | --- |
| 新稿落点 | `C:/Users/Ronnie/.dsh/corpus/dufuzhixin/memory` | 转写先写这里（临时落脚，DSH 家目录下） |
| **产物存档** | `C:/Users/Ronnie/Documents/GitHub/dufuzhixin/memory` | **真正的产品**（git 仓，kb 索引源）；转写成功自动归档，缺的可用 `corpus_mirror_sync` 补 |
| 旧主库（已退役） | AionUi 会话下的 `memory` | 2026-10-15 起冻结；插件不再读它（存档是唯一校准源），仅作历史备份 |

旧主库退役后清洗流水线照样能跑：`apply_smoothing.py` / `punct_apply.py` / snapshot 都按
「落点 + 存档」两副本工作，所以插件在**开批前**会把存档里有、落点里没有的稿自动补进落点
（批次结果里的 `hydrated` / `missingDocs` 字段），脚本一行不用改。

**标点通道的旧稿**：已落地 2277 篇，剩下的稿子大多只在存档里 —— 开批前插件会自动
`corpus_hydrate` 把存档稿补回落点（P2a 起 queue 扫的是「落点 + 存档」两副本），
之后 `corpus_batch {kind:'punct'}` 才有活干（面板的「标点通道」进度就是这么读的）。

| 路径 | 内容 |
| --- | --- |
| `…/_smoothing_state/ledger.json` | 清洗批次台账（现有 206 批） |
| `…/_smoothing_state/lint-baseline-archive.json` | 门禁基线（对「产物存档」单根，首次跑门禁自动生成） |
| `…/_smoothing_state/cockpit-state.json` | 暂停/门禁/auto 持久化 |
| `…/_smoothing_state/bili-jobs/jobs.json` | 转写任务台账（最近 200 条） |
| `…/memory/dufu-<bvid>.md` | 转写落点稿 |
| `C:/Users/Ronnie/Documents/GitHub/dufuzhixin/memory/dufu-<bvid>.md` | 归档稿（产品） |

## 环境变量

| 变量 | 默认 |
| --- | --- |
| `DSH_BILI_ENGINE_DIR` | 转写引擎脚本目录（默认内置 `python/engine/scripts`；外部技能目录仅作覆盖） |
| `DSH_BILI_SKILL_DIR` | **可选**外部技能目录：只在想用外部引擎时设置（默认空 = 用内置引擎） |
| `DSH_BILI_PY` | 跑 bridge 的 python.exe（默认自动探测：`~/.dsh/speech-to-text/venv` → 外部技能 venv → 系统 Python） |
| `DSH_BILI_BRIDGE` | `python/bridge.py` |
| `DSH_BILI_FFMPEG` / `DSH_BILI_FFPROBE` | 默认自动探测 `~/.dsh/speech-to-text/ffmpeg/` |
| `DSH_BILI_MODEL_DIR` / `SENSEVOICE_MODEL_DIR` | `~/.dsh/speech-to-text/sensevoice/models/sensevoice-onnx` |
| `DSH_CORPUS_CONFIG` | 多语料配置文件，默认 `~/.dsh/corpus/corpora.json`（P0 起语料从这里来） |
| `DSH_CORPUS_ACTIVE` | 首次启动时的默认语料 id（之后以 `~/.dsh/corpus/active-corpus.json` 为准） |
| `DSH_CORPUS_STATE` / `DSH_CORPUS_MAIN` / `DSH_CORPUS_ARCHIVE` / `DSH_CORPUS_LINT_DIR` | 单语料 env 兜底（不写 corpora.json 时生效；旧名 `DSH_CORPUS_MIRROR` 仍是 ARCHIVE 别名） |
| `DSH_CORPUS_PIPELINE` | 清洗脚本目录，默认插件内 `python/pipeline`（P2a 起不用 `_scratch`） |
| `DSH_CORPUS_PATCHES` | 补丁根，默认 `<STATE>/_patches`；由宿主自动传给 python 子进程 |
| `DSH_CORPUS_ENGINE` | `pipeline`（默认，用内嵌 `python/pipeline`）或 `legacy`（回退 `default-workspace/_scratch` 旧脚本；只对 dufuzhixin 有意义） |
| `DSH_CORPUS_SCRATCH` | `legacy` 模式下的旧脚本目录，默认 `C:/Users/Ronnie/Documents/deepseek-harness/default-workspace/_scratch` |
| `DSH_CORPUS_ID` | 宿主自动传：把语料 id 打进台账批次记录（`corpus` 字段），便于排查串库 |
| `DSH_CORPUS_AUTO` | `false`（不自动跑批） |
| `DSH_CORPUS_MIN_TRANSCRIPTS` | `1000`（lint 的 R20 目录可信度阈值；demo 这类小语料靠 `no-git-operations.md` 标记放行） |
| `DSH_CORPUS_DOC_PREFIX` | `dufu-`（宿主导出给清洗脚本；命名策略另有 `naming: bvid|title`） |

## 转写引擎（P1：已内嵌，2026-10-31）

转写引擎（B 站 WBI 下载 + SenseVoice 调用）已复制进 `python/engine/scripts/`，
`python/bridge.py` 默认用**内置引擎**，不再依赖 AionUi 会话里的技能目录（那个目录随时会被清）。
解析顺序：`DSH_BILI_ENGINE_DIR`（显式）→ 内置 → 外部技能目录（`--skill-dir` / `DSH_BILI_SKILL_DIR`）。

仍然需要的外部资产（都是模型/二进制，不进 git）：

| 资产 | 默认位置 | 说明 |
| --- | --- | --- |
| Python venv | `~/.dsh/speech-to-text/venv` | 需含 `sherpa_onnx`、`numpy`；缺失时回退外部技能 venv |
| SenseVoice 模型 | `~/.dsh/speech-to-text/sensevoice/models/sensevoice-onnx` | `model.int8.onnx` + `tokens.txt` |
| VAD 模型 | `~/.dsh/speech-to-text/sensevoice/models/silero/silero_vad.onnx` | 缺了会分段质量变差 |
| ffmpeg / ffprobe | `~/.dsh/speech-to-text/ffmpeg/` | 缺 ffprobe 会被 bridge 直接拦下（防静默 300s 预览） |

迁移到另一台机器：插件自带引擎脚本；把上述四样资产按 `DSH_BILI_*` 指过去即可。
`bili_jobs` 返回的 `skill.kind` 会显示当前用的是 `builtin` 还是 `external`。

## 第二个语料 / 多语料（实验）

**P0 已落地**：语料由 `~/.dsh/corpus/corpora.json` 描述（`id → {docPrefix,indexFile,landing,archive,state}`），
面板标题下有语料 chip 可直接切换（`corpus_use` / `POST /api/corpus` 同一效果），当前语料记在
`active-corpus.json`，重载插件后保持。新增语料 = 往 corpora.json 里加一条 + 重载插件。

自带冒烟脚本（2026-10-30 用 `demo` 语料实测通过）：

```bash
node scripts/smoke-second-corpus.mjs --id demo --init      # 建一个 2 篇演示稿的最小语料
node scripts/smoke-second-corpus.mjs --id demo             # 只读检查：status / hydrate / 门禁
node scripts/smoke-second-corpus.mjs --id demo --transcribe BV112uU6BEQM   # 顺带真转写并验归档
```

当前位置（可 `--landing/--archive/--state` 覆盖）：

| 角色 | demo 默认 |
| --- | --- |
| 落点 | `~/.dsh/corpus/demo/memory` |
| 存档 | `~/Documents/GitHub/demo-corpus/memory` |
| 状态 | `~/.dsh/corpus/demo/_smoothing_state` |

**已解耦、在第二个语料上验证过的**：转写（含冷跑）、自动归档 + 两处 `MEMORY.md` 索引、
`corpus_status` 两库计数、`corpus_hydrate`、`corpus_mirror_sync`、`corpus_gates`（每语料独立
`lint-baseline-archive.json`）、暂停/恢复。

**清洗引擎（P2a：已内嵌，2026-10-31）**：`smoothing_batch.py` / `apply_smoothing.py` /
`punct_apply.py` / `punct_queue.py` / `punct_local.py` / `corpus_lint.py` /
`corpus_lint_selftest.py` 全部复制进 `python/pipeline/`；路径由 `python/pipeline/paths.py`
从宿主注入的环境变量读取（`DSH_CORPUS_LANDING / ARCHIVE / STATE / PATCHES / DOC_PREFIX / ASR_RAW`），
不再依赖 `_scratch/` 和 AionUi 技能目录（宿主跑脚本时 `cwd` 也是 `python/pipeline`）。
补丁目录改为**按语料隔离**：`<STATE>/_patches/batch-N`（旧批次的补丁仍在
`default-workspace/_scratch/_patches`，属于历史）。
门禁默认 `cwd` 也是 `python/pipeline`（`DSH_CORPUS_LINT_DIR` 仍可覆盖；lint 默认只判存档单根）。

**P2b 对拍（2026-10-31）**：同一批稿件 + 同一份补丁，分别在「旧 `_scratch` 引擎」和
「新 `python/pipeline` 引擎」的沙箱副本上落地，报告（每篇 patches/dropped/sha_before/sha_after）
与产物 sha256 **逐字节一致**，见 `docs/generalization-plan.md` 附录 B。
demo 语料真批次（非 dryRun）也跑通：批 3 → queue 4 → 补丁 7 → 落地 2 篇 → 门禁 PASS，
落点/存档 sha 一致、MEMORY.md 索引 4 行未变、dufuzhixin 台账零改动。

**P2c 回滚开关（2026-10-31）**：`DSH_CORPUS_ENGINE=legacy` 时批次链路改走 `_scratch` 旧脚本
（其余功能不受影响）。旧脚本的路径写死 dufuzhixin，所以回退只对 dufuzhixin 有意义；
新代码出问题时把这一条塞进启动 env 即可回到 P2a 之前的行为。
台账批次记录现在带 `corpus` 字段（宿主自动传 `DSH_CORPUS_ID`），配合「每语料一个 STATE 目录」
可以一眼看出某批属于谁。

**命名策略（P3：`bvid` ｜ `title`，2026-10-31）**

语料在 `corpora.json` 里声明 `"naming": "bvid" | "title"`：

| naming | 产物文件名 | 适用 |
| --- | --- | --- |
| `bvid`（默认） | `dufu-BVxxxxxxxxxx.md` | 独夫之心这类历史语料 |
| `title` | `视频标题-BVxxxxxxxxxx.md`（`fileName` 模板默认 `{title}-{bvid}.md`） | 其它视频，标题可读可检索 |

标题式语料转写前会**先问名字**（两步）：

1. `bili_transcribe {target, propose: true}` → 只取元数据（不下载不 ASR），回
   `{ok, title, suggested: "标题-BVxxx.md", hint}`；
2. 用户确认（或改名）后 `bili_transcribe {target, name: "<文件名>", force: true, wait: true}` → 真转写。

标题式语料不带 `name` 直接调用时不会入队，而是回 `{ok:false, needName:true, suggested, hint}`；
面板（B站语料）里也是「转写」按钮 → 服务端回 needName → 输入框自动填上建议名 → 再点一次。
`name` 缺 BV 号会自动补 `-<bvid>`；标题里的 `\/:*?"<>|` 会被 `titleSlug()` 清掉、空白压成 `-`、限 60 字。

**存档去重**：同一 bvid 在存档里已有另一个文件名的稿时不重复归档（保留原稿，日志
「存档已有 X（同视频），保留原稿不重复归档」）；需要覆盖只能显式 `overwrite`（目前只有内部调用）。

**lint 解耦**：`corpus_lint.py` 不再要求 `dufu-` 前缀——只要文件名里带 `BV[0-9A-Za-z]{10}` 就算一篇稿，
索引行也接受任意 `- <stem>`（`dufu-BV...` 与 `标题-BV...` 等价）。`corpus_lint_selftest.py` 的
fixture 已改成合法 BV 号，`SELFTEST PASS`。


## 已知坑（都是踩过的）

1. **宿主进程必须给 python 子进程设 UTF-8**：`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`，
   否则 Windows 下 Python 往管道写 GBK，Node 侧读成乱码（bridge 的 JSON 协议直接崩）。
2. **ffmpeg 旁边必须有 ffprobe.exe**：缺 ffprobe 时技能代码会静默降级成 300 秒预览，
   转写稿被截断还不报错。bridge 启动即校验，缺则返回 `{"error":"ENV"}`。
3. **门禁只对「产物存档」单根跑，且要带 baseline**：存档目前是干净的（单根 `--update-baseline`
   写 0 条），首次自动生成 `lint-baseline-archive.json`，之后只判新增回归。不要连落点一起跑
   （落点只是临时落脚），否则 `gateBlocked` 一落盘就永久锁死。
4. **client 组件必须是两参数注册**：`ctx.slots.register(options, Component)`，组件放进
   `options.component` 会被静默忽略 → 错误边界吞成空白 div（2026-10 语料驾驶舱空白事故）。
5. `bsk`（浏览器 CLI）不在本机，视觉验收用 `~/.dsh/plugins/dsh-super-injector/scripts/render-probe.cjs`
   做「真执行 bundle」的渲染验证：`node render-probe.cjs lib/client.js @dsh-external/dsh-bili-corpus`。
6. 注入器自重载用 `dev_reload_package`；注入器自身的 entry 没有 `_dispose`，走 `entry.fiber.dispose()`。
7. **ASR 子进程靠 `SENSEVOICE_FFMPEG` 环境变量找 ffmpeg**：bridge 解析出 ffmpeg 后会写进子进程 env，
   只给 `bt.FFMPEG` 不够（`sensevoice_asr.py` 是独立 CLI）。2026-10-31 内置引擎实测时踩过。
8. **别给 `apply_smoothing.py` 传 `--guard`**：现版只有 `--snap`（写入前逐篇核对 sha），
   多传 `--guard` 会 argparse 报 `unrecognized arguments`（2026-10-31 演示批实测，已从宿主调用里去掉）。
9. **清洗脚本的常量是"旧世界"口径**：`smoothing_batch.py` 里 `STATE = _cp.STATE` 之类必须在 env 里
   给全（`DSH_CORPUS_STATE/PATCHES`），否则会退回 dufuzhixin 的默认路径 → 串库写入。

## 冒烟

```bash
node ~/.dsh/plugins/dsh-super-injector/scripts/verify-client.cjs lib/client.js @dsh-external/dsh-bili-corpus
node ~/.dsh/plugins/dsh-super-injector/scripts/render-probe.cjs lib/client.js @dsh-external/dsh-bili-corpus
node scripts/smoke-second-corpus.mjs --id demo      # 第二个语料的只读冒烟
```
