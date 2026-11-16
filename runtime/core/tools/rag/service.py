"""
总结服务类：用户提问，搜索参考资料，将提问和参考资料提交给模型，让模型总结回复。

检索器由 tools/rag/retrievers.py 按 config/rag.yml 的 retrieval_mode 创建：
默认 hybrid（向量 + BM25 关键词兜底），也可切换为 vector。
"""
from langchain_core.documents import Document
from langchain_core.prompts import PromptTemplate

from core.observability import latency
from core.observability.turn_usage import record_model_usage
from core.tools.rag.retrievers import get_retriever
from core.tools.rag.vector_store import VectorStoreService
from core.utils.logger_handler import logger
from core.utils.prompt_loader import load_rag_prompts


class RagSummarizeService:
    def __init__(self, retrieval_mode: str = None, retriever=None, where: dict | None = None):
        # The artifact's rag_index is copied into the instance workspace before
        # the worker starts.  Reconcile that private copy against the current
        # knowledge files so a stale or partial copy cannot silently omit a
        # source (for example a PDF that was indexed only as page-sized blocks).
        with latency.span("worker.rag_coverage"):
            try:
                self.coverage = VectorStoreService().ensure_coverage()
            except Exception as exc:  # noqa: BLE001 - keep retrieval usable on old index
                self.coverage = {
                    "schema_version": "rag-ingestion-coverage/v1",
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                logger.error(f"[知识库覆盖]启动检查失败：{exc}", exc_info=True)
        # retriever 可注入：评测用内存 Chroma，业务用持久化 Chroma
        with latency.span("worker.rag_service_init", retrieval_mode=retrieval_mode):
            self.retriever = retriever or get_retriever(retrieval_mode, where=where)
        self.prompt_text = load_rag_prompts()
        self.prompt_template = PromptTemplate.from_template(self.prompt_text)
        # 模型在第一次真正回答时才加载：资源目录 / 纯检索不需要 API key。
        self.port = None

    def _ensure_port(self):
        if self.port is None:
            with latency.span("model.load.rag_port"):
                from core.llm.factory import chat_model
                from core.llm.port import as_port

                self.port = as_port(chat_model)
        return self.port

    def retriever_docs(self, query: str) -> list[Document]:
        with latency.span("retrieval.total", query_chars=len(query or "")):
            return self.retriever.invoke(query)

    @staticmethod
    def _format_context(context_docs: list[Document]) -> str:
        context = ""
        for counter, doc in enumerate(context_docs, 1):
            context += f"【参考资料{counter}】: 参考资料：{doc.page_content} | 参考元数据：{doc.metadata}\n"
        return context

    def _answer(self, query: str, context_docs: list[Document]) -> str:
        import time
        import uuid

        prompt = self.prompt_template.format(
            input=query,
            context=self._format_context(context_docs),
        )
        started = time.perf_counter()
        call_id = f"rag_summary-{uuid.uuid4().hex[:12]}"
        with latency.bind_call(call_id, layer="rag_summary"):
            with latency.span(
                "rag.summary.invoke",
                call_id=call_id,
                method="rag_summarize",
                context_docs=len(context_docs),
            ):
                reply = self._ensure_port().invoke([("human", prompt)])
        record_model_usage(
            layer="rag_summary",
            usage=getattr(reply, "usage", None),
            model=getattr(self.port, "model", None).__class__.__name__
            if getattr(self.port, "model", None) is not None
            else None,
            latency_ms=int((time.perf_counter() - started) * 1000),
            call_id=call_id,
            method="rag_summarize",
        )
        return reply.text

    def rag_summarize_with_docs(self, query: str) -> tuple[str, list[Document]]:
        """一次检索同时返回答案和原始 docs，供评测读取 artifact。"""
        context_docs = self.retriever_docs(query)
        return self._answer(query, context_docs), context_docs

    def rag_summarize(self, query: str) -> str:
        return self.rag_summarize_with_docs(query)[0]


if __name__ == '__main__':
    rag = RagSummarizeService()

    print(rag.rag_summarize("小户型适合哪些扫地机器人"))
