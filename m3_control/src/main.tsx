import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'

type Activity = { at?: string; timestamp?: string; level?: string; component?: string; stage?: string; event: string; code?: string; message: string }
type Pairing = { state: string; code: string | null; candidate: string | null; suggested_workspace_root: string }
type FeishuRuntime = { queue_depth: number; queued_chats: number; active_chats: number; workers_total: number; workers_alive: number; heartbeat_alive: boolean }
type Feishu = { state: string; running: boolean; credentials_configured: boolean; operator_authorized: boolean; workspace_configured: boolean; workspace_roots: string[]; workspace_roots_valid?: boolean; invalid_workspace_roots?: string[]; workspace_policy_source: string; activity: Activity[]; runtime?: FeishuRuntime | null; transport_state?: string | null; transport_last_event_error?: { at: number; type: string; message: string } | null; pairing: Pairing; last_error_code: string | null; last_error_message: string | null }
type Model = { overall_status: string; runtime: { remote_execution_enabled: boolean; accept_new_tasks: boolean }; codex: { availability: string; doctor_status: string }; feishu: Feishu; doctor: { status: string; last_result_available: boolean } }
type Command = { status: string; error_code: string | null; message: string; current_state: unknown }
type Session = { chat_id: string; chat_type: string; state: string; selected_surface: 'code' | 'chat'; thread_id: string | null; pending_cwd: string | null; pending_settings?: Draft | null; approval_mode: 'ask' | 'auto' | 'full'; chat_state: string; chat_tab_id: string | null; chat_url: string | null; chat_project_id: string | null; chat_conversation_id: string | null; updated_at: number }
type Binding = { thread_id: string; thread_name: string | null; cwd: string; last_seen_turn_id: string | null; desktop_sync_state: string; writer_state: string; active_turn_id: string | null; bound_chat_id: string | null; created_at: number | null; updated_at: number | null }
type RuntimeMetrics = { queue_ms: number | null; runtime_prep_ms: number | null; app_server_start_ms: number | null; initialize_ms: number | null; thread_resume_ms: number | null; turn_start_ms: number | null; ttfn_ms: number | null; ttft_ms: number | null; transport_retry_ms: number | null; model_response_wait_ms: number | null; model_to_generation_ms: number | null; generation_ms: number | null; tool_execution_ms: number | null; cleanup_ms: number | null; final_delivery_ms: number | null; cfr_pre_turn_ms: number | null; cfr_post_turn_ms: number | null; cfr_controlled_overhead_ms: number | null; total_ms: number | null; elapsed_ms: number | null }
type RuntimeTool = { category: string; name: string; status: string; elapsed_ms: number | null }
type RuntimeEvent = { at: number; category: string; label: string; status?: string }
type RuntimeTelemetry = { status: string; stage: string; model: string | null; reasoning_effort: string | null; service_tier: string | null; metrics: RuntimeMetrics; dominant_owner: string; last_native_activity_at: number | null; last_native_activity_age_ms: number | null; transport_state: string | null; transport_retry_count: number; transport_fallback_count: number; token_usage: Record<string, number>; model_context_window: number | null; context_used_tokens: number | null; context_usage_percent: number | null; current_tool: RuntimeTool | null; recent_tool: RuntimeTool | null; timeline: RuntimeEvent[] }
type ChatTrace = { event: string; owner: string; at: number; duration_ms: number; elapsed_ms?: number | null; detail?: string | null }
type Job = { id: string; surface?: 'code' | 'chat'; thread_id: string | null; turn_id: string | null; status: string; active: boolean; origin: string | null; workspace: string | null; started_at: number | null; completed_at: number | null; last_activity_at: number | null; can_interrupt: boolean; runtime?: RuntimeTelemetry; chat_id?: string | null; conversation_id?: string | null; url?: string | null; stage?: string | null; attachment_count?: number | null; output_count?: number | null; error_code?: string | null; queue_ms?: number | null; total_ms?: number | null; current_owner?: string | null; timings_ms?: Record<string, number>; owner_timings_ms?: Record<string, number>; timeline?: ChatTrace[]; artifact_failures?: { name: string; error_code: string }[] }
type Approval = { approval_id: string; thread_id: string; turn_id: string | null; state: string | null; decision: string | null; feedback_state: string | null; request_kind: string | null; created_at: number | null; updated_at: number | null }
type CatalogModel = { id: string | null; model: string | null; display_name: string | null; description: string | null; is_default: boolean; default_reasoning_effort: string | null; supported_reasoning_efforts: { reasoning_effort: string | null; description: string | null }[]; service_tiers: { id: string | null; name: string | null; description: string | null }[]; default_service_tier: string | null; input_modalities: string[]; supports_personality: boolean; model_specialty: string | null; multi_agent_version?: string | null; availability_message?: string | null; upgrade_model?: string | null; extensions?: Record<string, unknown> }
type Catalog = { available: boolean; error_code: string | null; message: string; source?: string; catalog_schema_version?: number; data: CatalogModel[] }
type CapabilitySection<T> = { available: boolean; error_code: string | null; message: string; data: T[] }
type Capabilities = { context: string; permission_profiles: CapabilitySection<{ id: string | null; allowed: boolean; description: string | null }>; experimental_features: CapabilitySection<{ name: string | null; stage: string | null; enabled: boolean; default_enabled: boolean; display_name: string | null; description: string | null }> }
type Surface = { id: string; name: string; authority: string; available: boolean; status: string; description: string }
type Surfaces = { selected: string; chat_id: string | null; data: Surface[] }
type DefaultValue = { effective_value: string | null; source: string }
type Settings = { available: boolean; error_code: string | null; message: string; codex_model_defaults: { applies_to: string; model: DefaultValue; reasoning_effort: DefaultValue; service_tier: DefaultValue; managed_new_thread_defaults: { model: string | null; reasoning_effort: string | null; service_tier: string | null } } | null }
type Draft = { model: string | null; reasoning_effort: string | null; service_tier: string | null }
type SetupCheck = { key: string; label: string; required: boolean; ready: boolean; state: string; detail: string; action: string | null }
type ChatBackendState = { available: boolean; authenticated: boolean; login_pending: boolean; ready: boolean; profile_dir?: string; state_dir?: string; storage_path?: string }
type ChromeUseSetup = { installed: boolean; version: string | null; host_installed: boolean; host_healthy: boolean; relay_up: boolean; extension_version: string | null; session_running?: boolean; silent_window?: { session: string; tab_count: number; window_count: number; isolated: boolean; minimized: boolean } | null }
type SetupState = { ready: boolean; first_run: boolean; blocking: string[]; selected_surface: 'code' | 'chat'; network: { mode: 'auto' | 'direct' | 'proxy'; proxy_url: string | null; effective_mode: string; effective_source: string; effective_proxy: string | null }; codex: { executable: string | null; version: string | null; login_status: string | null; auth_mode: string | null; validated: boolean; validated_at: string | null; desktop_launcher_preference: 'auto' | 'codexhost' | 'stock'; desktop_runtime_mode: string | null; desktop_running: boolean; codexhost_available: boolean; codexhost_command: string | null; codexhost_version: string | null }; chat: { mode: string; preference: 'auto' | 'embedded' | 'dedicated' | 'shared'; effective_backend: 'embedded' | 'dedicated' | 'shared'; runtime_backend: 'embedded' | 'dedicated' | 'shared' | null; chrome_available: boolean; npx_available: boolean; authenticated: boolean | null; login_pending: boolean; profile_dir: string | null; ready: boolean; legacy_profile_preserved: boolean; linux_shared_tab?: boolean; shared_tab_name?: string | null; dedicated: ChatBackendState; embedded: ChatBackendState; shared: ChatBackendState; chrome_use?: ChromeUseSetup }; feishu: { app_id_configured: boolean; app_secret_configured: boolean; operator_count: number; workspace_roots: string[]; invalid_workspace_roots: string[]; sdk_installed: boolean; sdk_version: string | null }; checks: SetupCheck[] }
type Storage = { database_bytes: number; wal_bytes: number; shm_bytes: number; cfr_storage_bytes: number; codex_rollout_bytes: number; codex_rollout_count: number; largest_codex_rollout_bytes: number }
type Operational = { feishu: Feishu; sessions: Session[]; bindings: Binding[]; jobs: Job[]; surfaces: Surfaces; activity: Activity[]; storage: Storage }
type Page = 'setup' | 'runtime' | 'settings' | 'details'

