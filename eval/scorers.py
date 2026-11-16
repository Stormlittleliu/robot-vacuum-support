# -*- coding: utf-8 -*-
"""Separated deterministic and human/assisted scoring for robot-support eval v1.

Design rules (P1-05):

* Automatic checks are *structural*: answer presence, citation format,
  this-turn retrieval existence, citation filename -> this-turn retrieval
  mapping, state subset, tool/retrieval event presence, runtime error.
* ``required_claims`` / ``forbidden_claims`` / ``required_behaviors`` are
  semantic rubrics.  This module never matches them with a regex and never
  pretends an automatic pass; they stay ``pending_human``.
* ``expected_sources`` is checked in two separate ways: existence in this
  turn's retrieval (automatic) and existence in the answer citation (format),
  plus semantic support (human).  A filename in the answer is not evidence
  that the claim is supported.
* Missing dimensions are ``not_applicable`` / ``unsupported``, not a silent
  pass.  Unknown/new dataset fields are preserved by callers and ignored here
  unless they map to a stable semantic key.
"""
from __future__ import annotations

import math
import os
import re
import sys
from pathlib import Path
from statistics import median
from typing import Any, Iterable

# Reuse the same runtime import path convention as ``eval/runner.py`` so this
# module stays importable from another cwd.  The action catalogue is the single
# source of truth: importing ``actions`` must not pull in the server, model or
# RAG stack, and that boundary is covered by
# ``tests/test_action_catalog_scoring.py``.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_ROOT = _PROJECT_ROOT / "runtime"
if str(_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_ROOT))

from robot_vacuum_support.actions import ACTION_IDS  # noqa: E402

SCORER_VERSION = "v1.2-shared-actions"

#: Stable state keys allowed by the shared v1 contract.  ``next_action`` and
#: internal runtime fields are deliberately not invented here.
STABLE_STATE_KEYS = {
    "case_status",
    "tried_actions",
    "not_tried_actions",
    "model",
    "case_count",
    "scope",
}

#: The current product state module is a recharge-only contract.  Dataset
#: scopes are semantic labels, so map the naming difference explicitly instead
#: of reporting it as a product defect.
SCOPE_ALIASES = {
    "recharge": "recharge",
    "recharge_failure": "recharge",
}

#: Actions the current server-authoritative state actually records, derived
#: from the runtime's ``ACTION_CATALOG``.  A dataset action outside this
#: catalogue is ``unsupported``, not a fake pass/fail.  ``auto_next`` is a
#: product-suggestion policy, not a scoring condition: observation-only ids
#: such as ``replace_dock`` / ``move_dock`` are still evaluable when a case
#: says the user did them.
_RECHARGE_ACTION_IDS = frozenset(ACTION_IDS)

_FILE_RE = re.compile(r"\[([^\[\]]+?\.(?:txt|pdf|md|docx?|json))\]", re.I)
_PLAIN_FILE_RE = re.compile(
    r"(?<![\w./\\-])([\u4e00-\u9fa5A-Za-z0-9_\-]+\.(?:txt|pdf|md|docx?|json))",
    re.I,
)


def _check(
    name: str,
    status: str,
    *,
    dimension: str = "automatic",
    expected: Any = None,
    actual: Any = None,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "dimension": dimension,
        "status": status,
        "expected": expected,
        "actual": actual,
        "detail": detail,
    }


