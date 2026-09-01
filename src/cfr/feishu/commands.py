from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    argument: str | None = None


class CommandParser:
    COMMANDS = {
        'help', 'new', 'use', 'unbind', 'status', 'stop', 'list', 'whoami', 'approve', 'deny',
        'workspace', 'workspaces', 'doctor', 'models', 'model', 'reasoning', 'effort', 'tiers', 'tier',
        'modes', 'mode', 'steer', 'redirect', 'compact', 'unknown',
    }
    TOP_LEVEL = {
        'help': 'help', 'status': 'status', 'new': 'new', 'workspace': 'workspace',
        'workspaces': 'workspaces', 'workspacelist': 'workspaces', 'sessions': 'list',
        'sessionlist': 'list', 'session': 'use', 'unbind': 'unbind', 'stop': 'stop',
        'doctor': 'doctor', 'whoami': 'whoami', 'models': 'models', 'model': 'model',
        'reasoning': 'reasoning', 'effort': 'effort', 'tiers': 'tiers', 'tier': 'tier',
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
        return ParsedCommand(name, pieces[1].strip() if len(pieces) == 2 else None)

    def is_known(self, command: ParsedCommand) -> bool:
        return command.name in self.COMMANDS


HELP_TEXT = '''CFR 使用说明
普通文本：不以 / 开头，作为当前 CFR/Codex 会话的普通任务执行。
控制命令：以 / 开头，只执行 CFR 控制，不会作为普通任务发送给 Codex。

普通任务有两部分交付：Progress Card 只显示最新执行状态、最新思考摘要和最新输出预览，不保存完整过程；最终回复提供完整最终交付内容。

【模型】
/models：查看当前安装 Codex 的模型列表和编号。
/model：查看当前绑定线程的模型、推理档位、服务层级。
/model <编号|模型ID|模型名>：切换当前绑定线程模型，例如 /model 2。
/model default <编号|模型ID|模型名>：修改以后新建线程的默认模型，不修改当前线程。

【推理档位】
/reasoning 或 /effort：查看当前线程推理档位和当前模型支持的档位。
/reasoning <编号|档位>：修改当前线程，例如 /reasoning medium。
/reasoning default <编号|档位>：修改新建线程默认推理档位，不修改当前线程。

【服务层级】
/tiers：查看当前线程服务层级及当前模型可用层级。
/tier <编号|层级>：修改当前线程服务层级。
/tier default：当前线程恢复模型/运行时默认服务层级。

【Codex 协作模式】
/modes 或 /mode：查看可用 Codex collaboration mode。
/mode <编号|模式名>：切换当前 Codex 协作模式；/mode code 是 Codex Default 别名，/mode plan 是原生 Codex Plan。
这里的 /mode 不是未来的 Chat / Work / Code execution surface。

【会话】
/sessions：查看可用 CFR/Codex 会话列表。
/session <编号|线程ID>：将当前飞书 chat 绑定到已有会话；若已绑定，先 /unbind 再 /session。
/unbind：解除当前飞书 chat 绑定，不删除 Codex thread。
/new <绝对路径>：准备新会话；随后第一条普通任务才会创建 native Codex thread。

【工作区与运行】
/workspace：查看当前工作区；/workspace <绝对路径>：切换工作区。
/workspaces：查看允许的工作区。
/status：查看绑定、thread 与 active turn 状态。
/steer <追加指令>：引导；原生 Codex 软转向，只追加到同一个 Turn，不创建新 Turn，也不保证立即打断当前生成内容。
/redirect <新方向>：调整方向；立即请求停止当前 Turn，并在同一个 Thread 中排队创建新 Turn 按新方向执行。
/stop：停止当前 Turn，不自动继续。
/doctor：运行安全诊断；/whoami：查看当前飞书账号。
/compact：当前未实现安全的原生会话压缩。'''
