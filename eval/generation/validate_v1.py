# -*- coding: utf-8 -*-
"""Deterministic integrity validator for eval/datasets/v1.

It checks dataset structure, distribution, family/split disjointness, quote and
file hashes, duplicate risk, fault labels and review status.  It does NOT score
model answers; semantic support remains a human review item.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from source_index import check_quote, load_sources, sha256_file
from state_policy import ANSWER_ONLY_CASES, EXPLICIT_NOT_TRIED

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "eval" / "datasets" / "v1"
EXPECTED_CATEGORIES = {
    "direct_qa": 12,
    "multi_turn": 16,
    "missing_or_out_of_domain": 8,
    "negation_correction_ambiguity": 8,
    "fault_recovery": 4,
}
ALLOWED_STATE_KEYS = {
    "case_status",
    "tried_actions",
    "not_tried_actions",
    "model",
    "case_count",
    "scope",
}
ALLOWED_FAULTS = {"timeout", "process_restart", "empty_retrieval", "tool_error"}
WS = re.compile(r"[\s，。；！？,.;!?、]+")


def canonical_hash(cases: list[dict]) -> str:
    rows = [
        json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for case in sorted(cases, key=lambda item: item["id"])
    ]
    return hashlib.sha256(("\n".join(rows) + "\n").encode("utf-8")).hexdigest()


def normalize_text(text: str) -> str:
    return WS.sub("", text or "")


def shingles(text: str, n: int = 2) -> set[str]:
    text = normalize_text(text)
    return {text[i:i + n] for i in range(max(0, len(text) - n + 1))}


def similarity(a: str, b: str) -> float:
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _check(rows: list[dict], cid: str, ok: bool, detail: str) -> None:
    rows.append({"id": cid, "ok": bool(ok), "detail": detail})


def validate(root: Path | None = None) -> dict[str, Any]:
    data = Path(root) if root else DATA
    checks: list[dict] = []
    warnings: list[str] = []
    errors: list[str] = []

    suite_path = data / "suite.json"
    cases_path = data / "cases.jsonl"
    split_path = data / "split.json"
    evidence_path = data / "evidence_index.json"
    review_path = data / "review-todo.csv"

    for path in (suite_path, cases_path, split_path, evidence_path, review_path):
        _check(checks, f"file:{path.name}", path.is_file(), str(path.relative_to(ROOT)))
    if any(not item["ok"] for item in checks):
        return {"ok": False, "checks": checks, "errors": ["missing required files"]}

    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    split = json.loads(split_path.read_text(encoding="utf-8"))
    evidence_index = json.loads(evidence_path.read_text(encoding="utf-8"))
    sources = load_sources()

    _check(checks, "suite.schema_version", suite.get("schema_version") == "robot-support-eval/v1", str(suite.get("schema_version")))
    _check(checks, "suite.suite_id", suite.get("suite_id") == "robot-support-v1", str(suite.get("suite_id")))
    _check(checks, "suite.cases_file", suite.get("cases_file") == "cases.jsonl", str(suite.get("cases_file")))
    _check(checks, "suite.split_file", suite.get("split_file") == "split.json", str(suite.get("split_file")))
    _check(checks, "suite.review_status", suite.get("review_status") == "pending_human", str(suite.get("review_status")))
    _check(checks, "suite.counts", suite.get("counts") == {"development": 32, "holdout": 16}, json.dumps(suite.get("counts"), ensure_ascii=False))

    cases = []
    bad_lines = []
    for number, line in enumerate(cases_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            bad_lines.append(f"line {number}: empty")
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as exc:
            bad_lines.append(f"line {number}: {exc}")
    _check(checks, "cases.jsonl.parse", not bad_lines, "; ".join(bad_lines) or "all lines parse")
    _check(checks, "cases.total", len(cases) == 48, f"{len(cases)} cases")
    ids = [case.get("id") for case in cases]
    _check(checks, "cases.unique_ids", len(ids) == len(set(ids)), f"{len(ids)} ids, {len(set(ids))} unique")

    split_counts = Counter(case.get("split") for case in cases)
    _check(checks, "cases.split_counts", split_counts == Counter({"development": 32, "holdout": 16}), str(dict(split_counts)))
    cat_counts = Counter(case.get("category") for case in cases)
    _check(checks, "cases.category_counts", dict(cat_counts) == EXPECTED_CATEGORIES, json.dumps(dict(cat_counts), ensure_ascii=False))
    for case in cases:
        _check(checks, f"case.fields:{case.get('id')}", all(key in case for key in ("id", "family_id", "category", "split", "origin", "review", "turns", "evidence", "setup", "scoring")), "required top-level fields")

    family_rows: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        family_rows[case.get("family_id")].append(case)
    _check(checks, "families.count", len(family_rows) == 24, f"{len(family_rows)} families")
    _check(checks, "families.variant_budget", all(len(rows) == 2 for rows in family_rows.values()), "; ".join(f"{key}:{len(rows)}" for key, rows in family_rows.items() if len(rows) != 2) or "2 cases/family")
    split_by_family = {}
    for family_id, rows in family_rows.items():
        splits = {row.get("split") for row in rows}
        categories = {row.get("category") for row in rows}
        _check(checks, f"family.split:{family_id}", len(splits) == 1, str(splits))
        _check(checks, f"family.category:{family_id}", len(categories) == 1, str(categories))
        split_by_family[family_id] = next(iter(splits))
    _check(checks, "families.split_disjoint", all(len({row.get("split") for row in family_rows[fid]}) == 1 for fid in family_rows), "no family crosses splits")

    split_dev = set(split.get("development", {}).get("case_ids") or [])
    split_hold = set(split.get("holdout", {}).get("case_ids") or [])
    case_ids = set(ids)
    _check(checks, "split.case_ids_cover", split_dev | split_hold == case_ids and not (split_dev & split_hold), f"dev={len(split_dev)} holdout={len(split_hold)}")
    _check(checks, "split.development_families", set((split.get("development", {}).get("families") or {}).keys()) == {fid for fid, sp in split_by_family.items() if sp == "development"}, "family groups match")
    _check(checks, "split.holdout_families", set((split.get("holdout", {}).get("families") or {}).keys()) == {fid for fid, sp in split_by_family.items() if sp == "holdout"}, "family groups match")

    holdout_cases = [case for case in cases if case.get("split") == "holdout"]
    dev_cases = [case for case in cases if case.get("split") == "development"]
    holdout_hash = canonical_hash(holdout_cases)
    cases_hash = canonical_hash(cases)
    _check(checks, "hash.holdout", suite.get("source_snapshot", {}).get("holdout_cases_sha256") == holdout_hash, holdout_hash)
    _check(checks, "hash.cases", suite.get("source_snapshot", {}).get("cases_sha256") == cases_hash, cases_hash)
    _check(checks, "hash.split_holdout", split.get("holdout_cases_sha256") == holdout_hash, str(split.get("holdout_cases_sha256")))
    _check(checks, "holdout.no_seed_rewrites", all(not (case.get("origin", {}).get("seed_ids") or []) for case in holdout_cases), "holdout seed_ids must be empty")

    # Source documents and evidence quotes.
    docs = suite.get("source_snapshot", {}).get("documents") or []
    doc_by_path = {doc.get("path"): doc for doc in docs}
    for path in ("assets/knowledge/故障排除.txt", "assets/knowledge/扫地机器人100问.pdf", "assets/knowledge/扫地机器人100问2.txt", "assets/knowledge/扫拖一体机器人100问.txt", "assets/knowledge/维护保养.txt", "assets/knowledge/选购指南.txt", "README.md"):
        _check(checks, f"source.doc:{path}", path in doc_by_path and doc_by_path[path].get("sha256") == sources[path]["sha256"], path)
    _check(checks, "source.doc_count", len(docs) == 7, f"{len(docs)} docs")
    _check(checks, "evidence.index_docs", len(evidence_index.get("documents") or []) == 7, f"{len(evidence_index.get('documents') or [])} docs")

    evidence_rows = 0
    quote_exact = 0
    quote_normalized = 0
    quote_missing = []
    bad_hash = []
    for case in cases:
        if not case.get("evidence"):
            errors.append(f"{case.get('id')}: evidence empty")
        for item in case.get("evidence") or []:
            evidence_rows += 1
            path = item.get("file")
            source = sources.get(path)
            if not source:
                quote_missing.append(f"{item.get('id')}: missing file {path}")
                continue
            if item.get("sha256") != source["sha256"]:
                bad_hash.append(f"{item.get('id')}: {path}")
            status = check_quote(item.get("quote", ""), source["text"])
            if status == "exact":
                quote_exact += 1
            elif status == "normalized":
                quote_normalized += 1
            else:
                quote_missing.append(f"{item.get('id')}: {path}")
            if item.get("kind") not in {"knowledge", "requirement"}:
                errors.append(f"{item.get('id')}: bad kind {item.get('kind')}")
            if item.get("kind") == "requirement" and path != "README.md":
                errors.append(f"{item.get('id')}: requirement must be README")
    _check(checks, "evidence.quote_exact_or_normalized", not quote_missing, "; ".join(quote_missing) or f"exact={quote_exact}, normalized={quote_normalized}")
    _check(checks, "evidence.file_sha256", not bad_hash, "; ".join(bad_hash) or f"{evidence_rows} evidence rows")
    _check(checks, "evidence.kind_mix", any(item.get("kind") == "knowledge" for case in cases for item in case.get("evidence") or []), "at least one knowledge anchor")

    # Turn/expected/state contract.
    allowed_turn_roles = {"user", "assistant_fixture"}
    duplicate_exact = []
    near_pairs = []
    user_texts = {}
    for case in cases:
        turns = case.get("turns") or []
        if not turns:
            errors.append(f"{case.get('id')}: no turns")
        for index, turn in enumerate(turns, 1):
            cid = f"{case.get('id')}#{index}"
            _check(checks, f"turn.role:{cid}", turn.get("role") in allowed_turn_roles, str(turn.get("role")))
            if not str(turn.get("content") or "").strip():
                errors.append(f"{cid}: empty content")
            expected = turn.get("expected") or {}
            missing_keys = [key for key in ("required_claims", "forbidden_claims", "state", "required_behaviors", "expected_sources") if key not in expected]
            if missing_keys:
                errors.append(f"{cid}: expected missing {missing_keys}")
            if turn.get("role") == "user":
                nonempty = any(expected.get(key) for key in ("required_claims", "forbidden_claims", "required_behaviors", "expected_sources"))
                if not nonempty:
                    errors.append(f"{cid}: empty user expectation")
                state = expected.get("state") or {}
                bad_keys = set(state) - ALLOWED_STATE_KEYS
                if bad_keys:
                    errors.append(f"{cid}: non-stable state keys {sorted(bad_keys)}")
                if state.get("case_status") not in {None, "active", "resolved"}:
                    errors.append(f"{cid}: bad case_status {state.get('case_status')}")
                for tried in state.get("tried_actions") or []:
                    if not isinstance(tried, dict) or not tried.get("action_id") or not tried.get("outcome"):
                        errors.append(f"{cid}: bad tried_actions item")
                expected_not_tried = [] if case.get("id") in ANSWER_ONLY_CASES else EXPLICIT_NOT_TRIED.get(case.get("id"), {}).get(index, [])
                actual_not_tried = list(state.get("not_tried_actions") or [])
                _check(
                    checks,
                    f"state.not_tried_explicit:{cid}",
                    actual_not_tried == expected_not_tried,
                    f"actual={actual_not_tried} expected={expected_not_tried}",
                )
                for source in expected.get("expected_sources") or []:
                    if source not in sources:
                        errors.append(f"{cid}: expected_source not found {source}")
                    if source not in {item.get("file") for item in case.get("evidence") or []}:
                        errors.append(f"{cid}: expected_source not in evidence {source}")
                user_texts[cid] = str(turn.get("content") or "")

        review = case.get("review") or {}
        _check(checks, f"review.pending:{case.get('id')}", review.get("status") == "pending_human", str(review.get("status")))
        origin = case.get("origin") or {}
        _check(checks, f"origin.synthetic:{case.get('id')}", origin.get("kind") == "synthetic" and isinstance(origin.get("seed_ids"), list), str(origin))
        scoring = case.get("scoring") or {}
        _check(checks, f"scoring.lists:{case.get('id')}", bool(scoring.get("automatic")) and bool(scoring.get("human")), "automatic/human non-empty")
        _check(checks, f"scoring.expected_sources_mode:{case.get('id')}", scoring.get("expected_sources_mode") == "any_of", str(scoring.get("expected_sources_mode")))
        expected_denominator = "fault_injection" if case.get("category") == "fault_recovery" else "qa"
        _check(checks, f"scoring.denominator:{case.get('id')}", scoring.get("denominator") == expected_denominator, str(scoring.get("denominator")))
        if case.get("id") in ANSWER_ONLY_CASES:
            _check(checks, f"state.answer_only_empty:{case.get('id')}", all(not (turn.get("expected", {}).get("state") or {}) for turn in case.get("turns") or []), "all state must be empty")
            _check(checks, f"state.score_na:{case.get('id')}", case.get("state_scope") == "planned_out_of_scope_current_p1" and scoring.get("state_score") == "not_applicable_current_p1", f"{case.get('state_scope')}/{scoring.get('state_score')}")
        else:
            _check(checks, f"state.score_current:{case.get('id')}", case.get("state_scope") == "current_p1_recharge_state" and scoring.get("state_score") == "included_current_p1", f"{case.get('state_scope')}/{scoring.get('state_score')}")
        if case.get("category") == "fault_recovery":
            setup = case.get("setup") or {}
            _check(checks, f"fault.setup:{case.get('id')}", setup.get("kind") == "fault_injection" and setup.get("fault") in ALLOWED_FAULTS and bool(setup.get("steps")), str(setup))
            _check(checks, f"fault.injection_label:{case.get('id')}", origin.get("injection") == "synthetic_fault_injection", str(origin.get("injection")))
        else:
            _check(checks, f"fault.not_mixed:{case.get('id')}", case.get("setup") == {}, str(case.get("setup")))
            _check(checks, f"fault.no_injection_label:{case.get('id')}", "injection" not in origin, str(origin.get("injection")))

    # Duplicate checks: exact normalized turn text and 2-gram near duplicates.
    grouped = defaultdict(list)
    for cid, text in user_texts.items():
        grouped[normalize_text(text)].append(cid)
    duplicate_exact = [rows for rows in grouped.values() if len(rows) > 1]
    keys = list(user_texts)
    for i, left in enumerate(keys):
        for right in keys[i + 1:]:
            if left.split("#")[0] == right.split("#")[0]:
                continue
            score = similarity(user_texts[left], user_texts[right])
            if score >= 0.90:
                near_pairs.append({"a": left, "b": right, "score": round(score, 3)})
    _check(checks, "duplicates.exact", not duplicate_exact, json.dumps(duplicate_exact, ensure_ascii=False) or "none")
    if near_pairs:
        warnings.append(f"near-duplicate turn pairs >=0.90: {len(near_pairs)}")
    _check(checks, "duplicates.near_reviewed", True, f"{len(near_pairs)} pairs >=0.90 (warning only)")

    # Review todo must list every case, pending, with N/A option documented.
    with review_path.open(encoding="utf-8-sig", newline="") as fh:
        review_rows = list(csv.DictReader(fh))
    review_ids = {row.get("case_id") for row in review_rows}
    _check(checks, "review.todo_covers_cases", review_ids == case_ids, f"{len(review_ids)} rows")
    _check(checks, "review.todo_pending", all(row.get("status") == "pending_human" for row in review_rows), "all pending_human")

    errors.extend(item["detail"] for item in checks if not item["ok"])
    ok = not errors
    return {
        "ok": ok,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": "eval/datasets/v1",
        "counts": {
            "total": len(cases),
            "development": split_counts.get("development", 0),
            "holdout": split_counts.get("holdout", 0),
            "families": len(family_rows),
            "categories": dict(cat_counts),
            "evidence_rows": evidence_rows,
        },
        "hashes": {
            "cases_sha256": cases_hash,
            "holdout_cases_sha256": holdout_hash,
            "suite_sha256": sha256_file(suite_path),
            "split_sha256": sha256_file(split_path),
            "cases_file_sha256": sha256_file(cases_path),
        },
        "quotes": {"exact": quote_exact, "normalized": quote_normalized, "missing": quote_missing},
        "duplicates": {"exact": duplicate_exact, "near": near_pairs},
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
        "semantic_support": "pending_human (validator only checks quote/hash/structure, not whether an answer is semantically supported)",
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = validate()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("OK" if report["ok"] else "FAILED")
        print(json.dumps({k: report[k] for k in ("counts", "hashes", "quotes", "duplicates", "warnings")}, ensure_ascii=False, indent=2))
        for error in report["errors"][:30]:
            print("ERROR", error)
    raise SystemExit(0 if report["ok"] else 1)
