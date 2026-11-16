#!/usr/bin/env python
"""Offline evidence extraction for the approved development 24-QA run.

Read-only inputs:
  outputs/approved-development-{baseline,selection,freeze}.json
  eval/datasets/v1/cases.jsonl
  eval/results/<qa-run>/{run.json,cases.jsonl,instance-storage/...}
  eval/results/<fault-run>/{run.json,cases.jsonl,instance-storage/...}
  eval/reviews/**

Writes exactly one repository file (the JSON named by --out).  It never calls a
model, never starts/stops a service, never writes eval/reviews, and never reads
environment values or .env contents.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
QA_RUN = "v1-live-development-20261113-224057-3f8412"
FAULT_RUN = "v1-live-development-20261113-224522-4ceb45"
DATASET = ROOT / "eval" / "datasets" / "v1" / "cases.jsonl"

# The human run-answer review objects are exported by the evaluator, not by
# this script.  This mapping records only evidence attribution; it does not
# change any expected value, case, run, score, or review record.
ATTRIBUTION: dict[str, dict[str, Any]] = {
    "rsv1-dev-ambiguity-recharge-condition-01": {
        "class": "confirmed_product_defect",
        "layers": ["state", "validation", "prompt"],
        "mechanism": "RAG ran but the answer omitted all filenames; at the same time the recharge interpretation existed (last_turn_scope=recharge) while active_case_id/cases stayed empty.",
        "confirmed": [
            "state activation gap: expected case_status=active/scope=recharge, actual null/null although the turn was routed to recharge and RAG retrieved 故障排除.txt",
            "citation omission: expected_sources.cited 0/2 after a successful this-turn retrieval; actual answer has no knowledge filename",
        ],
        "excluded": [
            "RAG bypass is excluded for this case: 2 retrieval queries and recorded rag_retrieve events exist",
        ],
        "unknown": [
            "The exact state-activation branch is not line-localized in this pass; state.py hash matches the run artifact (3da14594...), but the report keeps this at the state-layer level rather than claiming a line-number defect.",
        ],
        "fix": "Activate a recharge case on the first explicit recharge report even when one referent ambiguity is open; add a post-generation citation check that rejects a substantive answer using retrieved knowledge without a source name.",
    },
    "rsv1-dev-direct-base-power-01": {
        "class": "confirmed_product_defect",
        "layers": ["state", "execution", "validation"],
        "mechanism": "A first-turn symptom ('放上去没反应') matched the follow-up regex and a deterministic clarification was returned before the agent/RAG. The answer is stale-context text ('你说的还是不行...') for a first report.",
        "confirmed": [
            "only one model request, and it is interpretation (930 tokens); zero agent calls, zero retrieval, empty trace_ids",
            "support.last_decision route=semantic, reason=ambiguous_new_information_or_correction; state.case_status/scope passed, so the case exists but the answer was produced before the agent",
            "The current source maps the state regex to the same lines as the run artifact (state.py sha256 3da14594...); the server.py line 590-624 is current-source corroboration: the run artifact server.py differs (sha256 0a60d344... vs current ca60ab0b...), and its equivalent direct_answer assignment is at line 556, not line 590. Exact line-by-line artifact diff was not re-run.",
        ],
        "excluded": [
            "model error and timeout are excluded: usage is reported and runtime.no_error passed",
        ],
        "unknown": [
            "The exact reason the first-report guard did not suppress this branch is not instrumented; code path evidence points to the ambiguous-follow-up branch rather than to RAG failure.",
        ],
        "fix": "Gate direct_answer/ambiguous-follow-up on an existing case with prior suggestions or turn > source_turn; on first report always run the main agent and retrieval. Keep '没反应' as a symptom, not as a follow-up.",
    },
    "rsv1-dev-multi-recharge-closure-02": {
        "class": "confirmed_path_difference_plus_independent_defect",
        "layers": ["state", "context", "tools", "validation"],
        "mechanism": "The fixture assistant turn was not replayed; the model generated a different prior turn. On the manual-success turn there was no this-turn retrieval, yet the answer cited three filenames and ignored the user's manual-charge success.",
        "confirmed": [
            "live T1 model/agent generated its own advice (clean dock obstacles); fixture '先清障、擦传感器。' was retained but not replayed",
            "T3 retrieval.attempted fail attempts=0; cited 扫地机器人100问2.txt/故障排除.txt/选购指南.txt were all present in T1 retrieval sources. The current scorer only maps this-turn fragments; this is traceable historical reuse, not proof of a no-source claim",
            "state records clear_dock_obstacles=failed but does not record the explicit manual placement success; expected reseat_dock=success",
            "answer T3 switches to clean contacts without using the manual-charge vs auto-return distinction",
        ],
        "citation_provenance": {
            "status": "known_traceable_historical_reuse",
            "cited": ["扫地机器人100问2.txt", "故障排除.txt", "选购指南.txt"],
            "prior_turn_sources": ["扫地机器人100问2.txt", "故障排除.txt", "维护保养.txt", "选购指南.txt"],
            "current_scorer": "this_turn_only",
            "conclusion": "not evidence that the cited files never supported an answer; only this-turn provenance is unavailable",
        },
        "excluded": [
            "The whole failure cannot be called a frozen-case regression: the prior assistant path differs by construction.",
        ],
        "unknown": [
            "Whether the expected action_id reseat_dock is the intended taxonomy for '手动放回'; the oracle does not define this mapping.",
        ],
        "fix": "Add partial-success/manual-vs-auto evidence to state; require retrieval or a recorded prior-source provenance on evidence-bearing turns; do not let the answer cite files that were not retrieved this turn.",
    },
    "rsv1-dev-multi-recharge-correction-01": {
        "class": "oracle_outcome_ambiguity_not_confirmed_product_failure",
        "layers": ["validation", "state"],
        "mechanism": "The correction's automatic/state items match (clean_contacts removed, clear_dock_obstacles added, question superseded); semantic quality is still human-pending. The only automatic fail is expected outcome=success vs actual unknown for an action the user only says was done, while the symptom is still failing.",
        "confirmed": [
            "T2 support.tried_actions=[{clear_dock_obstacles, unknown}], not_tried=[clean_contacts]; answer text accepts the correction, but its semantic quality is still human-pending",
            "RAG succeeded on both turns and citations are present",
        ],
        "excluded": [
            "State correction and citation behavior are not product failures in this turn.",
        ],
        "unknown": [
            "The dataset does not define whether outcome=success means 'step executed' or 'problem resolved'; the user said only '只清了...' while still reporting failure.",
        ],
        "fix": "Define outcome semantics explicitly (executed / failed / unknown / resolved) before changing state extraction; keep the original expected value and record the ambiguity.",
    },
    "rsv1-dev-multi-recharge-correction-02": {
        "class": "confirmed_product_defect",
        "layers": ["state", "context"],
        "mechanism": "The action taxonomy maps '换了一个新的充电座' to reseat_dock and the answer says '重新对准'; after the correction, replace_dock is not reopened as not-tried and the stale evidence remains attached to reseat_dock.",
        "confirmed": [
            "T1 expected replace_dock=unknown, actual reseat_dock=unknown; answer says '重新对准充电座'",
            "T1 had successful RAG (2 queries) but citation.present failed: no filename in the answer, so expected_sources.cited=0/1",
            "T2 expected not_tried_actions=[replace_dock], actual [] while support still cites the T1 '换了一个新的充电座' text for reseat_dock",
        ],
        "excluded": [
            "The correction sentence itself is acknowledged in the answer; the state defect is separate from the T1 citation omission.",
        ],
        "unknown": [
            "Whether the intended action id for replacing a dock is replace_dock; the dataset expected state uses it, so this is treated as the oracle key.",
        ],
        "fix": "Separate replace_dock/reseat_dock in interpretation; on correction, supersede the old action evidence and add the removed action to not_tried_actions.",
    },
    "rsv1-dev-multi-recharge-negation-01": {
        "class": "confirmed_path_difference_plus_independent_defect",
        "layers": ["state", "tools", "validation"],
        "mechanism": "The live prior assistant asked/suggested clearing, and the user then said '都没试过'; the state records no not-tried actions. The second turn also answers from memory: no retrieval, yet cites two filenames.",
        "confirmed": [
            "T2 expected not_tried=[clear_dock_obstacles, clean_contacts], actual []",
            "T2 retrieval.attempted fail attempts=0; cited 故障排除.txt/扫地机器人100问2.txt were both in T1 retrieval sources. The current scorer only maps this-turn fragments; this is historical reuse, not proof of no source",
            "T1 had successful RAG; the T2 answer repeats the T1 clearing advice",
        ],
        "citation_provenance": {
            "status": "known_traceable_historical_reuse",
            "cited": ["故障排除.txt", "扫地机器人100问2.txt"],
            "prior_turn_sources": ["扫地机器人100问2.txt", "故障排除.txt", "选购指南.txt"],
            "current_scorer": "this_turn_only",
            "conclusion": "not evidence that the cited files never supported an answer; only this-turn provenance is unavailable",
        },
        "excluded": [
            "The clean_contacts expectation is partly fixture-dependent: the live T1 did not clearly ask about contacts, unlike the fixture question.",
        ],
        "unknown": [
            "Whether a generic '都没试过' should bind to all pending suggested actions; no explicit contract is recorded.",
        ],
        "fix": "Bind generic negation to the pending suggestion set; record not-tried actions; do not answer an evidence-bearing troubleshooting turn with zero retrieval and prior-turn citations.",
    },
    "rsv1-dev-multi-recharge-negation-02": {
        "class": "automated_check_overreach_not_confirmed_product_failure",
        "layers": ["validation"],
        "mechanism": "The only failing turn is a pure clarification ('是机器人回不到座，还是您找不到座?') with no factual claim. Automatic citation/retrieval checks still require a source.",
        "confirmed": [
            "T1 retrieval.attempted fail attempts=0 and citation.present fail; answer is a single binary clarification",
            "T2 has successful RAG and state matches expected tried/not-tried actions; answer semantics remain human-pending",
        ],
        "excluded": [
            "No state, citation-on-claim, or answer-support defect is confirmed on the substantive T2.",
        ],
        "unknown": [
            "Whether the dataset requires retrieval on a pure clarification turn; the expected_sources list implies yes, the behavior rubric does not.",
        ],
        "fix": "Classify clarification-only turns as citation/retrieval not-applicable rather than fail; keep substantive evidence turns strict.",
    },
    "rsv1-dev-multi-recharge-tried-advance-02": {
        "class": "oracle_outcome_ambiguity_not_confirmed_product_failure",
        "layers": ["validation", "state"],
        "mechanism": "All three actions are present on T3; the mismatch is clear_dock_obstacles expected success vs actual unknown, while the user still reports failure. This is the same executed-vs-resolved ambiguity as correction-01.",
        "confirmed": [
            "T3 tried_actions actual clear=unknown, clean_contacts=failed, clean_sensors=failed; expected clear=success",
            "the answer text avoids repeating the three steps and asks for the dock indicator; this is an automatic/text observation, not a human semantic pass",
            "RAG succeeded on both turns",
        ],
        "excluded": [
            "No missing action, stale action, or citation defect is confirmed on T3.",
        ],
        "unknown": [
            "Whether '已经清了' alone is enough for outcome=success; no outcome contract exists.",
        ],
        "fix": "Define executed-vs-resolved outcomes; if the symptom persists, do not encode completion as success without a separate executed flag.",
    },
    "rsv1-dev-fault-process-restart-01": {
        "class": "recovery_verified_plus_oracle_path_artifacts",
        "layers": ["validation", "execution", "state"],
        "mechanism": "Same-session state survives the process restart (T2 recall lists the T1 actions; text/state match only, semantics human-pending), but the T3 empty-state assertion belongs to a 'new session B' step that the runner never executed. T1 clear=unknown vs expected success is the same outcome ambiguity.",
        "confirmed": [
            "server.log shows a new server process, GET of the same session, then T2/T3 messages; state turn and both actions are readable after restart",
            "fault.restart_session_recovered passed 2/2; T2 answer lists the two actions from turn 1",
            "only one POST /api/sessions per fault case; no session B request exists",
        ],
        "excluded": [
            "Session-isolation failure is excluded: the second-session step was not executed, so its expected empty state cannot be attributed to the product.",
            "Restart recovery failure is excluded.",
        ],
        "unknown": [
            "Why the runner skipped the documented new-session step; this is a harness/execution gap, not evidence of a product isolation defect.",
        ],
        "fix": "Either implement session B in the fault driver and record its session_id, or remove the empty-state assertion; keep restart recovery and task-state quality as separate checks.",
    },
    "rsv1-dev-fault-process-restart-02": {
        "class": "recovery_verified_plus_confirmed_state_defect",
        "layers": ["state", "context", "validation"],
        "mechanism": "Same-session recovery works, but '重启后还在吗' (service restart) is misread as the user restarting the robot, adding reset_robot. T3 also loses scope on the recall path; the new-session B step was again not executed.",
        "confirmed": [
            "T2 support.tried_actions adds reset_robot=unknown with evidence '重启后还在吗？我刚才查到哪了？'; the user never said they restarted the robot",
            "T3 state.scope fail expected recharge actual None",
            "T1 check_power expected failed, actual unknown; still-failing lamp symptom is under-reported",
            "no second POST /api/sessions; T3 remains the same session A",
        ],
        "excluded": [
            "Restart recovery failure is excluded: the same session was readable after restart and fault.restart_session_recovered passed.",
            "Session-isolation failure is excluded for the same missing-session-B reason as restart-01.",
        ],
        "unknown": [
            "Whether check_power should be failed or unknown under the unstated outcome contract.",
        ],
        "fix": "Do not extract user robot actions from service-restart/meta questions; preserve scope on recall turns; define check_power outcome; implement/record the second-session isolation step before scoring it.",
    },
}

PATH_LABEL = {
    "frozen_turns": "comparable_frozen_path",
    "model_generated_prior_turn": "path_dependent_fixture_not_replayed",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def unique_sources(retrieval: dict[str, Any] | None) -> list[str]:
    out: list[str] = []
    for row in (retrieval or {}).get("results") or []:
        src = row.get("source")
        if src and src not in out:
            out.append(src)
    return out


def knowledge_names() -> list[str]:
    names: set[str] = set()
    for row in read_jsonl(DATASET):
        for ev in row.get("evidence") or []:
            file = ev.get("file") or ""
            if file.startswith("assets/knowledge/"):
                names.add(Path(file).name)
    return sorted(names, key=len, reverse=True)


def cited_names(answer: str, names: list[str]) -> list[str]:
    found = [name for name in names if name in (answer or "")]
    return sorted(found, key=lambda n: (answer or "").find(n))


def trace_events(session_dir: Path) -> list[dict[str, Any]]:
    return read_jsonl(session_dir / "outputs" / "traces" / "trace_20261113.jsonl")


def summarize_trace(events: list[dict[str, Any]], trace_id: str | None) -> dict[str, Any]:
    if not trace_id:
        return {"trace_id": None, "events": 0, "model_calls": 0, "tool_calls_unique": 0,
                "tool_calls_raw": 0, "tools": [], "duplicate_tool_call_ids": 0}
    rows = [e for e in events if e.get("trace_id") == trace_id]
    starts = [e for e in rows if e.get("event") == "tool_start"]
    ids = [e.get("tool_call_id") for e in starts if e.get("tool_call_id")]
    counts = Counter(ids)
    return {
        "trace_id": trace_id,
        "events": len(rows),
        "model_calls": len([e for e in rows if e.get("event") == "model_end"]),
        "tool_calls_unique": len(counts),
        "tool_calls_raw": len(starts),
        "tools": sorted({e.get("tool") for e in starts if e.get("tool")}),
        "duplicate_tool_call_ids": sum(1 for n in counts.values() if n > 1),
    }


def summarize_support(support: dict[str, Any] | None) -> dict[str, Any]:
    support = support or {}
    active = support.get("active_case") or {}
    decision = support.get("last_decision") or {}
    audit = decision.get("actual_question_audit") or {}
    return {
        "turn": support.get("turn"),
        "active_case_id": active.get("case_id") or support.get("active_case_id"),
        "active_status": active.get("status"),
        "active_scope": active.get("scope"),
        "last_turn_scope": support.get("last_turn_scope"),
        "tried_actions": active.get("tried_actions") or [],
        "not_tried_actions": active.get("not_tried_actions") or [],
        "route": decision.get("route"),
        "route_reason": decision.get("reason"),
        "interpretation_method": decision.get("interpretation_method"),
        "actual_asked": len(audit.get("actual_asked") or []),
        "unexpected_asked": len(audit.get("unexpected_asked") or []),
        "unplanned_questions": len(audit.get("unplanned_questions") or []),
    }


def summarize_checks(checks: list[dict[str, Any]] | None) -> dict[str, Any]:
    checks = checks or []
    out = []
    for c in checks:
        if c.get("status") == "pass":
            continue
        out.append({
            "name": c.get("name"),
            "status": c.get("status"),
            "expected": c.get("expected"),
            "actual": c.get("actual"),
            "detail": c.get("detail"),
        })
    return {"failed": out, "statuses": dict(Counter(c.get("status") for c in checks))}


def case_evidence(row: dict[str, Any], run_dir: Path, names: list[str]) -> dict[str, Any]:
    sid = row.get("session_id")
    session_dir = run_dir / "instance-storage" / "sessions" / str(sid)
    events = trace_events(session_dir)
    transcript = read_jsonl(session_dir / "transcript.jsonl")
    dataset_turns = (row.get("case") or {}).get("turns") or []
    fixture_turns = [
        {"role": t.get("role"), "content": t.get("content") or t.get("answer") or "",
         "expected_state": (t.get("expected") or {}).get("state")}
        for t in dataset_turns
        if t.get("role") in ("assistant", "assistant_fixture")
    ]
    turns = []
    for t in row.get("turns") or []:
        answer = t.get("answer") or ""
        turns.append({
            "index": t.get("index"),
            "user": t.get("content") or "",
            "answer": answer,
            "answer_chars": len(answer),
            "answered_at": t.get("answered_at"),
            "latency_ms": t.get("latency_ms"),
            "trace_id": t.get("trace_id"),
            "trace": summarize_trace(events, t.get("trace_id")),
            "retrieval": {
                "status": (t.get("retrieval") or {}).get("status"),
                "attempts": (t.get("retrieval") or {}).get("attempts"),
                "sources": unique_sources(t.get("retrieval")),
                "cited_in_answer": cited_names(answer, names),
            },
            "support": summarize_support(t.get("support")),
            "checks": summarize_checks(t.get("state_checks")),
        })
    failed = [c for c in row.get("checks") or [] if c.get("status") != "pass"]
    return {
        "case_id": row.get("case_id"),
        "title": (row.get("case") or {}).get("title"),
        "category": row.get("category"),
        "bucket": row.get("bucket"),
        "status": row.get("status"),
        "live_variant": row.get("live_variant"),
        "fixture_policy": row.get("fixture_policy"),
        "path_comparability": PATH_LABEL.get(row.get("live_variant"), row.get("live_variant")),
        "session_id": sid,
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "latency_ms": row.get("latency_ms"),
        "usage_summary": row.get("usage_summary"),
        "transcript_path": rel(session_dir / "transcript.jsonl"),
        "trace_path": rel(session_dir / "outputs" / "traces" / "trace_20261113.jsonl"),
        "transcript_messages": len(transcript),
        "dataset_fixture_assistant_turns": fixture_turns,
        "dataset_expected_states": [
            ((t.get("expected") or {}).get("state")) for t in dataset_turns
        ],
        "actual_turns": turns,
        "failed_checks": [
            {"name": c.get("name"), "status": c.get("status"), "expected": c.get("expected"),
             "actual": c.get("actual"), "detail": c.get("detail")}
            for c in failed
        ],
        "semantic_counts": row.get("semantic_counts"),
    }


def review_records() -> dict[str, Any]:
    base = ROOT / "eval" / "reviews" / "reviews"
    records = []
    for path in sorted(base.glob("*.json")):
        d = load_json(path)
        records.append({
            "reviewId": d.get("reviewId"),
            "caseId": d.get("caseId"),
            "reviewScope": d.get("reviewScope"),
            "status": d.get("status"),
            "runId": d.get("runId"),
            "notes": d.get("notes") or "",
            "reviewer": d.get("reviewer"),
            "revision": d.get("revision"),
            "updatedAt": d.get("updatedAt"),
            "path": rel(path),
        })
    by_scope = {"case_definition": 0, "run_answer": 0}
    for r in records:
        by_scope[r["reviewScope"]] = by_scope.get(r["reviewScope"], 0) + 1
    by_status = Counter(r["status"] for r in records if r["reviewScope"] == "case_definition")
    return {
        "records": records,
        "by_scope": dict(by_scope),
        "case_definition_status": dict(by_status),
        "run_answer_records": [r for r in records if r["reviewScope"] == "run_answer"],
        "notes": [r for r in records if r.get("notes")],
    }


def gui_link(case_id: str, run_id: str) -> str:
    return f"http://127.0.0.1:8950/#case={case_id}&scope=run_answer&run={run_id}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="outputs/approved-development-failure-analysis.json")
    args = parser.parse_args()

    qa_run_dir = ROOT / "eval" / "results" / QA_RUN
    fault_run_dir = ROOT / "eval" / "results" / FAULT_RUN
    qa_rows = read_jsonl(qa_run_dir / "cases.jsonl")
    fault_rows = read_jsonl(fault_run_dir / "cases.jsonl")
    names = knowledge_names()
    baseline = load_json(ROOT / "outputs" / "approved-development-baseline.json")
    selection = load_json(ROOT / "outputs" / "approved-development-selection.json")
    freeze = load_json(ROOT / "outputs" / "approved-development-freeze.json")

    qa_evidence = [case_evidence(r, qa_run_dir, names) for r in qa_rows]
    fault_evidence = [case_evidence(r, fault_run_dir, names) for r in fault_rows]
    by_id = {r["case_id"]: r for r in qa_evidence}
    fby = {r["case_id"]: r for r in fault_evidence}

    failed_ids = [r["case_id"] for r in qa_rows if r.get("status") == "failed"]
    pending_ids = [r["case_id"] for r in qa_rows if r.get("status") == "pending_human"]
    attributions = []
    for case_id in failed_ids:
        a = dict(ATTRIBUTION[case_id])
        a["case_id"] = case_id
        a["evidence"] = by_id[case_id]
        attributions.append(a)
    fault_attributions = []
    for case_id in [r["case_id"] for r in fault_rows]:
        a = dict(ATTRIBUTION[case_id])
        a["case_id"] = case_id
        a["evidence"] = fby[case_id]
        fault_attributions.append(a)

    semantic = Counter()
    for r in qa_rows:
        for k, v in (r.get("semantic_counts") or {}).items():
            semantic[k] += v

    qa_review_rows = []
    for r in qa_rows:
        qa_review_rows.append({
            "case_id": r["case_id"],
            "status": r["status"],
            "category": r["category"],
            "path_comparability": PATH_LABEL.get(r["live_variant"], r["live_variant"]),
            "live_variant": r["live_variant"],
            "auto_failed_checks": sorted({c.get("name") for c in (r.get("checks") or []) if c.get("status") == "fail"}),
            "auto_unsupported_checks": sorted({c.get("name") for c in (r.get("checks") or []) if c.get("status") == "unsupported"}),
            "semantic_pending": (r.get("semantic_counts") or {}).get("pending_human", 0),
            "session_id": r["session_id"],
            "transcript_entry": f"eval/results/{QA_RUN}/instance-storage/sessions/{r['session_id']}/transcript.jsonl",
            "trace_entry": f"eval/results/{QA_RUN}/instance-storage/sessions/{r['session_id']}/outputs/traces/trace_20261113.jsonl",
            "trace_ids": r.get("trace_ids") or [],
            "gui_run_answer": gui_link(r["case_id"], QA_RUN),
            "user_answer_placeholder": True,
            "review_record_written": False,
        })

    reviews = review_records()
    counts = {
        "dataset_development": 32,
        "case_definition_reviewed": 32,
        "case_definition_passed": 26,
        "case_definition_failed": 2,
        "case_definition_uncertain": 4,
        "included": 26,
        "excluded": 6,
        "qa_included": 24,
        "fault_included": 2,
        "qa_auto_failed": 8,
        "qa_pending_human": 16,
        "semantic_pending_human": semantic.get("pending_human", 0),
        "semantic_not_applicable": semantic.get("not_applicable", 0),
        "semantic_exported_total": sum(semantic.values()),
    }
    assert counts["case_definition_passed"] + counts["case_definition_failed"] + counts["case_definition_uncertain"] == 32
    assert counts["qa_auto_failed"] == len(failed_ids)
    assert counts["semantic_pending_human"] == 294

    out = {
        "schema_version": "approved-development-failure-analysis/v1",
        "generated_at": "2026-11-14",
        "scope": {
            "qa_run_id": QA_RUN,
            "fault_run_id": FAULT_RUN,
            "state_run_id": baseline.get("state_baseline", {}).get("run_id"),
            "split": "development",
            "holdout_run": False,
            "human_answer_review": "not_performed_by_agent",
            "read_only_inputs": True,
        },
        "counts": counts,
        "count_correction": {
            "note": "Earlier plan lines wrote 27 passed / 2 failed / 4 uncertain. The authoritative current counts are 26 / 2 / 4.",
            "evidence": {
                "selection_counts": selection.get("counts"),
                "gui_meta_scope_counts": {
                    "case_definition": 32,
                    "run_answer": 0,
                    "byStatus": {"passed": 26, "failed": 2, "uncertain": 4},
                },
                "gui_health": "GET http://127.0.0.1:8950/api/health -> ok, reviewCount 32, readOnlyDataset true",
            },
        },
        "semantic_rubric": {
            "pending_human": semantic.get("pending_human", 0),
            "not_applicable": semantic.get("not_applicable", 0),
            "exported_total": sum(semantic.values()),
            "judgement_mode": "case-level whole-answer judgement is allowed; 294 individual clicks are not required",
        },
        "path_comparability_rule": {
            "frozen_turns": "directly comparable: only dataset user turns were replayed",
            "model_generated_prior_turn": "not directly comparable: the assistant fixture was retained in the dataset but not replayed; the live model generated a different prior turn",
            "counts": dict(Counter(r["live_variant"] for r in qa_rows)),
        },
        "qa_attributions": attributions,
        "fault_attributions": fault_attributions,
        "qa_answer_review": {
            "run_id": QA_RUN,
            "gui": "http://127.0.0.1:8950/",
            "rows": qa_review_rows,
            "pending_human_rows": pending_ids,
            "failed_rows": failed_ids,
            "existing_run_answer_reviews": len(reviews["run_answer_records"]),
            "note": "This file is review material only. It does not write any manual review, status, note, or score.",
        },
        "existing_reviews": reviews,
        "fault": {
            "run_id": FAULT_RUN,
            "restart_recovery": {
                "check": "fault.restart_session_recovered",
                "observed": "pass 2/2",
                "same_session_evidence": "server.log process restart followed by GET of the same session; T2 recall answers list T1 actions",
                "session_b": "not executed: one POST /api/sessions per fault case; T3 ran in the same session A",
            },
            "cases": fault_evidence,
        },
        "baseline_comparison": {
            "baseline_file": "outputs/approved-development-baseline.json",
            "baseline_status": baseline.get("status"),
            "baseline_counts": baseline.get("live_qa", {}).get("counts"),
            "agrees_on_8_qa_failures": set(baseline.get("failures", {}).get("live_qa_failed", [])) == set(failed_ids),
            "differences": [
                "This analysis does not accept all three baseline hypotheses: fixture variants are split into path artifacts plus independently provable defects; the restart T3 empty-state check is a missing session-B step, not a product isolation failure.",
                "The direct-base-power-01 deterministic direct-answer bypass is traced to server.py:590-624 and state.py:169-172, not to a model answer.",
            ],
        },
        "common_mechanisms": [
            "State lifecycle gaps: first-report activation, generic negation binding, correction supersession, and service-restart vs robot-restart confusion.",
            "Execution bypass: deterministic direct_answer/recall paths can return before the agent and retrieval, so a substantive turn can have no RAG evidence.",
            "Citation provenance: a no-this-turn-retrieval answer may reuse a recorded prior-turn source (closure-02, negation-01), but the scorer only maps this-turn fragments; this is a provenance/scoring boundary, not proof of an unsupported claim.",
            "Outcome taxonomy ambiguity: success/unknown/failed conflate 'step executed' with 'problem resolved'; several expected success values conflict with a still-failing symptom.",
            "Fixture path: assistant fixtures are not replayed, so action/state expectations can describe a conversation the live model never had.",
            "Fault path: same-session restart recovery is proven; the second-session isolation step is not executed, so its empty-state assertion is unverified.",
        ],
        "fix_layers": {
            "data": "Do not edit expected values or hidden errors. If the outcome contract is wrong, add a new revision with historical evidence, not a silent change.",
            "context": "Pass current case status, pending question, executed/not-tried actions, and manual-vs-auto success as structured context; mark fixture replay provenance.",
            "tools": "Permit reuse of a recorded prior-turn source with explicit provenance; require this-turn retrieval only when no provenance exists. Extend the scorer to map historical provenance instead of forcing every turn to retrieve.",
            "state": "Activate on first explicit report; bind generic negation to pending suggestions; supersede corrected actions; distinguish replace_dock/reseat_dock; do not extract robot reset from service-restart questions; preserve scope on recall.",
            "execution": "Do not return direct_answer before the agent except for protocol ack/duplicate/empty; classify recall and clarification paths explicitly so RAG checks are applicable or not.",
            "validation": "Citation check only on substantive claims; map citations to current or explicitly-proven prior fragments; treat truly unsourced claims separately; separate executed/resolved outcomes; mark path-dependent variants and missing fault steps.",
            "prompt": "Stable responsibilities only: cite sources actually used, do not claim unperformed actions, separate manual charging from automatic return, do not treat service restart as robot restart. No case-specific prompt patches.",
        },
        "not_done": [
            "No human run-answer review was written or implied; 294 pending semantic items remain.",
            "The executing analyst only read the GUI; the main model restored the exited 8950 service via exec_command (session 95057) after connection refusal, with no WMI and no visible window. No model, holdout, full test suite, latency run, runtime/dataset/review mutation, or artifact rebuild was performed by the analyst.",
            "The fixture replay/seedable-history decision and the outcome contract need a separate design decision.",
        ],
        "artifacts": {
            "script": "scripts/analyze_approved_failures.py",
            "report_md": "docs/approved-development-failure-analysis.md",
            "answer_review_md": "docs/answer-review-round-1.md",
        },
    }
    out_path = ROOT / args.out
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "out": rel(out_path), "counts": counts,
                      "failed": failed_ids, "fault": [r["case_id"] for r in fault_rows]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
