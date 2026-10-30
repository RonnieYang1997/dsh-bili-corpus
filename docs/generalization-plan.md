# dsh-bili-corpus 泛化改造计划（草案 v1 · 2026-10-30）

> 目标：从「独夫之心专用」变成「任意音频 → 转写稿」+「任意文本语料 → LLM 校对 / 标点恢复 / 门禁」的通用插件。
> 本文只是计划，不含实现；每个阶段都可独立交付、独立回滚。

## 0. 结论速览

- **合并已经完成**：`dsh-bili-corpus` 本身就是「转写线 + 清洗线」一体的插件；`dsh-corpus-cockpit` 是合并前的旧清洗插件，已于 2026-10-30 删除（目录 + 仓库）。
- 需要泛化的是四处「绑定独夫之心」：**路径默认值、命名约定（`dufu-` + `MEMORY.md`）、转写引擎依赖（AionUi 会话里的 skill venv）、清洗引擎位置（`_scratch/` 脚本 + 内部写死 `MIR`）**。
- 建议顺序：**P0 配置化（低风险，先做）→ P1 转写引擎内置 → P2 清洗引擎内嵌（最重）→ P3 多语料 + 面板**。
- P2 之前不要动数据写盘路径；P2 必须在「副本目录」上演练一遍再碰正式语料。

## 1. 现状：耦合清单

| 位置 | 写死/绑定的内容 | 影响 |
| --- | --- | --- |
| `src/index.ts:48-53` | `WORKSPACE`、`SCRATCH`、`STATE`、`MAIN`、`ARCHIVE` 默认指向独夫之心路径 | 换语料就得改代码/设一堆 env |
| `src/index.ts:54-58` | `PY_SYS`（Python312 绝对路径）、`PY_ASR`、`LINT_DIR` | 换机器就断 |
| `src/index.ts:66-81` | `BILI_SKILL_DIR` 默认在 AionUi 会话临时目录、`PY_BILI` 用它的 `.venv`、`BILI_FFMPEG` 相对 skill 目录推 | **那个会话目录被清 → 转写直接不可用** |
| `src/index.ts:87-90` | `smoothing_batch.py` / `apply_smoothing.py` / `punct_local.py` / `punct_apply.py` 固定从 `_scratch/` 取 | 插件不能独立分发 |
| `src/index.ts:396-412` | 门禁跑 `corpus_lint.py`（`cwd=LINT_DIR`），baseline 存 `STATE` | 校验规则与语料强绑定 |
| `src/index.ts:234,244,279,280,928,976` | 文件名恒为 `dufu-<bvid>.md` | 换前缀要改十几处 |
| `src/index.ts:258-271` | `MEMORY.md` + `- dufu-<bvid>` 行形（R27） | 索引格式不可配 |
| `python/bridge.py` | `import bilibili_transcribe`，再叠加 skill 的 `scripts/` 进 `sys.path` | 依赖外部技能目录 |
| 面板文案 | 「B站转写 · 语料文意疏通」、`corpus_*` 术语、`dufu` 字样 | 多语料时语义混乱 |
| 台账 | `ledger.json` 由 `_scratch` 脚本维护（现有 206 批），插件只读 + 追加 | 引擎迁移要兼容旧 schema |

已可用的口子：`DSH_CORPUS_*` / `DSH_BILI_*` 环境变量（`src/index.ts:48-98`），说明「配置化」不是从零开始。

## 2. 目标形态

```
┌──────────────────────────────────────────────────────────┐
│ dsh-bili-corpus（一个插件）                                │
│                                                          │
│  transcribe/  ── 适配器层 ──  bilibili(WBI+SenseVoice)    │
│                              file(本地音频，P3)           │
│                              → 统一产出 Transcript(md)    │
│                                                          │
│  corpora/    ── 语料层 ────  profile{landing,archive,     │
│                              state,docPrefix,index,lint}  │
│                              → 多语料并存，default 一个    │
│                                                          │
│  pipeline/   ── 校正层 ────  queue → LLM 出补丁 → verify   │
│                              → apply → record → gates     │
└──────────────────────────────────────────────────────────┘
```

