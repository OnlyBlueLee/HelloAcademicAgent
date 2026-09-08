"""模式切换层 - HelloAgents Academic Agent 的三模式（/paper /research /code）

模式 = 同一个 ReAct agent 换「系统提示词 + 工具子集 + 当前主题库」，不是多 agent。
- SessionState : 跨模式共享状态（当前主题库、当前模式）。
- ModeManager  : 持有全量工具实例，为每个模式惰性构建子 ToolRegistry；
                 get_prompt(mode)/get_registry(mode) 供 CodeAgent 切换。

工具实例在多个模式注册表间共享（同一对象引用），因此 note/terminal/paper_rag 等状态一致；
paper_rag 与 arxiv 通过共享的 SessionState.current_topic 保持主题库一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Any

from tools.registry import ToolRegistry
from tools.base import Tool


@dataclass
class SessionState:
    """跨模式共享的会话状态"""
    current_topic: str = "default"
    mode: str = "paper"


@dataclass
class ModeSpec:
    name: str
    prompt_file: str        # 相对 prompts 目录
    tool_names: List[str]   # 该模式暴露给模型的工具（不在全量工具中则忽略）
    label: str              # 展示名
    hint: str               # 一句话说明


MODES: Dict[str, ModeSpec] = {
    "paper": ModeSpec(
        name="paper", prompt_file="mode_paper.md", label="论文解读",
        tool_names=["paper_rag", "note", "context_fetch", "todo"],
        hint="就已入库论文做带引用的问答与精读",
    ),
    "research": ModeSpec(
        name="research", prompt_file="mode_research.md", label="检索分析",
        tool_names=["arxiv", "paper_rag", "note", "todo", "context_fetch"],
        hint="按方向在 arxiv 检索→下载→入库→产出候选清单与要点",
    ),
    "code": ModeSpec(
        name="code", prompt_file="mode_code.md", label="代码助手",
        tool_names=["terminal", "context_fetch", "note", "todo", "plan", "paper_rag", "spawn_subagent"],
        hint="开源代码部署 / 按论文方法辅助复现（human-in-loop）",
    ),
}


@dataclass
class SubAgentSpec:
    """中心化子 Agent 规格：受限工具白名单 + 角色提示词 + 步数上限。

    子 Agent 由主 Agent 通过 spawn_subagent 工具以「同步 tool call」方式调起，
    控制权始终在主 Agent，返回结构化结果即终止（不移交控制、不递归）。
    """
    name: str
    prompt_file: str          # 相对 prompts 目录的角色指令
    tool_names: List[str]     # 受限工具白名单（不得含 terminal/补丁类）
    max_steps: int = 8
    label: str = ""


SUBAGENTS: Dict[str, SubAgentSpec] = {
    "paper_expert": SubAgentSpec(
        name="paper_expert",
        prompt_file="subagent_paper_expert.md",
        tool_names=["paper_rag", "note"],
        max_steps=8,
        label="论文实现调查",
    ),
}


class ModeManager:
    """管理三模式的提示词与工具子注册表"""

    def __init__(self, prompts_dir: Path, tools: Dict[str, Tool], default_mode: str = "paper"):
        self.prompts_dir = Path(prompts_dir)
        self.tools = tools
        self.default_mode = default_mode if default_mode in MODES else "paper"
        self._registries: Dict[str, ToolRegistry] = {}
        self._prompts: Dict[str, str] = {}

    def resolve(self, mode: str) -> str:
        return mode if mode in MODES else self.default_mode

    def _build_registry(self, spec: ModeSpec) -> ToolRegistry:
        reg = ToolRegistry()
        for tn in spec.tool_names:
            tool = self.tools.get(tn)
            if tool is not None:
                reg.register_tool(tool)
        return reg

    def get_registry_for(self, tool_names: List[str]) -> ToolRegistry:
        """按任意工具名列表构建受限注册表（供子 Agent 使用）。未知工具跳过。"""
        reg = ToolRegistry()
        for tn in tool_names:
            tool = self.tools.get(tn)
            if tool is not None:
                reg.register_tool(tool)
        return reg

    def get_registry(self, mode: str) -> ToolRegistry:
        mode = self.resolve(mode)
        if mode not in self._registries:
            self._registries[mode] = self._build_registry(MODES[mode])
        return self._registries[mode]

    def get_prompt(self, mode: str) -> str:
        mode = self.resolve(mode)
        if mode not in self._prompts:
            p = self.prompts_dir / MODES[mode].prompt_file
            self._prompts[mode] = p.read_text(encoding="utf-8") if p.exists() else ""
        return self._prompts[mode]

    def tool_names(self, mode: str) -> List[str]:
        spec = MODES[self.resolve(mode)]
        return [tn for tn in spec.tool_names if tn in self.tools]

    def describe(self) -> str:
        lines = ["可用模式（斜杠切换）："]
        for m, spec in MODES.items():
            lines.append(f"  /{m:<9} {spec.label} — {spec.hint}")
        return "\n".join(lines)