def _action_pairs(items: Iterable[Any]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            action_id = item.get("action_id")
            outcome = item.get("outcome")
            if action_id:
                pairs.append({"action_id": str(action_id), "outcome": outcome})
        elif isinstance(item, str):
            pairs.append({"action_id": item, "outcome": None})
    return pairs


def _actual_tried(public_view: dict[str, Any] | None) -> list[dict[str, Any]]:
    case = (public_view or {}).get("active_case") or {}
    return _action_pairs(case.get("tried_actions") or [])


def _actual_not_tried(public_view: dict[str, Any] | None) -> list[dict[str, Any]]:
    case = (public_view or {}).get("active_case") or {}
    return _action_pairs(case.get("not_tried_actions") or [])


def _expected_not_tried(items: Iterable[Any]) -> list[dict[str, Any]]:
    return _action_pairs(items)


def _case_status(public_view: dict[str, Any] | None) -> Any:
    case = (public_view or {}).get("active_case")
    if not case:
        return None
    return case.get("status")


def score_state_subset(
    expected_state: dict[str, Any] | None,
    public_view: dict[str, Any] | None,
    *,
    scope: str | None = None,
) -> list[dict[str, Any]]:
    """Compare only the supplied stable semantic keys.

    ``tried_actions`` / ``not_tried_actions`` are subset checks, except that an
    explicit empty list requires the actual list to be empty too.  We do not
    slice the actual list or pick a single "correct next action".

    Canonicalisation:
    * ``recharge_failure`` and ``recharge`` are the same scope for this product.
    * A non-recharge scope (suction/mop/...) is not implemented by the
      recharge-only state module; those state keys are ``not_applicable`` for
      state scoring, never a fake product failure or a pass.
    * Action IDs outside the current recharge catalogue are ``unsupported``.
    """
    if not isinstance(expected_state, dict) or not expected_state:
        return []
    checks: list[dict[str, Any]] = []
    case = (public_view or {}).get("active_case") or {}
    expected_scope = expected_state.get("scope")
    non_recharge = expected_scope is not None and expected_scope not in SCOPE_ALIASES
    for key, expected in expected_state.items():
        if key not in STABLE_STATE_KEYS:
            checks.append(
                _check(
                    f"state.{key}",
                    "unsupported",
                    expected=expected,
                    detail="not a stable v1 state key; kept but not auto-scored",
                )
            )
            continue
        if non_recharge and key != "scope":
            checks.append(
                _check(
                    f"state.{key}",
                    "not_applicable",
                    expected=expected,
                    detail=(
                        "current state contract is recharge-only; non-recharge "
                        "state is QA-only and not auto-failed"
                    ),
                )
            )
            continue
        if key == "case_status":
            actual = _case_status(public_view)
            status = "pass" if actual == expected else "fail"
            checks.append(
                _check("state.case_status", status, expected=expected, actual=actual)
            )
        elif key == "scope":
            if non_recharge:
                checks.append(
                    _check(
                        "state.scope",
                        "not_applicable",
                        expected=expected,
                        actual=scope,
                        detail="non-recharge scope is not implemented by the recharge-only state module",
                    )
                )
                continue
            canonical_expected = SCOPE_ALIASES.get(str(expected), str(expected))
            canonical_actual = SCOPE_ALIASES.get(str(scope), str(scope))
            status = "pass" if canonical_actual == canonical_expected else "fail"
            checks.append(
                _check(
                    "state.scope",
                    status,
                    expected=expected,
                    actual=scope,
                    detail=f"canonical {canonical_expected!r} vs {canonical_actual!r}",
                )
            )
        elif key == "model":
            actual = case.get("model")
            status = "pass" if actual == expected else "fail"
            checks.append(_check("state.model", status, expected=expected, actual=actual))
        elif key == "case_count":
            actual = len((public_view or {}).get("cases") or [])
            status = "pass" if actual == expected else "fail"
            checks.append(
                _check("state.case_count", status, expected=expected, actual=actual)
            )
        elif key == "tried_actions":
            wanted = _action_pairs(expected if isinstance(expected, list) else [])
            actual = _actual_tried(public_view)
            unsupported = [item for item in wanted if item["action_id"] not in _RECHARGE_ACTION_IDS]
            supported = [item for item in wanted if item["action_id"] in _RECHARGE_ACTION_IDS]
            if wanted and not supported:
                checks.append(
                    _check(
                        "state.tried_actions",
                        "unsupported",
                        expected=wanted,
                        actual=actual,
                        detail="all expected actions are outside the current recharge action catalogue",
                    )
                )
                continue
            if not wanted:
                ok = not actual
                detail = "" if ok else "expected no tried actions"
            else:
                ok = all(
                    any(
                        got["action_id"] == want["action_id"]
                        and (want["outcome"] is None or got["outcome"] == want["outcome"])
                        for got in actual
                    )
                    for want in supported
                )
                detail = "" if ok else "expected tried actions are not all present"
            status = "pass" if ok else "fail"
            if status == "fail" and unsupported:
                status = "unsupported"
                detail += "; some expected actions are outside the current catalogue"
            checks.append(
                _check(
                    "state.tried_actions",
                    status,
                    expected=wanted,
                    actual=actual,
                    detail=detail,
                )
            )
        elif key == "not_tried_actions":
            wanted = _expected_not_tried(expected if isinstance(expected, list) else [])
            actual = _actual_not_tried(public_view)
            unsupported = [item for item in wanted if item["action_id"] not in _RECHARGE_ACTION_IDS]
            supported = [item for item in wanted if item["action_id"] in _RECHARGE_ACTION_IDS]
            if wanted and not supported:
                checks.append(
                    _check(
                        "state.not_tried_actions",
                        "unsupported",
                        expected=wanted,
                        actual=actual,
                        detail=(
                            "all expected actions are outside the current recharge "
                            "catalogue; this is not proof the user said them"
                        ),
                    )
                )
                continue
            if not wanted:
                ok = not actual
                detail = "" if ok else "expected no not-tried actions"
            else:
                ok = all(
                    any(got["action_id"] == want["action_id"] for got in actual)
                    for want in supported
                )
                detail = "" if ok else "expected not-tried actions are not all present"
            status = "pass" if ok else "fail"
            if status == "fail" and unsupported:
                status = "unsupported"
                detail += "; some expected actions are outside the current catalogue"
            checks.append(
                _check(
                    "state.not_tried_actions",
                    status,
                    expected=wanted,
                    actual=actual,
                    detail=detail,
                )
            )
    return checks


def citation_names(answer: str) -> list[str]:
    """Return citation candidates in order: bracketed first, then plain names."""
    names: list[str] = []
    text = str(answer or "")
    for match in _FILE_RE.finditer(text):
        name = match.group(1).strip()
        if name and name not in names:
            names.append(name)
    for match in _PLAIN_FILE_RE.finditer(text):
        name = match.group(1).strip()
        if name and name not in names:
            names.append(name)
    return names


def _source_key(name: str) -> str:
    """Normalise dataset paths and retrieval basenames to one comparison key."""
    value = str(name or "").strip().replace("\\", "/")
    return os.path.basename(value).lower()


def _retrieval_sources(retrieval: dict[str, Any] | None) -> list[str]:
    sources: list[str] = []
    for item in (retrieval or {}).get("results") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()
        if source and source not in sources:
            sources.append(source)
    return sources


def score_citations(
    answer: str,
    retrieval: dict[str, Any] | None,
    *,
    expected_sources: list[str] | None = None,
    require_citation: bool | None = None,
    expected_sources_mode: str = "any_of",
) -> list[dict[str, Any]]:
    """Structural citation/retrieval checks only; never semantic support.

    ``require_citation`` follows the case's declared dimension: a case that
    explicitly has no expected source may legitimately refuse without citing.
    If it does cite anyway, the filename is still mapped against this turn's
    retrieval (so a fabricated filename can fail), but absence of a citation is
    not automatically a failure for such a case.

    ``expected_sources_mode`` follows the dataset: ``any_of`` means the listed
    files are acceptable candidates and at least one should be used; ``all`` is
    supported only when a case explicitly asks for it.
    """
    checks: list[dict[str, Any]] = []
    text = str(answer or "")
    if not text.strip():
        checks.append(
            _check(
                "answer.nonempty",
                "fail",
                expected="non-empty answer",
                actual="",
            )
        )
        return checks
    checks.append(_check("answer.nonempty", "pass", actual="non-empty"))

    citations = citation_names(text)
    expected = [str(item) for item in (expected_sources or []) if str(item).strip()]
    if require_citation is None:
        require_citation = bool(expected)
    checks.append(
        _check(
            "citation.present",
            "pass" if citations else ("fail" if require_citation else "not_applicable"),
            expected="[知识库文件名]" if require_citation else "not required by this case",
            actual=citations,
            detail="" if citations else (
                "no bracket/plain knowledge filename found"
                if require_citation
                else "case declares no expected_sources; absence is not auto-failed"
            ),
        )
    )

    sources = _retrieval_sources(retrieval)
    attempts = int((retrieval or {}).get("attempts") or 0)
    status = str((retrieval or {}).get("status") or "not_retrieved")
    checks.append(
        _check(
            "retrieval.attempted",
            "pass" if attempts > 0 else ("fail" if require_citation else "not_applicable"),
            expected="at least one this-turn retrieval" if require_citation else "not required by this case",
            actual={"attempts": attempts, "status": status},
            detail="" if attempts > 0 else (
                "no this-turn retrieval event recorded"
                if require_citation
                else "case declares no expected_sources; no retrieval is not auto-failed"
            ),
        )
    )
    records = (retrieval or {}).get("records") or []
    tools = sorted(
        {
            str(item.get("tool"))
            for item in records
            if isinstance(item, dict) and item.get("tool")
        }
    )
    checks.append(
        _check(
            "retrieval.tool_event",
            "pass" if tools else ("fail" if require_citation else "not_applicable"),
            expected="rag tool event in this turn",
            actual={"tools": tools, "records": len(records)},
            detail="" if tools else "no tool name in this turn's retrieval records",
        )
    )

    if not citations:
        checks.append(
            _check(
                "citation.filename_mapping",
                "not_applicable",
                expected=sorted(sources),
                actual=[],
                detail="no citation to map",
            )
        )
    else:
        source_keys = {_source_key(source) for source in sources}
        unmapped = [name for name in citations if _source_key(name) not in source_keys]
        checks.append(
            _check(
                "citation.filename_mapping",
                "pass" if not unmapped else "fail",
                expected=sorted(sources),
                actual={"citations": citations, "unmapped": unmapped},
                detail="" if not unmapped else "citation filename is not in this turn's retrieval",
            )
        )

    # Separate the *retrieved fragment* question from the filename question.
    # A filename that exists in retrieval is still not semantic support, but a
    # citation with no retrieved snippet (or with an empty result) is a weaker
    # structural mapping and is shown separately.
    result_by_key: dict[str, dict[str, Any]] = {}
    for item in (retrieval or {}).get("results") or []:
        if not isinstance(item, dict):
            continue
        key = _source_key(str(item.get("source") or ""))
        if key and key not in result_by_key:
            result_by_key[key] = item
    fragment_rows: list[dict[str, Any]] = []
    for name in citations:
        result = result_by_key.get(_source_key(name))
        fragment = ""
        if isinstance(result, dict):
            fragment = str(result.get("snippet") or result.get("content") or result.get("text") or "")
        fragment_rows.append(
            {
                "citation": name,
                "mapped_source": (result or {}).get("source"),
                "rank": (result or {}).get("rank"),
                "chunk_id": (result or {}).get("chunk_id"),
                "fragment_present": bool(fragment.strip()),
                "fragment_preview": fragment.strip()[:240],
            }
        )
    if not citations:
        checks.append(
            _check(
                "citation.source_fragment",
                "not_applicable",
                expected="retrieved snippet for each cited file",
                actual=[],
                detail="no citation to map to a fragment",
            )
        )
    else:
        missing_fragments = [row["citation"] for row in fragment_rows if not row["fragment_present"]]
        checks.append(
            _check(
                "citation.source_fragment",
                "pass" if not missing_fragments else "fail",
                expected="each citation maps to a retrieved snippet/content fragment",
                actual={
                    "mappings": fragment_rows,
                    "missing_fragment": missing_fragments,
                    "not_semantic_support": True,
                },
                detail=(
                    "filename-to-retrieved-fragment presence only; it does not prove the "
                    "claim is supported"
                ),
            )
        )

    expected = [str(item) for item in (expected_sources or []) if str(item).strip()]
    mode = "all" if expected_sources_mode == "all" else "any_of"
    if expected:
        source_keys = {_source_key(source) for source in sources}
        retrieved = [name for name in expected if _source_key(name) in source_keys]
        retrieved_ok = bool(retrieved) if mode == "any_of" else len(retrieved) == len(expected)
        checks.append(
            _check(
                "expected_sources.retrieved",
                "pass" if retrieved_ok else "fail",
                expected=expected,
                actual={
                    "retrieved": retrieved,
                    "coverage": f"{len(retrieved)}/{len(expected)}",
                    "mode": mode,
                    "this_turn_sources": sources,
                },
                detail=(
                    f"expected_sources_mode={mode}; "
                    + (
                        "at least one declared evidence source was retrieved this turn"
                        if mode == "any_of"
                        else "all declared evidence sources were retrieved this turn"
                    )
                    + "; not semantic support"
                ),
            )
        )
        citation_keys = {_source_key(name) for name in citations}
        cited = [name for name in expected if _source_key(name) in citation_keys]
        cited_ok = bool(cited) if mode == "any_of" else len(cited) == len(expected)
        checks.append(
            _check(
                "expected_sources.cited",
                "pass" if cited_ok else ("fail" if require_citation else "not_applicable"),
                expected=expected,
                actual={
                    "cited": cited,
                    "coverage": f"{len(cited)}/{len(expected)}",
                    "mode": mode,
                    "citations": citations,
                },
                detail="filename presence in answer, not semantic support",
            )
        )
    else:
        checks.append(
            _check(
                "expected_sources.retrieved",
                "not_applicable",
                detail="case lists no expected_sources",
            )
        )
    return checks


def semantic_review_items(
    turns: Iterable[dict[str, Any]],
    *,
    mode: str,
) -> list[dict[str, Any]]:
    """Build the human rubric without inventing an automatic judge.

    In ``state`` mode there is no model answer, so answer-claim rubrics are
    ``not_applicable``; in ``live`` they are ``pending_human``.  A future
    model-assisted judge may fill these later, but it is not enabled here.
    """
    items: list[dict[str, Any]] = []
    for index, turn in enumerate(turns or [], 1):
        expected = turn.get("expected") or {}
        content = str(turn.get("content") or "")
        if turn.get("role") == "assistant_fixture":
            source = "assistant_fixture"
        else:
            source = "user_turn"
        for claim in expected.get("required_claims") or []:
            items.append(
                {
                    "turn": index,
                    "dimension": "semantic.required_claim",
                    "rubric": str(claim),
                    "input": content,
                    "source": source,
                    "status": "not_applicable" if mode == "state" else "pending_human",
                    "reviewer": None,
                    "verdict": None,
                    "reason": None,
                }
            )
        for claim in expected.get("forbidden_claims") or []:
            items.append(
                {
                    "turn": index,
                    "dimension": "semantic.forbidden_claim",
                    "rubric": str(claim),
                    "input": content,
                    "source": source,
                    "status": "not_applicable" if mode == "state" else "pending_human",
                    "reviewer": None,
                    "verdict": None,
                    "reason": None,
                }
            )
        for behavior in expected.get("required_behaviors") or []:
            items.append(
                {
                    "turn": index,
                    "dimension": "semantic.required_behavior",
                    "rubric": str(behavior),
                    "input": content,
                    "source": source,
                    "status": "not_applicable" if mode == "state" else "pending_human",
                    "reviewer": None,
                    "verdict": None,
                    "reason": None,
                }
            )
        for source_file in expected.get("expected_sources") or []:
            items.append(
                {
                    "turn": index,
                    "dimension": "semantic.source_support",
                    "rubric": f"answer claim is supported by {source_file}",
                    "input": content,
                    "source": source,
                    "status": "not_applicable" if mode == "state" else "pending_human",
                    "reviewer": None,
                    "verdict": None,
                    "reason": None,
                }
            )
    return items


def has_semantic_dimensions(turns: Iterable[dict[str, Any]]) -> bool:
    for turn in turns or []:
        expected = turn.get("expected") or {}
        if any(
            expected.get(key)
            for key in (
                "required_claims",
                "forbidden_claims",
                "required_behaviors",
                "expected_sources",
            )
        ):
            return True
    return False


def overall_status(
    checks: list[dict[str, Any]],
    semantic_items: list[dict[str, Any]],
    *,
    error: str | None = None,
    skipped: bool = False,
) -> str:
    """One case-level status; no semantic dimension can produce ``passed``."""
    if error:
        return "error"
    if skipped:
        return "skipped"
    automatic = [
        item for item in checks if item.get("dimension") == "automatic"
    ]
    failed = [item for item in automatic if item.get("status") == "fail"]
    if failed:
        return "failed"
    scored = [item for item in automatic if item.get("status") == "pass"]
    pending = [item for item in semantic_items if item.get("status") == "pending_human"]
    if pending:
        return "pending_human"
    if not scored:
        return "not_applicable"
    return "passed"


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


def latency_summary(values: Iterable[float | int | None]) -> dict[str, Any]:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return {"n": 0, "p50_ms": None, "p95_ms": None, "mean_ms": None, "max_ms": None}
    return {
        "n": len(clean),
        "p50_ms": _percentile(clean, 0.50),
        "p95_ms": _percentile(clean, 0.95),
        "mean_ms": round(sum(clean) / len(clean), 3),
        "max_ms": round(max(clean), 3),
        "note": "raw sample percentile; no significance claim" if len(clean) < 20 else "",
    }


def _token_total(usage: Any) -> int | None:
    if not isinstance(usage, dict) or not usage:
        return None
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)):
        return int(total)
    numbers = [
        value
        for key, value in usage.items()
        if key.endswith("tokens") and isinstance(value, (int, float))
    ]
    return int(sum(numbers)) if numbers else None