关键抽象：
- **Transcript**：`{bvid|sourceId, title, date, duration, body, meta}`；写盘时才落成 `docPrefix + id + .md`。
- **CorpusProfile**：一份语料的所有路径与约定；插件所有工具的第一个可选参数是 `corpus`。
- **Engine**：转写引擎与校正引擎都可替换；引擎只知道「输入在哪、输出到哪、状态写哪」。

## 3. 配置设计（草案）

插件 config（cordis config / `agent.cordis.yml` 的插件段）示例：

```yaml
dsh-bili-corpus:
  defaultCorpus: dufuzhixin
  corpora:
    dufuzhixin:
      docPrefix: dufu-                 # 文件名前缀，"" = 用 id 本身
      landing: ~/.dsh/corpus/dufuzhixin/memory      # 新稿落点（临时）
      archive: ~/Documents/GitHub/dufuzhixin/memory # 产物存档（git 仓）
      state: ~/.dsh/corpus/dufuzhixin/_smoothing_state
      indexFile: MEMORY.md             # "" = 不维护索引
      gates:
        script: builtin:corpus_lint    # builtin: 用插件自带脚本
        baseline: state/lint-baseline-archive.json
        roots: [archive]               # 单根判新增回归
  transcribe:
    bilibili: { engine: builtin:bridge, python: builtin:venv }
    file: { enabled: false }
  ui:
    title: 音频转写 · 语料校对
```

优先级：**config > env（`DSH_*` 兼容旧部署）> 内置默认**。所有 `~` 展开、相对路径按 DSH home 解析。
glob 规则：`docPrefix` 参与扫描（`^<prefix>.+\.md$`），索引行形模板 `indexLine: "- {prefix}{id}"`。

## 4. 分阶段实施

### P0 配置化（0.5–1 天，低风险）

- 新增 `CorpusProfile` 解析层：config → env → 默认；先只允许一个 profile 生效（数据结构支持多个）。
- 把 `dufu-`、`MEMORY.md`、roots 文案、`corpus_*` 描述里的「独夫之心」字样全部参数化。
- 工具签名加可选 `corpus?: string`（缺省 default），行为不变；HTTP `/api/status?corpus=` 同步。
- 验收：
  - 现有 dufuzhixin 语料**零迁移**，`corpus_status` / `corpus_gates` / `bili_transcribe`（缓存命中路径）结果与改造前逐字段一致；
  - 另一个空目录建临时 profile（2 篇样例）→ status 数为 2、gates 走独立 baseline；
  - `render-probe` 文案随 profile 变化。

### P1 转写引擎内置（+0.5 天）

- 把 skill 的 `bilibili_transcribe.py`、`sensevoice_asr.py`、下载器与 `requirements`（不含模型）复制进 `python/engine/`。
- `bridge.py` 改为 `import engine.bilibili_transcribe`；skill 目录降级为可选覆盖（`DSH_BILI_SKILL_DIR` 仍可指定）。
- 兼容现有模型：`BILI_MODEL_DIR` / `BILI_VAD_MODEL` / ffmpeg 仍走配置，找不到时启动自检直接报 ENV（不静默降级 300s）。
- 验收：把 skill 目录临时改名后，`bili_transcribe --force` 冷跑一个 1 分钟视频成功；`bili_jobs.skill` 自检里 `skillDir` 显示 `builtin`。

### P2 清洗引擎内嵌（2–3 天，最重）

- 迁移：`smoothing_batch.py`、`apply_smoothing.py`、`punct_local.py`、`punct_apply.py`、`punct_queue.py`、`corpus_lint.py`、`corpus_lint_selftest.py` → `python/pipeline/`。
- 去掉脚本内部写死：`MIR`、`MAIN`、`MAIN_ROOTS`、`corpus_paths` 依赖；统一 `--corpus-root`/`--archive`/`--state` 参数（插件注入）。
- 台账：`ledger.json` 增加 `corpus`/`version` 字段；旧格式只读兼容，新写入自动补字段。
- 门禁：baseline 按 corpus 存；首次自动生成沿用现有语义（存档单根、0 条基线）。
- 验收（在副本上）：
  - 复制 `~/.dsh/corpus/dufuzhixin` 与存档到 `_tmp/corpus-copy`，用新引擎跑 1 个 punct 波 + 1 个 error 批次（dryRun 与实跑各一次），报告与旧引擎一致；
  - 新空 corpus 用 3 篇样稿跑通 `queue → 补丁 → apply → record → gates`；
  - 正式语料只在验收通过后切换（保留旧脚本一版回滚）。

