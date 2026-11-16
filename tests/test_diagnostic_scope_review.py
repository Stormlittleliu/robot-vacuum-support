# -*- coding: utf-8 -*-
"""Focused tests for the isolated diagnostic-scope-review v2 workspace.

Deliberately small: no product model, no full-suite run, no holdout read.
The build script is imported directly so the tests can exercise the generic
``build_from_recorded_case`` entry point with temporary recorded data.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_diagnostic_scope_review.py"
WORKSPACE = ROOT / "outputs" / "diagnostic-scope-review-v2"
V1 = ROOT / "outputs" / "action-state-answer-review-v1"

spec = importlib.util.spec_from_file_location("build_diagnostic_scope_review", SCRIPT)
build = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(build)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DiagnosticScopeReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_jsonl(WORKSPACE / "dataset" / "cases.jsonl")
        cls.results = load_jsonl(WORKSPACE / "results" / build.RUN_ID / "cases.jsonl")
        cls.freeze = load_json(WORKSPACE / "freeze.json")
        cls.criteria = load_json(WORKSPACE / "dataset" / "criteria.json")
        cls.catalog = load_json(WORKSPACE / "dataset" / "issue_catalog.json")
        cls.exact = load_json(WORKSPACE / "dataset" / "exact-quotes.json")
        cls.old_cases = load_jsonl(V1 / "dataset" / "cases.jsonl")
        cls.old_results = load_jsonl(V1 / "results" / "asc-live-development-20261114-cdf5aac89d" / "cases.jsonl")

    def test_build_from_recorded_case_preserves_text_answer_trace_state(self):
        recorded = {
            "source": {"kind": "recorded_session_fixture", "session_id": "s-temp", "source_case_id": "tmp"},
            "turns": [
                {
                    "index": 1,
                    "role": "user",
                    "content": "回充失败，我刚擦了触点，还没看结果。",
                    "answer": "先确认一下结果，不要先下结论。",
                    "state": {"case_status": "active", "case_tried": [["clean_contacts", "unknown"]]},
                    "trace": {"trace_id": "run-temp", "run_ids": ["run-temp"], "events": [{"event": "model_call", "trace_id": "run-temp"}]},
                    "retrieval": {"current": [{"output": "原始资料"}], "history": []},
                }
            ],
        }
        spec_row = build.CASE_SPECS[0]
        case, result = build.build_from_recorded_case(
            recorded,
            new_case_id="ds-temp-causal-01",
            family_id="fam-ds-temp",
            title="temp",
            spec=spec_row,
        )
        self.assertEqual(case["turns"][0]["content"], recorded["turns"][0]["content"])
        self.assertNotIn("expected", case["turns"][0])
        self.assertEqual(result["turns"][0]["answer"], recorded["turns"][0]["answer"])
        self.assertEqual(result["turns"][0]["state"], recorded["turns"][0]["state"])
        self.assertEqual(result["turns"][0]["trace"], recorded["turns"][0]["trace"])
        self.assertEqual(result["turns"][0]["retrieval"], recorded["turns"][0]["retrieval"])

    def test_unknown_source_is_rejected(self):
        recorded = {
            "source": {"kind": "unknown-source-kind"},
            "turns": [{"role": "user", "content": "x", "answer": "y", "state": {}}],
        }
        with self.assertRaises(ValueError):
            build.build_from_recorded_case(
                recorded,
                new_case_id="ds-temp-causal-01",
                family_id="fam-ds-temp",
                title="temp",
                spec=build.CASE_SPECS[0],
            )

    def test_case_hash_matches_review_server_and_freeze(self):
        for case in self.cases:
            self.assertEqual(build.case_hash(case), self.freeze["case_hashes"][case["id"]])

    def test_freeze_exists_before_judge(self):
        self.assertEqual(self.freeze["status"], "frozen_before_judge")
        self.assertTrue(self.freeze["judge"]["not_invoked_yet"])
        self.assertEqual(self.freeze["rubric_version"], build.RUBRIC_VERSION)

    def test_source_hashes_match_files(self):
        expected = self.freeze["source_hashes"]
        self.assertEqual(sha256_file(V1 / "dataset" / "cases.jsonl"), expected["dataset_cases_sha256"])
        self.assertEqual(sha256_file(V1 / "results" / "asc-live-development-20261114-cdf5aac89d" / "cases.jsonl"), expected["result_cases_sha256"])
        self.assertEqual(sha256_file(V1 / "source-map.json"), expected["source_map_sha256"])
        self.assertEqual(sha256_file(WORKSPACE / "dataset" / "criteria.json"), self.freeze["rubric_sha256"])
        self.assertEqual(sha256_file(WORKSPACE / "dataset" / "issue_catalog.json"), self.freeze["issue_catalog_sha256"])
        for key, value in self.freeze["frozen_v1"]["hashes"].items():
            if key in ("cases.jsonl", "suite.json", "split.json"):
                self.assertEqual(sha256_file(ROOT / "eval" / "datasets" / "v1" / key), value)

    def test_original_v1_text_and_answers_unchanged(self):
        old_by_id = {row["id"]: row for row in self.old_cases}
        old_result_by_id = {row["case_id"]: row for row in self.old_results}
        for case, spec_row in zip(self.cases, build.CASE_SPECS):
            old = old_by_id[spec_row["source_id"]]
            self.assertEqual([turn["content"] for turn in case["turns"]], [turn["content"] for turn in old["turns"]])
            old_result = old_result_by_id[spec_row["source_id"]]
            new_result = next(row for row in self.results if row["case_id"] == case["id"])
            for old_turn, new_turn in zip(old_result["turns"], new_result["turns"]):
                self.assertEqual(old_turn["answer"], new_turn["answer"])
                self.assertEqual(old_turn["state"], new_turn["state"])
                self.assertEqual(old_turn["trace"], new_turn["trace"])
                self.assertEqual(old_turn["retrieval"], new_turn["retrieval"])

    def test_criteria_are_turn_scoped_and_catalog_complete(self):
        for case in self.cases:
            rubric = case["answer_review_rubric"]
            self.assertEqual(len(rubric), 15)
            self.assertEqual(sorted({item["turn_scope"] for item in rubric}), [1, 2, 3])
            self.assertFalse(any(item["turn_scope"] == 0 for item in rubric))
            for turn in (1, 2, 3):
                dimensions = {item["dimension"] for item in rubric if item["turn_scope"] == turn}
                self.assertEqual(dimensions, {
                    "observation_goal_scope",
                    "causal_exclusivity",
                    "condition_vs_fact",
                    "discriminative_observation",
                    "local_to_global_closure",
                })
            self.assertEqual(
                {item["criterion_id"] for item in rubric},
                {item["criterion_id"] for item in self.catalog["cases"][case["id"]]},
            )
            self.assertEqual(self.criteria["criteria"][case["id"]], rubric)

    def test_bundle_catalog_matches_case_rubric(self):
        for case in self.cases:
            bundle_path = WORKSPACE / "evidence_bundles" / f"{case['id']}.json"
            self.assertTrue(bundle_path.exists(), bundle_path)
            bundle = load_json(bundle_path)
            catalog = bundle["task"]["issue_catalog"]
            rubric = case["answer_review_rubric"]
            self.assertEqual(len(catalog), len(rubric))
            by_id = {row["criterion_id"]: row for row in catalog}
            for item in rubric:
                self.assertIn(item["criterion_id"], by_id)
                self.assertEqual(by_id[item["criterion_id"]]["focus"], item["focus"])
                self.assertEqual(by_id[item["criterion_id"]]["turn_scope"], item["turn_scope"])
                self.assertEqual(by_id[item["criterion_id"]]["text"], item["text"])
                self.assertTrue(by_id[item["criterion_id"]]["evidence_item_ids"])

    def test_bundle_has_no_prior_judge_manager_or_human_notes(self):
        forbidden = (
            "candidate_needs_counterexample",
            "manager_concern",
            "judge_context",
            "criteria_judged_passed",
            "jmc-s1-exclusive-causal-conclusion",
            "human_notes\": [",
        )
        for case in self.cases:
            bundle = load_json(WORKSPACE / "evidence_bundles" / f"{case['id']}.json")
            raw = json.dumps(bundle, ensure_ascii=False)
            for needle in forbidden:
                if needle == "human_notes\": [":
                    self.assertEqual(bundle.get("human_notes"), [])
                else:
                    self.assertNotIn(needle, raw)
            self.assertNotIn("manager_observations", bundle)
            self.assertNotIn("previous_judge", bundle)
            self.assertFalse(any(row.get("expected") for row in bundle["task"]["expected"]))
            self.assertEqual(bundle["human_notes"], [])

    def test_exact_quotes_are_verbatim(self):
        self.assertTrue(self.exact["all_verbatim"])
        old_by_id = {row["id"]: row for row in self.old_cases}
        old_result_by_id = {row["case_id"]: row for row in self.old_results}
        for row in self.exact["quotes"]:
            self.assertTrue(row["verbatim_in_source"], row["id"])
            if row["source_kind"] == "assistant_actual_answer":
                source = old_result_by_id[row["case_id"]]["turns"][row["turn"] - 1]["answer"]
            else:
                source = "\n".join(
                    str(record.get("output") or "")
                    for turn in old_result_by_id[row["case_id"]]["turns"]
                    for record in list((turn.get("retrieval") or {}).get("current") or []) + list((turn.get("retrieval") or {}).get("history") or [])
                )
            self.assertIn(row["quote"], source)

    def test_program_checks_pass_and_holdout_not_read(self):
        checks = load_json(WORKSPACE / "program-checks.json")
        self.assertTrue(checks["passed"])
        self.assertTrue(checks["checks"]["holdout_not_read"])
        self.assertTrue(checks["checks"]["exact_quotes_verbatim"])

    def test_reviews_directory_has_no_filled_human_review(self):
        store = load_json(WORKSPACE / "reviews" / "store.json")
        self.assertEqual(store.get("records"), [])


if __name__ == "__main__":
    unittest.main()
