# `code_agent` —— HelloAgents Academic Agent 主应用

> 项目的定位、安装、模式用法、配置与排错都在仓库根 [README.md](../README.md)。
> 本页只写**本包内部**的模块职责，避免和根文档重复维护。

## 模块

| 文件 | 职责 |
|---|---|
| `hello_code_cli.py` | CLI 入口：`--repo` / `--project` 解析、加载工作区根 `.env`、LLM 预检、交互循环与斜杠命令（`/paper` `/research` `/code` `/lib` `/status` `/plan` `/help` `/quit`）、补丁「提取 → 规范化 → 风险分级 → 确认 → 应用 → 记台账」 |
| `modes.py` | 声明式注册表：`MODES`（每模式 prompt 文件 + 工具白名单）、`SUBAGENTS`（角色 + 白名单 + `max_steps`）、`SessionState`（当前模式/主题库）、`ModeManager`（惰性构建子 `ToolRegistry`、缓存模式提示词、`get_registry_for()` 供子 Agent 裁剪） |
| `agentic/code_agent.py` | `CodeAgent` 编排层：装配共享工具实例 → 构建 `ModeManager` 与 `SubAgentTool` → 模式/主题库热切换 → `ContextBuilder` 保底上下文 → `ReActAgent` 执行 → 工具证据跨轮复用 + 会话持久化 |
| `executors/apply_patch_executor.py` | 补丁落地：`repo_root` 前缀校验（越界抛 `Path escapes repo_root`）、后缀白名单、临时文件 + `os.replace` 原子写、改前备份到 `.helloagents/backups/<时间戳>/` |
| `prompts/` | 提示词资产，见 [prompts/README.md](prompts/README.md) |

## 与早期版本描述的差异（已核对代码）

- 依赖清单是根目录的 `requirements.txt`；**不存在** `requirements-mvp.txt`、`.env.example`、`code_agent/cli/`、`code_agent/langchain_agent.py`、`docs/第九章 上下文工程.md`。
- `MemoryTool` / `RAGTool` 目前**没有**接入本 Agent（`CodeAgent` 构造时传 `None`），上下文只来自「系统提示 + 对话历史 + 最近工具摘要」，扩展信息靠 `context_fetch` 按需取。
- 补丁规模阈值（≥6 文件 / ≥400 变更行）只触发**人工确认**，不会拒绝写入；`Config` 里的 `patch_max_files` / `patch_max_total_lines` 未接线到执行器。
- 危险终端命令（`rm` / `chmod` / `git reset --hard`、写盘重定向）的门禁是「需要 `allow_dangerous=true`」，这个标志由模型自己在参数里置位，**CLI 不对终端做交互确认**；真正的人机闸门只有补丁应用一处。
- 上述缺口与修复方向集中记录在根 README 的[安全边界](../README.md#安全边界)一节。
