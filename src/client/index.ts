/**
 * @dsh-external/dsh-bili-corpus — client 面板（conversation.view slot）。
 *
 * 构建：npm run build:client（tsdown → lib/client.js，ModuleLoader.load 注册）。
 *
 * ⚠️ 三个必坑（2026-10 实测，DSH Web 渲染器）：
 *  ① apply 用 ctx.slots 必须 export const inject = ['slots']；
 *  ② register 签名是 **register(options, Component)** —— 组件是「第二个参数」。
 *     把 component 塞进 options 里不会报错，但 entry.component === undefined，
 *     React 渲染时抛 "Element type is invalid"，被 per-entry 错误边界吞成
 *     一个空的 <div data-slot-error="...">，表现就是 **标签在、内容全空白**；
 *  ③ Component 必须是真正的 React 组件（也可以用 createElement 写），
 *     不能返回 { render() { return HTMLElement } } 这种旧式对象。
 *
 * 面板两组：
 *  · 转写：贴 BVID / 链接 → 入队（POST /api/transcribe），下方看最近任务；
 *  · 清洗：从 /api/status 取数，暂停/继续走 /api/pause 与 /api/resume。
 * **面板没有「跑一批」按钮**——批次必须由用户在会话里明确确认后手动开。
 */
import {
  createElement as h,
  useCallback,
  useEffect,
  useState,
  type CSSProperties,
  type ReactElement,
} from 'react'

type SlotsRegistration = Record<string, unknown>

type ClientContext = {
  slots: {
    inject: (slot: string, factory: () => unknown) => unknown
    register: (options: SlotsRegistration, component: unknown) => unknown
  }
  effect?: (cb: () => unknown, label?: string) => unknown
}

export const name = 'dsh-bili-corpus'
export const inject = ['slots']

const API = '/dsh-bili-corpus/api'

const cardStyle: CSSProperties = {
  padding: '10px 12px',
  fontFamily: 'ui-monospace,Consolas,monospace',
  fontSize: 12,
  color: '#ddd',
  overflow: 'auto',
  maxHeight: '100%',
}

const titleStyle: CSSProperties = { fontWeight: 600, fontSize: 13, marginBottom: 6 }
const sectionStyle: CSSProperties = { marginTop: 12, paddingTop: 8, borderTop: '1px solid #333' }
const btnStyle: CSSProperties = {
  font: 'inherit',
  padding: '3px 10px',
  borderRadius: 4,
  border: '1px solid #555',
  background: '#333',
  color: '#ddd',
  cursor: 'pointer',
}
const inputStyle: CSSProperties = {
  font: 'inherit',
  padding: '3px 8px',
  borderRadius: 4,
  border: '1px solid #555',
  background: '#222',
  color: '#ddd',
  flex: '1 1 auto',
  minWidth: 160,
}

function fmtTime(iso?: string | null): string {
  if (!iso) return '-'
  try {
    const d = new Date(iso)
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  } catch {
    return String(iso)
  }
}

function Bar(props: { label: string; done: number; total: number; hint?: string }): ReactElement {
  const { label, done, total, hint } = props
  const pct = total > 0 ? Math.max(0, Math.min(100, (done / total) * 100)) : 0
  const note = total > 0 && done > total ? ` · 账本比总篇数多 ${done - total}（进度按 100% 计）` : ''
  const tail = hint ? ` · ${hint}` : ''
  return h(
    'div',
    { style: { margin: '6px 0' } },
    h('div', { style: { fontSize: 12 } }, `${label}：${done} / ${total}（${pct.toFixed(2)}%${note}）${tail}`),
    h(
      'div',
      { style: { height: 6, background: '#2a2a2a', borderRadius: 3, overflow: 'hidden', marginTop: 3 } },
      h('div', { style: { height: '100%', width: `${pct}%`, background: '#4a9eff' } }),
    ),
  )
}

function Row(props: { k: string; v: string }): ReactElement {
  return h(
    'div',
    { style: { display: 'flex', gap: 8, fontSize: 12, lineHeight: 1.6 } },
    h('span', { style: { color: '#888', minWidth: 96 } }, props.k),
    h('span', null, props.v),
  )
}

