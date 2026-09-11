from __future__ import annotations

import argparse
import os
import re
import logging
from pathlib import Path

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    def load_dotenv(*args, **kwargs):  # type: ignore
        return False

from core.llm import HelloAgentsLLM
from core.exceptions import HelloAgentsException
from core.config import Config
from code_agent.agentic import CodeAgent
from code_agent.executors.apply_patch_executor import ApplyPatchExecutor, PatchApplyError
from utils.cli_ui import c, hr, PRIMARY, ACCENT, INFO, WARN, ERROR


# 匹配 Codex 风格补丁块（宽松，跨行，允许前导空白或代码围栏）
PATCH_RE = re.compile(r"\s*\*\*\* Begin Patch[\s\S]*?\*\*\* End Patch", re.MULTILINE)
# 备用：从 ```patch/```diff 围栏中提取补丁主体
PATCH_FENCE_RE = re.compile(
    r"```(?:patch|diff|text)?\s*(\*\*\* Begin Patch[\s\S]*?\*\*\* End Patch)\s*```",
    re.MULTILINE,
)


def _extract_patch(text: str) -> str | None:
    """
    从 LLM 响应文本中提取补丁块。
    补丁块通常由 *** Begin Patch 和 *** End Patch 包围。
    """
    # 优先匹配代码围栏内的补丁
    m = PATCH_FENCE_RE.search(text)
    if m:
        return m.group(1)
    # 退回普通匹配（允许前导空白）
    m = PATCH_RE.search(text)
    return m.group(0).strip() if m else None


def _normalize_patch(patch_text: str) -> str:
    """
    规范化补丁文本，以宽容处理模型的一些格式错误。
    - 接受 'Delete File:' / 'Update File:' / 'Add File:' (即使缺少前导 '*** ')
    - 保持执行器所需的标准 Codex 风格格式。
    """
    lines = patch_text.splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("Add File:", "Update File:", "Delete File:")) and not stripped.startswith("*** "):
            out.append("*** " + stripped)
            continue
        out.append(line)
    return "\n".join(out)


def _patch_requires_confirmation(patch_text: str) -> bool:
    """
    判断补丁是否需要用户确认。
    策略：
    - 包含文件删除操作
    - 涉及文件数量过多 (>= 6)
    - 变更行数过多 (>= 400)
    """
    # MVP: Delete File / too many files / too big => confirm
    if "*** Delete File:" in patch_text:
        return True
    file_ops = patch_text.count("*** Add File:") + patch_text.count("*** Update File:") + patch_text.count("*** Delete File:")
    if file_ops >= 6:
        return True
    changed_lines = 0
    for line in patch_text.splitlines():
        if line.startswith("+") or line.startswith("-"):
            changed_lines += 1
    return changed_lines >= 400


