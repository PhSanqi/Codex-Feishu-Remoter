# CFR ↔ Broker 共存、模型路由与集成协议

**文档名：** `CFR_AND_BROKER_COEXISTENCE_ROUTING_CONTRACT_v1.2.md`  
**版本：** 1.2  
**Wire Protocol：** `cfr-broker/1`  
**Workspace Arbitration Protocol：** `workspace-arbitrator/1`  
**状态：** `NORMATIVE / IMPLEMENTATION BASELINE`  
**适用范围：** Local single-user / small-trusted-scope  
**兼容基线：** 继承 v1.1 的 Authority / Ownership / State Isolation / Route Stickiness / Fencing / Crash Recovery 不变量；本版本收紧 handoff、version/epoch ownership、workspace arbitration、event cursor、idempotency、transport framing、component authentication 与模型路由管理。

---

# 0. 规范用语

本文中的：

- **MUST / 必须**：不可违反的协议、状态机、安全或架构约束。
- **MUST NOT / 禁止**：不可出现的行为。
- **SHOULD / 应当**：默认实现方式；偏离必须有明确理由并记录。
- **MAY / 可以**：允许但非强制。
- **fail closed**：状态不确定时拒绝新写入、authority transition、route activation 或 lease reassignment，而不是猜测继续。
- **Direct Codex**：由 CFR 直接控制、允许面向目标工作区执行编程工作的 Codex。
- **Broker Host Codex**：CFR 承载、仅作为 Broker `external_host` Global Semantic Authority 的 Codex。
- **Broker Global Codex**：Broker `internal_codex` 模式下的 Global Semantic Authority。
- **Broker Codex Subagent**：Broker 内 WorkPackage-scoped bounded executor。
- **Native Codex Thread ID**：Codex provider / app-server / SDK 原生 thread/session identifier。
- **Logical Route ID**：CFR Router 自己维护的 route identifier。
- **GlobalTask ID / WorkPackage ID**：Broker 自己维护的 task identifiers。
- **Workspace Key**：真实工作目录 canonicalization 后得到的跨系统唯一工作区标识。
- **Repository Key**：多个 Git worktree 共享 Git common directory 时对应的逻辑仓库标识。
- **Route Version**：CFR RouteBinding 的 CAS version，仅属于 CFR/Router 状态域。
- **Task Version**：Broker GlobalTask 的 CAS version，仅属于 Broker 状态域。
- **Authority Epoch**：Broker-managed GlobalTask 的 semantic authority fence，由 Broker canonical state 持有并单调递增。
- **Fencing Token**：Workspace lease 每次成功 acquire 后递增的单调 token；旧 token 永久失效。
- **Model Route Policy**：Router 用于机械选择执行路径、模型族、provider preference 与 fallback 行为的策略；不得决定 semantic answer。

---

# 1. 八条最高优先级不变量

所有实现、测试、恢复流程、模型路由策略和未来重构都 MUST 满足：

```text
ONE REQUEST → ONE ROUTE.

ONE GLOBAL TASK → ONE SEMANTIC AUTHORITY.

ONE WORKSPACE → ONE CROSS-SYSTEM WRITER.

ONE NATIVE THREAD → ONE SUBSYSTEM OWNER.

ONE SUBSYSTEM → ITS OWN STATE STORE.

ONE MUTATION → ONE IDEMPOTENCY IDENTITY.

ONE AUTHORITY HANDOFF → ONE NEW AUTHORITY EPOCH.

ONE WORKSPACE REACQUIRE → ONE NEW FENCING TOKEN.
```

任何便利性设计、模型 fallback、provider fallback、retry、crash recovery 或 UI 行为都不能覆盖以上不变量。

---

# 2. 总体结构

```text
Human
  │
Feishu
  │
  ▼
┌─────────────────┐
│       CFR       │
│ Gateway / Host  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│     Router      │
│ Model Routing   │
└────────┬────────┘
         │
    ┌────┼───────────────────────────┐
    │    │                           │
    ▼    ▼                           ▼
direct_codex                 broker_external              broker_internal
    │                              │                            │
CFR Direct Codex            CFR Broker Host Codex         Broker Global Codex
    │                              │ semantic authority          │
    │                              ▼                            │
    │                           Broker ◄─────────────────────────┘
    │                              │
    │                       WorkPackage executor
    │                         ┌────┴────┐
    │                         │         │
    │                     DeepSeek   Codex Subagent
    │                         │         │
    │                         └────┬────┘
    │                              │
    │                      BrokerToolGateway
    │                              │
    ▼                              ▼
Target Workspace            Target Workspace

       ▲                         ▲
       └──── Workspace Arbitrator ────┘
```

控制原则：

```text
CFR owns ingress and host interaction.

Router owns route selection and model-route policy.

Router chooses WHERE and HOW a request runs,
but never WHAT semantic answer is correct.

Semantic Authority owns meaning.

Broker owns Broker-managed execution.

Workspace Arbitrator owns cross-system writer exclusion.

Native thread ownership never crosses subsystem boundaries.
```

---

# 3. 组件职责

## 3.1 CFR

CFR MUST 负责：

- Feishu ingress。
- Human identity / message mapping。
- conversation continuity。
- RouteBinding。
- Codex Desktop continuity。
- Direct Codex logical/native thread ownership。
- Broker external-host semantic session ownership。
- Human-required presentation / response collection。
- status/result presentation。
- CFR audit。
- 调用 Router。
- 作为 `workspace-arbitrator/1` client 为 `direct_codex` 获取/续约/释放 cross-system lease。

CFR MUST NOT：

- 直接读写 Broker SQLite。
- 直接取得 Broker WorkPackage internal writer lease。
- 直接调用 Broker internal MCP client / ToolGateway。
- 保存 DeepSeek credential。
- 恢复 Broker native provider thread。
- 把 Broker Codex Subagent 提升为 Global Semantic Authority。
- 伪造 Broker Task Version 或 Authority Epoch。

---

## 3.2 Router

Router MUST：

- 为新请求选择 RouteKind。
- 应用 Model Route Policy。
- 建立 durable RouteBinding。
- 维持 route stickiness。
- 驱动显式 handoff。
- 调用 Workspace Arbitrator。
- 把 Broker `human_required` event 送回 CFR。
- 保存 route-level routing rationale summary，禁止保存 chain-of-thought。
- fail closed。

Router MUST NOT：

- 自己回答 semantic decision。
- 自己修改 GlobalPlan。
- 自己做 semantic review。
- 自己代替 Human。
- 自己直接调用 Broker internal MCP runtime。
- 因 provider/model 不可用而 silent 改变 semantic authority。
- 把 Route Version 当作 Broker Task Version。

---

## 3.3 Broker

Broker MUST 负责：

- Project。
- GlobalTask。
- GlobalPlan。
- WorkPackage。
- semantic authority origin。
- canonical Authority Epoch。
- Task Version。
- DecisionPlane。
- workspace records。
- internal writer leases。
- execution attempts。
- BrokerToolGateway。
- MCP/tool runtimes。
- permissions。
- Human gates。
- DeepSeek executor。
- bounded Codex subagent。
- deterministic validation。
- semantic review lifecycle。
- Broker audit。
- durable northbound events。
- 作为 `workspace-arbitrator/1` client 为 Broker route 获取/续约/释放 cross-system lease。

