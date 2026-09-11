# HelloAgents Academic Agent CLI

面向 **CV / 遥感方向论文科研**的命令行 Agent：读论文、追前沿、复现代码。
基于 HelloAgents 范式自研实现（代码自包含，不依赖 `hello-agents` 包），提供类似 Claude Code / Codex 的交互体验，用**斜杠命令在三种工作模式间热切换**。

```
/paper      论文解读 —— 就已入库论文做带引用的问答与精读
/research   检索分析 —— 按研究方向在 arxiv 检索 → 下载 → 入库 → 产出候选清单与要点
/code       代码助手 —— 部署开源代码 / 按论文方法辅助复现（human-in-the-loop）
```

---

## 目录

- [它做什么](#它做什么)
- [架构](#架构)
- [快速开始](#快速开始)
- [使用指南](#使用指南)
- [配置项](#配置项)
- [安全边界](#安全边界)
- [项目结构](#项目结构)
- [已知限制与排错](#已知限制与排错)

---

## 它做什么

| 任务 | 模式 | 关键工具 | 产出 |
|---|---|---|---|
| **1. 论文解读 / 问答** | `/paper` | `paper_rag` | 带 `[n]` 引用的答案（含来源文件、章节路径、字符区间），可落 `.md` 精读笔记 |
| **2. 按方向检索分析** | `/research` | `arxiv` + `paper_rag` | 候选论文清单（标题/作者/日期/分类/摘要要点）+ 趋势小结 |
| **3. 部署与复现** | `/code` | `terminal` + `spawn_subagent` + `plan`/`todo` + 补丁 | 可运行的脚手架/模型代码补丁 + spec 缺口清单 |

三条设计原则：

- **模式 = 同一个 ReAct 引擎换「系统提示词 + 工具子集 + 当前主题库」**，不是多 agent 进程。切换即时生效（每步重建工具描述）。
- **论文按「主题库」隔离**：同一 Qdrant collection 内用 `rag_namespace` 过滤，`/lib <topic>` 切换当前库，`arxiv` 下载目录与 `paper_rag` 检索范围自动跟随。
- **调查与实施分离（中心化子 Agent）**：主 Agent 把「论文是怎么实现的」这类高 token 调查外包给受限子 Agent，拿回结构化 spec 后再规划与写码。控制权不移交、结果最小回传。

---

## 架构

```
┌──────────────────────────────────────────────────────────────┐
│  CLI 层  code_agent/hello_code_cli.py                        │
│  斜杠命令 / 交互循环 / 补丁提取与确认 / apply_patch          │
└───────────────────────────┬──────────────────────────────────┘
┌───────────────────────────▼──────────────────────────────────┐
│  编排层  code_agent/agentic/code_agent.py                    │
│  CodeAgent：模式热切换 set_mode / 主题库热切换 set_topic     │
│  ContextBuilder(GSSC) → ReActAgent(max_steps=20)             │
└──────────┬───────────────────────────────────┬───────────────┘
┌──────────▼─────────────────┐   ┌─────────────▼──────────────┐
│  模式层  code_agent/modes.py│   │  子 Agent 层               │
│  MODES: paper/research/code │   │  SUBAGENTS: paper_expert   │
│  每模式 prompt + 工具白名单 │──▶│  spawn_subagent(同步调用)  │
│  get_registry / get_prompt  │   │  受限注册表(无 terminal)   │
└──────────┬─────────────────┘   └─────────────┬──────────────┘
┌──────────▼─────────────────────────────────────▼─────────────┐
│  工具层  tools/builtin/                                       │
│  terminal │ context_fetch │ note │ todo │ plan │ paper_rag    │
│  arxiv │ spawn_subagent                                       │
└──────────┬───────────────────────────────────┬───────────────┘
┌──────────▼─────────────────┐   ┌─────────────▼──────────────┐
│  RAG 层  memory/rag/        │   │  执行层                    │
│  PDF→MarkItDown→标题感知分块│   │  ApplyPatchExecutor        │
│  →embedding→Qdrant(嵌入式)  │   │  原子写 + 自动备份         │
│  MQE/HyDE 扩展→向量+图信号  │   │  后缀白名单 + 路径逃逸拦截 │
│  融合重排→分组片段+引用     │   └────────────────────────────┘
└────────────────────────────┘
```

**关键实现点**

- **ReAct 引擎**（`agents/react_agent.py`）：严格 `Thought:` / `Action:` 两段式；解析失败会强制一次格式修复重试；支持重复动作早停、步数耗尽时兜底收敛（finalize）。
- **RAG 流水线**（`memory/rag/pipeline.py`）：PDF 解析（MinerU 云端 → pypdfium2 → markitdown 三级降级）→ 标题感知段落分块（带 `heading_path` 与字符区间）→ 向量入库 → 检索时可选 MQE/HyDE 查询扩展 → `compute_graph_signals_from_pool` + `rank`（向量 0.7 / 同篇邻近 0.3 融合）→ `merge_snippets_grouped` 输出 `[n]` 引用与 References。
- **PDF 解析**（`memory/rag/mineru_client.py`）：默认走 **MinerU 云端 API**（免登录，双栏版面还原正确、保留公式与标题层级，实测表格噪声 1% vs markitdown 38%）。配有 token 时自动升级到精确解析 API（≤200MB/200 页）；失败则回退本地 `pypdfium2`，再回退 markitdown。解析结果按文件哈希缓存到 `.helloagents/pdf_cache/`，同一 PDF 不会重复上传。
  > ⚠️ 云端解析会把 PDF 上传到 mineru.net。未发表/涉密论文请设 `MINERU_ENABLED=0` 走本地解析。
- **向量库**：Qdrant **嵌入式本地模式**（`QdrantClient(path=...)`），免 Docker；经 `QdrantConnectionManager` 按 `(url, collection)` 单例复用，避免文件锁冲突。设置 `QDRANT_URL` 即切回服务端/云模式。
- **子 Agent**（`tools/builtin/subagent_tool.py`）：与主 Agent 共享同一批工具实例和同一份 `react.md` 模板，但只暴露 `SubAgentSpec.tool_names` 白名单，步数受 `max_steps` 限制。白名单不含 `terminal`/补丁 → 天然无递归、权限收敛。

---

## 快速开始

### 1. 环境要求

- Python **3.10+**
- macOS / Linux / Windows
- 一个 OpenAI 兼容的 LLM API（DeepSeek / 通义 / Kimi / GLM / 本地 vLLM·Ollama 均可）

### 2. 安装

```bash
git clone https://github.com/OnlyBlueLee/HelloAcademicAgent.git
cd HelloAcademicAgent

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

依赖分档，按需装：

| 档 | 内容 | 用途 |
|---|---|---|
| 核心 | `openai` `pydantic` `python-dotenv` `tiktoken` | 只跑 `/code` 的终端/补丁/笔记链路 |
| RAG | `qdrant-client` `markitdown[pdf]` `numpy` `langdetect` + embedding 后端 | `/paper`、`/research`、`paper_rag` |
| arxiv | `arxiv`（锁 2.x，4.x 移除了下载 API） | `/research` 检索下载（**无需 API key**） |

embedding 后端二选一（`requirements.txt` 中默认启用 DashScope）：

```bash
pip install dashscope                     # 轻量：走百炼 API，免装 torch（本地试用推荐）
pip install sentence-transformers         # 本地/离线：需 torch，首次会下载模型
```

### 3. 配置 `.env`（放在**工作区根目录**，即 `--repo` 指向的目录）

```bash
# ---- LLM（必需）：有哪个 key 就自动选哪个 provider ----
DEEPSEEK_API_KEY=sk-xxxxxxxx
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL_ID=deepseek-chat            # 变量名是 LLM_MODEL_ID，不是 LLM_MODEL

# 也支持：OPENAI_API_KEY / DASHSCOPE_API_KEY / MODELSCOPE_API_KEY /
#         KIMI_API_KEY / ZHIPU_API_KEY / OLLAMA_HOST / VLLM_HOST

# ---- Embedding（论文 RAG 必需）----
EMBED_MODEL_TYPE=dashscope            # dashscope | local
EMBED_API_KEY=sk-xxxxxxxx             # local 模式留空即可；dashscope SDK 模式缺省会读 DASHSCOPE_API_KEY
EMBED_MODEL_NAME=text-embedding-v3    # dashscope 默认值；local 默认 sentence-transformers/all-MiniLM-L6-v2

# ---- 可选 ----
DEFAULT_MODE=paper                    # 启动默认模式
# PAPERS_DIR=papers                   # 论文 PDF 根目录（相对工作区）
# RAG_COLLECTION=research_papers
# QDRANT_LOCAL_PATH=.helloagents/qdrant   # 嵌入式本地存储，默认就是这个值（免 Docker）
# QDRANT_URL=http://localhost:6333        # 设了则优先，改用服务端/云 Qdrant
```

> `EMBED_MODEL_TYPE=dashscope` 与 `local` 的向量维度不同（1024 / 384）。**中途换后端必须重建 collection**，见[排错](#已知限制与排错)。

### 4. 启动

```bash
python -m code_agent.hello_code_cli --repo .                    # 当前目录作为工作区
python -m code_agent.hello_code_cli --repo /path/to/project     # 指定其他仓库
```

启动会打印 workspace、LLM provider/model/base_url、当前模式与主题库，并做一次 LLM 预检（key/base_url 配错会立刻报错并退出）。

CLI 参数只有 `--repo` 与 `--project`，其余全部走 `.env` / 环境变量。

---

## 使用指南

### 斜杠命令

| 命令 | 作用 |
|---|---|
| `/paper` `/research` `/code` | 切换模式（换系统提示词 + 工具子集） |
| `/lib <topic>` | 切换当前主题库（arxiv 下载目录、RAG 检索范围随之改变） |
| `/status` | 查看当前模式与主题库 |
| `/doctor` | 健康自检：LLM 非空应答、embedding、向量库、PDF 解析、各主题库零向量巡检 |
| `/plan <目标>` | 强制生成一份执行计划（不依赖当前模式是否注册 plan 工具） |
| `/help` | 帮助 + 各模式说明 |
| `/quit`（`/q` `exit` `:q`） | 退出 |

纯文本输入走 ReAct；问候语与「我们刚才说了什么」这类元问题会直接短路回复，不进工具循环。

### 任务 1：论文解读（`/paper`）

```
/lib change_detection
把 papers/change_detection/ChangeMamba.pdf 入库，然后回答：
它的网络结构是什么？主干用了什么？损失函数怎么定义的？
```

回答会带 `[1][2]` 引用，末尾 References 给出来源文件 + 字符区间 + 章节路径。可再要求「整理成精读笔记」→ 用 `note` 落 `.md`。

### 任务 2：按方向检索分析（`/research`）

```
/research
在 Mamba / 状态空间模型做遥感变化检测 这个方向上检索近 2 年 arxiv 论文，
列出候选清单并标注与我的 baseline 的差异，然后下载最相关的 3 篇入库。
```

`arxiv` 支持 `search` / `download` / `metadata`，默认按 `cs.CV` 过滤（可改或留空不限）。
公开 API 无需 key，`arxiv` 库自带约 3 秒请求间隔。
「批量下载前先说明要下哪些、存到哪个主题库并征得同意」是 **`/research` 模式提示词层约束**（不像 `/code` 的补丁确认那样由 CLI 强制拦截）。

### 任务 3：部署与复现（`/code`）

部署开源仓库：直接说需求，Agent 会用 `terminal` 小范围取证（`rg` / `ls` / `sed -n`），再给补丁。

按论文复现走 **plan-then-act**：

```
/code
/lib change_detection
参照 ChangeMamba 复现它的双分支 Mamba 编码器的最小可训练版本
```

1. **派子 Agent 调查** → `spawn_subagent[{"role":"paper_expert","task":"..."}]`
   子 Agent 只有 `paper_rag`/`note`，只读论文库、不写码，返回结构化**实现规格 spec**（网络结构 / 损失 / 超参 / 数据 / 训练评估 / novelty / **缺口与假设** / 引用）。
2. **主 Agent 规划** → 基于 spec 用 `plan` / `todo` 拆成可核对小步（数据加载 → 模型结构 → 损失 → 训练循环 → 评估），并显式列出 spec 缺口与自身假设。
3. **分模块实现** → 一次聚焦一个模块出补丁，不堆砌全部代码。
4. **交回用户** → 说明假设与缺口，由你验证迭代。

补丁由 CLI 提取并应用：低风险直接落盘，高风险（删除/大规模变更）会二次确认 `y/n`，落盘前自动备份到 `.helloagents/backups/`。

**定位是辅助复现，不承诺端到端直接跑通**——论文未写明的超参、数据处理细节需要人工补齐。

### 演示

视频（早期「代码助手」形态，交互与补丁流程通用）：https://www.bilibili.com/video/BV1UzBpBBE75/

---

## 配置项

`.env` 加载位置是**工作区根目录**。下列变量确认在代码里被真实读取：

| 环境变量 | 默认值 | 读取位置 | 说明 |
|---|---|---|---|
| `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `DASHSCOPE_API_KEY` / `MODELSCOPE_API_KEY` / `KIMI_API_KEY` / `ZHIPU_API_KEY` / `OLLAMA_HOST` / `VLLM_HOST` | — | `core/llm.py` | 有哪个就自动选哪个 provider |
| `LLM_MODEL_ID` | provider 默认 | `core/llm.py` | 模型名（**不是** `LLM_MODEL`） |
| `LLM_BASE_URL` / `LLM_API_KEY` | provider 默认 | `core/llm.py` | 自定义端点/兜底 key |
| `LLM_TIMEOUT` | `60` | `core/llm.py` | LLM 请求超时（秒） |
| `DEFAULT_MODE` | `paper` | `Config` → `CodeAgent` | 启动模式 |
| `HELLOAGENTS_DIR` / `CODE_AGENT_STATE_DIR` | `.helloagents` | `Config` → `Paths` | 状态根目录（notes/sessions/todos/backups/qdrant） |
| `PAPERS_DIR` | `papers` | `Config` → `ArxivTool` | PDF 根目录，下按 `<topic>/` 分目录 |
| `RAG_COLLECTION` | `research_papers` | `Config` → `PaperRagTool` | 论文向量库 collection |
| `QDRANT_LOCAL_PATH` | `.helloagents/qdrant` | `Config` → `PaperRagTool` | 嵌入式本地存储（免 Docker），默认即启用 |
| `QDRANT_URL` / `QDRANT_API_KEY` | 空 | `PaperRagTool._get_store` | 设置后改用服务端/云 Qdrant，优先级高于 local path |
| `QDRANT_HNSW_M` / `QDRANT_HNSW_EF_CONSTRUCT` / `QDRANT_SEARCH_EF` / `QDRANT_SEARCH_EXACT` | `32` / `256` / `128` / `0` | `qdrant_store.py` | 向量索引与检索参数 |
| `EMBED_MODEL_TYPE` | `dashscope` | `memory/embedding.py` | `dashscope`（1024 维）/ `local` sentence-transformers（384 维） |
| `EMBED_MODEL_NAME` / `EMBED_API_KEY` / `EMBED_BASE_URL` | 按后端 | `memory/embedding.py` | 覆盖 embedding 模型与端点 |
| `EMBED_MAX_BATCH` | `10` | `memory/rag/pipeline.py` | embedding 单批上限；dashscope `text-embedding-v3` 单次 ≤10，设为超过该值会导致整批失败 |
| `MINERU_ENABLED` | `1` | `memory/rag/mineru_client.py` | 是否用 MinerU 云端解析 PDF；设 `0` 则完全本地（不外传） |
| `MINERU_API_TOKEN` | 空 | `mineru_client` | 留空用免登录 Agent API（≤10MB）；填了自动升级精确 API v4（≤200MB/200 页） |
| `MINERU_LANGUAGE` / `MINERU_MODEL_VERSION` | `ch` / `pipeline` | `mineru_client` | OCR 语言 / 精确 API 模型版本（可 `vlm`） |
| `MINERU_TIMEOUT` / `MINERU_POLL_INTERVAL` | `600` / `3` | `mineru_client` | 单文件解析总超时（秒）/ 轮询间隔（秒） |
| `MINERU_AGENT_MAX_MB` | `10` | `mineru_client` | 免登录 API 的文件大小上限（MB），超过则跳过云端 |
| `MINERU_CACHE_DIR` | `.helloagents/pdf_cache` | `mineru_client` | 解析结果缓存目录（按 PDF 哈希） |
| `MINERU_SSL_VERIFY` | `1` | `mineru_client` | TLS 校验；证书链不全的环境可设 `0` |

补丁可改后缀白名单由 `ApplyPatchExecutor.allowed_write_suffixes` 决定（CLI 未传参，用内置默认）：
`.py .md .toml .json .yml .yaml .txt .html .htm .css .js`。

> **已定义但当前未接线**（设了不生效，别踩坑）：`CODE_AGENT_MAX_REACT_STEPS`、`CODE_AGENT_TERMINAL_TIMEOUT`、
> `CODE_AGENT_PATCH_MAX_FILES`、`CODE_AGENT_PATCH_MAX_LINES`、`config.patch_allowed_suffixes`、`TEMPERATURE`、`LOG_LEVEL`、`DEBUG`。
> 主 Agent ReAct 步数固定 20、终端超时固定 60s、上下文预算固定 8000 tokens（均在 `code_agent/agentic/code_agent.py` 内硬编码）；
> 补丁规模/删除的确认阈值由 CLI 判定（≥6 文件或 ≥400 变更行）。要调这些得改代码或把 config 真正传给构造处。

---

## 安全边界

**代码层强制**

- **补丁路径边界**：`ApplyPatchExecutor` 对每个目标 `resolve()` 后校验前缀，越界直接抛 `Path escapes repo_root`。
- **写盘通道收敛**：只有补丁能改文件；补丁走临时文件 + `os.replace` 原子写（含 `fsync`），改前先备份到 `.helloagents/backups/<时间戳>/`。
- **后缀白名单**：非白名单后缀报 `Disallowed file suffix`，避免误写二进制/敏感文件。
- **补丁人工确认**：含 `Delete File`、或 **≥6 个文件** / **≥400 变更行** → CLI 二次确认 `y/n`，否则只回文字不落盘。
- **终端执行**：`subprocess` 以 `shell=False` + argv 执行（防 shell 注入）；`cd` / `mkdir` / `rm` / `chmod` 的路径参数必须落在工作区内，否则 `拒绝在工作目录外操作`；`rm` / `chmod` / `git reset --hard` 与写盘型重定向需要 `allow_dangerous=true`。
- **权限收敛**：子 Agent 白名单不含 `terminal`/补丁，只能读论文库与写笔记，不能改代码，也不会递归调起自己。
- **不编造**：角色提示词要求论文未说明项显式写「论文未说明」，禁止臆造 arxiv id、数值、结构与引用。

**仅提示词层约束**（模型自我克制，不是硬门禁）：只读优先、按需小范围取证、避免无端全库扫描、下载前征求同意、`cat >` / `tee` / Here-Doc 之类的终端写法禁止。

**已知缺口**（后续要补，别当作已闭合）

- `allow_dangerous` 由模型自己在参数里填，**CLI 不对终端做交互确认**——目前唯一真正的人机闸门是补丁应用那一处。
- 只读命令（`cat` / `sed` / `rg` 等）未做路径沙箱，可用 `../` 或绝对路径读到工作区外的文件（写被拦，读没有）。
- 建议方向：把危险终端命令也接到 CLI 确认闸门；给只读命令加工作区前缀校验；把 `Config` 里的步数/超时/补丁上限真正传进构造处。

---

## 项目结构

```
HelloAcademicAgent/
├── code_agent/                     # 主应用
│   ├── hello_code_cli.py           # CLI 入口：斜杠命令 + 补丁提取/确认/应用
│   ├── modes.py                    # 模式层：MODES / SUBAGENTS / ModeManager
│   ├── agentic/code_agent.py       # CodeAgent：模式热切换 + ReAct + 上下文构建
│   ├── executors/apply_patch_executor.py
│   └── prompts/
│       ├── react.md                # ReAct 模板（含 {tools}/{question}/{history}）
│       ├── mode_paper.md           # 三模式系统提示词
│       ├── mode_research.md
│       ├── mode_code.md
│       ├── subagent_paper_expert.md# 论文实现调查子 Agent 角色指令
│       └── plan.md / summarize_observation.md
│
├── agents/                         # react_agent / plan_solve_agent / reflection / simple
├── core/                           # llm（多 provider 自动检测）/ config / message / agent / exceptions
├── context/                        # builder.py：GSSC 上下文流水线（[Role & Policies]/[Task]/[Evidence]/[Context]）
├── memory/                         # embedding（provider 工厂）/ types / storage / rag
│   ├── rag/pipeline.py             # PDF→分块→索引→扩展检索→融合重排→引用输出
│   └── storage/qdrant_store.py     # Qdrant 连接管理器（嵌入式本地 / 服务 / 云）
├── tools/                          # base / registry / chain + builtin/
│   └── builtin/                    # terminal, context_fetch, note, todo, plan,
│                                   # paper_rag, arxiv, subagent_tool, memory, mcp_wrapper, protocol(A2A/MCP/ANP)
├── utils/                          # cli_ui（Spinner/着色/日志）/ helpers / logging / serialization
└── requirements.txt                # 依赖清单
```

运行时状态全部落在 `.helloagents/`（notes / sessions / todos / backups / qdrant），论文 PDF 落在 `papers/<topic>/`；二者与 `.env` / `.venv` 均已在 `.gitignore` 中排除，不会进入公开仓库。

---

## 已知限制与排错

**验证状态**：本文描述的是代码当前实现（逐处核对过 config / 工具参数 / 执行器与 CLI 判定逻辑）。
已在本地环境实跑验证：`/research` 检索（含 `sort_by=SubmittedDate` 按时间排序）、arxiv 下载、PDF 解析（`markitdown[pdf]`）、`/paper` 引用问答、`/research` 下载→入库闭环、`/doctor` 自检；`/code` 补丁落盘按排错表自查即可。

| 现象 | 原因 / 处理 |
|---|---|
| 换 embedding 后端后检索报错或结果异常 | 维度不一致（dashscope 1024 / local 384）。删掉 `.helloagents/qdrant/` 后重新 `paper_rag[action=index]` 全量重建——这会**清空已入库向量**，PDF 本身还在 `papers/` |
| 提示连不上 `localhost:6333` | 说明这次调用没走嵌入式路径：检查是否设了 `QDRANT_URL`，或该调用来自未传 `local_path` 的旧入口（`paper_rag` 已默认传）。否则就起一个服务：`docker run -p 6333:6333 qdrant/qdrant` |
| 嵌入式 Qdrant 报文件锁被占用 | 本地模式是**单进程独占**，不要同时开两个 CLI 实例 |
| `paper_rag` 报"qdrant-client未安装"但确实装了 | qdrant-client 大版本升级曾移除 `models.SearchRequest` 等名字。已修：import 只保留实际使用的符号；若换其他版本仍报此错，用 `/doctor` 定位，并按 `require` 里的版本区间装 `qdrant-client` |
| 入库显示成功、但检索永远无命中 | 多为**零向量**：embedding 批次超限（dashscope `text-embedding-v3` 单次 ≤10）会让整批失败并曾静默补零。已修（批次钳制到 `EMBED_MAX_BATCH`，失败即报错）。用 `/doctor` 看主题库向量范数，有零向量就重新 `index` |
| arxiv 下载报 `CERTIFICATE_VERIFY_FAILED` | 已修：下载改走 `requests`（自带 certifi），失败才回退 arxiv 自带 urllib。若仍失败，设 `SSL_CERT_FILE=<certifi>/cacert.pem` |
| 检索结果与关键词毫不相关 | 此前把自然语言原样拼进 `cat:X AND (query)`，配时间排序会捞出大量无关最新投稿。已修：逐词加引号 + AND 连接（`build_arxiv_query`）；已含 arxiv 语法的输入原样透传 |
| `LLM 预检失败` | `.env` 里 key / `LLM_BASE_URL` / `LLM_MODEL_ID` 三者不匹配（模型名要用 provider 侧真实存在的 id） |
| 预检通过但模型/摘要偶发"空回复" | 推理型模型（如 `deepseek-v4-flash`）的 reasoning token 计入 `max_tokens`，预算太小会整段返回空。预检与摘要预算已上调；自建调用请留足 ≥512 |
| `所有嵌入模型都不可用` | 对应后端未安装或未配 key；`pip install dashscope` 或 `sentence-transformers` |
| tfidf embedding 建不了索引 | 已知限制：索引路径不训练 tfidf，**不要**把 `EMBED_MODEL_TYPE` 设成 `tfidf` |
| 论文没入库就问答，答不出东西 | `paper_rag` 只检索已索引内容。先在 `/research` 下载入库，或手动 `index` 本地 PDF |
| 同一方向"下载在这、检索在那" | topic 会被统一清洗（`utils/topic.py`），`/lib`、`arxiv`、`paper_rag` 三处一致。跨会话请用 `/lib <topic>` 固定并保持 `index`/`ask` 的 topic 相同 |
| 补丁被拒 | 目标后缀不在执行器白名单（报 `Disallowed file suffix`），或被判高风险后你在确认环节选了 `n` → 换后缀或拆成多个小补丁 |
| 复现跑不通 | 属预期：论文常省略超参/预处理细节。看 spec 的「缺口与假设」逐条与用户确认 |

**其他待办**：会话恢复（断点续传摘要）、终端命令拆成原子工具、把 `Config` 参数接进构造处；
`code_agent/README.md` 与 `code_agent/prompts/README.md` 已同步为「包内职责 + 提示词清单」，功能说明统一以本文为准。

---

## 致谢

- **Datawhale 社区 / Hello-Agents**：框架范式与学习资源
- **OpenAI / DeepSeek / 阿里 DashScope**：LLM 与 embedding 服务
- **arxiv 公开 API**、**MarkItDown**、**Qdrant**

## 许可证

[MIT License](LICENSE)
