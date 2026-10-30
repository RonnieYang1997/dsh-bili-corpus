/**
 * @dsh-external/dsh-bili-corpus — B站转写 + 语料「文意疏通」一体化驾驶舱（host 侧）。
 *
 * 两条产线，一个面板：
 *  · **转写线**：BVID/URL → 下载音频（WBI 签名 DASH）→ 本地 SenseVoice → dufu-<bvid>.md
 *    走 python/bridge.py（import 既有的 bilibili-transcribe 技能，不重造 ASR）；
 *  · **清洗线**：dufu-*.md → LLM 逐篇出补丁 → apply_smoothing.py / punct_apply.py 落地
 *    → 门禁（corpus_lint.py + selftest）→ 台账。
 *
 * 设计要点（沿用 dsh-corpus-cockpit，2026-10-29 合并改造）：
 *  · 小提示词直调 LLM（ctx.llm.stream），**不产生子代理会话**；系统提示词是稳定块。
 *  · 插件**绝不直写语料**：只写 _patches/batch-N/<bvid>.json，落地永远经既有 Python 脚本。
 *  · 转写同样不直写：产物由技能自己的 save_transcript 落盘（含 frontmatter 与 R19/R21 护栏）。
 *  · 进程内「同刻最多一个批次 / 一个转写任务」互斥；auto 默认 false（不自动跑批）。
 *  · 每批必跑门禁，任一失败 → 状态 FAILED 并拒绝开始下一批。
 *
 * ⚠️ 环境事实（2026-10-11 实测）：本机没有 DSH 源码 checkout（app.asar 是文件），
 * 因此本插件 **不 import 任何 @deepseek-ai/* 运行时模块**——工具注册用
 * ctx.tools.register(<普通对象>)，ctx.web / ctx.webServer 一律用 ctx.get(...) 可选读取。
 */
