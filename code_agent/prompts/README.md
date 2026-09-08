# Prompts

本目录集中管理 **HelloAgents Academic Agent** 的全部提示词资产，便于独立迭代与对比。

## 文件说明

**被代码加载的（有效）**

| 文件 | 加载处 | 作用 |
|---|---|---|
| `react.md` | `CodeAgent` / `SubAgentTool` | ReAct 回合格式模板。**唯一会被 `.format()` 的模板**，含 `{tools}` `{question}` `{history}` 三个槽；字面花括号需写 `{{}}` |
| `mode_paper.md` | `ModeManager.get_prompt("paper")` | `/paper` 模式系统指令（作为 `[Role & Policies]` 注入，不做 format） |
| `mode_research.md` | `ModeManager.get_prompt("research")` | `/research` 模式系统指令 |
| `mode_code.md` | `ModeManager.get_prompt("code")` | `/code` 模式系统指令 + 「子 Agent 取 spec → plan/todo → 分模块补丁」复现节奏 |
| `subagent_paper_expert.md` | `SubAgentTool.run()` | 论文实现调查子 Agent 的角色指令与 spec 输出骨架 |
| `plan.md` | `PlanTool(prompt_path=...)` | 规划工具专用提示词 |
| `summarize_observation.md` | `CodeAgent._summarize_observation` | 工具输出超阈值时的 LLM 摘要提示词 |

**未被加载的（历史遗留）**

- `system.md`：改造为多模式前是唯一系统提示词，现已被三个 `mode_*.md` 取代。
- `tools.md`：`CodeAgent` 只把它赋给 `tools_reference_path` 属性，没有任何读取处。

> 二者暂留作参考，确认不再需要可删。

## 核心设计理念（借鉴 Claude Code）

### 按需探索：两层上下文

- **保底上下文**（自动注入）：系统指令（当前模式 prompt）+ 对话历史最近若干轮 + 最近 3 条工具摘要，超出预算时先 LLM 压缩、再按段落截断。
- **扩展上下文**（按需获取）：由模型主动调用 `context_fetch`，避免把全仓内容塞进每一轮。

### 角色与模板解耦

`react.md` 第 1 行显式声明「具体角色以下文 `[Role & Policies]` 为准」，因此**换模式/换子 Agent 只需换注入的角色文本**，ReAct 引擎与模板不变。子 Agent 复用同一模板，但由白名单裁剪工具描述，实现「同一引擎、不同角色」。

### context_fetch 使用指南

**何时使用：**
- ✅ 需要搜索代码中的类/函数定义
- ✅ 用户问"有没有关于 X 的笔记"
- ✅ 提到错误栈/报错信息，需要找相关代码
- ❌ 用户问"我们刚才说了什么"（直接用对话历史）
- ❌ 已经通过 terminal 拿到足够证据

**参数说明：**
```json
{
  "sources": ["files", "notes", "tests"],
  "query": "ContextBuilder",
  "paths": "context/**/*.py"
}
```

**预算控制：** 每个数据源返回上限约 800 tokens（`ContextFetchTool(max_tokens_per_source=800)`），自动截断。

**调用策略：** 先用保底上下文推理，证据不足再调用；避免盲目全局扫描。

> 注意：`memory` 源虽在工具参数里可选，但本 Agent 未接入 `MemoryTool`（构造时传 `None`），检索不到内容。
