import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'

type Activity = { at?: string; timestamp?: string; level?: string; component?: string; stage?: string; event: string; code?: string; message: string }
type Pairing = { state: string; code: string | null; candidate: string | null; suggested_workspace_root: string }
type Feishu = { state: string; running: boolean; credentials_configured: boolean; operator_authorized: boolean; workspace_configured: boolean; workspace_roots: string[]; workspace_roots_valid?: boolean; invalid_workspace_roots?: string[]; workspace_policy_source: string; activity: Activity[]; pairing: Pairing; last_error_code: string | null; last_error_message: string | null }
type Model = { overall_status: string; runtime: { remote_execution_enabled: boolean; accept_new_tasks: boolean }; codex: { availability: string; doctor_status: string }; feishu: Feishu; doctor: { status: string; last_result_available: boolean } }
type Command = { status: string; error_code: string | null; message: string; current_state: unknown }
type Session = { chat_id: string; chat_type: string; state: string; thread_id: string | null; pending_cwd: string | null; updated_at: number }
type Binding = { thread_id: string; thread_name: string | null; cwd: string; last_seen_turn_id: string | null; desktop_sync_state: string; writer_state: string; active_turn_id: string | null; created_at: number | null; updated_at: number | null }
type RuntimeMetrics = { queue_ms: number | null; runtime_prep_ms: number | null; app_server_start_ms: number | null; initialize_ms: number | null; thread_resume_ms: number | null; turn_start_ms: number | null; ttfn_ms: number | null; ttft_ms: number | null; transport_retry_ms: number | null; model_response_wait_ms: number | null; model_to_generation_ms: number | null; generation_ms: number | null; tool_execution_ms: number | null; cleanup_ms: number | null; final_delivery_ms: number | null; cfr_pre_turn_ms: number | null; cfr_post_turn_ms: number | null; cfr_controlled_overhead_ms: number | null; total_ms: number | null; elapsed_ms: number | null }
type RuntimeTool = { category: string; name: string; status: string; elapsed_ms: number | null }
type RuntimeEvent = { at: number; category: string; label: string; status?: string }
type RuntimeTelemetry = { status: string; stage: string; model: string | null; reasoning_effort: string | null; service_tier: string | null; metrics: RuntimeMetrics; dominant_owner: string; last_native_activity_at: number | null; last_native_activity_age_ms: number | null; transport_state: string | null; transport_retry_count: number; transport_fallback_count: number; token_usage: Record<string, number>; model_context_window: number | null; context_used_tokens: number | null; context_usage_percent: number | null; current_tool: RuntimeTool | null; recent_tool: RuntimeTool | null; timeline: RuntimeEvent[] }
type Job = { id: string; thread_id: string | null; turn_id: string | null; status: string; active: boolean; origin: string | null; workspace: string | null; started_at: number | null; completed_at: number | null; last_activity_at: number | null; can_interrupt: boolean; runtime?: RuntimeTelemetry }
type Approval = { approval_id: string; thread_id: string; turn_id: string | null; state: string | null; decision: string | null; feedback_state: string | null; request_kind: string | null; created_at: number | null; updated_at: number | null }
type CatalogModel = { id: string | null; model: string | null; display_name: string | null; description: string | null; is_default: boolean; default_reasoning_effort: string | null; supported_reasoning_efforts: { reasoning_effort: string | null; description: string | null }[]; service_tiers: { id: string | null; name: string | null; description: string | null }[]; default_service_tier: string | null; input_modalities: string[]; supports_personality: boolean; model_specialty: string | null }
type Catalog = { available: boolean; error_code: string | null; message: string; data: CatalogModel[] }
type CapabilitySection<T> = { available: boolean; error_code: string | null; message: string; data: T[] }
type Capabilities = { context: string; permission_profiles: CapabilitySection<{ id: string | null; allowed: boolean; description: string | null }>; experimental_features: CapabilitySection<{ name: string | null; stage: string | null; enabled: boolean; default_enabled: boolean; display_name: string | null; description: string | null }> }
type DefaultValue = { effective_value: string | null; source: string }
type Settings = { available: boolean; error_code: string | null; message: string; codex_model_defaults: { applies_to: string; model: DefaultValue; reasoning_effort: DefaultValue; service_tier: DefaultValue; managed_new_thread_defaults: { model: string | null; reasoning_effort: string | null; service_tier: string | null } } | null }
type Draft = { model: string | null; reasoning_effort: string | null; service_tier: string | null }
type Page = 'runtime' | 'settings' | 'details'