Broker MUST NOT：

- 读取 CFR SQLite。
- 恢复 CFR native Codex thread。
- 保存 Feishu credential。
- 承担 Feishu conversation state。
- 假装成为 CFR Desktop continuity runtime。
- 接受 CFR 伪造的 native provider thread ID。
- 把 Route Version 当作 Broker Task Version。

---

## 3.4 Workspace Arbitrator

Workspace Arbitrator 是独立的本地 authority boundary。

它 MUST：

- 维护 canonical Workspace Key。
- 维护 Repository Key。
- 原子执行 cross-system lease acquire/renew/release/inspect。
- 生成单调 fencing token。
- 对 owner state 不确定时进入 `suspect` 并 fail closed。
- 拥有独立 durable state store。
- 不读取 CFR SQLite。
- 不读取 Broker SQLite。
- 不执行模型调用。
- 不做 semantic decision。

它 MUST NOT：

- 被 CFR 或 Broker 的普通 SQLite repository class 直接嵌入。
- 因 TTL 单独过期就覆盖状态未知的旧 writer。
- 暴露 native provider thread ID。

---

# 4. RouteKind

Wire/Router v1 只定义：

```text
direct_codex
broker_external
broker_internal
```

未来模式 MUST 作为新的显式 RouteKind 增加。

---

# 5. Model Route Policy

Router 可以管理模型和执行路径，但它不拥有 semantic authority。

## 5.1 Model Route Policy 输入

允许使用：

- 用户显式 route/model preference。
- continuation 是否已有 RouteBinding。
- workspace 是否需要写入。
- 请求是否需要 Broker-managed tools / WorkPackage orchestration。
- 是否需要 external_host / internal_codex semantic authority。
- provider availability。
- model availability。
- latency/cost policy。
- route capability constraints。
- route health。
- security policy。

禁止使用：

- 模型对“哪个 semantic answer 更正确”的自我声明。
- provider identity 推导 authority。
- native thread identity 推导 route。

## 5.2 Router 输出

Router 机械输出：

```json
{
  "route_kind": "broker_external",
  "semantic_authority_origin": "external_host",
  "model_policy": {
    "semantic_model_family": "codex",
    "executor_preferences": ["deepseek", "codex_subagent"],
    "fallback_mode": "explicit_only"
  },
  "workspace_access": "write",
  "reason_code": "BROKER_ORCHESTRATION_REQUIRED"
}
```

`reason_code` MUST 是枚举型、可审计、非 chain-of-thought。

## 5.3 Fallback

允许：

- 同 authority、同 route 内的 provider retry。
- 同 authority、同 route 内按预定义 policy 更换 executor。
- 无语义 authority 变化的 transport retry。

禁止：

```text
broker_external provider unavailable
→ silently broker_internal
```

```text
broker_internal Global Codex unavailable
→ silently CFR Host Codex
```

```text
direct_codex unavailable
→ silently broker route
```

RouteKind 或 semantic authority 改变 MUST 走 explicit handoff。

---

# 6. direct_codex

```text
Feishu
  ↓
CFR
  ↓
Router
  ↓
CFR Direct Codex
  ↓
Target Workspace
```

Authority：

```text
execution owner = CFR Direct Codex
semantic authority = CFR Direct Codex / Human
Broker = not in route
```

前提：

- CFR MUST 持有对应 Workspace Key 的有效 Cross-System Workspace Lease。
- lease owner_kind MUST 为 `cfr_direct`。
- lease 未确认时 MUST NOT 启动可写 turn。

允许：

- shell。
- filesystem。
- git。
- code edits。

禁止：

- 同一个 Workspace Key 同时存在 Broker cross-system writer owner。

RouteBinding target：

```text
target.kind = cfr_logical_thread
target.logical_id = CFR logical thread ID
```

CFR MAY 在自己的 DB 中保存 native Codex thread ID。

native Codex thread ID MUST NOT 发给 Broker 或 Workspace Arbitrator。

---

# 7. broker_external

```text
Feishu
  ↓
CFR
  ↓
Router
  ↓
CFR Broker Host Codex
  │ Global Semantic Authority only
  ▼
Broker Northbound Adapter
  ↓
Broker
  ↓
DeepSeek / Codex Subagent
  ↓
BrokerToolGateway
  ↓
Target Workspace
```

Broker GlobalTask：

```text
semantic_authority_origin = external_host
```

永久规则：

```text
CFR Direct Codex != CFR Broker Host Codex
```

即使：

- 使用同一个 provider。
- 使用同一个 ChatGPT account。
- 使用同一个 CFR CODEX_HOME。
- 使用同一个 model。

其 capability 仍 MUST 不同。

CFR Broker Host Codex MAY：

- intent interpretation。
- GlobalPlan semantic creation。
- semantic decisions。
- result review。
- rework request。
- evidence arbitration。
- final synthesis。

CFR Broker Host Codex MUST NOT：

- 直接写 Target Workspace。
- 直接调用 target shell/filesystem。
- 直接调用 Broker MCP client。
- 直接调用 Broker ToolGateway。
- 直接修改 Broker SQLite。
- 直接取得 Broker writer lease。

Target Workspace execution MUST：

```text
Broker
→ WorkPackage
→ bounded executor
→ BrokerToolGateway
```

Cross-System Workspace Lease owner MUST 为：

```text
broker
```

---

# 8. broker_internal

```text
Feishu
  ↓
CFR
  ↓
Router
  ↓
Broker
  ↓
Broker Global Codex
  ↓
Broker
  ↓
DeepSeek / Codex Subagent
```

GlobalTask：

```text
semantic_authority_origin = internal_codex
```

CFR 仅负责：

- ingress。
- transport。
- Human bridge。
- status/result presentation。

CFR MUST NOT：

- 为该 GlobalTask 做 semantic plan。
- 做 semantic review。
- 回答 internal semantic decision。
- 替代 Broker Global Codex。

---

# 9. Authority Ownership

Broker-managed GlobalTask 的 Authority Epoch canonical owner = Broker。

CFR RouteBinding MAY cache：

```text
semantic_authority_origin
authority_epoch
```

但该 cache MUST 以 Broker 返回值为准。

任何 authority-sensitive Broker mutation MUST 携带：

```text
expected_authority_epoch
```

不匹配：

```text
AUTHORITY_EPOCH_MISMATCH
```

任何 Global Semantic Authority 显式切换 MUST：

```text
authority_epoch += 1
```

旧 authority 在 epoch 改变后提交的 plan/review/decision MUST 被拒绝。

Direct route 不使用 Broker Authority Epoch。

---

# 10. Native Thread Ownership

```text
CFR native Codex thread
belongs only to CFR.

Broker native Codex thread
belongs only to Broker.
```

跨系统只允许逻辑 ID：

- route_id。
- broker_project_id。
- broker_global_task_id。
- broker_work_package_id。
- external_semantic_operation_id。
- human_request_id。

