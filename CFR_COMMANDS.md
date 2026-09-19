# CFR Linux 服务器操作命令

本文档适用于 Linux 上的 CFR 源码部署。以下示例假设仓库位于：

```bash
~/codex-workspace/CFR
```

当前约定：

- CFR Control Center：`127.0.0.1:18787`
- DevSpace Control：`127.0.0.1:8787`，不要占用或停止它
- Python 环境：`./.venv`
- 本地 Codex：`./.local-tools/node_modules/.bin/codex`
- CFR systemd 用户服务：`cfr.service`
- 示例允许工作区：`~/codex-workspace`

---

## 1. 每次开始操作先进入 CFR 根目录

```bash
cd ~/codex-workspace/CFR
```

建议后续所有命令都从这个目录执行。

---

## 2. 查看 CFR 是否正在运行

```bash
systemctl --user status cfr.service
```

只看简短状态：

```bash
systemctl --user is-active cfr.service
systemctl --user is-enabled cfr.service
```

正常结果应为：

```text
active
enabled
```

确认 CFR 正在监听 18787：

```bash
ss -ltnp | grep 18787
```

---

## 3. 启动、停止、重启 CFR

启动：

```bash
systemctl --user start cfr.service
```

停止：

```bash
systemctl --user stop cfr.service
```

重启：

```bash
systemctl --user restart cfr.service
```

查看最近日志：

```bash
journalctl --user -u cfr.service -n 100 --no-pager
```

实时看日志：

```bash
journalctl --user -u cfr.service -f
```

按 `Ctrl+C` 退出实时日志，不会停止 CFR。

---

## 4. 打开 Control Center 网页配置界面

Control Center 使用一次性 bootstrap URL 建立本地安全会话。

后台 systemd 模式使用 `--quiet`，不会把这个 URL 输出给你。因此要进入网页配置界面时，推荐临时以前台模式启动。

先停止后台服务：

```bash
systemctl --user stop cfr.service
```

然后以前台方式启动：

```bash
./START_CFR.sh
```

终端会输出类似：

```text
{"status":"started","url":"http://127.0.0.1:18787/?bootstrap=......"}
```

把完整的 `http://127.0.0.1:18787/?bootstrap=...` 地址复制到服务器上的浏览器打开。

如果你是通过 SSH 登录服务器、浏览器实际在另一台电脑上，则在那台电脑另外开一个终端：

```bash
ssh -L 18787:127.0.0.1:18787 z@服务器地址
```

然后在那台电脑浏览器打开终端打印的 bootstrap URL；把其中主机保持为 `127.0.0.1:18787` 即可。

前台 CFR 运行期间不要关闭这个终端。

配置完成后按：

```text
Ctrl+C
```

然后恢复后台常驻：

```bash
systemctl --user start cfr.service
```

---

## 5. 第一次配置 Feishu

先检查凭据状态：

```bash
./.venv/bin/python scripts/run_cfr.py feishu credentials status
```

设置 App ID：

```bash
./.venv/bin/python scripts/run_cfr.py feishu credentials set-app-id 'cli_xxxxxxxxxxxxx'
```

设置 App Secret：

```bash
./.venv/bin/python scripts/run_cfr.py feishu credentials set-secret
```

执行后会交互式要求输入 Secret。不要把 App Secret 直接写进命令、源码、聊天记录或日志。

再次确认状态：

```bash
./.venv/bin/python scripts/run_cfr.py feishu credentials status
```

然后按照第 4 节打开 Control Center，在配置页面执行：

1. 检查 Feishu App 凭据；
2. 点击“绑定我的飞书账号”；
3. CFR 会显示一个六位绑定码；
4. 用自己的飞书账号私聊 CFR Bot，发送 `绑定 六位码`；
5. 回到 Control Center 确认账号；
6. 检查允许工作区为你实际要开放的本机目录，例如 `~/codex-workspace`；
7. 启动飞书运行时。

Feishu 非 live 检查：

```bash
./.venv/bin/python scripts/run_cfr.py feishu doctor --json
```

只测试 Feishu Channel 连接、查看自己的 open_id/chat_id 时：

```bash
./.venv/bin/python scripts/run_cfr.py feishu run --setup-only --show-identifiers
```

按 `Ctrl+C` 结束 setup-only 模式。

正常运行时不要同时手工执行第二个 `feishu run`，避免两个进程竞争同一个 Feishu 连接。

---

## 6. Codex 检查与真实模型测试

查看 CFR 使用的 Codex：

```bash
source scripts/cfr_env.sh
"$CFR_CODEX_BIN" --version
```

当前安装应显示类似：

```text
codex-cli 0.154.0
```

普通 Doctor，不发送模型请求：

```bash
./.venv/bin/python scripts/run_cfr.py doctor --json
```

真实 live Doctor，会实际完成一次 Codex 模型回合：

```bash
./.venv/bin/python scripts/run_cfr.py doctor --live --json --gate-origin host_manual
```