const labels: Record<string, string> = { healthy: '正常', degraded: '异常', available: '可用', unavailable: '不可用', running: '运行中', stopped: '已停止', starting: '启动中', stopping: '停止中', idle: '空闲', active: '活动中', unknown: '未知', cfr_active: 'CFR 执行中', external_active: '外部执行中', completed: '已完成', failed: '失败', interrupted: '已中断', cancelled: '已取消', expired: '已过期', orphaned: '已失联', waiting_approval: '等待审批', pending: '待处理', approved: '已批准', declined: '已拒绝', PENDING: '待处理', PROCESSING: '处理中', ACKNOWLEDGED_PROCESSING: '已确认，处理中', APPROVED: '已批准', DECLINED: '已拒绝', EXECUTION_FAILED: '执行失败', accept: '批准', decline: '拒绝', runtimeDefault: '运行时默认', modelDefault: '模型默认', bound: '已绑定', unbound: '未绑定', pending_initial: '等待首个任务', pending_confirmation: '等待本机确认' }
const text = (value: string | null | undefined) => value ? labels[value] ?? value : '未提供'
const chatType = (value: string) => ({ p2p: '私聊', group: '群聊', topic: '话题' } as Record<string, string>)[value] ?? value
const csrf = () => document.cookie.split('; ').find((item) => item.startsWith('cfr_control_csrf='))?.split('=')[1] ?? ''

