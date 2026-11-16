# -*- coding: utf-8 -*-
"""Focused tests for the four-session diagnostic-scope after-review builder.

No Judge, no product model, no service.  The tests use temporary recorded
fixtures for the 1/2-turn generic path and the actual after workspace when it
has been frozen from the completed source run.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AFTER_SCRIPT = ROOT / "scripts" / "build_diagnostic_scope_after_review.py"
BASE_SCRIPT = ROOT / "scripts" / "build_diagnostic_scope_review.py"
WORKSPACE = ROOT / "outputs" / "diagnostic-scope-after-review-v2"
V1 = ROOT / "outputs" / "action-state-answer-review-v1"
BASELINE_V2 = ROOT / "outputs" / "diagnostic-scope-review-v2"
REPORT = ROOT / "outputs" / "diagnostic-scope-after-review-report.json"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


after = load_module("build_diagnostic_scope_after_review", AFTER_SCRIPT)
base = load_module("build_diagnostic_scope_review_for_after_test", BASE_SCRIPT)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_turn(index: int, content: str, answer: str, state: dict) -> dict:
    semantic = {"method": "fixture"}
    observations = [{"capability": "automatic_return", "result": "fails"}]
    diagnostic_scope = {"task_goal": "automatic_return", "resolved": False}
    snapshot = {
        "state": state,
        "semantic": semantic,
        "observations": observations,
        "diagnostic_scope": diagnostic_scope,
    }
    return {
        "index": index,
        "role": "user",
        "content": content,
        "answer": answer,
        "state": state,
        "semantic": semantic,
        "observations": observations,
        "diagnostic_scope": diagnostic_scope,
        "snapshot": snapshot,
        "trace": {
            "trace_id": f"run-fixture-{index}",
            "run_ids": [f"run-fixture-{index}"],
            "events": [{"event": "model_start", "trace_id": f"run-fixture-{index}", "ts": "2026-11-14T21:00:00"}],
            "tool_outputs": [],
            "event_counts": {"model_start": 1},
            "source_kind": "fixture",
            "not_session_cumulative": True,
        },
        "retrieval": {
            "current": [{"output": f"retrieval-turn-{index}", "source_turn": index}],
            "history": [{"output": "retrieval-turn-1", "source_turn": 1}] if index > 1 else [],
        },
        "request_id": f"req-fixture-{index}",
    }


class AfterReviewBuilderTests(unittest.TestCase):
    def test_four_fixed_sessions_and_turn_counts(self):
        self.assertEqual(
            [spec["id"] for spec in after.AFTER_SPECS],
            [
                "ds-live-manual-works-auto-fails-01",
                "ds-live-clearance-manual-only-01",
                "ds-live-manual-and-auto-fail-01",
                "ds-live-auto-recovered-01",
            ],
        )
        self.assertEqual([spec["turns"] for spec in after.AFTER_SPECS], [1, 1, 1, 2])
        self.assertEqual(sum(spec["turns"] for spec in after.AFTER_SPECS), 5)
        self.assertEqual(after.expected_summary()["turns"], 5)

    def test_criteria_are_generated_for_actual_turns_only(self):
        for spec in after.AFTER_SPECS:
            criteria = after.criteria_for_after_case(spec)
            self.assertEqual(len(criteria), 5 * spec["turns"])
            self.assertEqual(sorted({row["turn_scope"] for row in criteria}), list(range(1, spec["turns"] + 1)))
            self.assertFalse(any(row["turn_scope"] == 0 for row in criteria))
            for turn in range(1, spec["turns"] + 1):
                self.assertEqual(
                    {row["dimension"] for row in criteria if row["turn_scope"] == turn},
                    {
                        "observation_goal_scope",
                        "causal_exclusivity",
                        "condition_vs_fact",
                        "discriminative_observation",
                        "local_to_global_closure",
                    },
                )
        # The conditional-overreach limitation is explicit in the after rubric.
        auto = after.criteria_for_after_case(after.AFTER_SPECS[-1])
        self.assertTrue(any("conditional" in row["text"].lower() and "overreach" in row["text"].lower() for row in auto))
        self.assertTrue(any("explicit automated" in row["text"].lower() or "autonomously" in row["text"].lower() for row in auto))

    def test_generic_builder_supports_one_and_two_turn_cases(self):
        for spec in (after.AFTER_SPECS[0], after.AFTER_SPECS[-1]):
            turns = [
                fixture_turn(i, spec["questions"][i - 1], f"answer {i}", {"turn": i, "active": True})
                for i in range(1, spec["turns"] + 1)
            ]
            recorded = {
                "source": {"kind": "recorded_session_fixture", "session_id": f"s-{spec['id']}", "source_case_id": spec["id"]},
                "turns": turns,
            }
            case, result = base.build_from_recorded_case(
                recorded,
                new_case_id=spec["id"],
                family_id=spec["family_id"],
                title=spec["title"],
                spec=spec,
                criteria=after.criteria_for_after_case(spec),
                scoring=after.AFTER_SCORING,
            )
            self.assertEqual(len(case["turns"]), spec["turns"])
            self.assertEqual(len(case["answer_review_rubric"]), 5 * spec["turns"])
            self.assertIn("dataset.counts_4", case["scoring"]["automatic"])
            self.assertIn("dataset.turn_counts_1_1_1_2", case["scoring"]["automatic"])
            self.assertNotIn("dataset.counts_2", case["scoring"]["automatic"])
            self.assertEqual(len(result["turns"]), spec["turns"])
            for turn in result["turns"]:
                self.assertEqual(set(turn["snapshot"]), {"state", "semantic", "observations", "diagnostic_scope"})
            self.assertEqual(
                [turn["retrieval"]["current"][0]["output"] for turn in result["turns"]],
                [f"retrieval-turn-{i}" for i in range(1, spec["turns"] + 1)],
            )

    def test_baseline_three_turn_criteria_stay_byte_identical(self):
        baseline_freeze = load_json(BASELINE_V2 / "freeze.json")
        for spec in base.CASE_SPECS:
            self.assertEqual(base.criteria_for(spec), baseline_freeze["rubric"][spec["id"]])
            self.assertEqual(base.criteria_for_actual_turns(spec, 3), baseline_freeze["rubric"][spec["id"]])

    def test_baseline_three_turn_builder_keeps_default_scoring(self):
        spec = base.CASE_SPECS[0]
        turns = [
            fixture_turn(i, f"user {i}", f"answer {i}", {"turn": i})
            for i in range(1, 4)
        ]
        recorded = {"source": {"kind": "recorded_session_fixture", "session_id": "s-baseline"}, "turns": turns}
        case, _ = base.build_from_recorded_case(
            recorded,
            new_case_id="asc-dev-contact-correction-01",
            family_id=spec["family_id"],
            title="baseline compatibility",
            spec=spec,
        )
        self.assertEqual(len(case["answer_review_rubric"]), 15)
        self.assertIn("dataset.counts_2", case["scoring"]["automatic"])
        self.assertNotIn("dataset.counts_4", case["scoring"]["automatic"])

    def test_case_hash_is_stable_and_uses_review_server_algorithm(self):
        spec = after.AFTER_SPECS[-1]
        recorded = {
            "source": {"kind": "recorded_session_fixture", "session_id": "s-stable", "source_case_id": spec["id"]},
            "turns": [
                fixture_turn(i, spec["questions"][i - 1], f"answer {i}", {"turn": i})
                for i in range(1, spec["turns"] + 1)
            ],
        }
        kwargs = dict(
            new_case_id=spec["id"],
            family_id=spec["family_id"],
            title=spec["title"],
            spec=spec,
            criteria=after.criteria_for_after_case(spec),
            scoring=after.AFTER_SCORING,
        )
        case_a, _ = base.build_from_recorded_case(recorded, **kwargs)
        case_b, _ = base.build_from_recorded_case(recorded, **kwargs)
        self.assertEqual(base.case_hash(case_a), base.case_hash(case_b))
        payload = {key: value for key, value in case_a.items() if key != "review"}
        expected = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(base.case_hash(case_a), expected)

    def test_source_inspection_reports_missing_fields_without_inventing_cases(self):
        original = (after.SOURCE, after.SOURCE_RUN, after.INPUT_FREEZE)
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            run_path = tmp / "run.json"
            freeze_path = tmp / "input-freeze.json"
            run_path.write_text(json.dumps({
                "sessions": {
                    "ds-live-manual-works-auto-fails-01": {
                        "session_id": "s-temp",
                        "questions": ["q"],
                        "turns": [{"turn": 1, "answer": "a", "state": {}}],
                    }
                }
            }, ensure_ascii=False), encoding="utf-8")
            freeze_path.write_text(json.dumps({"expected_sessions": 4, "expected_turn_count": 5}, ensure_ascii=False), encoding="utf-8")
            after.SOURCE = tmp
            after.SOURCE_RUN = run_path
            after.INPUT_FREEZE = freeze_path
            try:
                inspection = after.inspect_source()
            finally:
                after.SOURCE, after.SOURCE_RUN, after.INPUT_FREEZE = original
        self.assertFalse(inspection["source_complete"])
        self.assertTrue(inspection["missing_fields"])
        self.assertIn("ds-live-manual-works-auto-fails-01", inspection["sessions"])
        self.assertIn("semantic", " ".join(inspection["missing_fields"]))
        self.assertIsNone(inspection["run_id"])  # no artifact -> no fabricated run id


@unittest.skipUnless((WORKSPACE / "freeze.json").exists(), "after freeze not built yet")
class FrozenAfterWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_jsonl(WORKSPACE / "dataset" / "cases.jsonl")
        cls.results = {row["case_id"]: row for row in load_jsonl(WORKSPACE / "results" / after.after_run_id("robot_vacuum-v4-6a805a8787") / "cases.jsonl")}
        cls.freeze = load_json(WORKSPACE / "freeze.json")
        cls.report = load_json(REPORT)

    def test_frozen_cases_have_actual_turn_counts(self):
        self.assertEqual([len(case["turns"]) for case in self.cases], [1, 1, 1, 2])
        self.assertEqual([len(case["answer_review_rubric"]) for case in self.cases], [5, 5, 5, 10])
        self.assertEqual(sum(len(case["turns"]) for case in self.cases), 5)
        self.assertFalse(any(case["id"].startswith("asc-") for case in self.cases))

    def test_case_hashes_match_freeze_and_bundles(self):
        for case in self.cases:
            self.assertEqual(base.case_hash(case), self.freeze["case_hashes"][case["id"]])
            bundle = load_json(WORKSPACE / "evidence_bundles" / f"{case['id']}.json")
            self.assertEqual(bundle["case_hash"], self.freeze["case_hashes"][case["id"]])
            self.assertEqual(bundle["input_hash"], self.freeze["evidence"][case["id"]]["input_hash"])

    def test_per_turn_snapshot_and_history_are_not_final_state_substitutes(self):
        auto = self.results["ds-live-auto-recovered-01"]
        self.assertEqual(len(auto["turns"]), 2)
        first, second = auto["turns"]
        self.assertEqual(set(first["snapshot"]), {"state", "semantic", "observations", "diagnostic_scope"})
        self.assertEqual(set(second["snapshot"]), {"state", "semantic", "observations", "diagnostic_scope"})
        self.assertNotEqual(first["snapshot"], second["snapshot"])
        self.assertNotEqual(first["trace"]["trace_id"], second["trace"]["trace_id"])
        self.assertTrue(second["retrieval"]["history"])
        self.assertTrue(all(row.get("source_turn") == 1 for row in second["retrieval"]["history"] if row.get("source_turn") is not None))

    def test_bundles_have_no_gold_leak(self):
        forbidden = (
            "candidate_needs_counterexample",
            "manager_concern",
            "judge_context",
            "criteria_judged_passed",
            "pos-explicit-automatic-recovery",
            "neg-conditional-overreach",
            "not_gold_for_independent_benchmark",
        )
        for case in self.cases:
            bundle = load_json(WORKSPACE / "evidence_bundles" / f"{case['id']}.json")
            raw = json.dumps(bundle, ensure_ascii=False)
            for needle in forbidden:
                self.assertNotIn(needle, raw)
            self.assertEqual(bundle.get("human_notes"), [])
            self.assertFalse(any(row.get("expected") for row in bundle["task"].get("expected", [])))
            self.assertEqual(len(bundle["task"]["issue_catalog"]), len(case["answer_review_rubric"]))
            self.assertTrue(all(row["evidence_item_ids"] for row in bundle["task"]["issue_catalog"]))

    def test_freeze_is_pre_judge_and_source_hashes_match(self):
        self.assertEqual(self.freeze["status"], "frozen_before_judge")
        self.assertTrue(self.freeze["judge"]["not_invoked_yet"])
        self.assertEqual(self.freeze["source_hashes"]["input_freeze_sha256"], sha256_file(ROOT / "outputs" / "diagnostic-scope-smoke-v1" / "input-freeze.json"))
        self.assertEqual(self.freeze["source_hashes"]["run_json_sha256"], sha256_file(ROOT / "outputs" / "diagnostic-scope-smoke-v1" / "run.json"))
        self.assertTrue(all(self.freeze["program_checks"]["checks"].values()))
        self.assertFalse(any((WORKSPACE / "judge_results").glob("*.json")))

    def test_report_is_prepared_waiting_judge_not_a_result(self):
        self.assertEqual(self.report["status"], "prepared_frozen_waiting_judge")
        self.assertFalse(self.report["judge_invoked"])
        self.assertFalse(self.report["product_model_called"])
        self.assertFalse(self.report["service_started"])
        self.assertIn("jr-diagnostic-after-20261114-01", self.report["next_command"])
        self.assertIsNone(self.report.get("verdict"))

    def test_baseline_v1_frozen_hashes_untouched(self):
        v1_freeze = load_json(V1 / "freeze.json")
        self.assertEqual(sha256_file(V1 / "dataset" / "cases.jsonl"), v1_freeze["source_hashes"]["dataset_cases_sha256"])
        self.assertEqual(sha256_file(V1 / "results" / "asc-live-development-20261114-cdf5aac89d" / "cases.jsonl"), v1_freeze["source_hashes"]["result_cases_sha256"])
        for case_id, digest in v1_freeze["case_hashes"].items():
            case = next(row for row in load_jsonl(V1 / "dataset" / "cases.jsonl") if row["id"] == case_id)
            self.assertEqual(base.case_hash(case), digest)


def write_cli_fixture(source_root: Path) -> None:
    source_root.mkdir(parents=True, exist_ok=True)
    sessions = {}
    for index, spec in enumerate(after.AFTER_SPECS, 1):
        sid = f"s-cli-{index}"
        session_dir = source_root / "instance-storage" / "sessions" / sid
        (session_dir / "outputs" / "traces").mkdir(parents=True, exist_ok=True)
        transcript = []
        turns = []
        for turn_index, question in enumerate(spec["questions"], 1):
            answer = f"fixture answer {index}-{turn_index}"
            at = 1794660000 + index * 100 + turn_index
            transcript.append({"role": "user", "content": question, "at": at, "message_id": f"m-u-{index}-{turn_index}", "turn": turn_index})
            transcript.append({"role": "assistant", "content": answer, "at": at + 10, "message_id": f"m-a-{index}-{turn_index}", "turn": turn_index})
            state = {"turn": turn_index, "active": True}
            semantic = {"method": "fixture"}
            observations = [{"capability": "automatic_return", "result": "fails"}]
            diagnostic_scope = {"task_goal": "automatic_return", "resolved": False}
            turns.append({
                "turn": turn_index,
                "request_id": f"req-{index}-{turn_index}",
                "http_status": 200,
                "answer": answer,
                "state": state,
                "semantic": semantic,
                "observations": observations,
                "diagnostic_scope": diagnostic_scope,
                "snapshot": {"state": state, "semantic": semantic, "observations": observations, "diagnostic_scope": diagnostic_scope},
                "trace": {
                    "trace_id": f"run-cli-{index}-{turn_index}",
                    "run_ids": [f"run-cli-{index}-{turn_index}"],
                    "events": [{
                        "event": "model_start",
                        "trace_id": f"run-cli-{index}-{turn_index}",
                        "run_id": f"run-cli-{index}-{turn_index}",
                        "ts": "2026-11-14T22:00:00",
                    }],
                    "tool_outputs": [],
                },
            })
        (session_dir / "transcript.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in transcript) + "\n", encoding="utf-8")
        (session_dir / "outputs" / "traces" / "trace_20261114.jsonl").write_text("{}\n", encoding="utf-8")
        sessions[spec["id"]] = {"session_id": sid, "questions": spec["questions"], "turns": turns}
    run = {
        "schema_version": "diagnostic-scope-smoke/v2-fixture",
        "run_id": "fixture-v2",
        "started_at": "2026-11-14T22:00:00",
        "artifact": {"artifact_id": "robot_vacuum-v4-test123456"},
        "sessions": sessions,
    }
    freeze = {
        "schema_version": "diagnostic-scope-smoke-freeze/v1",
        "frozen_at": "2026-11-14T21:59:00",
        "artifact": {"artifact_id": "robot_vacuum-v4-test123456"},
        "expected_sessions": 4,
        "expected_turn_count": 5,
        "cases": {spec["id"]: {"questions": spec["questions"]} for spec in after.AFTER_SPECS},
    }
    (source_root / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (source_root / "input-freeze.json").write_text(json.dumps(freeze, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@unittest.skipUnless((ROOT.parent / "dsh-judge" / "bin" / "dsh-judge.js").exists(), "dsh-judge CLI not available")
class AfterCliIntegrationTests(unittest.TestCase):
    def test_cli_builds_1_1_1_2_and_reuses_identical_freeze(self):
        token = uuid.uuid4().hex[:8]
        outputs = ROOT / "outputs"
        source = outputs / f"_after_cli_src_{token}"
        out = outputs / f"_after_cli_out_{token}"
        report = outputs / f"_after_cli_report_{token}.json"
        try:
            write_cli_fixture(source)
            cmd = [
                sys.executable,
                str(AFTER_SCRIPT),
                "--source-root", str(source),
                "--out", str(out),
                "--report", str(report),
                "--judge-run-id", "jr-diagnostic-after-test-01",
            ]
            first = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(first.returncode, 0, first.stderr)
            freeze_path = out / "freeze.json"
            freeze_bytes = freeze_path.read_bytes()
            cases = load_jsonl(out / "dataset" / "cases.jsonl")
            self.assertEqual([len(case["turns"]) for case in cases], [1, 1, 1, 2])
            self.assertEqual([len(case["answer_review_rubric"]) for case in cases], [5, 5, 5, 10])
            self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["status"], "prepared_frozen_waiting_judge")
            self.assertEqual(json.loads(freeze_bytes)["judge"]["not_invoked_yet"], True)
            self.assertEqual(json.loads(freeze_bytes)["judge_run_id"], "jr-diagnostic-after-test-01")

            second = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(freeze_path.read_bytes(), freeze_bytes)
            self.assertIn('"reused_existing_freeze": true', second.stdout)

            tampered = json.loads((source / "run.json").read_text(encoding="utf-8"))
            tampered["tampered"] = True
            (source / "run.json").write_text(json.dumps(tampered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            mismatch = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertIn("new --out", mismatch.stderr)
            self.assertEqual(freeze_path.read_bytes(), freeze_bytes)
        finally:
            shutil.rmtree(source, ignore_errors=True)
            shutil.rmtree(out, ignore_errors=True)
            if report.exists():
                report.unlink()


if __name__ == "__main__":
    unittest.main()
