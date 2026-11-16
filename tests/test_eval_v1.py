# -*- coding: utf-8 -*-
"""Tests for the formal v1 runner, scorers and Inspect adapter.

These tests build a temporary two-case dataset; they do not read or modify
``eval/datasets/v1``.  The real final dataset is validated separately.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval import runner  # noqa: E402
from eval import scorers  # noqa: E402


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case(case_id: str, split: str, *, state: dict, claims: list[str] | None = None) -> dict:
    return {
        "id": case_id,
        "family_id": f"family-{case_id}",
        "category": "recharge",
        "split": split,
        "origin": {"kind": "synthetic", "seed_ids": ["rs-01"]},
        "review": {"status": "pending_human"},
        "turns": [
            {
                "role": "user",
                "content": "回充失败怎么办",
                "expected": {
                    "required_claims": claims or ["给出低风险排查步骤"],
                    "forbidden_claims": ["承诺官方保修"],
                    "state": state,
                    "required_behaviors": ["先排查再结论"],
                    "expected_sources": ["故障排除.txt"],
                },
            }
        ],
        "evidence": [
            {
                "id": "e1",
                "file": "README.md",
                "quote": "扫地机器人信息问答客服",
                "sha256": _sha(ROOT / "README.md"),
                "kind": "requirement",
            }
        ],
        "setup": {"kind": "normal", "steps": []},
        "scoring": {"automatic": ["state_contract", "citation_present"], "human": ["semantic_claims"]},
    }


def _write_dataset(tmp_path: Path) -> Path:
    base = tmp_path / "v1"
    base.mkdir()
    state_hash = _sha(ROOT / "runtime" / "robot_vacuum_support" / "state.py")
    cases = [
        _case(
            "dev-1",
            "development",
            state={"case_status": "active", "tried_actions": [], "case_count": 1, "scope": "recharge"},
        ),
        _case(
            "hold-1",
            "holdout",
            state={"case_status": "active", "tried_actions": []},
            claims=["留出题只做完整性检查"],
        ),
    ]
    suite = {
        "schema_version": "robot-support-eval/v1",
        "suite_id": "robot-support-v1",
        "cases_file": "cases.jsonl",
        "split_file": "split.json",
        "counts": {"development": 1, "holdout": 1},
        "source_snapshot": {
            "generated_at": "2026-11-13",
            "runtime/robot_vacuum_support/state.py": state_hash,
            "spec_version": "3",
        },
        "review_status": "pending_human",
    }
    (base / "suite.json").write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
    (base / "cases.jsonl").write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )
    split = {
        "development": ["dev-1"],
        "holdout": ["hold-1"],
        "families": {"family-dev-1": ["dev-1"], "family-hold-1": ["hold-1"]},
    }
    (base / "split.json").write_text(json.dumps(split, ensure_ascii=False), encoding="utf-8")
    audit = {
        "old16": {
            "path": "eval/cases/v0/recharge-start.json",
            "sha256": _sha(ROOT / "eval" / "cases" / "v0" / "recharge-start.json"),
            "case_count": 16,
        }
    }
    (base / "audit.json").write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")
    return base


def test_validate_dataset_accepts_minimal_contract(tmp_path):
    report = runner.validate_dataset(_write_dataset(tmp_path))
    assert report["ok"], report["errors"]
    assert report["counts"] == {"development": 1, "holdout": 1}
    assert len(report["freeze"]["dataset_hash"]) == 64


def test_validate_dataset_detects_split_mismatch(tmp_path):
    base = _write_dataset(tmp_path)
    split = json.loads((base / "split.json").read_text(encoding="utf-8"))
    split["development"] = ["dev-1", "hold-1"]
    split["holdout"] = []
    (base / "split.json").write_text(json.dumps(split, ensure_ascii=False), encoding="utf-8")
    report = runner.validate_dataset(base)
    assert not report["ok"]
    assert any("split.json" in item for item in report["errors"])


def test_validate_dataset_detects_evidence_quote_and_hash_mismatch(tmp_path):
    base = _write_dataset(tmp_path)
    lines = (base / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    case = json.loads(lines[0])
    case["evidence"][0]["quote"] = "这句话不存在于 README"
    case["evidence"][0]["sha256"] = "0" * 64
    lines[0] = json.dumps(case, ensure_ascii=False)
    (base / "cases.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = runner.validate_dataset(base)
    assert not report["ok"]
    assert any("sha256 mismatch" in item for item in report["errors"])
    assert any("quote not found" in item for item in report["errors"])


def test_state_mode_fault_is_unsupported_not_recovery_pass(tmp_path):
    base = _write_dataset(tmp_path)
    data = runner.load_dataset(base)
    case = json.loads((base / "cases.jsonl").read_text(encoding="utf-8").splitlines()[0])
    case["id"] = "fault-1"
    case["setup"] = {"kind": "fault_injection", "fault": "process_restart", "steps": []}
    row = runner.run_state_case(case)
    assert row["status"] == "unsupported"
    assert row["bucket"] == "fault"
    assert row["checks"][0]["status"] == "unsupported"


def test_state_runner_uses_existing_pure_state(tmp_path, monkeypatch):
    base = _write_dataset(tmp_path)
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")
    run_dir = runner.run_state(data_dir=base, split="development")
    payload = runner.summary_run(run_dir)
    row = json.loads((run_dir / "cases.jsonl").read_text(encoding="utf-8").strip())
    assert row["case_id"] == "dev-1"
    assert row["mode"] == "state"
    assert any(check["name"] == "state.case_status" and check["status"] == "pass" for check in row["checks"])
    assert row["status"] == "passed"
    assert payload["run"]["dataset"]["dataset_hash"]
    assert payload["summary"]["counts"]["qa_total"] == 1


def test_state_runner_keeps_holdout_out_unless_explicit(tmp_path, monkeypatch):
    base = _write_dataset(tmp_path)
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")
    run_dir = runner.run_state(data_dir=base, split="development")
    row = json.loads((run_dir / "cases.jsonl").read_text(encoding="utf-8").strip())
    assert row["split"] == "development"


def test_citation_mapping_is_not_semantic_support():
    retrieval = {
        "status": "ok",
        "attempts": 1,
        "results": [{"source": "故障排除.txt", "snippet": "清理触点"}],
        "records": [{"tool": "rag_retrieve", "status": "ok"}],
    }
    checks = scorers.score_citations(
        "先清理触点。[故障排除.txt]",
        retrieval,
        expected_sources=["故障排除.txt"],
    )
    by_name = {check["name"]: check for check in checks}
    assert by_name["citation.present"]["status"] == "pass"
    assert by_name["citation.filename_mapping"]["status"] == "pass"
    assert by_name["expected_sources.retrieved"]["status"] == "pass"
    assert "not semantic support" in by_name["expected_sources.retrieved"]["detail"]


def test_unmapped_citation_fails_without_claiming_semantics():
    retrieval = {"status": "ok", "attempts": 1, "results": [{"source": "维护保养.txt"}]}
    checks = scorers.score_citations("先清理触点。[故障排除.txt]", retrieval)
    by_name = {check["name"]: check for check in checks}
    assert by_name["citation.filename_mapping"]["status"] == "fail"
    assert "unmapped" in by_name["citation.filename_mapping"]["actual"]


def test_expected_source_full_path_matches_retrieval_basename():
    retrieval = {
        "status": "ok",
        "attempts": 1,
        "results": [{"source": "故障排除.txt", "snippet": "清理触点"}],
        "records": [{"tool": "rag_retrieve", "status": "ok"}],
    }
    checks = scorers.score_citations(
        "先清理触点。[故障排除.txt]",
        retrieval,
        expected_sources=["assets/knowledge/故障排除.txt"],
    )
    by_name = {check["name"]: check for check in checks}
    assert by_name["expected_sources.retrieved"]["status"] == "pass"
    assert by_name["expected_sources.cited"]["status"] == "pass"


def test_expected_source_coverage_allows_partial_candidate_set():
    retrieval = {
        "status": "ok",
        "attempts": 1,
        "results": [{"source": "故障排除.txt", "snippet": "清理触点"}],
        "records": [{"tool": "rag_retrieve", "status": "ok"}],
    }
    checks = scorers.score_citations(
        "先清理触点。[故障排除.txt]",
        retrieval,
        expected_sources=["assets/knowledge/故障排除.txt", "assets/knowledge/维护保养.txt"],
    )
    by_name = {check["name"]: check for check in checks}
    assert by_name["expected_sources.retrieved"]["status"] == "pass"
    assert by_name["expected_sources.retrieved"]["actual"]["coverage"] == "1/2"
    assert by_name["expected_sources.cited"]["status"] == "pass"


def test_scope_alias_recharge_failure_maps_to_observed_recharge():
    view = {
        "active_case": {"status": "active", "topic": "recharge_failure"},
        "cases": [{"case_id": "c1"}],
    }
    checks = scorers.score_state_subset(
        {"scope": "recharge_failure", "case_status": "active"},
        view,
        scope="recharge",
    )
    by_name = {check["name"]: check for check in checks}
    assert by_name["state.scope"]["status"] == "pass"
    assert by_name["state.case_status"]["status"] == "pass"


def test_non_recharge_state_is_not_a_product_failure():
    view = {"active_case": None, "cases": []}
    checks = scorers.score_state_subset(
        {"scope": "suction", "case_status": "active", "not_tried_actions": ["clean_filter"]},
        view,
        scope="other",
    )
    assert {check["status"] for check in checks} <= {"not_applicable"}
    assert all(check["status"] != "pass" for check in checks)


def test_unknown_action_is_unsupported_not_pass_or_product_defect():
    view = {"active_case": {"status": "active"}, "cases": [{"case_id": "c1"}]}
    checks = scorers.score_state_subset(
        {"scope": "recharge", "tried_actions": [{"action_id": "clean_filter", "outcome": "failed"}]},
        view,
        scope="recharge",
    )
    by_name = {check["name"]: check for check in checks}
    assert by_name["state.tried_actions"]["status"] == "unsupported"


def test_semantic_claims_are_pending_not_passed():
    turns = [
        {
            "role": "user",
            "content": "回充失败",
            "expected": {
                "required_claims": ["说明资料不足"],
                "forbidden_claims": ["编造价格"],
                "required_behaviors": ["追问现象"],
                "expected_sources": ["故障排除.txt"],
                "state": {},
            },
        }
    ]
    items = scorers.semantic_review_items(turns, mode="live")
    assert {item["status"] for item in items} == {"pending_human"}
    checks = [{"name": "answer.nonempty", "dimension": "automatic", "status": "pass"}]
    assert scorers.overall_status(checks, items) == "pending_human"
    state_items = scorers.semantic_review_items(turns, mode="state")
    assert {item["status"] for item in state_items} == {"not_applicable"}


def test_live_missing_env_is_needs_configuration_not_mock(tmp_path, monkeypatch):
    base = _write_dataset(tmp_path)
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(runner, "_choose_env_file", lambda: None)
    run_dir = runner.run_live(data_dir=base, split="development")
    payload = runner.summary_run(run_dir)
    assert payload["run"]["status"] == "needs_configuration"
    assert payload["run"]["needs_configuration"]
    assert payload["run"]["live"]["source_rebuild"] is False


def test_inspect_task_smoke_through_real_framework(tmp_path):
    from eval import inspect_tasks

    base = _write_dataset(tmp_path)
    data = runner.load_dataset(base)
    cases = [case for case in data.cases if case["split"] == "development"]
    logs = inspect_tasks.run_inspect_state_smoke(cases, log_dir=tmp_path / "inspect-logs")
    assert logs
    log = logs[0]
    assert log.status == "success"
    assert log.samples
    score = next(iter(log.samples[0].scores.values()))
    assert score.metadata["status"] in {"passed", "pending_human", "failed", "not_applicable"}
