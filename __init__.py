"""
HelloAgents Academic Agent CLI - 多模式命令行科研智能体

基于 HelloAgents 范式自研（代码自包含），覆盖论文解读 / arxiv 检索入库 / 代码辅助复现三场景。
"""

# 配置第三方库的日志级别，减少噪音
import logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("qdrant_client").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("neo4j").setLevel(logging.WARNING)
logging.getLogger("neo4j.notifications").setLevel(logging.WARNING)

from .version import __version__, __author__, __email__, __description__

# 核心组件
from .core.llm import HelloAgentsLLM
from .core.config import Config
from .core.message import Message
from .core.exceptions import HelloAgentsException

# Agent实现
from .agents.simple_agent import SimpleAgent
from .agents.react_agent import ReActAgent
from .agents.reflection_agent import ReflectionAgent
from .agents.plan_solve_agent import PlanAndSolveAgent

# 工具系统
from .tools.registry import ToolRegistry, global_registry
from .tools.builtin.search import SearchTool, search
from .tools.builtin.calculator import CalculatorTool, calculate
from .tools.chain import ToolChain, ToolChainManager
from .tools.async_executor import AsyncToolExecutor

__all__ = [
    # 版本信息
    "__version__",
    "__author__",
    "__email__",
    "__description__",

    # 核心组件
    "HelloAgentsLLM",
    "Config",
    "Message",
    "HelloAgentsException",

    # Agent范式
    "SimpleAgent",
    "ReActAgent", 
    "ReflectionAgent",
    "PlanAndSolveAgent",

    # 工具系统
    "ToolRegistry",
    "global_registry",
    "SearchTool",
    "search",
    "CalculatorTool",
    "calculate",
    "ToolChain",
    "ToolChainManager",
    "AsyncToolExecutor",
]