重点检查：

```text
Verdict: PASS
CodexInterfaceCompatibility: PASS
LoginStatus: LOGGED_IN
LiveModelProbe.Status: PASS
RuntimeLeaseHealth: PASS
```

查看当前 Codex 动态模型目录：

```bash
PYTHONPATH=src ./.venv/bin/python - <<'PY'
from cfr.control.codex_catalog import model_catalog
r = model_catalog()
print('available =', r.get('available'))
for i, model in enumerate(r.get('data') or [], 1):
    print(i, model.get('model'), '-', model.get('display_name'))
PY
```

当前服务器已经验证可看到包括 `gpt-6-astra` 在内的运行时模型列表。

---

## 7. 不通过 Feishu，直接从终端使用 Codex Surface

列出 CFR 已知 Codex 会话：

```bash
./.venv/bin/python scripts/run_cfr.py --db cfr.sqlite3 codex list
```

创建一个新会话并执行第一条任务：

```bash
./.venv/bin/python scripts/run_cfr.py --db cfr.sqlite3 codex new \
  --cwd ~/codex-workspace/你的项目目录 \
  --name test-session \
  --message '检查这个项目并告诉我当前状态'
```

命令输出会包含 `thread_id`。之后继续发送消息：

```bash
./.venv/bin/python scripts/run_cfr.py --db cfr.sqlite3 codex send THREAD_ID '继续检查剩余问题'
```

查看线程状态：

```bash
./.venv/bin/python scripts/run_cfr.py --db cfr.sqlite3 codex status THREAD_ID
```

查看线程输出变化：

```bash
./.venv/bin/python scripts/run_cfr.py --db cfr.sqlite3 codex watch THREAD_ID
```

只看一次：

```bash
./.venv/bin/python scripts/run_cfr.py --db cfr.sqlite3 codex watch THREAD_ID --once
```

注意：独立 CLI 的 `codex stop` 只能中断由当前同一进程持有的活动 Turn；后台 Feishu/CFR 长任务应优先使用 Feishu `/stop`。

---

## 8. Feishu 中常用命令

完成绑定后，在与 CFR Bot 的聊天中使用：

```text
/help
/status
/workspaces
/workspace
/sessions
/session <编号或 thread ID>
/new
/unbind
/surfaces
/surface code
/surface chat
/models
/model
/model <编号>
/reasoning
/reasoning <编号或档位>
/tiers
/tier <编号或名称>
/modes
/mode <编号或名称>
/history 10
/steer <追加指令>
/redirect <新方向>
/stop
```

普通文本不是控制命令，会作为任务发给当前 Surface。

产品 Surface：

- `Code`：真实 Codex，当前可用；
- `Chat`：真实 ChatGPT 网页；Linux 通过 `chrome-use` 扩展 + Native Messaging 控制 CFR 自己的后台 tab group；
- `Work`：当前未接入，不会用 Codex 假装实现。

---

## 9. Chat Surface 登录

Linux 只使用你当前普通 Chrome 的登录态，并通过 `chrome-use` 扩展 + Native Messaging
控制 `session=cfr-chat` 自己创建的 tab group。CFR 不要求 Remote Debugging，也不创建第二套
Chrome Profile。

第一次使用时只需：

1. 在普通 Chrome 登录 `https://chatgpt.com/`；
2. 安装 chrome-use 扩展（扩展 ID：`knfcmbamhjmaonkfnjhldjedeobeafmk`）；
3. 运行 `scripts/install_chrome_use.sh`（bootstrap 会自动执行），注册 Native Messaging host；
4. 在 Control Center 点击“连接 / 检查后台 Chat”。

登录成功后，可以在 Feishu 中执行：

```text
/surface chat
```

回到 Code：

```text
/surface code
```

如果服务器没有图形桌面，需要先通过你现有的远程桌面/VNC/Web GUI 环境看到 Chrome 登录窗口；不要把 CFR Control 端口直接暴露到公网。

---

## 10. 完整测试

最推荐：

```bash
./scripts/linux_smoke.sh
```

当前服务器基线结果为：

```text
Ran 757 tests
OK (skipped=2)
Linux smoke: PASS
```

