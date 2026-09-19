from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    argument: str | None = None


class CommandParser:
    COMMANDS: ClassVar[set[str]] = {
        'help', 'new', 'use', 'unbind', 'status', 'stop', 'list', 'whoami', 'approve', 'deny',
        'workspace', 'workspaces', 'doctor', 'models', 'model', 'reasoning', 'effort', 'tiers', 'tier', 'approval',
        'surfaces', 'surface', 'projects', 'project', 'chats', 'chat', 'scheduled', 'search', 'deepresearch', 'image', 'upload',
        'modes', 'mode', 'steer', 'redirect', 'compact', 'history', 'unknown',
    }
    TOP_LEVEL: ClassVar[dict[str, str]] = {
        'help': 'help', 'status': 'status', 'new': 'new', 'workspace': 'workspace',
        'workspaces': 'workspaces', 'workspacelist': 'workspaces', 'sessions': 'list',
        'sessionlist': 'list', 'session': 'use', 'unbind': 'unbind', 'stop': 'stop',
        'doctor': 'doctor', 'whoami': 'whoami', 'models': 'models', 'model': 'model',
        'history': 'history',
        'reasoning': 'reasoning', 'effort': 'effort', 'tiers': 'tiers', 'tier': 'tier',
        'approval': 'approval', 'approvals': 'approval', 'permission': 'approval', 'permissions': 'approval',
        'surfaces': 'surfaces', 'surface': 'surface',
        'projects': 'projects', 'project': 'project', 'chats': 'chats', 'chat': 'chat', 'scheduled': 'scheduled', 'search': 'search', 'deepresearch': 'deepresearch', 'image': 'image', 'upload': 'upload',
        'modes': 'modes', 'mode': 'mode', 'steer': 'steer', 'redirect': 'redirect', 'compact': 'compact',
    }

    def parse(self, text: str | None) -> ParsedCommand | None:
        if not text or not text.strip().startswith('/'):
            return None
        raw = text.strip()
        if raw.startswith('/cfr') and (len(raw) == 4 or raw[4].isspace()):
            remainder = raw[4:].strip()
        else:
            pieces = raw[1:].split(maxsplit=1)
            name = self.TOP_LEVEL.get(pieces[0].lower()) if pieces and pieces[0] else None
            if not name:
                return ParsedCommand('unknown', raw)
            return ParsedCommand(name, pieces[1].strip() if len(pieces) == 2 else None)
        if not remainder:
            return ParsedCommand('help')
        pieces = remainder.split(maxsplit=1)
        name = pieces[0].lower()
        argument = pieces[1].strip() if len(pieces) == 2 else None
        return ParsedCommand(name, argument) if name in self.COMMANDS else ParsedCommand('unknown', raw)

    def is_known(self, command: ParsedCommand) -> bool:
        return command.name in self.COMMANDS


UNIVERSAL_COMMANDS = {'help', 'surfaces', 'surface', 'status', 'stop', 'whoami', 'history'}
CHAT_COMMANDS = UNIVERSAL_COMMANDS | {
    'new', 'projects', 'project', 'chats', 'chat', 'scheduled', 'search', 'deepresearch', 'image', 'upload',
    'models', 'model', 'reasoning', 'effort',
}
CODE_COMMANDS = UNIVERSAL_COMMANDS | {
    'new', 'workspace', 'workspaces', 'list', 'use', 'unbind', 'doctor', 'models', 'model', 'reasoning', 'effort',
    'tiers', 'tier', 'approval', 'modes', 'mode', 'steer', 'redirect', 'compact', 'approve', 'deny', 'upload',
}


