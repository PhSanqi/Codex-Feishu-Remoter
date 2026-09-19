# CFR 全系统代码、性能与健壮性审计

审计基线日期：2026-09-03

## 1. 目标与验收边界

本轮目标不是把所有文件重构一遍，而是在不破坏 Code / Chat / Feishu / Codex Desktop 共存能力的前提下，完成以下闭环：

1. 建立唯一的 Current Truth：运行进程、飞书连接、Surface、Code thread、Chat Conversation、活动任务与持久化记录必须分层显示，不能互相冒充。
2. 缩短 CFR 自身可控延迟：消除重复配置读取、重复控制面请求、无变化进度卡更新、历史全量读取和长回答二次增长传输。
3. 建立背压和资源上限：队列、附件、运行投影、去重状态、文件扫描、历史读取和 UI 展示必须有明确边界。
4. 提高故障隔离：可选遥测不得阻塞最终交付；局部 API、浏览器、数据库或 SDK 故障不能把整个控制平面拖死。
5. 保持安全边界：工作区 allowlist、线程 writer lease、飞书账号授权、Secret 存储、路径校验和本地 API 鉴权不得为了性能而放松。
6. 形成可重复证据：单元测试、静态故障扫描、性能预算、前端构建、EXE 冷启动、单实例、退出清理全部通过后才收口。

不在本轮自动执行的操作：真实飞书消息、真实 ChatGPT/Codex 外部任务、自动删除用户历史数据、Git commit/push。它们分别需要外部副作用授权、数据保留决策或正式提交授权。

## 2. 系统依赖图

```text
CFR.exe / START_CFR
  └─ Desktop shell (pywebview / WebView2)
      └─ LocalControlServer (127.0.0.1 + bootstrap cookie + CSRF)
          ├─ ControlReadModel ── SQLite / config / bounded runtime telemetry
          └─ CfrSupervisor
              ├─ Feishu transport (lark-channel WebSocket + outbound API)
              ├─ FeishuGateway (解析、授权、持久化、去重、入队)
              └─ FeishuDaemon (公平调度、命令、Surface 路由、生命周期)
                  ├─ CodeSurfaceRuntime
                  │   └─ CodexAdapter
                  │       ├─ runtime lease / writer ownership
                  │       ├─ Codex app-server
                  │       └─ native rollout / approvals / artifact delivery
                  └─ ChromeChatAdapter
                      └─ Chrome DevTools MCP
                          └─ CFR 专用 Chrome Profile / ChatGPT 网页
```

SQLite 是 durable queue、session、binding、approval、reply idempotency 和 lease 的 Current Truth。内存容器只承担调度、短期遥测和缓存，不能覆盖数据库事实。

## 3. 模块审计地图

### 3.1 入口与平台层

- `src/cfr/desktop.py`：EXE 单实例、日志、Control API、WebView、后台自动启动和关闭顺序。
- `src/cfr/platform.py`：Windows 隐藏子进程、进程树终止、Codex Desktop handoff。
- `src/cfr/config.py`：本机配置原子写入、默认 Surface、网络和机器状态。
- `src/cfr/network.py`：Auto / Direct / Proxy 解析、子进程环境和凭据脱敏。
- `src/cfr/cli/main.py`：命令行入口与人工诊断路径。

### 3.2 控制平面

- `src/cfr/control/api.py`：loopback-only HTTP、bootstrap session、CSRF、静态 UI 和结构化错误。
- `src/cfr/control/supervisor.py`：Feishu/Chat/配对/诊断的唯一生命周期 authority。
- `src/cfr/control/read_model.py`：面向 UI 的无副作用状态投影与短期缓存。
- `src/cfr/control/setup.py`：首次安装、Codex 登录/兼容性、Chat Profile 和飞书必需项。
- `src/cfr/control/codex_catalog.py`、`codex_settings.py`：模型、能力和新线程默认设置。
- `src/cfr/surfaces.py`：Code / Chat authority 目录，和 Codex collaboration mode 严格分离。

### 3.3 飞书接入与调度

