#!/usr/bin/env node
// 第二个语料的冒烟：证明插件的路径逻辑不依赖 dufuzhixin。
//
// 用法：
//   node scripts/smoke-second-corpus.mjs                 # 只做只读检查（status/hydrate/gates）
//   node scripts/smoke-second-corpus.mjs --init          # 先建一个最小语料（2 篇演示稿）
//   node scripts/smoke-second-corpus.mjs --transcribe BV112uU6BEQM   # 顺带真转写一篇并验归档
//
// 语料位置默认：
//   landing  ~/.dsh/corpus/<id>/memory
//   state    ~/.dsh/corpus/<id>/_smoothing_state
//   archive  ~/Documents/GitHub/<id>-corpus/memory
//
// 说明：插件常量在模块加载时从 env 求值，所以本脚本先设 env 再 import。
// 这也说明了现状——「换语料」目前是**进程级**（env），不是 UI 里的开关；那属于 P0。
import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createHash } from 'node:crypto'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = join(HERE, '..')
const HOME = process.env.USERPROFILE || process.env.HOME

function arg(name, fallback = null) {
  const i = process.argv.indexOf(`--${name}`)
  if (i < 0) return fallback
  const v = process.argv[i + 1]
  return v && !v.startsWith('--') ? v : true
}

const id = String(arg('id', 'demo'))
const landing = arg('landing', join(HOME, '.dsh', 'corpus', id, 'memory'))
const state = arg('state', join(HOME, '.dsh', 'corpus', id, '_smoothing_state'))
const archive = arg('archive', join(HOME, 'Documents', 'GitHub', `${id}-corpus`, 'memory'))
const transcribe = arg('transcribe', null)
const shouldInit = !!arg('init', false)

const failures = []
const ok = (cond, msg) => { if (!cond) failures.push(msg); return cond }

function initCorpus() {
  for (const d of [landing, state, archive]) mkdirSync(d, { recursive: true })
  const docs = {
    'dufu-BVdemo00001.md': [
      '---',
      `title: 演示语料（${id}）一`,
      'bvid: BVdemo00001',
      'date: 2026-05-12',
      'duration: 03:21',
      'type: monologue',
      'transcribed: 2026-10-30 22:10:00',
      '---',
      '',
      '今天我们不聊新闻，聊一个更基础的问题。',
      '很多争论之所以吵不出结果，是因为双方对同一个词的用法不一样。',
    ].join('\n'),
    'dufu-BVdemo00002.md': [
      '---',
      `title: 演示语料（${id}）二`,
      'bvid: BVdemo00002',
      'date: 2026-06-03',
      'duration: 02:14',
      'type: monologue',
      'transcribed: 2026-10-30 22:10:05',
      '---',
      '',
      '方向对了以后，速度本身就是稳定器。',
    ].join('\n'),
  }
  for (const [name, text] of Object.entries(docs)) {
    const p = join(archive, name)
    if (!existsSync(p)) writeFileSync(p, text + '\n', 'utf8')
  }
  const index = join(archive, 'MEMORY.md')
  if (!existsSync(index)) {
    writeFileSync(index, [`# ${id} 语料索引`, '', '- dufu-BVdemo00001.md: 演示语料一', '- dufu-BVdemo00002.md: 演示语料二', ''].join('\n'), 'utf8')
  }
  const lindex = join(landing, 'MEMORY.md')
  if (!existsSync(lindex)) writeFileSync(lindex, `# ${id} 落点\n\n新稿先落这里，完成后归档进 ${archive}。\n`, 'utf8')
  console.log(`init: ${landing}\n      ${archive}\n      ${state}`)
}

if (shouldInit) initCorpus()

// 常量在 import 时求值 → env 必须先设好
process.env.DSH_CORPUS_MAIN = landing
process.env.DSH_CORPUS_ARCHIVE = archive
process.env.DSH_CORPUS_STATE = state

const tools = new Map()
const ctx = {
  effect(fn) { const d = fn(); return d },
  get() { return undefined },
  tools: { register(t) { tools.set(t.name, t); return () => tools.delete(t.name) } },
}

