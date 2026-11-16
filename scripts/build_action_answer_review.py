# -*- coding: utf-8 -*-
"""Build the isolated review workspace for the two post-fix live dialogues.

This script is read-only with respect to the frozen v1 dataset, reviews, old
runs, the product runtime and the Judge core.  It projects the two real
sessions from the smoke run into:

  outputs/action-state-answer-review-v1/
    dataset/{suite.json,cases.jsonl,split.json}
    results/<run-id>/{run.json,cases.jsonl}
    reviews/                 (left empty for the user)
    judge_results/           (empty until the generic Judge CLI runs)
    source-map.json
    program-checks.json
    evidence_bundles/        (written later by the generic recorded-run adapter)

The projection records a per-turn state snapshot (never the final session state
as a substitute) and per-turn trace slices.  It does not call a model.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "outputs" / "action-state-contract-smoke-20261114-3"
OUT = ROOT / "outputs" / "action-state-answer-review-v1"
CONTRACT = ROOT / "docs" / "action-state-contract.md"
RUN_ID = "asc-live-development-20261114-cdf5aac89d"
SUITE_ID = "robot-support-action-answer-review-v1"
REVISION = "v1"
ARTIFACT_ID = "robot_vacuum-v4-cdf5aac89d"

CASE_SPECS = [
    {
        "id": "asc-dev-contact-correction-01",
        "family_id": "fam-dev-action-state-contact-correction",
        "session_key": "S1",
        "title": "触点清洁执行未说明结果，随后更正为未擦触点只清障",
        "old_action": "clean_contacts",
        "old_label": "清洁充电触点",
        "new_action": "clear_dock_obstacles",
        "new_label": "清理充电座周围障碍物",
        "correction_text": "说错了，我没擦触点，只是把充电座周围清了一下。",
    },
    {
        "id": "asc-dev-dock-correction-01",
        "family_id": "fam-dev-action-state-dock-correction",
        "session_key": "S2",
        "title": "换新座执行未说明结果，随后更正为只挪旧座",
        "old_action": "replace_dock",
        "old_label": "更换新的充电座",
        "new_action": "move_dock",
        "new_label": "挪动原来的充电座",
        "correction_text": "说错了，不是换新座，只是把旧充电座挪到了客厅。",
    },
]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def case_hash(case: dict[str, Any]) -> str:
    """Exact review_server.case_hash algorithm."""
    payload = {key: value for key, value in case.items() if key != "review"}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(raw.encode("utf-8"))


def load_transcript(session_id: str) -> list[dict[str, Any]]:
    path = SMOKE / "instance-storage" / "sessions" / session_id / "transcript.jsonl"
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            row = json.loads(line)
            row["_line"] = line_number
            rows.append(row)
    return rows


def load_trace(session_id: str) -> tuple[Path, list[dict[str, Any]]]:
    path = SMOKE / "instance-storage" / "sessions" / session_id / "outputs" / "traces" / "trace_20261114.jsonl"
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            row = json.loads(line)
            row["_line"] = line_number
            rows.append(row)
    return path, rows


def parse_ts(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def dedupe_tool_outputs(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    starts: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event") == "tool_start" and event.get("tool_call_id"):
            starts[str(event["tool_call_id"])] = event
    unique: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for event in events:
        if event.get("event") != "tool_end":
            continue
        call_id = str(event.get("tool_call_id") or "")
        start = starts.get(call_id, {})
        output = str(event.get("output") or "")
        input_args = str(start.get("input_args") or start.get("input") or "")
        key = "\n".join(
            [
                str(event.get("tool") or ""),
                input_args,
                output,
                str(event.get("status") or ""),
            ]
        )
        source_names = re.findall(r"【来源：(.*?)】", output)
        alias = {
            "tool_call_id": call_id or None,
            "tool_span_id": event.get("tool_span_id"),
            "run_id": event.get("run_id"),
            "trace_id": event.get("trace_id"),
            "ts": event.get("ts"),
            "event_line": event.get("_line"),
        }
        if key not in unique:
            unique[key] = {
                "tool": event.get("tool"),
                "status": event.get("status"),
                "latency_ms": event.get("latency_ms"),
                "query": None,
                "input_args": input_args,
                "output": output,
                "source_names": sorted(set(source_names)),
                "fragment_count": len(source_names),
                "aliases": [alias],
                "trace_event_refs": [
                    {
                        "kind": "tool_start",
                        "tool_call_id": call_id or None,
                        "tool_span_id": start.get("tool_span_id"),
                        "run_id": start.get("run_id"),
                        "trace_id": start.get("trace_id"),
                        "ts": start.get("ts"),
                        "event_line": start.get("_line"),
                    },
                    {
                        "kind": "tool_end",
                        "tool_call_id": call_id or None,
                        "tool_span_id": event.get("tool_span_id"),
                        "run_id": event.get("run_id"),
                        "trace_id": event.get("trace_id"),
                        "ts": event.get("ts"),
                        "event_line": event.get("_line"),
                    },
                ],
            }
        else:
            unique[key]["aliases"].append(alias)
    # Parse the query from the paired tool_start JSON when available.
    for record in unique.values():
        try:
            parsed = json.loads(record["input_args"])
            if isinstance(parsed, dict):
                record["query"] = parsed.get("query")
        except (ValueError, TypeError):
            record["query"] = None
    return list(unique.values())


def trace_slice(events: list[dict[str, Any]], user_at: float, assistant_at: float) -> dict[str, Any] | None:
    groups: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        trace_id = event.get("trace_id")
        if trace_id:
            groups.setdefault(str(trace_id), []).append(event)
    candidates = []
    for trace_id, group in groups.items():
        times = [parse_ts(str(item.get("ts"))) for item in group if item.get("ts")]
        if not times:
            continue
        lo, hi = min(times), max(times)
        if lo >= user_at - 1.0 and hi <= assistant_at + 1.0:
            candidates.append((lo, hi, trace_id, group))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    lo, hi, trace_id, group = candidates[0]
    counts: dict[str, int] = {}
    for event in group:
        kind = str(event.get("event") or "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return {
        "trace_id": trace_id,
        "thread_id": group[0].get("thread_id"),
        "run_ids": sorted({str(item.get("run_id")) for item in group if item.get("run_id")}),
        "parent_run_ids": sorted({str(item.get("parent_run_id")) for item in group if item.get("parent_run_id")}),
        "started_at": min((str(item.get("ts")) for item in group if item.get("ts")), default=None),
        "ended_at": max((str(item.get("ts")) for item in group if item.get("ts")), default=None),
        "event_counts": dict(sorted(counts.items())),
        "event_lines": [item.get("_line") for item in group],
        "events": [
            {
                "event": item.get("event"),
                "ts": item.get("ts"),
                "run_id": item.get("run_id"),
                "parent_run_id": item.get("parent_run_id"),
                "node": item.get("node"),
                "model": item.get("model"),
                "tool": item.get("tool"),
                "tool_call_id": item.get("tool_call_id"),
                "tool_span_id": item.get("tool_span_id"),
                "call_span_id": item.get("call_span_id"),
                "status": item.get("status"),
                "event_line": item.get("_line"),
            }
            for item in group
        ],
        "tool_outputs": dedupe_tool_outputs(group),
        "source_kind": "turn_trace_slice",
        "not_session_cumulative": True,
    }


def per_turn_snapshot(turn: dict[str, Any]) -> dict[str, Any]:
    """A snapshot copied from this turn's run row, not the final session state."""
    return {
        "snapshot_scope": "turn",
        "source": "run.json.sessions.<S>.turns[<turn>]",
        "active_case_id": turn.get("active_case_id"),
        "case_status": turn.get("case_status"),
        "case_stage": turn.get("case_stage"),
        "case_tried": turn.get("case_tried"),
        "case_not_tried": turn.get("case_not_tried"),
        "effective_tried": turn.get("effective_tried"),
        "effective_not_tried": turn.get("effective_not_tried"),
        "retracted_tried": turn.get("retracted_tried"),
        "superseded_not_tried": turn.get("superseded_not_tried"),
        "fact_audit": turn.get("fact_audit"),
        "negation_ambiguity": turn.get("negation_ambiguity"),
        "last_fact_binding": turn.get("last_fact_binding"),
        "last_asked_actions": turn.get("last_asked_actions"),
        "questions": turn.get("questions"),
        "next_step": turn.get("next_step"),
        "case_resolution": turn.get("case_resolution"),
        "case_resolved_at": turn.get("case_resolved_at"),
        "closure_evidence": turn.get("closure_evidence"),
        "semantic_method": (turn.get("semantic") or {}).get("method"),
        "semantic_validation_errors": (turn.get("semantic") or {}).get("validation_errors") or [],
        "activation_evidence": turn.get("activation_evidence"),
        "trace_summary": turn.get("turn_trace"),
        "main_agent_requests": turn.get("main_agent_requests"),
    }