任何 CFR ↔ Broker payload MUST NOT 包含 native provider thread ID。

---

# 11. CODEX_HOME Ownership

## 11.1 CFR

默认：

```text
CFR_CODEX_HOME = %USERPROFILE%\.codex
```

优先级：

```text
explicit --codex-home
→ CFR_CODEX_HOME
→ %USERPROFILE%\.codex
```

CFR 启动每个 Codex child 时 MUST 显式注入：

```text
CODEX_HOME=<resolved CFR_CODEX_HOME>
```

CFR MUST NOT 依赖 ambient/global `CODEX_HOME` 隐式决定 home。

## 11.2 Broker

默认：

```text
BROKER_CODEX_HOME =
%LOCALAPPDATA%\agent-orchestrator\codex-home
```

优先级：

```text
explicit Broker config
→ BROKER_CODEX_HOME
→ <Broker dataDirectory>\codex-home
```

Broker 启动 Global Codex / Codex Subagent 时 MUST 显式注入：

```text
CODEX_HOME=<resolved BROKER_CODEX_HOME>
```

Broker MUST NOT 默认使用 `%USERPROFILE%\.codex`。

禁止：

- CFR 与 Broker 默认共用一个 CODEX_HOME。
- 复制 CFR/Desktop sessions 到 Broker。
- 复制 Broker sessions 到 CFR。
- 通过 rollout/session/native-thread-ID copying 实现 continuity。

---

# 12. Database Ownership

```text
CFR DB != Broker DB != Workspace Arbitrator DB
```

推荐：

```text
CFR:
%LOCALAPPDATA%\cfr\state.sqlite

Broker:
%LOCALAPPDATA%\agent-orchestrator\state.sqlite

Workspace Arbitrator:
%LOCALAPPDATA%\cfr\workspace-arbitrator.sqlite
```

禁止：

```text
CFR → SELECT/UPDATE Broker SQLite
Broker → SELECT/UPDATE CFR SQLite
CFR/Broker → SELECT/UPDATE Arbitrator SQLite
Arbitrator → SELECT/UPDATE CFR/Broker SQLite
```

系统集成 MUST 经过 versioned API/RPC。

---

# 13. RouteBinding Schema

每个可持续 route MUST 有 durable RouteBinding。

建议 v1.2 内部模型：

```json
{
  "route_id": "route_xxx",
  "ingress_conversation_id": "feishu_xxx",

  "generation": 1,
  "route_version": 7,

  "route_kind": "broker_external",
  "status": "active",
  "availability": "available",

  "workspace_key": "wk_xxx",
  "repository_key": "repo_xxx",

  "target": {
    "kind": "broker_global_task",
    "logical_id": "gt_xxx"
  },

  "semantic_authority_origin": "external_host",
  "observed_authority_epoch": 3,
  "observed_task_version": 12,

  "cross_system_lease_id": "lease_xxx",
  "cross_system_fencing_token": 14,

  "event_cursor": {
    "generation": 1,
    "global_task_id": "gt_xxx",
    "event_seq": 92
  },

  "model_route_policy_id": "policy_default_v1",

  "created_at": "...",
  "updated_at": "...",
  "terminal_at": null
}
```

`target.logical_id` MUST NOT 是：

- CFR native Codex thread ID。
- Broker native provider thread ID。
- provider session ID。

---

# 14. Route State 与 Availability 分离

## 14.1 Route status

正式状态：

```text
creating
active
waiting_human
paused
handoff_preparing
cancelling
completed
failed
cancelled
recovery_blocked
```

Terminal：

```text
completed
failed
cancelled
```

## 14.2 Availability

独立字段：

```text
available
degraded
unavailable
unknown
```

例如 Broker unavailable：

```text
status = active
availability = unavailable
```

不得为了表达 availability 发明新的 Route state。

## 14.3 Route CAS

所有 CFR route state-sensitive mutation SHOULD 带：

```text
expected_route_version
```

不匹配：

```text
ROUTE_VERSION_CONFLICT
```

Broker Northbound API MUST NOT 接收 `expected_route_version` 作为 Broker task CAS。

---

# 15. Route Stickiness

一旦 continuation 已绑定：

```text
Feishu conversation
→ RouteBinding R1
→ target logical task
```

后续：

```text
继续
怎么样了
重试
改第三步
暂停
取消
```

MUST 继续 R1。

禁止每条消息重新分类 RouteKind。

Model Route Policy 对已有 active RouteBinding MUST 默认返回 `KEEP_EXISTING_ROUTE`。

只有显式 handoff 可以改变 route。

---

# 16. HandoffRecord

route 变更 MUST 使用 durable handoff。

```json
{
  "handoff_id": "handoff_xxx",
  "route_id": "route_xxx",
  "from_generation": 1,
  "to_generation": 2,
  "from_route_kind": "direct_codex",
  "to_route_kind": "broker_external",
  "state": "preparing",
  "expected_route_version": 7,
  "expected_authority_epoch": null,
  "source_lease_id": "lease_old",
  "target_lease_id": null,
  "created_at": "...",
  "updated_at": "..."
}
```

状态：

```text
preparing
source_quiesced
source_lease_released
target_prepared
target_lease_acquired
committed
failed
recovery_blocked
```

---

# 17. Handoff 原子化规则

目标：target 在 route commit 前 MUST 不可执行。

正常流程：

```text
1. CAS RouteBinding → handoff_preparing
2. persist HandoffRecord.preparing
3. quiesce source runtime/writer
4. confirm no active source write
5. persist source_quiesced
6. release source Cross-System Workspace Lease
7. persist source_lease_released
8. prepare target logical object in DORMANT/PENDING_ACTIVATION state
9. persist target_prepared
10. acquire target Cross-System Workspace Lease if required
11. persist target_lease_acquired
12. if semantic authority changes, Broker increments canonical authority_epoch
13. atomically commit new RouteBinding generation + route_kind + target + observed epoch/task version
14. activate target
15. mark HandoffRecord.committed
16. RouteBinding.status = active
```

关键规则：

- `target_prepared` MUST NOT 表示 target 可执行。
- Broker `task.create` 用于 handoff prepare 时 MUST 支持 `activation_mode = deferred`，或提供等价的 prepare operation。
- deferred target MUST NOT dispatch WorkPackage、invoke semantic model、call tools 或写 workspace。
- target activation MUST 在 route commit 后发生。
- target activation failure MUST 进入 `recovery_blocked`，不得 silent 回退 source route。

任一步 crash 后 MUST 通过 HandoffRecord 恢复，而不是重新猜 route。

---

# 18. Workspace Key

writer exclusion MUST 基于 canonical workspace，而不是用户输入字符串。

Windows 至少：

```text
absolute path
normalized separators
case normalization
real/canonical resolution where possible
```

无法安全 canonicalize：

```text
WORKSPACE_CANONICALIZATION_FAILED
```

并 fail closed。

---

# 19. Repository Key

Git worktree 的 workspace path 可以不同，但仍共享 Git common directory。