- `src/cfr/feishu/transport.py`：长连接、媒体上传下载、消息/卡片发送和超时边界。
- `src/cfr/feishu/gateway.py`：事件解析、操作者授权、去重、持久化和 admission。
- `src/cfr/feishu/daemon.py`：按 chat 公平串行、跨 chat 并发、命令和 Surface 路由。
- `src/cfr/feishu/store.py`：飞书 durable state 和 reply/approval 幂等性。
- `src/cfr/feishu/replies.py`：最终文本、图片、文件和卡片交付。
- `src/cfr/feishu/progress.py`：可选进度遥测，不拥有最终交付优先级。
- `src/cfr/feishu/security.py`：工作区/文件边界、外部附件名净化。
- `src/cfr/feishu/config.py`、`credentials.py`：非敏感配置与 OS keyring。
- `src/cfr/feishu/doctor.py`：飞书、Codex、数据库和 lease 的组合诊断。

### 3.4 Code Surface / Codex

- `src/cfr/feishu/code_runtime.py`：附件准备、Turn 执行、Artifact 收集、最终交付和清理。
- `src/cfr/codex/binding.py`：native thread 绑定和每 Turn writer authority。
- `src/cfr/codex/app_server.py`：JSON-RPC、通知订阅、超时和 app-server 关闭。
- `src/cfr/codex/turns.py`：活动 Turn、停止、重定向和有界遥测。
- `src/cfr/codex/runtime_lease.py`：跨进程 writer fencing。
- `src/cfr/codex/rollout.py`：原生 rollout 尾读、事件去重和历史投影。
- `src/cfr/codex/approvals.py`：native approval 映射和 fail-closed 决策。
- `src/cfr/codex/threads.py`、`launcher.py`、`diagnostics.py`：线程协议、进程发现和接口诊断。

### 3.5 Chat Surface

- `src/cfr/chat.py`：CFR 专用 Chrome、MCP 协议、Project/Conversation、附件、网页可见思考、回答、图片、文件和 Scheduled。
- Chat 和 Code 共享飞书入口与最终交付，但不共享执行 authority、线程身份或历史来源。

### 3.6 核心与存储

- `src/cfr/core/models.py`：跨模块值对象和结构化错误。
- `src/cfr/core/events.py`、`projector.py`：native 事件投影。
- `src/cfr/storage/db.py`：binding、runtime lease、rollout cursor 和技术性事件去重。

## 4. 关键运行链路

### 4.1 启动

`CFR.exe → 单实例锁 → rotating log → Supervisor → LocalControlServer ready probe → WebView → 后台 setup gate → 可选 Chat bridge → Feishu`

规则：窗口和本地状态先可见，外部探测在后台；关闭信号可以取消尚未完成的自动启动；任何子进程默认无可见终端。

### 4.2 飞书入站

`Feishu SDK → Gateway 授权/去重 → SQLite inbox → chat token queue → worker → command 或 Surface runtime`

规则：SQLite 是队列 authority；一个 chat 严格串行，不同 chat 可并发；内存计数漂移必须从数据库自愈。

### 4.3 Code

`pending attachments → allowlisted workspace → runtime lease → app-server → thread start/resume → turn → progress snapshot → text/artifacts → final delivery → release writer`

规则：每 Turn 释放 native writer，保障 CFR 与 Codex Desktop 共存。进度失败不影响 Turn 和最终文件。Artifact 必须来自当前 workspace、当前 native generated root 或本轮可信临时文件。

### 4.4 Chat

`pending attachments → bound tab/URL restore → composer upload → send confirmation → bounded DOM polling → terminal full read once → image/file download → Feishu delivery`

规则：文字、图片和文件必须来自同一最新 assistant root；新 Conversation URL 是发送成功和绑定更新证据；生成中只传有界预览，终态才读取完整文本。

### 4.5 关闭

普通窗口 `X`：`cancel close → hide CFR window → tray keeps runtime alive`。

真正退出：`tray Exit → stop admission → stop/finish active work → drain workers → stop Feishu transport → release daemon lease → close Chat bridge/WebView2 → stop Control API → release desktop instance lock`。

规则：不能先拔掉 transport 再让 worker 发送最终结果；隐藏到托盘不是进程退出；自动启动线程不能在真正退出后重新启动运行时。

## 5. 本轮确认并修复的问题

