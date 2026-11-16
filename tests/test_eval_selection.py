# -*- coding: utf-8 -*-
"""Selection, source-drift and usage tests for the approved development baseline.

These tests read the real frozen dataset/reviews only for the selection counts;
they never modify either.  Drift tests use explicit synthetic inputs where
possible and assert that no review/dataset bytes are rewritten.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval import runner, scorers  # noqa: E402
from eval import selection as selection_mod  # noqa: E402
from eval import source_drift  # noqa: E402


def test_real_selection_is_question_quality_filter_not_agent_score():
    manifest = selection_mod.build_selection()
    assert manifest["counts"]["dataset_development"] == 32
    assert manifest["counts"]["reviewed_case_definition"] == 32
    assert manifest["counts"]["included"] == 26
    assert len(manifest["qa_case_ids"]) == 24
    assert len(manifest["fault_case_ids"]) == 2
    assert manifest["counts"]["excluded_failed"] == 2
    assert manifest["counts"]["excluded_uncertain"] == 4
    assert manifest["selection_is_question_quality_filter_not_agent_score"] is True
    assert manifest["selection_rule"]["not_agent_performance_filter"] is True
    # All 32 development case definitions are still present and reviewed.
    rows = manifest["cases"]
    assert len(rows) == 32
    assert all(row["hash_match"] for row in rows)
    assert all(row["dataset_match"] for row in rows)
    excluded = {row["case_id"]: row for row in manifest["excluded_cases"]}
    assert excluded["rsv1-dev-multi-recharge-closure-01"]["review_status"] == "failed"
    assert excluded["rsv1-dev-negation-filter-not-cleaned-02"]["review_status"] == "failed"
    assert sum(1 for row in excluded.values() if row["review_status"] == "uncertain") == 4
    assert all(row["review_id"] and row["review_revision"] is not None for row in rows)


def test_selection_hash_is_deterministic():
    one = selection_mod.build_selection()
    two = selection_mod.build_selection()
    assert one["selection_sha256"] == two["selection_sha256"]
    assert one["included_case_ids"] == two["included_case_ids"]


def test_validate_manifest_fails_closed_on_case_hash_drift():
    manifest = selection_mod.build_selection()
    manifest["cases"][0]["case_hash"] = "0" * 64
    with pytest.raises(selection_mod.SelectionError) as exc:
        selection_mod.validate_manifest(manifest)
    assert "caseHash" in str(exc.value) or "case_hash" in str(exc.value)


def test_runner_resolves_both_review_status_and_manifest_paths():
    data = runner.load_dataset()
    selected, payload, manifest = runner._resolve_selection(
        data, split="development", review_status="passed", bucket="qa"
    )
    assert len(selected) == 24
    assert payload["frozen"] is True
    assert manifest is not None
    assert payload["selection_sha256"] == manifest["selection_sha256"]
    faults, fault_payload, _ = runner._resolve_selection(
        data,
        split="development",
        selection_manifest="outputs/approved-development-selection.json",
        bucket="fault",
    )
    assert len(faults) == 2
    assert fault_payload["source"] == "selection_manifest"
    assert all((case.get("setup") or {}).get("kind") == "fault_injection" for case in faults)


def test_stale_review_is_historical_and_does_not_enter_selection(tmp_path):
    # Minimal dataset with one development case and a deliberately stale review.
    base = tmp_path / "v1"
    base.mkdir()
    case = {
        "id": "dev-stale-1",
        "family_id": "family-stale",
        "category": "direct_qa",
        "split": "development",
        "origin": {"kind": "synthetic", "seed_ids": []},
        "review": {"status": "pending_human"},
        "turns": [],
        "evidence": [],
        "setup": {"kind": "normal", "steps": []},
        "scoring": {"automatic": [], "human": []},
    }
    (base / "cases.jsonl").write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
    (base / "suite.json").write_text(
        json.dumps({"suite_id": "robot-support-v1", "revision": "v1.1", "source_snapshot": {}}),
        encoding="utf-8",
    )
    (base / "split.json").write_text(
        json.dumps({"development": ["dev-stale-1"], "holdout": []}), encoding="utf-8"
    )
    (base / "audit.json").write_text(json.dumps({"old16": {}}), encoding="utf-8")
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    record = {
        "reviewId": "stale",
        "caseId": "dev-stale-1",
        "reviewScope": "case_definition",
        "status": "passed",
        "revision": 1,
        "updatedAt": "2026-11-13T00:00:00+08:00",
        "caseHash": "0" * 64,
        "suiteRevision": "v1.0",
        "datasetCasesSha256": "1" * 64,
        "notes": "keep original",
    }
    (reviews / "stale.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    manifest = selection_mod.build_selection(dataset_dir=base, review_dir=reviews)
    assert manifest["included_case_ids"] == []
    assert manifest["counts"]["excluded_stale"] == 1
    row = manifest["cases"][0]
    assert row["historical"] is True
    assert row["review_id"] == "stale"
    assert row["notes"] == "keep original"


def test_source_drift_waiver_is_scoped_to_requirement_quotes():
    audit = source_drift.build_source_drift_audit()
    assert audit["ok"] is True
    assert audit["waived"] is True
    assert audit["knowledge_drift"] == []
    assert [row["path"] for row in audit["requirement_drift"]] == ["README.md"]
    assert audit["quote_failures"] == []
    assert audit["schema_compatibility"]["compatible"] is True
    assert audit["waiver"]["scope"] == "requirement-only source hash drift"
    assert "knowledge file hash drift" in audit["waiver"]["not_waived"]


def test_quote_status_distinguishes_exact_and_missing():
    assert source_drift._quote_status("扫地 机器人", "前言 扫地 机器人 后记") == "exact_or_whitespace_normalised"
    assert source_drift._quote_status("扫地机器人", "前言 扫地 机器人 后记") == "whitespace_only_after_compaction"
    assert source_drift._quote_status("不存在的句子", "前言 扫地 机器人 后记") == "missing"


def test_turn_usage_keeps_requests_and_ledger_separate():
    usage = {
        "turn_metrics": {
            "requests": 2,
            "model_calls": 2,
            "calls": [
                {"layer": "interpretation", "usage": {"total_tokens": 10}},
                {"layer": "main_agent", "usage": {"total_tokens": 20}},
            ],
            "total": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
            "usage_status": "reported",
            "by_layer": {
                "interpretation": {"requests": 1, "model_calls": 1, "total_tokens": 10},
                "main_agent": {"requests": 1, "model_calls": 1, "total_tokens": 20},
            },
            "route": "semantic",
        }
    }
    row = runner._turn_usage(usage)
    assert row["model_requests"] == 2
    assert row["model_calls"] == 2
    assert row["ledger_events"] == 2
    assert row["total_tokens"] == 30
    aggregate = runner._aggregate_usage([{"index": 1, "usage": usage}])
    assert aggregate["model_requests"] == 2
    assert aggregate["ledger_events"] == 2
    assert aggregate["total_tokens"] == 30
    assert aggregate["cost"] is None
    assert aggregate["cost_status"] == "unavailable_no_authoritative_unit_price"


def test_missing_usage_is_null_not_estimated():
    aggregate = runner._aggregate_usage([{"index": 1, "usage": None}])
    assert aggregate["model_requests"] is None
    assert aggregate["total_tokens"] is None
    assert aggregate["usage_status"] == "unavailable"


def test_citation_fragment_mapping_is_separate_from_semantic_support():
    retrieval = {
        "status": "ok",
        "attempts": 1,
        "results": [{"source": "故障排除.txt", "snippet": "清理充电座触点", "content": "清理充电座触点"}],
        "records": [{"tool": "rag_retrieve", "status": "ok"}],
    }
    checks = scorers.score_citations("先清理触点。[故障排除.txt]", retrieval, expected_sources=["故障排除.txt"])
    by_name = {check["name"]: check for check in checks}
    assert by_name["citation.filename_mapping"]["status"] == "pass"
    assert by_name["citation.source_fragment"]["status"] == "pass"
    assert by_name["citation.source_fragment"]["actual"]["not_semantic_support"] is True


def test_citation_without_retrieved_fragment_is_not_a_semantic_pass():
    retrieval = {
        "status": "ok",
        "attempts": 1,
        "results": [{"source": "故障排除.txt", "snippet": "", "content": ""}],
        "records": [{"tool": "rag_retrieve", "status": "ok"}],
    }
    checks = scorers.score_citations("先清理触点。[故障排除.txt]", retrieval)
    by_name = {check["name"]: check for check in checks}
    assert by_name["citation.filename_mapping"]["status"] == "pass"
    assert by_name["citation.source_fragment"]["status"] == "fail"