/** host 端点的 roots 历史上给过两种形状：数字（篇数）或 { dufuFiles }，都兼容。 */
type Count = number | { dufuFiles?: number }

type JobBrief = {
  id?: string
  bvid?: string
  state?: string
  stage?: string
  message?: string
  duration?: number | null
  textLength?: number | null
  cached?: boolean
  startedAt?: string | null
  endedAt?: string | null
}

type Transcribe = {
  total?: number
  queued?: number
  running?: number
  done?: number
  failed?: number
  busy?: { id?: string; bvid?: string; stage?: string } | null
  cachedFiles?: number
  recent?: JobBrief[]
  skill?: { pythonExists?: boolean; bridgeExists?: boolean; dir?: string }
}

type Status = {
  at?: string
  corpus?: string
  error?: {
    done?: number
    total?: number
    batches?: number
    lastBatch?: { at?: string; recordedAt?: string; queued?: number; applied?: number; totalPatches?: number }
  }
  punct?: { done?: number; total?: number; undone?: number; unneeded?: number }
  roots?: { main?: Count; archive?: Count }
  running?: { batch?: number; phase?: string } | null
  paused?: boolean
  gateBlocked?: boolean
  lastGate?: { ok?: boolean } | null
  auto?: boolean
  tokens?: { input?: number; output?: number; calls?: number }
  transcribe?: Transcribe
}

type Corpora = {
  ok?: boolean
  active?: string
  corpora?: Array<{ id: string; docPrefix?: string; naming?: string; fileName?: string; landing?: string; archive?: string }>
}

function countOf(v: Count | undefined): string {
  if (typeof v === 'number') return String(v)
  if (v && typeof v.dufuFiles === 'number') return String(v.dufuFiles)
  return '?'
}

function stateIcon(j: JobBrief): string {
  if (j.state === 'done') return j.cached ? '✓缓存' : '✓完成'
  if (j.state === 'failed') return '✗失败'
  if (j.state === 'running') return '…进行中'
  return '· 排队'
}