| 层 | 问题机制 | 处理 |
|---|---|---|
| 控制轮询 | tracked config 仍每 5 秒读配置和 Keyring | 有文件 revision 时精确失效；TTL 只用于无可追踪 loader |
| 模型目录 | 刷新可能重复执行高成本模型/能力探测 | 成功缓存 60 秒，失败只缓存 5 秒 |
| 新模型接入 | 型号/推理档位散落硬编码会导致每次模型发布都改 CFR | `model_registry` 以 Codex `model/list` 为 authority，统一归一化 model/reasoning/tier/multi-agent/upgrade，并保留未知 extensions |
| 控制 API | WebView 取消请求产生 BrokenPipe/假故障 | 客户端断开作为正常请求终止；handler 使用 daemon threads |
| 前端轮询 | 运行中重复轮询 Feishu 与 operational | 运行中只使用组合快照，停止/配对阶段才轮询轻量 Feishu 状态 |
| 长期 Chat 状态 | sessions/surface/chat-binding 三张永久表被控制面全量扫描 | 先从三张索引各取有界候选，再按 chat 合并最近 200 个，并完整补回三类关联状态；持久化事实不删除 |
| Job 投影 | 每次轮询扫描全部历史 binding | 只读取当前遥测涉及的 thread；列表仅投影最近 200 条 |
| Approval 投影 | 每个 Turn 单独查询 approval | 一次 SQL 读取 pending `(thread_id, turn_id)` 集合 |
| 进度卡 | 即使可见状态不变也每 2 秒 PATCH | revision 驱动；无状态变化不调用飞书 API |
| 长回复 | 12,000 字符分片对中文可能超字节限制 | 按 UTF-8 字节预算切分且保持 Unicode 完整 |
| Chat 流式读取 | 每 400 ms 传输完整累计回答，近似二次增长 | 生成中只取 4,000 字符预览和总长度；终态完整读取一次 |
| Chat history | 先读取整页全文再在 Python 截断 | 浏览器端先取最近 N 条并限制单条抓取长度 |
| Chat Runtime | 终态记录可能无限留在内存 | 保留最近 32 条终态；活动任务永不淘汰 |
| Chat 输出 | 同名或同产物并发下载共享路径 | 每次交付独立 UUID 目录、原子落盘、交付后清理空目录 |
| 飞书附件 | 同名附件覆盖、路径穿越、Windows 保留名/超长名 | 消息 UUID5 + 资源序号目录；净化为最长 120 字符的 portable leaf |
| 附件批次 | 多资源消息中途失败留下半批状态 | 本消息批次失败时只回滚本批已下载文件和数据库记录 |
| 附件背压 | 未消费附件可无限积累 | 每 chat 最多 32 个，超限在下载前拒绝 |
| Queue | 内存 pending count 漂移可导致空 chat 自旋 | 数据库无 queued 行时直接清理内存调度状态 |
| Chat lock | 历史 chat ID 永久保留 RLock | WeakValueDictionary，锁无调用者后自动回收 |
| rollout dedupe | 技术性 event key 无限增长 | 90 天/25 万行有界维护；维护失败只延期，不影响投影 |
| Doctor | 飞书凭据齐全可掩盖 Codex/DB/lease 失败 | 总 Verdict 继承所有强依赖；SQLite 损坏时返回结构化 FAIL |
| Desktop | 关窗与后台 autostart 竞态 | 关闭信号、阶段检查、生命周期锁和有限 join |
| 日志 | httpx 成功请求持续写盘并暴露标识 | CFR 保留 INFO；httpx/httpcore 成功日志降到 WARNING |

## 6. 性能预算

本地、可重复、无外部账号副作用的性能门禁：

- tracked settings cache：p95 ≤ 0.5 ms。
- 组合 operational 直接调用：p95 ≤ 20 ms。
- loopback HTTP operational：p95 ≤ 50 ms。
- 5,000 个历史 Chat 的 composite session projection：p95 ≤ 50 ms。
- 5,000 个历史 Chat 下完整 operational snapshot：p95 ≤ 50 ms。
- 8 worker 并发写入 2,000 条 SQLite inbox：全部成功且总耗时 ≤ 10 s。
- 50,000 条技术去重记录压缩到 10,000 条：≤ 2 s。
- 128 MiB sparse native rollout 的最近 20 条历史尾读：p95 ≤ 50 ms。
- 创建 20,000 个历史 chat lock 后强制 GC：保留数 ≤ 1。
- Chat 生成中 DOM 文本传输：每次最多 4,000 字符预览；完整回答只在终态读取一次。

最终实测输出保存在 `.tmp/final_performance_audit.json`；该文件是本机验收证据，不进入产品运行时。

## 7. 必须保留的边界与 Unknown