```text
Workspace Key = worktree-local filesystem write domain
Repository Key = shared Git metadata domain
```

多个 worktree MAY 有不同 Workspace Key，但若 `.git` 指向同一 common directory，则 MUST 有同一个 Repository Key。

---

# 20. Cross-System Workspace Lease

永久不变量：

```text
One canonical real workspace
=
at most one cross-system writer owner.
```

owner：

```text
cfr_direct
broker
```

建议 schema：

```json
{
  "workspace_key": "wk_xxx",
  "lease_id": "lease_xxx",
  "owner_kind": "broker",
  "owner_logical_id": "gt_xxx",
  "status": "active",
  "fencing_token": 42,
  "acquired_at": "...",
  "heartbeat_at": "...",
  "expires_at": "...",
  "released_at": null,
  "version": 3
}
```

---

# 21. workspace-arbitrator/1 API

Workspace Arbitrator MUST 至少提供：

```text
workspace.lease.acquire
workspace.lease.renew
workspace.lease.release
workspace.lease.inspect

repository.lease.acquire
repository.lease.renew
repository.lease.release
repository.lease.inspect
```

## 21.1 Workspace acquire

请求：

```json
{
  "workspace_key": "wk_xxx",
  "owner_kind": "broker",
  "owner_logical_id": "gt_xxx",
  "requested_ttl_ms": 30000,
  "expected_current_lease_id": null
}
```

成功：

- 原子确认不存在有效冲突 owner。
- 创建新 lease_id。
- `fencing_token` 单调递增。
- 返回 expires_at。

冲突：

```text
WORKSPACE_LEASE_CONFLICT
```

## 21.2 Renew

必须同时匹配：

```text
lease_id
owner_kind
owner_logical_id
fencing_token
```

否则：

```text
LEASE_FENCED
```

## 21.3 Release

release MUST 幂等。

重复 release 同一 lease 返回第一次 release 的 logical result。

## 21.4 Repository lease

Repository-level destructive operations SHOULD 获取 Repository Lease，包括：

- `git gc`。
- branch deletion/rename。
- shared ref destructive updates。
- repository-wide maintenance。
- 可能影响所有 worktree 的 destructive operation。

普通 worktree-local commit MAY 依赖 Git 自身 lock，不要求长期 Repository Lease。

---

# 22. Fencing 与 Lease Recovery

每次成功 workspace acquire：

```text
fencing_token = previous_token + 1
```

旧 token MUST 永久失效。

BrokerToolGateway 对所有 workspace write MUST 校验当前有效 fencing token。

CFR Direct Codex：

1. 启动可写 turn 前确认 lease 有效。
2. turn 运行期间 heartbeat。
3. lease 丢失或 fencing token 改变时 MUST 尝试停止 CFR-owned writer runtime。
4. 在旧 CFR writer 被确认退出前，Arbitrator MUST NOT 仅因 TTL 到期把 Workspace Key 授予另一个 owner。
5. owner process 状态未知时：

```text
lease.status = suspect
```

并 fail closed。

Lease recovery：

```text
expired + previous owner definitely inactive
→ MAY reacquire with new fencing token

expired + previous owner alive
→ MUST NOT reassign

expired + previous owner unknown
→ suspect
→ fail closed
```

---

# 23. 两层 Lease

```text
Cross-System Workspace Lease
         ↓
Subsystem Internal Lease
```

CFR：

```text
cross-system owner = cfr_direct
→ CFR thread/app-server writer lifecycle
```

Broker：

```text
cross-system owner = broker
→ Broker WorkPackage WriterLease
```

Cross-System Lease 不替代 subsystem internal lease。

---

# 24. cfr-broker/1 Request Envelope

```json
{
  "protocol": "cfr-broker/1",
  "message_type": "request",

  "request_id": "req_xxx",
  "trace_id": "trace_xxx",
  "route_id": "route_xxx",
  "route_generation": 1,

  "operation": "task.create",
  "idempotency_key": "idem_xxx",

  "deadline_at": "2026-08-18T00:00:00Z",

  "expected_task_version": 12,
  "expected_authority_epoch": 3,

  "caller": {
    "component": "cfr-router",
    "instance_id": "cfr_xxx"
  },

  "payload": {}
}
```

规则：

- mutating operation MUST 有 `idempotency_key`。
- Broker task state-sensitive mutation SHOULD 有 `expected_task_version`。
- authority-sensitive mutation SHOULD 有 `expected_authority_epoch`。
- `request_id` 每次网络调用唯一。
- `trace_id` MAY 跨重试保持。
- `route_generation` MUST 匹配当前 CFR RouteBinding generation。
- `deadline_at` 过期后接收端 MUST NOT 开始新的 mutation。
- `expected_route_version` MUST NOT 进入 Broker task mutation envelope。

---

# 25. Response Envelope

成功：

```json
{
  "protocol": "cfr-broker/1",
  "message_type": "response",
  "request_id": "req_xxx",
  "trace_id": "trace_xxx",
  "route_id": "route_xxx",
  "route_generation": 1,
  "ok": true,
  "result": {},
  "server_time": "..."
}
```

失败：

```json
{
  "protocol": "cfr-broker/1",
  "message_type": "response",
  "request_id": "req_xxx",
  "trace_id": "trace_xxx",
  "route_id": "route_xxx",
  "route_generation": 1,
  "ok": false,
  "error": {
    "code": "TASK_VERSION_CONFLICT",
    "message": "task version changed",
    "retryable": false,
    "current_task_version": 13,
    "details": {}
  },
  "server_time": "..."
}
```

同一 response MUST NOT 同时有 `result` 和 `error`。

---

# 26. Error Namespace

v1 至少冻结：

```text
PROTOCOL_VERSION_UNSUPPORTED
FRAME_INVALID
MESSAGE_TOO_LARGE
INVALID_REQUEST
CALLER_UNAUTHORIZED
OPERATION_NOT_ALLOWED

ROUTE_NOT_FOUND
ROUTE_GENERATION_MISMATCH
ROUTE_STATE_CONFLICT
ROUTE_VERSION_CONFLICT

TASK_NOT_FOUND
TASK_VERSION_CONFLICT
WORK_PACKAGE_NOT_FOUND
AUTHORITY_EPOCH_MISMATCH

IDEMPOTENCY_CONFLICT
IDEMPOTENCY_IN_PROGRESS

HUMAN_REQUEST_NOT_FOUND
HUMAN_REQUEST_STALE

WORKSPACE_CANONICALIZATION_FAILED
WORKSPACE_LEASE_CONFLICT
REPOSITORY_LEASE_CONFLICT
LEASE_FENCED
LEASE_RECOVERY_REQUIRED

BROKER_UNAVAILABLE
CFR_UNAVAILABLE
ARBITRATOR_UNAVAILABLE
PROVIDER_UNAVAILABLE
RATE_LIMITED

DEADLINE_EXCEEDED
TIMEOUT
CANCELLED

INTERNAL_ERROR
```

未知 error code MUST 被调用方按不可自动重试处理，除非 response 明确 `retryable=true`。

---

# 27. Idempotency

