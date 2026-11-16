"""向量库服务：文件解析 → 按类型切片 → 分批写入 Chroma。

与基础项目 KnowledgeBase-RAG-LLM-System 对齐的关键点：
1. 支持 txt / pdf / docx / md 四种格式；
2. 每种文件类型使用自己的切片参数（config/chroma.yml）；
3. md5 去重记录带来源文件名（`md5\\t文件名`），同一文件内容变化时替换旧分块；
4. 兼容 DashScope text-embedding-v4 的单批 10 条限制；本地 BGE 同样分批写入。
"""
import hashlib
import os
import re
import uuid
from collections import Counter
from datetime import datetime
from typing import Any

from langchain_chroma import Chroma
from langchain_text_splitters import RecursiveCharacterTextSplitter

from core.observability import latency
from core.tools.rag.classification import classify
from core.tools.rag.embedding import embed_model
from core.utils.config_handler import chroma_conf, rag_conf
from core.utils.file_handler import (
    get_bytes_md5_hex,
    get_file_type,
    listdir_with_allowed_type,
    parse_document,
)
from core.utils.logger_handler import logger
from core.utils.path_tool import get_abs_path

# 批量写入：兼容 DashScope 单批 10 条限制（本地 BGE 也可用）
EMBEDDING_BATCH_SIZE = 10

# 入库结构版本：写入分块 metadata，便于排查“索引存在但切片口径过旧”的情况。
INGESTION_SCHEMA = "numbered-faq-v1"


def _sample_evenly(chunks: list[str], limit: int) -> list[str]:
    """按位置均匀取样；首尾必留，保持源文档的结构覆盖。"""
    if len(chunks) <= limit:
        return chunks
    picked = []
    for index in range(limit):
        position = round(index * (len(chunks) - 1) / max(1, limit - 1))
        if position not in picked:
            picked.append(position)
    return [chunks[position] for position in picked]


_FAQ_ENTRY = re.compile(r"(?m)^(?=\s*\d+[.、]\s*)")


def _split_numbered_entries(text: str, *, min_entries: int = 3) -> list[str]:
    """按 ``1.`` / ``2.、`` 这类编号条目切 FAQ；不满足时返回空列表。

    100问 / 故障排除这类文档的编号条目是天然语义边界。按 1000 字切片会把
    多条问答塞进一个 chunk，检索时整块命中，单条事实反而被稀释。
    """
    parts = [part.strip() for part in _FAQ_ENTRY.split(text) if part.strip()]
    if len(parts) < min_entries:
        return []
    # 每条问答单独成块；不合并短文，否则编号边界又会被抹掉。
    return parts


def _numbered_faq_entries(text: str) -> list[str]:
    """Generic numbered-entry segmentation for FAQ-shaped documents.

    The old rule was keyed on a ``.txt`` filename whitelist.  PDF FAQ documents
    (for example ``扫地机器人100问.pdf``) have the same numbered structure but
    were left as page-sized chunks, so a single fact was diluted inside a block
    that covers 7-10 unrelated questions.  Detect the structure from the text
    itself, independent of file type or filename, and keep a density guard so a
    prose document with a short list is not split into fake FAQ entries.
    """
    entries = _split_numbered_entries(text, min_entries=8)
    if not entries:
        return []
    lines = [line for line in text.splitlines() if line.strip()]
    numbered = sum(1 for line in lines if re.match(r"\s*\d+[.、]\s*", line))
    if not lines or numbered / len(lines) < 0.2:
        return []
    return entries


def _chunk_multiset(chunks: list[str]) -> Counter:
    """Order-independent chunk identity (whitespace-insensitive)."""
    return Counter(re.sub(r"\s+", "", str(chunk or "")) for chunk in chunks)