function TranscribeSection(props: { status: Status | null; refresh: () => void; naming?: string }): ReactElement {
  const { status, refresh, naming = 'bvid' } = props
  const [target, setTarget] = useState('')
  const [name, setName] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const t = status?.transcribe ?? {}

  const submit = useCallback(() => {
    const value = target.trim()
    if (!value) {
      setMsg('先贴一个 BVID 或视频链接')
      return
    }
    setBusy(true)
    setMsg(null)
    fetch(`${API}/transcribe`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ target: value, name: name.trim() || undefined }),
    })
      .then((r) => r.json().then((body: unknown) => ({ ok: r.ok, body })))
      .then(({ ok, body }: { ok: boolean; body: any }) => {
        // 标题式语料：服务端先不入队，回一个建议名（needName）
        if (body?.needName === true) {
          if (body?.suggested) setName(String(body.suggested))
          setMsg(body?.hint ?? '这是标题式语料，请确认文件名后再点转写')
          return
        }
        if (!ok || body?.ok === false) setMsg(`入队失败：${body?.error ?? '未知错误'}`)
        else {
          setMsg(`已入队 ${body?.job?.bvid ?? ''}（${body?.job?.id ?? ''}）`)
          setTarget('')
          setName('')
        }
      })
      .catch((e: unknown) => setMsg(`请求失败：${String(e)}`))
      .finally(() => {
        setBusy(false)
        refresh()
      })
  }, [target, name, refresh])

  const rows: ReactElement[] = []
  rows.push(
    h(Row, {
      key: 'skill',
      k: '转写引擎',
      v: t.skill?.pythonExists && t.skill?.bridgeExists
        ? `就绪（本地 SenseVoice，已转 ${t.cachedFiles ?? '?'} 篇）`
        : `未就绪（venv=${t.skill?.pythonExists ? '✓' : '✗'} bridge=${t.skill?.bridgeExists ? '✓' : '✗'}）`,
    }),
    h(Row, {
      key: 'naming',
      k: '命名策略',
      v: naming === 'title' ? '标题-BV号（转写前需确认文件名）' : 'dufu-BV号',
    }),
    h(Row, {
      key: 'queue',
      k: '队列',
      v: `排队 ${t.queued ?? 0} · 进行 ${t.running ?? 0} · 完成 ${t.done ?? 0} · 失败 ${t.failed ?? 0}`,
    }),
    h(Row, {
      key: 'busy',
      k: '当前任务',
      v: t.busy ? `${t.busy.bvid ?? ''} · ${t.busy.stage ?? ''}` : '（空闲）',
    }),
  )
  const recent = (t.recent ?? []).slice(0, 6)
  if (recent.length > 0) {
    rows.push(
      h(
        'div',
        { key: 'recent', style: { marginTop: 6 } },
        ...recent.map((j) =>
          h(
            'div',
            { key: j.id ?? j.bvid, style: { fontSize: 11, lineHeight: 1.6, color: j.state === 'failed' ? '#e88' : '#bbb' } },
            `${stateIcon(j)} ${j.bvid ?? ''} · ${j.message ?? j.stage ?? ''}${j.textLength ? ` · ${j.textLength} 字` : ''}${j.endedAt ? ` · ${fmtTime(j.endedAt)}` : ''}`,
          ),
        ),
      ),
    )
  }

  return h(
    'div',
    { style: sectionStyle },
    h('div', { style: titleStyle }, 'B 站转写 → 语料'),
    h('div', { style: { display: 'flex', gap: 8, margin: '6px 0' } },
      h('input', {
        style: inputStyle,
        placeholder: 'BVID 或 https://www.bilibili.com/video/BV…',
        value: target,
        onChange: (e: any) => setTarget(String(e?.target?.value ?? '')),
        onKeyDown: (e: any) => {
          if (e?.key === 'Enter') submit()
        },
      }),
      h('button', { type: 'button', style: btnStyle, disabled: busy, onClick: submit }, busy ? '入队中…' : '转写'),
    ),
    h('input', {
      style: { ...inputStyle, marginBottom: 6 },
      placeholder: naming === 'title' ? '文件名（标题-BV号.md；留空会先给建议名）' : '文件名（可选，留空用 dufu-BV号.md）',
      value: name,
      onChange: (e: any) => setName(String(e?.target?.value ?? '')),
    }),
    msg ? h('div', { style: { color: msg.startsWith('已入队') ? '#8c8' : '#e88', fontSize: 11 } }, msg) : null,
    h('div', null, rows),
    h(
      'div',
      { style: { marginTop: 6, color: '#888', fontSize: 11, lineHeight: 1.5 } },
      '转写为本地 SenseVoice（0 token）：先写当前语料的落点，完成后自动归档进它的产物存档并补索引文件；同一时刻只跑一个任务。',
    ),
  )
}