def rubric_for(case: dict[str, Any]) -> list[dict[str, Any]]:
    old = case["old_label"]
    new = case["new_label"]
    items = [
        {
            "criterion_id": "turn-1-user-intent-and-outcome-separation",
            "focus": "task_completion",
            "turn_scope": 1,
            "dimension": "turn_intent_and_outcome",
            "text": f"Turn 1: recognize the user's actual intent and the stated execution fact ({old} performed) while keeping the outcome unknown; do not state it failed or succeeded.",
        },
        {
            "criterion_id": "turn-1-single-step-and-no-unsupported-claim",
            "focus": "task_completion",
            "turn_scope": 1,
            "dimension": "single_step",
            "text": "Turn 1: give one appropriate next step or necessary clarification (confirm the outcome) without inventing a result, closing the case, or recommending a hardware replacement.",
        },
        {
            "criterion_id": "turn-1-evidence-support",
            "focus": "evidence_support",
            "turn_scope": 1,
            "dimension": "evidence_support",
            "text": "Turn 1 evidence support: any factual claim about contacts/dock/cleaning effectiveness or clearance/sensor behavior must be supported by the current retrieval excerpt; filename-only or fabricated citations do not count.",
        },
        {
            "criterion_id": "turn-2-correction-retracts-old-fact",
            "focus": "task_completion",
            "turn_scope": 2,
            "dimension": "correction_retraction",
            "text": f"Turn 2: treat the user's correction as authoritative: retract the earlier {old} executed fact, record {new} as executed with outcome unknown, and do not keep the old fact as current.",
        },
        {
            "criterion_id": "turn-2-does-not-repeat-negated-assumption",
            "focus": "task_completion",
            "turn_scope": 2,
            "dimension": "negated_assumption",
            "text": f"Turn 2: do not repeat or re-suggest the negated assumption ({old}); give one appropriate next step/necessary clarification and keep the case active.",
        },
        {
            "criterion_id": "turn-2-evidence-support",
            "focus": "evidence_support",
            "turn_scope": 2,
            "dimension": "evidence_support",
            "text": "Turn 2 evidence support: maintenance/clearance/IR or other factual assertions must be supported by current or legitimately reused historical retrieval; historical reuse is allowed.",
        },
        {
            "criterion_id": "turn-3-recall-current-facts-only",
            "focus": "task_completion",
            "turn_scope": 3,
            "dimension": "recall_current_facts",
            "text": f"Turn 3: recall only currently effective facts: {new} executed with outcome unknown; {old} explicitly not performed. Do not present retracted facts as current.",
        },
        {
            "criterion_id": "turn-3-unknown-not-success-and-no-forced-closure",
            "focus": "task_completion",
            "turn_scope": 3,
            "dimension": "unknown_not_success",
            "text": "Turn 3: do not turn unknown outcome into success, do not resolve the case without evidence, and give at most one appropriate next step.",
        },
        {
            "criterion_id": "turn-3-citation-applicability",
            "focus": "evidence_support",
            "turn_scope": 3,
            "dimension": "citation_applicability",
            "text": "Turn 3 citation applicability: pure recall/clarification with no external factual claim makes retrieval/citation not_applicable; if a maintenance/domain assertion is added, it must be source-supported.",
        },
        {
            "criterion_id": "case-task-completion",
            "focus": "task_completion",
            "turn_scope": 0,
            "dimension": "case_task_completion",
            "text": "Overall task completion across the three actual user turns, including correction consistency and no premature closure.",
        },
        {
            "criterion_id": "case-evidence-support",
            "focus": "evidence_support",
            "turn_scope": 0,
            "dimension": "case_evidence_support",
            "text": "Overall evidence support across the three actual answers; citation applicability and historical retrieval reuse are judged per turn; no fabricated source support.",
        },
    ]
    return items