const mod = await import(`file://${join(PLUGIN, 'lib', 'index.js').replace(/\\/g, '/')}`)
const plugin = mod.default ?? mod
plugin.apply(ctx, {})

const call = (name, args = {}) => {
  const t = tools.get(name)
  if (!t) throw new Error(`tool missing: ${name}`)
  return t.execute(args)
}
const json = (v) => (typeof v === 'string' ? JSON.parse(v) : v)

console.log(`\n▸ 语料 ${id}`)
console.log(`  landing ${landing}`)
console.log(`  archive ${archive}`)
console.log(`  state   ${state}\n`)

// 1) 只读状态：两个根必须指向本次的语料（而不是 dufuzhixin）
const st = json(await call('corpus_status'))
const roots = st.roots
ok(roots?.main?.dir === landing.replace(/\\/g, '/') || roots?.main?.dir === landing, `roots.main 不对：${roots?.main?.dir}`)
ok(roots?.archive?.dir === archive.replace(/\\/g, '/') || roots?.archive?.dir === archive, `roots.archive 不对：${roots?.archive?.dir}`)
ok(typeof roots?.main?.dufuFiles === 'number' && roots.main.dufuFiles >= 0, 'roots.main.dufuFiles 缺失')
console.log(`  status ok：落点 ${roots?.main?.dufuFiles} 篇 · 存档 ${roots?.archive?.dufuFiles} 篇 · 错字 ${st.error?.docsDone}/${st.error?.total}`)

// 2) hydrate：存档有、落点缺的稿子应当补进落点（dryRun 与实跑各一次）
const dry = json(await call('corpus_hydrate', { dryRun: true }))
console.log(`  hydrate(dry)：落点 ${dry.landingFiles} 篇，存档缺 ${dry.missing} 篇，计划补 ${dry.planned} 篇`)
const real = json(await call('corpus_hydrate', { dryRun: false }))
ok(real.copied === dry.planned, `hydrate 实际补 ${real.copied} ≠ 计划 ${dry.planned}`)
console.log(`  hydrate(apply)：补了 ${real.copied} 篇`)

// 3) 门禁：该语料自己的 baseline，必须 PASS
const gate = json(await call('corpus_gates'))
ok(gate.pass === true, `门禁未通过：${JSON.stringify(gate.lint)}`)
ok(gate.lint?.regressionsLine?.includes('NEW REGRESSIONS: none'), `回归行异常：${gate.lint?.regressionsLine}`)
console.log(`  门禁：pass=${gate.pass} · ${gate.lint?.resultLine}`)

// 4) 可选：真转写一篇 → 落点 + 归档 + 两处索引行
if (transcribe) {
  const target = String(transcribe)
  console.log(`\n▸ 转写 ${target}（真跑，可能需要几分钟）`)
  const r = json(await call('bili_transcribe', { target, wait: true }))
  const job = r.job ?? r
  ok(job?.state === 'done', `转写未完成：${job?.state} ${job?.message}`)
  const bvid = /BV[0-9A-Za-z]{10}/.exec(target)?.[0]
  const name = `dufu-${bvid}.md`
  const lp = join(landing, name)
  const ap = join(archive, name)
  ok(existsSync(ap), `存档没有 ${name}`)
  if (existsSync(lp) && existsSync(ap)) {
    const sha = (p) => createHash('sha256').update(readFileSync(p)).digest('hex')
    ok(sha(lp) === sha(ap), '落点与存档内容不一致')
    ok(statSync(ap).size > 64, '存档稿过短')
  }
  console.log(`  转写 ${job?.state}：${job?.message} · 存档 ${existsSync(ap) ? '有' : '缺'}`)
}

console.log('')
if (failures.length) {
  console.error(`FAIL（${failures.length}）`)
  for (const f of failures) console.error(`  - ${f}`)
  process.exit(1)
}
console.log('PASS：第二个语料走通了 状态 / hydrate / 门禁' + (transcribe ? ' / 转写归档' : ''))
process.exit(0)