function CockpitPanel(): ReactElement {
  const [status, setStatus] = useState<Status | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  type CorpusChip = {
    id: string
    naming?: string
    docPrefix?: string
    indexFile?: string
    landing?: string
    archive?: string
    state?: string
  }
  type CorpusForm = { id: string; naming: string; docPrefix: string; indexFile: string; landing: string; archive: string; state: string }
  const [corpora, setCorpora] = useState<CorpusChip[]>([])
  const [active, setActive] = useState('')

  const refresh = useCallback(async (corpus?: string) => {
    try {
      const c: Corpora = await fetch(`${API}/corpora`, { headers: { accept: 'application/json' } }).then((r) => r.json())
      setCorpora(Array.isArray(c?.corpora) ? (c.corpora as CorpusChip[]) : [])
      const want = corpus || active || c?.active || ''
      if (want) setActive(want)
      const url = `${API}/status${want ? `?corpus=${encodeURIComponent(want)}` : ''}`
      const s: Status = await fetch(url, { headers: { accept: 'application/json' } })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      setStatus(s)
      setFailure(null)
    } catch (e) {
      setFailure(String(e))
    }
  }, [active])

  const switchTo = useCallback((id: string) => {
    fetch(`${API}/corpus`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ id }) })
      .then((r) => r.json())
      .then((r: { ok?: boolean; error?: string }) => {
        if (r?.ok === false) setFailure(r.error ?? '切换语料失败')
        else {
          setActive(id)
          void refresh(id)
        }
      })
      .catch((e: unknown) => setFailure(String(e)))
  }, [refresh])

  const [manage, setManage] = useState(false)
  const [form, setForm] = useState<CorpusForm | null>(null)
  const [editing, setEditing] = useState('')
  const [busy, setBusy] = useState(false)
  const [mmsg, setMmsg] = useState<string | null>(null)

  const startCreate = useCallback(() => {
    setEditing('')
    setForm({ id: '', naming: 'bvid', docPrefix: 'dufu-', indexFile: 'MEMORY.md', landing: '', archive: '', state: '' })
    setMmsg(null)
    setManage(true)
  }, [])

  const startEdit = useCallback((c: CorpusChip) => {
    setEditing(c.id)
    setForm({
      id: c.id,
      naming: c.naming || 'bvid',
      docPrefix: c.docPrefix || 'dufu-',
      indexFile: c.indexFile ?? 'MEMORY.md',
      landing: c.landing || '',
      archive: c.archive || '',
      state: c.state || '',
    })
    setMmsg(null)
  }, [])

  const saveCorpus = useCallback(() => {
    if (!form) return
    setBusy(true)
    const path = editing ? '/corpora/update' : '/corpora/create'
    fetch(`${API}${path}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ ...form, id: editing || form.id }),
    })
      .then((r) => r.json())
      .then((r: { ok?: boolean; error?: string }) => {
        if (r?.ok === false) { setMmsg(r.error ?? '保存失败'); return }
        setMmsg(editing ? `已更新 ${editing}` : '已新建')
        setForm(null)
        setEditing('')
        void refresh()
      })
      .catch((e: unknown) => setMmsg(String(e)))
      .finally(() => setBusy(false))
  }, [form, editing, refresh])

  const removeCorpus = useCallback((id: string) => {
    if (typeof window !== 'undefined' && typeof window.confirm === 'function' && !window.confirm(`删除语料 ${id}？只删配置，目录保留；目录里还有稿件会被拒绝。`)) return
    setBusy(true)
    setMmsg(null)
    fetch(`${API}/corpora/delete`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ id }),
    })
      .then((r) => r.json())
      .then((r: { ok?: boolean; error?: string }) => {
        if (r?.ok === false) { setMmsg(r.error ?? '删除失败'); return }
        setMmsg(`已删除 ${id}`)
        void refresh()
      })
      .catch((e: unknown) => setMmsg(String(e)))
      .finally(() => setBusy(false))
  }, [refresh])

  useEffect(() => {
    void refresh()
    const timer = setInterval(() => { void refresh() }, 15000)
    return () => clearInterval(timer)
  }, [refresh])

  const post = useCallback(
    (path: string) => {
      fetch(`${API}${path}`, { method: 'POST' })
        .catch(() => undefined)
        .finally(() => refresh())
    },
    [refresh],
  )

  const rows: ReactElement[] = []
  if (status) {
    rows.push(
      h(Bar, {
        key: 'bar-error',
        label: '错字通道已落地',
        done: Number(status.error?.done ?? 0),
        total: Number(status.error?.total ?? 0),
      }),
      h(Bar, {
        key: 'bar-punct',
        label: '标点通道已处理',
        done: Number(status.punct?.done ?? 0),
        total: Number(status.punct?.total ?? 0),
        hint:
          Number(status.punct?.unneeded ?? 0) > 0
            ? `另有 ${status.punct?.unneeded} 篇无长连读、无需处理`
            : '',
      }),
      h(Row, {
        key: 'roots',
        k: '稿件落点 / 存档',
        v: `落点 ${countOf(status.roots?.main)} · 存档 ${countOf(status.roots?.archive)}`,
      }),
      h(Row, { key: 'batches', k: '批次数', v: String(status.error?.batches ?? '?') }),
      h(Row, {
        key: 'last-batch',
        k: '最近一批',
        v: status.error?.lastBatch
          ? `落地 ${status.error.lastBatch.applied ?? '?'}/${status.error.lastBatch.queued ?? '?'} 篇 · 补丁 ${status.error.lastBatch.totalPatches ?? '?'} · ${fmtTime(status.error.lastBatch.at ?? status.error.lastBatch.recordedAt)}`
          : '-',
      }),
      h(Row, {
        key: 'running',
        k: '在跑批次',
        v: status.running ? `批${status.running.batch} · ${status.running.phase}` : '（空闲）',
      }),
      h(Row, { key: 'paused', k: '暂停', v: status.paused ? '是' : '否' }),
      h(Row, {
        key: 'gate',
        k: '门禁',
        v: status.gateBlocked ? '阻塞（上一批未过）' : status.lastGate ? (status.lastGate.ok ? '通过' : '未通过') : '未跑过',
      }),
      h(Row, { key: 'auto', k: 'auto', v: status.auto ? '开（会自驱跑批！）' : '关（默认）' }),
      h(Row, {
        key: 'tokens',
        k: '累计 token',
        v: `in ${status.tokens?.input ?? 0} / out ${status.tokens?.output ?? 0}（${status.tokens?.calls ?? 0} 次调用）`,
      }),
      h(Row, { key: 'at', k: '更新于', v: fmtTime(status.at) }),
    )
  }

  const fieldRow = (label: string, key: keyof CorpusForm, placeholder?: string, locked?: boolean): ReactElement =>
    h(
      'label',
      { key, style: { display: 'flex', gap: 6, alignItems: 'center', margin: '2px 0' } },
      h('span', { style: { width: 88, color: '#666', fontSize: 12 } }, label),
      h('input', {
        value: form ? form[key] : '',
        placeholder: placeholder ?? '',
        disabled: busy || locked,
        onChange: (e: any) => {
          const v = String(e?.target?.value ?? '')
          setForm((f) => (f ? { ...f, [key]: v } : f))
        },
        style: { flex: 1, fontSize: 12, padding: '2px 6px', minWidth: 0 },
      }),
    )

  const manageBox: ReactElement = form
    ? h(
        'div',
        { style: { border: '1px solid #ddd', borderRadius: 6, padding: '6px 8px', margin: '0 0 8px', background: '#fafafa' } },
        h('div', { style: { fontSize: 12, color: '#555', marginBottom: 4 } }, editing ? `编辑语料 ${editing}` : '新建语料'),
        fieldRow('id', 'id', '小写字母/数字/-/_', !!editing),
        h(
          'label',
          { style: { display: 'flex', gap: 6, alignItems: 'center', margin: '2px 0' } },
          h('span', { style: { width: 88, color: '#666', fontSize: 12 } }, '命名策略'),
          h(
            'select',
            {
              value: form.naming,
              disabled: busy,
              onChange: (e: any) => {
                const v = String(e?.target?.value ?? 'bvid')
                setForm((f) => (f ? { ...f, naming: v } : f))
              },
              style: { fontSize: 12 },
            },
            h('option', { value: 'bvid' }, 'bvid（dufu-BV号.md）'),
            h('option', { value: 'title' }, 'title（标题-BV号.md）'),
          ),
        ),
        fieldRow('落点', 'landing', '绝对路径；留空 = ~/.dsh/corpus/<id>/memory'),
        fieldRow('存档', 'archive', '绝对路径（必填）'),
        fieldRow('状态目录', 'state', '绝对路径；留空 = ~/.dsh/corpus/<id>/_smoothing_state'),
        fieldRow('文件名前缀', 'docPrefix', 'dufu-'),
        fieldRow('索引文件', 'indexFile', 'MEMORY.md；留空 = 不维护索引'),
        mmsg ? h('div', { style: { color: '#c66', fontSize: 12 } }, mmsg) : null,
        h(
          'div',
          { style: { display: 'flex', gap: 8, marginTop: 6 } },
          h('button', { type: 'button', style: btnStyle, disabled: busy, onClick: saveCorpus }, busy ? '保存中…' : '保存'),
          h('button', { type: 'button', style: btnStyle, disabled: busy, onClick: () => { setForm(null); setEditing('') } }, '取消'),
        ),
      )
    : h(
        'div',
        { style: { border: '1px solid #ddd', borderRadius: 6, padding: '6px 8px', margin: '0 0 8px', background: '#fafafa' } },
        h('div', { style: { fontSize: 12, color: '#555', marginBottom: 4 } }, '语料管理（写 corpora.json；删除只删配置，目录保留）'),
        ...corpora.map((c) =>
          h(
            'div',
            { key: c.id, style: { display: 'flex', gap: 8, alignItems: 'center', fontSize: 12, margin: '2px 0' } },
            h('span', { style: { width: 90, fontWeight: 600 } }, c.id),
            h('span', { style: { color: '#777', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, `${c.naming || 'bvid'} · ${c.archive || '-'}`),
            h('button', { type: 'button', style: { ...btnStyle, padding: '1px 8px' }, onClick: () => startEdit(c) }, '编辑'),
            h('button', { type: 'button', style: { ...btnStyle, padding: '1px 8px' }, onClick: () => removeCorpus(c.id) }, '删除'),
          ),
        ),
        mmsg ? h('div', { style: { color: '#c66', fontSize: 12, marginTop: 4 } }, mmsg) : null,
        h(
          'div',
          { style: { display: 'flex', gap: 8, marginTop: 6 } },
          h('button', { type: 'button', style: btnStyle, onClick: startCreate }, '＋ 新建语料'),
          h('button', { type: 'button', style: btnStyle, onClick: () => { setManage(false); setMmsg(null) } }, '收起'),
        ),
      )

  return h(
    'div',
    { style: cardStyle },
    h('div', { style: titleStyle }, status?.corpus ? `B 站转写 · 语料文意疏通（${status.corpus}）` : 'B 站转写 · 语料文意疏通'),
    h(
      'div',
      { style: { display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center', margin: '2px 0 8px' } },
      h('span', { style: { color: '#777', fontSize: 12 } }, '语料'),
      ...corpora.map((c) => h('button', {
        key: c.id,
        type: 'button',
        disabled: c.id === active,
        style: { ...btnStyle, padding: '2px 8px', opacity: c.id === active ? 0.55 : 1 },
        onClick: () => switchTo(c.id),
      }, c.id)),
      h('button', { type: 'button', style: { ...btnStyle, padding: '2px 8px' }, onClick: () => setManage((v) => !v) }, manage ? '收起管理' : '管理'),
    ),
    manage ? manageBox : null,
    failure
      ? h('div', { style: { color: '#e88' } }, `host 端点不可用（${failure}）。请在会话里用工具 bili_jobs / corpus_status 查看。`)
      : status
        ? h('div', null, rows)
        : h('div', null, '加载中…'),
    h(TranscribeSection, { status, refresh, naming: corpora.find((c) => c.id === active)?.naming ?? 'bvid' }),
    h(
      'div',
      { style: { display: 'flex', gap: 8, marginTop: 10 } },
      h('button', { type: 'button', style: btnStyle, onClick: () => { void refresh() } }, '刷新'),
      h(
        'button',
        { type: 'button', style: btnStyle, onClick: () => post('/pause') },
        '暂停',
      ),
      h(
        'button',
        { type: 'button', style: btnStyle, onClick: () => post('/resume') },
        '继续',
      ),
    ),
    h(
      'div',
      { style: { marginTop: 8, color: '#888', fontSize: 11, lineHeight: 1.5 } },
      '清洗批次须由用户在会话里确认后手动开（corpus_batch）；本面板不会自动开批。',
    ),
  )
}

export function apply(ctx: ClientContext): void {
  const mount = (): unknown =>
    ctx.slots.register(
      {
        name: 'conversation.view',
        id: 'dsh-bili-corpus-panel',
        label: () => 'B站语料',
      },
      CockpitPanel,
    )

  if (typeof ctx.effect === 'function') {
    ctx.effect(() => ctx.slots.inject('conversation.view', mount), 'dsh-bili-corpus: panel')
  } else {
    ctx.slots.inject('conversation.view', mount)
  }
}