只跑完整单元测试：

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest discover -s tests/unit -p 'test*.py'
```

编译检查：

```bash
./.venv/bin/python -m compileall -q src scripts tests
```

Git whitespace 检查：

```bash
git diff --check
```

重新构建 Control Center：

```bash
cd m3_control
npm run build
cd ..
```

---

## 11. 重新安装/更新本机依赖

通常不需要重复执行。只有 `.venv`、Codex 本地 runtime 或前端依赖损坏/更新后才运行：

```bash
bash scripts/bootstrap_linux.sh
```

它会：

- 安装/更新 CFR Python 依赖；
- 安装项目本地 Codex；
- 安装匹配 Linux 架构的 Codex native payload；
- 安装固定版本 `chrome-use` CLI，并注册 Native Messaging host；
- 构建 Control Center。

重新安装 systemd 用户服务：

```bash
bash scripts/install_systemd_user.sh
```

---

## 12. 常见问题

### 12.1 18787 端口没有监听

```bash
systemctl --user restart cfr.service
systemctl --user status cfr.service
journalctl --user -u cfr.service -n 100 --no-pager
```

### 12.2 浏览器显示 CONTROL_AUTH_REQUIRED

这是安全机制，不是服务坏了。

按第 4 节执行：

```bash
systemctl --user stop cfr.service
./START_CFR.sh
```

然后使用终端新打印的 `?bootstrap=...` URL。

### 12.3 8787 已被占用

正常。8787 是当前服务器 DevSpace Control 使用的端口。

CFR 使用：

```text
127.0.0.1:18787
```

不要停止 DevSpace 来抢 8787。

### 12.4 Feishu 无法启动

依次检查：

```bash
./.venv/bin/python scripts/run_cfr.py feishu credentials status
./.venv/bin/python scripts/run_cfr.py feishu doctor --json
systemctl --user status cfr.service
journalctl --user -u cfr.service -n 100 --no-pager
```

并确认已经在 Control Center 中完成账号绑定。

### 12.5 Codex 出问题

```bash
source scripts/cfr_env.sh
"$CFR_CODEX_BIN" --version
./.venv/bin/python scripts/run_cfr.py doctor --live --json --gate-origin host_manual
```

### 12.6 查看当前配置阻塞项

```bash
PYTHONPATH=src ./.venv/bin/python - <<'PY'
from pathlib import Path
from cfr.control import CfrSupervisor
s = CfrSupervisor(project_root=Path.cwd(), database=Path('cfr.sqlite3'))
state = s.setup_state()
print('ready =', state.get('ready'))
print('blocking =', ', '.join(state.get('blocking') or []) or 'none')
for item in state.get('checks') or []:
    if not item.get('ready'):
        print('-', item.get('key'), ':', item.get('detail'))
PY
```

---

## 13. 当前服务器已验证状态

截至当前迁移版本：

```text
CFR tests:                  757 PASS, 2 skipped
Linux smoke:                PASS
Codex:                      codex-cli 0.154.0
Codex login:                LOGGED_IN / CHATGPT
Codex interface:            PASS
Live model probe:           PASS
Runtime lease:              PASS
Control Center build:       PASS
systemd cfr.service:        enabled + active
CFR Control port:           127.0.0.1:18787
DevSpace Control port:      127.0.0.1:8787
Allowed workspace root:     ~/codex-workspace
Feishu credentials:         waiting for user configuration
Feishu operator pairing:    waiting for user pairing
Chat dedicated profile:     waiting for user login
```

最终源码快照：

```text
~/codex-workspace/CFR-linux-final-<date>.tar.gz
```

---

## 14. 最短日常操作流程

查看服务：

```bash
cd ~/codex-workspace/CFR
systemctl --user status cfr.service
```

看实时日志：

```bash
journalctl --user -u cfr.service -f
```

需要进网页配置时，现在只需要一个命令：

```bash
./START_CFR.sh
```

它会自动打开 Control Center。如果 `cfr.service` 正在后台运行，脚本会临时停止后台实例；前台 CFR 退出后会自动恢复原来的 systemd 服务，不需要手工来回切换。

配置结束后：

```text
Ctrl+C
```

如果启动前后台服务本来没有运行，退出前台后也不会擅自启动它。

完整自检：

```bash
./scripts/linux_smoke.sh
```

### ChatGPT 浏览器（Linux 唯一模式）

Linux 固定使用 **当前普通 Chrome + chrome-use Native Messaging + CFR 独占 tab group**，直接复用
`~/.config/google-chrome/Default` 的登录态。Linux 不再使用 WebView2、dedicated Chrome、
Remote Debugging、`chrome-devtools-mcp` 或第二套 Profile。

1. 保持你平时使用的 Chrome 正常运行。
2. 用你平时的 Chrome Profile 人工登录 ChatGPT；已经登录过就不需要再次登录。
3. 确认 chrome-use 扩展已安装并启用。
4. 回到 CFR 点击 **连接 / 检查后台 Chat**。

`chrome-use` 固定使用 `session=cfr-chat`。该 session 只能驱动自己 `created`/`adopted` 的 tab，
CFR 还会额外过滤 `foreign` tab，因此 Gmail、GitHub、你的其他 ChatGPT 对话不会成为 CFR 目标。
CFR process 重启时保留这个 session/tab group，不会每次重新创建网页。

如果浏览器仍显示 “Chrome is being controlled by automated test software”，说明旧的 Remote
Debugging 还开着；Linux CFR 已经不再需要它，可以在 Chrome 设置里关闭。

旧 dedicated/embedded Linux 配置和旧浏览器 Profile 已从当前运行路径清理。