class VectorStoreService:
    def __init__(self):
        self.persist_directory = get_abs_path(chroma_conf["persist_directory"])
        os.makedirs(self.persist_directory, exist_ok=True)

        with latency.span("retrieval.chroma_open", collection=chroma_conf["collection_name"]):
            self.vector_store = Chroma(
                collection_name=chroma_conf["collection_name"],
                embedding_function=embed_model,
                persist_directory=self.persist_directory,
            )

        # 按文件类型缓存的切片器：不同类型使用 config/chroma.yml 中各自的参数
        self._splitters: dict[str, RecursiveCharacterTextSplitter] = {}

    # ===================== 切片器 =====================
    def _get_splitter(self, file_type: str) -> RecursiveCharacterTextSplitter:
        """按文件类型返回切片器；同一类型只创建一次（惰性缓存）。"""
        chunking_conf = chroma_conf["chunking"]
        if file_type not in chunking_conf:
            # 未在 YAML 中配置的类型回退到 txt 的切片参数
            file_type = "txt"

        if file_type not in self._splitters:
            chunk_conf = chunking_conf[file_type]
            self._splitters[file_type] = RecursiveCharacterTextSplitter(
                chunk_size=chunk_conf["chunk_size"],
                chunk_overlap=chunk_conf["chunk_overlap"],
                separators=chunk_conf["separators"],
                length_function=len,
            )
        return self._splitters[file_type]

    def split_text(
        self,
        data: str,
        file_type: str,
        domain: str | None = None,
        source: str | None = None,
    ) -> list[str]:
        """按文件类型切片；短文本整段入库，避免被切得过碎。

        服装域文档很小，但希望保留语义段落而不是整篇一个 chunk：
        用更小的 separator 切片，再按位置均匀取样（首/尾必留），
        这样既覆盖全文结构，又不用把每篇都塞进向量库。

        FAQ / 故障排除 / PDF 100问这类编号文档优先按编号条目切，避免 1000 字
        chunk 把多条问答混在一起，导致检索只命中宽泛块、单条事实被稀释。
        结构判断只看抽取后的正文，不依赖文件名或扩展名。
        """
        if domain == "clothing":
            chunks = [
                item.strip()
                for item in RecursiveCharacterTextSplitter(
                    chunk_size=420,
                    chunk_overlap=50,
                    separators=["\n\n", "\n", "。", "；", " ", ""],
                    length_function=len,
                ).split_text(data)
                if item.strip()
            ]
            return _sample_evenly(chunks, 6)
        entries = _numbered_faq_entries(data)
        if entries:
            return entries
        if len(data) <= int(chroma_conf["max_split_char_number"]):
            return [data]
        return self._get_splitter(file_type).split_text(data)

    # ===================== 检索器 =====================
    def get_retriever(self, k: int = None, where: dict | None = None):
        """纯向量检索器；``where`` 是 Chroma metadata 过滤（如 {"domain":"clothing"}）。"""
        search_kwargs: dict = {"k": int(k or rag_conf["retrieval_k"])}
        if where:
            search_kwargs["filter"] = dict(where)
        return self.vector_store.as_retriever(search_kwargs=search_kwargs)

    # ===================== md5 去重记录 =====================
    @property
    def md5_store_path(self) -> str:
        return get_abs_path(chroma_conf["md5_hex_store"])

    def _read_md5_records(self) -> list[tuple[str, str]]:
        """读取 md5 记录，返回 [(md5, 来源文件), ...]；兼容旧格式（只有 md5）。"""
        if not os.path.exists(self.md5_store_path):
            return []
        records = []
        with open(self.md5_store_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if "\t" in line:
                    md5_str, source = line.split("\t", 1)
                else:
                    md5_str, source = line, ""
                records.append((md5_str, source))
        return records

    def _write_md5_records(self, records: list[tuple[str, str]]):
        os.makedirs(os.path.dirname(self.md5_store_path), exist_ok=True)
        with open(self.md5_store_path, "w", encoding="utf-8") as f:
            for md5_str, source in records:
                f.write(f"{md5_str}\t{source}\n" if source else f"{md5_str}\n")

    def check_md5(self, md5_str: str, source: str = "") -> bool:
        """同一来源下内容是否已处理过。旧格式记录没有来源，按全局去重处理。"""
        for old_md5, old_source in self._read_md5_records():
            if old_md5 == md5_str and (not old_source or old_source == source):
                return True
        return False

    def save_md5(self, md5_str: str, source: str = ""):
        """记录 md5 与来源；同一来源的旧记录会被覆盖。"""
        records = [
            (old_md5, old_source)
            for old_md5, old_source in self._read_md5_records()
            if old_source != source
        ]
        records.append((md5_str, source))
        self._write_md5_records(records)

    # ===================== 入库 =====================
    def _add_texts_in_batches(self, texts: list[str], metadatas: list[dict], ids: list[str]):
        """分批写入向量库；任一批失败会回滚本文件已写入的新分块。"""
        written_ids: list[str] = []
        try:
            for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
                end = start + EMBEDDING_BATCH_SIZE
                batch_ids = ids[start:end]
                self.vector_store.add_texts(
                    texts[start:end],
                    metadatas=metadatas[start:end],
                    ids=batch_ids,
                )
                written_ids.extend(batch_ids)
                logger.info(f"[加载知识库]已写入 {end if end < len(texts) else len(texts)}/{len(texts)} 个分块")
        except Exception:
            # 新内容没有完整写入：删掉已写的部分，旧分块仍然保留，下次可安全重试
            if written_ids:
                self.vector_store.delete(ids=written_ids)
            raise

    def _source_chunks(self, source: str) -> dict[str, Any]:
        """Read the current private-index chunks for one source file."""
        return self.vector_store.get(
            where={"source": source},
            include=["documents", "metadatas"],
        )

    def ensure_coverage(self) -> dict[str, Any]:
        """Make the private index cover every current knowledge source.

        The artifact copies a read-only index into the instance workspace.  That
        copy can be stale relative to ``data/knowledge`` (old chunking, a source
        added after the index was built, or a partially copied index).  This
        method reconciles the *private* copy only:

        * existing source chunks are compared by whitespace-insensitive content;
        * a missing or structurally different source is re-embedded and swapped
          atomically (new chunks first, old ids deleted only after success);
        * an unchanged source is not re-embedded, so restart does not duplicate;
        * a source file missing from disk never deletes its prior chunks; it is
          reported instead of silently pretending the index is complete.

        The caller must never point this at the protected source ``rag_index``;
        instance startup has already copied it to the writable workspace.
        """
        data_path = get_abs_path(chroma_conf["data_path"])
        allowed_files_path = listdir_with_allowed_type(data_path, chroma_conf["allow_file_types"])
        before = self.vector_store.get(include=["metadatas"])
        report: dict[str, Any] = {
            "schema_version": "rag-ingestion-coverage/v1",
            "data_path": str(data_path),
            "ingestion_schema": INGESTION_SCHEMA,
            "files_on_disk": [os.path.basename(path) for path in allowed_files_path],
            "chunks_before": len(before.get("ids") or []),
            "unchanged": [],
            "reindexed": [],
            "failed": [],
            "missing_persisted_sources": [],
        }
        if not allowed_files_path:
            logger.warning(f"[知识库覆盖]{data_path} 下没有可加载的文档")

        disk_sources = {os.path.basename(path) for path in allowed_files_path}
        for path in allowed_files_path:
            filename = os.path.basename(path)
            try:
                with open(path, "rb") as handle:
                    content = handle.read()
                md5_hex = get_bytes_md5_hex(content)
                file_type = get_file_type(filename)
                text = parse_document(content, filename)
                if not text.strip():
                    raise ValueError("文档没有可提取的文字")
                doc_class = classify(filename, text)
                knowledge_chunks = self.split_text(
                    text, file_type, domain=doc_class.domain, source=filename
                )
                if not knowledge_chunks:
                    raise ValueError("文档切片后没有有效文本")

                existing = self._source_chunks(filename)
                old_ids = list(existing.get("ids") or [])
                old_chunks = list(existing.get("documents") or [])
                if _chunk_multiset(old_chunks) == _chunk_multiset(knowledge_chunks):
                    # Content and chunk boundaries already match: no embedding.
                    self.save_md5(md5_hex, filename)
                    report["unchanged"].append(
                        {"source": filename, "chunks": len(knowledge_chunks)}
                    )
                    logger.info(
                        f"[知识库覆盖]{filename} 已覆盖且分块一致（{len(knowledge_chunks)} 块），跳过"
                    )
                    continue

                content_sha256 = hashlib.sha256(content).hexdigest()
                metadata = {
                    "source": filename,
                    "file_type": file_type,
                    "create_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "operator": "客户",
                    "content_sha256": content_sha256,
                    "ingestion_schema": INGESTION_SCHEMA,
                    **doc_class.__dict__,
                }
                chunk_ids = [uuid.uuid4().hex for _ in knowledge_chunks]
                metadatas = [
                    {**metadata, "chunk_id": chunk_id} for chunk_id in chunk_ids
                ]
                self._add_texts_in_batches(knowledge_chunks, metadatas, chunk_ids)
                if old_ids:
                    self.vector_store.delete(ids=old_ids)
                self.save_md5(md5_hex, filename)
                report["reindexed"].append(
                    {
                        "source": filename,
                        "old_chunks": len(old_ids),
                        "new_chunks": len(knowledge_chunks),
                        "content_sha256": content_sha256,
                        "reason": "missing" if not old_ids else "chunk_mismatch",
                    }
                )
                logger.info(
                    f"[知识库覆盖]{filename} 私有索引已修复：{len(old_ids)} -> {len(knowledge_chunks)} 块"
                )
            except Exception as exc:  # noqa: BLE001 - one bad source must not hide others
                report["failed"].append(
                    {"source": filename, "error": f"{type(exc).__name__}: {exc}"}
                )
                logger.error(f"[知识库覆盖]{filename} 修复失败：{exc}", exc_info=True)

        after = self.vector_store.get(include=["metadatas"])
        indexed_sources = {
            str((metadata or {}).get("source"))
            for metadata in after.get("metadatas") or []
            if (metadata or {}).get("source")
        }
        report["missing_persisted_sources"] = sorted(indexed_sources - disk_sources)
        if report["missing_persisted_sources"]:
            logger.warning(
                "[知识库覆盖]索引中存在但磁盘缺失的来源（保留旧分块，不删除）："
                + ", ".join(report["missing_persisted_sources"])
            )
        report["chunks_after"] = len(after.get("ids") or [])
        report["ok"] = not report["failed"]
        return report

    def load_document(self) -> None:
        """Reconcile the private index with ``data/knowledge``.

        Kept as the stable entry point used by ``rebuild`` and CLI callers; the
        actual work is the idempotent coverage pass above.
        """
        self.ensure_coverage()


    def rebuild(self) -> None:
        """清空当前 collection 与 md5 记录，按 data/knowledge 重新入库。

        只用于本地知识库结构升级（例如新增 domain/category 元数据）。
        不是增量加载：会重新计算 embedding。
        """
        data = self.vector_store.get()
        ids = data.get("ids") or []
        if ids:
            self.vector_store.delete(ids=ids)
            logger.info(f"[重建知识库]已删除旧分块 {len(ids)} 个")
        md5_path = self.md5_store_path
        if os.path.exists(md5_path):
            os.remove(md5_path)
        self.load_document()


if __name__ == '__main__':
    import sys

    vs = VectorStoreService()
    if "--rebuild" in sys.argv:
        vs.rebuild()
    else:
        vs.load_document()

    retriever = vs.get_retriever()
    res = retriever.invoke("迷路")
    for r in res:
        print(r.page_content)
        print("-" * 20)