作用域：

```text
authenticated_caller_id
+ route_id
+ route_generation
+ operation
+ idempotency_key
```

## 27.1 Canonical payload

接收方 MUST：

1. 解析请求到 typed schema。
2. 移除 transport-only fields。
3. 对对象 key 使用稳定排序。
4. 使用 UTF-8 canonical JSON 序列化。
5. 计算 payload fingerprint。

推荐：

```text
SHA-256(canonical-json(payload))
```

## 27.2 Atomic claim

第一次收到 mutating request：

```text
atomically insert idempotency record
state = in_progress
```

并记录：

- scope。
- fingerprint。
- logical mutation identity。
- created_at。

并发第二个相同 key + 相同 fingerprint：

- MAY 等待第一次完成；或
- 返回：

```text
IDEMPOTENCY_IN_PROGRESS
```

但 MUST NOT 创建第二个 mutation。

同 key + 不同 fingerprint：

```text
IDEMPOTENCY_CONFLICT
```

第一次 mutation 完成后：

```text
state = completed
```

后续同 key + 同 fingerprint MUST 返回第一次 logical result。

保留期 SHOULD 至少：

- 24 小时；或
- 对应 RouteBinding/GlobalTask terminal 后 24 小时；
- 取更晚者。

---

# 28. Northbound Operation Set

v1 正式操作：

```text
task.create
task.activate
task.inspect
task.cancel

external.plan.submit
external.review.submit
external.decision.resolve

human.response.submit

result.read

event.subscribe
event.ack
```

不开放：

```text
query_sqlite
write_sqlite
grant_writer_lease
resume_native_codex_thread
call_broker_mcp_directly
start_runtime_directly
set_semantic_authority_without_handoff
set_execution_session
set_principal_as_model_claim
```

---

# 29. task.create / task.activate

`task.create` 支持：

```text
activation_mode = immediate | deferred
```

Handoff prepare MUST 使用：

```text
activation_mode = deferred
```

成功返回：

```text
global_task_id
state
task_version
authority_epoch
activation_state
```

不得返回 Broker native provider thread ID。

`task.activate`：

- MUST 只允许 deferred task。
- MUST 校验 task_version。
- MUST 校验 authority_epoch。
- MUST 校验 route_generation。
- 如果 task 将写 workspace，调用方 MUST 已持有对应 Broker cross-system lease。

---

# 30. task.inspect

只返回 northbound-safe state：

- global_task_id。
- state。
- task_version。
- authority_epoch。
- activation_state。
- current_phase。
- WorkPackage counts/status。
- human_required status。
- artifact refs。
- safe failure code。

禁止返回：

- raw provider transcript。
- raw model reasoning。
- credentials。
- SQLite rows。
- runtime handles。
- native provider thread IDs。

---

# 31. external.plan.submit

仅用于：

```text
semantic_authority_origin = external_host
```

必须校验：

- GlobalTask。
- authority origin。
- expected_authority_epoch。
- expected_task_version。
- operation claim。
- authenticated caller identity。
- route_generation。

CFR Host Codex 只能提交 semantic payload。

不得自声明：

- lease ID。
- runtime ID。
- trusted principal。
- execution session ID。

---

# 32. external.review.submit

仅用于 `external_host`。

允许语义结果：

```text
accept
needs_rework
request_evidence
human_required
```

CFR Host Codex MUST NOT 直接把 WorkPackage 标记 completed。

最终状态转换由 Broker 执行。

---

# 33. external.decision.resolve

```text
bounded executor
→ Broker DecisionPlane
→ waiting_external_authority
→ CFR Host Codex
→ external.decision.resolve
→ Broker
```

CFR MUST NOT 直接联系 bounded executor native provider thread。

---

# 34. Human-Required

HumanRequest：

```json
{
  "human_request_id": "human_xxx",
  "global_task_id": "gt_xxx",
  "route_id": "route_xxx",
  "route_generation": 1,
  "authority_epoch": 3,
  "state": "open",
  "prompt": "...",
  "choices": [],
  "created_at": "...",
  "expires_at": null
}
```

状态：

```text
open
responded
expired
cancelled
```

Broker → CFR：

```text
human.required event
```

CFR：

```text
present
authenticate
collect
return
```

禁止：

- CFR 自动回答。
- CFR Codex 替 Human 回答。
- Broker Codex Subagent 替 Human 回答。

`human.response.submit` MUST 携带：

```text
human_request_id
global_task_id
route_id
route_generation
expected_authority_epoch
```

非 open request：

```text
HUMAN_REQUEST_STALE
```

---

# 35. Broker Event Protocol

Event envelope：

```json
{
  "protocol": "cfr-broker/1",
  "message_type": "event",
  "event_id": "evt_xxx",
  "route_id": "route_xxx",
  "route_generation": 1,
  "global_task_id": "gt_xxx",
  "event_seq": 93,
  "event_type": "human.required",
  "occurred_at": "...",
  "payload": {}
}
```

v1 event types 至少：

```text
task.status.changed
human.required
task.completed
task.failed
task.cancelled
```

交付语义：

```text
at-least-once
```

CFR MUST 以 `event_id` 去重。

`event_seq` MUST 在以下 scope 内单调递增：

```text
(route_id, route_generation, global_task_id)
```

不同 generation / GlobalTask 的 seq MAY 从 1 重新开始。

---

# 36. Event Cursor / Replay

CFR RouteBinding 保存：

```json
{
  "generation": 1,
  "global_task_id": "gt_xxx",
  "event_seq": 92
}
```

`event.subscribe`：

```json
{
  "route_id": "route_xxx",
  "route_generation": 1,
  "payload": {
    "global_task_id": "gt_xxx",
    "after_event_seq": 92
  }
}
```

Broker MUST：

- 从下一条 durable event 开始 replay。
- 然后继续 live delivery。
- 不要求 CFR 持有 Broker internal runtime handle。

CFR 收到并 durable persist 后调用 `event.ack`。

ack 至少包含：

```text
event_id
route_generation
global_task_id
event_seq
```

Handoff 到新 GlobalTask 后 MUST 建立新的 cursor scope。

---

# 37. task.cancel

Broker 自己负责：

- WorkPackage cancellation。
- provider cancellation。
- runtime shutdown。
- internal lease release。
- state transition。
- audit。

CFR MUST NOT 直接 kill Broker child process，除非系统管理员执行故障恢复。

cancel MUST 幂等。

已 terminal task 的重复 cancel MUST 返回当前 terminal state，而不是创建新 mutation。

---

# 38. result.read

返回：

- final result。
- artifact references。
- validation summary。
- safe semantic review summary。
- safe status。

不得返回：

- raw model transcript。
- chain-of-thought。
- credentials。
- provider internal state。

---

# 39. Transport

Windows 第一实现 SHOULD 使用：

```text
named pipe
```

可选：

```text
localhost authenticated RPC
```

无论哪种 transport，MUST：

- 有协议版本。
- 有 authenticated caller identity。
- 只允许本地可信 caller。
- 不复用 Broker internal MCP stdio channel。
- 不把 credential 写日志。
- 拒绝未知 major version。
- 有 framing。
- 有消息大小限制。
- 有 in-flight 限制。
- 有 backpressure。