def expected_for_turn(case: dict[str, Any], turn_index: int) -> dict[str, Any]:
    old = case["old_label"]
    new = case["new_label"]
    common = {"case_status": "active", "case_resolution": None}
    if turn_index == 1:
        return {
            **common,
            "required_claims": [f"用户明确说自己做过“{old}”，但尚未说明效果。"],
            "forbidden_claims": [f"不能说“{old}”没用/失败，也不能说已经恢复或结案。"],
            "required_behaviors": ["把执行与效果未知分开，给出一个确认结果或必要澄清的下一步。"],
            "expected_state": {
                "effective_tried": [[case["old_action"], "unknown"]],
                "effective_not_tried": [],
                "fact_audit": [],
            },
            "citation_applicability": "required_if_external_fact_claimed",
        }
    if turn_index == 2:
        return {
            **common,
            "required_claims": [f"更正经确认：“{old}”没有做，实际只做了“{new}”，效果未知。"],
            "forbidden_claims": [f"不能继续把“{old}”当作已做事实，也不能重复建议这一步。"],
            "required_behaviors": [f"撤销旧事实，记录“{new}”为已做/效果未知，保持 case active，只推进一个下一步。"],
            "expected_state": {
                "effective_tried": [[case["new_action"], "unknown"]],
                "effective_not_tried": [case["old_action"]],
                "fact_audit_kind": "retract_tried",
            },
            "citation_applicability": "required_if_external_fact_claimed",
        }
    return {
        **common,
        "required_claims": [f"只回忆当前有效事实：“{new}”已做/结果未说明；“{old}”明确没做。"],
        "forbidden_claims": ["不能把结果未知说成成功/没效果，也不能把已撤销旧事实说成当前事实。"],
        "required_behaviors": ["只复述当前有效事实，最多一个下一步；纯回忆轮不强制检索/引用。"],
        "expected_state": {
            "effective_tried": [[case["new_action"], "unknown"]],
            "effective_not_tried": [case["old_action"]],
            "case_status": "active",
        },
        "citation_applicability": "not_applicable_for_pure_recall",
    }


