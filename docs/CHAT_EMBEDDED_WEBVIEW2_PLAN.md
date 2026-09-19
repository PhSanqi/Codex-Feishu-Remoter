# CFR Chat 内置 WebView2 可行性与迁移方案

日期：2026-09-03

## 当前结论

可行，而且不需要再打包一套 CEF/Chromium。

当前 CFR 已实现第一版双后端与无损迁移：

- `auto`：默认。CFR Desktop 只要内置 WebView2 页面可用就优先使用它；旧 Chrome 登录态不会再触发外部浏览器弹窗。
- `embedded`：显式使用 CFR 主窗口内默认隐藏的 ChatGPT WebView2 automation page；只有需要人工登录时临时显示。
- `dedicated`：显式保留原有 CFR 专用 Chrome。

两套状态相互隔离：

```text
Legacy / retained:
%LOCALAPPDATA%\CFR\browser\chatgpt-profile
%LOCALAPPDATA%\CFR\browser\chatgpt-authenticated

Current embedded:
%LOCALAPPDATA%\CFR\webview2
%LOCALAPPDATA%\CFR\browser\embedded\chatgpt-authenticated
```

CFR 不复制或解密 Chrome Cookie，也不删除旧 Profile。这样避免破坏已有登录，也避免把浏览器密钥材料跨引擎迁移。第一次启用内置后端时，CFR 会临时显示隐藏的 ChatGPT automation page 供人工登录；登录确认后立即恢复只显示 Control Center。完成前 Chat Surface 保持等待用户登录，不会因为旧 Chrome 已登录而自动弹出外部浏览器。

CFR 当前桌面壳本身已经通过 pywebview 使用 Windows WebView2。Microsoft 官方文档明确支持：

- WebView2 使用持久化 User Data Folder（UDF）保存 cookie、权限、缓存等浏览器状态；同一 host app 的多个 WebView2 可以共享一个 UDF 和浏览器进程以降低资源消耗。
- WebView2 可以开启 `--remote-debugging-port=0` 并暴露 Chrome DevTools Protocol（CDP）。
- Microsoft 现有文档已经给出 Chrome DevTools MCP 自动连接 WebView2 的方法；连接时 `--user-data-dir` 要指向 WebView2 UDF 下的 `EBWebView`。
- `chrome-devtools-mcp` 本身支持 `--autoConnect`、`--browserUrl`、`--wsEndpoint` 和显式 `--userDataDir`。

因此目标结构可以从：

```text
CFR.exe -> Control Center WebView2
       -> external Chrome -> chrome-devtools-mcp -> ChatGPT
```

收敛为：

```text
CFR.exe
  └─ one Windows top-level window
       ├─ Control Center WebView2 page (user-visible)
       └─ ChatGPT WebView2 automation page (hidden except login)
            └─ local-only CDP
                 └─ chrome-devtools-mcp
```

ChatGPT 真实网页仍由 WebView2 承载，但正常运行时不作为用户界面显示；CFR 仅在登录需要人工介入时临时显示它。不再创建第二个 ChatGPT 顶层窗口，也不需要额外的 Chrome 应用窗口。

## 实现边界

当前 Chat Surface 已经承载：ChatGPT 登录、Project、Conversation、附件上传、History、Search、Deep Research、生成图片、生成文件和 Scheduled。直接把浏览器宿主和本轮九项故障修复一起迁移，会把“网页选择器问题”和“浏览器宿主迁移问题”混在同一验收范围内。

另外，2026 年 WebView2 runtime 曾出现过 remote-debugging-port 回归报告，所以 CFR 必须保留外部 Chrome fallback，而不能把 WebView2 CDP 当成永远稳定的隐含契约。

内置 WebView2 只提供真实网页、持久化登录态、上传/下载与本地 CDP；不增加完整浏览器地址栏、书签、扩展商店和历史管理器。外部 Chrome 仍作为显式 fallback。

## 建议实现

### 1. 一个窗口、一个可见页面 + 一个隐藏 automation page

