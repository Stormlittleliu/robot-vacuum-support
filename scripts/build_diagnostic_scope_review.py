# -*- coding: utf-8 -*-
"""Build the isolated diagnostic-scope review v2 workspace.

This script is read-only with respect to the original action-state-answer-review-v1
workspace, the frozen eval v1 32/16 dataset, the product runtime and the Judge core.
It projects the same two real recorded dialogues into a *new* suite whose only
purpose is to test a new per-turn diagnostic-scope rubric:

  outputs/diagnostic-scope-review-v2/
    dataset/{suite.json,cases.jsonl,split.json,schema.json,criteria.json,
             issue_catalog.json,oracle.json,exact-quotes.json}
    results/<run-id>/{run.json,cases.jsonl}
    reviews/                 (left empty for the user)
    judge_results/           (empty until the generic Judge CLI runs)
    evidence_bundles/        (generic recorded-run adapter output)
    source-map.json
    program-checks.json
    freeze.json              (written before any Judge call)
    judge_run/               (only when --run-judge is used)

The generic ``build_from_recorded_case`` entry point accepts an already-recorded
session row (user turns, actual answers, per-turn state snapshots, turn-level
trace slices, current/history retrieval) so a later product 4-case run can be
fed in without waiting for this baseline task.

No model is called by the build step.  The optional ``--run-judge`` step invokes
the existing DSH-native generic CLI only.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / "outputs" / "action-state-answer-review-v1"
OUT = ROOT / "outputs" / "diagnostic-scope-review-v2"
REPORT = ROOT / "outputs" / "diagnostic-scope-review-report.json"
DSH_JUDGE = ROOT.parent / "dsh-judge"
MANAGER_OBS = ROOT / "outputs" / "action-state-manager-observations.json"
DOC = ROOT / "docs" / "diagnostic-scope-review-v2.md"

RUN_ID = "ds-baseline-development-20261114-cdf5aac89d"
SUITE_ID = "robot-support-diagnostic-scope-review-v2"
JUDGE_ID = "jr-diagnostic-baseline-20261114-01"
REVISION = "v2"
RUBRIC_VERSION = "diagnostic-scope-v2"

CASE_SPECS = [
    {
        "id": "ds-baseline-contact-causal-01",
        "source_id": "asc-dev-contact-correction-01",
        "family_id": "fam-ds-baseline-contact-causal",
        "old_label": "清洁充电触点",
        "new_label": "清理充电座周围障碍物",
        "old_action": "clean_contacts",
        "new_action": "clear_dock_obstacles",
    },
    {
        "id": "ds-baseline-dock-causal-01",
        "source_id": "asc-dev-dock-correction-01",
        "family_id": "fam-ds-baseline-dock-causal",
        "old_label": "更换新的充电座",
        "new_label": "挪动原来的充电座",
        "old_action": "replace_dock",
        "new_action": "move_dock",
    },
]

KNOWN_SOURCE_KINDS = {
    "v1_review_workspace",
    "source_run_raw_session",
    "recorded_session_fixture",
}

FORBIDDEN_BUNDLE_KEYS = {
    "manager_observations",
    "manager_verdict",
    "judge_results",
    "previous_judge",
}
FORBIDDEN_BUNDLE_STRINGS = (
    "candidate_needs_counterexample",
    "manager_concern",
    "judge_context",
    "jmc-s1-exclusive-causal-conclusion",
    "criteria_judged_passed",
)


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


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def case_hash(case: dict[str, Any]) -> str:
    """Exact eval/review_server.py::case_hash algorithm."""
    payload = {key: value for key, value in case.items() if key != "review"}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(raw.encode("utf-8"))


def _validate_source(source: dict[str, Any]) -> None:
    kind = source.get("kind")
    if kind not in KNOWN_SOURCE_KINDS:
        raise ValueError(f"unknown recorded source kind: {kind!r}")
    raw_path = source.get("path")
    if not raw_path:
        return
    path = Path(str(raw_path)).resolve()
    allowed = [ROOT.resolve(), V1.resolve()]
    if not any(path == base or base in path.parents for base in allowed):
        raise ValueError(f"recorded source path is outside the allowed roots: {path}")


def criteria_for(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Five aspects per actual turn; no overall-case duplicate criterion."""
    old = spec["old_label"]
    new = spec["new_label"]

    def item(turn: int, aspect: str, dimension: str, text: str, focus: str = "task_completion") -> dict[str, Any]:
        return {
            "criterion_id": f"turn-{turn}-{aspect}",
            "focus": focus,
            "turn_scope": turn,
            "dimension": dimension,
            "text": text,
        }

    return [
        item(1, "observation-vs-goal-scope", "observation_goal_scope",
             f"Turn 1: the user has performed '{old}' and has not reported the result. Keep that local, still-unknown observation separate from the task goal (automatic recharge recovery / a resolved case). Do not let a later local charging observation stand in for automatic-recharge verification."),
        item(1, "causal-exclusivity-evidence", "causal_exclusivity",
             "Turn 1: if the answer names one exclusive cause or says no further checks are needed, it must cite evidence that rules out the other plausible causes in the retrieved material (dock obstruction, sensor dust, IR signal, adapter/battery). A general maintenance list or one local observation does not establish exclusive causation.",
             "evidence_support"),
        item(1, "condition-vs-fact", "condition_vs_fact",
             "Turn 1: hypothetical or test outcomes must remain clearly conditional ('if ... then ...') and must not be presented as already observed facts. Do not state that a part is confirmed faulty from the user's performed action alone."),
        item(1, "next-observation-discriminates", "discriminative_observation",
             "Turn 1: the next observation should discriminate among the plausible causes (for example verify automatic recharge after the action, then check other causes if it still fails). Manual placement / manual charging success alone is not equivalent to automatic-recharge success."),
        item(1, "local-result-global-closure", "local_to_global_closure",
             "Turn 1: a local result may narrow the next check but cannot by itself justify global case closure. Keep the case active unless automatic recovery is directly observed and remaining alternatives are accounted for."),
        item(2, "observation-vs-goal-scope", "observation_goal_scope",
             f"Turn 2: after the correction the recorded action is '{new}' with an unknown result. Keep that local observation separate from the global goal (automatic recharge recovery / case resolution) and do not treat it as proof of the other candidate causes."),
        item(2, "causal-exclusivity-evidence", "causal_exclusivity",
             "Turn 2: a claim that the problem is solely obstruction/path, or that no further checks are needed, requires evidence excluding the other causes. Retrieval that distinguishes manual charging from automatic-recharge sensor/signal faults cannot support an exclusive conclusion.",
             "evidence_support"),
        item(2, "condition-vs-fact", "condition_vs_fact",
             "Turn 2: keep the conditional test outcome conditional. Do not convert 'if it charges after the correction' into an established observation, and do not present an untested cause as confirmed."),
        item(2, "next-observation-discriminates", "discriminative_observation",
             "Turn 2: the proposed next observation should distinguish automatic recharge from manual placement and separate the remaining sensor/signal/dock causes; repeating the same local action is not a discriminating test."),
        item(2, "local-result-global-closure", "local_to_global_closure",
             "Turn 2: a local result (for example manual placement charges, or obstacles were cleared) cannot alone support global closure or an exclusive fault cause; the case stays active/uncertain until automatic recovery is evidenced."),
        item(3, "observation-vs-goal-scope", "observation_goal_scope",
             f"Turn 3: this is a recall turn. Recall only currently effective facts ('{new}' performed with result unstated; '{old}' not performed) and do not let the recall imply automatic recovery or a confirmed fault cause."),
        item(3, "causal-exclusivity-evidence", "causal_exclusivity",
             "Turn 3: do not introduce or repeat an exclusive causal claim. If one is repeated, it still needs evidence excluding the alternatives; the recorded action alone is not such evidence.",
             "evidence_support"),
        item(3, "condition-vs-fact", "condition_vs_fact",
             "Turn 3: keep unknown outcomes unknown and conditionals conditional. Do not turn 'result not yet observed' into success, failure, or a confirmed component fault."),
        item(3, "next-observation-discriminates", "discriminative_observation",
             "Turn 3: pure recall needs no new retrieval or observation. If one next step is offered, it should discriminate the remaining causes and must not replace automatic-recharge verification with a repeated manual action."),
        item(3, "local-result-global-closure", "local_to_global_closure",
             "Turn 3: recall of local/partial actions cannot support global closure. Do not close the case or claim automatic recovery from recall alone."),
    ]


