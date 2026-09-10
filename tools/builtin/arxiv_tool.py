"""arxiv 检索/下载工具 - 任务2（按方向检索 arxiv 论文并下载到主题库）

设计要点：
- 依赖 `arxiv` PyPI 包（>=2.0，Client/Search API），在方法内惰性导入，
  未安装时构造工具不会失败，只有调用 action 时才提示安装。
- 下载的 PDF 落到 papers_root/<topic>/，topic 缺省取共享 SessionState.current_topic（鸭子类型，
  不强依赖 modes 模块，避免 tools/ 反向依赖 code_agent/）。
"""

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List

from ..base import Tool, ToolParameter
from utils.topic import sanitize_topic


# arxiv API 的查询语法中，裸词按 OR/松散匹配处理。过去把自然语言原样拼进
# `cat:X AND (query)`，配 sort_by=SubmittedDate 时就等于"把该分类最新投稿里
# 沾到任一词的全捞出来"，返回大量无关论文（实测会把世界模型/水稻分类当成
# "遥感 VLM"）。这里对自然语言查询逐词加引号并用 AND 连接，保证命中所有词。
_QUERY_RAW_RE = re.compile(r":|\b(?:AND|OR|ANDNOT)\b", re.IGNORECASE)
_STOPWORDS = {
    "a", "an", "the", "of", "for", "in", "on", "with", "about", "to", "and",
    "or", "using", "based", "recent", "latest", "new", "paper", "papers",
    "article", "articles", "study", "studies",
}


def build_arxiv_query(query: str, category: str = "") -> str:
    """把自然语言查询转成 arxiv 检索式。

    - 输入已含 arxiv 语法（字段前缀 / AND / OR）时原样透传，尊重调用方意图；
      若其中已自带 cat: 限定，则不再重复包裹 category。
    - 否则逐词加引号并用 AND 连接（去掉常见停用词），显著提升精确度。
    """
    q = (query or "").strip()
    if not q:
        return ""
    if _QUERY_RAW_RE.search(q):
        if re.search(r"\bcat:", q, re.IGNORECASE) or not category:
            return q
        return f"cat:{category} AND ({q})"
    terms = []
    for tok in re.findall(r'"[^"]*"|\S+', q):
        if tok.startswith('"') and tok.endswith('"'):
            terms.append(tok)
        else:
            t = tok.strip().rstrip(",").strip()
            if t and t.lower() not in _STOPWORDS:
                terms.append(f'"{t}"')
    if not terms:
        terms = [f'"{q}"']
    expr = " AND ".join(terms)
    return f"cat:{category} AND ({expr})" if category else expr