---

# 40. Framing

`cfr-broker/1` 与 `workspace-arbitrator/1` 默认 framing：

```text
4-byte unsigned big-endian payload length
followed by UTF-8 JSON payload
```

规则：

- payload MUST 是单个 UTF-8 JSON object。
- maximum payload size 默认 `1 MiB`。
- 超过：

```text
MESSAGE_TOO_LARGE
```

- invalid length / invalid UTF-8 / invalid JSON：

```text
FRAME_INVALID
```

- 单连接默认最大 in-flight requests：`32`。
- 超过 SHOULD 施加 backpressure，不得无限缓存。
- event replay SHOULD 分批，每批默认不超过 `100` events 或 `512 KiB`。

---

# 41. Named Pipe Identity

若使用 named pipe：

- pipe ACL MUST 默认只允许当前用户 SID。
- MAY 允许本机 Administrators 用于故障恢复。
- MUST NOT 使用 Everyone / Authenticated Users broad write ACL。
- 同用户 SID 只代表 OS-user authorization，不等于 component authentication。
- CFR/Router 与 Broker MUST 额外使用 installation-scoped local capability secret 完成 component authentication。
- secret 至少 256-bit 随机。
- secret 保存于 user-only ACL 文件。
- secret MUST NOT 进入 source、audit、RouteBinding、Broker DB payload。
- hello 中 MUST 证明 capability possession。

Authorization failure：

```text
CALLER_UNAUTHORIZED
```

---

# 42. Transport Hello

第一帧：

```json
{
  "protocol": "cfr-broker/1",
  "message_type": "hello",
  "component": "cfr-router",
  "instance_id": "cfr_xxx",
  "supported_major": [1],
  "auth": {
    "scheme": "local-capability-v1",
    "proof": "..."
  }
}
```

Broker：

```json
{
  "protocol": "cfr-broker/1",
  "message_type": "hello_ack",
  "component": "broker",
  "instance_id": "broker_xxx",
  "selected_major": 1,
  "max_message_bytes": 1048576,
  "max_in_flight": 32
}
```

未知 major：

```text
PROTOCOL_VERSION_UNSUPPORTED
```

fail closed。

---

# 43. Proxy / Network Environment

CFR 与 Broker SHOULD 使用：

```text
child-only network environment
```

分别解析：

- HTTP_PROXY。
- HTTPS_PROXY。
- NO_PROXY。

禁止为了其中一个组件工作而修改 system-wide proxy/environment 从而影响另一个组件。

---

# 44. Process Ownership

CFR owns：

- CFR-created Codex app-server children。
- CFR daemon。

Broker owns：

- Broker-created Codex SDK/CLI children。
- DeepSeek runtime。
- Broker-owned MCP runtime children。
- Broker service process。

禁止：

- CFR 复用 Broker child process handle。
- Broker 复用 CFR app-server process。

跨系统 cancel 使用 protocol，不跨进程偷 handle。

---

# 45. Failure Isolation

CFR crash：

```text
MUST NOT corrupt Broker state.
```

Broker crash：

```text
MUST NOT corrupt CFR state.
```

Workspace Arbitrator crash：

```text
new cross-system writes MUST fail closed
until lease state is reconciled.
```

Broker unavailable：

```text
broker_* route
→ availability = unavailable
```

MUST NOT 自动变成 `direct_codex`。

CFR unavailable：

- `broker_internal` MAY 继续非 Human-required 工作。
- Human-required 等待 CFR/Human channel。
- `broker_external` MUST 等待 external authority。
- MUST NOT 自动切换 internal Codex authority。

Provider unavailable：

- MAY retry within current route/authority policy。
- MUST NOT silent authority handoff。

---

# 46. Restart / Reconciliation

## 46.1 CFR startup

CFR MUST：

1. 恢复 nonterminal RouteBinding。
2. 恢复 CFR logical/native thread binding。
3. 恢复 event cursor。
4. 对 broker route 调 `task.inspect`。
5. 对 direct route 检查 Workspace Arbitrator lease 与 CFR writer process 状态。
6. 对 handoff route 读取 HandoffRecord。
7. 发现不一致时 fail closed 到 `recovery_blocked`。

## 46.2 Broker startup

Broker MUST 自己恢复：

- GlobalTask。
- WorkPackage。
- canonical Authority Epoch。
- Task Version。
- internal provider bindings。
- internal leases/recovery state。
- pending HumanRequest。
- durable northbound events。

不得向 CFR 索要 native provider thread ID。

## 46.3 Arbitrator startup

Arbitrator MUST：

- 恢复 active/suspect lease records。
- 不因进程重启自动释放 lease。
- 对旧 owner 活性未知的 lease 标记 `suspect`。
- 在 owner 被证明 inactive 前拒绝新 owner acquire。

---

# 47. Route/Handoff Recovery

启动发现：

```text
RouteBinding.status = handoff_preparing
```

MUST 读取 HandoffRecord。

按最后 durable step 恢复：

```text
preparing
→ ensure source quiescence

source_quiesced
→ release/verify source lease

source_lease_released
→ prepare dormant target

target_prepared
→ acquire/verify target lease

target_lease_acquired
→ commit route generation/authority

committed
→ activate/verify target
→ mark route active
```

不能重新从用户文本猜 RouteKind。

---

# 48. Audit Boundary

CFR audit SHOULD 记录：

- route created。
- model route policy decision code。
- route selected。
- route handoff。
- Broker request sent。
- Broker event consumed。
- human response sent。
- workspace cross-system lease reference。
- recovery decision。

禁止保存：

- chain-of-thought。
- raw model reasoning。
- raw provider transcript。

Broker audit继续记录：

- GlobalTask。
- WorkPackage。
- authority epoch transition。
- task version transition。
- decision。
- lease reference。
- tool。
- runtime。
- semantic operation。
- human gate。

Arbitrator audit SHOULD 记录：

- acquire/renew/release。
- fencing token changes。
- suspect transitions。
- recovery decisions。

三个 audit store MUST 独立。

---

# 49. Credentials Ownership

CFR owns：

- Feishu credentials。
- CFR-specific service credentials。
- CFR Codex auth state。

Broker owns：

- DeepSeek credential。
- Broker Codex auth state。
- Broker tool/MCP credentials。

Arbitrator owns：

- 仅本地 RPC capability material。

禁止：

```text
CFR 获取 DEEPSEEK_API_KEY
Broker 获取 Feishu app secret
```

两个系统与 Arbitrator MUST：

- 不在 source 中硬编码 secret。
- 不在日志/audit 中保存 secret。
- 不在 RouteBinding 中保存 secret。
- 请求只携带 operation 必需 credential/capability proof。

---

# 50. Protocol Version

Wire：

```text
cfr-broker/1
workspace-arbitrator/1
```

不兼容 wire change MUST 升 major。

同 major 内 MAY 增加向后兼容字段。

未知字段 SHOULD 忽略，除非字段属于 authority/security critical tagged union。

