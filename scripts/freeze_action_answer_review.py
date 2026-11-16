# -*- coding: utf-8 -*-
"""Freeze the two answer-review cases, rubric, evidence bundles and hashes.

The freeze is written *before* any Judge call. It records the review-server
case hash, the generic bundle input hash, the source contract hash, the rubric,
the evidence-bundle hashes, the frozen program checks and the untouched v1
hashes. It never reads or writes Judge results.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "action-state-answer-review-v1"
RUN_ID = "asc-live-development-20261114-cdf5aac89d"
JUDGE_RUN_ID = "jr-action-review-20261114-01"
CONTRACT = ROOT / "docs" / "action-state-contract.md"
NODE = "node"
DSH_JUDGE = ROOT.parent / "dsh-judge"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def review_server_case_hash(case: dict[str, Any]) -> str:
    payload = {key: value for key, value in case.items() if key != "review"}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(raw)


def load_review_server_module():
    path = ROOT / "eval" / "review_server.py"
    spec = importlib.util.spec_from_file_location("review_server_for_freeze", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load review_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reviews_aggregate(reviews_dir: Path) -> str | None:
    if not reviews_dir.exists():
        return None
    parts = []
    for path in sorted(reviews_dir.rglob("*.json")):
        parts.append(f"{path.relative_to(reviews_dir).as_posix()}:{sha256_file(path)}")
    return sha256_text("\n".join(parts)) if parts else None


def main() -> None:
    dataset_dir = OUT / "dataset"
    result_dir = OUT / "results" / RUN_ID
    bundle_dir = OUT / "evidence_bundles"
    freeze_path = OUT / "freeze.json"
    judge_files = list((OUT / "judge_results").rglob("*.json")) if (OUT / "judge_results").exists() else []
    if freeze_path.exists() and any(path.is_file() for path in judge_files):
        raise SystemExit("freeze.json already exists with Judge output; refusing to rewrite a post-Judge freeze")
    # Re-freezing before the Judge call is allowed so schema fixes can be made
    # without ever mutating a frozen rubric after seeing Judge conclusions.
    cases = read_jsonl(dataset_dir / "cases.jsonl")
    results = read_jsonl(result_dir / "cases.jsonl")
    run = read_json(result_dir / "run.json")
    manifest = read_json(OUT / "evidence-manifest.json")
    program_checks = read_json(OUT / "program-checks.json")
    source_map = read_json(OUT / "source-map.json")
    if run.get("run_id") != RUN_ID:
        raise SystemExit(f"unexpected run id: {run.get('run_id')}")
    if len(cases) != 2 or len(results) != 2:
        raise SystemExit(f"expected 2 cases/results, got {len(cases)}/{len(results)}")

    # The new cases must not enter the frozen v1 32/16 split.
    v1_dir = ROOT / "eval" / "datasets" / "v1"
    v1_split = read_json(v1_dir / "split.json")
    v1_dev_ids = set(v1_split["development"]["case_ids"])
    new_ids = {case["id"] for case in cases}
    if new_ids & v1_dev_ids:
        raise SystemExit("new answer-review case id collides with frozen v1 development split")

    # Verify the authoritative Python review-server hash and the algorithm copy.
    review_server = load_review_server_module()
    for case in cases:
        expected = review_server_case_hash(case)
        authoritative = review_server.case_hash(case)
        if expected != authoritative:
            raise SystemExit(f"{case['id']}: local case_hash does not match review_server.case_hash")

    # Validate the bundles with the real Judge contract code, not a copy.
    validation = subprocess.run(
        [NODE, str(DSH_JUDGE / "bin" / "dsh-judge.js"), "validate", "--bundle-dir", str(bundle_dir)],
        cwd=str(DSH_JUDGE),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=120,
    )
    if validation.returncode != 0:
        raise SystemExit(f"bundle validation failed:\n{validation.stdout}\n{validation.stderr}")

    manifest_by_case = {item["case_id"]: item for item in manifest["cases"]}
    rubric = {}
    evidence = {}
    case_hashes = {}
    for case in cases:
        case_id = case["id"]
        item = manifest_by_case.get(case_id)
        if item is None:
            raise SystemExit(f"manifest missing {case_id}")
        bundle_path = bundle_dir / f"{case_id}.json"
        bundle = read_json(bundle_path)
        if bundle["case_hash"] != review_server_case_hash(case):
            raise SystemExit(f"{case_id}: bundle case_hash does not match review_server hash")
        if bundle["input_hash"] != item["input_hash"]:
            raise SystemExit(f"{case_id}: manifest/bundle input_hash mismatch")
        if bundle["case_id"] != case_id or bundle["run_id"] != RUN_ID:
            raise SystemExit(f"{case_id}: bundle identity mismatch")
        catalog = bundle["task"]["issue_catalog"]
        expected_rubric = case["answer_review_rubric"]
        if len(catalog) != len(expected_rubric):
            raise SystemExit(f"{case_id}: catalog/rubric count mismatch")
        by_id = {row["criterion_id"]: row for row in catalog}
        for row in expected_rubric:
            got = by_id.get(row["criterion_id"])
            if got is None:
                raise SystemExit(f"{case_id}: catalog missing {row['criterion_id']}")
            if (got["focus"], got["turn_scope"], got["text"]) != (row["focus"], row["turn_scope"], row["text"]):
                raise SystemExit(f"{case_id}: catalog mismatch for {row['criterion_id']}")
            if not got.get("evidence_item_ids"):
                raise SystemExit(f"{case_id}: criterion {row['criterion_id']} has no evidence refs")
        actual_turns = bundle["conversation"]["actual_turns"]
        if [turn["index"] for turn in actual_turns] != [1, 2, 3]:
            raise SystemExit(f"{case_id}: actual turn indexes are not exactly 1/2/3")
        if any(turn.get("fixture") for turn in actual_turns):
            raise SystemExit(f"{case_id}: fixture turn found in actual evidence")
        if any(not turn.get("answer") for turn in actual_turns):
            raise SystemExit(f"{case_id}: missing actual answer")
        trace_ids = [turn.get("trace_id") for turn in actual_turns if turn.get("trace_id")]
        if len(trace_ids) != len(set(trace_ids)):
            raise SystemExit(f"{case_id}: duplicate turn trace_id")
        if len(bundle["task"]["checks"]) != 13:
            raise SystemExit(f"{case_id}: expected 13 frozen program checks, got {len(bundle['task']['checks'])}")
        if any(check.get("status") != "pass" for check in bundle["task"]["checks"]):
            raise SystemExit(f"{case_id}: frozen program checks are not all pass")
        case_hashes[case_id] = bundle["case_hash"]
        rubric[case_id] = expected_rubric
        evidence[case_id] = {
            "bundle_file": f"evidence_bundles/{case_id}.json",
            "bundle_sha256": sha256_file(bundle_path),
            "input_hash": bundle["input_hash"],
            "evidence_count": len(bundle["evidence"]),
            "catalog_count": len(catalog),
            "actual_turns": len(actual_turns),
            "current_retrieval_items": len(bundle["retrieval"]["current"]),
            "history_retrieval_items": len(bundle["retrieval"]["history"]),
            "per_turn_current_retrieval": [len(turn["retrieval"]["current"]) for turn in actual_turns],
            "per_turn_history_retrieval": [len(turn["retrieval"]["history"]) for turn in actual_turns],
            "trace_ids": trace_ids,
        }

    # Recompute source hashes from the actual files and compare with the map.
    expected_source = source_map["source_hashes"]
    actual_source = {
        "contract": sha256_file(CONTRACT),
        "run_json": sha256_file(ROOT / "outputs/action-state-contract-smoke-20261114-3/run.json"),
        "transcripts": {
            session_id: sha256_file(ROOT / "outputs/action-state-contract-smoke-20261114-3/instance-storage/sessions" / session_id / "transcript.jsonl")
            for session_id in expected_source["transcripts"]
        },
        "traces": {
            session_id: sha256_file(ROOT / "outputs/action-state-contract-smoke-20261114-3/instance-storage/sessions" / session_id / "outputs/traces/trace_20261114.jsonl")
            for session_id in expected_source["traces"]
        },
    }
    if actual_source["contract"] != expected_source["contract"]["sha256"]:
        raise SystemExit("contract hash changed before freeze")
    if actual_source["run_json"] != expected_source["run_json"]["sha256"]:
        raise SystemExit("source run hash changed before freeze")
    for session_id, digest in actual_source["transcripts"].items():
        if digest != expected_source["transcripts"][session_id]["sha256"]:
            raise SystemExit(f"transcript hash changed: {session_id}")
    for session_id, digest in actual_source["traces"].items():
        if digest != expected_source["traces"][session_id]["sha256"]:
            raise SystemExit(f"trace hash changed: {session_id}")

    # The review store for this workspace must be empty before the user starts.
    review_files = list((OUT / "reviews").rglob("*")) if (OUT / "reviews").exists() else []
    if any(path.is_file() for path in review_files):
        raise SystemExit("reviews/ is not empty; refusing to freeze a seeded human review")

    # Frozen v1 hashes and review aggregate must still match the source map.
    frozen_hash_now = {
        "cases.jsonl": sha256_file(v1_dir / "cases.jsonl"),
        "suite.json": sha256_file(v1_dir / "suite.json"),
        "split.json": sha256_file(v1_dir / "split.json"),
        "holdout_cases_sha256": read_json(v1_dir / "suite.json").get("source_snapshot", {}).get("holdout_cases_sha256"),
    }
    if frozen_hash_now != source_map["frozen_v1_hashes"]:
        raise SystemExit("frozen v1 hashes changed; refusing to freeze")
    reviews_now = reviews_aggregate(ROOT / "eval" / "reviews")
    if reviews_now != source_map["reviews_aggregate_before"]:
        raise SystemExit("eval/reviews aggregate changed; refusing to freeze")

    freeze = {
        "schema_version": "action-state-answer-review-freeze/v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "frozen_before_judge",
        "run_id": RUN_ID,
        "judge_run_id": JUDGE_RUN_ID,
        "suite_id": "robot-support-action-answer-review-v1",
        "revision": "v1",
        "artifact": run["source"],
        "contract": {
            "path": "docs/action-state-contract.md",
            "sha256": actual_source["contract"],
        },
        "source_hashes": {
            "run_json_sha256": actual_source["run_json"],
            "transcripts": actual_source["transcripts"],
            "traces": actual_source["traces"],
            "dataset_cases_sha256": sha256_file(dataset_dir / "cases.jsonl"),
            "suite_sha256": sha256_file(dataset_dir / "suite.json"),
            "split_sha256": sha256_file(dataset_dir / "split.json"),
            "result_cases_sha256": sha256_file(result_dir / "cases.jsonl"),
            "result_run_sha256": sha256_file(result_dir / "run.json"),
            "program_checks_sha256": sha256_file(OUT / "program-checks.json"),
            "source_map_sha256": sha256_file(OUT / "source-map.json"),
        },
        "case_hashes": case_hashes,
        "case_hash_method": "review_server.case_hash: json.dumps(payload_without_review, ensure_ascii=False, sort_keys=True, separators=(',', ':'))",
        "rubric": rubric,
        "evidence": evidence,
        "program_checks": program_checks,
        "frozen_v1": {
            "hashes": frozen_hash_now,
            "development_count": len(v1_split["development"]["case_ids"]),
            "holdout_count": len(v1_split["holdout"]["case_ids"]),
            "new_case_ids_outside_split": sorted(new_ids),
            "reviews_aggregate": reviews_now,
        },
        "reviews_workspace_empty": True,
        "judge": {
            "not_invoked_yet": True,
            "planned_command": [
                "node", "bin/dsh-judge.js", "run",
                "--bundle-dir", str(bundle_dir),
                "--output-dir", str(OUT / "judge_results" / JUDGE_RUN_ID),
                "--run", RUN_ID,
                "--judge-run-id", JUDGE_RUN_ID,
                "--concurrency", "2",
                "--timeout", "480000",
            ],
        },
        "bundle_validation": {
            "command": f"node bin/dsh-judge.js validate --bundle-dir {bundle_dir}",
            "returncode": validation.returncode,
            "stdout": validation.stdout.strip(),
            "stderr": validation.stderr.strip(),
        },
        "notes": [
            "Frozen before any Judge call; Judge results cannot change this rubric.",
            "The two cases are an isolated development projection, not the frozen v1 32/16 split.",
            "Program checks are structural only; they are not a semantic oracle or a success rate.",
        ],
    }
    (OUT / "freeze.json").write_text(json.dumps(freeze, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "freeze": str(OUT / "freeze.json"),
        "generated_at": freeze["generated_at"],
        "case_hashes": case_hashes,
        "input_hashes": {case_id: item["input_hash"] for case_id, item in evidence.items()},
        "contract_sha256": actual_source["contract"],
        "bundle_validation": validation.returncode,
        "frozen_v1": frozen_hash_now,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
