"""主题库(topic)命名规范化 - 三处入口共用同一规则。

topic 同时被用作 papers/<topic>/ 目录名与 Qdrant 向量库的 rag_namespace。
此前 arxiv 工具会清洗、paper_rag 工具不清洗、CLI 的 /lib 也不清洗，
同一方向可能落到不同名字上（目录一个、向量库 namespace 另一个），
导致"下载在这、检索在那"地互相找不到。这里统一出口。
"""

from __future__ import annotations

import re

DEFAULT_TOPIC = "default"
_TOPIC_RE = re.compile(r"[^A-Za-z0-9_\-\.]")


def sanitize_topic(topic: object, fallback: str = DEFAULT_TOPIC) -> str:
    """把任意输入规范成安全的 topic 名；空输入回退 fallback。"""
    t = _TOPIC_RE.sub("_", str(topic if topic is not None else "").strip())
    t = t.strip("._-") or fallback
    return t