import { spawn, spawnSync } from 'node:child_process'
import { appendFileSync, copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { basename, dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = '@dsh-external/dsh-bili-corpus'
/** tools 提供 ctx.tools.register；llm 提供 ctx.llm.stream（web/webServer 走 ctx.get 可选读取）。 */
export const inject = ['tools', 'llm']

/** 插件上下文里我们真正用到的那部分（本机无 .d.ts，故本地声明）。 */
interface PluginContext {
  logger?: { info?: (...a: unknown[]) => void; warn?: (...a: unknown[]) => void }
  effect: (cb: () => unknown, label?: string) => unknown
  on: (event: string, listener: (...a: any[]) => unknown) => unknown
  get: (name: string) => any
  tools: { register: (definition: unknown) => () => void }
  llm: {
    stream: (options: Record<string, unknown>) => AsyncIterable<Record<string, any>>
    listProviders: () => Array<{ id: string; name?: string }>
    listModels: (provider: string) => Promise<Array<{ id: string; name?: string }>>
    resolveCallConfig?: (config: Record<string, unknown>, signal?: AbortSignal) => Promise<Record<string, unknown>>
  }
}

// ─────────────────────────────────────────── 语料 profile（P0：配置文件驱动）
const E = process.env
const HOME = homedir().replace(/\\/g, '/')
const WORKSPACE = E.DSH_CORPUS_WORKSPACE || 'C:/Users/Ronnie/Documents/deepseek-harness/default-workspace'
const SCRATCH = E.DSH_CORPUS_SCRATCH || join(WORKSPACE, '_scratch')

/** 多语料配置：一份 JSON 描述若干 corpus；改它 + 重载插件即可加语料（env 仍作兼容兜底）。 */
const CORPORA_FILE = E.DSH_CORPUS_CONFIG || join(HOME, '.dsh', 'corpus', 'corpora.json')
const ACTIVE_FILE = join(HOME, '.dsh', 'corpus', 'active-corpus.json')

type CorpusProfile = {
  id: string
  /** 稿件文件名前缀，例如 `dufu-`（文件名 = `${docPrefix}${bvid}.md`）。 */
  docPrefix: string
  /** 每语料自己的索引文件（默认 MEMORY.md；空串 = 不维护索引）。 */
  indexFile: string
  landing: string
  archive: string
  state: string
  lintDir: string
  python: string
  pythonAsr: string
}

/** 引擎路径（Python / 技能脚本）与语料数据路径无关，全局一份。 */
const ENGINE = {
  python: E.DSH_CORPUS_PY || 'C:/Users/Ronnie/AppData/Local/Programs/Python/Python312/python.exe',
  pythonAsr: E.DSH_CORPUS_ASR_PY
    || 'C:/Users/Ronnie/AppData/Roaming/AionUi/aionui/conversations/aionrs-temp-5c938b2a/.aionrs/skills/bilibili-transcribe/.venv/Scripts/python.exe',
  lintDir: E.DSH_CORPUS_LINT_DIR
    || 'C:/Users/Ronnie/AppData/Roaming/AionUi/aionui/conversations/aionrs-temp-5c938b2a/.aionrs/skills/bilibili-transcribe/scripts',
}

/** 内置默认语料（env 单语料模式的等价物）。 */
function envProfile(): CorpusProfile {
  return {
    id: 'dufuzhixin',
    docPrefix: 'dufu-',
    indexFile: 'MEMORY.md',
    landing: E.DSH_CORPUS_MAIN || join(HOME, '.dsh', 'corpus', 'dufuzhixin', 'memory'),
    archive: E.DSH_CORPUS_ARCHIVE || E.DSH_CORPUS_MIRROR || 'C:/Users/Ronnie/Documents/GitHub/dufuzhixin/memory',
    state: E.DSH_CORPUS_STATE || join(HOME, '.dsh', 'corpus', 'dufuzhixin', '_smoothing_state'),
    lintDir: ENGINE.lintDir,
    python: ENGINE.python,
    pythonAsr: ENGINE.pythonAsr,
  }
}

let PROFILES: Record<string, CorpusProfile> | null = null
let ACTIVE_ID = E.DSH_CORPUS_ACTIVE || 'dufuzhixin'

function readJsonFile<T>(file: string, fallback: T): T {
  try {
    return existsSync(file) ? (JSON.parse(readFileSync(file, 'utf8')) as T) : fallback
  } catch {
    return fallback
  }
}

/** 从 corpora.json + env 解析全部 profile（缺字段继承内置默认）。 */
function loadProfiles(): Record<string, CorpusProfile> {
  const base = envProfile()
  const cfg = readJsonFile<Record<string, any>>(CORPORA_FILE, {})
  const out: Record<string, CorpusProfile> = { [base.id]: base }
  const corpora = (cfg && typeof cfg.corpora === 'object' && cfg.corpora) || {}
  for (const [id, raw] of Object.entries<any>(corpora)) {
    if (!id || !raw || typeof raw !== 'object') continue
    const strs = Object.fromEntries(Object.entries(raw).filter(([, v]) => typeof v === 'string' && v))
    out[id] = { ...base, ...strs, id } as CorpusProfile
  }
  PROFILES = out
  const saved = readJsonFile<{ active?: string }>(ACTIVE_FILE, {})
  const want = String((cfg && cfg.default) || saved.active || ACTIVE_ID)
  ACTIVE_ID = out[want] ? want : base.id
  return out
}

/** 当前生效的语料。所有数据路径都从这里取，不再有模块级路径常量。 */
function P(): CorpusProfile {
  if (!PROFILES) loadProfiles()
  return (PROFILES as Record<string, CorpusProfile>)[ACTIVE_ID] ?? envProfile()
}

function profileOf(id?: string | null): CorpusProfile {
  if (!PROFILES) loadProfiles()
  const map = PROFILES as Record<string, CorpusProfile>
  if (id && map[id]) return map[id]
  return P()
}

/**
 * 语料切换（显式 corpus 绑定 / corpus_use / HTTP 切换）后，把「转写队列 + 门禁记忆 + token」
 * 重新挂到新语料上。由 apply() 内部赋值（它才拿得到 jobs/state 这些闭包变量）。
 */
let onCorpusChanged: (() => void) | null = null

/** 工具/任务执行期间把「当前语料」临时切到 id，结束自动恢复（批次/转写全局单并发，安全）。 */
function bindCorpus<T>(id: string | undefined | null, fn: () => T): T {
  if (!id) return fn()
  const prev = ACTIVE_ID
  ACTIVE_ID = profileOf(id).id
  onCorpusChanged?.()
  try { return fn() } finally { ACTIVE_ID = prev; onCorpusChanged?.() }
}
async function bindCorpusAsync<T>(id: string | undefined | null, fn: () => Promise<T>): Promise<T> {
  if (!id) return fn()
  const prev = ACTIVE_ID
  ACTIVE_ID = profileOf(id).id
  onCorpusChanged?.()
  try { return await fn() } finally { ACTIVE_ID = prev; onCorpusChanged?.() }
}

/** 切换生效语料（写 active-corpus.json；重载插件后仍是它）。 */
function switchCorpus(id: string): { ok: boolean; error?: string; active?: string; corpora?: string[] } {
  if (!PROFILES) loadProfiles()
  const map = PROFILES as Record<string, CorpusProfile>
  const ids = Object.keys(map)
  if (!map[id]) return { ok: false, error: `未知语料：${id}（可用：${ids.join(', ')}）`, corpora: ids }
  ACTIVE_ID = id
  try {
    mkdirSync(dirname(ACTIVE_FILE), { recursive: true })
    writeFileSync(ACTIVE_FILE, `${JSON.stringify({ active: id, at: new Date().toISOString() }, null, 2)}\n`)
  } catch { /* 写不进就只在进程内生效 */ }
  return { ok: true, active: id, corpora: ids }
}

/** 语料清单（给面板选择器 / HTTP 用）。 */
function corporaSummary(): Record<string, unknown> {
  if (!PROFILES) loadProfiles()
  const map = PROFILES as Record<string, CorpusProfile>
  return {
    ok: true,
    active: ACTIVE_ID,
    file: CORPORA_FILE,
    root: join(HOME, '.dsh', 'corpus'),
    corpora: Object.values(map).map((p) => ({
      id: p.id, docPrefix: p.docPrefix, indexFile: p.indexFile,
      landing: p.landing, archive: p.archive, state: p.state,
    })),
  }
}

/** 稿件文件名（按语料前缀，P0 起不再写死 dufu-）。 */
function docName(id: string, prefix: string = P().docPrefix): string { return `${prefix}${id}.md` }
function isDocName(file: string, prefix: string = P().docPrefix): boolean {
  return file.startsWith(prefix) && file.endsWith('.md')
}

const ledgerPath = (p: CorpusProfile = P()): string => join(p.state, 'ledger.json')
const pausedFlag = (p: CorpusProfile = P()): string => join(p.state, 'cockpit-paused.flag')
const cockpitState = (p: CorpusProfile = P()): string => join(p.state, 'cockpit-state.json')
const lintBaseline = (p: CorpusProfile = P()): string => join(p.state, 'lint-baseline-archive.json')
const biliJobsDir = (p: CorpusProfile = P()): string => E.DSH_BILI_JOBS || join(p.state, 'bili-jobs')

// ─────────────────────────────────────────────── 转写线（B站 → 本地 ASR）路径
// P1（2026-10-31）：引擎本体已内嵌到 python/engine/scripts，不再依赖 AionUi 会话里的技能目录。
// 外部技能目录降级为「可选覆盖」（DSH_BILI_SKILL_DIR / DSH_BILI_ENGINE_DIR），解析顺序与 bridge.py 一致。
const BILI_SKILL_DIR = E.DSH_BILI_SKILL_DIR || ''
const BILI_ENGINE_DIR = E.DSH_BILI_ENGINE_DIR
  || join(dirname(fileURLToPath(import.meta.url)), '..', 'python', 'engine', 'scripts')
const BILI_ENGINE_KIND = existsSync(join(BILI_ENGINE_DIR, 'bilibili_transcribe.py')) ? 'builtin' : 'external'
/** 跑 bridge 的解释器：显式 env → 外部技能 venv → DSH 家目录 venv → 系统 Python（需自带 sherpa-onnx 依赖）。 */
function resolveBiliPython(): string {
  const cands = [
    E.DSH_BILI_PY,
    BILI_SKILL_DIR ? join(BILI_SKILL_DIR, '.venv', 'Scripts', 'python.exe') : '',
    join(HOME, '.dsh', 'speech-to-text', 'venv', 'Scripts', 'python.exe'),
    'C:/Users/Ronnie/AppData/Local/Programs/Python/Python312/python.exe',
  ]
  for (const c of cands) if (c && existsSync(c)) return c
  return E.DSH_BILI_PY || ''
}
const PY_BILI = resolveBiliPython()
/** 桥：拷流程进度到 stdout 的 JSON 行；本插件只认这个契约。 */
const BRIDGE = E.DSH_BILI_BRIDGE || join(dirname(fileURLToPath(import.meta.url)), '..', 'python', 'bridge.py')
// 本地模型（已备好，插件只做存在性检查，绝不自动下载——SKILL.md 明令）。
const BILI_MODEL_DIR = E.DSH_BILI_MODEL_DIR
  || 'C:/Users/Ronnie/.dsh/speech-to-text/sensevoice/models/sensevoice-onnx'
const BILI_VAD_MODEL = E.DSH_BILI_VAD_MODEL
  || 'C:/Users/Ronnie/.dsh/speech-to-text/sensevoice/models/silero/silero_vad.onnx'
// ffmpeg/ffprobe：P1 起优先用 ~/.dsh/speech-to-text/ffmpeg（已从会话目录搬出来），
// 再回退外部技能目录上溯；缺 ffprobe 会被 bridge 直接拦下（静默 300s 预览是历史事故）。
function firstExisting(paths: Array<string | undefined>): string {
  for (const p of paths) if (p && existsSync(p)) return p
  return ''
}
const BILI_FFMPEG = E.DSH_BILI_FFMPEG
  || firstExisting([
    join(HOME, '.dsh', 'speech-to-text', 'ffmpeg', 'ffmpeg.exe'),
    BILI_SKILL_DIR ? join(BILI_SKILL_DIR, '..', '..', '..', 'ffmpeg.exe') : '',
  ])
  || join(HOME, '.dsh', 'speech-to-text', 'ffmpeg', 'ffmpeg.exe')
const BILI_FFPROBE = E.DSH_BILI_FFPROBE || BILI_FFMPEG.replace('ffmpeg.exe', 'ffprobe.exe')
const BILI_MODEL_FILE = join(BILI_MODEL_DIR, 'model.int8.onnx')

const BATCH_SCRIPT = join(SCRATCH, 'smoothing_batch.py')
const APPLY_SCRIPT = join(SCRATCH, 'apply_smoothing.py')
const PUNCT_LOCAL = join(SCRATCH, 'punct_local.py')
const PUNCT_APPLY = join(SCRATCH, 'punct_apply.py')
// 与 _scratch/smoothing_batch.py 的 DONE_STATUS 对齐（2026-10-13）：
// 'skip' 是"代理读过、判定无需改动但落地时报补丁为空"的稿子——它同样算走完一遍，
// 否则驾驶舱的 docsDone 永远差 14 篇到不了 3747（UI 上会显示"未完成"）。
const DONE_STATUS = new Set(['done', 'already', 'empty', 'skip'])
const KINDS = new Set(['homophone', 'name', 'stutter', 'number', 'punct', 'other'])
const BODY_LIMIT = Number(E.DSH_CORPUS_BODY_LIMIT || 60000)
const AUTO = /^(1|true|yes|on)$/i.test(String(E.DSH_CORPUS_AUTO || ''))
const INTERVAL_MS = Number(E.DSH_CORPUS_INTERVAL_MS || 900000)

// ─────────────────────────────────────── LLM 系统提示词（稳定块：逐字，勿逐篇改）
export const SYSTEM_PROMPT = `你是中文语音转写稿的校对员，任务是「文意疏通」：只修同音错字、专有名词误听、口吃重复。
不做改写、润色、摘要、删减。

铁律：
1. 只输出 JSON，不要任何解释文字。
2. 不许删内容；replace 里不许出现省略号（… 或 ...）。
3. 数字/单位/型号默认不改（747、25%、100式、Q10…）。唯一例外：整条补丁是专名修正且 kind=name
   （例如误听的 8V→巴威、CC系统部→C4系统部），此时 why 必须写依据。
4. 不确定就不要改。改错一个事实比留一个错别字严重得多。
5. find 必须与正文逐字精确一致且全文唯一；同一片段出现多次就加 "all": true，或用 "occurrence": 2。
6. find 要带足够上下文（6~20 字），不要只写 2 个字。
7. kind=stutter 只用于口吃/重复：只许删 1~8 字、最多改 2 字、插入的只能是标点。
8. 其他 kind 不许删字（净缩短 ≤2 字）。

只输出这个结构：
{"bvid":"<给定 bvid>","patches":[{"find":"...","replace":"...","kind":"homophone|name|stutter|number|other","why":"一句话依据"}]}
没有发现问题就输出 {"bvid":"<给定 bvid>","patches":[]}`

// ───────────────────────────────────────────────────────────────── 小工具
function nowIso(): string {
  return new Date().toISOString()
}

function ensureDir(p: string): void {
  try { mkdirSync(p, { recursive: true }) } catch { /* 已存在 */ }
}

function readJson<T = any>(file: string, fallback: T): T {
  try {
    if (!existsSync(file)) return fallback
    return JSON.parse(readFileSync(file, 'utf8')) as T
  } catch {
    return fallback
  }
}

/** UTF-8 无 BOM 写 JSON（语料侧所有产物都要求无 BOM）。 */
function writeJson(file: string, value: unknown): void {
  ensureDir(dirname(file))
  writeFileSync(file, JSON.stringify(value, null, 2), { encoding: 'utf8' })
}

interface RunResult { rc: number; out: string; error: string | null; ms: number }

function run(cmd: string, args: string[], opts: { cwd?: string; timeoutMs?: number } = {}): RunResult {
  const started = Date.now()
  const r = spawnSync(cmd, args, {
    cwd: opts.cwd,
    encoding: 'utf8',
    timeout: opts.timeoutMs ?? 600000,
    maxBuffer: 128 * 1024 * 1024,
    windowsHide: true,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  })
  const out = String(r.stdout ?? '') + String(r.stderr ?? '')
  return { rc: r.status ?? -1, out, error: r.error ? String(r.error.message) : null, ms: Date.now() - started }
}

/** 从子进程输出里抓最后一个 "RESULT: ..." 之类的行，便于回报。 */
function grabLine(text: string, re: RegExp): string | null {
  const lines = text.split(/\r?\n/)
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    if (re.test(lines[i])) return lines[i].trim()
  }
  return null
}

function tail(text: string, n = 1600): string {
  return text.length > n ? text.slice(text.length - n) : text
}

// ─────────────────────────────────────────────────────────────────── 插件体
export function apply(ctx: PluginContext, _config?: Record<string, unknown>): void {
  const SHORT = 'dsh-bili-corpus'
  const dshHome = process.env.DSH_HOME || join(homedir(), '.dsh')
  const LOG = process.env.DSH_BILI_LOG || join(dshHome, 'super-injector', `dsh-bili-corpus.log`)

  const log = (msg: string): void => {
    try {
      ensureDir(dirname(LOG))
      appendFileSync(LOG, `[${nowIso()}] ${msg}\n`)
    } catch { /* 日志失败静默 */ }
  }

  // ── 多语料配置：首次跑自动落一份模板；之后改 corpora.json + 重载插件即可加语料
  try {
    if (!existsSync(CORPORA_FILE)) {
      const template = {
        default: 'dufuzhixin',
        corpora: {
          dufuzhixin: {
            docPrefix: 'dufu-',
            indexFile: 'MEMORY.md',
            landing: join(HOME, '.dsh', 'corpus', 'dufuzhixin', 'memory'),
            archive: 'C:/Users/Ronnie/Documents/GitHub/dufuzhixin/memory',
            state: join(HOME, '.dsh', 'corpus', 'dufuzhixin', '_smoothing_state'),
          },
          demo: {
            docPrefix: 'dufu-',
            indexFile: 'MEMORY.md',
            landing: join(HOME, '.dsh', 'corpus', 'demo', 'memory'),
            archive: join(HOME, 'Documents', 'GitHub', 'demo-corpus', 'memory'),
            state: join(HOME, '.dsh', 'corpus', 'demo', '_smoothing_state'),
          },
        },
      }
      ensureDir(dirname(CORPORA_FILE))
      writeFileSync(CORPORA_FILE, `${JSON.stringify(template, null, 2)}\n`)
      log(`已写入多语料配置模板：${CORPORA_FILE}`)
    }
    loadProfiles()
    log(`语料：active=${ACTIVE_ID} 共 ${Object.keys(PROFILES as Record<string, CorpusProfile>).length} 个（${Object.keys(PROFILES as Record<string, CorpusProfile>).join(', ')}）`)
  } catch (e) {
    log(`多语料配置初始化失败（退回内置默认）: ${String(e)}`)
  }

  // ── 进程内状态（互斥锁 + 门禁记忆 + 累计 token）
  let inFlight: { kind: string; batch: number; phase: string; startedAt: string } | null = null
  let lastGate: { ok: boolean; at: string; lintRc: number; lintPass: boolean; selftestPass: boolean } | null = null
  let lastBatch: Record<string, unknown> | null = null
  let gateBlocked = false
  let tokens = { input: 0, output: 0, calls: 0 }
  let cycles = 0
  let lastRoute: { provider: string; model: string } | null = null

  // 落盘状态：跨热重载/重启保住「门禁失败 → 拒绝开下一批」这条硬规则
  const persisted = readJson<Record<string, any>>(cockpitState(), {})
  if (persisted && typeof persisted === 'object') {
    if (persisted.lastGate && typeof persisted.lastGate.ok === 'boolean') lastGate = persisted.lastGate
    if (persisted.lastBatch) lastBatch = persisted.lastBatch
    if (persisted.tokens && typeof persisted.tokens === 'object') {
      tokens = {
        input: Number(persisted.tokens.input) || 0,
        output: Number(persisted.tokens.output) || 0,
        calls: Number(persisted.tokens.calls) || 0,
      }
    }
    gateBlocked = persisted.gateBlocked === true
  }

  const saveState = (): void => {
    try {
      writeJson(cockpitState(), {
        at: nowIso(), auto: AUTO, paused: isPaused(), gateBlocked,
        lastGate, lastBatch, tokens, cycles,
      })
    } catch { /* 状态落盘失败不致命 */ }
  }

  const isPaused = (): boolean => existsSync(pausedFlag())

  // 捕获主模型路由（waterfall 必须 next() 委托）
  try {
    ctx.on('llm/stream', (options: any, next: () => unknown) => {
      if (options && typeof options.provider === 'string') {
        lastRoute = { provider: options.provider, model: String(options.model) }
      }
      return next()
    })
  } catch { /* 无 llm 事件面时忽略 */ }

  // ────────────────────────────────────────────────────────── 语料/台账读取
  function countRoot(root: string): { dufu: number; other: string[] } {
    try {
      const all = readdirSync(root)
      const dufu = all.filter((f) => isDocName(f))
      return { dufu: dufu.length, other: all.filter((f) => !isDocName(f)) }
    } catch {
      return { dufu: -1, other: [] }
    }
  }

  /** 找稿：新稿落点 → 产物存档（存档是唯一校准源）。 */
  function findDoc(bvid: string): string | null {
    for (const root of [P().landing, P().archive]) {
      const p = join(root, docName(bvid))
      if (existsSync(p)) return p
    }
    return null
  }

  function docChars(path: string): number {
    try {
      return readFileSync(path, 'utf8').length
    } catch {
      return 0
    }
  }

  /** MEMORY.md 索引：没有就补一行 `- dufu-<bvid>`（R27 只认这种行形；保持原文件的换行风格）。 */
  /** 索引行：缺就补一行 `- <prefix><id>`（R27 行形；保持原文件换行风格）。 */
  function ensureIndexLine(root: string, bvid: string): void {
    const indexName = P().indexFile
    if (!indexName) return // indexFile 为空 = 该语料不维护索引
    const file = join(root, indexName)
    try {
      const text = existsSync(file) ? readFileSync(file, 'utf8') : ''
      const line = `- ${P().docPrefix}${bvid}`
      const already = text.split(String.fromCharCode(10)).some((l) => {
        const t = l.trim()
        return t === line || t === `${line}.md`
      })
      if (already) return
      const eol = text.indexOf(String.fromCharCode(13, 10)) >= 0 ? String.fromCharCode(13) + String.fromCharCode(10) : String.fromCharCode(10)
      const next = text.length === 0 ? `${line}${eol}` : text.endsWith(eol) ? `${text}${line}${eol}` : `${text}${eol}${line}${eol}`
      writeFileSync(file, next, 'utf8')
    } catch (e) {
      log(`索引更新失败 ${root}: ${String(e).slice(0, 160)}`)
    }
  }

  /**
   * 把新稿归档进 git 仓，并给两处 MEMORY.md 补索引。
   * 2026-10-29 案底：`--force` 重转会把存档里已是「清洗后成品」的稿覆盖成新 ASR 原稿（丢补丁），
   * 所以存档里已有同名稿时默认**保留**原样，只有显式 overwrite 才覆盖。
   */
  function archiveDoc(bvid: string, srcPath?: string, opts: { overwrite?: boolean } = {}): { archived: boolean; kept: boolean } {
    const src = srcPath || join(P().landing, docName(bvid))
    const dst = join(P().archive, docName(bvid))
    let archived = false
    let kept = false
    try {
      if (existsSync(src)) {
        ensureDir(P().archive)
        if (!existsSync(dst)) {
          copyFileSync(src, dst)
          archived = true
        } else if (opts.overwrite) {
          if (!readFileSync(src).equals(readFileSync(dst))) {
            copyFileSync(src, dst)
            archived = true
          }
        } else if (!readFileSync(src).equals(readFileSync(dst))) {
          kept = true
          log(`存档已有 ${bvid}，保留原稿不覆盖（要覆盖请显式 overwrite）`)
        }
      }
    } catch (e) {
      log(`归档失败 ${bvid}: ${String(e).slice(0, 200)}`)
    }
    ensureIndexLine(P().landing, bvid)
    ensureIndexLine(P().archive, bvid)
    return { archived, kept }
  }

  /**
   * 把存档里有、落点里没有的稿补进落点。
   * 清洗脚本（queue/snapshot/verify/punct_apply/apply_smoothing）都按「落点 + 存档」两副本工作，
   * 而旧主库已退役 → 开批前先从存档 hydrate，脚本不用改。
   */
  function hydrateDocs(bvids: string[]): { copied: number; missing: string[] } {
    let copied = 0
    const missing: string[] = []
    for (const bvid of bvids) {
      const dst = join(P().landing, docName(bvid))
      if (existsSync(dst)) continue
      const src = join(P().archive, docName(bvid))
      if (!existsSync(src)) {
        missing.push(bvid)
        continue
      }
      try {
        ensureDir(P().landing)
        copyFileSync(src, dst)
        copied += 1
      } catch (e) {
        log(`hydrate 失败 ${bvid}: ${String(e).slice(0, 160)}`)
        missing.push(bvid)
      }
    }
    return { copied, missing }
  }

  /**
   * 标点通道的「需要处理」口径：只统计进过 punct 队列的稿子。
   * punct_queue.py 只挑「最长无标点连读 > --min-run（默认 2000）」的稿子派单，
   * 本来就带标点的稿子永远不会入队 → 用「未入队」区分「没做」和「不用做」。
   */
  function punctQueueStats(): { queued: number; undone: number } {
    const led = readJson<Record<string, any>>(ledgerPath(), {})
    const punct = (led.punct as Record<string, any>) || {}
    const seen = new Set<string>()
    let undone = 0
    try {
      for (const f of readdirSync(P().state)) {
        if (!/^punct-queue-batch-\d+\.json$/.test(f)) continue
        const q = readJson<Array<{ bvid: string }>>(join(P().state, f), [])
        for (const x of q) {
          const b = String(x?.bvid ?? '')
          if (!b || seen.has(b)) continue
          seen.add(b)
          if (String(punct[b]?.status) !== 'done') undone += 1
        }
      }
    } catch { /* P().state 读不到就按 0 算 */ }
    return { queued: seen.size, undone }
  }

  function ledgerSummary(): Record<string, unknown> {
    const led = readJson<Record<string, any>>(ledgerPath(), {})
    const batches = (led.batches as Record<string, any>) || {}
    const docs = (led.docs as Record<string, any>) || {}
    const punct = (led.punct as Record<string, any>) || {}
    let docsDone = 0
    for (const k of Object.keys(docs)) if (DONE_STATUS.has(String(docs[k]?.status))) docsDone += 1
    let punctDone = 0
    for (const k of Object.keys(punct)) if (String(punct[k]?.status) === 'done') punctDone += 1
    const ids = Object.keys(batches).map(Number).filter((n) => Number.isFinite(n)).sort((a, b) => a - b)
    const lastId = ids.length ? ids[ids.length - 1] : null
    const last = lastId === null ? null : batches[String(lastId)]
    return {
      ledgerPath: ledgerPath(),
      batches: ids.length,
      batchIds: ids,
      lastBatchId: lastId,
      lastBatch: last
        ? {
            at: last.at ?? null,
            queued: Array.isArray(last.queued) ? last.queued.length : null,
            applied: last.applied ?? null,
            skipped: last.skipped ?? null,
            totalPatches: last.total_patches ?? null,
            totalDropped: last.total_dropped ?? null,
            recordedAt: last.recorded_at ?? null,
          }
        : null,
      docsRecorded: Object.keys(docs).length,
      docsDone,
      punctDone,
    }
  }

  // ────────────────────────────────────────────────────────────────── 门禁
  function runGates(): Record<string, unknown> {
    // 两库目前带着 52 个历史缺陷（镜像独有 51 篇 + MEMORY.md 哈希不一致），
    // 裸跑 corpus_lint.py 必然 FAIL，gateBlocked 一落盘就把后续批次永久锁死。
    // 正确口径：baseline 记录“已知状态”，门禁只对“相对 baseline 的新增回归”红。
    // 判的是「产物存档」（GitHub 的 dufuzhixin/memory，真正的产品），单根跑；
    // 判的是唯一校准源（存档）；新稿落点只是临时落脚，不单独进门禁。
    const baselineArgs = ['--root', P().archive, '--baseline', lintBaseline()]
    if (!existsSync(lintBaseline())) {
      const seed = run(P().python, ['corpus_lint.py', '--update-baseline', ...baselineArgs], { cwd: P().lintDir, timeoutMs: 900000 })
      log(`lint baseline 初始化 rc=${seed.rc} -> ${lintBaseline()}`)
    }
    const lint = run(P().python, ['corpus_lint.py', ...baselineArgs], { cwd: P().lintDir, timeoutMs: 900000 })
    const lintPass = lint.rc === 0
      && /RESULT:\s*PASS/.test(lint.out)
      && /NEW REGRESSIONS:\s*none/.test(lint.out)
    const st = run(P().python, ['corpus_lint_selftest.py'], { cwd: P().lintDir, timeoutMs: 300000 })
    const selftestPass = /SELFTEST PASS/.test(st.out)
    const ok = lintPass && selftestPass
    lastGate = { ok, at: nowIso(), lintRc: lint.rc, lintPass, selftestPass }
    gateBlocked = !ok
    saveState()
    log(`gates ok=${ok} lintRc=${lint.rc} lintPass=${lintPass} selftestPass=${selftestPass}`)
    return {
      at: nowIso(),
      cwd: P().lintDir,
      pass: ok,
      lint: {
        rc: lint.rc, pass: lintPass, ms: lint.ms,
        resultLine: grabLine(lint.out, /RESULT:/),
        regressionsLine: grabLine(lint.out, /NEW REGRESSIONS:/),
        error: lint.error,
        tail: lintPass ? null : tail(lint.out),
      },
      selftest: {
        rc: st.rc, pass: selftestPass, ms: st.ms,
        resultLine: grabLine(st.out, /SELFTEST/),
        error: st.error,
        tail: selftestPass ? null : tail(st.out),
      },
      // 门禁失败 → 拒绝开下一批（规格 §0.3）
      nextBatchAllowed: ok,
    }
  }

  // ────────────────────────────────────────────────────────────── LLM 调用面
  async function resolveRoute(): Promise<{ provider: string; model: string } | null> {
    if (E.DSH_CORPUS_PROVIDER && E.DSH_CORPUS_MODEL) {
      return { provider: E.DSH_CORPUS_PROVIDER, model: E.DSH_CORPUS_MODEL }
    }
    if (lastRoute) return lastRoute
    try {
      const providers = ctx.llm.listProviders() || []
      for (const p of providers) {
        try {
          const models = await ctx.llm.listModels(p.id)
          if (models && models.length > 0) return { provider: p.id, model: models[0].id }
        } catch { /* 该路由模型目录不可用，试下一个 */ }
      }
    } catch { /* 无 llm 服务 */ }
    return null
  }

  /** 校验推理档位：模型不支持就整条省略（省略=提供方默认），别让整批因一个旋钮失败。 */
  async function resolveEffort(route: { provider: string; model: string }): Promise<string | null> {
    const want = E.DSH_CORPUS_REASONING_EFFORT ?? 'off'
    if (!want || want === 'default') return null
    if (typeof ctx.llm.resolveCallConfig !== 'function') return null
    try {
      await ctx.llm.resolveCallConfig({ provider: route.provider, model: route.model, reasoningEffort: want }, AbortSignal.timeout(20000))
      return want
    } catch {
      log(`reasoningEffort=${want} 不被 ${route.provider}/${route.model} 接受 → 省略该参数`)
      return null
    }
  }

  interface LlmResult { text: string; usage: { input: number; output: number } | null; ms: number }

  async function callLlm(system: string, user: string, maxTokens: number): Promise<LlmResult> {
    const route = await resolveRoute()
    if (!route) throw new Error('没有可用 LLM 路由（ctx.llm 未注册适配器，且未捕获到主模型调用）')
    const effort = await resolveEffort(route)
    const opts: Record<string, unknown> = {
      provider: route.provider,
      model: route.model,
      system,
      messages: [{ role: 'user', content: [{ type: 'text', text: user }] }],
      temperature: 0,
      maxTokens,
    }
    if (effort) opts.reasoningEffort = effort
    const started = Date.now()
    let text = ''
    let usage: LlmResult['usage'] = null
    const stream = ctx.llm.stream(opts)
    for await (const chunk of stream) {
      const t = chunk?.type
      if (t === 'text-delta') text += String(chunk.text ?? '')
      else if (t === 'usage' && chunk.usage) {
        usage = {
          input: Number(chunk.usage.inputTokens) || 0,
          output: Number(chunk.usage.outputTokens) || 0,
        }
      } else if (t === 'finish') {
        const reason = chunk.reason
        if (reason && (reason.kind === 'error' || reason.kind === 'aborted')) {
          const f = reason.failure || {}
          throw new Error(`LLM ${reason.kind}: ${f.code || '?'} ${f.message || ''}`.trim())
        }
      }
    }
    tokens.calls += 1
    if (usage) { tokens.input += usage.input; tokens.output += usage.output }
    return { text, usage, ms: Date.now() - started }
  }

  // ─────────────────────────────────────────────────────────── 补丁解析/写盘
  function parsePatches(raw: string, bvid: string): Array<Record<string, unknown>> {
    let t = String(raw || '').trim()
    const fence = t.match(/```(?:json)?\s*([\s\S]*?)```/)
    if (fence) t = fence[1].trim()
    const i = t.indexOf('{')
    const j = t.lastIndexOf('}')
    if (i < 0 || j <= i) throw new Error('未找到 JSON 对象')
    const obj = JSON.parse(t.slice(i, j + 1)) as Record<string, unknown>
    if (obj.bvid && String(obj.bvid) !== bvid) throw new Error(`bvid 不匹配（${String(obj.bvid)}）`)
    const list = Array.isArray(obj.patches) ? obj.patches : null
    if (list === null) throw new Error('缺 patches 数组')
    const out: Array<Record<string, unknown>> = []
    for (const p of list as Array<Record<string, any>>) {
      if (!p || typeof p.find !== 'string' || typeof p.replace !== 'string') continue
      if (p.find.length === 0) continue
      if (p.replace.includes('...') || p.replace.includes('…')) continue // 铁律：不许省略号
      const kind = String(p.kind || 'other')
      const patch: Record<string, unknown> = {
        find: p.find,
        replace: p.replace,
        kind: KINDS.has(kind) ? kind : 'other',
        why: String(p.why || ''),
      }
      if (p.all === true) patch.all = true
      if (Number.isInteger(p.occurrence) && p.occurrence > 0) patch.occurrence = p.occurrence
      out.push(patch)
    }
    return out
  }

  /** 专名核实（可选）：对 kind=name 的补丁取前 3 条各搜一次，把证据写进 why。 */
  async function verifyNames(bvid: string, body: string, patches: Array<Record<string, unknown>>): Promise<number> {
    const web = (() => { try { return ctx.get('web') } catch { return undefined } })()
    if (!web || typeof web.search !== 'function') return 0
    let checked = 0
    for (const p of patches) {
      if (checked >= 3) break
      if (p.kind !== 'name') continue
      const cand = String(p.replace)
      const at = body.indexOf(String(p.find))
      const keyword = at >= 0 ? body.slice(Math.max(0, at - 14), at + String(p.find).length + 14).replace(/\s+/g, ' ') : ''
      try {
        const res = await web.search({ query: `${cand} ${keyword}`.trim(), maxResults: 3 })
        const sources = (res && (res.sources || res.results)) || []
        if (Array.isArray(sources) && sources.length > 0) {
          const ev = sources.slice(0, 2).map((s: any) => [s.title, s.url].filter(Boolean).join(' ')).filter(Boolean).join(' | ')
          if (ev) p.why = `${p.why || ''}（web 核实：${ev}）`.trim()
        } else if (res && typeof res.content === 'string' && res.content.length > 0) {
          p.why = `${p.why || ''}（web 依据：${res.content.slice(0, 120)}）`.trim()
        }
        checked += 1
      } catch (e) {
        log(`web 核实跳过（${bvid} ${cand}）: ${String(e).slice(0, 120)}`)
      }
    }
    return checked
  }

  /** 单篇：读正文 → LLM → 解析（失败重试 1 次）→ 写补丁文件。任何失败只记 no_patch。 */
  async function processDoc(batchNo: number, bvid: string): Promise<'patched' | 'empty' | 'no_patch'> {
    const file = findDoc(bvid)
    if (!file) {
      log(`找不到稿件 ${bvid}（新稿落点/存档都没有）`)
      return 'no_patch'
    }
    let raw: string
    try {
      raw = readFileSync(file, 'utf8')
    } catch (e) {
      log(`读正文失败 ${bvid}: ${String(e).slice(0, 120)}`)
      return 'no_patch'
    }
    const fm = raw.match(/^---\r?\n[\s\S]*?\r?\n---\r?\n/)
    const offset = fm ? fm[0].length : 0
    const body = raw.slice(offset).slice(0, BODY_LIMIT)
    const user = `bvid: ${bvid}\n文件: ${file}\n\n${body}`
    let patches: Array<Record<string, unknown>> | null = null
    let parsed: string | null = null
    for (let attempt = 1; attempt <= 2 && patches === null; attempt += 1) {
      try {
        const sys = attempt === 1
          ? SYSTEM_PROMPT
          : `${SYSTEM_PROMPT}\n\n注意：上一次输出不是合法 JSON。这一次**只输出一个 JSON 对象**，不要任何多余文字。`
        const r = await callLlm(sys, user, 4000)
        patches = parsePatches(r.text, bvid)
        parsed = r.text
      } catch (e) {
        if (attempt === 2) {
          log(`LLM/解析失败（记 no_patch 继续）${bvid}: ${String(e).slice(0, 200)}`)
          return 'no_patch'
        }
      }
    }
    if (patches === null) return 'no_patch'
    if (patches.length === 0) return 'empty'
    try {
      await verifyNames(bvid, body, patches)
    } catch { /* 核实只是加分项，绝不阻断 */ }
    const dir = join(SCRATCH, '_patches', `batch-${batchNo}`)
    ensureDir(dir)
    writeJson(join(dir, `${bvid}.json`), {
      bvid,
      patches,
      notes: `cockpit 批次 ${batchNo}；模型原文长度 ${parsed ? parsed.length : 0}`,
    })
    return 'patched'
  }

  // ─────────────────────────────────────────────────────── 错字通道（error）
  async function runErrorBatch(opts: { size: number; batch?: number; dryRun: boolean }): Promise<Record<string, unknown>> {
    const state = readJson<Record<string, any>>(ledgerPath(), {})
    const batches = (state.batches as Record<string, any>) || {}
    let maxN = 0
    for (const k of Object.keys(batches)) {
      const n = Number(k)
      if (Number.isFinite(n) && n > maxN) maxN = n
    }
    try {
      for (const f of readdirSync(P().state)) {
        const m = f.match(/^queue-batch-(\d+)\.json$/)
        if (m) maxN = Math.max(maxN, Number(m[1]))
      }
    } catch { /* P().state 不可读 */ }
    const batchNo = Number.isFinite(opts.batch as number) ? Number(opts.batch) : maxN + 1

    inFlight = { kind: 'error', batch: batchNo, phase: 'queue', startedAt: nowIso() }
    const q = run(P().python, [BATCH_SCRIPT, 'queue', '--batch', String(batchNo), '--n', String(opts.size)], { cwd: SCRATCH, timeoutMs: 300000 })
    if (q.rc !== 0) {
      return { ok: false, batch: batchNo, kind: 'error', phase: 'queue', error: `queue rc=${q.rc}`, tail: tail(q.out) }
    }
    const qf = join(P().state, `queue-batch-${batchNo}.json`)
    const queue = readJson<Array<{ bvid: string }>>(qf, [])
    // 旧主库已退役：清洗脚本按「落点 + 存档」两副本工作，缺的从存档补进落点。
    const hyd = hydrateDocs(queue.map((x) => String(x.bvid)))
    if (hyd.copied > 0) log(`批次 ${batchNo}：从存档补齐 ${hyd.copied} 篇到落点`)
    const snap = run(P().python, [BATCH_SCRIPT, 'snapshot', '--batch', String(batchNo)], { cwd: SCRATCH, timeoutMs: 600000 })
    if (snap.rc !== 0) {
      return { ok: false, batch: batchNo, kind: 'error', phase: 'snapshot', error: `snapshot rc=${snap.rc}`, tail: tail(snap.out) }
    }

    // 逐篇串行（1 篇 1 次 LLM 调用）；单篇失败只记 no_patch，不炸整批
    let patched = 0; let empty = 0; let noPatch = 0
    const t0 = Date.now()
    for (const item of queue) {
      const bvid = String(item.bvid)
      inFlight = { kind: 'error', batch: batchNo, phase: `llm ${patched + empty + noPatch + 1}/${queue.length} ${bvid}`, startedAt: nowIso() }
      const r = await processDoc(batchNo, bvid)
      if (r === 'patched') patched += 1
      else if (r === 'empty') empty += 1
      else noPatch += 1
    }

    inFlight = { kind: 'error', batch: batchNo, phase: 'verify', startedAt: nowIso() }
    const ver = run(P().python, [BATCH_SCRIPT, 'verify', '--batch', String(batchNo)], { cwd: SCRATCH, timeoutMs: 900000 })
    if (ver.rc !== 0) {
      return {
        ok: false, batch: batchNo, kind: 'error', phase: 'verify', status: 'FAILED',
        error: `守卫失败（rc=${ver.rc}）：本批语料在 LLM 阶段被改动——绝不允许`, tail: tail(ver.out),
        queued: queue.length, docsWithPatches: patched, empty, noPatch,
      }
    }

    inFlight = { kind: 'error', batch: batchNo, phase: 'apply', startedAt: nowIso() }
    const patchDir = join(SCRATCH, '_patches', `batch-${batchNo}`)
    const reportFile = join(P().state, `report-batch-${batchNo}.json`)
    const applyArgs = [
      APPLY_SCRIPT, '--dir', patchDir,
      '--snap', join(P().state, `snap-batch-${batchNo}.json`),
      '--report', reportFile,
      '--drop-bad', '--guard',
    ]
    if (opts.dryRun) applyArgs.push('--dry-run')
    const ap = run(P().python, applyArgs, { cwd: SCRATCH, timeoutMs: 900000 })
    if (ap.rc !== 0) {
      return { ok: false, batch: batchNo, kind: 'error', phase: 'apply', status: 'FAILED', error: `apply rc=${ap.rc}`, tail: tail(ap.out) }
    }
    const report = readJson<Record<string, any>>(reportFile, {})
    if (!opts.dryRun) {
      const rec = run(P().python, [BATCH_SCRIPT, 'record', '--batch', String(batchNo)], { cwd: SCRATCH, timeoutMs: 300000 })
      if (rec.rc !== 0) {
        return { ok: false, batch: batchNo, kind: 'error', phase: 'record', status: 'FAILED', error: `record rc=${rec.rc}`, tail: tail(rec.out) }
      }
    }

    inFlight = { kind: 'error', batch: batchNo, phase: 'gates', startedAt: nowIso() }
    const gate = runGates()
    const result: Record<string, unknown> = {
      ok: Boolean(gate.pass),
      status: gate.pass ? 'DONE' : 'FAILED',
      batch: batchNo,
      kind: 'error',
      size: opts.size,
      dryRun: opts.dryRun,
      queued: queue.length,
      hydrated: hyd.copied,
      missingDocs: hyd.missing.slice(0, 10),
      docsWithPatches: patched,
      docsEmpty: empty,
      docsNoPatch: noPatch,
      patches: report.total_patches ?? null,
      dropped: report.total_dropped ?? null,
      applied: Array.isArray(report.reports) ? report.reports.filter((r: any) => r && r.ok).length : null,
      gate: {
        lintRc: (gate.lint as any).rc, lintPass: (gate.lint as any).pass,
        selftestPass: (gate.selftest as any).pass, pass: gate.pass,
      },
      tokens: { input: tokens.input, output: tokens.output, calls: tokens.calls },
      elapsedMs: Date.now() - t0,
      at: nowIso(),
      nextBatchAllowed: gate.pass,
    }
    lastBatch = result
    return result
  }

  // ─────────────────────────────────────────────────────── 标点通道（punct）
  function pickPunctBatch(): number | null {
    const led = readJson<Record<string, any>>(ledgerPath(), {})
    const punct = (led.punct as Record<string, any>) || {}
    const waves: number[] = []
    try {
      for (const f of readdirSync(P().state)) {
        const m = f.match(/^punct-queue-batch-(\d+)\.json$/)
        if (m) waves.push(Number(m[1]))
      }
    } catch { /* ignore */ }
    waves.sort((a, b) => a - b)
    for (const n of waves) {
      const q = readJson<Array<{ bvid: string }>>(join(P().state, `punct-queue-batch-${n}.json`), [])
      const undone = q.filter((x) => String(punct[String(x.bvid)]?.status) !== 'done')
      if (undone.length > 0) return n
    }
    return waves.length ? waves[waves.length - 1] : null
  }

  async function runPunctBatch(opts: { batch?: number; dryRun: boolean }): Promise<Record<string, unknown>> {
    const batchNo = Number.isFinite(opts.batch as number) ? Number(opts.batch) : pickPunctBatch()
    if (batchNo === null) {
      return { ok: false, kind: 'punct', error: '找不到可跑的点标波（P().state 下没有 punct-queue-batch-N.json）' }
    }
    const qf = join(P().state, `punct-queue-batch-${batchNo}.json`)
    const queue = readJson<Array<{ bvid: string }>>(qf, [])
    // 同上：标点脚本也按两副本工作，先把存档里的稿补进落点。
    const hyd = hydrateDocs(queue.map((x) => String(x.bvid)))
    if (hyd.copied > 0) log(`punct 批次 ${batchNo}：从存档补齐 ${hyd.copied} 篇到落点`)
    inFlight = { kind: 'punct', batch: batchNo, phase: 'local-model', startedAt: nowIso() }
    const local = run(P().pythonAsr, [PUNCT_LOCAL, '--batch', String(batchNo)], {
      cwd: SCRATCH,
      timeoutMs: Number(E.DSH_CORPUS_PUNCT_TIMEOUT_MS || 4 * 3600 * 1000),
    })
    if (local.rc !== 0) {
      return { ok: false, batch: batchNo, kind: 'punct', phase: 'local', status: 'FAILED', error: `punct_local rc=${local.rc}`, tail: tail(local.out) }
    }
    inFlight = { kind: 'punct', batch: batchNo, phase: 'apply', startedAt: nowIso() }
    const applyArgs = [PUNCT_APPLY, '--batch', String(batchNo)]
    if (opts.dryRun) applyArgs.push('--dry-run')
    const ap = run(P().python, applyArgs, { cwd: SCRATCH, timeoutMs: 1800000 })
    if (ap.rc !== 0) {
      return { ok: false, batch: batchNo, kind: 'punct', phase: 'apply', status: 'FAILED', error: `punct_apply rc=${ap.rc}`, tail: tail(ap.out) }
    }
    const report = readJson<Array<Record<string, any>>>(join(P().state, `report-punct-${batchNo}.json`), [])
    const landed = report.filter((r) => r && r.ok).length
    const rejected = report.filter((r) => r && !r.ok).length
    inFlight = { kind: 'punct', batch: batchNo, phase: 'gates', startedAt: nowIso() }
    const gate = runGates()
    const result: Record<string, unknown> = {
      ok: Boolean(gate.pass),
      status: gate.pass ? 'DONE' : 'FAILED',
      batch: batchNo,
      kind: 'punct',
      dryRun: opts.dryRun,
      queued: queue.length,
      hydrated: hyd.copied,
      missingDocs: hyd.missing.slice(0, 10),
      landed,
      rejected,
      gate: {
        lintRc: (gate.lint as any).rc, lintPass: (gate.lint as any).pass,
        selftestPass: (gate.selftest as any).pass, pass: gate.pass,
      },
      at: nowIso(),
      nextBatchAllowed: gate.pass,
    }
    lastBatch = result
    return result
  }

  /** 批次互斥 + 门禁闸门 + 暂停闸门。 */
  async function runBatch(kind: string, opts: { size?: number; batch?: number; dryRun?: boolean; corpus?: string }): Promise<Record<string, unknown>> {
    if (inFlight) return { ok: false, error: 'BUSY', detail: inFlight, message: '同一时刻只允许一个批次在跑' }
    if (isPaused()) return { ok: false, error: 'PAUSED', message: '驾驶舱已暂停（corpus_paused.flag 存在）——先 corpus_resume' }
    if (gateBlocked) {
      return {
        ok: false, error: 'GATE_FAILED',
        message: '上一批门禁未通过 → 拒绝开始下一批（先修问题再跑 corpus_gates 复验）',
        lastGate,
      }
    }
    const prevActive = ACTIVE_ID
    if (opts.corpus) ACTIVE_ID = profileOf(opts.corpus).id
    try {
      if (kind === 'punct') return await runPunctBatch({ batch: opts.batch, dryRun: Boolean(opts.dryRun) })
      return await runErrorBatch({ size: Number(opts.size) || 24, batch: opts.batch, dryRun: Boolean(opts.dryRun) })
    } catch (e) {
      log(`批次异常: ${String(e)}`)
      return { ok: false, error: 'EXCEPTION', message: String(e), status: 'FAILED' }
    } finally {
      inFlight = null
      ACTIVE_ID = prevActive
      saveState()
    }
  }

  // ══════════════════════════════════════════════════════ 转写线（B站 → 本地）
  // 一次只跑一个任务（ASR 吃 CPU/GPU，并发只会互相拖慢）；进度从 bridge.py 的
  // stdout JSON 行来，面板按 jobId 轮询。
  type BiliJob = {
    id: string
    bvid: string
    url: string | null
    state: 'queued' | 'running' | 'done' | 'failed'
    stage: string
    message: string
    path: string | null
    duration: number | null
    textLength: number | null
    cached: boolean
    force: boolean
    /** 入队时绑定的语料（执行期间按它切 profile，避免中途切换语料串库）。 */
    corpus?: string
    createdAt: string
    startedAt: string | null
    endedAt: string | null
  }

  /**
   * 任务台账：每个语料自己的 `state/bili-jobs/jobs.json`，内存里按 id 合并成一张全局表。
   * 以前用「单个全局数组 + 当前语料路径」，切语料时会把 A 的任务写进 B 的目录。
   */
  const jobsFileOf = (id: string): string => join(biliJobsDir(profileOf(id)), 'jobs.json')
  let jobs: BiliJob[] = []
  const loadAllJobs = (): void => {
    const merged = new Map<string, BiliJob>()
    const ids = Object.keys(loadProfiles())
    for (const id of ids.length ? ids : [profileOf().id]) {
      const list = readJson<BiliJob[]>(jobsFileOf(id), [])
      if (!Array.isArray(list)) continue
      for (const j of list) merged.set(j.id, { ...j, corpus: j.corpus || id })
    }
    jobs = Array.from(merged.values()).sort((a, b) => String(a.createdAt).localeCompare(String(b.createdAt)))
  }
  loadAllJobs()
  /** 某语料名下的任务（旧记录没有 corpus 字段，归 dufuzhixin）。 */
  const jobsOf = (id: string = profileOf().id): BiliJob[] => jobs.filter((j) => (j.corpus || 'dufuzhixin') === id)
  let biliBusy: BiliJob | null = null
  const BILI_TIMEOUT_MS = Number(E.DSH_BILI_TIMEOUT_MS || 4 * 60 * 60 * 1000)
  const BILI_MAX_JOBS = 200

  /** 语料切换后重挂运行时状态（否则面板会显示上一个语料的「完成 N」与门禁记忆）。 */
  onCorpusChanged = () => {
    const running = jobs.find((j) => j.state === 'running')
    biliBusy = running && (running.corpus || 'dufuzhixin') === profileOf().id ? running : (running ? biliBusy : null)
    const p = readJson<Record<string, any>>(cockpitState(), {})
    lastGate = p && p.lastGate && typeof p.lastGate.ok === 'boolean' ? p.lastGate : null
    lastBatch = p && p.lastBatch ? p.lastBatch : null
    gateBlocked = p ? p.gateBlocked === true : false
    tokens = p && typeof p.tokens === 'object'
      ? { input: Number(p.tokens.input) || 0, output: Number(p.tokens.output) || 0, calls: Number(p.tokens.calls) || 0 }
      : { input: 0, output: 0, calls: 0 }
  }

  const saveJobs = (corpusId?: string): void => {
    try {
      const id = corpusId || (biliBusy?.corpus ?? profileOf().id)
      const partition = jobsOf(id)
      ensureDir(biliJobsDir(profileOf(id)))
      writeJson(jobsFileOf(id), partition.slice(-BILI_MAX_JOBS))
    } catch { /* 台账落盘失败不致命 */ }
  }
  const findJob = (id: string): BiliJob | null => jobs.find((j) => j.id === id) ?? null
  /** 输入可以是 BVID、av 号、完整链接或整段分享文案。 */
  const extractBvid = (raw: string): string | null => {
    const m = String(raw || '').match(/BV[0-9A-Za-z]{10}/)
    return m ? m[0] : null
  }

  function biliSummary(): Record<string, unknown> {
    const list = jobsOf()
    const queued = list.filter((j) => j.state === 'queued').length
    const running = list.filter((j) => j.state === 'running').length
    const done = list.filter((j) => j.state === 'done').length
    const failed = list.filter((j) => j.state === 'failed').length
    const busy = biliBusy && (biliBusy.corpus || 'dufuzhixin') === profileOf().id ? biliBusy : null
    return {
      total: list.length, queued, running, done, failed,
      busy: busy ? { id: busy.id, bvid: busy.bvid, stage: busy.stage } : null,
      cachedFiles: countRoot(P().archive).dufu,
      landingFiles: countRoot(P().landing).dufu,
      recent: list.slice(-8).reverse().map((j) => ({
        id: j.id, bvid: j.bvid, state: j.state, stage: j.stage,
        message: j.message, duration: j.duration, textLength: j.textLength, cached: j.cached,
        startedAt: j.startedAt, endedAt: j.endedAt,
      })),
      skill: {
        kind: BILI_ENGINE_KIND,
        dir: BILI_ENGINE_DIR,
        externalDir: BILI_SKILL_DIR || null,
        python: PY_BILI,
        bridge: BRIDGE,
        pythonExists: existsSync(PY_BILI),
        bridgeExists: existsSync(BRIDGE),
        modelExists: existsSync(BILI_MODEL_FILE),
        vadExists: existsSync(BILI_VAD_MODEL),
        ffmpegExists: existsSync(BILI_FFMPEG),
        ffprobeExists: existsSync(BILI_FFPROBE),
      },
    }
  }

  /** 跑一次 bridge.py；stdout 每行一个 JSON（stage / result）。 */
  function runBridge(job: BiliJob): Promise<void> {
    return new Promise((resolve) => {
      const args = [
        BRIDGE, '--bvid', job.bvid, '--out', P().landing, '--temp', join(P().state, 'bili-temp'),
        '--lang', 'zh',
      ]
      if (BILI_SKILL_DIR) args.push('--skill-dir', BILI_SKILL_DIR)
      if (job.force) args.push('--force')
      job.state = 'running'
      job.stage = 'start'
      job.message = '启动转写'
      job.startedAt = nowIso()
      saveJobs(job.corpus)
      log(`转写开始 ${job.id} ${job.bvid}${job.force ? ' (force)' : ''}`)
      let stdout = ''
      let stderr = ''
      let settled = false
      let timer: ReturnType<typeof setTimeout> | null = null
      const finish = (patch: Partial<BiliJob>, errMsg?: string): void => {
        if (settled) return
        settled = true
        if (timer) clearTimeout(timer)
        Object.assign(job, patch)
        job.endedAt = nowIso()
        if (errMsg) log(`转写失败 ${job.id} ${job.bvid}: ${errMsg.slice(0, 300)}`)
        else log(`转写结束 ${job.id} ${job.bvid} state=${job.state} len=${job.textLength ?? '?'}`)
        biliBusy = null
        saveJobs(job.corpus)
        resolve()
      }
      // 命中缓存：三库任一有 ≥3000 字的同名稿就秒回（bridge 自己的缓存只看 --out 一个目录）。
      if (!job.force) {
        const hit = findDoc(job.bvid)
        if (hit && docChars(hit) >= 3000) {
          const len = docChars(hit)
          const arch = archiveDoc(job.bvid, hit)
          finish({
            state: 'done', stage: 'done', path: join(P().archive, docName(job.bvid)),
            textLength: len, cached: true,
            message: `命中缓存（${basename(dirname(hit))}）${arch.archived ? ' · 已补归档' : arch.kept ? ' · 存档已有稿（保留）' : ''}`,
          })
          return
        }
      }
      timer = setTimeout(() => {
        try { child.kill('SIGKILL') } catch { /* ignore */ }
        finish({ state: 'failed', stage: 'timeout', message: `超时（${BILI_TIMEOUT_MS} ms）` }, 'timeout')
      }, BILI_TIMEOUT_MS)
      let child: ReturnType<typeof spawn>
      try {
        child = spawn(PY_BILI, args, {
          cwd: P().state,
          windowsHide: true,
          env: {
            ...process.env,
            // Python 在 Windows 默认按 cp936 写 stdout —— Node 按 UTF-8 读会乱码。
            PYTHONUTF8: '1',
            PYTHONIOENCODING: 'utf-8',
            SENSEVOICE_FFMPEG: BILI_FFMPEG,
            SENSEVOICE_MODEL_DIR: BILI_MODEL_DIR,
            SENSEVOICE_VAD_MODEL: BILI_VAD_MODEL,
          },
        })
      } catch (e) {
        finish({ state: 'failed', stage: 'spawn', message: String(e) }, String(e))
        return
      }
      child.stdout?.setEncoding('utf8')
      child.stderr?.setEncoding('utf8')
      child.stdout?.on('data', (chunk: string) => {
        stdout += chunk
        let idx: number
        while ((idx = stdout.indexOf('\n')) >= 0) {
          const line = stdout.slice(0, idx).trim()
          stdout = stdout.slice(idx + 1)
          if (!line.startsWith('{')) continue
          try {
            const msg = JSON.parse(line) as Record<string, any>
            job.message = String(msg.msg ?? msg.message ?? job.message)
            if (msg.type === 'stage') {
              job.stage = String(msg.stage || job.stage)
            } else if (msg.type === 'result') {
              if (msg.ok === true) {
                // 落点在 P().landing（临时落脚）；真正的产物要归档进 git 仓 + 补索引。
                const arch = msg.cached === true ? { archived: false, kept: false } : archiveDoc(job.bvid)
                const productPath = join(P().archive, docName(job.bvid))
                finish({
                  state: 'done', stage: 'done',
                  path: existsSync(productPath) ? productPath : (msg.path ? String(msg.path) : null),
                  duration: msg.duration != null ? Number(msg.duration) : null,
                  textLength: msg.textLength != null ? Number(msg.textLength) : null,
                  cached: msg.cached === true,
                  message: msg.cached
                    ? '命中缓存'
                    : `完成（${Number(msg.textLength) || 0} 字）${arch.archived ? ' · 已归档' : arch.kept ? ' · 存档已有稿（保留，未覆盖）' : ''}`,
                })
                return
              }
              const code = String(msg.error || 'UNKNOWN')
              finish({ state: 'failed', stage: code, message: String(msg.message || code) }, `${code} ${msg.message || ''}`)
              return
            }
          } catch { /* 非 JSON 行忽略 */ }
        }
      })
      child.stderr?.on('data', (chunk: string) => { stderr = (stderr + chunk).slice(-4000) })
      child.on('error', (e: Error) => finish({ state: 'failed', stage: 'spawn', message: String(e) }, String(e)))
      child.on('close', (code: number | null) => {
        if (code !== 0 && job.state !== 'failed' && job.state !== 'done') {
          finish({ state: 'failed', stage: 'exit', message: `退出码 ${code}：${tail(stderr, 300)}` }, stderr)
        } else if (job.state === 'running') {
          finish({ state: 'failed', stage: 'exit', message: `结束但无结果行：${tail(stderr, 300)}` }, 'no-result')
        }
      })
    })
  }

  /** 队列泵：空闲时取下一条 queued 串行执行（按任务入队时的语料绑定，防串库）。 */
  async function drainBiliQueue(): Promise<void> {
    while (!biliBusy) {
      const next = jobs.find((j) => j.state === 'queued')
      if (!next) return
      biliBusy = next
      await bindCorpusAsync(next.corpus, () => runBridge(next))
    }
  }

  function enqueueBili(input: string, force: boolean, corpus?: string): { ok: true; job: BiliJob } | { ok: false; error: string } {
    const bvid = extractBvid(input)
    if (!bvid) return { ok: false, error: `无法从输入里认出 BVID：${String(input).slice(0, 80)}` }
    const existing = jobs.find((j) => j.bvid === bvid && (j.state === 'queued' || j.state === 'running'))
    if (existing) return { ok: true, job: existing }
    const job: BiliJob = {
      id: `bj-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`,
      bvid, url: /^https?:/.test(input) ? input : null,
      state: 'queued', stage: 'queued', message: '排队中',
      path: null, duration: null, textLength: null, cached: false, force,
      corpus: profileOf(corpus).id,
      createdAt: nowIso(), startedAt: null, endedAt: null,
    }
    jobs.push(job)
    saveJobs(job.corpus)
    log(`转写入队 ${job.id} ${bvid}`)
    void drainBiliQueue()
    return { ok: true, job }
  }

  // ───────────────────────────────────────────────────────────── 工具注册
  const textOutput = {
    schema: { type: 'string' },
    render: (_args: unknown, value: unknown) => [{ type: 'text', text: String(value) }],
  }

  /** 所有工具统一注入可选 `corpus` 参数：执行期临时切换 profile，结束恢复（防串库）。 */
  function register(tool: Record<string, any>, label?: string): void {
    const tag = label || String(tool.name ?? 'tool')
    const exec = tool.execute
    if (typeof exec === 'function') {
      tool.execute = (args: any) => bindCorpusAsync(args && args.corpus ? String(args.corpus) : undefined, () => exec(args))
    }
    const params = tool.parameters
    if (params && params.type === 'object') {
      params.properties = {
        ...(params.properties || {}),
        corpus: { type: 'string', description: '语料 id（缺省 = 当前生效语料；用 corpus_use 切换）' },
      }
    }
    try {
      ctx.effect(() => ctx.tools.register(tool), `${SHORT}: ${tag}`)
    } catch (e) {
      log(`工具注册失败 ${tag}: ${String(e)}`)
    }
  }

  register({
    name: 'corpus_use',
    description: '查看/切换当前生效语料。不带 id = 列出全部语料与当前值；带 id = 切换（写 active-corpus.json，重载插件后保持），批次/转写在跑时拒绝。',
    parameters: {
      type: 'object',
      properties: { id: { type: 'string', description: '要切换到的语料 id（缺省 = 只列出）' } },
    },
    output: textOutput,
    async execute(args: Record<string, unknown>) {
      const id = args?.id ? String(args.id) : ''
      if (!id) return JSON.stringify(corporaSummary(), null, 2)
      if (inFlight) return JSON.stringify({ ok: false, error: 'BUSY', message: `批次在跑（${inFlight}），跑完再切语料`, active: ACTIVE_ID }, null, 2)
      if (biliBusy) return JSON.stringify({ ok: false, error: 'BUSY', message: `转写任务在跑（${biliBusy.id}），跑完再切语料`, active: ACTIVE_ID }, null, 2)
      const res = switchCorpus(id)
      if (!res.ok) return JSON.stringify({ ...res, ok: false }, null, 2)
      onCorpusChanged?.()
      saveState()
      log(`切换语料 → ${id}（${P().landing}）`)
      return JSON.stringify({ ...res, ok: true, landing: P().landing, archive: P().archive, state: P().state }, null, 2)
    },
  })

  register({
    name: 'corpus_status',
    description: '语料「文意疏通」驾驶舱只读状态：错字/标点两条通道进度、新稿落点/产物存档两处文件数、最近一批、是否在跑、是否暂停、累计 token、门禁记忆。返回 JSON。',
    parameters: { type: 'object', properties: {} },
    output: textOutput,
    async execute() {
      const led = ledgerSummary()
      const main = countRoot(P().landing)
      const archive = countRoot(P().archive)
      const pq = punctQueueStats()
      const status = {
        at: nowIso(),
        ok: true,
        corpus: P().id,
        ledgerPath: led.ledgerPath,
        roots: {
          main: { dir: P().landing, role: '新稿落点（临时落脚，DSH 家目录下）', dufuFiles: main.dufu, otherFiles: main.other.length, other: main.other.slice(0, 8) },
          archive: { dir: P().archive, role: '产物存档（git 仓，真正的产品）', dufuFiles: archive.dufu, otherFiles: archive.other.length, other: archive.other.slice(0, 8) },
        },
        error: {
          channel: 'error',
          total: archive.dufu,
          batches: led.batches,
          batchIds: led.batchIds,
          docsRecorded: led.docsRecorded,
          docsDone: led.docsDone,
          docsRemaining: archive.dufu >= 0 ? archive.dufu - Number(led.docsDone) : null,
          lastBatchId: led.lastBatchId,
          lastBatch: led.lastBatch,
        },
        punct: {
          channel: 'punct',
          // total = 需要处理的篇数（进过 punct 队列的），不是全库篇数
          total: Number(led.punctDone) + pq.undone,
          done: led.punctDone,
          queued: pq.queued,
          undone: pq.undone,
          unneeded: Math.max(0, archive.dufu - Number(led.punctDone) - pq.undone),
          note: '只对「最长无标点连读 > 2000」的稿子派单；未入队 = 本来就带标点，无需处理',
        },
        running: inFlight,
        paused: isPaused(),
        pausedFlag: pausedFlag(),
        auto: AUTO,
        autoEnv: 'DSH_CORPUS_AUTO',
        gateBlocked,
        lastGate,
        lastBatch,
        tokens,
        cycles,
      }
      return JSON.stringify(status, null, 2)
    },
  })

  const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms))

  register({
    name: 'bili_transcribe',
    description: '把 B 站视频转成稿件 dufu-<bvid>.md（本地 SenseVoice 转写，不走云端）。入队一次转写（全局单并发），返回 jobId；用 bili_jobs 查进度。输入可传 BVID、视频链接或整段分享文案。',
    parameters: {
      type: 'object',
      properties: {
        target: { type: 'string', description: 'BVID / 视频链接 / 分享文案' },
        force: { type: 'boolean', description: '忽略已有缓存强制重转（默认 false：主库已有 ≥3000 字的同名稿就秒回 cached）' },
        wait: { type: 'boolean', description: '阻塞等到该任务结束（最多 30 分钟）。默认 false：立刻返回 jobId' },
      },
      required: ['target'],
    },
    output: textOutput,
    async execute(args: Record<string, unknown>) {
      const res = enqueueBili(String(args?.target ?? ''), args?.force === true, typeof args?.corpus === 'string' ? args.corpus : undefined)
      if (!res.ok) return JSON.stringify({ ok: false, error: res.error }, null, 2)
      if (args?.wait === true) {
        const deadline = Date.now() + 30 * 60 * 1000
        while (Date.now() < deadline && (res.job.state === 'queued' || res.job.state === 'running')) await sleep(3000)
      }
      return JSON.stringify({
        ok: res.job.state !== 'failed',
        jobId: res.job.id,
        job: res.job,
        hint: res.job.state === 'queued' ? '已入队；用 bili_jobs 查进度，或看面板「转写」区' : undefined,
        queue: biliSummary(),
      }, null, 2)
    },
  })

  register({
    name: 'bili_jobs',
    description: '查 B 站转写队列：不带参返回摘要 + 最近任务；传 id 返回该任务明细；clear=true 清掉已结束（done/failed）的记录。',
    parameters: {
      type: 'object',
      properties: {
        id: { type: 'string', description: '任务 id（bj-…）' },
        limit: { type: 'number', description: '最近任务条数（默认 8，最多 50）' },
        clear: { type: 'boolean', description: '清空已结束任务记录（不影响已转写文件）' },
      },
    },
    output: textOutput,
    async execute(args: Record<string, unknown>) {
      if (args?.clear === true) {
        const scoped = jobsOf()
        const keep = scoped.filter((j) => j.state === 'queued' || j.state === 'running')
        const keepIds = new Set(keep.map((j) => j.id))
        jobs = jobs.filter((j) => !scoped.some((s) => s.id === j.id) || keepIds.has(j.id))
        saveJobs()
        return JSON.stringify({ ok: true, removed: scoped.length - keep.length, left: keep.length }, null, 2)
      }
      const id = args?.id ? String(args.id) : ''
      if (id) {
        const job = findJob(id)
        return JSON.stringify(job ? { ok: true, job } : { ok: false, error: `没有任务 ${id}` }, null, 2)
      }
      const limit = Math.min(50, Math.max(1, Number(args?.limit) || 8))
      const summary = biliSummary()
      return JSON.stringify({
        ok: true,
        ...summary,
        recent: jobsOf().slice(-limit).reverse(),
      }, null, 2)
    },
  })

  register({
    name: 'corpus_mirror_sync',
    description: '把新稿落点里的 dufu-*.md 补进「产物存档」（GitHub 的 dufuzhixin/memory，git 仓）：内容不同才覆盖、缺的补上；存档里多出来的只报告不删。默认 dryRun=true 只报告。',
    parameters: {
      type: 'object',
      properties: { dryRun: { type: 'boolean', description: '默认 true 只报告' } },
    },
    output: textOutput,
    async execute(args: Record<string, unknown>) {
      const dryRun = args?.dryRun !== false
      const report = {
        archive: P().archive, landing: P().landing, dryRun,
        copied: [] as string[], updated: [] as string[], extraInArchive: [] as string[], errors: [] as string[],
      }
      // 文件名 → 绝对路径；只认新稿落点（旧主库已退役）。
      const sources = new Map<string, string>()
      for (const root of [P().landing]) {
        try {
          for (const f of readdirSync(root)) {
            if (!isDocName(f) || sources.has(f)) continue
            sources.set(f, join(root, f))
          }
        } catch { /* 目录不存在就跳过 */ }
      }
      if (sources.size === 0) return JSON.stringify({ ok: false, error: '新稿落点读不到稿件' }, null, 2)
      try { ensureDir(P().archive) } catch { /* 已在 */ }
      for (const [f, src] of sources) {
        const dst = join(P().archive, f)
        try {
          if (!existsSync(dst)) {
            report.copied.push(f)
            if (!dryRun) copyFileSync(src, dst)
            continue
          }
          if (!readFileSync(src).equals(readFileSync(dst))) {
            report.updated.push(f)
            if (!dryRun) copyFileSync(src, dst)
          }
        } catch (e) {
          report.errors.push(`${f}: ${String(e)}`)
        }
      }
      try {
        for (const f of readdirSync(P().archive)) {
          if (isDocName(f) && !sources.has(f)) report.extraInArchive.push(f)
        }
      } catch { /* 存档读不到 */ }
      log(`archive-sync dryRun=${dryRun} copy=${report.copied.length} update=${report.updated.length} extra=${report.extraInArchive.length}`)
      return JSON.stringify({
        ok: report.errors.length === 0,
        ...report,
        copied: report.copied.slice(0, 30),
        updated: report.updated.slice(0, 30),
        extraInArchive: report.extraInArchive.slice(0, 30),
      }, null, 2)
    },
  })

  register({
    name: 'corpus_hydrate',
    description: '把「产物存档」里有、新稿落点里没有的 dufu-*.md 补进落点（旧主库退役后清洗脚本按「落点+存档」两副本工作，补齐旧稿后才能在落点上开批）。默认 dryRun=true 只报告。',
    parameters: {
      type: 'object',
      properties: {
        dryRun: { type: 'boolean', description: '默认 true 只报告' },
        limit: { type: 'number', description: '最多补多少篇（缺省全部）' },
      },
    },
    output: textOutput,
    async execute(args: Record<string, unknown>) {
      const dryRun = args?.dryRun !== false
      const limitRaw = Number(args?.limit)
      const limit = Number.isFinite(limitRaw) && limitRaw > 0 ? Math.floor(limitRaw) : Number.POSITIVE_INFINITY
      const landing = new Set<string>()
      try {
        for (const f of readdirSync(P().landing)) {
          if (isDocName(f)) landing.add(f)
        }
      } catch { /* 落点不存在 = 全缺 */ }
      const missing: string[] = []
      try {
        for (const f of readdirSync(P().archive)) {
          if (isDocName(f) && !landing.has(f)) missing.push(f)
        }
      } catch {
        return JSON.stringify({ ok: false, error: '产物存档读不到' }, null, 2)
      }
      const todo = Number.isFinite(limit) ? missing.slice(0, limit) : missing
      let copied = 0
      const errors: string[] = []
      if (!dryRun) {
        try { ensureDir(P().landing) } catch { /* 已在 */ }
        for (const f of todo) {
          try {
            copyFileSync(join(P().archive, f), join(P().landing, f))
            copied += 1
          } catch (e) {
            errors.push(`${f}: ${String(e).slice(0, 120)}`)
          }
        }
      }
      log(`hydrate dryRun=${dryRun} landing=${landing.size} missing=${missing.length} copied=${copied}`)
      return JSON.stringify({
        ok: errors.length === 0,
        dryRun,
        landing: P().landing,
        archive: P().archive,
        landingFiles: landing.size,
        missing: missing.length,
        planned: todo.length,
        copied,
        sample: todo.slice(0, 10),
        errors: errors.slice(0, 10),
      }, null, 2)
    },
  })

  register({
    name: 'corpus_gates',
    description: '跑语料验收门禁：corpus_lint.py（rc 必须 0 且含 "RESULT: PASS" 与 "NEW REGRESSIONS: none"）+ corpus_lint_selftest.py（含 "SELFTEST PASS"）。只看不写。返回 JSON。',
    parameters: { type: 'object', properties: {} },
    output: textOutput,
    async execute() {
      return JSON.stringify(runGates(), null, 2)
    },
  })

  register({
    name: 'corpus_pause',
    description: '暂停驾驶舱（写 _smoothing_state/cockpit-paused.flag）：暂停后 corpus_batch 与守护循环都拒绝开批。返回 JSON。',
    parameters: { type: 'object', properties: { reason: { type: 'string', description: '暂停原因（写进 flag 文件）' } } },
    output: textOutput,
    async execute(args: Record<string, unknown>) {
      writeJson(pausedFlag(), { at: nowIso(), reason: String(args?.reason ?? '手动暂停'), by: 'corpus_pause' })
      saveState()
      log('paused')
      return JSON.stringify({ ok: true, at: nowIso(), paused: true, flag: pausedFlag(), reason: String(args?.reason ?? '手动暂停') }, null, 2)
    },
  })

  register({
    name: 'corpus_resume',
    description: '恢复驾驶舱（删 cockpit-paused.flag）。注意：恢复≠自动跑批，auto 仍默认 false。返回 JSON。',
    parameters: { type: 'object', properties: {} },
    output: textOutput,
    async execute() {
      let removed = false
      try {
        if (existsSync(pausedFlag())) { rmSync(pausedFlag(), { force: true }); removed = true }
      } catch (e) {
        return JSON.stringify({ ok: false, error: String(e) }, null, 2)
      }
      saveState()
      log('resumed')
      return JSON.stringify({ ok: true, at: nowIso(), paused: false, flagRemoved: removed }, null, 2)
    },
  })

  register({
    name: 'corpus_batch',
    description: '⚠️ 会真改语料（经既有 apply_smoothing.py / punct_apply.py 两条通道落地）。跑一个批次：queue→snapshot→逐篇 LLM→verify→apply(--drop-bad --guard)→record→门禁。kind=error 走错字通道（size 默认 24），kind=punct 走本地标点模型（0 token）。同一时刻只允许一个批次；门禁失败会拒绝开下一批。返回 JSON。',
    parameters: {
      type: 'object',
      properties: {
        kind: { type: 'string', enum: ['error', 'punct'], description: 'error=错字通道；punct=标点通道' },
        size: { type: 'integer', description: '错字通道本批篇数（默认 24）' },
        batch: { type: 'integer', description: '指定批号/波号（缺省自动取下一个）' },
        dryRun: { type: 'boolean', description: 'true=只预览不落地（LLM 与补丁照常产出）' },
      },
      required: ['kind'],
    },
    output: textOutput,
    async execute(args: Record<string, any>) {
      const kind = String(args?.kind ?? '').trim()
      if (kind !== 'error' && kind !== 'punct') {
        return JSON.stringify({ ok: false, error: 'BAD_ARGS', message: 'kind 必须是 error 或 punct' }, null, 2)
      }
      const res = await runBatch(kind, {
        size: args?.size === undefined ? undefined : Number(args.size),
        batch: args?.batch === undefined ? undefined : Number(args.batch),
        dryRun: args?.dryRun === true,
      })
      return JSON.stringify(res, null, 2)
    },
  })

  // ──────────────────────────────────────────────────── 守护循环（默认关闭）
  if (AUTO) {
    log(`守护循环启动：intervalMs=${INTERVAL_MS}（auto=true）`)
  } else {
    log(`守护循环待命：auto=false（要自动跑批需设 DSH_CORPUS_AUTO=1）`)
  }
  try {
    ctx.effect(() => {
      const timer = setInterval(() => {
        void (async () => {
          cycles += 1
          // 每 tick 最多跑一个批次，跑完立即结束本 tick（绝不连跑）
          if (!AUTO) { saveState(); return }
          if (isPaused() || inFlight || gateBlocked) {
            log(`tick#${cycles} 跳过（paused=${isPaused()} inFlight=${Boolean(inFlight)} gateBlocked=${gateBlocked}）`)
            saveState()
            return
          }
          log(`tick#${cycles} 开跑一批（kind=error）`)
          await runBatch('error', {})
        })().catch((e) => log(`tick#${cycles} 异常: ${String(e)}`))
      }, INTERVAL_MS)
      return () => clearInterval(timer)
    }, `${SHORT}: daemon`)
  } catch (e) {
    log(`守护循环挂载失败: ${String(e)}`)
  }

  // ── 可选：host HTTP 端点，给 client 面板取状态用（拿不到 webServer 就跳过）
  try {
    const webServer = ctx.get('webServer')
    if (webServer && typeof webServer.register === 'function') {
      ctx.effect(() => webServer.register({
        kind: 'prefix',
        path: '/dsh-bili-corpus',
        handler: (req: any, res: any) => {
          const url = new URL(String(req.url ?? '/'), 'http://x')
          const json = (code: number, body: unknown): void => {
            res.writeHead(code, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' })
            res.end(JSON.stringify(body, null, 2))
          }
          const qCorpus = url.searchParams.get('corpus') || undefined
          const path = url.pathname
          const statusBody = (): Record<string, unknown> => {
            const led = ledgerSummary()
            const landing = countRoot(P().landing)
            const archive = countRoot(P().archive)
            const q = punctQueueStats()
            return {
              at: nowIso(), corpus: P().id,
              error: { total: archive.dufu, done: led.docsDone, batches: led.batches, lastBatch: led.lastBatch },
              punct: {
                total: Number(led.punctDone) + q.undone, done: led.punctDone,
                queued: q.queued, undone: q.undone,
                unneeded: Math.max(0, archive.dufu - Number(led.punctDone) - q.undone),
              },
              roots: {
                main: { dir: P().landing, role: '新稿落点（临时落脚）', dufuFiles: landing.dufu },
                archive: { dir: P().archive, role: '产物存档（git 仓）', dufuFiles: archive.dufu },
              },
              running: inFlight, paused: isPaused(), gateBlocked, lastGate, lastBatch, tokens, auto: AUTO,
              transcribe: biliSummary(),
            }
          }
          if (path === '/dsh-bili-corpus/api/corpora') { json(200, corporaSummary()); return }
          if (path === '/dsh-bili-corpus/api/corpus' && String(req.method).toUpperCase() === 'POST') {
            let body = ''
            req.on('data', (c: any) => { body = (body + String(c)).slice(0, 4096) })
            req.on('end', () => {
              try {
                const parsed = body ? JSON.parse(body) : {}
                const id = String(parsed.id ?? parsed.corpus ?? '')
                if (!id) { json(400, { ok: false, error: '缺少 id' }); return }
                if (inFlight || biliBusy) { json(409, { ok: false, error: 'BUSY', active: ACTIVE_ID }); return }
                const r = switchCorpus(id)
                if (!r.ok) { json(400, r); return }
                onCorpusChanged?.()
                saveState()
                json(200, { ...r, landing: P().landing, archive: P().archive })
              } catch (e) {
                json(400, { ok: false, error: `请求体不是 JSON：${String(e)}` })
              }
            })
            return
          }
          if (path === '/dsh-bili-corpus/api/status') { json(200, bindCorpus(qCorpus, statusBody)); return }
          if (path === '/dsh-bili-corpus/api/jobs') {
            const body = bindCorpus(qCorpus, () => ({ at: nowIso(), ok: true, ...biliSummary(), jobs: jobs.slice(-50).reverse() }))
            json(200, body)
            return
          }
          if (url.pathname === '/dsh-bili-corpus/api/transcribe' && String(req.method).toUpperCase() === 'POST') {
            let body = ''
            req.on('data', (c: any) => { body = (body + String(c)).slice(0, 4096) })
            req.on('end', () => {
              try {
                const parsed = body ? JSON.parse(body) : {}
                const res2 = enqueueBili(String(parsed.target ?? parsed.bvid ?? parsed.url ?? ''), parsed.force === true, qCorpus)
                json(res2.ok ? 200 : 400, res2)
              } catch (e) {
                json(400, { ok: false, error: `请求体不是 JSON：${String(e)}` })
              }
            })
            return
          }
          if (path === '/dsh-bili-corpus/api/pause') {
            bindCorpus(qCorpus, () => {
              writeJson(pausedFlag(), { at: nowIso(), reason: '面板暂停', by: 'panel' })
              saveState()
            })
            json(200, { ok: true, paused: true, corpus: P().id })
            return
          }
          if (path === '/dsh-bili-corpus/api/resume') {
            bindCorpus(qCorpus, () => {
              try { rmSync(pausedFlag(), { force: true }) } catch { /* ignore */ }
              saveState()
            })
            json(200, { ok: true, paused: false, corpus: P().id })
            return
          }
          json(404, { ok: false, error: 'not found', path: url.pathname })
        },
      }), `${SHORT}: http`)
      log('host HTTP 端点已挂载：/dsh-bili-corpus/api/{corpora,corpus,status,jobs,transcribe,pause,resume}')
    }
  } catch (e) {
    log(`HTTP 端点挂载失败（面板降级为只读提示）: ${String(e)}`)
  }

  ctx.logger?.info?.(`[${name}] 驾驶舱就绪：tools=corpus_status/corpus_gates/corpus_batch/corpus_pause/corpus_resume/corpus_mirror_sync/corpus_hydrate/bili_transcribe/bili_jobs；auto=${AUTO}`)
  log(`apply 完成：auto=${AUTO} intervalMs=${INTERVAL_MS} state=${P().state}`)
}