- CFR 只创建一个 Windows 顶层窗口。
- 同一个 WinForms Form 内使用两个 WebView2 child control：`控制中心` 对用户可见，`ChatGPT` 作为隐藏 automation target。
- 两个 child control 保持同时存活；隐藏 ChatGPT target 不会结束 Chat runtime，也不会丢失页面状态。
- 产品 UI 和托盘不提供日常“打开 ChatGPT 页面”入口；只有登录流程可以临时显示 automation target。
- 不新增 Electron、CEF 或独立浏览器发行包。

### 2. CFR WebView2 持久化 UDF

当前 pywebview/WinForms 后端的 `storage_path` 是进程级配置。ChatGPT child WebView2 复用 Control Center 已创建的 `CoreWebView2Environment`，因此同一个 CFR.exe 中两个页面共享一个 WebView2 User Data Folder：

```text
%LOCALAPPDATA%\CFR\webview2
```

这是有意采用的轻量方案：两个页面共享 WebView2 browser process，而 cookie/local storage 仍按 Web Origin 隔离。ChatGPT 的登录数据属于 `chatgpt.com` Origin；CFR Control Center 是本机 loopback Origin。`%LOCALAPPDATA%\CFR\browser\embedded` 只保存 CFR 自己的登录确认/迁移状态，不保存 OpenAI 密码或 Cookie。

旧的 dedicated Chrome profile 仍完全独立，CFR 不复制 Cookie，也不会让 Chrome 和 WebView2 同时打开同一个 UDF。

### 3. CDP 只绑定 loopback

当前实现先向 Windows 申请一个临时 loopback 端口，然后在 `webview.start()` 前设置 pywebview 的 `REMOTE_DEBUGGING_PORT`。最终 WebView2/CDP listener 必须实测只出现在 `127.0.0.1`。

```text
--remote-debugging-port=<CFR 临时 loopback 端口>
```

不使用固定产品端口，也不绑定外部网卡。这里存在一个很短的“选端口后交给 WebView2”窗口，所以最终 EXE 验收必须包含重复冷启动/CDP loopback 检查；如果实际压力测试证明端口争抢成为问题，再切换到 `DevToolsActivePort`/自动端口发现，而不是预先增加第二套浏览器宿主。

### 4. MCP 连接策略

当前首选：

```text
chrome-devtools-mcp --browserUrl=http://127.0.0.1:<临时端口>
```

这样 MCP 不负责创建/持有浏览器 Profile，只连接 CFR 已经拥有的 WebView2。CFR Desktop 的 `auto` 不再静默回退到 dedicated Chrome，避免用户在正常使用中遇到外部浏览器弹窗；需要旧 Chrome 时必须显式选择 `dedicated`。

`embedded` 模式只保留一个持久、隐藏的 ChatGPT CDP target。Conversation / Project / Scheduled 导航都复用该 target；不得调用 `new_page` 创建额外 WebView2 顶层窗口。多个飞书聊天共享这一 automation target，CFR 只串行化真正的浏览器事务；最终飞书文本/文件发送不再长期占有浏览器锁。

### 5. 只保留基本浏览器能力

内置 ChatGPT 页面只需要：

- 页面渲染
- cookie / storage / 登录态
- 文件选择和下载
- 剪贴板必要权限
- ChatGPT 页面导航
- ChatGPT 原生 Thinking effort（当前网页 Power 控件，支持 Instant / Medium / High / Extra High）
- ChatGPT 原生模型菜单；`/models` 和 `/model` 动态读取当前账号网页实际暴露的模型和 disabled 状态，不复用 Codex `model/list`
- CDP

不增加书签、扩展商店、下载管理器、历史管理器、普通浏览器地址栏等完整 Chrome UI。

### 6. 登录边界