# qdrant 本地模式直接向 root logger 打 WARNING（"Payload indexes have no effect"），
# setLevel 压不住，按消息内容过滤静音
class _NoQdrantLocalNoise(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "Payload indexes have no effect" not in record.getMessage()


def _run_doctor(agent, llm) -> None:
    """健康自检：把此前"静默降级/假成功"的环节显式暴露出来。"""
    print(c("\n🩺 自检开始", PRIMARY))
    ok = True

    # 1) LLM：必须返回非空内容（推理模型的 reasoning token 也占用 max_tokens，
    #    只给 1~5 个 token 时会返回空串，看起来却"通过"）。
    try:
        r = llm.invoke([{"role": "user", "content": "回复：ok"}], max_tokens=512)
        if (r or "").strip():
            print(c("  ✅ LLM 连通，返回非空", INFO))
        else:
            ok = False
            print(c("  ❌ LLM 返回空内容（可能 token 预算过小或模型为推理型）", ERROR))
    except Exception as e:
        ok = False
        print(c(f"  ❌ LLM 调用失败: {e}", ERROR))

    # 2) Embedding：实时 encode 一条，确认后端可用与维度
    try:
        from memory.embedding import get_text_embedder
        emb = get_text_embedder()
        v = emb.encode("health_check")
        print(c(f"  ✅ Embedding 可用: {type(emb).__name__} dim={len(v)}", INFO))
    except Exception as e:
        ok = False
        print(c(f"  ❌ Embedding 不可用: {e}", ERROR))

    # 3) 向量库 import（此前 SearchRequest 会让 QDRANT_AVAILABLE 误判为 False）
    try:
        from memory.storage.qdrant_store import QDRANT_AVAILABLE
        if QDRANT_AVAILABLE:
            print(c("  ✅ 向量库(qdrant)可用", INFO))
        else:
            ok = False
            print(c("  ❌ 向量库(qdrant)不可用（依赖未装或版本不兼容）", ERROR))
    except Exception as e:
        ok = False
        print(c(f"  ❌ 向量库导入失败: {e}", ERROR))

    # 4) PDF 解析：MinerU 云端优先，本地 pypdfium2 / markitdown 兜底
    try:
        from memory.rag.mineru_client import describe as _mineru_describe, is_enabled as _mineru_on
        if _mineru_on():
            print(c(f"  ℹ️ PDF 解析: MinerU {_mineru_describe()}（论文会上传至 mineru.net）", INFO))
        else:
            print(c(f"  ℹ️ PDF 解析: {_mineru_describe()}", INFO))
    except Exception as e:
        print(c(f"  ⚠️ MinerU 客户端不可用: {e}", WARN))
    try:
        import pypdfium2  # noqa: F401
        print(c("  ✅ 本地 PDF 解析(pypdfium2)可用", INFO))
    except Exception:
        print(c("  ⚠️ pypdfium2 未安装：MinerU 失败时无本地兜底", WARN))
    try:
        from markitdown import MarkItDown  # noqa: F401
        print(c("  ✅ 文档解析(markitdown)可用", INFO))
    except Exception:
        print(c("  ⚠️ markitdown 未安装：非 PDF 文档可能无法解析", WARN))

    # 5) 各主题库的向量健康度：零向量意味着"入库成功但检索必失真"
    try:
        from tools.builtin.paper_rag_tool import PaperRagTool
        tool = agent.paper_rag_tool
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue
        store = tool._get_store()
        pts, _ = store.client.scroll(
            collection_name=store.collection_name, limit=5000,
            with_payload=True, with_vectors=True,
        )
        by_ns: dict = {}
        for p in pts:
            ns = (p.payload or {}).get("rag_namespace", "(none)")
            vec = p.vector
            if isinstance(vec, dict):
                vec = list(vec.values())[0]
            try:
                import math
                norm = math.sqrt(sum(float(x) * float(x) for x in vec))
            except Exception:
                norm = 0.0
            d = by_ns.setdefault(ns, {"n": 0, "zero": 0})
            d["n"] += 1
            if norm < 1e-6:
                d["zero"] += 1
        if not by_ns:
            print(c("  ℹ️ 向量库为空（还没有任何主题库入库）", INFO))
        for ns, d in sorted(by_ns.items()):
            if d["zero"]:
                ok = False
                print(c(f"  ❌ 主题库 '{ns}': {d['n']} 个片段中有 {d['zero']} 个零向量（检索会失真，需重新入库）", ERROR))
            else:
                print(c(f"  ✅ 主题库 '{ns}': {d['n']} 个片段，向量均正常", INFO))
    except Exception as e:
        print(c(f"  ⚠️ 主题库巡检跳过: {e}", WARN))

    print(c("🩺 自检完成：" + ("全部通过" if ok else "存在问题，见上方 ❌"), PRIMARY if ok else ERROR))


def main(argv: list[str] | None = None) -> int:
    """
    CLI 入口点。
    初始化 LLM、CodeAgent（模式化 ReAct + 工具）与 ApplyPatchExecutor，并进入交互式循环。
    """
    # 1. 解析命令行参数
    parser = argparse.ArgumentParser(description="HelloAgents Academic Agent CLI (Claude Code/Codex-like，三模式科研辅助)")
    parser.add_argument("--repo", type=str, default=".", help="Repository root (workspace). Default: .")
    parser.add_argument("--project", type=str, default=None, help="Project name (default: repo folder name)")
    args = parser.parse_args(argv)

    # 2. 初始化环境和 LLM
    repo_root = Path(args.repo).resolve()
    load_dotenv(dotenv_path=repo_root / ".env", override=False)

    project = args.project or repo_root.name
    config = Config.from_env()
    llm = HelloAgentsLLM()  # auto-detect provider from env
    # reduce noisy HTTP client logs in the CLI
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("openai._base_client").setLevel(logging.WARNING)
    logging.getLogger("memory").setLevel(logging.WARNING)
    logging.getLogger().addFilter(_NoQdrantLocalNoise())

    print(c(hr("=", 80), INFO))
    print(c("HelloAgents Academic Agent CLI", PRIMARY))
    print(c(f"workspace: {repo_root}", INFO))
    print(c(f"LLM: provider={llm.provider} model={llm.model} base_url={llm.base_url}", INFO))
    print(c(f"state: {Path(config.helloagents_dir).as_posix()}", INFO))
    print(c(hr("=", 80), INFO))

    # Optional preflight to surface auth issues early.
    # 注意：max_tokens 不能给 1 —— 推理型模型（如 deepseek-v4-flash）的 reasoning
    # token 也计入该预算（实测 <128 就会返回空内容却"看起来通过"）。这里要求非空。
    try:
        _pre = llm.invoke([{"role": "user", "content": "ping"}], max_tokens=512)
        if not (_pre or "").strip():
            print(c("LLM 预检返回空内容：可能是推理型模型 token 预算不足，或配置异常。", WARN))
    except HelloAgentsException as e:
        print(c("LLM 预检失败（通常是 API key/base_url/model 配置问题）。", ERROR))
        print(c(f"error: {e}", ERROR))
        print(c("请检查 .env 中的 DEEPSEEK_API_KEY / LLM_* 配置是否正确。", WARN))
        return 2

    # 3. 初始化核心组件（ReAct + tools）
    agent = CodeAgent(repo_root=repo_root, llm=llm, config=config)
    patch_executor = ApplyPatchExecutor(repo_root=repo_root)

    def _print_help() -> None:
        print(c("\n命令（斜杠前缀）：", INFO))
        print(c("  /paper", ACCENT) + c("     论文解读模式：就已入库论文做带引用问答", INFO))
        print(c("  /research", ACCENT) + c("  检索分析模式：arxiv 检索→下载→入库→要点", INFO))
        print(c("  /code", ACCENT) + c("      代码助手模式：部署开源代码 / 辅助复现", INFO))
        print(c("  /lib <topic>", ACCENT) + c("  切换当前主题库（论文按库隔离）", INFO))
        print(c("  /status", ACCENT) + c("    查看当前模式与主题库", INFO))
        print(c("  /doctor", ACCENT) + c("    健康自检：LLM/向量库/embedding/零向量", INFO))
        print(c("  /plan <目标>", ACCENT) + c("  强制生成计划", INFO))
        print(c("  /help", ACCENT) + c("      显示本帮助", INFO))
        print(c("  /quit", ACCENT) + c("      退出", INFO))
        print(c(agent.mode_manager.describe(), INFO))
        print()

    # 4. 进入交互循环
    print(c(agent.current_mode(), ACCENT))
    print(c("输入自然语言需求开始；/help 查看全部命令。", INFO))
    while True:
        try:
            user_in = input(c("👤 > ", PRIMARY))
        except (EOFError, KeyboardInterrupt):
            print("\n" + c("bye", INFO))
            return 0

        if user_in is None:
            continue
        user_in = user_in.strip()
        if not user_in:
            print(c("请提供具体指令或问题。", WARN))
            continue

        # 退出（斜杠命令 + 兼容旧别名）
        if user_in in {"/quit", "/q", ":q", ":quit", "quit", "exit"}:
            print(c("bye", INFO))
            return 0

        # 斜杠命令
        if user_in.startswith("/"):
            parts = user_in.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""
            if cmd in {"/paper", "/research", "/code"}:
                print(c(agent.set_mode(cmd[1:]), PRIMARY))
            elif cmd == "/lib":
                print(c(agent.set_topic(arg), PRIMARY))
            elif cmd == "/status":
                print(c(agent.current_mode(), ACCENT))
            elif cmd == "/doctor":
                _run_doctor(agent, llm)
            elif cmd == "/help":
                _print_help()
            elif cmd == "/plan":
                goal = arg or "请为当前任务生成一个可执行计划"
                response = agent.plan_tool.run({"goal": goal})
                print("\n" + c("🤖 plan", PRIMARY))
                print(response + "\n")
            else:
                print(c(f"未知命令: {cmd}。用 /help 查看可用命令。", WARN))
            continue

        # 兼容旧别名 :plan
        if user_in.startswith(":plan"):
            goal = user_in[len(":plan") :].strip() or "请为当前任务生成一个可执行计划"
            response = agent.plan_tool.run({"goal": goal})
            print("\n" + c("🤖 plan", PRIMARY))
            print(response + "\n")
            continue

        # 5. 运行一轮对话（ReAct 可能按需调用终端/笔记/记忆）
        try:
            response = agent.run_turn(user_in)
        except HelloAgentsException as e:
            print(c(f"LLM 调用失败: {e}", ERROR))
            continue

        # 对于 direct reply（未经过 ReAct 的控制台打印），在 CLI 里补打一份输出
        if getattr(agent, "last_direct_reply", False):
            print(c("🤖 assistant", PRIMARY))
            print(response)
        
        # 7. 提取并应用补丁
        patch_text = _extract_patch(response)
        if not patch_text:
            continue
        patch_text = _normalize_patch(patch_text)
        # Ignore empty patch blocks
        if patch_text.strip() == "*** Begin Patch\n*** End Patch":
            continue

        needs_confirm = _patch_requires_confirmation(patch_text)
        if needs_confirm:
            # If user just answered y/n as the *current* input, treat it as confirmation for this patch.
            if user_in.strip().lower() in {"n", "no"}:
                print("已取消补丁应用。")
                continue
            if user_in.strip().lower() not in {"y", "yes"}:
                print("\n⚠️ 检测到高风险补丁（删除/大规模变更）。是否应用？(y/n)")
                ans = input("confirm> ").strip().lower()
                if ans not in {"y", "yes"}:
                    print("已取消补丁应用。")
                    continue

        try:
            res = patch_executor.apply(patch_text)
            print("\n" + c("✅ Patch applied", PRIMARY))
            print(c(f"files: {', '.join(res.files_changed) if res.files_changed else '(none)'}", INFO))
            if res.backups:
                print(c(f"backups: {len(res.backups)} (in .helloagents/backups/...)", INFO))

            # 记录到 NoteTool（action）
            agent.note_tool.run({
                "action": "create",
                "title": "Patch applied",
                "content": f"User input:\n{user_in}\n\nPatch:\n\n```text\n{patch_text}\n```\n\nFiles:\n"
                + "\n".join([f"- {p}" for p in res.files_changed]),
                "note_type": "action",
                "tags": [project, "patch_applied"],
            })
        except PatchApplyError as e:
            print("\n" + c(f"❌ Patch failed: {e}", ERROR))
            agent.note_tool.run({
                "action": "create",
                "title": "Patch failed",
                "content": f"Error: {e}\n\nUser input:\n{user_in}\n\nPatch:\n\n```text\n{patch_text}\n```\n",
                "note_type": "blocker",
                "tags": [project, "patch_failed"],
            })
            continue

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
