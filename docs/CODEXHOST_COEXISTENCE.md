# CFR 与 CodexHost 共存边界

日期：2026-09-03

## 问题

CodexHost 不是普通 Codex Desktop 快捷方式。它在启动 Desktop 时注入自己的 CLI shim，当前 Windows 进程树中可观察到 `codexhost-shim.exe`。旧 CFR 的“重启并打开”会杀掉 Codex Desktop 后直接通过 `codex://` 启动 stock Desktop，因而把 CodexHost 管理态移除。

## 当前实现

CFR 保存一个独立的 Desktop launcher preference：

- `auto`：默认。运行中的 Desktop 含 `codexhost-shim.exe` 时按 CodexHost 恢复；运行中为 stock 时保持 stock；没有 Desktop 时优先使用可用的 CodexHost，否则使用 stock。
- `codexhost`：重启操作显式通过 CodexHost 恢复。
- `stock`：只有用户显式选择后，重启才允许切换为原生 Codex。

普通“打开线程”不重启 Desktop，因此不会改变当前启动来源。只有“按当前方式重启并打开”会关闭当前 Desktop；执行前仍要求 CFR thread 没有 active turn、writer idle，并由控制中心要求用户确认。

## 进程识别与清理

CFR 从 OpenAI.Codex AppX 安装目录中的 `ChatGPT.exe` 子进程开始识别 Desktop 树，只向上补全同名 `ChatGPT.exe` 根进程，再向下收集子进程。不会无界向上杀终端、DevSpace 或其他 launcher 的父进程。

检测到 CodexHost 时：

1. 记录旧 `codexhost-shim.exe` PID。
2. 终止当前 Codex Desktop 树。
3. 通过 `codexhost.exe/.cmd/.bat` 启动；不选择可能被 PowerShell ExecutionPolicy 拒绝的 `codexhost.ps1`。
4. 等待新的 shim PID 出现。
5. 再发送 `codex://threads/<thread-id>`。

## 持久化边界

CodexHost Harness 会话、原生 Codex thread 和 CFR binding 是不同层。CFR 不删除或迁移 CodexHost 数据，只保存启动偏好和自己的 thread binding。切换 Desktop launcher 不会改写 ChatGPT WebView2/Chrome 登录目录。

## 当前机器事实

本轮只读检查确认：

- 当前 Codex Desktop 为 CodexHost-managed，进程树含 `codexhost-shim.exe`。
- 可执行 launcher 为 `codexhost.cmd`，PowerShell 会拒绝同目录的 `codexhost.ps1`。
- 当前本机版本为 0.4.1。

CFR 没有自动升级 CodexHost，也没有在验证中真实重启用户当前的 Desktop。升级和真实重启都会产生外部状态变更，应单独授权和验收。