1. **Codex 每 Turn app-server 固定开销**：这是释放 native writer、允许 Codex Desktop 共存的代价。没有新的 writer 协议证据前不做长驻池化。
2. **ChatGPT DOM 适配风险**：网页 selector、下载接口和生成状态属于外部 UI 合约。已有 fail-closed 和结构化错误，但上游页面变化仍需要回归。
3. **超大 Chat 生成文件**：当前通过浏览器认证上下文取得文件并传回，仍存在浏览器 ArrayBuffer/base64 的峰值内存。需要真实大文件需求和协议证据后再实现分块通道。
4. **native rollout 增长**：主要磁盘和上下文增长在 Codex 原生历史，不在 CFR SQLite。重启 CFR 不会重置；应使用 `/new` 创建新 thread。
5. **飞书历史保留**：terminal inbox/replies/approvals 已按现有保留策略和行数上限维护；长期 session/surface/chat-binding 仍作为 durable state 保留，但控制面只读取最近 200 个 composite chat，不会随历史数量线性拖慢 UI。
6. **第三方 SDK 前置开销**：lark-channel 可能在 CFR handler 前执行 sender hydration/contact 查询；必须先确认 SDK 可配置接口，不能通过脆弱 monkey patch 优化。
7. **真实外部 E2E**：单元测试和本地 EXE 验收不能替代真实 Feishu + ChatGPT/Codex 账号任务。该步骤会产生外部副作用，需要单独授权。
8. **大文件职责边界**：PDF/视频内容分析能力由当前执行 Surface 和可用工具决定；CFR 负责可靠传输与状态，不伪造分析结果。

## 8. 结构性判断

`feishu/daemon.py` 与 `chat.py` 体量较大，是后续改动的主要回归面；但本轮没有为了“代码看起来更整齐”继续拆分。当前职责边界已经由 `Gateway → Daemon → CodeSurfaceRuntime / ChromeChatAdapter → Replies` 明确，现阶段继续大迁移的风险高于性能收益。

触发下一次结构拆分的条件：新增第二种飞书 transport、Chat DOM 下载再增加两类以上独立协议、命令模块出现独立发布/测试需求，或 profiling 证明当前文件边界造成实际阻塞。届时应先固定集成契约和行为测试，再移动代码。

## 9. 最终验收清单

1. `python -m compileall -q src scripts tests`
2. 完整 `tests/unit` 全绿。
3. Ruff 只作为运行期名称错误门禁：`F821/F822/F823`；Python 语法由 `compileall` 独立门禁。`git diff --check`、公开发布/Secret 扫描同时通过。不要在 release 收口时把 import 排序、压缩测试 fixture 等历史风格规则升级为阻塞项。
4. `.tmp/resource_leak_gate.py` 无 unclosed event loop/file/socket。
5. `.tmp/final_performance_audit.py` 全部预算通过。
6. `m3_control` TypeScript + Vite production build 通过。
7. PyInstaller one-file、windowed EXE 构建通过。
8. `scripts/run_desktop_smoke.ps1` 使用隔离 `LOCALAPPDATA` 重复冷启动，必须分别识别 Control API 与 WebView2 CDP loopback listener；Control API 未认证访问保持 403，且无可见终端、无隔离测试 orphan。
9. 托盘验证：主窗口 `X` 后进程继续运行且窗口隐藏；第二次启动只唤回原窗口，不创建第二个 Control backend；隔离 smoke 前后不得改变已存在的 CodexHost shim 集合或真实 legacy Chat 登录 marker。
10. 内置 Chat 验证：CFR 只有一个 Windows 顶层窗口；Control Center 对用户可见，ChatGPT 是同一窗口内持续存活但默认隐藏的 WebView2 automation target，仅登录时临时显示。WebView2 CDP 可发现唯一持久 ChatGPT target，连续飞书 Chat 指令不得创建额外 page/window；原生 Thinking effort 与动态模型菜单可读写并校验，按钮型生成文件卡不得依赖伪 file-id 回传。
11. Code approval 验证：`/approval` 三个模式必须映射到当前 Codex `approvalPolicy` / `approvalsReviewer` / `sandbox` 原生字段，并在 `thread/start` 与 `thread/resume` 上生效；file-change 审批卡优先按 `itemId` 投影 `patchUpdated` 的真实路径，没有投影时明确标记协议缺少路径而不是显示 unknown。
12. 共存验证：测试 EXE 不改变已经运行的 CodexHost shim/launcher mode，也不删除 legacy Chat profile/login marker。
13. 真正退出路径由 tray Exit/source-level lifecycle tests 验证完整 cleanup；二进制自动验收结束时只强制回收其自己创建的隔离测试进程，不触碰用户进程。

只有以上证据属于同一份最终源码和同一份 EXE，才可标记本轮完成。