const labels: Record<string, string> = { healthy: '正常', degraded: '异常', available: '可用', unavailable: '不可用', running: '运行中', stopped: '已停止', starting: '启动中', stopping: '停止中', ready: '就绪', reconnecting: '重连中', idle: '空闲', active: '活动中', unknown: '未知', cfr_active: 'CFR 执行中', external_active: '外部执行中', completed: '已完成', failed: '失败', interrupted: '已中断', cancelled: '已取消', expired: '已过期', orphaned: '已失联', waiting_approval: '等待审批', pending: '待处理', approved: '已批准', declined: '已拒绝', PENDING: '待处理', PROCESSING: '处理中', ACKNOWLEDGED_PROCESSING: '已确认，处理中', APPROVED: '已批准', DECLINED: '已拒绝', EXECUTION_FAILED: '执行失败', accept: '批准', decline: '拒绝', runtimeDefault: '运行时默认', modelDefault: '模型默认', bound: '已绑定', unbound: '未绑定', pending_initial: '等待首个任务', pending_confirmation: '等待本机确认', conversation: '已绑定 Conversation', new_pending: '新对话待首条消息' }
const text = (value: string | null | undefined) => value ? labels[value] ?? value : '未提供'
const chatType = (value: string) => ({ p2p: '私聊', group: '群聊', topic: '话题' } as Record<string, string>)[value] ?? value
const csrf = () => document.cookie.split('; ').find((item) => item.startsWith('cfr_control_csrf='))?.split('=')[1] ?? ''