const metric = (value: number | null | undefined) => value == null ? '—' : value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(2)} s`
const count = (value: number | null | undefined) => value == null ? '—' : value.toLocaleString()
const wallTime = (value: number | null | undefined) => value == null ? '—' : new Date(value * 1000).toLocaleTimeString('zh-CN', { hour12: false })
const activityAge = (value: number | null | undefined) => {
  if (value == null) return '—'
  if (value < 1000) return '刚刚'
  const seconds = Math.floor(value / 1000)
  return seconds < 60 ? `${seconds}s 前` : `${Math.floor(seconds / 60)}m ${seconds % 60}s 前`
}

function RuntimeJob({ job }: { job: Job }) {
  const runtime = job.runtime
  if (!runtime) return <article><strong>{text(job.status)}</strong>{job.workspace && <span>Workspace: {job.workspace}</span>}<span>Thread: {job.thread_id ?? '—'}</span><span>Turn: {job.turn_id ?? '—'}</span></article>
  const breakdown: [string, number | null][] = [
    ['Queue', runtime.metrics.queue_ms], ['Runtime prep', runtime.metrics.runtime_prep_ms],
    ['App-server start', runtime.metrics.app_server_start_ms], ['Initialize', runtime.metrics.initialize_ms],
    ['Thread resume', runtime.metrics.thread_resume_ms], ['Turn start', runtime.metrics.turn_start_ms],
    ['TTFN', runtime.metrics.ttfn_ms], ['TTFT', runtime.metrics.ttft_ms],
    ['Transport retry', runtime.metrics.transport_retry_ms], ['Model response wait', runtime.metrics.model_response_wait_ms],
    ['Model to generation', runtime.metrics.model_to_generation_ms], ['Generation', runtime.metrics.generation_ms],
    ['Tool execution', runtime.metrics.tool_execution_ms], ['Cleanup', runtime.metrics.cleanup_ms],
    ['Final delivery', runtime.metrics.final_delivery_ms], ['CFR pre-turn', runtime.metrics.cfr_pre_turn_ms],
    ['CFR post-turn', runtime.metrics.cfr_post_turn_ms], ['Total', runtime.metrics.total_ms ?? runtime.metrics.elapsed_ms],
  ]
  const tool = runtime.current_tool ?? runtime.recent_tool
  return <article className="runtime-job">
    <strong>{text(runtime.status)} · {runtime.stage}</strong>
    <span>{runtime.model ?? '—'} / {runtime.reasoning_effort ?? '—'} / {runtime.service_tier ?? '—'}</span>
    <span>总耗时：{metric(runtime.metrics.total_ms ?? runtime.metrics.elapsed_ms)} · CFR 开销：{metric(runtime.metrics.cfr_controlled_overhead_ms)}</span>
    <span>TTFN：{metric(runtime.metrics.ttfn_ms)} · TTFT：{metric(runtime.metrics.ttft_ms)}</span>
    <span>Transport：{runtime.transport_state ?? 'normal'} · retry {runtime.transport_retry_count} · fallback {runtime.transport_fallback_count}</span>
    <span>阶段：{text(runtime.stage)} · 最近 Codex 活动：{activityAge(runtime.last_native_activity_age_ms)}</span>
    <span>主导耗时：{runtime.dominant_owner}</span>
    {job.workspace && <span>Workspace: {job.workspace}</span>}<span>Thread: {job.thread_id ?? '—'} · Turn: {job.turn_id ?? '—'}</span>
    <h3>Runtime breakdown</h3><div className="runtime-metrics">{breakdown.map(([label, value]) => <span key={label}>{label}: {metric(value)}</span>)}</div>
    <h3>Tokens and context</h3><div className="runtime-metrics"><span>Input: {count(runtime.token_usage.input_tokens)}</span><span>Cached input: {count(runtime.token_usage.cached_input_tokens)}</span><span>Cache write: {count(runtime.token_usage.cache_write_input_tokens)}</span><span>Output: {count(runtime.token_usage.output_tokens)}</span><span>Reasoning: {count(runtime.token_usage.reasoning_output_tokens)}</span><span>Total: {count(runtime.token_usage.total_tokens)}</span><span>Context: {count(runtime.context_used_tokens)} / {count(runtime.model_context_window)} ({runtime.context_usage_percent == null ? '—' : `${runtime.context_usage_percent}%`})</span></div>
    <h3>Tool activity</h3><span>{tool ? `${tool.name} · ${tool.status} · ${metric(tool.elapsed_ms)}` : '—'}</span>
    <h3>Activity timeline</h3><ol className="runtime-timeline">{runtime.timeline.slice(-20).map((event, index) => <li key={`${event.at}:${index}`}><time>{wallTime(event.at)}</time><span>{event.label}{event.status ? ` · ${event.status}` : ''}</span></li>)}</ol>
  </article>
}

function normalizeFeishu(value: Partial<Feishu> | undefined): Feishu {
  const defaults: Feishu = { state: 'stopped', running: false, credentials_configured: false, operator_authorized: false, workspace_configured: false, workspace_roots: [], workspace_policy_source: '', activity: [], last_error_code: null, last_error_message: null, pairing: { state: 'idle', code: null, candidate: null, suggested_workspace_root: '' } }
  return { ...defaults, ...value, workspace_roots: Array.isArray(value?.workspace_roots) ? value.workspace_roots : [], invalid_workspace_roots: Array.isArray(value?.invalid_workspace_roots) ? value.invalid_workspace_roots : [], activity: Array.isArray(value?.activity) ? value.activity : [], pairing: { ...defaults.pairing, ...value?.pairing } }
}

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: 'same-origin', ...options, headers: { 'Content-Type': 'application/json', ...(options?.method && options.method !== 'GET' ? { 'X-CFR-CSRF': csrf() } : {}), ...options?.headers } })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.message || '控制 API 请求失败')
  return payload
}

function App() {
  const [page, setPage] = useState<Page>('runtime')
  const [model, setModel] = useState<Model | null>(null)
  const [sessions, setSessions] = useState<Session[]>([])
  const [bindings, setBindings] = useState<Binding[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [approvals, setApprovals] = useState<Approval[]>([])
  const [models, setModels] = useState<Catalog | null>(null)
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [doctor, setDoctor] = useState<unknown>({ Verdict: 'NOT_RUN' })
  const [draft, setDraft] = useState<Draft>({ model: null, reasoning_effort: null, service_tier: null })
  const [workspace, setWorkspace] = useState('')
  const [pairingWorkspace, setPairingWorkspace] = useState('')
  const [backendSkew, setBackendSkew] = useState(false)
  const [message, setMessage] = useState('正在加载控制状态…')
  const [busy, setBusy] = useState(false)

  const updateFeishu = (value: Partial<Feishu>) => setModel((current) => current ? { ...current, feishu: normalizeFeishu(value) } : current)
  const updateActivity = (activity: Activity[]) => setModel((current) => current ? { ...current, feishu: { ...current.feishu, activity: Array.isArray(activity) ? activity : current.feishu.activity } } : current)
  const refresh = async () => {
    const [status, sessionsResult, bindingsResult, jobsResult, approvalsResult, modelsResult, capabilitiesResult, settingsResult, doctorResult] = await Promise.all([
      api<{ current_state: Model }>('/api/v1/status'), api<{ current_state: Session[] }>('/api/v1/sessions'), api<{ current_state: Binding[] }>('/api/v1/bindings'), api<{ current_state: Job[] }>('/api/v1/jobs'), api<{ current_state: Approval[] }>('/api/v1/approvals'), api<{ current_state: Catalog }>('/api/v1/models'), api<{ current_state: Capabilities }>('/api/v1/capabilities'), api<{ current_state: Settings }>('/api/v1/settings'), api<{ current_state: unknown }>('/api/v1/doctor'),
    ])
    const rawFeishu = status.current_state.feishu
    setBackendSkew(!Array.isArray(rawFeishu.workspace_roots) || !Array.isArray(rawFeishu.activity) || !rawFeishu.workspace_policy_source)
    setModel({ ...status.current_state, feishu: normalizeFeishu(rawFeishu) })
    setSessions(sessionsResult.current_state); setBindings(bindingsResult.current_state); setJobs(jobsResult.current_state); setApprovals(approvalsResult.current_state); setModels(modelsResult.current_state); setCapabilities(capabilitiesResult.current_state); setSettings(settingsResult.current_state); setDoctor(doctorResult.current_state)
    const defaults = settingsResult.current_state.codex_model_defaults
    if (defaults) setDraft({ model: defaults.model.source === 'runtimeDefault' ? null : defaults.model.effective_value, reasoning_effort: defaults.reasoning_effort.source === 'modelDefault' ? null : defaults.reasoning_effort.effective_value, service_tier: defaults.service_tier.source === 'modelDefault' ? null : defaults.service_tier.effective_value })
  }

  useEffect(() => { refresh().then(() => setMessage('控制 API 已连接。')).catch((error: Error) => setMessage(error.message)) }, [])
  useEffect(() => {
    const state = model?.feishu.pairing.state
    if (state !== 'starting' && state !== 'waiting') return
    const timer = window.setInterval(() => api<{ current_state: Feishu }>('/api/v1/feishu').then((result) => updateFeishu(result.current_state)).catch(() => undefined), 1200)
    return () => window.clearInterval(timer)
  }, [model?.feishu.pairing.state])
  useEffect(() => {
    if (page !== 'runtime' || (!busy && !model?.feishu.running)) return
    const timer = window.setInterval(() => api<{ current_state: Activity[] }>('/api/v1/feishu/activity').then((result) => updateActivity(result.current_state)).catch(() => undefined), 1000)
    return () => window.clearInterval(timer)
  }, [busy, page, model?.feishu.running])
  useEffect(() => {
    if (!model?.feishu.running) return
    const refreshOperationalState = () => Promise.all([api<{ current_state: Session[] }>('/api/v1/sessions'), api<{ current_state: Job[] }>('/api/v1/jobs')]).then(([sessionResult, jobResult]) => { setSessions(sessionResult.current_state); setJobs(jobResult.current_state) }).catch(() => undefined)
    const timer = window.setInterval(refreshOperationalState, 1200)
    return () => window.clearInterval(timer)
  }, [model?.feishu.running])

  const command = async (path: string, body?: object, success?: string) => {
    setBusy(true)
    try { const result = await api<Command>(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined }); if (result.status !== 'ok') throw new Error(`${result.error_code}: ${result.message}`); setMessage(success ?? result.message); await refresh() }
    catch (error) { setMessage(error instanceof Error ? error.message : '控制 API 请求失败') }
    finally { setBusy(false) }
  }
  const openDesktop = async (threadId: string) => {
    setBusy(true)
    try { const result = await api<Command>(`/api/v1/threads/${encodeURIComponent(threadId)}/open-desktop`, { method: 'POST' }); if (result.status !== 'ok') throw new Error(`${result.error_code}: ${result.message}`); setMessage('已请求在 Codex Desktop 中打开。') }
    catch (error) { setMessage(error instanceof Error ? error.message : '无法打开 Codex Desktop') }
    finally { setBusy(false) }
  }
  const copyPairingMessage = async (code: string) => {
    try { await navigator.clipboard.writeText(`绑定 ${code}`); setMessage('配对消息已复制。') }
    catch { setMessage('无法使用剪贴板，请手动复制配对消息。') }
  }
  const copyThreadId = async (threadId: string) => {
    try { await navigator.clipboard.writeText(threadId); setMessage('完整 Thread ID 已复制。') }
    catch { setMessage('无法使用剪贴板，请手动复制 Thread ID。') }
  }
  const saveDefaults = async () => {
    setBusy(true)
    try { const result = await api<Command>('/api/v1/settings/codex/model-defaults', { method: 'PUT', body: JSON.stringify(draft) }); setMessage(result.message); await refresh() }
    catch (error) { setMessage(error instanceof Error ? error.message : '保存失败') }
    finally { setBusy(false) }
  }
  if (!model) return <main className="loading">{message}</main>

  const feishu = model.feishu; const pairing = feishu.pairing
  const pairingActive = ['starting', 'waiting', 'pending_confirmation'].includes(pairing.state)
  const workspaceRootsValid = feishu.workspace_roots_valid !== false
  const invalidRoots = feishu.invalid_workspace_roots ?? []
  const lifecycleTransitioning = ['starting', 'stopping'].includes(feishu.state)
  const startBlocked = backendSkew || !workspaceRootsValid || !feishu.operator_authorized || !feishu.workspace_configured || pairingActive
  const defaultCatalogModel = models?.data.find((item) => item.is_default) ?? null
  const selectedModel = draft.model === null ? defaultCatalogModel : models?.data.find((item) => item.model === draft.model) ?? null
  const modelUnavailable = draft.model !== null && !selectedModel
  const changeModel = (value: string) => {
    const nextModel = value || null
    const selected = nextModel === null ? defaultCatalogModel : models?.data.find((item) => item.model === nextModel) ?? null
    setDraft((current) => ({ model: nextModel, reasoning_effort: selected?.supported_reasoning_efforts.some((item) => item.reasoning_effort === current.reasoning_effort) ? current.reasoning_effort : null, service_tier: selected?.service_tiers.some((item) => item.id === current.service_tier) ? current.service_tier : null }))
  }
  const rowTime = (item: Activity) => {
    const value = item.timestamp ?? item.at ?? ''
    const date = new Date(value)
    return Number.isNaN(date.getTime()) ? value.slice(11, 19) : date.toLocaleTimeString('zh-CN', { hour12: false })
  }
  const pendingApprovals = approvals.filter((item) => item.state === 'pending' || item.feedback_state === 'PENDING').length

  const pairingPanel = <section className="panel"><h2>飞书账号 / 当前绑定状态</h2><p>状态：{text(pairing.state)}</p>
    {feishu.credentials_configured && !feishu.operator_authorized && !pairingActive && <button onClick={() => command('/api/v1/feishu/pairing/start')} disabled={busy || feishu.state !== 'stopped'}>绑定我的飞书账号</button>}
    {pairing.state === 'waiting' && pairing.code && <><p>请向 CFR 飞书机器人私聊发送：绑定 {pairing.code}</p><button onClick={() => copyPairingMessage(pairing.code!)} disabled={busy}>复制配对消息</button><button onClick={() => command('/api/v1/feishu/pairing/cancel')} disabled={busy}>取消配对</button></>}
    {pairing.state === 'pending_confirmation' && <><p>检测到候选账号 {pairing.candidate}，请确认本地工作区。</p><label>允许的本地工作区<input value={pairingWorkspace || pairing.suggested_workspace_root} onChange={(event) => setPairingWorkspace(event.target.value)} disabled={busy} /></label><button onClick={() => setPairingWorkspace(pairing.suggested_workspace_root)} disabled={busy}>使用当前 CFR 目录</button><button onClick={() => command('/api/v1/feishu/pairing/confirm', { workspace_root: pairingWorkspace || pairing.suggested_workspace_root })} disabled={busy}>确认绑定</button><button onClick={() => command('/api/v1/feishu/pairing/cancel')} disabled={busy}>取消</button></>}
    {pairing.state === 'expired' && <button onClick={() => command('/api/v1/feishu/pairing/start')} disabled={busy || feishu.state !== 'stopped'}>重新开始绑定</button>}
  </section>

  return <main>
    <header><div><p className="eyebrow">CFR 本地控制平面</p><h1>控制中心</h1></div><button onClick={() => refresh()} disabled={busy}>刷新</button></header>
    <nav aria-label="控制中心页面"><button className={page === 'runtime' ? 'selected' : ''} onClick={() => setPage('runtime')}>运行</button><button className={page === 'settings' ? 'selected' : ''} onClick={() => setPage('settings')}>配置</button><button className={page === 'details' ? 'selected' : ''} onClick={() => setPage('details')}>详情</button></nav>
    <p className="message" role="status">{message}</p>
    {backendSkew && <section className="error"><strong>控制中心与后端版本不兼容</strong><span>检测到正在运行的 CFR 后端版本较旧，与当前控制中心不兼容。请关闭旧 CFR 后重新启动 START_CFR.cmd。</span></section>}

    {page === 'runtime' && <>
      <section className="grid"><article><h2>CFR 总体状态</h2><strong>{text(model.overall_status)}</strong></article><article><h2>Codex</h2><strong>{text(model.codex.availability)}</strong><span>诊断：{text(model.codex.doctor_status)}</span></article><article><h2>飞书</h2><strong>{text(feishu.state)}</strong><span>账号：{feishu.operator_authorized ? '已绑定' : '未绑定'}</span></article><article><h2>任务接收</h2><strong>{model.runtime.accept_new_tasks ? '开启' : '排空'}</strong><span>{model.runtime.remote_execution_enabled ? '远程执行已启用' : '远程执行已禁用'}</span></article></section>
      {!workspaceRootsValid && <section className="error"><strong>工作区配置无效</strong>{invalidRoots.map((root) => <span key={root}>{root}：路径不存在。</span>)}<button onClick={() => setPage('settings')}>前往配置</button></section>}
      <section className="controls"><div><h2>运行控制</h2><button onClick={() => command('/api/v1/feishu/start')} disabled={busy || lifecycleTransitioning || feishu.running || feishu.state === 'degraded' || startBlocked}>启动飞书</button><button onClick={() => command('/api/v1/feishu/stop')} disabled={busy || lifecycleTransitioning || (!feishu.running && feishu.state !== 'degraded' && !pairingActive)}>停止飞书</button><button onClick={() => command('/api/v1/feishu/reconnect')} disabled={busy || lifecycleTransitioning || feishu.state === 'degraded' || pairingActive || !workspaceRootsValid || backendSkew}>重新连接</button>{startBlocked && !pairingActive && <p>{!workspaceRootsValid ? '请先修复无效工作区。' : '请先完成飞书账号和工作区配置。'}</p>}</div><div><h2>任务控制</h2><button onClick={() => command('/api/v1/runtime/remote-execution', { enabled: !model.runtime.remote_execution_enabled })} disabled={busy}>切换远程执行</button><button onClick={() => command('/api/v1/runtime/accept-new-work', { enabled: !model.runtime.accept_new_tasks })} disabled={busy}>切换新任务接收</button><button onClick={() => command('/api/v1/runtime/drain', undefined, '已暂停接收新任务。')} disabled={busy}>排空</button><button onClick={() => command('/api/v1/doctor')} disabled={busy}>运行诊断 / 健康检查</button></div></section>
      <section className="panel"><h2>运行日志 / Runtime Console</h2><div className="console" aria-label="运行日志">{feishu.activity.length === 0 ? <span>暂无安全运行事件。</span> : feishu.activity.map((item, index) => <div className={`console-row ${item.level ?? 'info'}`} key={`${item.timestamp ?? item.at}-${index}`}><time>{rowTime(item)}</time><b>{(item.level ?? 'info').toUpperCase()}</b><span>{item.component ?? 'control'}{item.stage ? `.${item.stage}` : ''}</span><code>{item.code ?? item.event}</code><em>{item.message}</em></div>)}</div></section>
      <section className="summary"><span>当前 Session：{sessions.length}</span><span>当前 Job：{jobs.filter((job) => job.active).length || 'idle'}</span><span>待处理 Approval：{pendingApprovals}</span><button onClick={() => setPage('details')}>查看详情</button></section>
    </>}

    {page === 'settings' && <>
      {pairingPanel}
      <section className="panel"><h2>允许的工作区</h2><p>策略来源：{feishu.workspace_policy_source || '未知'}。修改仅影响后续新会话。</p>{feishu.workspace_roots.length === 0 ? <p>尚未配置工作区。</p> : feishu.workspace_roots.map((root) => <article key={root}><span>{root}{invalidRoots.includes(root) && ' — 路径不存在'}</span><button onClick={() => command('/api/v1/feishu/workspaces/remove', { workspace_root: root })} disabled={busy || backendSkew || feishu.running || pairingActive || feishu.workspace_policy_source === 'environment'}>移除</button></article>)}<label>新增绝对工作区<input value={workspace} onChange={(event) => setWorkspace(event.target.value)} disabled={busy || backendSkew || feishu.running || pairingActive || feishu.workspace_policy_source === 'environment'} /></label><button onClick={() => command('/api/v1/feishu/workspaces/add', { workspace_root: workspace })} disabled={busy || !workspace || backendSkew || feishu.running || pairingActive || feishu.workspace_policy_source === 'environment'}>添加工作区</button><button onClick={() => setWorkspace(pairing.suggested_workspace_root)} disabled={busy || backendSkew || feishu.workspace_policy_source === 'environment'}>使用当前 CFR 目录</button>{feishu.workspace_policy_source === 'environment' && <p>工作区由环境变量管理，当前为只读。</p>}</section>
      <section className="panel"><h2>Codex 默认设置</h2><p>仅用于新建线程；当前线程不会改变。</p>{!settings?.available || !settings.codex_model_defaults ? <p>{settings?.message ?? 'Codex 设置不可用。'}</p> : <><span>有效模型：{settings.codex_model_defaults.model.effective_value ?? 'Codex 默认'}（{text(settings.codex_model_defaults.model.source)}）</span><span>有效推理强度：{settings.codex_model_defaults.reasoning_effort.effective_value ?? '模型默认'}（{text(settings.codex_model_defaults.reasoning_effort.source)}）</span><span>有效服务层级：{settings.codex_model_defaults.service_tier.effective_value ?? '模型默认'}（{text(settings.codex_model_defaults.service_tier.source)}）</span><span>受管理的新线程默认值：{settings.codex_model_defaults.managed_new_thread_defaults.model ?? '无'} / {settings.codex_model_defaults.managed_new_thread_defaults.reasoning_effort ?? '无'} / {settings.codex_model_defaults.managed_new_thread_defaults.service_tier ?? '无'}</span><label>模型<select value={draft.model ?? ''} onChange={(event) => changeModel(event.target.value)} disabled={busy || !models?.available}><option value="">使用 Codex 默认值</option>{models?.data.map((item) => item.model && <option key={item.id ?? item.model} value={item.model}>{item.display_name ?? item.model}</option>)}</select></label>{modelUnavailable && <p>当前配置的模型不在已安装运行时的模型目录中。</p>}<label>推理强度<select value={draft.reasoning_effort ?? ''} onChange={(event) => setDraft((value) => ({ ...value, reasoning_effort: event.target.value || null }))} disabled={busy || !selectedModel}><option value="">使用模型默认值</option>{selectedModel?.supported_reasoning_efforts.map((item) => item.reasoning_effort && <option key={item.reasoning_effort} value={item.reasoning_effort}>{item.reasoning_effort}</option>)}</select></label><label>服务层级<select value={draft.service_tier ?? ''} onChange={(event) => setDraft((value) => ({ ...value, service_tier: event.target.value || null }))} disabled={busy || !selectedModel}><option value="">使用模型默认值</option>{selectedModel?.service_tiers.map((item) => item.id && <option key={item.id} value={item.id}>{item.name ?? item.id}</option>)}</select></label><button onClick={saveDefaults} disabled={busy || !settings.available || !models?.available || !selectedModel || modelUnavailable}>保存默认设置</button></>}</section>
    </>}

    {page === 'details' && <>
      <section className="panel"><h2>Codex Runtime</h2><p>Bounded, process-local telemetry for active and recent turns. Missing values are shown as —.</p>{jobs.length === 0 ? <p>No observable CFR jobs.</p> : jobs.map((job) => <RuntimeJob key={job.id} job={job} />)}</section>
      <section className="controls"><div><h2>当前飞书绑定</h2>{sessions.length === 0 ? <p>没有当前飞书 chat 绑定。</p> : sessions.map((session) => <article key={session.chat_id}><strong>{text(session.state)}</strong><span>聊天类型：{chatType(session.chat_type)}</span><span>聊天 ID：{session.chat_id}</span><span>线程 ID：{session.thread_id ?? '无'}</span>{session.pending_cwd && <span>待确认工作区：{session.pending_cwd}</span>}<span>更新时间：{session.updated_at}</span><button onClick={() => { if (window.confirm('解除这个飞书会话绑定？不会删除 Codex 线程。')) command(`/api/v1/sessions/${encodeURIComponent(session.chat_id)}/unbind`, undefined, '会话已解除绑定。') }} disabled={busy}>解除绑定</button></article>)}</div><div className="catalog-panel"><h2>可用 CFR 会话</h2>{bindings.length === 0 ? <p>没有可用的 CFR/Codex 会话。</p> : bindings.map((binding) => { const desktopSafe = !jobs.some((job) => job.active && job.thread_id === binding.thread_id) && binding.writer_state === 'idle'; return <article key={binding.thread_id}><strong>{binding.thread_name ?? binding.thread_id}</strong><span>线程：{binding.thread_id}</span><span>工作区：{binding.cwd}</span><span>最近 Turn：{binding.last_seen_turn_id ?? '无'}</span><span>Writer：{text(binding.writer_state)}</span>{binding.active_turn_id && <span>活动 Turn：{binding.active_turn_id}</span>}<span>桌面同步：{text(binding.desktop_sync_state)}</span><button onClick={() => openDesktop(binding.thread_id)} disabled={busy || !desktopSafe}>在 Codex Desktop 中打开</button><button onClick={() => copyThreadId(binding.thread_id)} disabled={busy}>复制完整 Thread ID</button>{!desktopSafe && <span>仅可打开无活动 Turn 且 Writer 空闲的会话。</span>}</article> })}</div></section>
      <section className="controls"><div><h2>Approvals</h2><p>审批决定仍在飞书中处理。</p>{approvals.length === 0 ? <p>没有持久化的 CFR 审批记录。</p> : approvals.map((item) => <article key={item.approval_id}><strong>{text(item.feedback_state ?? item.state)}</strong><span>决定：{text(item.decision ?? 'pending')}</span><span>线程：{item.thread_id}</span><span>轮次：{item.turn_id ?? '无'}</span>{item.request_kind && <span>类型：{item.request_kind}</span>}<span>创建时间：{item.created_at ?? '未知'}</span><span>更新时间：{item.updated_at ?? '未知'}</span></article>)}</div><div><h2>Doctor / Health</h2><pre className="doctor">{JSON.stringify(doctor, null, 2)}</pre></div></section>
      <section className="controls"><div className="catalog-panel"><h2>Models</h2>{!models?.available ? <p>{models?.message ?? '模型目录不可用。'}</p> : models.data.map((item) => <article key={item.id ?? item.model ?? 'model'}><strong>{item.display_name ?? item.model ?? item.id}</strong>{item.is_default && <span>默认</span>}<span>模型：{item.model ?? '不可用'}</span>{item.description && <span>{item.description}</span>}{item.supported_reasoning_efforts.length > 0 && <span>推理强度：{item.supported_reasoning_efforts.map((value) => value.reasoning_effort ?? '不可用').join(', ')}</span>}{item.service_tiers.length > 0 && <span>服务层级：{item.service_tiers.map((value) => value.name ?? value.id ?? '不可用').join(', ')}</span>}{item.input_modalities.length > 0 && <span>输入模态：{item.input_modalities.join(', ')}</span>}{item.supports_personality && <span>支持人格设定</span>}</article>)}</div><div className="catalog-panel"><h2>Capabilities</h2><p>上下文：{capabilities?.context ?? '未知'}</p><h3>权限配置</h3>{!capabilities?.permission_profiles.available ? <p>{capabilities?.permission_profiles.message ?? '能力目录不可用。'}</p> : capabilities.permission_profiles.data.map((item) => <article key={item.id ?? 'profile'}><strong>{item.id ?? '不可用'}</strong><span>允许：{item.allowed ? '是' : '否'}</span>{item.description && <span>{item.description}</span>}</article>)}<h3>实验性功能</h3>{!capabilities?.experimental_features.available ? <p>{capabilities?.experimental_features.message ?? '能力目录不可用。'}</p> : capabilities.experimental_features.data.map((item) => <article key={item.name ?? 'feature'}><strong>{item.display_name ?? item.name ?? '不可用'}</strong><span>阶段：{item.stage ?? '不可用'}</span><span>{item.enabled ? '已启用' : '已禁用'} / 默认 {item.default_enabled ? '启用' : '禁用'}</span>{item.description && <span>{item.description}</span>}</article>)}</div></section>
    </>}
    {feishu.last_error_code && page !== 'runtime' && <section className="error"><strong>{feishu.last_error_code}</strong><span>{feishu.last_error_message}</span></section>}
  </main>
}

createRoot(document.getElementById('root')!).render(<App />)