### P3 多语料 + 面板（1 天）

- 面板顶部 corpus 选择器（`/api/corpora` 列表 + 当前选择记忆在 `cockpit-state.json`）。
- 转写任务台账带 `corpus`；任务列表可按 corpus 过滤。
- 新增「本地音频文件」入口（`file` 适配器）：选文件 → 转写 → 进当前 corpus 落点/存档。

## 5. 兼容与迁移

- **工具名不变**（`corpus_*`、`bili_*`），新增参数都可选 → 旧调用 100% 兼容。
- **数据不动**：landing/archive 原地使用，`dufu-` 前缀保持默认值。
- **旧台账/baseline 不重写**：只读兼容，新写入补 `corpus` 字段。
- 旧 env `DSH_CORPUS_MIRROR` 继续作为 `archive` 别名。
- 回滚点：P1/P2 各留一个 `legacy/` 目录保存旧引擎入口，config 开关切回。

## 6. 测试矩阵

| 层 | 用例 |
| --- | --- |
| 单元 | profile 解析优先级、前缀扫描、索引行模板、路径展开 |
| 契约 | `verify-client.cjs`（两参数 register）+ `render-probe.cjs`（数据渲染）每阶段都跑 |
| 集成 | bridge 冷跑（--force）、缓存命中、ENV 自检失败路径 |
| 回归 | 旧语料 status/gates 与基线快照对比（P0/P2 各一次） |
| 端到端 | 新 corpus 样例：转写 → 归档 → 清洗 → 门禁 |

## 7. 开放问题（需要拍板）

1. `docPrefix` 默认保留 `dufu-` 吗？（`kb-mcp` 检索与 `MEMORY.md` 的 R27 行形依赖它；改前缀要同步迁移）
2. `MEMORY.md` 索引是硬需求，还是允许换成 JSON 清单（`indexFile: ""` 时）？
3. Python 环境怎么带：插件自带 venv（干净但体积/安装慢）还是要求系统 Python + `pip install -r requirements.txt`？
4. 是否只支持 Windows？（现在路径、ffmpeg、`.exe` 全是 Windows 形态）
5. 校正用的 LLM prompt / 专名表（`glossary`）是否也要按 corpus 分文件？
6. 分发形态：私有注入 / `npm pack` 的 tgz / 公开仓库？影响 P2 打包策略。

## 8. 建议顺序

1. **P0 先做**（一天内、零数据风险）→ 由你验收「换 profile 也能跑」。
2. 再决定 P1/P2 是否合并做（它们都只动「引擎从哪来」，不动数据布局）。
3. P3 面板多语料放最后，避免 UI 先于能力。

## 附录 A：第二个语料实测（2026-10-30，`demo`）

用 `node scripts/smoke-second-corpus.mjs --id demo --init` 建了一个与独夫之心无关的语料
（2 篇演示稿，落点 `~/.dsh/corpus/demo/memory`、存档 `~/Documents/GitHub/demo-corpus/memory`），
然后 `--transcribe BV112uU6BEQM` 全链路跑了一遍。结果：

| 环节 | 结果 |
| --- | --- |
| `corpus_status` | 两个根都指向 demo 语料；错字 0/2、标点 unneeded 2（都不需要处理） |
| `corpus_hydrate` | dryRun 计划 1 篇 → 实跑补 1 篇（存档→落点） |
| `corpus_gates` | 自己的 baseline 首次生成；`RESULT: PASS` / `NEW REGRESSIONS: none` / `SELFTEST PASS` |
| 真转写（8m19s 视频） | 51 s 完成 · 3654 字 · 落点写稿 → 自动归档 → 两处 `MEMORY.md` 各补 1 行 |
| 落点/存档一致性 | sha256 相同（11363 B） |

**结论**：转写线、归档、面板数据源（status）、hydrate、门禁已经完全与「独夫之心」解耦；
清洗引擎（`corpus_batch` 背后的 `_scratch` 脚本）仍然绑死 dufuzhixin（`corpus_paths.py` 常量 +
`MIR` 写死 + `assert_corpus_root(min_transcripts=1000)`），在 demo 上跑 `corpus_batch` 会读错库。
这正好把 P0/P2 的必要性从「猜测」变成了「实测」。
