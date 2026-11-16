# -*- coding: utf-8 -*-
"""Offline regression for the isolated two-dialogue answer-review slice.

No product model and no Judge model are called.  Node is used only for the
existing evidence-bundle contract validator and the new generic recorded-run
adapter.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "action-state-answer-review-v1"
RUN_ID = "asc-live-development-20261114-cdf5aac89d"
DSH_JUDGE = ROOT.parent / "dsh-judge"
NODE = "node"
SMOKE = ROOT / "outputs" / "action-state-contract-smoke-20261114-3"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_review_server():
    spec = importlib.util.spec_from_file_location("review_server_test", ROOT / "eval" / "review_server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fixture_data():
    return {
        "cases": read_jsonl(OUT / "dataset" / "cases.jsonl"),
        "results": read_jsonl(OUT / "results" / RUN_ID / "cases.jsonl"),
        "run": read_json(OUT / "results" / RUN_ID / "run.json"),
        "freeze": read_json(OUT / "freeze.json"),
        "manifest": read_json(OUT / "evidence-manifest.json"),
        "program_checks": read_json(OUT / "program-checks.json"),
    }


def test_two_development_cases_and_six_real_turns(fixture_data):
    cases = fixture_data["cases"]
    results = fixture_data["results"]
    assert [case["id"] for case in cases] == ["asc-dev-contact-correction-01", "asc-dev-dock-correction-01"]
    assert {case["split"] for case in cases} == {"development"}
    assert all(case["review"]["status"] == "pending_human" for case in cases)
    assert len(cases) == 2 and len(results) == 2
    for row in results:
        assert row["mode"] == "live" and row["status"] == "completed"
        assert [turn["index"] for turn in row["turns"]] == [1, 2, 3]
        assert all(turn["answer"].strip() for turn in row["turns"])
        assert all(turn["role"] == "user" for turn in row["turns"])
    assert fixture_data["run"]["run_id"] == RUN_ID
    assert fixture_data["run"]["split"] == "development"
    assert fixture_data["run"]["mode"] == "live"


def test_actual_answers_equal_transcripts_and_source_hashes(fixture_data):
    source_map = read_json(OUT / "source-map.json")
    for case, row in zip(fixture_data["cases"], fixture_data["results"]):
        transcript_path = SMOKE / "instance-storage" / "sessions" / row["session_id"] / "transcript.jsonl"
        transcript = [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assistants = [item for item in transcript if item["role"] == "assistant"]
        assert [item["content"] for item in assistants] == [turn["answer"] for turn in row["turns"]]
        assert sha256_file(transcript_path) == source_map["source_hashes"]["transcripts"][row["session_id"]]["sha256"]
        trace_path = SMOKE / "instance-storage" / "sessions" / row["session_id"] / "outputs" / "traces" / "trace_20261114.jsonl"
        assert sha256_file(trace_path) == source_map["source_hashes"]["traces"][row["session_id"]]["sha256"]


def test_case_hash_matches_review_server(fixture_data):
    review_server = load_review_server()
    for case in fixture_data["cases"]:
        expected = review_server.case_hash(case)
        assert fixture_data["freeze"]["case_hashes"][case["id"]] == expected
        bundle = read_json(OUT / "evidence_bundles" / f"{case['id']}.json")
        assert bundle["case_hash"] == expected


def test_bundle_contract_validation_and_role_coverage(fixture_data):
    review = subprocess.run(
        [NODE, str(DSH_JUDGE / "bin" / "dsh-judge.js"), "validate", "--bundle-dir", str(OUT / "evidence_bundles")],
        cwd=str(DSH_JUDGE), text=True, encoding="utf-8", capture_output=True, timeout=120,
    )
    assert review.returncode == 0, review.stdout + review.stderr
    for case in fixture_data["cases"]:
        bundle = read_json(OUT / "evidence_bundles" / f"{case['id']}.json")
        catalog = bundle["task"]["issue_catalog"]
        assert len(catalog) == 11
        assert {item["focus"] for item in catalog} == {"task_completion", "evidence_support"}
        assert {item["turn_scope"] for item in catalog} == {0, 1, 2, 3}
        assert all(item["evidence_item_ids"] for item in catalog)
        evidence_ids = {item["id"] for item in bundle["evidence"]}
        assert all(set(item["evidence_item_ids"]) <= evidence_ids for item in catalog)
        assert [turn["index"] for turn in bundle["conversation"]["actual_turns"]] == [1, 2, 3]
        assert all(turn["answer"].strip() for turn in bundle["conversation"]["actual_turns"])
        assert bundle["conversation"]["fixture_premises"] == []
        assert len(bundle["task"]["checks"]) == 13
        assert all(item["status"] == "pass" for item in bundle["task"]["checks"])
        assert "human_notes" in bundle and bundle["human_notes"] == []
        assert bundle["task"]["meta_state_checks"]
        assert bundle["input_hash"] == fixture_data["freeze"]["evidence"][case["id"]]["input_hash"]


def test_turn_level_trace_isolation_and_full_tool_outputs():
    for case in read_jsonl(OUT / "dataset" / "cases.jsonl"):
        row = next(item for item in read_jsonl(OUT / "results" / RUN_ID / "cases.jsonl") if item["case_id"] == case["id"])
        raw_events = []
        trace_path = SMOKE / "instance-storage" / "sessions" / row["session_id"] / "outputs" / "traces" / "trace_20261114.jsonl"
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                raw_events.append(json.loads(line))
        raw_outputs = {event.get("output") for event in raw_events if event.get("event") == "tool_end"}
        bundle = read_json(OUT / "evidence_bundles" / f"{case['id']}.json")
        trace_ids = [turn["trace_id"] for turn in row["turns"] if turn["trace_id"]]
        assert trace_ids == [turn["trace_id"] for turn in bundle["conversation"]["actual_turns"] if turn["trace_id"]]
        assert len(trace_ids) == len(set(trace_ids)) == 2
        assert row["turns"][2]["trace_id"] is None
        for turn in bundle["conversation"]["actual_turns"]:
            if turn["trace"] is None:
                continue
            assert all(event.get("trace_id") in (None, turn["trace_id"]) for event in turn["trace"]["events"])
            for record in turn["retrieval"]["current"]:
                assert record["output"] in raw_outputs
                assert "output_preview" not in record
        retrieval_items = [item for item in bundle["evidence"] if item["kind"].startswith("retrieval")]
        assert retrieval_items
        assert all(item["text"] in raw_outputs for item in retrieval_items)


def test_generic_adapter_is_deterministic_in_temp_workspace(tmp_path):
    tmp_root = tmp_path / "robot-vacuum-support"
    ws = tmp_root / "outputs" / "action-state-answer-review-v1"
    (tmp_root / "docs").mkdir(parents=True)
    shutil.copy2(ROOT / "docs" / "action-state-contract.md", tmp_root / "docs" / "action-state-contract.md")
    for name in ("dataset", "results"):
        shutil.copytree(OUT / name, ws / name)
    shutil.copy2(OUT / "program-checks.json", ws / "program-checks.json")
    first = subprocess.run(
        [NODE, str(DSH_JUDGE / "scripts" / "build-recorded-bundles.js"), "--workspace", str(ws), "--out", str(ws / "bundles-a")],
        cwd=str(DSH_JUDGE), text=True, encoding="utf-8", capture_output=True, timeout=120,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    second = subprocess.run(
        [NODE, str(DSH_JUDGE / "scripts" / "build-recorded-bundles.js"), "--workspace", str(ws), "--out", str(ws / "bundles-b")],
        cwd=str(DSH_JUDGE), text=True, encoding="utf-8", capture_output=True, timeout=120,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    for case_id in ("asc-dev-contact-correction-01", "asc-dev-dock-correction-01"):
        a = read_json(ws / "bundles-a" / f"{case_id}.json")
        b = read_json(ws / "bundles-b" / f"{case_id}.json")
        assert a["case_hash"] == b["case_hash"]
        assert a["input_hash"] == b["input_hash"]
        assert [turn["answer"] for turn in a["conversation"]["actual_turns"]] == [turn["answer"] for turn in b["conversation"]["actual_turns"]]


def test_adapter_rejects_missing_actual_answer(tmp_path):
    tmp_root = tmp_path / "robot-vacuum-support"
    ws = tmp_root / "outputs" / "action-state-answer-review-v1"
    (tmp_root / "docs").mkdir(parents=True)
    shutil.copy2(ROOT / "docs" / "action-state-contract.md", tmp_root / "docs" / "action-state-contract.md")
    for name in ("dataset", "results"):
        shutil.copytree(OUT / name, ws / name)
    shutil.copy2(OUT / "program-checks.json", ws / "program-checks.json")
    result_path = ws / "results" / RUN_ID / "cases.jsonl"
    rows = read_jsonl(result_path)
    rows[0]["turns"][0]["answer"] = ""
    result_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    bad = subprocess.run(
        [NODE, str(DSH_JUDGE / "scripts" / "build-recorded-bundles.js"), "--workspace", str(ws), "--out", str(ws / "bundles")],
        cwd=str(DSH_JUDGE), text=True, encoding="utf-8", capture_output=True, timeout=120,
    )
    assert bad.returncode != 0
    assert "no actual answer" in (bad.stdout + bad.stderr)


def test_frozen_v1_and_reviews_unchanged(fixture_data):
    freeze = fixture_data["freeze"]
    dataset_dir = ROOT / "eval" / "datasets" / "v1"
    assert sha256_file(dataset_dir / "cases.jsonl") == freeze["frozen_v1"]["hashes"]["cases.jsonl"]
    assert sha256_file(dataset_dir / "suite.json") == freeze["frozen_v1"]["hashes"]["suite.json"]
    assert sha256_file(dataset_dir / "split.json") == freeze["frozen_v1"]["hashes"]["split.json"]
    assert read_json(dataset_dir / "suite.json")["source_snapshot"]["holdout_cases_sha256"] == freeze["frozen_v1"]["hashes"]["holdout_cases_sha256"]
    reviews_dir = ROOT / "eval" / "reviews"
    parts = []
    for path in sorted(reviews_dir.rglob("*.json")):
        parts.append(f"{path.relative_to(reviews_dir).as_posix()}:{sha256_file(path)}")
    aggregate = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest() if parts else None
    assert aggregate == freeze["frozen_v1"]["reviews_aggregate"]
    assert freeze["judge"]["not_invoked_yet"] is True


def test_freeze_has_13_original_checks_and_no_seeded_human_review(fixture_data, tmp_path):
    checks = fixture_data["program_checks"]["checks"]
    assert len(checks) == 13
    assert all(value is True for value in checks.values())

    # ``reviews_workspace_empty`` is the original freeze-time declaration, not
    # a live requirement that the user's review workspace stays empty forever.
    assert fixture_data["freeze"]["reviews_workspace_empty"] is True

    # Distinguish store initialization metadata from actual human verdicts:
    # ``reviews/store.json`` (and ``exposures.json``) are metadata; verdicts are
    # written as ``reviews/reviews/<reviewId>.json``.  Do not assert on the live
    # record count once the user has started reviewing.
    live_reviews = OUT / "reviews"
    assert {path.name for path in live_reviews.glob("*.json")} <= {"store.json", "exposures.json"}

    # A freshly created store must only initialize metadata; it never seeds a
    # human judgement.  Use an isolated store so this is independent of the
    # current state of the production review directory.
    review_server = load_review_server()
    store = review_server.ReviewStore(tmp_path / "review-store")
    meta = read_json(store.meta_path)
    assert meta["schema_version"] == review_server.STORE_SCHEMA
    assert "reviewId" not in meta
    assert store.reviews_dir.is_dir()
    assert list(store.reviews_dir.glob("*.json")) == []
    assert store.counts()["reviewCount"] == 0
    assert not store.exposures_path.exists()

    assert fixture_data["manifest"]["no_model_call"] is True
    assert fixture_data["manifest"]["no_hardcoded_case_whitelist"] is True