def _usage_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    usage_rows = [row.get("usage_summary") or {} for row in rows]
    token_values = [
        usage.get("total_tokens")
        for usage in usage_rows
        if isinstance(usage.get("total_tokens"), (int, float))
    ]
    request_values = [
        usage.get("model_requests")
        for usage in usage_rows
        if isinstance(usage.get("model_requests"), (int, float))
    ]
    call_values = [
        usage.get("model_calls")
        for usage in usage_rows
        if isinstance(usage.get("model_calls"), (int, float))
    ]
    ledger_values = [
        usage.get("ledger_events")
        for usage in usage_rows
        if isinstance(usage.get("ledger_events"), (int, float))
    ]
    by_layer: dict[str, dict[str, int]] = {}
    for usage in usage_rows:
        for layer, values in (usage.get("by_layer") or {}).items():
            target = by_layer.setdefault(
                layer,
                {"requests": 0, "model_calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cases": 0},
            )
            target["cases"] += 1
            for key in ("requests", "model_calls", "input_tokens", "output_tokens", "total_tokens"):
                value = values.get(key)
                if isinstance(value, (int, float)):
                    target[key] += int(value)
    usage_statuses: dict[str, int] = {}
    for usage in usage_rows:
        status = str(usage.get("usage_status") or "unavailable")
        usage_statuses[status] = usage_statuses.get(status, 0) + 1
    return {
        "model_requests": sum(request_values) if request_values else None,
        "model_calls": sum(call_values) if call_values else None,
        "ledger_events": sum(ledger_values) if ledger_values else None,
        "reported_token_sum": sum(int(value) for value in token_values) if token_values else None,
        "token_samples_n": len(token_values),
        "request_samples_n": len(request_values),
        "usage_status": dict(sorted(usage_statuses.items())),
        "by_layer": dict(sorted(by_layer.items())),
        "n_cases": len(usage_rows),
        "note": (
            "model_requests counts real model requests; ledger_events counts aggregate "
            "usage rows. Token sum covers available samples only; missing usage is null, "
            "never estimated."
        ),
    }