def build_case(case_spec: dict[str, Any], run: dict[str, Any], source_hashes: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    session = run["sessions"][case_spec["session_key"]]
    session_id = session["session_id"]
    transcript = load_transcript(session_id)
    trace_path, trace_events = load_trace(session_id)
    users = [row for row in transcript if row.get("role") == "user"]
    assistants = [row for row in transcript if row.get("role") == "assistant"]
    run_turns = session["turns"]
    if not (len(users) == len(assistants) == len(run_turns) == 3):
        raise SystemExit(f"{case_spec['id']}: expected 3 complete user/assistant turns, got {len(users)}/{len(assistants)}/{len(run_turns)}")

    turns: list[dict[str, Any]] = []
    trace_map: list[dict[str, Any]] = []
    previous_retrieval: list[dict[str, Any]] = []
    for index, (user, assistant, run_turn) in enumerate(zip(users, assistants, run_turns), 1):
        if user.get("turn") != index or assistant.get("turn") != index:
            raise SystemExit(f"{case_spec['id']}: transcript turn index mismatch at {index}")
        if user.get("content") != session["questions"][index - 1]:
            raise SystemExit(f"{case_spec['id']}: transcript user content differs from run questions at turn {index}")
        if assistant.get("content") != run_turn.get("answer"):
            raise SystemExit(f"{case_spec['id']}: transcript assistant answer differs from run answer at turn {index}")
        user_at = float(user["at"])
        assistant_at = float(assistant["at"])
        sliced = trace_slice(trace_events, user_at, assistant_at)
        if index < 3 and sliced is None:
            raise SystemExit(f"{case_spec['id']}: missing turn-level trace slice at turn {index}")
        if index == 3 and sliced is not None and sliced.get("event_counts"):
            raise SystemExit(f"{case_spec['id']}: turn 3 unexpectedly has model/tool trace; refusing to attach")
        current = [] if sliced is None else sliced.get("tool_outputs") or []
        retrieval = {
            "current": current,
            "history": previous_retrieval,
            "current_status": "retrieved" if current else "not_retrieved",
            "history_note": "Earlier turns' real tool outputs are retained for legitimate historical reuse; no forced RAG on recall.",
        }
        snapshot = per_turn_snapshot(run_turn)
        turns.append(
            {
                "index": index,
                "role": "user",
                "content": user["content"],
                "answer": assistant["content"],
                "answer_chars": len(assistant["content"]),
                "message_id": user.get("message_id"),
                "assistant_message_id": assistant.get("message_id"),
                "user_at": user_at,
                "assistant_at": assistant_at,
                "request_id": run_turn.get("request_id"),
                "trace_id": None if sliced is None else sliced["trace_id"],
                "trace_run_ids": [] if sliced is None else sliced["run_ids"],
                "trace": sliced,
                "retrieval": retrieval,
                "state": snapshot,
                "state_checks": [],
                "errors": [] if run_turn.get("http_status") == 200 else [f"http_status={run_turn.get('http_status')}"],
                "live_variant": "recorded_live_session",
                "normative_state": "actual_turn_snapshot",
            }
        )
        trace_map.append(
            {
                "turn": index,
                "request_id": run_turn.get("request_id"),
                "trace_id": None if sliced is None else sliced["trace_id"],
                "trace_run_ids": [] if sliced is None else sliced["run_ids"],
                "trace_event_lines": [] if sliced is None else sliced["event_lines"],
                "transcript_user_line": user.get("_line"),
                "transcript_assistant_line": assistant.get("_line"),
                "user_message_id": user.get("message_id"),
                "assistant_message_id": assistant.get("message_id"),
                "user_at": user_at,
                "assistant_at": assistant_at,
                "tool_output_count": len(current),
            }
        )
        previous_retrieval = previous_retrieval + [
            {**record, "source_turn": index} for record in current
        ]

    rubric = rubric_for(case_spec)
    case = {
        "id": case_spec["id"],
        "family_id": case_spec["family_id"],
        "category": "multi_turn",
        "split": "development",
        "title": case_spec["title"],
        "origin": {
            "kind": "live_derived_projection",
            "source_run_id": RUN_ID,
            "source_smoke_run_id": run.get("schema_version"),
            "source_session_id": session_id,
            "source_turns": [1, 2, 3],
            "source_artifact_id": ARTIFACT_ID,
        },
        "review": {"status": "pending_human"},
        "turns": [
            {
                "role": "user",
                "content": turn["content"],
                "expected": expected_for_turn(case_spec, turn["index"]),
            }
            for turn in turns
        ],
        "answer_review_rubric": rubric,
        "evidence": [],
        "setup": {},
        "scoring": {
            "automatic": [
                "dataset.schema",
                "dataset.counts_2",
                "dataset.split_disjoint",
                "evidence.real_source_projection",
                "review.pending_human",
            ],
            "human": [
                "answer.actual_answer_semantics",
                "answer.correction_and_negation_lifecycle",
                "answer.unknown_vs_success",
                "answer.recall_current_facts_only",
                "answer.citation_applicability_and_semantic_support",
            ],
            "denominator": "qa",
            "expected_sources_mode": "current_or_legitimate_history",
            "state_score": "not_applicable_program_checks_are_structural",
        },
        "state_scope": "development_action_state_answer_review",
    }
    result_row = {
        "case_id": case["id"],
        "mode": "live",
        "status": "completed",
        "attempted": 3,
        "session_id": session_id,
        "started_at": run.get("started_at"),
        "ended_at": run.get("finished_at"),
        "latency_ms": int(round((turns[-1]["assistant_at"] - turns[0]["user_at"]) * 1000)),
        "turns": [
            {
                "index": turn["index"],
                "role": "user",
                "content": turn["content"],
                "answer": turn["answer"],
                "answer_chars": turn["answer_chars"],
                "message_id": turn["message_id"],
                "assistant_message_id": turn["assistant_message_id"],
                "user_at": turn["user_at"],
                "assistant_at": turn["assistant_at"],
                "request_id": turn["request_id"],
                "trace_id": turn["trace_id"],
                "trace_run_ids": turn["trace_run_ids"],
                "retrieval": turn["retrieval"],
                "state": turn["state"],
                "trace": turn["trace"],
                "events": [] if turn["trace"] is None else turn["trace"]["events"],
                "errors": turn["errors"],
                "live_variant": turn["live_variant"],
                "normative_state": turn["normative_state"],
            }
            for turn in turns
        ],
        "checks": [],
        "trace_ids": [turn["trace_id"] for turn in turns if turn["trace_id"]],
        "source": {
            "smoke_root": str(SMOKE.relative_to(ROOT)).replace("\\", "/"),
            "transcript_file": f"instance-storage/sessions/{session_id}/transcript.jsonl",
            "transcript_sha256": source_hashes["transcripts"][session_id],
            "trace_file": f"instance-storage/sessions/{session_id}/outputs/traces/trace_20261114.jsonl",
            "trace_sha256": source_hashes["traces"][session_id],
            "contract_sha256": source_hashes["contract_sha256"],
            "run_json_sha256": source_hashes["run_json_sha256"],
            "artifact_id": ARTIFACT_ID,
            "session_id": session_id,
            "trace_slices_are_turn_level": True,
        },
    }
    return case, result_row, {"case_id": case["id"], "session_id": session_id, "turns": trace_map, "trace_path": str(trace_path)}


def review_aggregate(reviews_dir: Path) -> str | None:
    if not reviews_dir.exists():
        return None
    parts = []
    for path in sorted(reviews_dir.rglob("*.json")):
        parts.append(f"{path.relative_to(reviews_dir).as_posix()}:{sha256_file(path)}")
    return sha256_bytes("\n".join(parts).encode("utf-8")) if parts else None


def main() -> None:
    smoke_run_path = SMOKE / "run.json"
    run = read_json(smoke_run_path)
    if run.get("artifact", {}).get("artifact_id") != ARTIFACT_ID:
        raise SystemExit("unexpected artifact id in smoke run")
    if run.get("error") is not None:
        raise SystemExit("smoke run has an error; refusing to project")

    dataset_dir = ROOT / "eval" / "datasets" / "v1"
    dataset_hashes = {
        "cases.jsonl": sha256_file(dataset_dir / "cases.jsonl"),
        "suite.json": sha256_file(dataset_dir / "suite.json"),
        "split.json": sha256_file(dataset_dir / "split.json"),
        "holdout_cases_sha256": read_json(dataset_dir / "suite.json").get("source_snapshot", {}).get("holdout_cases_sha256"),
    }
    reviews_dir = ROOT / "eval" / "reviews"
    reviews_before = review_aggregate(reviews_dir)

    source_hashes: dict[str, Any] = {
        "contract_sha256": sha256_file(CONTRACT),
        "run_json_sha256": sha256_file(smoke_run_path),
        "transcripts": {},
        "traces": {},
    }
    for spec in CASE_SPECS:
        session_id = run["sessions"][spec["session_key"]]["session_id"]
        source_hashes["transcripts"][session_id] = sha256_file(
            SMOKE / "instance-storage" / "sessions" / session_id / "transcript.jsonl"
        )
        source_hashes["traces"][session_id] = sha256_file(
            SMOKE / "instance-storage" / "sessions" / session_id / "outputs" / "traces" / "trace_20261114.jsonl"
        )

    cases = []
    result_rows = []
    source_map_cases = []
    for spec in CASE_SPECS:
        case, row, mapping = build_case(spec, run, source_hashes)
        cases.append(case)
        result_rows.append(row)
        source_map_cases.append(mapping)

    # The two new cases deliberately do not enter the frozen v1 split.  This
    # local split is read only by the isolated review server invocation.
    split = {
        "schema_version": "robot-support-eval/v1",
        "suite_id": SUITE_ID,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "development": {"count": len(cases), "case_ids": [case["id"] for case in cases], "families": {}},
        "holdout": {"count": 0, "case_ids": [], "note": "No holdout case exists in this isolated answer-review workspace."},
        "frozen_v1_untouched": True,
        "frozen_v1_hashes": dataset_hashes,
    }
    suite = {
        "schema_version": "robot-support-eval/v1",
        "suite_id": SUITE_ID,
        "revision": REVISION,
        "cases_file": "cases.jsonl",
        "split_file": "split.json",
        "counts": {"development": len(cases), "holdout": 0},
        "source_snapshot": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_run_id": RUN_ID,
            "source_smoke_run": "outputs/action-state-contract-smoke-20261114-3/run.json",
            "artifact_id": ARTIFACT_ID,
            "contract": "docs/action-state-contract.md",
            "contract_sha256": source_hashes["contract_sha256"],
            "run_json_sha256": source_hashes["run_json_sha256"],
            "transcripts": source_hashes["transcripts"],
            "traces": source_hashes["traces"],
            "frozen_v1_hashes": dataset_hashes,
            "note": "Isolated two-dialogue/three-turn development answer review; not the frozen v1 32/16 split.",
        },
        "review_status": "pending_human",
    }
    run_projection = {
        "schema_version": "action-state-answer-review-run/v1",
        "run_id": RUN_ID,
        "split": "development",
        "mode": "live",
        "kind": "answer_review_projection",
        "status": "completed",
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "dataset": {
            "suite_id": SUITE_ID,
            "revision": REVISION,
            "file_sha256": {},
            "dataset_hash": None,
        },
        "source": {
            "smoke_run_id": run.get("schema_version"),
            "smoke_run_file": "outputs/action-state-contract-smoke-20261114-3/run.json",
            "smoke_run_sha256": source_hashes["run_json_sha256"],
            "artifact_id": ARTIFACT_ID,
            "artifact_root": run.get("artifact", {}).get("artifact_root"),
            "runtime_hash": run.get("artifact", {}).get("runtime_hash"),
            "sessions": {
                spec["id"]: run["sessions"][spec["session_key"]]["session_id"] for spec in CASE_SPECS
            },
            "contract": "docs/action-state-contract.md",
            "contract_sha256": source_hashes["contract_sha256"],
        },
        "cases": [
            {
                "case_id": case["id"],
                "session_id": row["session_id"],
                "turns": 3,
                "request_ids": [turn["request_id"] for turn in row["turns"]],
                "trace_ids": [turn["trace_id"] for turn in row["turns"]],
            }
            for case, row in zip(cases, result_rows)
        ],
        "note": "Read-only projection of two real smoke sessions; no product model was called.",
    }

    # Write dataset + results before hashes are stamped into run.json.
    write_jsonl(OUT / "dataset" / "cases.jsonl", cases)
    write_json(OUT / "dataset" / "suite.json", suite)
    write_json(OUT / "dataset" / "split.json", split)
    write_jsonl(OUT / "results" / RUN_ID / "cases.jsonl", result_rows)
    write_json(OUT / "results" / RUN_ID / "run.json", run_projection)

    suite_bytes = (OUT / "dataset" / "suite.json").read_bytes()
    cases_bytes = (OUT / "dataset" / "cases.jsonl").read_bytes()
    split_bytes = (OUT / "dataset" / "split.json").read_bytes()
    run_bytes = (OUT / "results" / RUN_ID / "run.json").read_bytes()
    run_cases_bytes = (OUT / "results" / RUN_ID / "cases.jsonl").read_bytes()
    file_hashes = {
        "suite.json": sha256_bytes(suite_bytes),
        "cases.jsonl": sha256_bytes(cases_bytes),
        "split.json": sha256_bytes(split_bytes),
    }
    dataset_hash = sha256_bytes(
        json.dumps(
            {
                "suite": file_hashes["suite.json"],
                "cases": file_hashes["cases.jsonl"],
                "split": file_hashes["split.json"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    run_projection["dataset"]["file_sha256"] = file_hashes
    run_projection["dataset"]["dataset_hash"] = dataset_hash
    run_projection["run_cases_sha256"] = sha256_bytes(run_cases_bytes)
    write_json(OUT / "results" / RUN_ID / "run.json", run_projection)
    run_file_sha256 = sha256_bytes((OUT / "results" / RUN_ID / "run.json").read_bytes())

    (OUT / "reviews").mkdir(parents=True, exist_ok=True)
    (OUT / "judge_results").mkdir(parents=True, exist_ok=True)

    program_checks = {
        "source": "outputs/action-state-contract-smoke-20261114-3/run.json#checks",
        "source_run_id": run.get("schema_version"),
        "artifact_id": ARTIFACT_ID,
        "passed": run.get("passed"),
        "checks": dict(run.get("checks") or {}),
        "meta_state_checks": run.get("meta_state_checks"),
        "note": "Frozen structural checks from the smoke run. These are not semantic answer judgments and are not an oracle for the Judge.",
    }
    write_json(OUT / "program-checks.json", program_checks)

    source_map = {
        "schema_version": "action-state-answer-review-source-map/v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_id": RUN_ID,
        "artifact": {
            "artifact_id": ARTIFACT_ID,
            "artifact_root": run.get("artifact", {}).get("artifact_root"),
            "runtime_hash": run.get("artifact", {}).get("runtime_hash"),
            "source": run.get("artifact", {}).get("source"),
        },
        "source_hashes": {
            "contract": {"path": "docs/action-state-contract.md", "sha256": source_hashes["contract_sha256"]},
            "run_json": {"path": "outputs/action-state-contract-smoke-20261114-3/run.json", "sha256": source_hashes["run_json_sha256"]},
            "transcripts": {
                session_id: {
                    "path": f"outputs/action-state-contract-smoke-20261114-3/instance-storage/sessions/{session_id}/transcript.jsonl",
                    "sha256": digest,
                }
                for session_id, digest in source_hashes["transcripts"].items()
            },
            "traces": {
                session_id: {
                    "path": f"outputs/action-state-contract-smoke-20261114-3/instance-storage/sessions/{session_id}/outputs/traces/trace_20261114.jsonl",
                    "sha256": digest,
                }
                for session_id, digest in source_hashes["traces"].items()
            },
        },
        "frozen_v1_hashes": dataset_hashes,
        "reviews_aggregate_before": reviews_before,
        "cases": source_map_cases,
        "turn_mapping_note": "Turn-isolated trace slices are selected by user/assistant transcript timestamps; no session-cumulative trace is used for a turn.",
    }
    write_json(OUT / "source-map.json", source_map)

    # Write a small workspace README without making any human review claim.
    (OUT / "README.md").write_text(
        "\n".join(
            [
                "# Action-state answer review workspace (isolated)",
                "",
                f"- run_id: `{RUN_ID}`",
                f"- artifact: `{ARTIFACT_ID}`",
                "- dataset: 2 development answer-review cases; not the frozen v1 32/16 split",
                "- results: read-only projection of the two real smoke sessions (6 turns)",
                "- reviews/: empty; the user writes human reviews here",
                "- judge_results/: machine results only; no human review is auto-filled",
                "- source-map.json: turn/request/trace/session/hash mapping",
                "- program-checks.json: frozen 13/13 structural checks; not semantic judgment",
                "",
            ]
        ),
        encoding="utf-8",
    )

    print(json.dumps({
        "workspace": str(OUT),
        "run_id": RUN_ID,
        "cases": [{"id": case["id"], "case_hash": case_hash(case)} for case in cases],
        "source_hashes": source_hashes,
        "frozen_v1_hashes": dataset_hashes,
        "reviews_aggregate_before": reviews_before,
        "run_file_sha256": run_file_sha256,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
