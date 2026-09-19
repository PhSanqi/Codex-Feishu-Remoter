# CFR — Codex Feishu Remoter

> Local-first remote control for Codex and ChatGPT through Feishu, with a local Control Center for setup, runtime status, permissions, and browser/session management.

> 通过飞书远程使用 Codex / ChatGPT，并用本机 Control Center 管理配置、运行状态、权限和浏览器会话。

[中文](#中文) · [English](#english)

> **Project status / 项目状态**
>
> CFR is under active development. The repository contains the current Windows and Linux source versions, but the project is not yet considered release-ready. **No release package is published for this update.**
>
> CFR 仍在持续开发中。仓库会同步维护 Windows 与 Linux 当前源码，但目前还不视为正式发行版本，**本轮不发布 Release 安装包**。

---

## 中文

### CFR 是什么

CFR（Codex Feishu Remoter）把本机的 Codex / ChatGPT 能力接到飞书，让你可以在手机或另一台电脑上发送任务、查看进度、审批操作和接收结果，同时仍由自己的电脑保存项目文件和运行状态。

它适合这些场景：

- 在飞书里远程让 Codex 处理本机代码仓库；
- 不把项目文件上传到额外的远程执行服务器；
- 在多个工作区之间切换，并限制机器人只能访问允许的目录；
- 在 Code 与 Chat 两种执行方式之间切换；
- 用网页 Control Center 完成首次配置、账号绑定、诊断和运行状态查看；
- Windows 与 Linux 使用同一套 CFR 核心能力，同时保留各平台更合适的本地运行方式。

### 主要功能

#### 1. 飞书远程控制

- 使用你自己的飞书企业自建应用（BYO Feishu App）；
- 支持私聊、群聊 / 话题等消息入口；
- 首次使用通过六位配对码绑定本机操作者；
- 工作区白名单限制 CFR 可操作的本地目录；
- 长连接运行，不要求把 CFR Control Center 暴露到公网；
- 支持进度、结果、审批和错误反馈；
- 支持文本、附件和生成结果的回传流程。

#### 2. Code Surface

Code Surface 使用真实 Codex 运行任务，支持：

- 新建 / 恢复原生 Codex Thread；
- 模型、Reasoning、Service Tier 与协作模式；
- `/stop`、`/steer`、`/redirect` 等运行控制；
- 权限审批；
- 原生历史读取；
- 本地文件、工具调用与生成文件回传；
- Codex 兼容性检查与 live doctor。

#### 3. Chat Surface

Chat Surface 使用真实 ChatGPT 网页能力，不用 Codex 模拟普通 Chat。

- 保留普通 ChatGPT 的网页会话与模型能力；
- 与 Code Surface 分开使用，不把普通 Chat 请求改走 Codex CLI；
- Linux 当前使用 `chrome-use + Chrome Extension + Native Messaging`；
- Linux 固定使用 `session=cfr-chat`，只操作 CFR 自己创建的浏览器标签 / 窗口；
- CFR 会在确认窗口只属于 CFR 后将其保持为非聚焦、最小化状态，避免打扰用户正在使用的 Chrome；
- 不需要 Remote Debugging，也不需要第二套 ChatGPT 登录 Profile。

> Work Surface 目前仍未接入，CFR 不会用 Codex 冒充 Work。

#### 4. 本地 Control Center

Control Center 默认只监听本机 loopback 地址，用于：

- Codex 登录与兼容性诊断；
- 飞书 App ID / Secret 配置；
- 飞书账号配对；
- 工作区白名单管理；
- Code / Chat Surface 状态；
- 网络 / 代理策略；
- 运行日志、任务和审批状态；
- 浏览器连接状态与 Chat 后端健康检查。

### 安装前准备

通用要求：

- Python 3.11+；
- Node.js / npm（用于 Control Center 构建及本地工具）；
- 已安装并登录 Codex；
- 一个你自己创建的飞书企业自建应用；
- Git。

如果要使用 Chat Surface，还需要可正常登录 ChatGPT 的 Chrome。

### Windows 使用

当前 Windows 版本以源码运行和本地桌面 / Control Center 为主。

```powershell
git clone https://github.com/PhSanqi/Codex-Feishu-Remoter.git
cd Codex-Feishu-Remoter

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e ".[feishu,desktop]"

cd m3_control
npm install
npm run build
cd ..
```

启动：

```powershell
.\START_CFR.cmd
```

也可以直接运行：

```powershell
.\.venv\Scripts\python.exe scripts\run_cfr_control.py --open-browser
```

首次启动后，在 Control Center 中完成 Codex、飞书、工作区和 Chat 的配置。

### Linux 使用

Linux 提供 bootstrap、统一启动脚本和 systemd user service。

```bash
git clone https://github.com/PhSanqi/Codex-Feishu-Remoter.git
cd Codex-Feishu-Remoter

bash scripts/bootstrap_linux.sh
./START_CFR.sh
```

`bootstrap_linux.sh` 会准备 Python 环境、本地 Codex 运行依赖、Control Center，以及 Linux Chat 所需的 `chrome-use` Native Messaging host。

Linux Chat 还需要安装一次 Chrome 扩展：

- [chrome-use — Chrome Web Store](https://chromewebstore.google.com/detail/chrome-use/knfcmbamhjmaonkfnjhldjedeobeafmk)

安装扩展后不需要开启 Chrome Remote Debugging。

如需后台常驻：

```bash
bash scripts/install_systemd_user.sh
systemctl --user status cfr.service
```

### 飞书首次配置

1. 在飞书开放平台创建“企业自建应用”；
2. 开启机器人能力，并按你的组织要求配置消息权限；
3. 获取 App ID 和 App Secret；
4. 在 CFR Control Center 填入飞书凭据；
5. 点击“开始飞书账号配对”；
6. 在飞书里私聊机器人发送页面显示的：

```text
绑定 123456
```

7. 回到 Control Center 确认账号，并选择允许的本地工作区；
8. 启动飞书运行时。

App Secret 不应提交到 Git。CFR 会优先使用本机安全凭据存储。

### 飞书常用命令

不同 Surface 可用命令略有差异，常用入口包括：

```text
/help
/status
/surface code
/surface chat
/history 10
/stop
/steer <追加指令>
/redirect <新方向>
```

Code Surface 还支持原生 Codex 控制，例如：

```text
/model
/reasoning
/tier
/mode
```

普通文本消息会作为当前 Surface 的任务发送。

### 诊断与测试

Codex / CFR：

```bash
.venv/bin/python scripts/run_cfr.py doctor --json
.venv/bin/python scripts/run_cfr.py doctor --live --json
```

Windows 可将 `.venv/bin/python` 替换为 `.venv\Scripts\python.exe`。

Linux 完整 smoke：

```bash
./scripts/linux_smoke.sh
```

单元测试：

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/unit -p 'test*.py'
```

### 安全边界

- Control Center 默认只绑定本机地址，不建议直接暴露公网；
- 飞书 App 由使用者自己创建和持有；
- App Secret 不应写入仓库；
- 本地工作区必须显式允许；
- CFR 不会自动修改你的系统网络 / 全局代理设置；
- Linux Chat 的自动化被限制到 `cfr-chat` 自有 session，发现混入普通用户标签时会拒绝继续控制；
- 迁移到新电脑时应重新登录 Codex、重新确认飞书凭据和本机工作区，不建议直接复制整个运行时状态目录。

### 当前未完成内容

CFR 仍在开发中，以下内容不应视为稳定发行承诺：

- Work Surface 尚未接入；
- Windows / Linux 的安装与升级体验仍在继续整理；
- ChatGPT 网页 UI 更新可能要求继续维护浏览器适配；
- Release 安装包与正式版本号策略尚未冻结。

因此当前更适合作为源码项目使用，而不是当作已经稳定发布的桌面产品。

### License

MIT License，见 [`LICENSE`](LICENSE)。

---

## English

### What is CFR?

CFR (Codex Feishu Remoter) connects local Codex / ChatGPT capabilities to Feishu. You can send tasks from another computer or phone, monitor progress, approve actions, and receive results while your project files and runtime stay on your own machine.

Typical use cases:

- remotely run Codex against local repositories from Feishu;
- keep source files on the execution machine instead of uploading them to an extra remote executor;
- restrict automation to explicitly allowed workspace roots;
- switch between Code and Chat execution surfaces;
- configure and monitor the runtime from a local web Control Center;
- use the same CFR feature set on Windows and Linux with platform-appropriate local integrations.

### Main features

#### Feishu remote control

- Bring Your Own Feishu App;
- private/group/topic message handling;
- six-digit local operator pairing;
- local workspace allowlist;
- persistent outbound Feishu connection — no public CFR webhook is required;
- progress, result, approval, and error feedback;
- attachment and generated-artifact delivery flows.

#### Code Surface

Code Surface runs real Codex and supports native threads, models, reasoning settings, service tiers, collaboration modes, approvals, history, stop/steer/redirect controls, local tools/files, generated artifacts, and compatibility diagnostics.

#### Chat Surface

Chat Surface uses real ChatGPT web sessions instead of pretending Codex is ordinary ChatGPT Chat.

On Linux, the current backend uses `chrome-use + Chrome Extension + Native Messaging` with a dedicated `cfr-chat` session. CFR operates only its own browser tabs/window and keeps the isolated CFR window unfocused and minimized when it is safe to do so. Remote Debugging and a second ChatGPT profile are not required.

> Work Surface is not connected yet and is intentionally not emulated with Codex.

#### Local Control Center

The loopback-only Control Center provides setup and runtime management for Codex login/compatibility, Feishu credentials and pairing, workspace roots, Code/Chat status, proxy policy, tasks, approvals, logs, and browser health.

### Requirements

- Python 3.11+;
- Node.js / npm;
- Codex installed and signed in;
- your own Feishu custom app;
- Git;
- Chrome with a valid ChatGPT login if you want Chat Surface.

### Windows

```powershell
git clone https://github.com/PhSanqi/Codex-Feishu-Remoter.git
cd Codex-Feishu-Remoter

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e ".[feishu,desktop]"

cd m3_control
npm install
npm run build
cd ..

.\START_CFR.cmd
```

Then finish the first-run setup in the Control Center.

### Linux

```bash
git clone https://github.com/PhSanqi/Codex-Feishu-Remoter.git
cd Codex-Feishu-Remoter

bash scripts/bootstrap_linux.sh
./START_CFR.sh
```

For Chat Surface, install the one-time Chrome extension:

- [chrome-use — Chrome Web Store](https://chromewebstore.google.com/detail/chrome-use/knfcmbamhjmaonkfnjhldjedeobeafmk)

Remote Debugging is not required.

Optional persistent user service:

```bash
bash scripts/install_systemd_user.sh
systemctl --user status cfr.service
```

### First Feishu setup

1. Create a Feishu custom enterprise app and enable its bot capability.
2. Obtain the App ID and App Secret.
3. Enter them in the CFR Control Center.
4. Start local Feishu account pairing.
5. Send the displayed `绑定 <six-digit-code>` message to the bot in a private Feishu chat.
6. Confirm the detected account and allowed local workspace.
7. Start the Feishu runtime.

Do not commit the App Secret. CFR prefers the operating-system secure credential store.

### Common Feishu commands

```text
/help
/status
/surface code
/surface chat
/history 10
/stop
/steer <additional instruction>
/redirect <new direction>
```

Code Surface also exposes native Codex controls such as `/model`, `/reasoning`, `/tier`, and `/mode`.

Normal text is sent as a task to the currently selected Surface.

### Diagnostics

```bash
.venv/bin/python scripts/run_cfr.py doctor --json
.venv/bin/python scripts/run_cfr.py doctor --live --json
./scripts/linux_smoke.sh
```

On Windows, use `.venv\Scripts\python.exe` instead of `.venv/bin/python`.

### Security notes

- The Control Center is intended to stay on loopback/local access.
- You own the Feishu app and credentials.
- App Secrets must not be committed to source control.
- Workspace roots are explicitly allowlisted.
- CFR does not rewrite your host/global network proxy configuration.
- Linux browser automation is scoped to the CFR-owned `cfr-chat` session and fails closed if user tabs appear inside the CFR-owned window.

### Current limitations

CFR is still under active development. Work Surface is not connected, installation/upgrade UX is still evolving, ChatGPT UI changes can require browser-adapter updates, and the release/versioning process is not frozen yet. For now, treat the repository as a source-first project rather than a finished packaged product.

### License

MIT License. See [`LICENSE`](LICENSE).
