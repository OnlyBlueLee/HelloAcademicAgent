"""HelloAgents Academic Agent：多模式科研辅助 Agent 的实现与 CLI 入口。

- `hello_code_cli.py`  : CLI 入口（斜杠命令 + 补丁提取/确认/应用）
- `modes.py`           : 模式与子 Agent 的声明式注册（MODES / SUBAGENTS / ModeManager）
- `agentic/code_agent.py`: CodeAgent 编排（模式热切换 + ReAct + 上下文构建）
- `executors/`         : 补丁执行器（路径边界、原子写、备份）
- `prompts/`           : 系统提示词、ReAct 模板与各模式/子 Agent 角色指令
"""

