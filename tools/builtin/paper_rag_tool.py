"""论文 RAG 工具 - 包装 memory/rag/pipeline.py，按主题库(namespace)索引与问答

设计要点：
- 复用现成 RAG 流水线（PDF→分块→embedding→Qdrant→检索→重排→引用），不重写。
- 单一 Qdrant store 实例（经 QdrantConnectionManager 单例），避免嵌入式本地模式下的文件锁冲突；
  主题库通过 rag_namespace 参数在每次操作时传入，而非每个主题各建一个 store。
- 所有重依赖（pipeline / qdrant / embedding）均在方法内惰性导入，
  这样即便依赖未安装，构造工具/导入模块本身也不会失败，只在真正调用 action 时才需要依赖。
"""

import os
from typing import Dict, Any, List

from ..base import Tool, ToolParameter
from utils.topic import sanitize_topic


class PaperRagTool(Tool):
    """论文知识库(RAG)工具：按主题库索引本地 PDF 论文，并就该库做带引用的问答。

    actions:
      - index : 把 PDF 路径入库到某主题库(namespace)
      - ask   : 就某主题库检索问答，返回带 [n] 引用的证据片段
      - list  : 列出某主题库已索引的论文
      - stats : 查看向量库统计
    """

    def __init__(
        self,
        collection_name: str = "research_papers",
        session_state: Any = None,
        default_top_k: int = 8,
        local_path: Any = None,
    ):
        super().__init__(
            name="paper_rag",
            description=(
                "论文知识库(RAG)工具：把本地 PDF 论文按主题库索引，并就该库做带引用的问答。"
                "action=index 入库(需 paths)；action=ask 检索问答(需 query)；"
                "action=list 列出某主题库论文；action=stats 向量库统计。"
                "topic 指定主题库(缺省用当前主题库)。答案务必基于返回的引用证据。"
            ),
        )
        self.collection_name = collection_name
        self.session_state = session_state
        self.default_top_k = default_top_k
        # 嵌入式本地存储路径（免 Docker）；为空则回退到环境变量/服务/云
        self.local_path = str(local_path) if local_path else None
        self._store = None

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="action", type="string",
                description="操作：index(入库)/ask(问答,带引用)/list(列出主题库论文)/stats(统计)",
                required=True,
            ),
            ToolParameter(name="query", type="string", description="ask 时的检索问题", required=False),
            ToolParameter(name="paths", type="array", description="index 时的 PDF 路径列表(也可用;分隔的字符串)", required=False),
            ToolParameter(name="topic", type="string", description="主题库名(=namespace)；缺省用当前主题库", required=False),
            ToolParameter(name="top_k", type="integer", description="ask 返回片段数(默认8)", required=False, default=8),
            ToolParameter(name="expand", type="boolean", description="ask 是否启用 MQE/HyDE 查询扩展(默认false)", required=False, default=False),
        ]

    # ---------- 内部辅助 ----------
    def _resolve_topic(self, params: Dict[str, Any]) -> str:
        t = params.get("topic") or getattr(self.session_state, "current_topic", None) or "default"
        # 与 arxiv 工具/CLI 共用同一清洗规则，避免目录名与 namespace 不一致。
        return sanitize_topic(t)

    def _get_store(self):
        """惰性获取单一 Qdrant store（连接管理器单例，兼容嵌入式本地/服务/云）。"""
        if self._store is None:
            from memory.storage.qdrant_store import QdrantConnectionManager
            from memory.embedding import get_dimension
            dim = get_dimension(384)
            self._store = QdrantConnectionManager.get_instance(
                url=os.getenv("QDRANT_URL"),
                api_key=os.getenv("QDRANT_API_KEY"),
                collection_name=self.collection_name,
                vector_size=dim,
                distance="cosine",
                local_path=self.local_path,
            )
        return self._store

    # ---------- Tool 接口 ----------
    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return "❌ 参数验证失败：缺少必需的 action"
        action = (parameters.get("action") or "").strip().lower()
        try:
            if action == "index":
                return self._index(parameters)
            if action in ("ask", "search"):
                return self._ask(parameters)
            if action == "list":
                return self._list(parameters)
            if action == "stats":
                return self._stats()
            return f"不支持的操作: {action}。支持: index, ask, list, stats"
        except Exception as e:
            return f"❌ paper_rag {action} 失败: {e}"

    def _index(self, params: Dict[str, Any]) -> str:
        from memory.rag.pipeline import load_and_chunk_texts, index_chunks

        paths = params.get("paths")
        if isinstance(paths, str):
            paths = [p.strip() for p in paths.split(";") if p.strip()]
        if not paths:
            return "❌ index 需要 paths（PDF 路径列表，或用 ; 分隔的字符串）"
        topic = self._resolve_topic(params)
        store = self._get_store()
        chunks = load_and_chunk_texts(
            paths=list(paths), chunk_size=800, chunk_overlap=100,
            namespace=topic, source_label="paper",
        )
        if not chunks:
            return f"⚠️ 未从 {len(paths)} 个文件提取到内容（检查路径是否正确/PDF 是否为空/解析依赖是否安装）"
        index_chunks(store=store, chunks=chunks, rag_namespace=topic)
        docs = sorted({c["metadata"].get("source_path") for c in chunks if c.get("metadata")})
        return (
            f"✅ 已入库到主题库 '{topic}'：{len(chunks)} 个片段，来自 {len(docs)} 篇文档。\n"
            + "\n".join(f"- {d}" for d in docs)
        )

    def _ask(self, params: Dict[str, Any]) -> str:
        from memory.rag.pipeline import (
            search_vectors, search_vectors_expanded,
            compute_graph_signals_from_pool, rank, merge_snippets_grouped,
        )

        query = (params.get("query") or "").strip()
        if not query:
            return "❌ ask 需要 query"
        topic = self._resolve_topic(params)
        top_k = int(params.get("top_k") or self.default_top_k)
        expand = bool(params.get("expand"))
        store = self._get_store()
        pool = max(top_k * 3, 20)
        if expand:
            hits = search_vectors_expanded(
                store=store, query=query, top_k=pool, rag_namespace=topic,
                only_rag_data=True, enable_mqe=True, mqe_expansions=2, enable_hyde=True,
            )
        else:
            hits = search_vectors(
                store=store, query=query, top_k=pool, rag_namespace=topic, only_rag_data=True,
            )
        if not hits:
            current = sanitize_topic(getattr(self.session_state, "current_topic", None))
            hint = (
                f"当前主题库是 '{current}'；若你传的 topic 与入库时不同，请改用 list 确认。"
                if topic != current else
                "该库可能尚未入库：可先用 list 查看，或在 /research 下载后用 index 入库。"
            )
            return f"🔍 主题库 '{topic}' 中没有检索到相关内容。{hint}"
        graph = compute_graph_signals_from_pool(hits)
        ranked = rank(hits, graph)
        text = merge_snippets_grouped(ranked[: top_k * 2], max_chars=2500, include_citations=True)
        return f"[主题库 {topic}] 检索证据（含引用，回答时请标注 [n]）：\n\n{text}"

    def _list(self, params: Dict[str, Any]) -> str:
        topic = self._resolve_topic(params)
        store = self._get_store()
        try:
            from qdrant_client.http.models import Filter, FieldCondition, MatchValue
            flt = Filter(must=[FieldCondition(key="rag_namespace", match=MatchValue(value=topic))])
            points, _ = store.client.scroll(
                collection_name=store.collection_name, scroll_filter=flt,
                limit=2000, with_payload=True,
            )
        except Exception as e:
            return f"⚠️ 无法列出主题库 '{topic}': {e}"
        by_doc: Dict[str, int] = {}
        for p in points:
            payload = p.payload or {}
            sp = payload.get("source_path") or payload.get("doc_id") or "unknown"
            by_doc[sp] = by_doc.get(sp, 0) + 1
        if not by_doc:
            return f"主题库 '{topic}' 还没有索引任何论文。"
        lines = [f"主题库 '{topic}' 已索引 {len(by_doc)} 篇："]
        for sp, n in sorted(by_doc.items()):
            lines.append(f"- {sp}  ({n} 片段)")
        return "\n".join(lines)

    def _stats(self) -> str:
        store = self._get_store()
        return f"向量库统计: {store.get_collection_stats()}"
