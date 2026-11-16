# -*- coding: utf-8 -*-
"""RAG 工具：检索与总结。

- `rag_summarize`：检索 + LLM 总结，业务场景使用；
- `rag_retrieve`：只返回原始检索片段，评测/调试使用；
- 两者都返回 `content_and_artifact`：模型只看到 content，评测从 artifact 读取
  docs / rank / source，不污染模型上下文。
"""
from __future__ import annotations

import os

from langchain_core.tools import tool

from core.tools.rag.evidence import record_retrieval, timed
from core.tools.rag.service import RagSummarizeService


class RetrievalTimeout(RuntimeError):
    """The retrieval call exceeded its configured budget."""


def _source_name(doc) -> str:
    return os.path.basename(str((doc.metadata or {}).get("source", "")))


def _artifact(query: str, docs) -> dict:
    return {
        "query": query,
        "docs": [
            {
                "rank": index,
                "source": _source_name(doc),
                "chunk_id": (doc.metadata or {}).get("chunk_id"),
                "content": doc.page_content,
                "metadata": doc.metadata or {},
            }
            for index, doc in enumerate(docs, 1)
        ],
    }


def _unique_sources(docs) -> list[str]:
    sources: list[str] = []
    for doc in docs:
        name = _source_name(doc)
        if name and name not in sources:
            sources.append(name)
    return sources


def _with_sources(answer: str, docs) -> str:
    """Expose retrieval sources to the model, not only to the tool artifact.

    ``content_and_artifact`` keeps raw docs out of the model context, but a
    knowledge-base answer still has to cite where it came from.  Append the
    de-duplicated source file names so ReAct can produce a real citation
    instead of inventing one.
    """
    sources = _unique_sources(docs)
    if not sources:
        return answer
    lines = "\n".join(f"- [{name}]" for name in sources)
    return f"{answer}\n\n参考来源：\n{lines}"


def build_rag_tools(
    service: RagSummarizeService | None = None,
    *,
    where: dict | None = None,
    summarize_name: str = "rag_summarize",
    retrieve_name: str = "rag_retrieve",
):
    """按 service / where 构造 RAG 工具；名字可覆盖，便于同一 toolbox 放多套域。

    ``where={"domain": "clothing"}`` 时，向量检索和 BM25 语料都只取服装分块；
    不传则查全库（通用调试用）。
    """
    service = service or RagSummarizeService(where=where)

    @tool(
        summarize_name,
        description="从本地知识库检索参考资料并总结回答；回答末尾会列出参考来源文件名",
        response_format="content_and_artifact",
    )
    def rag_summarize(query: str):
        started = timed()
        try:
            answer, docs = service.rag_summarize_with_docs(query)
        except TimeoutError as exc:
            record_retrieval(
                tool=summarize_name,
                query=query,
                status="timeout",
                error=str(exc) or "检索超时",
                elapsed_ms=(timed() - started) * 1000,
            )
            raise RetrievalTimeout("知识库检索超时，请稍后重试；本轮没有可用来源。") from exc
        except Exception as exc:  # noqa: BLE001 - record then re-raise, never fake success
            record_retrieval(
                tool=summarize_name,
                query=query,
                status="error",
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=(timed() - started) * 1000,
            )
            raise
        record_retrieval(
            tool=summarize_name,
            query=query,
            docs=docs,
            status="ok" if docs else "empty",
            elapsed_ms=(timed() - started) * 1000,
        )
        if not docs:
            answer = "知识库中没有检索到与该问题相关的资料。"
            return answer, _artifact(query, docs)
        return _with_sources(answer, docs), _artifact(query, docs)

    @tool(
        retrieve_name,
        description="检索知识库并返回带来源文件名的原始资料片段（评测用）",
        response_format="content_and_artifact",
    )
    def rag_retrieve(query: str, k: int = 4):
        started = timed()
        try:
            docs = service.retriever_docs(query)[:k]
        except TimeoutError as exc:
            record_retrieval(
                tool=retrieve_name,
                query=query,
                status="timeout",
                error=str(exc) or "检索超时",
                elapsed_ms=(timed() - started) * 1000,
            )
            raise RetrievalTimeout("知识库检索超时，请稍后重试；本轮没有可用来源。") from exc
        except Exception as exc:  # noqa: BLE001 - record then re-raise
            record_retrieval(
                tool=retrieve_name,
                query=query,
                status="error",
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=(timed() - started) * 1000,
            )
            raise
        record_retrieval(
            tool=retrieve_name,
            query=query,
            docs=docs,
            status="ok" if docs else "empty",
            elapsed_ms=(timed() - started) * 1000,
        )
        content = "\n\n".join(
            f"【来源：{_source_name(doc)}】\n{doc.page_content}" for doc in docs
        )
        return content, _artifact(query, docs)

    return rag_summarize, rag_retrieve
