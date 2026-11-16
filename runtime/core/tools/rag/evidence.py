# -*- coding: utf-8 -*-
"""Minimal generic retrieval-evidence interface.

The RAG tools keep returning ``content_and_artifact`` unchanged.  This module
only records *what was actually retrieved in this turn* so the product can show
real source snippets and map citations to the current evidence instead of
claiming that any square-bracket filename is correct.

It is deliberately business-neutral: no relevance thresholds, no safety claim,
no recharge keywords.  Business judgement lives in the project package.
"""
from __future__ import annotations

import contextvars
import time
from typing import Any

_CURRENT: contextvars.ContextVar["EvidenceCollector | None"] = contextvars.ContextVar(
    "rag_evidence_collector", default=None
)


def _snippet(text: str, limit: int = 320) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit]


class EvidenceCollector:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, record: dict[str, Any]) -> None:
        self.records.append(record)

    def payload(self) -> dict[str, Any]:
        return {
            "status": _overall_status(self.records),
            "attempts": len(self.records),
            "queries": [item.get("query") for item in self.records],
            "results": _flatten(self.records),
            "records": list(self.records),
        }


def _overall_status(records: list[dict[str, Any]]) -> str:
    if not records:
        return "not_retrieved"
    statuses = [str(item.get("status") or "ok") for item in records]
    if "error" in statuses:
        return "error"
    if "timeout" in statuses:
        return "timeout"
    if all(status == "empty" for status in statuses):
        return "empty"
    return "ok"


def _flatten(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        for item in record.get("results") or []:
            key = (str(item.get("source") or ""), str(item.get("chunk_id") or item.get("snippet") or ""))
            if key in seen:
                continue
            seen.add(key)
            rows.append(item)
            if len(rows) >= 12:
                return rows
    return rows


def begin_collection() -> contextvars.Token:
    return _CURRENT.set(EvidenceCollector())


def end_collection(token: contextvars.Token | None = None) -> dict[str, Any]:
    collector = _CURRENT.get()
    payload = collector.payload() if collector is not None else EvidenceCollector().payload()
    if token is not None:
        try:
            _CURRENT.reset(token)
        except ValueError:
            _CURRENT.set(None)
    else:
        _CURRENT.set(None)
    return payload


def current_collector() -> EvidenceCollector | None:
    return _CURRENT.get()


def record_retrieval(
    *,
    tool: str,
    query: str,
    docs: list[Any] | None = None,
    status: str = "ok",
    error: str | None = None,
    elapsed_ms: float | None = None,
) -> None:
    """Record actual documents (``page_content``/``metadata``) for one tool call.

    ``status`` is one of ``ok|empty|error|timeout``.  Errors are recorded and
    re-raised by the caller; this function never turns an error into an empty
    successful answer.
    """
    collector = _CURRENT.get()
    if collector is None:
        return
    import os

    results: list[dict[str, Any]] = []
    for index, doc in enumerate(docs or [], 1):
        metadata = getattr(doc, "metadata", None) or {}
        content = str(getattr(doc, "page_content", "") or "")
        if not content:
            continue
        source = os.path.basename(str(metadata.get("source", "")))
        results.append(
            {
                "rank": index,
                "source": source,
                "chunk_id": metadata.get("chunk_id"),
                "snippet": _snippet(content),
                "content": content,
                "metadata": {key: value for key, value in metadata.items() if key != "source"},
            }
        )
    if status == "ok" and not results:
        status = "empty"
    record = {
        "tool": tool,
        "query": str(query or ""),
        "status": status,
        "error": error,
        "elapsed_ms": round(float(elapsed_ms), 1) if elapsed_ms is not None else None,
        "results": results,
        "at": time.time(),
    }
    collector.record(record)


def timed() -> float:
    return time.perf_counter()