const metric = (value: number | null | undefined) => value == null ? '—' : value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(2)} s`
const count = (value: number | null | undefined) => value == null ? '—' : value.toLocaleString()
const bytes = (value: number | null | undefined) => value == null ? '—' : value < 1024 ? `${value} B` : value < 1024 ** 2 ? `${(value / 1024).toFixed(1)} KiB` : value < 1024 ** 3 ? `${(value / 1024 ** 2).toFixed(1)} MiB` : `${(value / 1024 ** 3).toFixed(2)} GiB`
const wallTime = (value: number | null | undefined) => value == null ? '—' : new Date(value * 1000).toLocaleTimeString('zh-CN', { hour12: false })
const activityAge = (value: number | null | undefined) => {
  if (value == null) return '—'
  if (value < 1000) return '刚刚'
  const seconds = Math.floor(value / 1000)
  return seconds < 60 ? `${seconds}s 前` : `${Math.floor(seconds / 60)}m ${seconds % 60}s 前`
}

function RuntimeJob({ job }: { job: Job }) {
  const runtime = job.runtime
  if (job.surface === 'chat') {
    const timings = Object.entries(job.timings_ms ?? {}).sort((a, b) => b[1] - a[1])
    const owners = Object.entries(job.owner_timings_ms ?? {}).sort((a, b) => b[1] - a[1])
    return <article className="runtime-job"><strong>Chat · {text(job.status)} · {job.stage ?? '—'}</strong>
      <span>开始：{job.started_at ? new Date(job.started_at * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'} · 总耗时：{metric(job.total_ms)}</span>
      <span>队列：{metric(job.queue_ms)} · 当前等待归属：{job.current_owner ?? '—'}</span>
      <span>Conversation：{job.conversation_id ?? (job.active ? '正在建立/读取' : '未确认')}</span><span>附件：{job.attachment_count ?? 0} · 已交付：{job.output_count ?? 0}</span>{job.error_code && <span>错误：{job.error_code}</span>}{job.url && <span>URL：{job.url}</span>}
      <h3>Chat / Browser / OpenAI / Feishu 耗时</h3><div className="runtime-metrics">{owners.length ? owners.map(([owner, value]) => <span key={owner}>{owner}: {metric(value)}</span>) : <span>等待阶段采样…</span>}</div>
      <h3>阶段耗时</h3><div className="runtime-metrics">{timings.length ? timings.slice(0, 18).map(([stage, value]) => <span key={stage}>{stage}: {metric(value)}</span>) : <span>等待阶段采样…</span>}</div>
      {Boolean(job.artifact_failures?.length) && <><h3>Artifact errors</h3>{job.artifact_failures!.map((item, index) => <span key={`${item.name}:${index}`}>{item.name} · {item.error_code}</span>)}</>}
      <h3>Activity timeline</h3><ol className="runtime-timeline">{(job.timeline ?? []).slice(-30).map((event, index) => <li key={`${event.at}:${index}`}><time>{wallTime(event.at)}</time><span>{event.event} · {event.owner} · 本阶段 {metric(event.duration_ms)}{event.elapsed_ms != null ? ` · 累计 ${metric(event.elapsed_ms)}` : ''}{event.detail ? ` · ${event.detail}` : ''}</span></li>)}</ol>
    </article>
  }
  if (!runtime) return <article><strong>Code · {text(job.status)}</strong>{job.workspace && <span>Workspace: {job.workspace}</span>}<span>Thread: {job.thread_id ?? '—'}</span><span>Turn: {job.turn_id ?? '—'}</span></article>
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
  const retryable = !options?.method || options.method === 'GET'
  const slowRead = retryable && ['/api/v1/setup', '/api/v1/models', '/api/v1/capabilities', '/api/v1/settings'].includes(path)
  const attempts = retryable ? (slowRead ? 1 : 5) : 1
  const timeoutMs = slowRead ? 30_000 : 5_000
  let lastError: unknown
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const controller = retryable && !options?.signal ? new AbortController() : null
    const timeout = controller ? window.setTimeout(() => controller.abort(), timeoutMs) : undefined
    try {
      const response = await fetch(path, { credentials: 'same-origin', ...options, signal: options?.signal ?? controller?.signal, headers: { 'Content-Type': 'application/json', ...(options?.method && options.method !== 'GET' ? { 'X-CFR-CSRF': csrf() } : {}), ...options?.headers } })
      const payload = await response.json()
      if (response.ok) return payload
      throw Object.assign(new Error(payload.message || '控制 API 请求失败'), { status: response.status })
    } catch (error) {
      lastError = error
      const status = (error as Error & { status?: number }).status
      if (!retryable || (status !== undefined && status < 500) || attempt === attempts - 1) break
    } finally {
      if (timeout !== undefined) window.clearTimeout(timeout)
    }
    if (attempt < attempts - 1) await new Promise((resolve) => window.setTimeout(resolve, 150 * (attempt + 1)))
  }
  throw lastError instanceof Error ? lastError : new Error('控制 API 暂时不可达')
}

function pollSerially(task: () => Promise<unknown>, intervalMs: number, onError?: (error: unknown) => void) {
  let stopped = false
  let timer: number | undefined
  const run = async () => {
    try { await task() }
    catch (error) { onError?.(error) }
    finally { if (!stopped) timer = window.setTimeout(run, intervalMs) }
  }
  void run()
  return () => { stopped = true; if (timer !== undefined) window.clearTimeout(timer) }
}

function App() {
  const [theme, setTheme] = useState<'dark' | 'light'>(() => {
    try { return localStorage.getItem('cfr-control-theme') === 'dark' ? 'dark' : 'light' }
    catch { return 'light' }
  })
  const [page, setPage] = useState<Page>('runtime')
  const [setup, setSetup] = useState<SetupState | null>(null)
  const [model, setModel] = useState<Model | null>(null)
  const [sessions, setSessions] = useState<Session[]>([])
  const [bindings, setBindings] = useState<Binding[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [approvals, setApprovals] = useState<Approval[]>([])
  const [models, setModels] = useState<Catalog | null>(null)
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null)
  const [surfaces, setSurfaces] = useState<Surfaces | null>(null)
  const [storage, setStorage] = useState<Storage | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [doctor, setDoctor] = useState<unknown>({ Verdict: 'NOT_RUN' })
  const [draft, setDraft] = useState<Draft>({ model: null, reasoning_effort: null, service_tier: null })
  const [workspace, setWorkspace] = useState('')
  const [pairingWorkspace, setPairingWorkspace] = useState('')
  const [setupSurface, setSetupSurface] = useState<'code' | 'chat'>('code')
  const [chatBrowserBackend, setChatBrowserBackend] = useState<'auto' | 'embedded' | 'dedicated' | 'shared'>('auto')
  const [codexDesktopLauncher, setCodexDesktopLauncher] = useState<'auto' | 'codexhost' | 'stock'>('auto')
  const [networkMode, setNetworkMode] = useState<'auto' | 'direct' | 'proxy'>('auto')
  const [proxyUrl, setProxyUrl] = useState('')
  const [feishuAppId, setFeishuAppId] = useState('')
  const [feishuAppSecret, setFeishuAppSecret] = useState('')
  const [backendSkew, setBackendSkew] = useState(false)
  const [message, setMessage] = useState('正在加载控制状态…')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try { localStorage.setItem('cfr-control-theme', theme) } catch {}
  }, [theme])

  const updateFeishu = (value: Partial<Feishu>) => setModel((current) => current ? { ...current, feishu: normalizeFeishu(value) } : current)
  const refresh = async () => {
    const status = await api<{ current_state: Model }>('/api/v1/status')
    const rawFeishu = status.current_state.feishu
    setBackendSkew(!Array.isArray(rawFeishu.workspace_roots) || !Array.isArray(rawFeishu.activity) || !rawFeishu.workspace_policy_source)
    setModel({ ...status.current_state, feishu: normalizeFeishu(rawFeishu) })
    const [setupResult, operationalResult, approvalsResult, doctorResult] = await Promise.allSettled([
      api<{ current_state: SetupState }>('/api/v1/setup'), api<{ current_state: Operational }>('/api/v1/operational'), api<{ current_state: Approval[] }>('/api/v1/approvals'), api<{ current_state: unknown }>('/api/v1/doctor'),
    ])
    if (setupResult.status === 'fulfilled') { const value = setupResult.value.current_state; setSetup(value); setSetupSurface(value.selected_surface); setChatBrowserBackend(value.chat.preference ?? 'auto'); setCodexDesktopLauncher(value.codex.desktop_launcher_preference ?? 'auto'); setNetworkMode(value.network.mode); setProxyUrl(value.network.proxy_url ?? '') }
    if (operationalResult.status === 'fulfilled') {
      const operational = operationalResult.value.current_state
      updateFeishu({ ...operational.feishu, activity: operational.activity })
      setSessions(operational.sessions); setBindings(operational.bindings); setJobs(operational.jobs); setSurfaces(operational.surfaces); setStorage(operational.storage)
    }
    if (approvalsResult.status === 'fulfilled') setApprovals(approvalsResult.value.current_state)
    if (doctorResult.status === 'fulfilled') setDoctor(doctorResult.value.current_state)
    if (page === 'settings' || page === 'details') {
      void Promise.all([
        api<{ current_state: Catalog }>('/api/v1/models').catch(() => null),
        api<{ current_state: Capabilities }>('/api/v1/capabilities').catch(() => null),
        api<{ current_state: Settings }>('/api/v1/settings').catch(() => null),
      ]).then(([modelsResult, capabilitiesResult, settingsResult]) => {
        if (modelsResult) setModels(modelsResult.current_state)
        if (capabilitiesResult) setCapabilities(capabilitiesResult.current_state)
        if (settingsResult) {
          setSettings(settingsResult.current_state)
          const defaults = settingsResult.current_state.codex_model_defaults
          if (defaults) setDraft({ model: defaults.model.source === 'runtimeDefault' ? null : defaults.model.effective_value, reasoning_effort: defaults.reasoning_effort.source === 'modelDefault' ? null : defaults.reasoning_effort.effective_value, service_tier: defaults.service_tier.source === 'modelDefault' ? null : defaults.service_tier.effective_value })
        }
      })
    }
  }

  useEffect(() => { refresh().then(() => setMessage('控制 API 已连接。')).catch((error: Error) => setMessage(error.message)) }, [])
  useEffect(() => { if (setup && !setup.ready) setPage('setup') }, [setup?.ready])
  useEffect(() => {
    if ((page === 'settings' || page === 'details') && (!models || !settings || !capabilities)) {
      void refresh().catch((error: Error) => setMessage(error.message))
    }
  }, [page])
  useEffect(() => {
    if (page !== 'settings' && page !== 'details') return
    return pollSerially(async () => {
      const result = await api<{ current_state: Catalog }>('/api/v1/models')
      setModels(result.current_state)
    }, 60_000)
  }, [page])
  useEffect(() => {
    if (!model) return
    let failures = 0
    const refreshOperationalState = async () => {
      const result = await api<{ current_state: Operational }>('/api/v1/operational')
      const operational = result.current_state
      updateFeishu({ ...operational.feishu, activity: operational.activity })
      setSessions(operational.sessions); setBindings(operational.bindings); setJobs(operational.jobs); setSurfaces(operational.surfaces); setStorage(operational.storage)
      if (failures) setMessage('控制 API 已恢复连接。')
      failures = 0
    }
    return pollSerially(refreshOperationalState, model?.feishu.running ? 1200 : 3000, () => {
      failures += 1
      if (failures >= 3) setMessage('控制 API 连接中断；控制中心正在自动重试。')
    })
  }, [model?.feishu.running])

  const command = async (path: string, body?: object, success?: string) => {
    setBusy(true)
    try { const result = await api<Command>(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined }); if (result.status !== 'ok') throw new Error(`${result.error_code}: ${result.message}`); setMessage(success ?? result.message); await refresh() }
    catch (error) { setMessage(error instanceof Error ? error.message : '控制 API 请求失败') }
    finally { setBusy(false) }
  }
  const openDesktop = async (threadId: string, restart = false) => {
    setBusy(true)
    try { const result = await api<Command>(`/api/v1/threads/${encodeURIComponent(threadId)}/open-desktop`, { method: 'POST', body: restart ? JSON.stringify({ restart: true }) : undefined }); if (result.status !== 'ok') throw new Error(`${result.error_code}: ${result.message}`); setMessage(restart ? `已按 ${setup?.codex.desktop_launcher_preference ?? 'auto'} 方式重启 Codex Desktop，并请求打开该线程。` : '已请求在当前 Codex Desktop 中打开；当前 CodexHost/原生启动来源不会改变。') }
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
  const saveSetupPreferences = async () => command('/api/v1/setup/preferences', { default_surface: setupSurface, network_mode: networkMode, proxy_url: networkMode === 'proxy' ? proxyUrl : null, chat_browser_backend: chatBrowserBackend, codex_desktop_launcher: codexDesktopLauncher })
  const saveFeishuCredentials = async () => {
    if (!feishuAppId.trim() || !feishuAppSecret.trim()) { setMessage('请输入飞书 App ID 和 App Secret。'); return }
    await command('/api/v1/setup/feishu/credentials', { app_id: feishuAppId.trim(), app_secret: feishuAppSecret }, '飞书 App 凭据已保存到本机安全存储。')
    setFeishuAppSecret('')
  }
  const retryInitialLoad = async () => {
    setBusy(true)
    setMessage('正在重新连接控制 API…')
    try { await refresh(); setMessage('控制 API 已连接。') }
    catch (error) { setMessage(error instanceof Error ? error.message : '控制 API 暂时不可达') }
    finally { setBusy(false) }
  }
  if (!model) return <main className="loading"><p>{message}</p><button onClick={retryInitialLoad} disabled={busy}>重试连接</button></main>

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
  const desktopRestartBlocked = jobs.some((job) => job.active) || bindings.some((binding) => binding.active_turn_id != null || binding.writer_state !== 'idle')

  const pairingPanel = <section className="panel"><h2>飞书账号 / 当前绑定状态</h2><p>状态：{text(pairing.state)}</p>
    {feishu.credentials_configured && !feishu.operator_authorized && !pairingActive && <button onClick={() => command('/api/v1/feishu/pairing/start')} disabled={busy || feishu.state !== 'stopped'}>绑定我的飞书账号</button>}
    {pairing.state === 'waiting' && pairing.code && <><p>请向 CFR 飞书机器人私聊发送：绑定 {pairing.code}</p><button onClick={() => copyPairingMessage(pairing.code!)} disabled={busy}>复制配对消息</button><button onClick={() => command('/api/v1/feishu/pairing/cancel')} disabled={busy}>取消配对</button></>}
    {pairing.state === 'pending_confirmation' && <><p>检测到候选账号 {pairing.candidate}，请确认本地工作区。</p><label>允许的本地工作区<input value={pairingWorkspace || pairing.suggested_workspace_root} onChange={(event) => setPairingWorkspace(event.target.value)} disabled={busy} /></label><button onClick={() => setPairingWorkspace(pairing.suggested_workspace_root)} disabled={busy}>使用当前 CFR 目录</button><button onClick={() => command('/api/v1/feishu/pairing/confirm', { workspace_root: pairingWorkspace || pairing.suggested_workspace_root })} disabled={busy}>确认绑定</button><button onClick={() => command('/api/v1/feishu/pairing/cancel')} disabled={busy}>取消</button></>}
    {pairing.state === 'expired' && <button onClick={() => command('/api/v1/feishu/pairing/start')} disabled={busy || feishu.state !== 'stopped'}>重新开始绑定</button>}
  </section>

  return <main>
    <header><div><p className="eyebrow">CFR 本地控制平面</p><h1>控制中心</h1></div><div className="header-actions"><button onClick={() => setTheme((value) => value === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? '浅色模式' : '深色模式'}</button><button onClick={() => refresh()} disabled={busy}>刷新</button></div></header>
    <nav aria-label="控制中心页面"><button className={page === 'setup' ? 'selected' : ''} onClick={() => setPage('setup')}>启动配置</button><button className={page === 'runtime' ? 'selected' : ''} onClick={() => setPage('runtime')} disabled={!setup?.ready}>运行</button><button className={page === 'settings' ? 'selected' : ''} onClick={() => setPage('settings')}>配置</button><button className={page === 'details' ? 'selected' : ''} onClick={() => setPage('details')}>详情</button></nav>
    <p className="message" role="status">{message}</p>
    {backendSkew && <section className="error"><strong>控制中心与后端版本不兼容</strong><span>检测到正在运行的 CFR 后端版本较旧，与当前控制中心不兼容。请关闭旧 CFR 后重新启动 START_CFR.cmd。</span></section>}

    {page === 'setup' && setup && <>
      <section className={`setup-hero ${setup.ready ? 'ready' : ''}`}><div><h2>{setup.ready ? '启动配置已就绪' : '首次启动配置'}</h2><strong>{setup.ready ? 'CFR 可以启动运行时' : `还有 ${setup.blocking.length} 个阻塞项`}</strong><p>配置保存在本机。飞书 App Secret 只进入操作系统安全存储；Git 仓库不保存登录态或 Secret。</p></div>{setup.ready && <button onClick={() => setPage('runtime')}>进入运行页面</button>}</section>
      <section className="setup-grid">{setup.checks.map((item) => <article className={item.ready ? 'setup-check ready' : item.required ? 'setup-check blocked' : 'setup-check'} key={item.key}><strong>{item.ready ? '✓' : item.required ? '!' : '·'} {item.label}</strong><span>{item.detail}</span>{item.action === 'codex_login' && <button onClick={() => command('/api/v1/setup/codex/login')} disabled={busy}>打开 Codex 登录</button>}{item.action === 'validate_codex' && <button onClick={() => command('/api/v1/setup/codex/validate')} disabled={busy}>运行 Codex 校验</button>}{item.action === 'chat_login' && setup.chat.effective_backend === 'embedded' && <button onClick={() => command('/api/v1/setup/chat/start')} disabled={busy}>打开 ChatGPT 登录</button>}{item.action === 'chat_login' && setup.chat.effective_backend === 'dedicated' && <><button onClick={() => command('/api/v1/setup/chat/manual-login/start')} disabled={busy}>人工登录 ChatGPT（无自动化）</button>{setup.chat.dedicated.login_pending && <button onClick={() => command('/api/v1/setup/chat/manual-login/verify')} disabled={busy}>我已登录并关闭窗口，检测状态</button>}</>}{item.action === 'chat_login' && setup.chat.effective_backend === 'shared' && <><button onClick={() => command('/api/v1/setup/chat/manual-login/start')} disabled={busy}>在当前 Chrome 打开 ChatGPT</button><button onClick={() => command('/api/v1/setup/chat/manual-login/verify')} disabled={busy}>检测共享 Chrome</button></>}{item.action === 'pair_feishu' && feishu.credentials_configured && <>
        {!pairingActive && <button onClick={() => command('/api/v1/feishu/pairing/start')} disabled={busy || feishu.state !== 'stopped'}>{pairing.state === 'expired' ? '重新开始飞书账号配对' : '开始飞书账号配对'}</button>}
        {pairing.state === 'starting' && <span>正在建立飞书配对连接…</span>}
        {pairing.state === 'waiting' && pairing.code && <><span>配对连接已就绪。请在飞书中私聊 CFR 机器人发送：</span><strong>绑定 {pairing.code}</strong><button onClick={() => copyPairingMessage(pairing.code!)} disabled={busy}>复制配对消息</button><button onClick={() => command('/api/v1/feishu/pairing/cancel')} disabled={busy}>取消配对</button></>}
        {pairing.state === 'pending_confirmation' && <><span>已检测到候选账号 {pairing.candidate}，请确认允许的本地工作区。</span><label>允许的本地工作区<input value={pairingWorkspace || pairing.suggested_workspace_root} onChange={(event) => setPairingWorkspace(event.target.value)} disabled={busy} /></label><button onClick={() => setPairingWorkspace(pairing.suggested_workspace_root)} disabled={busy}>使用当前 CFR 目录</button><button onClick={() => command('/api/v1/feishu/pairing/confirm', { workspace_root: pairingWorkspace || pairing.suggested_workspace_root })} disabled={busy}>确认绑定</button><button onClick={() => command('/api/v1/feishu/pairing/cancel')} disabled={busy}>取消</button></>}
      </>}</article>)}</section>
      <section className="controls"><div><h2>默认执行模式</h2><p>Code 与 Chat 的执行状态独立保存。切换浏览器后端不会删除另一套登录目录。</p><label>默认 Surface<select value={setupSurface} onChange={(event) => setSetupSurface(event.target.value as 'code' | 'chat')} disabled={busy}><option value="code">Code</option><option value="chat">Chat</option></select></label><button onClick={saveSetupPreferences} disabled={busy}>保存模式</button></div><div><h2>网络</h2><p>Auto 优先环境变量，其次系统代理；Direct 强制直连；Proxy 使用下面保存的固定 HTTP(S) 代理。</p><label>网络模式<select value={networkMode} onChange={(event) => setNetworkMode(event.target.value as 'auto' | 'direct' | 'proxy')} disabled={busy}><option value="auto">Auto</option><option value="direct">Direct</option><option value="proxy">Proxy</option></select></label>{networkMode === 'proxy' && <label>代理地址<input value={proxyUrl} onChange={(event) => setProxyUrl(event.target.value)} placeholder="http://127.0.0.1:7890" disabled={busy} /></label>}<button onClick={saveSetupPreferences} disabled={busy || (networkMode === 'proxy' && !proxyUrl.trim())}>保存网络设置</button><span>当前有效：{setup.network.effective_mode} / {setup.network.effective_source}{setup.network.effective_proxy ? ` / ${setup.network.effective_proxy}` : ''}</span></div></section>
      <section className="controls"><div><h2>Chat 浏览器</h2><p>Linux 固定使用 chrome-use 扩展 + Native Messaging。CFR 只操作 session <code>cfr-chat</code> 自己创建的后台 tab group，不使用 Remote Debugging，也不会把你的当前标签页作为操作目标。</p><span>当前运行：{setup.chat.runtime_backend ?? '尚未启动'}</span><span>Chrome 登录：{setup.chat.shared.authenticated ? '已检测到 ChatGPT 登录' : '尚未检测到 ChatGPT 登录'} · {setup.chat.shared.profile_dir ?? '—'}</span><span>chrome-use：{setup.chat.chrome_use?.installed ? `CLI ${setup.chat.chrome_use.version ?? '版本未知'}` : '未安装'} · Host {setup.chat.chrome_use?.host_healthy ? '正常' : '未就绪'} · 扩展 {setup.chat.chrome_use?.relay_up ? `已连接 ${setup.chat.chrome_use.extension_version ?? ''}` : '未连接'}</span>{setup.chat.chrome_use?.silent_window && <span>静默窗口：{setup.chat.chrome_use.silent_window.isolated ? '已隔离' : '未隔离'} · {setup.chat.chrome_use.silent_window.minimized ? '已最小化' : '未最小化'} · {setup.chat.chrome_use.silent_window.tab_count} 个 CFR tab</span>}<button onClick={() => command('/api/v1/setup/chat/manual-login/start')} disabled={busy}>在普通 Chrome 打开 ChatGPT</button><button onClick={() => command('/api/v1/setup/chat/start')} disabled={busy}>连接 / 检查后台 Chat</button></div><div><h2>Codex Desktop 启动来源</h2><p>Auto 会保留当前启动来源：检测到 CodexHost 就继续由 CodexHost 重启；检测到原生 Codex 就保持原生。不会再无差别切成 stock。</p><label>启动方式<select value={codexDesktopLauncher} onChange={(event) => setCodexDesktopLauncher(event.target.value as 'auto' | 'codexhost' | 'stock')} disabled={busy}><option value="auto">Auto（保留当前方式）</option><option value="codexhost" disabled={!setup.codex.codexhost_available}>CodexHost</option><option value="stock">原生 Codex</option></select></label><span>当前运行：{setup.codex.desktop_running ? setup.codex.desktop_runtime_mode : '未运行'}</span><span>CodexHost：{setup.codex.codexhost_available ? `${setup.codex.codexhost_version ?? '版本未知'} · ${setup.codex.codexhost_command}` : '未找到可执行 launcher'}</span>{codexDesktopLauncher === 'stock' && setup.codex.desktop_runtime_mode === 'codexhost' && <span className="warning-text">警告：下一次“重启并打开”会显式退出 CodexHost 管理态；Harness 会话需重新通过 CodexHost 启动后显示。</span>}<button onClick={saveSetupPreferences} disabled={busy}>保存启动方式</button></div></section>
      <section className="controls"><div><h2>飞书 App</h2><p>使用你自己的飞书自建应用。App Secret 不写入 config.json。飞书运行中替换凭据时，保存后会自动重新连接。</p><label>App ID<input value={feishuAppId} onChange={(event) => setFeishuAppId(event.target.value)} placeholder={setup.feishu.app_id_configured ? '已配置；输入新值可替换' : 'cli_xxx'} disabled={busy} /></label><label>App Secret<input type="password" value={feishuAppSecret} onChange={(event) => setFeishuAppSecret(event.target.value)} placeholder={setup.feishu.app_secret_configured ? '已配置；输入新值可替换' : '输入 App Secret'} disabled={busy} /></label><button onClick={saveFeishuCredentials} disabled={busy || !feishuAppId.trim() || !feishuAppSecret.trim()}>保存飞书凭据</button></div><div><h2>后续配置</h2><p>飞书账号配对和工作区管理统一放在“配置”页面；启动配置不再保留第二套工作区入口。</p><button onClick={() => setPage('settings')} disabled={busy}>前往配置</button></div></section>
    </>}

    {page === 'runtime' && <>
      <section className="grid"><article><h2>CFR 总体状态</h2><strong>{text(model.overall_status)}</strong></article><article><h2>Codex</h2><strong>{text(model.codex.availability)}</strong><span>诊断：{text(model.codex.doctor_status)}</span></article><article><h2>飞书</h2><strong>{text(feishu.state)}</strong><span>Transport：{text(feishu.transport_state)}</span><span>账号：{feishu.operator_authorized ? '已绑定' : '未绑定'}</span>{feishu.transport_last_event_error && <span>最近请求级错误：{feishu.transport_last_event_error.type} · {feishu.transport_last_event_error.message}</span>}</article><article><h2>任务接收</h2><strong>{model.runtime.accept_new_tasks ? '开启' : '排空'}</strong><span>{model.runtime.remote_execution_enabled ? '远程执行已启用' : '远程执行已禁用'}</span></article></section>
      <section className="panel"><h2>Execution Surface</h2><p>这是最近活跃飞书会话的顶层执行 authority；与 Code 内部的 Codex /mode 分离。{surfaces?.chat_id ? ` 当前目标 Chat：${surfaces.chat_id}` : ''}</p><div className="surface-grid">{surfaces?.data.map((surface) => <article className={`surface-card ${surface.id === surfaces.selected ? 'selected' : ''}`} key={surface.id}><strong>{surface.name}</strong><span>{surface.id === surfaces.selected ? '当前 Surface' : surface.available ? '可用' : '尚未接入'}</span><span>{surface.description}</span><button onClick={() => command('/api/v1/surfaces/select', { surface: surface.id, chat_id: surfaces.chat_id }, `已切换到 ${surface.name} Surface。`)} disabled={busy || !surfaces.chat_id || surface.id === surfaces.selected || !surface.available}>{surface.id === surfaces.selected ? '当前' : surface.available ? '选择' : '未接入'}</button></article>)}</div><p>Chat / Work 只有在真实 ChatGPT Chat / Work authority 接入后才会开放，不会由 Codex 模拟。</p></section>
      {!workspaceRootsValid && <section className="error"><strong>工作区配置无效</strong>{invalidRoots.map((root) => <span key={root}>{root}：路径不存在。</span>)}<button onClick={() => setPage('settings')}>前往配置</button></section>}
      <section className="controls"><div><h2>运行控制</h2><button onClick={() => command('/api/v1/feishu/start')} disabled={busy || lifecycleTransitioning || feishu.running || feishu.state === 'degraded' || startBlocked}>启动飞书</button><button onClick={() => command('/api/v1/feishu/stop')} disabled={busy || lifecycleTransitioning || (!feishu.running && feishu.state !== 'degraded' && !pairingActive)}>停止飞书</button><button onClick={() => command('/api/v1/feishu/reconnect')} disabled={busy || lifecycleTransitioning || pairingActive || !workspaceRootsValid || backendSkew}>重新连接</button>{startBlocked && !pairingActive && <p>{!workspaceRootsValid ? '请先修复无效工作区。' : '请先完成飞书账号和工作区配置。'}</p>}</div><div><h2>任务控制</h2><button onClick={() => command('/api/v1/runtime/remote-execution', { enabled: !model.runtime.remote_execution_enabled })} disabled={busy}>切换远程执行</button><button onClick={() => command('/api/v1/runtime/accept-new-work', { enabled: !model.runtime.accept_new_tasks })} disabled={busy}>切换新任务接收</button><button onClick={() => command('/api/v1/runtime/drain', undefined, '已暂停接收新任务。')} disabled={busy}>排空</button><button onClick={() => command('/api/v1/doctor')} disabled={busy}>运行诊断 / 健康检查</button></div></section>
      <section className="panel"><h2>运行日志 / Runtime Console</h2><div className="console" aria-label="运行日志">{feishu.activity.length === 0 ? <span>暂无安全运行事件。</span> : feishu.activity.map((item, index) => <div className={`console-row ${item.level ?? 'info'}`} key={`${item.timestamp ?? item.at}-${index}`}><time>{rowTime(item)}</time><b>{(item.level ?? 'info').toUpperCase()}</b><span>{item.component ?? 'control'}{item.stage ? `.${item.stage}` : ''}</span><code>{item.code ?? item.event}</code><em>{item.message}</em></div>)}</div></section>
      <section className="summary"><span>当前 Session：{sessions.length}</span><span>当前 Job：{jobs.filter((job) => job.active).length || 'idle'}</span><span>队列：{feishu.runtime?.queue_depth ?? 0} · 活跃聊天：{feishu.runtime?.active_chats ?? 0}</span><span>Worker：{feishu.runtime ? `${feishu.runtime.workers_alive}/${feishu.runtime.workers_total}` : '—'} · Heartbeat：{feishu.runtime?.heartbeat_alive ? '正常' : '—'}</span><span>Transport：{text(feishu.transport_state)}</span><span>CFR 数据：{bytes(storage?.cfr_storage_bytes)} · Codex 历史：{bytes(storage?.codex_rollout_bytes)}</span><span>待处理 Approval：{pendingApprovals}</span><button onClick={() => setPage('details')}>查看详情</button></section>
    </>}

    {page === 'settings' && <>
      {pairingPanel}
      <section className="panel"><h2>允许的工作区</h2><p>策略来源：{feishu.workspace_policy_source || '未知'}。运行中可以新增工作区并立即生效；移除工作区仍要求先停止飞书，避免已绑定会话绕过撤销后的安全边界。</p>{feishu.workspace_roots.length === 0 ? <p>尚未配置工作区。</p> : feishu.workspace_roots.map((root) => <article key={root}><span>{root}{invalidRoots.includes(root) && ' — 路径不存在'}</span><button onClick={() => command('/api/v1/feishu/workspaces/remove', { workspace_root: root })} disabled={busy || backendSkew || feishu.running || pairingActive || feishu.workspace_policy_source === 'environment'}>移除</button></article>)}<label>新增绝对工作区<input value={workspace} onChange={(event) => setWorkspace(event.target.value)} disabled={busy || backendSkew || pairingActive || feishu.workspace_policy_source === 'environment'} /></label><button onClick={() => command('/api/v1/feishu/workspaces/add', { workspace_root: workspace })} disabled={busy || !workspace || backendSkew || pairingActive || feishu.workspace_policy_source === 'environment'}>添加工作区</button><button onClick={() => setWorkspace(pairing.suggested_workspace_root)} disabled={busy || backendSkew || feishu.workspace_policy_source === 'environment'}>使用当前 CFR 目录</button>{feishu.workspace_policy_source === 'environment' && <p>工作区由环境变量管理，当前为只读。</p>}</section>
      <section className="panel"><h2>Codex 默认设置</h2><p>仅用于新建 Codex 线程；当前线程不会改变。这里的目录由本机 Codex runtime 动态提供，不与 ChatGPT 网页模型混用；Chat Surface 的 /models 会直接读取当前 ChatGPT 网页实际可选项。</p>{!settings?.available || !settings.codex_model_defaults ? <p>{settings?.message ?? 'Codex 设置不可用。'}</p> : <><span>有效模型：{settings.codex_model_defaults.model.effective_value ?? 'Codex 默认'}（{text(settings.codex_model_defaults.model.source)}）</span><span>有效推理强度：{settings.codex_model_defaults.reasoning_effort.effective_value ?? '模型默认'}（{text(settings.codex_model_defaults.reasoning_effort.source)}）</span><span>有效服务层级：{settings.codex_model_defaults.service_tier.effective_value ?? '模型默认'}（{text(settings.codex_model_defaults.service_tier.source)}）</span><span>受管理的新线程默认值：{settings.codex_model_defaults.managed_new_thread_defaults.model ?? '无'} / {settings.codex_model_defaults.managed_new_thread_defaults.reasoning_effort ?? '无'} / {settings.codex_model_defaults.managed_new_thread_defaults.service_tier ?? '无'}</span><label>模型<select value={draft.model ?? ''} onChange={(event) => changeModel(event.target.value)} disabled={busy || !models?.available}><option value="">使用 Codex 默认值</option>{models?.data.map((item) => item.model && <option key={item.id ?? item.model} value={item.model}>{item.display_name ?? item.model}</option>)}</select></label>{modelUnavailable && <p>当前配置的模型不在已安装运行时的模型目录中。</p>}<label>推理强度<select value={draft.reasoning_effort ?? ''} onChange={(event) => setDraft((value) => ({ ...value, reasoning_effort: event.target.value || null }))} disabled={busy || !selectedModel}><option value="">使用模型默认值</option>{selectedModel?.supported_reasoning_efforts.map((item) => item.reasoning_effort && <option key={item.reasoning_effort} value={item.reasoning_effort}>{item.reasoning_effort}</option>)}</select></label><label>服务层级<select value={draft.service_tier ?? ''} onChange={(event) => setDraft((value) => ({ ...value, service_tier: event.target.value || null }))} disabled={busy || !selectedModel}><option value="">使用模型默认值</option>{selectedModel?.service_tiers.map((item) => item.id && <option key={item.id} value={item.id}>{item.name ?? item.id}</option>)}</select></label><button onClick={saveDefaults} disabled={busy || !settings.available || !models?.available || !selectedModel || modelUnavailable}>保存默认设置</button></>}</section>
    </>}

    {page === 'details' && <>
      <section className="panel"><h2>CFR Runtime</h2><p>Code 与 Chat 的有界、进程内运行观测。缺失值显示为 —。</p>{jobs.length === 0 ? <p>当前没有可观测的 CFR 任务。</p> : jobs.map((job) => <RuntimeJob key={job.id} job={job} />)}</section>
      <section className="controls"><div><h2>当前飞书绑定 / 双 Surface 状态</h2>{sessions.length === 0 ? <p>没有持久化的飞书会话状态。</p> : sessions.map((session) => <article key={session.chat_id}><strong>当前 Surface：{session.selected_surface === 'chat' ? 'Chat' : 'Code'}</strong><span>聊天类型：{chatType(session.chat_type)}</span><span>聊天 ID：{session.chat_id}</span><span>Code：{text(session.state)} · 线程 {session.thread_id ?? '尚未创建'}</span>{session.state !== 'unbound' && <label>Code 审批模式<select value={session.approval_mode} onChange={(event) => command(`/api/v1/sessions/${encodeURIComponent(session.chat_id)}/approval-mode`, { mode: event.target.value }, 'Code 审批模式已保存；从下一次 Code 执行开始生效。')} disabled={busy}><option value="ask">全部请求</option><option value="auto">替我审批</option><option value="full">全部开放权限</option></select></label>}{session.pending_cwd && <span>Code 新线程工作区：{session.pending_cwd}</span>}{session.pending_settings && <span>Code 新线程预设：{session.pending_settings.model ?? '默认模型'} / {session.pending_settings.reasoning_effort ?? '默认推理'} / {session.pending_settings.service_tier ?? '默认层级'}</span>}<span>Chat：{text(session.chat_state)} · Conversation {session.chat_conversation_id ?? '尚未确认'}</span>{session.chat_project_id && <span>Chat Project：{session.chat_project_id}</span>}{session.chat_url && <span>Chat URL：{session.chat_url}</span>}<span>更新时间：{new Date(session.updated_at * 1000).toLocaleString('zh-CN', { hour12: false })}</span>{session.thread_id && <button onClick={() => { if (window.confirm('解除这个飞书会话的 Code 绑定？不会删除 Codex 线程，也不会删除 Chat 绑定。')) command(`/api/v1/sessions/${encodeURIComponent(session.chat_id)}/unbind`, undefined, 'Code 会话已解除绑定。') }} disabled={busy}>解除 Code 绑定</button>}</article>)}</div><div className="catalog-panel"><h2>可用 CFR 会话 / 持久化 Codex 线程（最多 200 条）</h2><p>控制中心采用有界投影，不会随着历史线程无限增长。普通打开不重启 Desktop；重启是全局 Desktop 操作，只有所有 CFR 线程和 Writer 都空闲时才允许。</p>{bindings.length === 0 ? <p>没有持久化的 CFR/Codex 线程。</p> : bindings.map((binding) => { const desktopSafe = !jobs.some((job) => job.active && job.thread_id === binding.thread_id) && binding.writer_state === 'idle'; return <article key={binding.thread_id}><strong>{binding.thread_name ?? binding.thread_id}</strong><span>{binding.bound_chat_id ? '当前已绑定到飞书会话' : '当前未绑定，可用 /session 选择'}</span><span>线程：{binding.thread_id}</span><span>工作区：{binding.cwd}</span><span>最近 Turn：{binding.last_seen_turn_id ?? '无'}</span><span>Writer：{text(binding.writer_state)}</span>{binding.active_turn_id && <span>活动 Turn：{binding.active_turn_id}</span>}<span>桌面同步：{text(binding.desktop_sync_state)}</span><button onClick={() => openDesktop(binding.thread_id)} disabled={busy || !desktopSafe}>在 Codex Desktop 中打开</button><button onClick={() => { if (window.confirm(`将按“${setup?.codex.desktop_launcher_preference ?? 'auto'}”启动方式重启 Codex Desktop 并打开线程。Auto 会保留 CodexHost 管理态。未保存的桌面 UI 状态可能丢失，是否继续？`)) void openDesktop(binding.thread_id, true) }} disabled={busy || !desktopSafe || desktopRestartBlocked}>按当前方式重启并打开</button><button onClick={() => copyThreadId(binding.thread_id)} disabled={busy}>复制完整 Thread ID</button>{!desktopSafe && <span>仅可打开无活动 Turn 且 Writer 空闲的会话。</span>}{desktopRestartBlocked && desktopSafe && <span>当前存在其他活动 CFR 线程或 Writer，已阻止全局 Desktop 重启。</span>}</article> })}</div></section>
      <section className="controls"><div><h2>Approvals</h2><p>审批决定仍在飞书中处理。</p>{approvals.length === 0 ? <p>没有持久化的 CFR 审批记录。</p> : approvals.map((item) => <article key={item.approval_id}><strong>{text(item.feedback_state ?? item.state)}</strong><span>决定：{text(item.decision ?? 'pending')}</span><span>线程：{item.thread_id}</span><span>轮次：{item.turn_id ?? '无'}</span>{item.request_kind && <span>类型：{item.request_kind}</span>}<span>创建时间：{item.created_at ?? '未知'}</span><span>更新时间：{item.updated_at ?? '未知'}</span></article>)}</div><div><h2>Doctor / Health</h2><pre className="doctor">{JSON.stringify(doctor, null, 2)}</pre></div></section>
      <section className="controls"><div className="catalog-panel"><h2>Models</h2>{models?.available && <p>来源：{models.source ?? 'Codex runtime'} · schema v{models.catalog_schema_version ?? 1}。新模型由运行时发现，不在 CFR 中硬编码。</p>}{!models?.available ? <p>{models?.message ?? '模型目录不可用。'}</p> : models.data.map((item) => <article key={item.id ?? item.model ?? 'model'}><strong>{item.display_name ?? item.model ?? item.id}</strong>{item.is_default && <span>默认</span>}<span>模型：{item.model ?? '不可用'}</span>{item.description && <span>{item.description}</span>}{item.supported_reasoning_efforts.length > 0 && <span>推理强度：{item.supported_reasoning_efforts.map((value) => value.reasoning_effort ?? '不可用').join(', ')}</span>}{item.service_tiers.length > 0 && <span>服务层级：{item.service_tiers.map((value) => value.name ?? value.id ?? '不可用').join(', ')}</span>}{item.input_modalities.length > 0 && <span>输入模态：{item.input_modalities.join(', ')}</span>}{item.multi_agent_version && <span>Multi-agent runtime：{item.multi_agent_version}</span>}{item.supports_personality && <span>支持人格设定</span>}{item.availability_message && <span>运行时提示：{item.availability_message}</span>}{item.upgrade_model && <span>运行时建议升级到：{item.upgrade_model}</span>}</article>)}</div><div className="catalog-panel"><h2>Capabilities</h2><p>上下文：{capabilities?.context ?? '未知'}</p><h3>权限配置</h3>{!capabilities?.permission_profiles.available ? <p>{capabilities?.permission_profiles.message ?? '能力目录不可用。'}</p> : capabilities.permission_profiles.data.map((item) => <article key={item.id ?? 'profile'}><strong>{item.id ?? '不可用'}</strong><span>允许：{item.allowed ? '是' : '否'}</span>{item.description && <span>{item.description}</span>}</article>)}<h3>实验性功能</h3>{!capabilities?.experimental_features.available ? <p>{capabilities?.experimental_features.message ?? '能力目录不可用。'}</p> : capabilities.experimental_features.data.map((item) => <article key={item.name ?? 'feature'}><strong>{item.display_name ?? item.name ?? '不可用'}</strong><span>阶段：{item.stage ?? '不可用'}</span><span>{item.enabled ? '已启用' : '已禁用'} / 默认 {item.default_enabled ? '启用' : '禁用'}</span>{item.description && <span>{item.description}</span>}</article>)}</div></section>
    </>}
    {feishu.last_error_code && page !== 'runtime' && <section className="error"><strong>{feishu.last_error_code}</strong><span>{feishu.last_error_message}</span></section>}
  </main>
}

createRoot(document.getElementById('root')!).render(<App />)