CHAT_HELP_TEXT = '''CFR · Chat Surface 指令
普通文本：发送到当前绑定的真实 ChatGPT 对话；不会进入 Codex。

【Surface 控制】
/help：只显示 Chat Surface 可用指令。
/surface：查看当前 Surface 和 Chat / Work / Code 可用状态。
/surface code：切回 Code Surface；保留当前 ChatGPT Project / Conversation 绑定。
/status：查看 CFR Chat 浏览器、当前 WebView2 页面、Project / Conversation URL。
/history [数量]：读取当前原生 ChatGPT 对话最近的可见消息，默认 10 条，最多 50 条。
/stop：停止普通 ChatGPT 回复或图片生成；Deep Research 暂无已验证的网页停止控件。
/whoami：显示当前飞书发送者 open_id。

【Project / Conversation（项目 / 对话）】
/projects：列出当前 ChatGPT 账号可见 Projects；只读取，不切换。
/project <编号|名称|Project ID|URL>：打开指定 Project；/projects 列表最后一个“普通 Chat”编号用于退出 Project。
/chats：列出当前 Project 页面可见对话，返回编号和 Conversation ID 后缀。
/chat <编号|Conversation ID|URL>：打开指定原生对话；后续普通文本继续该上下文。
/new [首条消息]：在当前 Project 中打开新的原生对话；若当前不在 Project，则打开普通新聊天。带消息时会新建后立即发送。

推荐浏览顺序：/projects（可选）→ /project <编号>（可选）→ /chats → /chat <编号> → /history。
/projects 与 /project 只决定当前浏览范围；不是 /chats 或 /history 的前置条件。
当前已经打开具体对话时，/history 可直接读取；若还停留在首页或 Project 首页，/history 会提示先用 /chats 和 /chat 选择对话。
/chats、/projects、/models 等列表返回的编号均从 1 开始，后续对应命令可直接使用该编号。

【对话内原生工具】
/models：每次从当前 ChatGPT 网页模型菜单读取实时可选模型；不会复用或硬编码 Codex 模型目录，网页新增模型后会随原生菜单自动出现。
/model：查看当前 ChatGPT 模型和网页可选列表。
/model <编号|模型名>：通过 ChatGPT 原生模型菜单选择模型，并在网页上重新校验选中状态。
/reasoning 或 /effort：查看当前 ChatGPT Thinking effort 档位。
/reasoning <编号|档位>：通过 ChatGPT 原生 Power 控件调整当前 Thinking effort；编号以当前网页实际档位数为准，不会修改 Codex 设置。
/search <问题>：显式选择 ChatGPT“网页搜索”后发送；不会依赖模型自动决定是否搜索。
/deepresearch <问题>：启动原生 Deep Research，立即返回“运行中”，不阻塞飞书任务队列。
/deepresearch status：读取当前对话最近一次 Deep Research 状态；完成后返回原生报告文本。
/image <提示词>：启动原生图片生成，立即返回“运行中”。
/image status：读取最近一次图片生成状态；完成后返回图片说明、尺寸和当前会话临时 URL。
/upload <绝对文件路径>：把一个本地文件附加到当前对话；路径必须位于 CFR 允许的工作区内。只附加，不自动发送额外提示词。
直接发送飞书图片/文件：CFR 先保存为待发送附件；下一条普通文本会先把附件上传到当前 ChatGPT 对话，再发送文本。

【Scheduled 定时任务】
/scheduled：打开独立的原生 Scheduled 管理页；不会覆盖当前对话绑定。
/scheduled list：列出 Scheduled 定时任务、原生 task ID、运行状态和调度摘要。
/scheduled create <自然语言任务>：通过原生 Scheduled composer 创建任务。
/scheduled pause <编号|task ID>：暂停指定任务。
/scheduled resume <编号|task ID>：恢复指定任务。
/scheduled edit <编号|task ID> title=<新标题>：修改任务标题。
/scheduled delete <编号|task ID>：删除指定任务。
任务说明、频率、时间等编辑字段尚未完成真实写入验收，因此当前不开放对应指令。

Chat Surface 不接受 /mode、/workspace、/steer、/redirect 等 Code 指令；跨 Surface 指令会直接拒绝。'''


