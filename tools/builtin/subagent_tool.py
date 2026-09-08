"""spawn_subagent 工具 - 中心化 orchestrator→worker 子 Agent

设计（与"对等 A2A"相对）：
- 主 Agent（如 /code）通过本工具以「同步 tool call」调起一个受限子 Agent，
  控制权始终在主 Agent，子 Agent 返回结构化结果即终止，**不移交控制、不递归**。
- 子 Agent 复用同一个 ReAct 引擎与同一批共享工具实例，但只暴露 SubAgentSpec.tool_names
  白名单里的工具（例如 paper_expert 只有 paper_rag/note，没有 terminal/补丁），
  从而做到「上下文隔离 + 权限收敛」，且零额外进程、零 HTTP。
- 子 Agent 的 ReAct 步数被 spec.max_steps 限制，配合 ReActAgent 的重复早停，避免发散。

注意：本工具依赖 agents.react_agent，导入较重但已在主流程加载；此处仍在方法内惰性导入
ReActAgent，避免 tools 包在 import 期拉起 agent 依赖。
"""

from pathlib import Path
from typing import Dict, Any, List

from ..base import Tool, ToolParameter


class SubAgentTool(Tool):
    """按角色调起一个受限子 Agent，返回其结论（如论文实现规格 spec）。"""

    def __init__(
        self,
        llm: Any,
        mode_manager: Any,
        prompts_dir: Any,
        subagents: Dict[str, Any],
        session_state: Any = None,
        default_max_tokens: int = 2000,
    ):
        super().__init__(
            name="spawn_subagent",
            description=(
                "调起一个受限子 Agent 完成一项隔离的调查任务，返回其结构化结论。"
                "role=paper_expert：让子 Agent 用 paper_rag 把论文实现细节查清楚，"
                "返回带引用的『实现规格 spec』（网络结构/损失/超参/数据/训练）。"
                "主 Agent 保留控制权，拿 spec 后再规划与写补丁。"
            ),
        )
        self.llm = llm
        self.mode_manager = mode_manager
        self.prompts_dir = Path(prompts_dir)
        self.subagents = subagents
        self.session_state = session_state
        self.default_max_tokens = default_max_tokens
        # 子 Agent 复用与主 Agent 相同的 ReAct 模板（已验证 format-safe）
        self._react_prompt = self._load_react_prompt()

    def get_parameters(self) -> List[ToolParameter]:
        roles = ", ".join(self.subagents.keys()) if self.subagents else "(无)"
        return [
            ToolParameter(
                name="role", type="string",
                description=f"子 Agent 角色，可选: {roles}", required=True,
            ),
            ToolParameter(
                name="task", type="string",
                description="交给子 Agent 的具体调查任务（尽量聚焦、可验收）", required=True,
            ),
            ToolParameter(
                name="topic", type="string",
                description="主题库名（缺省用当前主题库）；paper_expert 检索时使用", required=False,
            ),
        ]

    def _load_react_prompt(self) -> str:
        p = self.prompts_dir / "react.md"
        try:
            return p.read_text(encoding="utf-8") if p.exists() else ""
        except Exception:
            return ""

    def _resolve_topic(self, params: Dict[str, Any]) -> str:
        t = params.get("topic") or getattr(self.session_state, "current_topic", None) or "default"
        return str(t).strip() or "default"

    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return "❌ 参数验证失败：需要 role 与 task"
        role = (parameters.get("role") or "").strip()
        task = (parameters.get("task") or "").strip()
        if not task:
            return "❌ task 不能为空"

        spec = self.subagents.get(role)
        if spec is None:
            return f"❌ 未知子 Agent 角色: {role}。可用: {', '.join(self.subagents.keys())}"

        try:
            from agents.react_agent import ReActAgent
        except Exception as e:  # pragma: no cover
            return f"❌ 无法加载 ReActAgent: {e}"

        # 受限子注册表：只给白名单工具（无 terminal/补丁），天然收敛权限、避免递归
        registry = self.mode_manager.get_registry_for(spec.tool_names)

        topic = self._resolve_topic(params=parameters)
        role_prompt = ""
        rp = self.prompts_dir / spec.prompt_file
        if rp.exists():
            try:
                role_prompt = rp.read_text(encoding="utf-8")
            except Exception:
                role_prompt = ""

        input_text = (
            f"[Role & Policies]\n{role_prompt}\n\n"
            f"[Task]\n## 本次调查任务\n{task}\n\n"
            f"## 当前主题库\n{topic}\n"
        ).strip()

        sub = ReActAgent(
            name=f"sub:{role}",
            llm=self.llm,
            tool_registry=registry,
            max_steps=spec.max_steps,
            custom_prompt=self._react_prompt,
            summarize_threshold_chars=1800,
        )
        try:
            result = sub.run(input_text, max_tokens=self.default_max_tokens)
        except Exception as e:
            return f"❌ 子 Agent '{role}' 执行失败: {e}"

        return f"[子Agent·{spec.label or role} 结论]\n{result}"