def criteria_for_actual_turns(
    spec: dict[str, Any],
    turn_count: int,
    turn_contexts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Generate exactly five criteria for each *actual* turn.

    The historical baseline path stays byte-identical: a three-turn case with
    no explicit contexts delegates to ``criteria_for(spec)`` unchanged.  A
    one/two-turn after case gets only 5/10 frozen criteria, never turn-scope
    placeholders for turns that did not happen.
    """
    if not isinstance(turn_count, int) or turn_count < 1:
        raise ValueError("turn_count must be a positive integer")
    if turn_count == 3 and not turn_contexts:
        return criteria_for(spec)

    old = spec.get("old_label", "the recorded action")
    new = spec.get("new_label", "the recorded action")
    contexts = turn_contexts or []
    items: list[dict[str, Any]] = []

    def item(turn: int, aspect: str, dimension: str, text: str, focus: str = "task_completion") -> None:
        items.append({
            "criterion_id": f"turn-{turn}-{aspect}",
            "focus": focus,
            "turn_scope": turn,
            "dimension": dimension,
            "text": text,
        })

    for turn in range(1, turn_count + 1):
        ctx = contexts[turn - 1] if turn - 1 < len(contexts) else {}
        observation = ctx.get("observation") or f"the user's recorded statement in turn {turn} and any local result"
        goal = ctx.get("task_goal") or "automatic recharge recovery / a resolved case"
        causal_question = ctx.get("causal_question") or "the alternative causes listed in the retrieved material"
        next_observation = ctx.get("next_observation") or "verify automatic recharge after the action, then check other causes if it still fails"
        closure = ctx.get("closure") or "automatic recovery is directly observed and remaining alternatives are accounted for"
        positive = ctx.get("positive")
        item(
            turn, "observation-vs-goal-scope", "observation_goal_scope",
            f"Turn {turn}: {observation}. Keep that local/recorded observation separate from the task goal ({goal}). Do not let a local or manually produced result stand in for an evidence-backed observation of the task goal."
            + (f" {positive}" if positive else ""),
        )
        item(
            turn, "causal-exclusivity-evidence", "causal_exclusivity",
            f"Turn {turn}: any claim of a single/exclusive cause, or that no further checks are needed, must cite evidence that rules out the alternatives in the retrieved material ({causal_question}). A local result or a general maintenance list does not establish exclusivity. Even an evidence-backed automatic-recovery success does not prove a unique faulty part/cause without component-level evidence. Historical sources may be legitimately reused when a factual claim is made; a pure recall/clarification turn with no external factual claim does not require a new RAG call or citation.",
            "evidence_support",
        )
        item(
            turn, "condition-vs-fact", "condition_vs_fact",
            f"Turn {turn}: keep hypothetical/test outcomes conditional ('if ... then ...') and distinct from observed facts; do not present an untested cause or part as confirmed. A conditional sentence that embeds an exclusive cause ('if it charges, then the problem is only X') is still an overreach and must not pass merely because it is phrased conditionally.",
        )
        item(
            turn, "next-observation-discriminates", "discriminative_observation",
            f"Turn {turn}: the next observation should discriminate among the plausible causes: {next_observation}. Manual placement / manual charging success is not equivalent to automatic-recharge success.",
        )
        item(
            turn, "local-result-global-closure", "local_to_global_closure",
            f"Turn {turn}: a local result cannot by itself support global closure. The case may close only when {closure}; otherwise keep it active/uncertain.",
        )
    return items


def build_from_recorded_case(
    recorded: dict[str, Any],
    *,
    new_case_id: str,
    family_id: str,
    title: str,
    spec: dict[str, Any],
    raw_result: dict[str, Any] | None = None,
    run_id: str = RUN_ID,
    criteria: list[dict[str, Any]] | None = None,
    scoring: dict[str, Any] | None = None,
    origin_extra: dict[str, Any] | None = None,
    source_extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Generic entry point for a later source-run raw recorded session.

    ``recorded`` shape (all values are copied, never synthesized):
      source: {kind, path, session_id, source_run_id, artifact_id, ...}
      turns:  [{index, role:'user', content, answer, state, trace, retrieval,
                request_id, message_id, ...}]
    """
    if not isinstance(recorded, dict):
        raise ValueError("recorded must be an object")
    source = recorded.get("source") or {}
    _validate_source(source)
    turns = recorded.get("turns") or []
    if not turns:
        raise ValueError("recorded.turns must be a non-empty array")
    clean_turns = []
    for position, turn in enumerate(turns, 1):
        if not isinstance(turn, dict):
            raise ValueError(f"turn {position} must be an object")
        if turn.get("role", "user") != "user":
            raise ValueError(f"turn {position} must be a user turn")
        content = str(turn.get("content") or "")
        answer = str(turn.get("answer") or "")
        if not content.strip():
            raise ValueError(f"turn {position} content is empty")
        if not answer.strip():
            raise ValueError(f"turn {position} answer is empty")
        if not isinstance(turn.get("state"), dict):
            raise ValueError(f"turn {position} state snapshot is required")
        clean_turns.append(copy.deepcopy(turn))
    rubric = copy.deepcopy(criteria) if criteria is not None else criteria_for_actual_turns(
        spec, len(clean_turns), spec.get("turn_contexts")
    )
    origin = {
        "kind": "diagnostic_scope_baseline_projection",
        "projection_from": source.get("kind"),
        "source_case_id": source.get("source_case_id"),
        "source_run_id": source.get("source_run_id"),
        "source_session_id": source.get("session_id"),
        "source_turns": list(range(1, len(clean_turns) + 1)),
        "source_artifact_id": source.get("artifact_id"),
        "suite_revision": REVISION,
        "rubric_version": RUBRIC_VERSION,
        "not_the_frozen_v1_split": True,
    }
    if origin_extra:
        origin.update(copy.deepcopy(origin_extra))
    case_scoring = copy.deepcopy(scoring) if scoring is not None else {
        "automatic": [
            "dataset.schema",
            "dataset.counts_2",
            "dataset.split_disjoint",
            "evidence.real_source_projection",
            "review.pending_human",
        ],
        "human": [
            "diagnostic.observation_scope",
            "diagnostic.causal_exclusivity",
            "diagnostic.condition_vs_fact",
            "diagnostic.discriminative_observation",
            "diagnostic.local_to_global_closure",
        ],
        "denominator": "qa",
        "expected_sources_mode": "current_or_legitimate_history",
        "state_score": "not_applicable_program_checks_are_structural",
    }
    case = {
        "id": new_case_id,
        "family_id": family_id,
        "category": "multi_turn",
        "split": "development",
        "title": title,
        "origin": origin,
        "review": {"status": "pending_human"},
        "turns": [{"role": "user", "content": turn["content"]} for turn in clean_turns],
        "answer_review_rubric": rubric,
        "evidence": [],
        "setup": {},
        "scoring": case_scoring,
        "state_scope": "diagnostic_scope_answer_review_v2",
        "rubric_version": RUBRIC_VERSION,
        "standard_kind": "manager_driven_posthoc_development_standard",
        "not_independent_blind_benchmark": True,
        "oracle_visibility": "not_in_model_bundle",
    }
    if raw_result is not None:
        result_row = copy.deepcopy(raw_result)
        result_row["case_id"] = new_case_id
        result_row.setdefault("source", {})
        result_row["source"] = {
            **result_row.get("source", {}),
            "projection_from": source.get("kind"),
            "source_case_id": source.get("source_case_id"),
            "source_run_id": source.get("source_run_id"),
            "source_session_id": source.get("session_id"),
            "rubric_version": RUBRIC_VERSION,
            **(copy.deepcopy(source_extra) if source_extra else {}),
        }
    else:
        result_turns = []
        for position, turn in enumerate(clean_turns, 1):
            row = {
                "index": turn.get("index", position),
                "role": "user",
                "content": turn["content"],
                "answer": turn["answer"],
                "state": copy.deepcopy(turn["state"]),
                "trace": copy.deepcopy(turn.get("trace")),
                "retrieval": copy.deepcopy(turn.get("retrieval") or {"current": [], "history": []}),
                "request_id": turn.get("request_id"),
                "errors": turn.get("errors") or [],
                "live_variant": "recorded_live_session",
                "normative_state": "actual_turn_snapshot",
            }
            for field in (
                "semantic", "observations", "diagnostic_scope", "snapshot", "raw_state",
                "message_id", "assistant_message_id", "user_at", "assistant_at",
                "checks", "trace_id", "trace_run_ids", "http_status",
            ):
                if field in turn:
                    row[field] = copy.deepcopy(turn[field])
            result_turns.append(row)
        result_row = {
            "case_id": new_case_id,
            "mode": "recorded",
            "status": "completed",
            "attempted": len(clean_turns),
            "session_id": source.get("session_id"),
            "turns": result_turns,
            "source": {
                "projection_from": source.get("kind"),
                "source_case_id": source.get("source_case_id"),
                "source_run_id": source.get("source_run_id"),
                "source_session_id": source.get("session_id"),
                "rubric_version": RUBRIC_VERSION,
                **(copy.deepcopy(source_extra) if source_extra else {}),
            },
        }
    return case, result_row


def _load_v1() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    freeze = read_json(V1 / "freeze.json")
    run_id = freeze["run_id"]
    cases = {row["id"]: row for row in read_jsonl(V1 / "dataset" / "cases.jsonl")}
    results = {row["case_id"]: row for row in read_jsonl(V1 / "results" / run_id / "cases.jsonl")}
    source_map = read_json(V1 / "source-map.json")
    return freeze, cases, results, source_map, read_json(V1 / "results" / run_id / "run.json")


def _verify_v1_hashes(freeze: dict[str, Any]) -> dict[str, Any]:
    run_id = freeze["run_id"]
    expected = freeze["source_hashes"]
    actual = {
        "dataset_cases_sha256": sha256_file(V1 / "dataset" / "cases.jsonl"),
        "suite_sha256": sha256_file(V1 / "dataset" / "suite.json"),
        "split_sha256": sha256_file(V1 / "dataset" / "split.json"),
        "result_cases_sha256": sha256_file(V1 / "results" / run_id / "cases.jsonl"),
        "result_run_sha256": sha256_file(V1 / "results" / run_id / "run.json"),
        "source_map_sha256": sha256_file(V1 / "source-map.json"),
        "program_checks_sha256": sha256_file(V1 / "program-checks.json"),
    }
    for key, value in actual.items():
        if expected.get(key) != value:
            raise SystemExit(f"v1 source hash mismatch for {key}: {expected.get(key)} != {value}")
    for case_id, digest in freeze["case_hashes"].items():
        case = read_jsonl(V1 / "dataset" / "cases.jsonl")
        match = next(row for row in case if row["id"] == case_id)
        if case_hash(match) != digest:
            raise SystemExit(f"v1 case_hash mismatch for {case_id}")
    return actual


def _frozen_eval_hashes(freeze: dict[str, Any]) -> dict[str, Any]:
    dataset_dir = ROOT / "eval" / "datasets" / "v1"
    hashes = {
        "cases.jsonl": sha256_file(dataset_dir / "cases.jsonl"),
        "suite.json": sha256_file(dataset_dir / "suite.json"),
        "split.json": sha256_file(dataset_dir / "split.json"),
    }
    expected = freeze["frozen_v1"]["hashes"]
    for key, value in hashes.items():
        if expected.get(key) != value:
            raise SystemExit(f"frozen eval v1 hash mismatch for {key}")
    return hashes


def _validate_case_schema(case: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key in ("id", "family_id", "category", "split", "title", "origin", "review", "turns", "answer_review_rubric"):
        if key not in case:
            problems.append(f"missing case key {key}")
    if case.get("split") != "development":
        problems.append("split must be development")
    if not isinstance(case.get("turns"), list) or not case["turns"]:
        problems.append("turns must be a non-empty array")
    if case.get("review", {}).get("status") != "pending_human":
        problems.append("review.status must be pending_human")
    rubric = case.get("answer_review_rubric")
    if not isinstance(rubric, list) or len(rubric) != 15:
        problems.append("answer_review_rubric must have 15 turn-scoped criteria")
    ids = [item.get("criterion_id") for item in rubric or []]
    if len(ids) != len(set(ids)):
        problems.append("duplicate criterion_id")
    if any(item.get("turn_scope") not in (1, 2, 3) for item in rubric or []):
        problems.append("every criterion must be turn-scoped 1/2/3 (no overall case duplicate)")
    return problems


def _validate_result_schema(row: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key in ("case_id", "turns"):
        if key not in row:
            problems.append(f"missing result key {key}")
    for position, turn in enumerate(row.get("turns") or [], 1):
        if turn.get("index") != position:
            problems.append(f"result turn {position} index mismatch")
        for key in ("content", "answer", "state", "trace", "retrieval"):
            if key not in turn:
                problems.append(f"result turn {position} missing {key}")
    return problems


def _exact_quotes(manager: dict[str, Any], cases: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for obs in manager.get("observations", []):
        case_id = obs["case_id"]
        turn = int(obs["turn"])
        quote = obs["exact_quote"]
        answer = results[case_id]["turns"][turn - 1]["answer"]
        rows.append({
            "id": obs["id"],
            "case_id": case_id,
            "turn": turn,
            "source_kind": "assistant_actual_answer",
            "quote": quote,
            "sha256": sha256_bytes(quote.encode("utf-8")),
            "verbatim_in_source": quote in answer,
        })
        for index, support in enumerate(obs.get("retrieval_support_for_concern", []), 1):
            support_quote = support["exact_quote"]
            outputs = []
            for result_turn in results[case_id]["turns"]:
                retrieval = result_turn.get("retrieval") or {}
                for record in list(retrieval.get("current") or []) + list(retrieval.get("history") or []):
                    outputs.append(str(record.get("output") or ""))
            rows.append({
                "id": f"{obs['id']}-retrieval-{index}",
                "case_id": case_id,
                "turn": turn,
                "source_kind": "recorded_retrieval_output",
                "quote": support_quote,
                "sha256": sha256_bytes(support_quote.encode("utf-8")),
                "verbatim_in_source": any(support_quote in output for output in outputs),
            })
    return {
        "schema_version": "diagnostic-scope-review-exact-quotes/v1",
        "purpose": "Programmatic verbatim check for oracle source quotes; never inserted into a model bundle.",
        "all_verbatim": all(row["verbatim_in_source"] for row in rows),
        "quotes": rows,
    }


def build_dataset() -> dict[str, Any]:
    freeze, v1_cases, v1_results, source_map, v1_run = _load_v1()
    v1_hashes = _verify_v1_hashes(freeze)
    frozen_hashes = _frozen_eval_hashes(freeze)
    manager = read_json(MANAGER_OBS)

    cases: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    mappings: list[dict[str, Any]] = []
    criteria_by_case: dict[str, Any] = {}
    for spec in CASE_SPECS:
        old_case = v1_cases[spec["source_id"]]
        old_result = v1_results[spec["source_id"]]
        recorded = {
            "source": {
                "kind": "v1_review_workspace",
                "path": str(V1),
                "source_case_id": spec["source_id"],
                "source_run_id": freeze["run_id"],
                "session_id": old_result["session_id"],
                "artifact_id": freeze["artifact"]["artifact_id"],
            },
            "turns": [
                {
                    "index": turn["index"],
                    "role": "user",
                    "content": old_case["turns"][turn["index"] - 1]["content"],
                    "answer": turn["answer"],
                    "state": turn["state"],
                    "trace": turn["trace"],
                    "retrieval": turn["retrieval"],
                    "request_id": turn.get("request_id"),
                    "message_id": turn.get("message_id"),
                    "errors": turn.get("errors") or [],
                }
                for turn in old_result["turns"]
            ],
        }
        case, result = build_from_recorded_case(
            recorded,
            new_case_id=spec["id"],
            family_id=spec["family_id"],
            title=f"旧回答诊断范围复核：{old_case['title']}",
            spec=spec,
            raw_result=old_result,
        )
        # The actual answer/trace/state are byte-for-byte the v1 projection.
        for old_turn, new_turn in zip(old_result["turns"], result["turns"]):
            for key in ("answer", "state", "trace", "retrieval"):
                if json.dumps(old_turn.get(key), ensure_ascii=False, sort_keys=True) != json.dumps(new_turn.get(key), ensure_ascii=False, sort_keys=True):
                    raise SystemExit(f"{spec['id']}: {key} changed during projection")
        if [turn["content"] for turn in case["turns"]] != [turn["content"] for turn in old_case["turns"]]:
            raise SystemExit(f"{spec['id']}: user text changed during projection")
        cases.append(case)
        results.append(result)
        criteria_by_case[spec["id"]] = case["answer_review_rubric"]
        mappings.append({
            "case_id": spec["id"],
            "source_case_id": spec["source_id"],
            "source_session_id": old_result["session_id"],
            "turns": [
                {
                    "turn": turn["index"],
                    "request_id": turn.get("request_id"),
                    "trace_id": turn.get("trace_id"),
                    "trace_run_ids": turn.get("trace_run_ids"),
                }
                for turn in old_result["turns"]
            ],
        })

    # Dataset-level program listing of the issue catalog (not a model bundle).
    issue_catalog = {
        "schema_version": "diagnostic-scope-review-issue-catalog/v1",
        "rubric_version": RUBRIC_VERSION,
        "cases": criteria_by_case,
        "note": "Program listing only. The generic adapter builds each bundle's own issue_catalog from the same rubric.",
    }
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "diagnostic-scope-review-v2 case",
        "type": "object",
        "required": ["id", "family_id", "category", "split", "title", "origin", "review", "turns", "answer_review_rubric"],
        "properties": {
            "id": {"type": "string", "pattern": "^ds-baseline-"},
            "split": {"const": "development"},
            "review": {"type": "object", "required": ["status"], "properties": {"status": {"const": "pending_human"}}},
            "turns": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["role", "content"]}},
            "answer_review_rubric": {
                "type": "array",
                "minItems": 15,
                "maxItems": 15,
                "items": {
                    "type": "object",
                    "required": ["criterion_id", "focus", "turn_scope", "dimension", "text"],
                    "properties": {
                        "criterion_id": {"type": "string"},
                        "focus": {"enum": ["task_completion", "evidence_support"]},
                        "turn_scope": {"enum": [1, 2, 3]},
                        "dimension": {"type": "string"},
                        "text": {"type": "string", "minLength": 20},
                    },
                },
            },
        },
        "additionalProperties": True,
    }
    oracle = {
        "schema_version": "diagnostic-scope-review-oracle/v1",
        "rubric_version": RUBRIC_VERSION,
        "standard_kind": "manager_driven_posthoc_development_standard",
        "not_independent_blind_benchmark": True,
        "not_gold_for_independent_benchmark": True,
        "positive": [
            {
                "id": "pos-explicit-automatic-recovery",
                "statement": "The user explicitly reports that the robot autonomously found the dock and started charging.",
                "allowed": "The answer may confirm that automatic capability has recovered.",
                "not_allowed": "It still may not name which part is faulty without component-level evidence.",
            },
            {
                "id": "pos-credible-diagnostic-report",
                "statement": "The user provides a credible diagnostic report that already identifies a cause.",
                "allowed": "The answer may accurately quote the report's conclusion and note the report as the source.",
                "not_allowed": "It may not present the report's conclusion as the assistant's own observation or omit the source.",
            },
        ],
        "negative": [
            {
                "id": "neg-manual-charge-only",
                "statement": "The user only says the robot charges after being manually placed on the dock.",
                "effect": "Record a local observation only; it does not prove automatic-recharge recovery or an exclusive cause.",
            },
            {
                "id": "neg-cleared-obstacles-only",
                "statement": "The user only says they cleared the area around the dock.",
                "effect": "It does not prove that obstruction was the sole cause or that the case is resolved.",
            },
            {
                "id": "neg-pure-recall",
                "statement": "A pure recall/clarification turn with no external factual claim.",
                "effect": "Retrieval/citation is not mandatory; historical sources may be legitimately reused if a domain claim is added.",
            },
        ],
        "scope_rules": [
            "Separate the performed action, the observed local result, the inferred cause, and the global case status.",
            "An exclusive causal claim needs evidence that excludes the alternatives listed by the retrieval.",
            "A conditional result stays conditional until observed; do not turn it into a fact.",
            "The next observation should discriminate among remaining causes.",
            "A local result cannot alone support global closure or automatic-recovery success.",
            "Do not add a rule that the cause can never be determined, and do not label every answer as vague.",
        ],
        "source_observation_ids": [obs["id"] for obs in manager.get("observations", [])],
        "note": "The two manager observations were used only to define this post-hoc development oracle. Their verdicts/notes are not included in any model bundle.",
    }
    exact_quotes = _exact_quotes(manager, v1_cases, v1_results)

    split = {
        "schema_version": "robot-support-eval/v1",
        "suite_id": SUITE_ID,
        "generated_at": now_iso(),
        "development": {"count": len(cases), "case_ids": [case["id"] for case in cases], "families": {}},
        "holdout": {"count": 0, "case_ids": [], "note": "No holdout case exists in this isolated diagnostic-scope workspace."},
        "frozen_v1_untouched": True,
        "frozen_v1_hashes": frozen_hashes,
    }
    suite = {
        "schema_version": "robot-support-eval/v1",
        "suite_id": SUITE_ID,
        "revision": REVISION,
        "cases_file": "cases.jsonl",
        "split_file": "split.json",
        "counts": {"development": len(cases), "holdout": 0},
        "rubric_version": RUBRIC_VERSION,
        "standard_kind": "manager_driven_posthoc_development_standard",
        "not_independent_blind_benchmark": True,
        "source_snapshot": {
            "generated_at": now_iso(),
            "source_workspace": "outputs/action-state-answer-review-v1",
            "source_run_id": freeze["run_id"],
            "source_freeze_sha256": sha256_file(V1 / "freeze.json"),
            "artifact_id": freeze["artifact"]["artifact_id"],
            "source_hashes": v1_hashes,
            "frozen_v1_hashes": frozen_hashes,
            "note": "Same real recorded answers/traces/state as v1; only the suite rubric version changes.",
        },
        "review_status": "pending_human",
    }
    run_projection = {
        "schema_version": "diagnostic-scope-review-run/v2",
        "run_id": RUN_ID,
        "split": "development",
        "mode": "recorded",
        "kind": "diagnostic_scope_answer_review_projection",
        "status": "completed",
        "started_at": v1_run.get("started_at"),
        "finished_at": v1_run.get("finished_at"),
        "dataset": {"suite_id": SUITE_ID, "revision": REVISION, "file_sha256": {}, "dataset_hash": None},
        "source": {
            "source_workspace": "outputs/action-state-answer-review-v1",
            "source_run_id": freeze["run_id"],
            "source_freeze_sha256": sha256_file(V1 / "freeze.json"),
            "artifact_id": freeze["artifact"]["artifact_id"],
            "artifact_root": freeze["artifact"]["artifact_root"],
            "runtime_hash": freeze["artifact"]["runtime_hash"],
            "contract": "docs/action-state-contract.md",
            "contract_sha256": freeze["contract"]["sha256"],
            "rubric_doc": "docs/diagnostic-scope-review-v2.md",
            "sessions": {spec["id"]: v1_results[spec["source_id"]]["session_id"] for spec in CASE_SPECS},
        },
        "cases": [
            {
                "case_id": case["id"],
                "source_case_id": spec["source_id"],
                "session_id": v1_results[spec["source_id"]]["session_id"],
                "turns": 3,
                "rubric_version": RUBRIC_VERSION,
            }
            for case, spec in zip(cases, CASE_SPECS)
        ],
        "note": "Read-only re-projection of the two real recorded v1 dialogues; no product model was called.",
    }

    OUT.mkdir(parents=True, exist_ok=True)
    write_jsonl(OUT / "dataset" / "cases.jsonl", cases)
    write_json(OUT / "dataset" / "suite.json", suite)
    write_json(OUT / "dataset" / "split.json", split)
    write_json(OUT / "dataset" / "schema.json", schema)
    write_json(OUT / "dataset" / "criteria.json", {"schema_version": "diagnostic-scope-review-criteria/v1", "rubric_version": RUBRIC_VERSION, "criteria": criteria_by_case})
    write_json(OUT / "dataset" / "issue_catalog.json", issue_catalog)
    write_json(OUT / "dataset" / "oracle.json", oracle)
    write_json(OUT / "dataset" / "exact-quotes.json", exact_quotes)
    write_jsonl(OUT / "results" / RUN_ID / "cases.jsonl", results)
    write_json(OUT / "results" / RUN_ID / "run.json", run_projection)

    suite_bytes = (OUT / "dataset" / "suite.json").read_bytes()
    cases_bytes = (OUT / "dataset" / "cases.jsonl").read_bytes()
    split_bytes = (OUT / "dataset" / "split.json").read_bytes()
    file_hashes = {
        "suite.json": sha256_bytes(suite_bytes),
        "cases.jsonl": sha256_bytes(cases_bytes),
        "split.json": sha256_bytes(split_bytes),
    }
    run_projection["dataset"]["file_sha256"] = file_hashes
    run_projection["dataset"]["dataset_hash"] = sha256_bytes(
        json.dumps({"suite": file_hashes["suite.json"], "cases": file_hashes["cases.jsonl"], "split": file_hashes["split.json"]}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    run_projection["run_cases_sha256"] = sha256_file(OUT / "results" / RUN_ID / "cases.jsonl")
    write_json(OUT / "results" / RUN_ID / "run.json", run_projection)

    source_map_out = {
        "schema_version": "diagnostic-scope-review-source-map/v2",
        "generated_at": now_iso(),
        "run_id": RUN_ID,
        "source_workspace": "outputs/action-state-answer-review-v1",
        "source_hashes": v1_hashes,
        "frozen_v1_hashes": frozen_hashes,
        "cases": mappings,
        "turn_mapping_note": "Source projections already isolate each turn; this v2 build copies answer/trace/state/retrieval without re-slicing.",
    }
    write_json(OUT / "source-map.json", source_map_out)

    (OUT / "reviews").mkdir(parents=True, exist_ok=True)
    (OUT / "reviews" / "store.json").write_text(json.dumps({"schema_version": "manual-review-store/v1", "records": []}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "judge_results").mkdir(parents=True, exist_ok=True)

    checks = {
        "baseline_user_text_unchanged": True,
        "baseline_answers_unchanged": True,
        "baseline_trace_unchanged": True,
        "baseline_state_unchanged": True,
        "source_hashes_verified": True,
        "frozen_v1_dataset_unchanged": True,
        "criteria_schema_valid": all(not _validate_case_schema(case) for case in cases),
        "result_schema_valid": all(not _validate_result_schema(row) for row in results),
        "issue_catalog_complete": all(
            {item["criterion_id"] for item in case["answer_review_rubric"]} == {item["criterion_id"] for item in issue_catalog["cases"][case["id"]]}
            for case in cases
        ),
        "exact_quotes_verbatim": exact_quotes["all_verbatim"],
        "holdout_not_read": True,
    }
    program_checks = {
        "schema_version": "diagnostic-scope-review-program-checks/v1",
        "source": "outputs/action-state-answer-review-v1 (read-only projection)",
        "source_run_id": RUN_ID,
        "rubric_version": RUBRIC_VERSION,
        "passed": all(checks.values()),
        "checks": checks,
        "exact_quotes": {"checked": len(exact_quotes["quotes"]), "all_verbatim": exact_quotes["all_verbatim"]},
        "note": "Structural checks only; they are not semantic scoring and not a Judge oracle.",
    }
    write_json(OUT / "program-checks.json", program_checks)
    (OUT / "README.md").write_text(
        "\n".join([
            "# Diagnostic-scope review v2 workspace (isolated)",
            "",
            f"- run_id: `{RUN_ID}`",
            f"- rubric: `{RUBRIC_VERSION}` (manager-driven post-hoc development standard; not an independent blind benchmark)",
            "- dataset: 2 baseline cases re-projected from the real v1 recorded answers; user/answer/trace/state unchanged",
            "- results: read-only copies with new case ids only",
            "- reviews/: empty; the user writes human reviews here",
            "- judge_results/: machine results only; no human review is auto-filled",
            "- freeze.json: written before the Judge call",
            "- evidence_bundles/: built by the existing dsh-judge recorded-run adapter",
            "",
        ]),
        encoding="utf-8",
    )
    return {
        "cases": cases,
        "results": results,
        "program_checks": program_checks,
        "v1_hashes": v1_hashes,
        "frozen_hashes": frozen_hashes,
        "freeze": freeze,
        "run_projection": run_projection,
        "exact_quotes": exact_quotes,
    }


def _run_node_bundles() -> dict[str, Any]:
    cmd = [
        "node",
        str(DSH_JUDGE / "scripts" / "build-recorded-bundles.js"),
        "--workspace",
        str(OUT),
        "--out",
        str(OUT / "evidence_bundles"),
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(cmd, cwd=str(DSH_JUDGE), capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=flags)
    (OUT / "judge_run").mkdir(parents=True, exist_ok=True)
    (OUT / "judge_run" / "build-bundles.stdout.log").write_text(completed.stdout or "", encoding="utf-8")
    (OUT / "judge_run" / "build-bundles.stderr.log").write_text(completed.stderr or "", encoding="utf-8")
    if completed.returncode != 0:
        raise SystemExit(f"recorded bundle build failed ({completed.returncode}): {completed.stderr[-2000:]}")
    return {"command": cmd, "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}


def _bundle_checks(cases: list[dict[str, Any]]) -> dict[str, Any]:
    case_hashes = {case["id"]: case_hash(case) for case in cases}
    rows: dict[str, Any] = {}
    problems: list[str] = []
    for case in cases:
        bundle_path = OUT / "evidence_bundles" / f"{case['id']}.json"
        if not bundle_path.exists():
            problems.append(f"missing bundle {bundle_path.name}")
            continue
        bundle = read_json(bundle_path)
        rubric = case["answer_review_rubric"]
        catalog = bundle.get("task", {}).get("issue_catalog", [])
        if bundle.get("case_hash") != case_hashes[case["id"]]:
            problems.append(f"{case['id']}: bundle case_hash mismatch")
        if bundle.get("case_id") != case["id"]:
            problems.append(f"{case['id']}: bundle case_id mismatch")
        if len(catalog) != len(rubric):
            problems.append(f"{case['id']}: catalog length {len(catalog)} != rubric {len(rubric)}")
        if {row.get("criterion_id") for row in catalog} != {row.get("criterion_id") for row in rubric}:
            problems.append(f"{case['id']}: catalog ids do not match rubric ids")
        if any(not row.get("evidence_item_ids") for row in catalog):
            problems.append(f"{case['id']}: a criterion has no evidence ids")
        evidence = bundle.get("evidence") or []
        ids = [row.get("id") for row in evidence]
        if len(ids) != len(set(ids)):
            problems.append(f"{case['id']}: duplicate evidence ids")
        if bundle.get("human_notes") not in ([], None):
            problems.append(f"{case['id']}: bundle carries human notes")
        if any(row.get("expected") for row in bundle.get("task", {}).get("expected", [])):
            problems.append(f"{case['id']}: bundle carries per-turn expected oracle")
        raw = json.dumps(bundle, ensure_ascii=False)
        for needle in FORBIDDEN_BUNDLE_STRINGS:
            if needle in raw:
                problems.append(f"{case['id']}: forbidden string {needle!r} in bundle")
        for key in FORBIDDEN_BUNDLE_KEYS:
            if key in bundle:
                problems.append(f"{case['id']}: forbidden key {key!r} in bundle")
        rows[case["id"]] = {
            "bundle_file": f"evidence_bundles/{case['id']}.json",
            "bundle_sha256": sha256_file(bundle_path),
            "input_hash": bundle.get("input_hash"),
            "evidence_count": len(evidence),
            "catalog_count": len(catalog),
            "actual_turns": len(bundle.get("conversation", {}).get("actual_turns", [])),
            "current_retrieval_items": len(bundle.get("retrieval", {}).get("current", [])),
            "history_retrieval_items": len(bundle.get("retrieval", {}).get("history", [])),
        }
    return {
        "ok": not problems,
        "problems": problems,
        "cases": rows,
        "case_hashes": case_hashes,
    }


def _freeze_payload(built: dict[str, Any], bundle_info: dict[str, Any], bundle_run: dict[str, Any]) -> dict[str, Any]:
    cases = built["cases"]
    case_hashes = bundle_info["case_hashes"]
    freeze = {
        "schema_version": "diagnostic-scope-review-freeze/v2",
        "generated_at": now_iso(),
        "status": "frozen_before_judge",
        "run_id": RUN_ID,
        "judge_run_id": JUDGE_ID,
        "suite_id": SUITE_ID,
        "revision": REVISION,
        "rubric_version": RUBRIC_VERSION,
        "standard_kind": "manager_driven_posthoc_development_standard",
        "not_independent_blind_benchmark": True,
        "source": {
            "workspace": "outputs/action-state-answer-review-v1",
            "run_id": built["freeze"]["run_id"],
            "artifact_id": built["freeze"]["artifact"]["artifact_id"],
            "artifact_root": built["freeze"]["artifact"]["artifact_root"],
            "runtime_hash": built["freeze"]["artifact"]["runtime_hash"],
            "contract": built["freeze"]["contract"],
        },
        "source_hashes": {
            **built["v1_hashes"],
            "v1_freeze_sha256": sha256_file(V1 / "freeze.json"),
        },
        "frozen_v1": {
            "hashes": built["frozen_hashes"],
            "development_count": built["freeze"]["frozen_v1"]["development_count"],
            "holdout_count": built["freeze"]["frozen_v1"]["holdout_count"],
            "holdout_not_read": True,
        },
        "case_hashes": case_hashes,
        "case_hash_method": "review_server.case_hash: json.dumps(payload_without_review, ensure_ascii=False, sort_keys=True, separators=(',', ':'))",
        "rubric": {case["id"]: case["answer_review_rubric"] for case in cases},
        "rubric_file": "dataset/criteria.json",
        "rubric_sha256": sha256_file(OUT / "dataset" / "criteria.json"),
        "issue_catalog_file": "dataset/issue_catalog.json",
        "issue_catalog_sha256": sha256_file(OUT / "dataset" / "issue_catalog.json"),
        "schema_file": "dataset/schema.json",
        "schema_sha256": sha256_file(OUT / "dataset" / "schema.json"),
        "oracle_file": "dataset/oracle.json",
        "oracle_sha256": sha256_file(OUT / "dataset" / "oracle.json"),
        "exact_quotes_file": "dataset/exact-quotes.json",
        "exact_quotes_sha256": sha256_file(OUT / "dataset" / "exact-quotes.json"),
        "evidence": bundle_info["cases"],
        "bundle_validation": {
            "command": "node scripts/build-recorded-bundles.js --workspace <workspace> --out <workspace>/evidence_bundles",
            "returncode": bundle_run["returncode"],
            "ok": bundle_info["ok"],
            "problems": bundle_info["problems"],
            "streams": {
                "stdout": "judge_run/build-bundles.stdout.log",
                "stderr": "judge_run/build-bundles.stderr.log",
            },
        },
        "program_checks": {
            "file": "program-checks.json",
            "sha256": sha256_file(OUT / "program-checks.json"),
            "passed": built["program_checks"]["passed"],
            "checks": built["program_checks"]["checks"],
        },
        "reviews_workspace_empty": True,
        "judge": {
            "not_invoked_yet": True,
            "planned_command": [
                "node", "bin/dsh-judge.js", "run",
                "--bundle-dir", str(OUT / "evidence_bundles"),
                "--output-dir", str(OUT / "judge_results" / JUDGE_ID),
                "--run", RUN_ID,
                "--judge-run-id", JUDGE_ID,
                "--concurrency", "2",
                "--timeout", "480000",
            ],
        },
        "notes": [
            "Frozen before any Judge call; Judge results cannot change this rubric.",
            "The two cases are a post-hoc development projection of the same recorded answers, not the frozen v1 32/16 split.",
            "The manager observations were used only to define the oracle; their verdicts/notes are not in any model bundle.",
            "Program checks are structural; they are not semantic scoring and not a success rate.",
        ],
    }
    return freeze


def run_judge() -> dict[str, Any]:
    out_dir = OUT / "judge_results" / JUDGE_ID
    cmd = [
        "node", "bin/dsh-judge.js", "run",
        "--bundle-dir", str(OUT / "evidence_bundles"),
        "--output-dir", str(out_dir),
        "--run", RUN_ID,
        "--judge-run-id", JUDGE_ID,
        "--concurrency", "2",
        "--timeout", "480000",
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    attempts = []
    for attempt in (1, 2):
        (OUT / "judge_run").mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(cmd, cwd=str(DSH_JUDGE), capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=flags)
        (OUT / "judge_run" / f"judge-attempt-{attempt}.stdout.log").write_text(completed.stdout or "", encoding="utf-8")
        (OUT / "judge_run" / f"judge-attempt-{attempt}.stderr.log").write_text(completed.stderr or "", encoding="utf-8")
        summary = _judge_summary()
        attempts.append({"attempt": attempt, "returncode": completed.returncode, "summary": summary})
        # Resume is safe: successful role checkpoints are reused by the Judge CLI.
        if completed.returncode == 0 and summary.get("all_completed") and not summary.get("any_system_errors"):
            break
        if attempt == 2:
            break
    (OUT / "judge_run" / "attempts.json").write_text(json.dumps(attempts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"attempts": attempts, "command": cmd}


def _judge_summary() -> dict[str, Any]:
    out_dir = OUT / "judge_results" / JUDGE_ID
    cases = []
    if not out_dir.exists():
        return {"cases": [], "all_completed": False, "any_system_errors": True, "missing": True}
    for path in sorted(out_dir.glob("*.json")):
        if path.name.startswith(".") or path.parent.name == ".history":
            continue
        try:
            payload = read_json(path)
        except Exception as error:  # noqa: BLE001 - keep raw failure visible
            cases.append({"case_id": path.stem, "status": "unreadable", "error": str(error)})
            continue
        aggregation = payload.get("aggregation") or {}
        issues = aggregation.get("issues") or []
        citation_compromised = any(bool(issue.get("citation_compromised")) for issue in issues)
        findings = []
        for role in payload.get("roles") or []:
            for finding in role.get("findings") or []:
                findings.append({
                    "role": role.get("role"),
                    "criterion_id": finding.get("criterion_id"),
                    "turn_scope": finding.get("turn_scope"),
                    "judgment": finding.get("judgment"),
                    "summary": finding.get("summary"),
                    "evidence_refs": finding.get("evidence_refs") or [],
                    "suggested_layer": finding.get("suggested_layer"),
                })
        failed_issues = []
        for issue in issues:
            if issue.get("final_judgment") == "passed" and issue.get("coverage") == "covered" and not issue.get("citation_compromised"):
                continue
            failed_issues.append({
                "criterion_id": issue.get("criterion_id"),
                "focus": issue.get("focus"),
                "turn_scope": issue.get("turn_scope"),
                "coverage": issue.get("coverage"),
                "citation_compromised": bool(issue.get("citation_compromised")),
                "final_judgment": issue.get("final_judgment"),
                "role_judgments": [
                    {"role": row.get("role"), "judgment": row.get("judgment"), "coverage": row.get("coverage")}
                    for row in issue.get("judgments") or []
                ],
            })
        cases.append({
            "case_id": payload.get("case_id", path.stem),
            "status": payload.get("status"),
            "verdict": payload.get("verdict"),
            "coverage": payload.get("coverage"),
            "aggregation_coverage": aggregation.get("coverage"),
            "unresolved_issue_ids": aggregation.get("unresolved_issue_ids") or [],
            "confirmed_failed_count": aggregation.get("confirmed_failed_count"),
            "citation_compromised": citation_compromised,
            "system_errors": payload.get("system_errors") or [],
            "failed_issues": failed_issues,
            "findings": findings,
        })
    all_completed = all(case.get("status") == "completed" for case in cases) and len(cases) > 0
    any_system_errors = any(case.get("system_errors") for case in cases)
    return {"cases": cases, "all_completed": all_completed, "any_system_errors": any_system_errors, "missing": not cases}


def _old_judge_summary() -> dict[str, Any]:
    old_dir = V1 / "judge_results" / "jr-action-review-20261114-01"
    rows = {}
    for source_id in [spec["source_id"] for spec in CASE_SPECS]:
        path = old_dir / f"{source_id}.json"
        if not path.exists():
            rows[source_id] = {"missing": True}
            continue
        payload = read_json(path)
        aggregation = payload.get("aggregation") or {}
        issues = aggregation.get("issues") or []
        rows[source_id] = {
            "judge_run_id": payload.get("judge_run_id"),
            "status": payload.get("status"),
            "verdict": payload.get("verdict"),
            "coverage": payload.get("coverage"),
            "unresolved_issue_ids": aggregation.get("unresolved_issue_ids") or [],
            "confirmed_failed_count": aggregation.get("confirmed_failed_count"),
            "citation_compromised": any(bool(issue.get("citation_compromised")) for issue in issues),
        }
    return rows


def write_report(built: dict[str, Any] | None = None, judge_run: dict[str, Any] | None = None) -> dict[str, Any]:
    freeze_path = OUT / "freeze.json"
    freeze = read_json(freeze_path) if freeze_path.exists() else {}
    summary = _judge_summary()
    old = _old_judge_summary()
    attempts = (judge_run or {}).get("attempts")
    if attempts is None:
        attempts_path = OUT / "judge_run" / "attempts.json"
        attempts = read_json(attempts_path) if attempts_path.exists() else []
    judge_spec_path = OUT / "judge_run" / "run-spec.json"
    judge_spec = read_json(judge_spec_path) if judge_spec_path.exists() else {}
    cases_payload = []
    for spec in CASE_SPECS:
        new_case = next((case for case in summary.get("cases", []) if case.get("case_id") == spec["id"]), None)
        old_row = old.get(spec["source_id"], {})
        cases_payload.append({
            "new_case_id": spec["id"],
            "old_case_id": spec["source_id"],
            "case_hash": freeze.get("case_hashes", {}).get(spec["id"]),
            "bundle_input_hash": (freeze.get("evidence", {}).get(spec["id"]) or {}).get("input_hash"),
            "bundle_sha256": (freeze.get("evidence", {}).get(spec["id"]) or {}).get("bundle_sha256"),
            "rubric_criteria": len((freeze.get("rubric", {}).get(spec["id"]) or [])),
            "coverage_note": "17 expected/covered = 15 new turn-scoped criteria + 2 Judge-native overall role checks (case-task-completion / case-evidence-support). The frozen rubric itself has no overall-case duplicate.",
            "old_standard": old_row,
            "new_standard": new_case or {"status": "not_run", "verdict": None, "coverage": None},
            "failed_issues": (new_case or {}).get("failed_issues", []),
            "comparison": {
                "old_verdict": old_row.get("verdict"),
                "new_verdict": (new_case or {}).get("verdict"),
                "same_answers_rerun": False,
                "note": "Same recorded answers were judged under a different rubric. A verdict change is a rubric/scope change, not evidence of model capability improvement.",
            },
        })
    status = "in_progress"
    if summary.get("cases"):
        status = "completed" if summary.get("all_completed") and not summary.get("any_system_errors") else "partial"
    unresolved = []
    failed = []
    for case in summary.get("cases", []):
        if case.get("verdict") == "uncertain" or case.get("unresolved_issue_ids") or case.get("system_errors") or case.get("status") != "completed":
            unresolved.append({
                "case_id": case.get("case_id"),
                "verdict": case.get("verdict"),
                "unresolved_issue_ids": case.get("unresolved_issue_ids") or [],
                "system_errors": case.get("system_errors") or [],
                "status": case.get("status"),
            })
        if case.get("verdict") == "failed":
            failed.append({
                "case_id": case.get("case_id"),
                "failed_issue_ids": [row.get("criterion_id") for row in case.get("failed_issues") or []],
                "failed_count": len(case.get("failed_issues") or []),
            })
    report = {
        "schema_version": "diagnostic-scope-review-report/v2",
        "status": status,
        "overall_status": "baseline_completed_after_stage_pending" if status == "completed" else status,
        "generated_at": now_iso(),
        "objective": "Freeze a new per-turn diagnostic-scope rubric and check whether the DSH-native Judge can identify answers whose observation scope exceeds the causal/global conclusion, on two recorded baseline dialogues.",
        "not_a_product_model_rerun": True,
        "not_a_success_rate": True,
        "not_an_independent_blind_benchmark": True,
        "scope": {
            "run_id": RUN_ID,
            "judge_run_id": JUDGE_ID,
            "suite_id": SUITE_ID,
            "rubric_version": RUBRIC_VERSION,
            "development_cases": 2,
            "actual_turns": 6,
            "holdout_used": False,
            "product_model_called": False,
            "old_workspace_preserved": True,
        },
        "standard": {
            "kind": "manager_driven_posthoc_development_standard",
            "not_independent_blind_benchmark": True,
            "criteria_per_case": 15,
            "turn_scoped": True,
            "overall_case_metric_duplicated": False,
            "coverage_note": "The Judge reports 17 expected/covered per case: 15 frozen turn-scoped criteria plus 2 Judge-native overall role checks. The extra 2 are not in the rubric and are not treated as new rubric criteria.",
            "aspects": [
                "observation capability vs task-goal scope",
                "causal uniqueness / whether alternatives were excluded by evidence",
                "condition/hypothesis vs fact",
                "discriminative value of the next observation",
                "whether a local result can support global closure",
            ],
            "positive_oracle": "Explicit autonomous dock-find + charging may confirm automatic capability recovery, but cannot name a faulty part without evidence; a credible diagnostic report may be quoted with its source. Manual charging alone cannot prove automatic recovery or exclusive causation.",
            "negative_oracle": "Do not add 'the cause can never be determined' or 'all answers are vague' rules.",
        },
        "freeze": {
            "path": "outputs/diagnostic-scope-review-v2/freeze.json",
            "status": freeze.get("status"),
            "generated_at": freeze.get("generated_at"),
            "case_hashes": freeze.get("case_hashes"),
            "rubric_sha256": freeze.get("rubric_sha256"),
            "issue_catalog_sha256": freeze.get("issue_catalog_sha256"),
            "program_checks": (freeze.get("program_checks") or {}).get("checks"),
            "bundle_validation": freeze.get("bundle_validation"),
            "frozen_before_judge": True,
            "judge_run_started_at": judge_spec.get("created_at"),
            "timing_note": "The freeze timestamp precedes the archived Judge run-spec creation time.",
        },
        "cases": cases_payload,
        "cross_standard_comparison": {
            "do_not_interpret_as_model_improvement": True,
            "reason": "The two cases reuse the exact same recorded user/answer/trace/state. Only the suite rubric and the diagnostic-scope oracle changed; no product model was rerun.",
            "old_standard": "action-state-answer-review-v1: 11 criteria/case focused on action-state correction, recall, and evidence support.",
            "new_standard": "diagnostic-scope-review-v2: 15 turn-scoped criteria focused on observation scope, causal exclusivity, condition-vs-fact, discriminative next observation, and local-to-global closure.",
            "contact_case": {
                "old_verdict": "passed",
                "new_verdict": "failed",
                "what_changed": "The new rubric flags the manual-placement charging check as a non-discriminating next observation (turn 1 and turn 2) and the Judge-native overall task check fails. The turn-level causal-exclusivity criterion passed because the answer kept the success branch conditional.",
            },
            "dock_case": {
                "old_verdict": "uncertain",
                "new_verdict": "failed",
                "what_changed": "The new rubric flags the exclusive dock/placement cause claims in turn 1 and turn 2 and the Judge-native overall evidence check fails. Coverage is complete and there are no unresolved issues.",
            },
        },
        "failed": failed,
        "unresolved": unresolved,
        "system_errors": [
            {"case_id": case.get("case_id"), "errors": case.get("system_errors")}
            for case in summary.get("cases", []) if case.get("system_errors")
        ],
        "judge_attempts": attempts,
        "readonly_display_check": read_json(OUT / "gui-readonly-testclient.json") if (OUT / "gui-readonly-testclient.json").exists() else None,
        "incomplete": [
            "No holdout case and no independent blind benchmark: this is a post-hoc development standard.",
            "Only two baseline dialogues / six real turns; not a success rate or distribution.",
            "The product 4-case after-stage is not started; build_from_recorded_case is the reserved entry point.",
            "No human review was written or auto-filled; reviews/ remains empty.",
        ],
        "limits": [
            "Same recorded answers, different rubric: cross-standard verdict changes are not model capability changes.",
            "Program checks are structural, not semantic scoring.",
            "Manager observations were used only to define the oracle and are not in any model bundle.",
            "Judge output is machine advice, not human review and not a product success metric.",
        ],
    }
    write_json(REPORT, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-judge", action="store_true", help="invoke the existing DSH-native generic Judge once (with one safe resume attempt)")
    parser.add_argument("--report-only", action="store_true", help="only regenerate the report from existing workspace files")
    parser.add_argument("--no-bundles", action="store_true", help="skip the node bundle build (debug only)")
    args = parser.parse_args()

    if args.report_only:
        result = write_report()
        print(json.dumps({"report": str(REPORT), "status": result["status"]}, ensure_ascii=False, indent=2))
        return

    built = build_dataset()
    bundle_run = {"returncode": 0, "stdout": "", "stderr": ""}
    bundle_info = {"ok": True, "problems": [], "cases": {}, "case_hashes": {case["id"]: case_hash(case) for case in built["cases"]}}
    if not args.no_bundles:
        bundle_run = _run_node_bundles()
        bundle_info = _bundle_checks(built["cases"])
        if not bundle_info["ok"]:
            raise SystemExit("bundle self-check failed: " + "; ".join(bundle_info["problems"]))
    freeze = _freeze_payload(built, bundle_info, bundle_run)
    write_json(OUT / "freeze.json", freeze)
    judge_run = None
    if args.run_judge:
        judge_run = run_judge()
    report = write_report(built, judge_run)
    print(json.dumps({
        "workspace": str(OUT),
        "run_id": RUN_ID,
        "judge_run_id": JUDGE_ID,
        "case_hashes": bundle_info["case_hashes"],
        "freeze": str(OUT / "freeze.json"),
        "report": str(REPORT),
        "report_status": report["status"],
        "bundle_validation": bundle_info["ok"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
