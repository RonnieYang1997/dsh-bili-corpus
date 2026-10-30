# @dsh-external/dsh-bili-corpus

B 站视频转写 + 语料「文意疏通」一体化驾驶舱。一个插件、一块面板、两条产线：

```
 转写线    BVID / 视频链接 / 整段分享文案
             │  bili_transcribe（单并发队列）
             ▼
           python/bridge.py ──import──▶ bilibili-transcribe 技能
             │  (进程内调用，不产生新会话)
             ▼
           WBI 签名下载 DASH → 本地 SenseVoice → 落点 memory/dufu-<bvid>.md
             │  成功后自动归档进产物存档（git 仓）并补 MEMORY.md 索引
             ▼

 清洗线    dufu-*.md
             │  corpus_batch（错字通道 24 篇/批，LLM 逐篇出补丁）
             ▼
           逐篇 LLM → verify（专名校验）→ apply_smoothing.py --drop-bad --guard
             │  punct 通道走本地 punct_apply.py（0 token）
             ▼
           记录台账 → 门禁（corpus_lint.py + selftest）→ 通过才允许下一批
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

## 工具（9 个）

| 工具 | 作用 |
| --- | --- |
| `bili_transcribe {target, force?, wait?}` | 入队一个转写任务。target 可为 BVID、链接或整段分享文案；`force` 忽略缓存重转；`wait=true` 最多阻塞 30 分钟。返回 jobId，或任务结束时的终态。 |
| `bili_jobs {id?, limit?, clear?}` | 任务队列摘要 / 单任务明细 / 清掉已结束记录。 |
| `corpus_status` | 清洗线全量状态：落点 / 存档两处文件数、错字与标点进度、最近一批、在跑批次、暂停、门禁、token。 |
| `corpus_gates` | 跑门禁（对「产物存档」单根跑 lint 带 baseline + selftest），返回 `nextBatchAllowed`。 |
| `corpus_batch {kind:'error'\|'punct', size?, batch?, dryRun?}` | 开一批。同一时刻只允许一个批次；门禁红则拒绝。 |
| `corpus_pause {reason?}` / `corpus_resume` | 暂停写 `_smoothing_state/cockpit-paused.flag`；恢复只删 flag，**不会**自动开批。 |
| `corpus_mirror_sync {dryRun?}` | 把落点里的稿补进「产物存档」（内容不同才覆盖、缺的补、存档多出的只报告）。默认 dryRun=true。 |
| `corpus_hydrate {dryRun?, limit?}` | 反向补齐：把存档里有、落点里没有的稿复制回落点（旧稿要在落点开批前先补齐）。默认 dryRun=true 只报告。 |

## HTTP 端点

前缀 `/dsh-bili-corpus/api`：

- `GET  /status` — 面板数据（清洗状态 + `transcribe` 队列摘要与引擎自检）
- `GET  /jobs` — 转写队列
- `POST /transcribe` — `{target|bvid|url, force?}` 入队
- `POST /pause` / `POST /resume` — 清洗闸门

## 状态与产物路径

三处目录的分工（与 `_scratch/corpus_paths.py` 对齐）：

| 角色 | 路径 | 说明 |
| --- | --- | --- |
| 新稿落点 | `C:/Users/Ronnie/.dsh/corpus/dufuzhixin/memory` | 转写先写这里（临时落脚，DSH 家目录下） |
| **产物存档** | `C:/Users/Ronnie/Documents/GitHub/dufuzhixin/memory` | **真正的产品**（git 仓，kb 索引源）；转写成功自动归档，缺的可用 `corpus_mirror_sync` 补 |
| 旧主库（已退役） | AionUi 会话下的 `memory` | 2026-10-15 起冻结；插件不再读它（存档是唯一校准源），仅作历史备份 |

旧主库退役后清洗流水线照样能跑：`apply_smoothing.py` / `punct_apply.py` / snapshot 都按
「落点 + 存档」两副本工作，所以插件在**开批前**会把存档里有、落点里没有的稿自动补进落点
（批次结果里的 `hydrated` / `missingDocs` 字段），脚本一行不用改。

**标点通道的旧稿**：已落地 2277/3755，剩下的 1400+ 篇只在存档里 —— 清洗脚本的 queue 只扫
「落点 + 旧主库」，所以要先 `corpus_hydrate` 把存档稿补回落点，再跑 `punct_queue.py` 出波，
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
| `DSH_BILI_SKILL_DIR` | 技能目录（默认 AionUi 会话下的 bilibili-transcribe） |
| `DSH_BILI_PY` | 技能 venv 的 python.exe |
| `DSH_BILI_BRIDGE` | `python/bridge.py` |
| `DSH_BILI_MODEL_DIR` / `SENSEVOICE_MODEL_DIR` | `~/.dsh/speech-to-text/sensevoice/models/sensevoice-onnx` |
| `DSH_CORPUS_STATE` / `DSH_CORPUS_MAIN` / `DSH_CORPUS_ARCHIVE` / `DSH_CORPUS_LEGACY` / `DSH_CORPUS_LINT_DIR` | 见 `src/index.ts` 顶部常量（旧名 `DSH_CORPUS_MIRROR` 仍作为 ARCHIVE 的别名） |
| `DSH_CORPUS_AUTO` | `false`（不自动跑批） |

## 第二个语料 / 多语料（实验）

路径常量在 `src/index.ts` 加载时从 `DSH_CORPUS_MAIN` / `DSH_CORPUS_ARCHIVE` / `DSH_CORPUS_STATE`
求值，所以**现在换语料 = 换 env 起一个独立的插件进程**（不是面板开关；面板里的切换属于
[`docs/generalization-plan.md`](docs/generalization-plan.md) 的 P0）。

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

**仍绑 dufuzhixin 的是清洗引擎（P2 才解）**：`_scratch/corpus_paths.py` 把
`MAIN/LEGACY_MAIN/MIRROR/STATE` 写成 dufuzhixin 常量；`apply_smoothing.py` / `punct_apply.py`
各自把 `MIR` 写死；`corpus_lint.py` 的 `assert_corpus_root(min_transcripts=1000)` 会拒绝小语料。
所以在 demo 上**不要**跑 `corpus_batch` —— 它会读/写 dufuzhixin 的批次与状态。

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

## 冒烟

```bash
node ~/.dsh/plugins/dsh-super-injector/scripts/verify-client.cjs lib/client.js @dsh-external/dsh-bili-corpus
node ~/.dsh/plugins/dsh-super-injector/scripts/render-probe.cjs lib/client.js @dsh-external/dsh-bili-corpus
node scripts/smoke-second-corpus.mjs --id demo      # 第二个语料的只读冒烟
```