def aggregate_case_results(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Separate QA and fault buckets; never fold fault success into QA rates."""
    all_rows = list(rows)
    qa = [row for row in all_rows if row.get("bucket") != "fault"]
    fault = [row for row in all_rows if row.get("bucket") == "fault"]

    def counts(items: list[dict[str, Any]]) -> dict[str, int]:
        result: dict[str, int] = {}
        for row in items:
            status = str(row.get("status") or "unknown")
            result[status] = result.get(status, 0) + 1
        return dict(sorted(result.items()))

    automatic: dict[str, dict[str, int]] = {}
    semantic: dict[str, int] = {}
    for row in all_rows:
        for check in row.get("checks") or []:
            if check.get("dimension") != "automatic":
                continue
            bucket = automatic.setdefault(check["name"], {})
            bucket[check["status"]] = bucket.get(check["status"], 0) + 1
        for item in row.get("semantic_review") or []:
            dimension = item.get("dimension") or "semantic"
            semantic[dimension] = semantic.get(dimension, 0) + 1

    latencies = [row.get("latency_ms") for row in qa if row.get("status") in {"passed", "failed", "pending_human", "not_applicable"}]
    turn_latencies = [
        turn.get("latency_ms")
        for row in qa
        for turn in (row.get("turns") or [])
        if turn.get("latency_ms") is not None
    ]
    qa_usage = _usage_block(qa)
    fault_usage = _usage_block(fault)
    all_usage = _usage_block(all_rows)
    qa_attempted = sum(1 for row in qa if row.get("attempted"))
    qa_completed = sum(
        1 for row in qa if row.get("status") in {"passed", "failed", "pending_human", "not_applicable"}
    )
    by_category: dict[str, dict[str, Any]] = {}
    for row in all_rows:
        category = str(row.get("category") or "unknown")
        bucket = by_category.setdefault(category, {"total": 0, "status": {}, "qa": 0, "fault": 0})
        bucket["total"] += 1
        bucket["qa" if row.get("bucket") != "fault" else "fault"] += 1
        status = str(row.get("status") or "unknown")
        bucket["status"][status] = bucket["status"].get(status, 0) + 1
    return {
        "counts": {
            "total": len(all_rows),
            "qa_total": len(qa),
            "fault_total": len(fault),
            "qa_status": counts(qa),
            "fault_status": counts(fault),
            "qa_model_error": sum(1 for row in qa if row.get("status") == "error"),
            "qa_skipped": sum(1 for row in qa if row.get("status") == "skipped"),
            "qa_not_applicable": sum(1 for row in qa if row.get("status") == "not_applicable"),
            "qa_pending_human": sum(1 for row in qa if row.get("status") == "pending_human"),
            "qa_passed": sum(1 for row in qa if row.get("status") == "passed"),
            "qa_failed": sum(1 for row in qa if row.get("status") == "failed"),
            "qa_completed": qa_completed,
            "qa_attempted": qa_attempted,
            "qa_coverage": {
                "attempted": qa_attempted,
                "completed": qa_completed,
                "attempted_rate": round(qa_attempted / len(qa), 4) if qa else None,
                "completed_rate": round(qa_completed / len(qa), 4) if qa else None,
            },
        },
        "automatic": automatic,
        "semantic_review": semantic,
        "by_category": dict(sorted(by_category.items())),
        "latency": {
            **latency_summary(latencies),
            "unit": "ms",
            "scope": "qa completed cases (whole case wall time)",
            "turn_latency": {
                **latency_summary(turn_latencies),
                "unit": "ms",
                "scope": "all qa user turns with a recorded latency",
                "raw_ms": [round(float(value), 3) for value in turn_latencies],
            },
        },
        "usage": qa_usage,
        "fault_usage": fault_usage,
        "usage_all": all_usage,
        "tokens": {
            "sum": qa_usage["reported_token_sum"],
            "n": qa_usage["token_samples_n"],
            "note": "sum of available API usage totals; missing values not estimated",
        },
        "cost": {
            "value": None,
            "currency": None,
            "note": "no configured per-token price; cost is null by design",
        },
        "denominator_note": (
            "qa_total excludes fault_injection cases. passed/failed/pending_human "
            "are separate; model errors and skipped are retained and not counted as pass."
        ),
    }


__all__ = [
    "SCORER_VERSION",
    "STABLE_STATE_KEYS",
    "aggregate_case_results",
    "citation_names",
    "has_semantic_dimensions",
    "latency_summary",
    "overall_status",
    "score_citations",
    "score_state_subset",
    "semantic_review_items",
]
