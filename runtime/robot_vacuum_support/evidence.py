# -*- coding: utf-8 -*-
"""Business-side retrieval evidence review for the recharge-support product.

The generic runtime only says *what documents were retrieved*.  This module
decides whether they are usable as candidate evidence for this product, maps
answer citations to the current snippets, and produces explicit recoverable
feedback.  It deliberately does **not** claim semantic correctness or safety.
"""
from __future__ import annotations

import re
from typing import Any

_FILE_RE = re.compile(r"\[([^\[\]]+?\.(?:txt|pdf|md|docx?|json))\]", re.I)
#: Models sometimes list "参考来源：故障排除.txt" without brackets.  Treat a
#: plain known-looking filename as a citation candidate; it is still mapped
#: against this turn's retrieval evidence before anything is claimed.
_PLAIN_FILE_RE = re.compile(
    r"(?<![\w./\\-])([\u4e00-\u9fa5A-Za-z0-9_\-]+\.(?:txt|pdf|md|docx?|json))",
    re.I,
)
_DURATION_RE = re.compile(r"(?:约|大约|需要|耗时)?\s*(\d+(?:\.\d+)?)\s*(小时|分钟|h|min)")
_RECHARGE_HINT = re.compile(r"回充|充电|充电座|充电触点|电池|电量")

_STATUS_NOTICE = {
    "not_retrieved": "本轮没有调用知识库检索，无法给出有依据的引用；可以重试一次，或直接说明现象让我继续追问。",
    "empty": "本轮检索没有返回可用资料；不要把它当成已确认结论。可以换个说法重试，或补充具体现象。",
    "error": "本轮检索工具报错，未取得资料；这不是正常空结果。可以重试。",
    "timeout": "本轮检索超时，未取得资料；可以稍后重试。",
}


def _citation_names(answer: str) -> list[str]:
    names: list[str] = []
    value = str(answer or "")
    for match in _FILE_RE.finditer(value):
        name = match.group(1).strip()
        if name and name not in names:
            names.append(name)
    for match in _PLAIN_FILE_RE.finditer(value):
        name = match.group(1).strip()
        if name and name not in names:
            names.append(name)
    return names


def _dedupe_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in results:
        source = str(item.get("source") or "")
        key = (source, str(item.get("chunk_id") or item.get("snippet") or ""))
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "source": source,
                "chunk_id": item.get("chunk_id"),
                "snippet": item.get("snippet") or "",
                "rank": item.get("rank"),
            }
        )
        if len(rows) >= 8:
            break
    return rows


def _conflict(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Conservative conflict signal for repeated numeric durations.

    This is intentionally narrow: it only flags different duration numbers in
    the currently retrieved snippets.  It is a prompt to confirm, not a claim
    that the knowledge base is internally contradictory.
    """
    values: dict[str, set[str]] = {}
    for item in results:
        for match in _DURATION_RE.finditer(str(item.get("snippet") or "")):
            unit = match.group(2).lower()
            bucket = "min" if unit in {"分钟", "min"} else "hour"
            values.setdefault(bucket, set()).add(match.group(1))
    for bucket, numbers in values.items():
        if len(numbers) > 1:
            unit = "分钟" if bucket == "min" else "小时"
            return {
                "kind": "duration",
                "values": sorted(numbers),
                "message": f"本轮资料在时长上出现不同数值（{'/'.join(sorted(numbers))} {unit}），不能强行合并；请以机型说明书或官方售后确认为准。",
            }
    return None


def review(answer: str, retrieval: dict[str, Any] | None, *, recharge_scope: bool = False) -> dict[str, Any]:
    """Return a UI-safe review of current retrieval + citations."""
    retrieval = retrieval if isinstance(retrieval, dict) else {}
    status = str(retrieval.get("status") or "not_retrieved")
    results = _dedupe_results(list(retrieval.get("results") or []))
    citations = _citation_names(answer)
    sources = []
    for item in results:
        if item["source"] and item["source"] not in sources:
            sources.append(item["source"])
    mapped = [name for name in citations if name in sources]
    unmapped = [name for name in citations if name not in sources]
    relevant = True
    if recharge_scope and results:
        relevant = any(_RECHARGE_HINT.search(str(item.get("snippet") or "")) for item in results)
    conflict = _conflict(results) if results else None

    if not results:
        citation_status = "no_evidence"
    elif citations and not unmapped:
        citation_status = "matched_current_evidence"
    elif citations and mapped:
        citation_status = "partial"
    elif citations:
        citation_status = "unmapped"
    else:
        citation_status = "no_citation"

    notices: list[str] = []
    if status in _STATUS_NOTICE:
        notices.append(_STATUS_NOTICE[status])
    if status == "ok" and not results:
        notices.append(_STATUS_NOTICE["empty"])
    if results and not relevant:
        notices.append("检索到的片段与当前回充问题关联不足以支撑结论；先按现象追问，不把候选片段当已验证答案。")
    if unmapped:
        notices.append("回答中出现的来源文件名不在本轮检索证据里，不能宣称引用正确。")
    if conflict:
        notices.append(conflict["message"])
    if status == "ok" and results and not notices:
        notices.append("来源文件名已与当前检索片段做存在性映射；片段内容是否完全支持结论仍需人工核对。")

    return {
        "status": status,
        "query": (retrieval.get("queries") or [None])[0] if retrieval.get("queries") else None,
        "results": results,
        "sources": sources,
        "citations": citations,
        "mapped_citations": mapped,
        "unmapped_citations": unmapped,
        "citation_status": citation_status,
        "relevance": "candidate" if relevant and results else ("insufficient" if results else "no_evidence"),
        "conflict": conflict,
        "notice": " ".join(notices) if notices else None,
        "claim": "仅做来源存在性与片段展示，不声称引用语义完全正确或已验证安全。",
    }
