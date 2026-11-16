# -*- coding: utf-8 -*-
"""Focused regression for private-index ingestion coverage.

Covers: generic numbered-entry PDF/TXT splitting, PDF chunk reindexing on a
private copy, idempotence on restart, content-change replacement, missing
persisted source, and source-index immutability.  No model or network call.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "runtime") not in sys.path:
    sys.path.insert(0, str(ROOT / "runtime"))

import core.paths as project_paths  # noqa: E402
from core.tools.rag import vector_store as vs_module  # noqa: E402
from core.tools.rag.vector_store import (  # noqa: E402
    INGESTION_SCHEMA,
    VectorStoreService,
    _numbered_faq_entries,
)


class _FakeEmbeddings:
    """Deterministic 512-dim vectors so tests never load the BGE model."""

    def _vec(self, text: str) -> list[float]:
        digest = hashlib.sha256(str(text).encode("utf-8")).digest()
        return [((digest[index % len(digest)] / 255.0) - 0.5) for index in range(512)]

    def embed_documents(self, texts):
        return [self._vec(text) for text in texts]

    def embed_query(self, text):
        return self._vec(text)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _faq_text(count: int = 10) -> str:
    lines = []
    for index in range(1, count + 1):
        lines.append(f"{index}. 问题 {index}？")
        lines.append(f"- 这是第 {index} 条的答案。")
    return "\n".join(lines)


@pytest.fixture()
def private_index(tmp_path: Path, monkeypatch):
    data = tmp_path / "data"
    workspace = tmp_path / "workspace"
    (data / "knowledge").mkdir(parents=True)
    workspace.mkdir(parents=True)
    shutil.copytree(ROOT / "assets" / "rag_index", workspace / "chroma_db")
    monkeypatch.setattr(vs_module, "embed_model", _FakeEmbeddings())
    monkeypatch.setattr(project_paths, "DATA_ROOT", data)
    monkeypatch.setattr(project_paths, "RUNTIME_ROOT", workspace)
    return data, workspace


def _service() -> VectorStoreService:
    return VectorStoreService()


def test_numbered_faq_entries_apply_to_pdf_structure_without_filename():
    # Same numbered structure regardless of extension; density guard blocks prose.
    text = "\n".join(
        f"{index}. 问题 {index}？\n- 答案 {index}。" for index in range(1, 12)
    )
    assert len(_numbered_faq_entries(text)) == 11
    prose = "这是一段说明。\n" * 40 + "1. 只有一个列表项。\n2. 第二个列表项。"
    assert _numbered_faq_entries(prose) == []


def test_pdf_private_index_is_reindexed_and_idempotent(private_index):
    data, _workspace = private_index
    pdf = ROOT / "assets" / "knowledge" / "扫地机器人100问.pdf"
    shutil.copy2(pdf, data / "knowledge" / pdf.name)
    source_index = ROOT / "assets" / "rag_index" / "chroma.sqlite3"
    source_hash = _sha256(source_index)

    service = _service()
    first = service.ensure_coverage()
    row = next(item for item in first["reindexed"] if item["source"] == pdf.name)
    assert row["old_chunks"] == 8
    assert row["new_chunks"] == 101
    assert row["content_sha256"] == _sha256(pdf)
    assert first["chunks_after"] == 823 - 8 + 101
    assert service.vector_store.get(where={"source": pdf.name})["ids"]
    assert len(service.vector_store.get(where={"source": pdf.name})["ids"]) == 101

    second = service.ensure_coverage()
    assert second["reindexed"] == []
    assert second["chunks_before"] == second["chunks_after"] == 916
    assert len(service.vector_store.get(where={"source": pdf.name})["ids"]) == 101
    assert _sha256(source_index) == source_hash


def test_existing_txt_chunks_are_not_reembedded(private_index):
    data, _workspace = private_index
    txt = ROOT / "assets" / "knowledge" / "故障排除.txt"
    shutil.copy2(txt, data / "knowledge" / txt.name)
    report = _service().ensure_coverage()
    assert not any(item["source"] == txt.name for item in report["reindexed"])
    assert any(item["source"] == txt.name for item in report["unchanged"])


def test_content_change_replaces_chunks_without_duplicates(private_index):
    data, _workspace = private_index
    source = data / "knowledge" / "faq.txt"
    source.write_text(_faq_text(10), encoding="utf-8")
    service = _service()
    first = service.ensure_coverage()
    assert first["reindexed"][0]["new_chunks"] == 10
    first_ids = set(service.vector_store.get(where={"source": "faq.txt"})["ids"])

    source.write_text(_faq_text(10).replace("第 5 条的答案", "第 5 条的新答案"), encoding="utf-8")
    second = service.ensure_coverage()
    row = second["reindexed"][0]
    assert row["old_chunks"] == row["new_chunks"] == 10
    assert row["content_sha256"] == _sha256(source)
    second_ids = set(service.vector_store.get(where={"source": "faq.txt"})["ids"])
    assert len(second_ids) == 10
    assert first_ids.isdisjoint(second_ids)
    metadata = service.vector_store.get(where={"source": "faq.txt"})["metadatas"]
    assert {item["ingestion_schema"] for item in metadata} == {INGESTION_SCHEMA}
    assert {item["content_sha256"] for item in metadata} == {row["content_sha256"]}


def test_missing_persisted_source_is_reported_not_deleted(private_index):
    _data, _workspace = private_index
    service = _service()
    before = len(service.vector_store.get()["ids"])
    report = service.ensure_coverage()
    assert report["reindexed"] == []
    assert report["failed"] == []
    assert report["chunks_before"] == report["chunks_after"] == before
    assert "扫地机器人100问.pdf" in report["missing_persisted_sources"]
    assert before == 823
