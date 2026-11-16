# -*- coding: utf-8 -*-
"""Bounded fact-boundary closeout batch (4 sessions / 6 turns, isolated port 8949).

This script is intentionally narrow.  It freezes the planned cases/oracles before
any model call, reuses the existing final artifact and the real smoke helpers,
runs independent sessions with bounded concurrency 2, captures actual
semantic/state/prompt/answer per turn, and reports state checks separately from
answer findings.  It does not rebuild the artifact, run holdout, run Judge,
change the rubric, or rerun a model to turn an oracle green.

The script was authored while the DSH shell was blocked by a workspace
permission failure.  It is therefore unexecuted in the closeout batch that
created it; see outputs/fact-boundary-closeout-v1/batch-status.json.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "runtime"))

import diagnostic_scope_smoke as dsm  # noqa: E402
import first_turn_context_smoke as ft  # noqa: E402
from robot_vacuum_support.state import prompt_context  # noqa: E402

dl = dsm.dl

DEFAULT_ARTIFACT = ROOT.parent / "robot-vacuum-support-built" / "robot_vacuum-v4-6aa0ba49c6"
DEFAULT_FREEZE = ROOT / "outputs" / "fact-boundary-closeout-v1" / "input-freeze.json"
DEFAULT_OUT = ROOT / "outputs" / "fact-boundary-closeout-v1" / "live"
DEFAULT_ENV = ROOT.parent / "my_agent_harness" / ".env"

SOURCE_FILES = (
    "state.py",
    "diagnostic_scope.py",
    "interpretation.py",
    "actions.py",
    "questions.py",
    "routing.py",
)

_PHRASES = {
    "mentions_place": re.compile(r"放上|放回|放到|搁|放上去|放回充电座"),
    "mentions_alignment": re.compile(r"对准|对齐|对准充电极片"),
    "mentions_failure": re.compile(r"充不上|充不进|失败|没充|未恢复|还是不行"),
    "says_outcome_unknown": re.compile(r"结果还不清楚|结果未知|结果未说明|不清楚结果|结果不清楚"),
    "mentions_correction": re.compile(r"更正|说错|纠正|以你(?:最新|刚才)|改口"),
    "mentions_third_party": re.compile(r"朋友|他人|别人|第三方|他家"),
    "hedges_transfer": re.compile(r"不能|无法|不代表|不一定|还需要|没法|说明不了"),
    "claims_resolved": re.compile(r"可以结案|任务目标已达成|已经恢复|问题已解决|已经解决|已恢复|成功"),
    "claims_cause_confirmed": re.compile(r"原因(?:就)?是|确定是|已确认(?:原因|故障)|说明(?:就是|是).{0,4}(?:原因|故障)"),
}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _phrase_flags(answer: str) -> dict[str, bool]:
    """Cheap, report-only answer phrase flags.  Not a semantic score."""
    value = str(answer or "")
    return {name: bool(pattern.search(value)) for name, pattern in _PHRASES.items()}


def _safe_prompt_text(support: dict[str, Any], snapshot: dict[str, Any]) -> str:
    try:
        text = prompt_context(copy.deepcopy(support), copy.deepcopy(snapshot))
    except Exception as exc:  # noqa: BLE001 - capture, never silently turn green
        return f"<prompt_context_error: {type(exc).__name__}: {str(exc)[:300]}>"
    return str(text or "")


def _tried_lines(prompt_text: str) -> list[str]:
    return [
        line
        for line in str(prompt_text or "").splitlines()
        if line.startswith("- ") and "用户第" in line and "消息" in line
    ]


def _semantic_view(support: dict[str, Any]) -> dict[str, Any]:
    block = support.get("last_interpretation") or {}
    interpretation = block.get("interpretation") or {}
    trace = block.get("trace") or {}
    return {
        "method": trace.get("method"),
        "model": trace.get("model"),
        "usage_status": trace.get("usage_status"),
        "total_tokens": (trace.get("usage") or {}).get("total_tokens"),
        "validation_errors": trace.get("validation_errors") or [],
        "fallback_reason": trace.get("fallback_reason"),
        "topic": interpretation.get("topic"),
        "closure_signal": interpretation.get("closure_signal"),
        "interaction_stage": interpretation.get("interaction_stage"),
        "executed_actions": interpretation.get("executed_actions") or [],
        "not_tried_actions": interpretation.get("not_tried_actions") or [],
        "capability_observations": interpretation.get("capability_observations") or [],
        "observation_retractions": interpretation.get("observation_retractions") or [],
        "negation_scope": interpretation.get("negation_scope"),
        "negation_quote": interpretation.get("negation_quote") or "",
    }


def _effective(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        row
        for row in rows or []
        if isinstance(row, dict) and str(row.get("fact_status") or "effective") == "effective"
    ]


def _state_view(active: dict[str, Any] | None) -> dict[str, Any]:
    active = active or {}
    return {
        "case_id": active.get("case_id"),
        "status": active.get("status"),
        "stage": active.get("stage"),
        "resolved": bool(active.get("resolved_at") or active.get("status") == "resolved"),
        "resolved_at": active.get("resolved_at"),
        "resolution": active.get("resolution"),
        "closure_evidence": active.get("closure_evidence"),
        "next_step": active.get("next_step"),
        "tried": [
            {
                "action_id": row.get("action_id"),
                "label": row.get("label"),
                "outcome": row.get("outcome"),
                "evidence": row.get("evidence"),
                "fact_status": row.get("fact_status"),
                "source_turn": row.get("source_turn"),
                "source_message_id": row.get("source_message_id"),
            }
            for row in _effective(active.get("tried_actions"))
        ],
        "not_tried": [
            {
                "action_id": row.get("action_id"),
                "label": row.get("label"),
                "evidence": row.get("evidence"),
                "fact_status": row.get("fact_status"),
                "source_turn": row.get("source_turn"),
            }
            for row in _effective(active.get("not_tried_actions"))
        ],
        "observations": [
            {
                "capability": row.get("capability"),
                "result": row.get("result"),
                "polarity": row.get("polarity"),
                "quote": row.get("quote"),
                "turn": row.get("turn"),
                "source": row.get("source"),
                "extraction": row.get("extraction"),
                "fact_status": row.get("fact_status"),
            }
            for row in _effective(active.get("observations"))
        ],
        "questions": [
            {
                "slot": row.get("slot"),
                "status": row.get("status"),
                "ask_count": row.get("ask_count"),
                "answer_value": row.get("answer_value"),
            }
            for row in active.get("questions") or []
        ],
        "fact_audit": active.get("fact_audit") or [],
    }


def _capture_turn(turn: dict[str, Any], artifacts: dict[str, Any], index: int) -> dict[str, Any]:
    support = artifacts.get("support_state") or {}
    active = artifacts.get("case") or {}
    snapshot = support.get("decision_snapshot") or {}
    prompt_text = _safe_prompt_text(support, snapshot)
    answer = turn.get("answer") or ""
    return {
        "turn": index,
        "request_id": turn.get("request_id"),
        "turn_id": turn.get("turn_id"),
        "server_request_id": turn.get("server_request_id"),
        "http_status": turn.get("http_status"),
        "error": turn.get("error"),
        "transport_attempts": turn.get("transport_attempts") or [],
        "answer": answer,
        "answer_chars": len(answer),
        "answer_flags": _phrase_flags(answer),
        "semantic": _semantic_view(support),
        "state": _state_view(active),
        "prompt": {
            "text": prompt_text[:12000],
            "tried_lines": _tried_lines(prompt_text),
            "sha256": _sha256_text(prompt_text),
            "reconstructed_from_post_turn_state": True,
            "caveat": "tried/observation lines reflect the pre-answer state; suggested_actions may include the post-answer write",
        },
        "snapshot": snapshot,
    }


def _retryable_transport(turn: dict[str, Any]) -> bool:
    """Only retry when the request clearly never reached a committed server turn.

    A timeout or a dropped response after the server may have committed would
    duplicate transcript/state side effects; those are preserved as failed
    output instead of retried.  See batch policy: one retry for the affected
    call only, never a model rerun to turn an oracle green.
    """
    if turn.get("answer") or turn.get("http_status") is not None or turn.get("event_types"):
        return False
    error = str(turn.get("error") or "")
    return any(
        token in error
        for token in (
            "Connection refused",
            "ConnectionRefused",
            "10061",
            "NewConnectionError",
            "Failed to establish",
        )
    )


def _send_turn_one_retry(
    url: str, request_id: str, turn_id: str, question: str, latency
) -> dict[str, Any]:
    """At most one retry, only for a clearly pre-commit transport failure."""
    attempts: list[dict[str, Any]] = []
    turn: dict[str, Any] = {}
    for attempt in range(2):
        attempt_request_id = request_id if attempt == 0 else f"{request_id}-retry1"
        turn = dl._send_turn(url, attempt_request_id, turn_id, question, latency)
        retryable = _retryable_transport(turn)
        attempts.append(
            {
                "attempt": attempt + 1,
                "request_id": attempt_request_id,
                "http_status": turn.get("http_status"),
                "error": turn.get("error"),
                "answer_chars": len(turn.get("answer") or ""),
                "retryable_precommit": retryable,
            }
        )
        if not turn.get("error") and turn.get("answer"):
            break
        if attempt == 0 and retryable:
            time.sleep(1)
            continue
        break
    turn["transport_attempts"] = attempts
    return turn


def _run_session(
    run_dir: Path, case_id: str, questions: list[str], latency
) -> dict[str, Any]:
    session_id: str | None = None
    turns: list[dict[str, Any]] = []
    try:
        session_id = dl._http_json(
            f"http://127.0.0.1:{dl.PORT}/api/sessions", {"workflow": "agent"}
        )["session_id"]
        previous_trace_count = 0
        for index, text in enumerate(questions, start=1):
            turn = _send_turn_one_retry(
                f"http://127.0.0.1:{dl.PORT}/api/sessions/{session_id}/messages",
                f"{case_id}-t{index}-{int(time.time())}",
                f"{session_id}-t{index}",
                text,
                latency,
            )
            try:
                artifacts = ft._session_artifacts(run_dir, session_id)
                turn_events = (artifacts.get("trace_events") or [])[previous_trace_count:]
                previous_trace_count = len(artifacts.get("trace_events") or [])
                captured = _capture_turn(turn, artifacts, index)
                captured["trace_event_count"] = len(turn_events)
            except Exception as exc:  # noqa: BLE001 - preserve the actual answer
                captured = {
                    "turn": index,
                    "request_id": turn.get("request_id"),
                    "http_status": turn.get("http_status"),
                    "error": turn.get("error") or f"capture_error: {type(exc).__name__}: {str(exc)[:300]}",
                    "transport_attempts": turn.get("transport_attempts") or [],
                    "answer": turn.get("answer") or "",
                    "answer_chars": len(turn.get("answer") or ""),
                    "answer_flags": _phrase_flags(turn.get("answer") or ""),
                    "semantic": {},
                    "state": {},
                    "prompt": {
                        "text": "",
                        "tried_lines": [],
                        "sha256": "",
                        "reconstructed_from_post_turn_state": False,
                        "caveat": "capture failed; no prompt projection",
                    },
                    "snapshot": {},
                    "capture_error": f"{type(exc).__name__}: {str(exc)[:300]}",
                }
            turns.append(captured)
    except Exception as exc:  # noqa: BLE001
        return {
            "session_id": session_id,
            "questions": questions,
            "turns": turns,
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            "partial": True,
        }
    base = run_dir / "instance-storage" / "sessions" / str(session_id)
    return {
        "session_id": session_id,
        "questions": questions,
        "turns": turns,
        "source_paths": {
            "support_state": str(base / "support_state.json"),
            "transcript": str(base / "transcript.jsonl"),
            "trace_dir": str(base / "outputs" / "traces"),
            "raw_session_dir": str(base),
        },
    }


def _state_checks(oracle: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    tried = {(row.get("action_id"), row.get("outcome")) for row in state.get("tried") or []}
    tried_ids = {row.get("action_id") for row in state.get("tried") or []}
    not_tried_ids = {row.get("action_id") for row in state.get("not_tried") or []}
    observations = {
        (row.get("capability"), row.get("result")) for row in state.get("observations") or []
    }
    checks: dict[str, bool] = {}
    expected_status = oracle.get("expected_case_status")
    if expected_status and expected_status != "any":
        checks["case_status"] = state.get("status") == expected_status
    if "expected_resolved" in oracle:
        checks["resolved"] = bool(state.get("resolved")) is bool(oracle["expected_resolved"])
    for capability, result in (oracle.get("expected_observations") or {}).items():
        checks[f"observation:{capability}={result}"] = (capability, result) in observations
    for item in oracle.get("expected_tried") or []:
        checks[f"tried:{item.get('action_id')}={item.get('outcome')}"] = (
            item.get("action_id"),
            item.get("outcome"),
        ) in tried
    for action_id in oracle.get("forbid_tried") or []:
        checks[f"forbid_tried:{action_id}"] = action_id not in tried_ids
    for action_id in oracle.get("expected_not_tried") or []:
        checks[f"not_tried:{action_id}"] = action_id in not_tried_ids
    for capability in oracle.get("forbid_observations") or []:
        checks[f"forbid_observation:{capability}"] = not any(
            observed_capability == capability for observed_capability, _ in observations
        )
    return {
        "checks": checks,
        "passed": all(checks.values()) if checks else None,
    }


def _boundary(
    case_id: str, turn_index: int, state_check: dict[str, Any], answer_flags: dict[str, bool]
) -> str:
    passed = state_check.get("passed")
    unknown = bool(answer_flags.get("says_outcome_unknown"))
    if case_id == "fb-partial-place-vs-align-01" and turn_index == 1:
        distinguishes = bool(answer_flags.get("mentions_place")) and bool(
            answer_flags.get("mentions_alignment")
        )
        if passed and distinguishes and not unknown:
            return "observed"
        if passed is False or unknown:
            return "unobserved"
        return "uncertain"
    if case_id == "fb-partial-place-vs-align-01" and turn_index == 2:
        if passed and answer_flags.get("mentions_alignment") and not unknown:
            return "observed"
        if passed is False or unknown:
            return "unobserved"
        return "uncertain"
    if case_id == "fb-third-party-hypothetical-01":
        hedged = bool(answer_flags.get("mentions_third_party")) and bool(
            answer_flags.get("hedges_transfer")
        )
        unsafe = bool(answer_flags.get("claims_cause_confirmed")) or bool(
            answer_flags.get("claims_resolved")
        )
        if passed and hedged and not unsafe:
            return "observed"
        if passed is False or unsafe:
            return "unobserved"
        return "uncertain"
    if case_id == "fb-correction-execution-outcome-01" and turn_index == 1:
        if passed and not unknown:
            return "observed"
        if passed is False or unknown:
            return "unobserved"
        return "uncertain"
    if case_id == "fb-correction-execution-outcome-01" and turn_index == 2:
        if passed and answer_flags.get("mentions_correction"):
            return "observed"
        if passed is False:
            return "unobserved"
        return "uncertain"
    if case_id == "fb-explicit-auto-recovery-01":
        closes = bool(answer_flags.get("claims_resolved"))
        cause_safe = not bool(answer_flags.get("claims_cause_confirmed"))
        if passed and closes and cause_safe:
            return "observed"
        if passed is False or not closes or not cause_safe:
            return "unobserved"
        return "uncertain"
    if passed:
        return "observed"
    if passed is False:
        return "unobserved"
    return "uncertain"


def _evaluate(payload: dict[str, Any], freeze_cases: dict[str, Any]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for case_id, spec in freeze_cases.items():
        session = (payload.get("sessions") or {}).get(case_id) or {}
        case_result: dict[str, Any] = {
            "session_id": session.get("session_id"),
            "turns": [],
            "note": spec.get("note"),
        }
        for index, turn in enumerate(session.get("turns") or [], start=1):
            oracle = ((spec.get("oracle") or {}).get(f"turn_{index}") or {})
            state_check = _state_checks(oracle, turn.get("state") or {})
            boundary = _boundary(case_id, index, state_check, turn.get("answer_flags") or {})
            case_result["turns"].append(
                {
                    "turn": index,
                    "boundary": boundary,
                    "boundary_goal": oracle.get("boundary_goal"),
                    "expected_answer_findings": oracle.get("expected_answer_findings") or [],
                    "state_check": state_check,
                    "answer_flags": turn.get("answer_flags") or {},
                    "answer_excerpt": (turn.get("answer") or "")[:600],
                }
            )
        results[case_id] = case_result
    return results


def _review_counts() -> dict[str, Any]:
    counts: dict[str, Any] = {}

    def _store_records(path: Path) -> Any:
        if not path.is_file():
            return "missing"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "unreadable"
        if isinstance(data, dict):
            return len(data.get("records") or [])
        return "unexpected_shape"

    eval_store = ROOT / "eval" / "reviews" / "store.json"
    eval_review_dir = ROOT / "eval" / "reviews" / "reviews"
    after_store = (
        ROOT / "outputs" / "diagnostic-scope-after-review-v2-validated" / "reviews" / "store.json"
    )
    action_store = (
        ROOT / "outputs" / "action-state-answer-review-v1" / "reviews" / "store.json"
    )
    counts["eval_reviews_review_files"] = len(list(eval_review_dir.glob("*.json")))
    counts["eval_reviews_store_records"] = _store_records(eval_store)
    counts["after_v2_reviews_records"] = _store_records(after_store)
    counts["action_state_review_records"] = _store_records(action_store)
    counts["eval_reviews_reviews_dir_exists"] = eval_review_dir.is_dir()
    counts["all_expected_present"] = (
        eval_store.is_file()
        and eval_review_dir.is_dir()
        and after_store.is_file()
        and action_store.is_file()
    )
    return counts


def _port_flags(ports: tuple[int, ...] = (8950, 8955, 8956)) -> dict[str, bool]:
    return {str(port): bool(dl._port_in_use("127.0.0.1", port)) for port in ports}


def _batch_checks(payload: dict[str, Any]) -> dict[str, bool]:
    sessions = payload.get("sessions") or {}
    all_turns = [turn for session in sessions.values() for turn in session.get("turns") or []]
    transport_errors: list[tuple[Any, Any, Any]] = []
    for case_id, session in sessions.items():
        if session.get("error"):
            transport_errors.append((case_id, "session", session.get("error")))
        for turn in session.get("turns") or []:
            if turn.get("error"):
                transport_errors.append((case_id, turn.get("turn"), turn.get("error")))
            for attempt in turn.get("transport_attempts") or []:
                if attempt.get("error"):
                    transport_errors.append(
                        (
                            case_id,
                            turn.get("turn"),
                            f"attempt {attempt.get('attempt')}: {attempt.get('error')}",
                        )
                    )
    checks = {
        "four_sessions": len(sessions) == 4,
        "six_turns": len(all_turns) == 6,
        "freeze_written_before_model_calls": bool(payload.get("freeze_written_before_model_calls")),
        "artifact_check_ok": bool((payload.get("artifact_check") or {}).get("ok")),
        "source_artifact_hash_match": bool(payload.get("source_artifact_hash_match")),
        "port_released": bool(payload.get("port_released")),
        "all_answers_nonempty": all(int(turn.get("answer_chars") or 0) > 0 for turn in all_turns),
        "no_transport_errors": not transport_errors,
        "no_session_errors": all(not session.get("error") for session in sessions.values()),
        "review_counts_preserved": bool(
            (payload.get("review_counts_before") or {}).get("all_expected_present")
        )
        and payload.get("review_counts_before") == payload.get("review_counts_after"),
        "existing_service_ports_preserved": payload.get("existing_service_ports_before")
        == payload.get("existing_service_ports_after"),
    }
    payload["transport_errors"] = transport_errors
    payload["transport_error_count"] = len(transport_errors)
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT))
    parser.add_argument("--freeze", default=str(DEFAULT_FREEZE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--env-file", default=str(DEFAULT_ENV))
    parser.add_argument("--port", type=int, default=8949)
    args = parser.parse_args()

    dl.PORT = int(args.port)
    if dl.PORT != 8949:
        raise SystemExit("this closeout batch is restricted to isolated port 8949")
    freeze_path = Path(args.freeze).resolve()
    if not freeze_path.is_file():
        raise SystemExit(f"freeze not found: {freeze_path}")
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    if not isinstance(freeze, dict):
        raise SystemExit("freeze root is not an object")
    freeze_cases = freeze.get("cases") or {}
    if not isinstance(freeze_cases, dict) or not freeze_cases:
        raise SystemExit("freeze has no case object")
    for case_id, spec in freeze_cases.items():
        if not isinstance(spec, dict):
            raise SystemExit(f"freeze case {case_id!r} is not an object")
        questions = spec.get("questions")
        if not isinstance(questions, list) or not questions:
            raise SystemExit(f"freeze case {case_id!r} has no questions list")
        if not all(isinstance(question, str) and question.strip() for question in questions):
            raise SystemExit(f"freeze case {case_id!r} has a non-string/empty question")
        if not isinstance(spec.get("oracle"), dict):
            raise SystemExit(f"freeze case {case_id!r} has no oracle object")
    if sum(len(spec["questions"]) for spec in freeze_cases.values()) > 6:
        raise SystemExit("freeze exceeds the 6-turn cap")
    if len(freeze_cases) > 4:
        raise SystemExit("freeze exceeds the 4-session cap")

    run_dir = Path(args.out).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise SystemExit(f"refusing to overwrite non-empty run directory: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    for child in ("spans", "provider-records"):
        (run_dir / child).mkdir(parents=True, exist_ok=True)
    env_file = Path(args.env_file)
    if not env_file.is_file():
        raise SystemExit("environment file not found (values are not printed)")

    built = dl._load_artifact(Path(args.artifact_root))
    source_hashes, artifact_hashes, hash_match = dsm._hashes(Path(built["artifact_root"]))
    if not hash_match:
        raise SystemExit("source/artifact hash mismatch; refusing to call the model")

    try:
        review_before = _review_counts()
    except Exception as exc:  # noqa: BLE001
        review_before = {"all_expected_present": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    try:
        ports_before = _port_flags()
    except Exception as exc:  # noqa: BLE001
        ports_before = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    frozen_copy = copy.deepcopy(freeze)
    frozen_copy.update(
        {
            "execution_status": "frozen_before_model_calls",
            "frozen_at": _now_iso(),
            "artifact": built,
            "source_hashes": source_hashes,
            "artifact_hashes": artifact_hashes,
            "source_artifact_hash_match": hash_match,
            "port": dl.PORT,
            "expected_sessions": len(freeze_cases),
            "expected_turn_count": sum(
                len(spec.get("questions") or []) for spec in freeze_cases.values()
            ),
            "review_counts_before": review_before,
            "existing_service_ports_before": ports_before,
        }
    )
    frozen_path = run_dir / "input-freeze.json"
    frozen_path.write_text(
        json.dumps(frozen_copy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    payload: dict[str, Any] = {
        "schema_version": "fact-boundary-closeout-run/v1",
        "run_id": run_dir.name,
        "started_at": _now_iso(),
        "port": dl.PORT,
        "artifact": built,
        "artifact_check": None,
        "source_hashes": source_hashes,
        "artifact_hashes": artifact_hashes,
        "source_artifact_hash_match": hash_match,
        "freeze_path": str(frozen_path),
        "freeze_written_before_model_calls": True,
        "env_file_used": str(env_file),
        "env_values_read_or_logged": False,
        "review_counts_before": review_before,
        "existing_service_ports_before": ports_before,
        "sessions": {},
        "error": None,
        "not_holdout": True,
        "not_success_rate": True,
    }

    server: Any = None
    try:
        instance = dl._write_instance(run_dir, built, env_file)
        server = dl.Server(
            built,
            instance,
            run_dir / "server.log",
            run_dir / "spans",
            run_dir / "provider-records",
        )
        from core.observability import latency

        payload["artifact_check"] = dsm._external_check(Path(built["artifact_root"]))
        if not payload["artifact_check"].get("ok"):
            raise RuntimeError("artifact --check failed before model calls")
        payload["health"] = server.start()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(_run_session, run_dir, case_id, list(spec["questions"]), latency): case_id
                for case_id, spec in freeze_cases.items()
            }
            for future in as_completed(futures):
                case_id = futures[future]
                try:
                    payload["sessions"][case_id] = future.result()
                except Exception as exc:  # noqa: BLE001
                    payload["sessions"][case_id] = {
                        "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                        "turns": [],
                    }
    except Exception as exc:  # noqa: BLE001
        payload["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        if server is not None:
            try:
                server.stop()
            except Exception as exc:  # noqa: BLE001 - never skip run.json for cleanup
                payload["server_stop_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        payload["finished_at"] = _now_iso()
        try:
            payload["port_released"] = not dl._port_in_use("127.0.0.1", dl.PORT)
        except Exception as exc:  # noqa: BLE001
            payload["port_released"] = None
            payload["port_check_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        try:
            payload["review_counts_after"] = _review_counts()
        except Exception as exc:  # noqa: BLE001
            payload["review_counts_after"] = None
            payload["review_counts_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        try:
            payload["existing_service_ports_after"] = _port_flags()
        except Exception as exc:  # noqa: BLE001
            payload["existing_service_ports_after"] = None
            payload["service_ports_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        payload["server_log"] = str(run_dir / "server.log")

    try:
        payload["evaluation"] = _evaluate(payload, freeze_cases)
    except Exception as exc:  # noqa: BLE001
        payload["evaluation"] = {}
        payload["evaluation_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    try:
        payload["checks"] = _batch_checks(payload)
    except Exception as exc:  # noqa: BLE001
        payload["checks"] = {"internal_error": False}
        payload["checks_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    before_counts = payload.get("review_counts_before") or {}
    after_counts = payload.get("review_counts_after") or {}
    payload["review_counts_preserved"] = bool(
        before_counts.get("all_expected_present")
    ) and before_counts == after_counts
    payload["existing_service_ports_preserved"] = (
        payload.get("existing_service_ports_before") == payload.get("existing_service_ports_after")
    )
    payload["passed"] = all(payload["checks"].values()) and not payload.get("error")
    try:
        (run_dir / "run.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "run": str(run_dir),
                    "error": f"run.json write failed: {type(exc).__name__}: {str(exc)[:300]}",
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "run": str(run_dir),
                "artifact": built.get("artifact_id"),
                "sessions": len(payload.get("sessions") or {}),
                "turns": sum(len(s.get("turns") or []) for s in (payload.get("sessions") or {}).values()),
                "passed_structural_checks": payload.get("passed"),
                "checks": payload.get("checks"),
                "review_counts_preserved": payload.get("review_counts_preserved"),
                "error": payload.get("error"),
                "boundaries": {
                    case_id: [turn.get("boundary") for turn in result.get("turns") or []]
                    for case_id, result in (payload.get("evaluation") or {}).items()
                },
            },
            ensure_ascii=False,
        )
    )
    return 0 if payload.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