未知 major：

```text
PROTOCOL_VERSION_UNSUPPORTED
```

Authority/security-critical payload 禁止“猜着解析”。

---

# 51. 实现阶段

推荐顺序：

```text
Phase 0
两个项目独立稳定

Phase 1
CFR_CODEX_HOME explicit injection
BROKER_CODEX_HOME explicit isolation
DB isolation
native thread ownership

Phase 2
Workspace Arbitrator
Workspace/Repository canonicalization
lease + fencing
recovery

Phase 3
RouteBinding
Route state + availability
Route stickiness
Model Route Policy

Phase 4
cfr-broker/1 framing
request/response/error/idempotency
transport identity/authentication

Phase 5
durable event delivery
event cursor/replay
Human-required Feishu roundtrip

Phase 6
durable deferred handoff
route generation
authority epoch synchronization

Phase 7
broker_external E2E

Phase 8
broker_internal E2E

Phase 9
single-product packaging
```

---

# 52. Integration Gate

真正 CFR ↔ Broker 联调前：

Broker SHOULD 已达到：

```text
local operational milestone PASS
real live certification PASS
explicit BROKER_CODEX_HOME PASS
```

CFR SHOULD 已达到：

```text
stable Codex Desktop continuity
stable explicit CFR_CODEX_HOME
stable state persistence
current auth/network/model-backed integration PASS
```

Workspace Arbitrator SHOULD 已达到：

```text
lease/fencing deterministic PASS
crash recovery PASS
same-user + component auth PASS
```

不应在 subsystem 自身 live gate 不稳定时同时开发 cross-system integration。

---

# 53. Conformance Tests

## C01 CODEX_HOME isolation

```text
CFR child CODEX_HOME != Broker child CODEX_HOME
```

## C02 Desktop continuity

CFR-created Desktop-compatible thread：

```text
CFR 可继续
Desktop 可发现/继续（按当前 Codex 实际能力）
```

Broker 不得发现/绑定该 native thread。

## C03 Broker thread isolation

Broker Global Codex / Subagent 使用 Broker CODEX_HOME。

CFR 不得将其纳入 CFR binding。

## C04 DB isolation

```text
CFR DB != Broker DB != Arbitrator DB
```

## C05 Direct workspace exclusion

CFR 持有 Workspace A lease。

Broker 在 A 请求 write：

```text
WORKSPACE_LEASE_CONFLICT
```

## C06 Broker workspace exclusion

Broker 持有 A。

CFR Direct Codex write：

```text
reject / wait
```

## C07 Worktree parallelism

两个不同 Workspace Key：

```text
A-cfr
A-broker
```

允许 worktree-local 并行 writer。

## C08 Repository coordination

两个 worktree 共用同一 Repository Key。

同时执行 repository-wide destructive maintenance：

第二个请求 MUST 被拒绝/等待。

## C09 broker_external authority

```text
route = broker_external
```

必须：

```text
CFR Host semantic provider call > 0
Broker internal Global Codex semantic call = 0
```

除非 explicit audited handoff。

## C10 broker_external execution isolation

CFR Broker Host Codex：

```text
target workspace direct writes = 0
```

所有写操作经过 BrokerToolGateway。

## C11 broker_internal authority

```text
route = broker_internal
```

CFR semantic decision call：

```text
0
```

## C12 Human-required

```text
Broker
→ CFR
→ Feishu Human
→ CFR
→ Broker
```

roundtrip PASS。

## C13 Route stickiness

CFR/Router restart 后，同一 continuation：

```text
same RouteBinding
same generation
same logical target
```

## C14 No native ID leakage

CFR ↔ Broker payload：

```text
no CFR native Codex thread ID
no Broker native provider thread ID
```

## C15 Credential isolation

CFR env/log/DB：

```text
no DeepSeek secret
```

Broker env/log/DB：

```text
no Feishu secret
```

## C16 No silent fallback

停止 CFR external authority：

```text
broker_external task
→ waits / unavailable
```

不能自动转 `broker_internal`。

## C17 Cancellation

```text
Feishu cancel
→ CFR
→ Router
→ Broker
→ task cancelled
→ runtimes/leases cleaned
```

## C18 Route CAS

两个并发 CFR route mutation 使用同一 `expected_route_version`。

只能一个成功。

另一个：

```text
ROUTE_VERSION_CONFLICT
```

## C19 Task CAS

两个并发 Broker task mutation 使用同一 `expected_task_version`。

只能一个成功。

另一个：

```text
TASK_VERSION_CONFLICT
```

## C20 Authority epoch fencing

旧 `expected_authority_epoch` 提交 plan/review：

```text
AUTHORITY_EPOCH_MISMATCH
```

## C21 Idempotency sequential retry

同 idempotency key + 同 canonical payload 重试：

```text
one logical mutation
same logical result
```

## C22 Idempotency concurrent claim

两个并发相同 key + 相同 payload：

```text
one mutation only
```

第二个只能：

```text
wait
or
IDEMPOTENCY_IN_PROGRESS
```

## C23 Idempotency conflict

同 key + 不同 payload：

```text
IDEMPOTENCY_CONFLICT
```

## C24 Workspace fencing

旧 fencing token 在 lease reacquire 后写 BrokerToolGateway：

```text
LEASE_FENCED
```

## C25 Crash during handoff

在 `target_prepared` 后 crash。

重启后：

```text
target remains dormant
resume durable handoff
no silent route reclassification
```

## C26 Crash after target lease

在 `target_lease_acquired` 后 crash。

重启后：

```text
verify lease
commit route or recovery_blocked
no duplicate lease
no duplicate task activation
```

## C27 Event replay

CFR 掉线。

Broker 产生 events。

CFR reconnect with scoped cursor：

```text
all missing events replay exactly once logically
```

物理交付允许 at-least-once。

## C28 Event cursor generation isolation

Generation 1 event_seq=92，handoff 后 Generation 2 新 task event_seq=1。

CFR MUST 正确消费 Generation 2 event 1，不得因旧 cursor=92 跳过。

## C29 Lease expiry with unknown writer

lease 过期但旧 CFR writer process 状态 unknown：

```text
new writer acquisition denied
state = suspect/recovery_blocked
```

## C30 Transport authorization

未授权 local client：

```text
CALLER_UNAUTHORIZED
```

## C31 Same-user component impersonation

同一 Windows user 的未授权进程即使能连接 named pipe，也不能通过 local capability authentication。

## C32 Unknown protocol major

发送：

```text
cfr-broker/99
```

返回：

```text
PROTOCOL_VERSION_UNSUPPORTED
```

## C33 Framing limit

发送 >1 MiB frame：

```text
MESSAGE_TOO_LARGE
```

## C34 Malformed frame

非法 length / UTF-8 / JSON：

```text
FRAME_INVALID
```

## C35 Model route no authority inference

provider/model 相同但 route_kind 不同：

```text
authority remains defined by route + task state
```

不得从 model/provider identity 推导 authority。

## C36 Model route stickiness

已有 active RouteBinding 时模型路由策略重新评估：