class ArxivTool(Tool):
    """arxiv 论文检索与下载工具。

    actions:
      - search   : 按方向/关键词检索（可选 category 过滤，默认 cs.CV）
      - download : 下载指定 arxiv_id 的 PDF 到主题库目录
      - metadata : 查看单篇详情
    """

    def __init__(
        self,
        papers_root: Any,
        session_state: Any = None,
        default_category: str = "cs.CV",
        default_max_results: int = 8,
    ):
        super().__init__(
            name="arxiv",
            description=(
                "arxiv 论文检索与下载工具。action=search 按方向检索(query，可选 category 如 cs.CV；"
                "可选 sort_by=SubmittedDate 找最新/今日论文，配 since_days 过滤最近 N 天)；"
                "action=download 下载指定 arxiv_id 的 PDF 到主题库目录；action=metadata 查看单篇详情。"
                "下载后用 paper_rag(action=index) 入库即可问答。"
            ),
        )
        self.papers_root = Path(papers_root)
        self.session_state = session_state
        self.default_category = default_category
        self.default_max_results = default_max_results
        self._client = None

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="action", type="string",
                          description="操作：search(检索)/download(下载PDF)/metadata(单篇详情)", required=True),
            ToolParameter(name="query", type="string", description="search 检索词（研究方向/关键词）", required=False),
            ToolParameter(name="arxiv_id", type="string", description="download/metadata 的论文 id（如 2401.12345）", required=False),
            ToolParameter(name="category", type="string", description="arxiv 分类过滤（默认 cs.CV；留空则不限）", required=False, default="cs.CV"),
            ToolParameter(name="max_results", type="integer", description="search 返回数量（默认8）", required=False, default=8),
            ToolParameter(name="sort_by", type="string",
                          description="search 排序：Relevance(相关度,默认)/SubmittedDate(最新提交)/LastUpdatedDate(最新更新)；找今日/最新论文用 SubmittedDate",
                          required=False),
            ToolParameter(name="since_days", type="integer",
                          description="只保留最近 N 天提交的论文（可选；配合 sort_by=SubmittedDate 使用）",
                          required=False),
            ToolParameter(name="topic", type="string", description="download 保存到哪个主题库目录；缺省用当前主题库", required=False),
        ]

    # ---------- 内部辅助 ----------
    def _client_get(self):
        if self._client is None:
            import arxiv
            self._client = arxiv.Client()
        return self._client

    def _resolve_topic(self, params: Dict[str, Any]) -> str:
        t = params.get("topic") or getattr(self.session_state, "current_topic", None) or "default"
        return sanitize_topic(t)

    def _fmt(self, r) -> str:
        authors_list = list(getattr(r, "authors", []) or [])
        authors = ", ".join(a.name for a in authors_list[:4])
        if len(authors_list) > 4:
            authors += " et al."
        published = getattr(r, "published", None)
        # 暴露完整提交日期：只给年份时模型无法判断"最近一篇"，只能靠 arxiv ID 猜月份（易错一年）。
        published_str = published.strftime("%Y-%m-%d") if published else "unknown"
        year = getattr(published, "year", "")
        sid = r.get_short_id()
        cats = ",".join(getattr(r, "categories", []) or [])
        summary = re.sub(r"\s+", " ", (getattr(r, "summary", "") or "")).strip()
        return (
            f"[{sid}] {r.title}\n"
            f"    作者: {authors} | 提交: {published_str} ({year}) | {cats}\n"
            f"    摘要: {summary[:300]}...\n"
            f"    PDF: {r.pdf_url}"
        )

    def _by_id(self, arxiv_id: str):
        import arxiv
        s = arxiv.Search(id_list=[arxiv_id])
        return next(self._client_get().results(s), None)

    # ---------- Tool 接口 ----------
    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return "❌ 参数验证失败：缺少必需的 action"
        action = (parameters.get("action") or "").strip().lower()
        try:
            import arxiv  # noqa: F401
        except ImportError:
            return "❌ 未安装 arxiv 包，请运行: pip install 'arxiv>=2.0'"
        try:
            if action == "search":
                return self._search(parameters)
            if action == "download":
                return self._download(parameters)
            if action == "metadata":
                return self._metadata(parameters)
            return f"不支持的操作: {action}。支持: search, download, metadata"
        except Exception as e:
            return f"❌ arxiv {action} 失败: {e}"

    def _search(self, params: Dict[str, Any]) -> str:
        import arxiv
        query = (params.get("query") or "").strip()
        if not query:
            return "❌ search 需要 query"
        category = params.get("category")
        if category is None:
            category = self.default_category
        max_results = int(params.get("max_results") or self.default_max_results)
        # 排序：默认相关度；找"今日/最新"论文时模型可传 sort_by=SubmittedDate
        sort_name = (str(params.get("sort_by") or "Relevance")).strip().lower().replace("_", "").replace("-", "")
        sort_by = {
            "relevance": arxiv.SortCriterion.Relevance,
            "lastupdateddate": arxiv.SortCriterion.LastUpdatedDate,
            "submitteddate": arxiv.SortCriterion.SubmittedDate,
        }.get(sort_name, arxiv.SortCriterion.Relevance)
        q = build_arxiv_query(query, category or "")
        s = arxiv.Search(
            query=q,
            max_results=max_results,
            sort_by=sort_by,
            sort_order=arxiv.SortOrder.Descending,
        )
        results = list(self._client_get().results(s))
        # 可选：只保留最近 N 天提交的论文（客户端侧按 published 字段过滤）
        since_days = params.get("since_days")
        if since_days:
            try:
                cutoff = datetime.now() - timedelta(days=int(since_days))
                results = [r for r in results if getattr(r, "published", None) and r.published >= cutoff]
            except Exception:
                pass
        if not results:
            extra = f", sort_by={sort_name}" + (f", since_days={since_days}" if since_days else "")
            return f"🔍 arxiv 未检索到: {query} (category={category or '不限'}{extra})\n检索式: {q}"
        out = [f"arxiv 检索 '{query}' (cat={category or '不限'}) 命中 {len(results)} 篇（按 {sort_name} 排序）：\n"]
        for r in results:
            out.append(self._fmt(r))
        out.append("\n下一步：arxiv[action=download, arxiv_id=<id>, topic=<主题库>] 下载，再 paper_rag[action=index] 入库。")
        return "\n".join(out)

    def _download(self, params: Dict[str, Any]) -> str:
        aid = (params.get("arxiv_id") or "").strip()
        if not aid:
            return "❌ download 需要 arxiv_id"
        r = self._by_id(aid)
        if r is None:
            return f"❌ 未找到 arxiv_id={aid}"
        topic = self._resolve_topic(params)
        topic_dir = self.papers_root / topic
        topic_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{r.get_short_id().replace('/', '_')}.pdf"
        path = topic_dir / filename
        # arxiv 包内部用 urllib + 系统默认 CA，在部分环境（如 Windows 上
        # OpenSSL 只认不到 cert.pem 时）会 CERTIFICATE_VERIFY_FAILED。
        # 这里优先用 requests（自带 certifi），失败再回退到 arxiv 自带下载。
        try:
            import requests
            resp = requests.get(
                r.pdf_url, timeout=120,
                headers={"User-Agent": "HelloAcademicAgent/arxiv-downloader"},
            )
            resp.raise_for_status()
            if not (resp.content or b"").startswith(b"%PDF"):
                raise RuntimeError("返回内容不是 PDF（可能是限流页）")
            path.write_bytes(resp.content)
            path = str(path)
        except Exception as dl_err:
            try:
                path = r.download_pdf(dirpath=str(topic_dir), filename=filename)
            except Exception as second_err:
                return (
                    f"❌ 下载失败: {dl_err}\n"
                    f"    回退 arxiv 自带下载也失败: {second_err}\n"
                    f"    提示: 证书校验问题可尝试设置环境变量 SSL_CERT_FILE=<certifi 的 cacert.pem 路径>"
                )
        return (
            f"✅ 已下载: {path}\n主题库: {topic}\n标题: {r.title}\n"
            f"下一步: paper_rag[action=index, paths=[\"{path}\"], topic=\"{topic}\"]"
        )

    def _metadata(self, params: Dict[str, Any]) -> str:
        aid = (params.get("arxiv_id") or "").strip()
        if not aid:
            return "❌ metadata 需要 arxiv_id"
        r = self._by_id(aid)
        if r is None:
            return f"❌ 未找到 arxiv_id={aid}"
        return self._fmt(r)