CODE_HELP_TEXT = '''CFR · Code Surface 指令
普通文本：发送到当前绑定的真实 Codex thread；不会进入 ChatGPT 网页。
进度卡片只显示最新状态、思考摘要和输出预览；最终回复提供完整交付。

【Surface 控制】
/help：只显示 Code Surface 可用指令。
/surface：查看当前 Surface 和 Chat / Work / Code 可用状态。
/surface chat：切到真实 ChatGPT Chat Surface；保留当前 Codex thread 绑定。
/status：查看当前 CFR/Codex 会话、thread 和活动 Turn。
/history [数量]：从当前原生 Codex rollout 的文件尾读取最近对话，默认 10 条，最多 50 条；不会随历史增长而全文件扫描。
/stop：停止当前 Codex Turn，不自动继续。
/whoami：显示当前飞书发送者 open_id。

【Session / Workspace（会话 / 工作区）】
/new [绝对路径]：准备新的 CFR/Codex 会话；省略路径时复用当前工作区，提供路径时在该工作区下新建；下一条普通任务才创建原生 Codex thread。
/sessions：列出现有 CFR/Codex 会话。
/session <编号|thread ID/后缀>：把当前飞书 chat 绑定到已有 thread；列表展示的 8 位 thread 后缀也可直接使用。若已绑定其他 thread 需先 /unbind。
/unbind：解除当前飞书聊天与 Codex thread 的绑定，不删除 thread；重新绑定使用 /sessions → /session <编号>。
/workspace：查看当前工作区，并按编号列出允许的已有工作区。
/workspace <编号|名称|绝对路径>：选择已有工作区或指定允许路径，并准备一个新的 Code thread；无需重复输入完整路径。
/workspaces：列出允许的工作区根目录。
/upload <绝对文件路径>：把本地文件加入下一条 Codex 消息；图片使用原生 localImage，其他文件会复制/保留在当前工作区并把路径交给 Codex。路径必须位于 CFR 允许的工作区内。
直接发送飞书图片/文件：CFR 先保存为待发送附件；下一条普通任务会和附件一起进入同一个原生 Codex Turn。

推荐的新线程顺序：/workspace → /workspace <编号> → /models → /model <编号> → /reasoning <档位> → 发送第一条普通任务。
在待创建会话中，/model、/reasoning、/tier 会作为 thread/start 预设一次性应用，不需要先创建 thread 再修改。

【模型 / 推理 / 服务层级】
/models：列出当前 Codex 模型目录。
/model：查看当前 thread；若还未创建原生 thread，则查看新线程预设模型。
/model <编号|模型ID|模型名>：待创建会话中预设新 thread 模型；已有 thread 中执行原生热切换。
/model default <编号|模型ID|模型名>：修改以后新 thread 默认模型，不改变当前 thread。
/reasoning 或 /effort：查看当前/待创建 thread 的推理档位和模型支持档位。
/reasoning <编号|档位>：待创建会话中预设新 thread 推理档位；已有 thread 中修改当前档位。
/reasoning default <编号|档位>：修改新 thread 默认推理档位。
/tiers：查看当前模型支持的服务层级。
/tier <编号|层级>：修改当前 thread 服务层级；/tier default 恢复模型/运行时默认。
/approval：查看当前 Code 审批模式和三个 Codex 原生映射。
/approval <1|2|3|ask|auto|full>：选择“全部请求 / 替我审批 / 全部开放权限”；设置从下一次 Code turn 起生效。
已开始使用的 thread 上再切换模型可能降低后续上下文缓存复用；需要新模型时优先新建 session 并在首条任务前完成预设。

【Codex 执行控制】
/modes 或 /mode：查看 Codex collaboration mode；这不是 Chat / Code Surface 切换。
/mode <编号|模式名>：切换 Codex collaboration mode，例如 default / plan。
/steer <追加指令>：软转向；把指令追加到同一 Turn，不创建新 Turn，也不保证立即中断当前输出。
/redirect <新方向>：请求停止当前 Turn，并在同一 Thread 排队创建新 Turn 按新方向执行。
/approve <approval ID 前缀>：对唯一匹配的待审批请求执行 approve once。
/deny <approval ID 前缀>：拒绝唯一匹配的待审批请求。
/doctor：运行 CFR/Codex 安全与运行诊断。
/compact：当前未实现安全的原生会话压缩，仅返回未实现状态。

Code Surface 不接受 /project、/chat、/search、/deepresearch、/image、/scheduled 等 Chat 指令；跨 Surface 指令会直接拒绝。'''


def help_text(surface: str) -> str:
    return CHAT_HELP_TEXT if surface == 'chat' else CODE_HELP_TEXT


# Backward-compatible import for older callers/tests; runtime /help is surface-aware.
HELP_TEXT = CODE_HELP_TEXT