```text
KEEP_EXISTING_ROUTE
```

除非 explicit handoff request/policy condition。

## C37 Provider outage no silent handoff

当前 `broker_external` semantic provider 不可用：

```text
availability = degraded/unavailable
```

不得 silent 变为 `broker_internal`。

---

# 54. 第一阶段最小验收

真正实现 broker_external real E2E 前，至少完成：

```text
G01 CFR CODEX_HOME explicit injection PASS
G02 Broker CODEX_HOME explicit isolation PASS
G03 CFR/Broker/Arbitrator DB isolation PASS
G04 Workspace Arbitrator lease + fencing PASS
G05 Workspace/Repository canonicalization PASS
G06 RouteBinding state persistence PASS
G07 Route status + availability separation PASS
G08 Model Route Policy PASS
G09 request/response/error framing PASS
G10 Route Version / Task Version separation PASS
G11 idempotency atomic claim PASS
G12 route stickiness PASS
G13 caller auth PASS
G14 durable Broker event replay PASS
G15 scoped event cursor PASS
G16 human_required roundtrip PASS
G17 deferred target handoff PASS
G18 authority epoch fencing PASS
G19 crash recovery PASS
```

G01–G19 全部 PASS 后，才进入 `broker_external` real E2E。

---

# 55. 永久禁止清单

永久禁止：

```text
CFR 与 Broker 共用一个 SQLite

CFR/Broker 直接读写 Arbitrator SQLite

CFR 与 Broker 默认共用一个 CODEX_HOME

两个系统互传 native Codex thread ID

CFR 直接修改 Broker DB

Broker 直接修改 CFR DB

CFR Host Codex 绕过 Broker 写 target workspace

Broker external_host silent fallback 到 internal Codex

bounded Codex subagent 成为 Global Semantic Authority

同一真实 workspace 同时由 CFR 和 Broker 写

Router 自己做 semantic decision

Router 从 provider/model identity 推导 authority

模型自声明 lease / principal / execution authority

Route Version 与 Task Version 混用

CFR 自己 increment Broker Authority Epoch

仅凭 lease TTL 到期覆盖状态未知的旧 writer

handoff target 在 route commit 前开始执行

同一个 idempotency key 用于两个不同 canonical mutation payload

同一 event cursor 跨 generation/global_task 无 scope 复用

named-pipe 仅凭同用户 SID 视为 component authentication
```

---

# 56. 最终执行规则

最终实现必须能机械回答以下问题。

### 请求由谁执行？

看：

```text
RouteBinding.route_kind
```

### Router 为什么选择这条路径？

看：

```text
model_route_policy_id
reason_code
```

不得看 chain-of-thought。

### 语义由谁负责？

Broker route：

```text
GlobalTask.semantic_authority_origin
GlobalTask.authority_epoch
```

Direct route：

```text
RouteBinding.route_kind = direct_codex
```

### 谁能写 workspace？

看：

```text
Workspace Arbitrator lease
fencing_token
```

### conversation continuation 去哪里？

看：

```text
RouteBinding
route_generation
target.logical_id
```

### Broker task 是否还能接受 mutation？

看：

```text
task_version
authority_epoch
activation_state
```

### 原生 Codex thread 属于谁？

看：

```text
subsystem ownership
```

不得从 provider/model identity 推断。

### 系统 crash 后怎么办？

看：

```text
durable RouteBinding
HandoffRecord
Workspace Arbitrator lease state
event cursor
task.inspect
reconciliation rules
```

不得重新猜。

---

# 57. Final Constitution

```text
CFR owns ingress.

Router owns routing and model-route policy.

Router chooses WHERE and HOW, never WHAT is semantically correct.

Semantic Authority owns meaning.

Broker owns Broker-managed execution.

Broker owns canonical task authority epoch.

Workspace Arbitrator owns cross-system writer exclusion.

Native thread ownership never crosses subsystem boundaries.

State stores never become shared mutable databases.

Route Version and Task Version are separate CAS domains.

Retries are idempotent.

Authority changes are epoch-fenced.

Workspace changes are lease/fencing-token controlled.

Handoff targets remain dormant until route commit.

Event cursors are generation/task scoped.

Crash recovery follows durable state, never model guesses.

Model/provider availability never silently changes authority.
```

最终最短版本：

```text
ONE REQUEST → ONE ROUTE.

ONE GLOBAL TASK → ONE SEMANTIC AUTHORITY.

ONE WORKSPACE → ONE CROSS-SYSTEM WRITER.

ONE NATIVE THREAD → ONE SUBSYSTEM OWNER.

ONE SUBSYSTEM → ITS OWN STATE STORE.

ONE MUTATION → ONE IDEMPOTENCY IDENTITY.

ONE AUTHORITY HANDOFF → ONE NEW AUTHORITY EPOCH.

ONE WORKSPACE REACQUIRE → ONE NEW FENCING TOKEN.

ROUTER CHOOSES WHERE/HOW, NEVER WHAT.

HANDOFF TARGET STAYS DORMANT UNTIL COMMIT.
```

---

# 58. v1.2 相对 v1.1 的关键变化

1. `expected_version` 拆分为：
   - CFR `expected_route_version`。
   - Broker `expected_task_version`。

2. Broker 成为 Broker-managed GlobalTask `authority_epoch` 的 canonical owner；CFR 只 cache observed epoch。

3. Workspace Arbitrator 从模糊职责升级为独立 local authority boundary，并定义 `workspace-arbitrator/1`。

4. Handoff 增加：
   - `source_lease_released`。
   - `target_prepared`。
   - `target_lease_acquired`。
   - deferred task activation。

5. target 在 RouteBinding commit 前必须保持 dormant。

6. event cursor 改为：

```text
(route_id, generation, global_task_id, event_seq)
```

7. Idempotency 增加 canonical payload fingerprint + atomic in-progress claim。

8. named pipe 明确：
   - SID = user authorization。
   - local capability = component authentication。

9. Route `status` 与 `availability` 分离。

10. 增加 Repository Lease API，补齐 worktree shared-metadata coordination。

11. Wire 增加 length-prefixed framing、message size、in-flight 与 replay backpressure。

12. 正式加入 Model Route Policy，同时冻结：

```text
Router chooses WHERE/HOW.
Semantic Authority decides WHAT.
```

13. 明确 provider/model outage 不得 silent authority handoff。

14. Conformance 从 26 项扩展到 37 项，并增加 version domain、concurrent idempotency、handoff crash、event generation、transport framing、component impersonation、model-route safety 等测试。

---

# 59. 当前实现状态解释规则

本协议是 normative target，不代表 CFR/Broker 当前已经实现全部能力。

任何状态文档 MUST 将以下两个层级分开：

```text
A. subsystem core/live readiness

B. CFR ↔ Broker coexistence/integration readiness
```

例如：

```text
Broker T6D core live PASS
```

不能自动推出：

```text
BROKER_CODEX_HOME coexistence PASS
Workspace Arbitrator PASS
Router PASS
CFR↔Broker integration PASS
```

实现状态以最新真实 runtime / live output / actual source / current-SHA certificate 为准。