- CFR 不读取 OpenAI 密码。
- 用户首次登录时，CFR 临时显示隐藏的 ChatGPT automation page；登录完成后自动隐藏。
- 登录态由 WebView2 UDF 持久化。
- OAuth / Passkey / 企业 SSO 如果在 WebView2 中出现兼容问题，提供“在外部浏览器完成登录/继续使用外部 Chrome”的显式 fallback。

## 迁移阶段

### Phase A — Probe（已实现，重复压力验收属于最终发布门禁）

做一个不接飞书的 WebView2/CDP probe：打开 ChatGPT、登录、MCP 读取页面、上传一个本地测试文件、读取一个 Conversation。

单次 candidate probe 已覆盖 CDP、窗口显示/隐藏、旧登录 Profile 不被删除、单实例和 CodexHost 不受影响。连续 20 次启动/关闭、无 orphan、CDP 仅 loopback 属于最终发布压力门禁；只有实际跑过同一份候选 EXE 后才能把它记为已验证事实。

### Phase B — Dual backend（已实现）

给 `ChromeChatAdapter` 增加浏览器宿主接口，但保持现有业务方法不变。支持：

- `embedded_webview2`
- `dedicated_chrome`

默认使用 `auto`，在 CFR Desktop 中直接选择内置页面。旧 Chrome Profile 继续原样保留，但不会参与默认选择；用户仍可显式选择 `dedicated` 兼容模式。

### Phase C — Default embedded（实现完成，真实 E2E 待继续验证）

桌面端 `auto` 已改为内置页面优先，并取消静默外部 Chrome fallback。真实飞书 E2E 仍需要继续验证登录、上传/下载与长任务链路。

### Phase D — Optional Chrome retirement（未执行）

只有在多个 WebView2 runtime 版本和登录方式均验证稳定后，才考虑把 Chrome 从必需启动项降为可选依赖。

## 验收标准

1. CFR 只有一个 Windows 顶层窗口；Control Center 是正常可见页面，ChatGPT 是同一窗口内默认隐藏的 automation target。
2. 默认/`embedded` 路径没有额外 ChatGPT 顶层窗口，也没有额外可见 Chrome 应用窗口。
3. ChatGPT 登录跨 CFR 重启保留。
4. `/history`、Project、Conversation、上传、图片、XLSX 下载和飞书回传全部与现有行为等价。
5. Control Center 能分别展示 CFR、WebView2/browser、OpenAI waiting、download、Feishu delivery 的阶段耗时。
6. WebView2 remote debug 只监听本机。
7. 关闭 CFR 后没有 WebView2、MCP、Node orphan。
8. WebView2 连接失败时 fail-closed 并给出可诊断状态；只有用户显式选择 `dedicated` 才启动旧 Chrome。
9. 飞书连续发送 Chat 指令不会增加 WebView2 page/Windows 顶层窗口数量；`/reasoning` 可读取并修改原生 Thinking effort。
10. `/models` / `/model` 只操作当前 ChatGPT 网页的原生模型菜单；模型名称不在 CFR 中硬编码。
11. 生成文件回传优先使用文件卡自己的 `Download file` 流程。新版 ChatGPT 文件卡没有稳定 `href`/file-id 时，CFR 捕获该卡触发的当前 signed ChatGPT backend URL，再用同一登录会话执行受大小上限约束的下载；DOM 装饰类名（例如 `file-icon`）不得被当成 file id。

补充：`auto` 和显式 `embedded` 在 CFR Desktop 中都不会静默启动外部 Chrome；`dedicated` 是显式兼容选项。

## 参考

- Microsoft Learn: Manage user data folders in WebView2.
- Microsoft Learn: Let agents inspect your site and WebView2 app with Chrome DevTools MCP.
- Microsoft Learn: Debug WebView2 apps with Visual Studio Code / CDP.
- ChromeDevTools/chrome-devtools-mcp: configuration (`--autoConnect`, `--userDataDir`, `--browserUrl`, `--wsEndpoint`).
- MicrosoftEdge/WebView2Feedback: 2026 runtime 150 remote-debugging-port regression report，作为保留 fallback 的风险证据。
