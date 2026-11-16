# -*- coding: utf-8 -*-
"""Build eval/datasets/v1 from the evidence/premise catalogue + family seeds.

Usage (from the project root, no model API needed)::

    python eval/generation/build_v1.py --seed 20261113

The script is intentionally data-driven: it validates evidence first, expands
hand-authored variants into the shared JSONL contract, writes review/freeze
artifacts, then runs the integrity validator.  It does not call an LLM, start
the product, or modify knowledge/model/rag assets.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from audit_v0 import audit_v0  # noqa: E402
from evidence_data import COVERAGE_CHECKS, DOCS, EVIDENCE, PREMISES  # noqa: E402
from families_dev import FAMILIES as DEV_FAMILIES  # noqa: E402
from families_holdout import FAMILIES as HOLD_FAMILIES  # noqa: E402
from source_index import check_quote, doc_entry, load_sources, sha256_file  # noqa: E402
from state_policy import (  # noqa: E402
    PREVIOUS_CASES_SHA256,
    PREVIOUS_HOLDOUT_SHA256,
    PREVIOUS_REVISION,
    apply_state_policy,
)
from validate_v1 import canonical_hash  # noqa: E402

DATA = ROOT / "eval" / "datasets" / "v1"
OUT_REPORT = ROOT / "outputs" / "case-generation-report.json"
GENERATOR_VERSION = "v1.0"
DEFAULT_SEED = 20261113

AUTOMATIC_CHECKS = [
    "dataset.schema",
    "dataset.unique_id",
    "dataset.counts_32_16",
    "dataset.family_split_disjoint",
    "evidence.quote_exact_or_normalized",
    "evidence.file_sha256",
    "duplicates.normalized_exact",
    "review.pending_human",
]
HUMAN_CHECKS = [
    "answer.required_claims_semantically_supported",
    "answer.forbidden_claims_absent",
    "answer.state_semantics_consistent",
    "answer.expected_sources_semantically_support_claims",
    "answer.required_behaviors_observed",
]


def load_families() -> list[dict]:
    return [*DEV_FAMILIES, *HOLD_FAMILIES]


def build_evidence_index(sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    evidence = []
    seen = set()
    for item in EVIDENCE:
        eid = item["id"]
        if eid in seen:
            raise SystemExit(f"duplicate evidence id: {eid}")
        seen.add(eid)
        source = sources.get(item["file"])
        if not source:
            raise SystemExit(f"{eid}: missing source file {item['file']}")
        status = check_quote(item["quote"], source["text"])
        if status == "missing":
            raise SystemExit(f"{eid}: quote not found in {item['file']}")
        evidence.append({
            "id": eid,
            "file": item["file"],
            "quote": item["quote"],
            "sha256": source["sha256"],
            "kind": item["kind"],
            "topic": item["topic"],
            "quote_check": status,
        })
    return {
        "schema_version": "robot-support-eval/v1",
        "created_before_cases": True,
        "documents": [doc_entry(path, sources[path]) for path in DOCS],
        "premises": PREMISES,
        "coverage_checks": COVERAGE_CHECKS,
        "evidence": evidence,
    }


def build_cases(families: list[dict], evidence_index: dict[str, Any]) -> list[dict]:
    evidence_by_id = {item["id"]: item for item in evidence_index["evidence"]}
    cases = []
    for family in families:
        variants = family.get("variants") or []
        if len(variants) != family.get("variant_budget"):
            raise SystemExit(f"{family['family_id']}: variant budget mismatch {len(variants)} != {family['variant_budget']}")
        for variant in variants:
            evidence_ids = list(dict.fromkeys([*family.get("evidence_ids", []), *variant.get("evidence_ids", [])]))
            evidence = []
            for eid in evidence_ids:
                item = evidence_by_id.get(eid)
                if not item:
                    raise SystemExit(f"{variant['id']}: unknown evidence id {eid}")
                evidence.append({
                    "id": item["id"],
                    "file": item["file"],
                    "quote": item["quote"],
                    "sha256": item["sha256"],
                    "kind": item["kind"],
                    "topic": item["topic"],
                })
            origin = {"kind": "synthetic", "seed_ids": list(variant.get("seed_ids") or [])}
            if family["category"] == "fault_recovery":
                origin["injection"] = "synthetic_fault_injection"
            turns = []
            for turn in variant.get("turns") or []:
                expected = {
                    "required_claims": list(turn["expected"].get("required_claims") or []),
                    "forbidden_claims": list(turn["expected"].get("forbidden_claims") or []),
                    "state": dict(turn["expected"].get("state") or {}),
                    "required_behaviors": list(turn["expected"].get("required_behaviors") or []),
                    "expected_sources": list(turn["expected"].get("expected_sources") or []),
                }
                row = {"role": turn["role"], "content": turn["content"], "expected": expected}
                if turn.get("session"):
                    row["session"] = turn["session"]
                turns.append(row)
            human = list(dict.fromkeys([*HUMAN_CHECKS, *family.get("human_focus", [])]))
            cases.append({
                "id": variant["id"],
                "family_id": family["family_id"],
                "category": family["category"],
                "split": family["split"],
                "title": variant.get("title") or family.get("title"),
                "origin": origin,
                "review": {"status": "pending_human"},
                "turns": turns,
                "evidence": evidence,
                "setup": dict(variant.get("setup") or {}),
                "scoring": {
                    "automatic": list(AUTOMATIC_CHECKS),
                    "human": human,
                    "denominator": "fault_injection" if family["category"] == "fault_recovery" else "qa",
                    "expected_sources_mode": "any_of",
                    "expected_sources_note": "expected_sources are acceptable/minimum-coverage candidates; not all must be retrieved or cited. Citation presence and semantic support are scored separately.",
                },
            })
    return cases


def write_review_files(cases: list[dict]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    fields = [
        "case_id", "family_id", "category", "split", "title", "status",
        "reviewer", "required_claims", "forbidden_claims", "state_scope", "state_score",
        "source_semantic_support", "overall", "notes",
    ]
    with (DATA / "review-todo.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for case in sorted(cases, key=lambda item: item["id"]):
            writer.writerow({
                "case_id": case["id"],
                "family_id": case["family_id"],
                "category": case["category"],
                "split": case["split"],
                "title": case.get("title", ""),
                "status": "pending_human",
                "reviewer": "",
                "required_claims": "",
                "forbidden_claims": "",
                "state_scope": case.get("state_scope", ""),
                "state_score": (case.get("scoring") or {}).get("state_score", ""),
                "source_semantic_support": "",
                "overall": "",
                "notes": "未审核；clauses 无该维度填 N/A，不要填 pass；两名复核者独立判定，分歧记录理由。",
            })

    (DATA / "review-template.md").write_text(
        """# 人工审核模板（v1）

状态：`pending_human`。本文件不是“已审核”证明。

## 每案必填

| 字段 | 允许值 | 说明 |
| --- | --- | --- |
| required_claims | pass / partial / fail / N/A | 逐条语义判断，不用正则命中代替 |
| forbidden_claims | pass / partial / fail / N/A | 出现即失败；N/A 仅当该案没有该维度 |
| state | pass / partial / fail / N/A | 只看稳定公共语义（active/resolved、已试/未试、scope 等），不强制唯一 next_action |
| source_semantic_support | pass / partial / fail / N/A | 引文存在≠支持结论；逐条核对来源是否真的支持 |
| overall | pass / partial / fail / N/A | 综合判定 |
| notes | 文本 | 失败理由、分歧、证据缺口 |

## 规则

1. `clauses` 没有该维度时填 `N/A`，不要填 `pass`；N/A 不计入通过率分子/分母。
2. 两名复核者独立看同一 rubric；分歧由第三人裁决并留 notes。
3. 来源存在性由 `validate_v1.py` 自动检查；来源是否语义支持结论仍由人工判断。
4. 不把本轮人工构造/未审核案例写成“真实用户验证”“成功率”或“官方售后结论”。
5. 发现真实失败时，按 `docs/case-generation.md` 的“加入真实失败”流程新增开发案例，不直接改冻结 holdout。
""",
        encoding="utf-8",
    )


def write_split(cases: list[dict], families: list[dict], seed: int, generated_at: str) -> dict:
    def block(split: str) -> dict:
        rows = [case for case in cases if case["split"] == split]
        family_groups: dict[str, list[str]] = {}
        for case in sorted(rows, key=lambda item: item["id"]):
            family_groups.setdefault(case["family_id"], []).append(case["id"])
        return {
            "count": len(rows),
            "case_ids": sorted(case["id"] for case in rows),
            "families": dict(sorted(family_groups.items())),
        }

    family_meta = {}
    for family in families:
        family_meta[family["family_id"]] = {
            "category": family["category"],
            "split": family["split"],
            "title": family["title"],
            "oracle": family["oracle"],
            "variant_budget": family["variant_budget"],
        }
    holdout = block("holdout")
    payload = {
        "schema_version": "robot-support-eval/v1",
        "suite_id": "robot-support-v1",
        "generated_at": generated_at,
        "seed": seed,
        "development": block("development"),
        "holdout": holdout,
        "family_split": {fid: meta["split"] for fid, meta in sorted(family_meta.items())},
        "family_metadata": family_meta,
        "holdout_cases_sha256": canonical_hash([case for case in cases if case["split"] == "holdout"]),
        "cases_sha256": canonical_hash(cases),
        "frozen_holdout": True,
        "note": "holdout 已冻结；本轮只做 schema/证据完整检查，不运行、不据其调参。",
    }
    return payload


def build_report(suite: dict, split: dict, validation: dict, audit: dict, corrections: list[dict], revision_history: list[dict], generated_at: str, seed: int) -> dict:
    cases = []
    for line in (DATA / "cases.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(json.loads(line))
    provenance = [
        {"case_id": case["id"], "split": case["split"], "seed_ids": case["origin"].get("seed_ids") or []}
        for case in sorted(cases, key=lambda item: item["id"])
        if case["origin"].get("seed_ids")
    ]
    docs = suite["source_snapshot"]["documents"]
    evidence_counts: dict[str, int] = {}
    for case in cases:
        for item in case.get("evidence") or []:
            evidence_counts[item["file"]] = evidence_counts.get(item["file"], 0) + 1
    return {
        "schema_version": "robot-support-eval/v1",
        "generated_at": generated_at,
        "revision": "v1.1",
        "status": "dataset_built_validated_unreviewed",
        "what_ran": [
            "read-only source extraction (TXT UTF-8; PDF pdftotext -layout)",
            "evidence/premise catalogue validation before case generation",
            "48-case expansion from 24 hand-authored families (2 variants/family)",
            "v1.1 data-oracle correction: non-recharge state removed, not_tried limited to explicit negation, expected_sources=any_of",
            "deterministic integrity validation (structure, counts, family split, hashes, quotes, duplicates, fault labels, review pending)",
            "v0 16-case read-only audit",
        ],
        "what_did_not_run": [
            "no Agent model call, no LLM API call, no product service start",
            "no answer-quality/semantic scorer; human review pending",
            "no real-user data, no success-rate measurement",
            "no modification of assets/knowledge, assets/models, assets/rag_index, old v0 file, runtime/scorers/tests/plans/main repo/DSH",
        ],
        "counts": {
            "total": 48,
            "development": split["development"]["count"],
            "holdout": split["holdout"]["count"],
            "families": len(split["family_metadata"]),
            "categories": validation["counts"]["categories"],
            "denominators": {"qa": 44, "fault_injection": 4},
        },
        "source_references": [
            {
                "path": doc["path"],
                "sha256": doc["sha256"],
                "kind": doc["kind"],
                "extraction": doc["extraction"],
                "case_evidence_rows": evidence_counts.get(doc["path"], 0),
            }
            for doc in docs
        ],
        "premises": [
            {"id": item["id"], "claim": item["claim"], "evidence_ids": item["evidence_ids"]}
            for item in PREMISES
        ],
        "seed_provenance": {
            "generator_seed": seed,
            "old_v0_baseline": audit["baseline"],
            "dev_cases_with_old_seed_ids": provenance,
            "holdout_policy": "所有 holdout origin.seed_ids 为空；holdout 来自未参与 v0 调试的新场景族。",
            "semantic_authorship": "问法/多轮/rubric 由本次 DeepSeek executor 依据证据前提手写；没有把随机同义替换当成生成，也没有依赖运行时 regex 产物。",
        },
        "holdout": {
            "frozen": True,
            "holdout_cases_sha256": split["holdout_cases_sha256"],
            "cases_sha256": split["cases_sha256"],
            "note": "本轮只校验 schema/证据完整并冻结 hash；未运行 holdout，也未据 holdout 调参。",
        },
        "revision_history": revision_history,
        "oracle_corrections": {
            "count": len(corrections),
            "by_kind": {
                kind: sum(1 for item in corrections if item["kind"] == kind)
                for kind in sorted({item["kind"] for item in corrections})
            },
            "previous_cases_sha256": PREVIOUS_CASES_SHA256,
            "previous_holdout_cases_sha256": PREVIOUS_HOLDOUT_SHA256,
            "current_cases_sha256": split["cases_sha256"],
            "current_holdout_cases_sha256": split["holdout_cases_sha256"],
            "reason": "Data-only review correction: P1 structures only recharge-failure state; not_tried only on explicit negation; expected_sources are any_of candidates. No product/runtime/scorer change and no holdout execution.",
        },
        "validation": {
            "ok": validation["ok"],
            "checked_at": validation["checked_at"],
            "quotes": validation["quotes"],
            "duplicates": {"exact": validation["duplicates"]["exact"], "near_count": len(validation["duplicates"]["near"])},
            "warnings": validation["warnings"],
            "hashes": validation["hashes"],
            "semantic_support": validation["semantic_support"],
        },
        "audit_v0": audit["summary"],
        "review": {
            "status": "pending_human",
            "todo": "eval/datasets/v1/review-todo.csv",
            "template": "eval/datasets/v1/review-template.md",
            "not_claimed": "未完成人工语义复核；不能写成已审核通过、真实用户验证或成功率。",
        },
        "limitations": [
            "48 条均为合成案例，不是真实用户样本，不宣称统计显著或全量成功率。",
            "语义 rubric（claims/forbidden/behavior）由 executor 手写；自动校验只覆盖结构/引用/hash/分布/重复，不是语义裁判。",
            "v1.1 删除了非回充主题的强制结构化 state（answer-only / not state-scored）；这不代表这些 QA 不需要状态，而是不在 P1 本月 recharge-failure state 范围。合法回充状态缺陷保留。",
            "not_tried 只在用户明确否定时出现；沉默/未提及不写事实，因此没有 not_tried 不等于用户已做。",
            "expected_sources 是 any_of 候选；不要求全部检索/引用，引用存在也不等于语义支持。",
            "holdout 只冻结未运行；未来若有人查看过答案并据此调产品，必须迁回开发集并补新的留出场景。",
            "PDF 引文依赖 pdftotext -layout 抽取；换工具可能产生空白差异，因此校验同时接受 whitespace-normalized 命中并记录。",
            "6 份资料是通用文档，不是品牌官方授权售后；价格/保修只能作一般说明。",
            "Galaxy X99 的“无资料”是已做全文覆盖的语料结论，不是现实世界不存在，也不是 top-k 未命中的证明。",
            "故障注入只定义 setup/steps 和预期，不包含真实注入器运行结果；不计入 QA 分母。",
            "边刷周期等资料存在跨文档差异，已作为条件/范围差异处理，不强行合并为唯一值。",
            "没有启动服务/浏览器，没有运行 Agent 模型；没有读取或写入密钥。",
        ],
        "artifacts": {
            "suite": "eval/datasets/v1/suite.json",
            "cases": "eval/datasets/v1/cases.jsonl",
            "split": "eval/datasets/v1/split.json",
            "evidence_index": "eval/datasets/v1/evidence_index.json",
            "audit": "eval/datasets/v1/audit.json",
            "review_todo": "eval/datasets/v1/review-todo.csv",
            "review_template": "eval/datasets/v1/review-template.md",
            "docs": "docs/case-generation.md",
            "scripts": [
                "eval/generation/source_index.py",
                "eval/generation/evidence_data.py",
                "eval/generation/families_dev.py",
                "eval/generation/families_holdout.py",
                "eval/generation/state_policy.py",
                "eval/generation/build_v1.py",
                "eval/generation/validate_v1.py",
                "eval/generation/audit_v0.py",
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--generated-at", default=None)
    args = parser.parse_args()
    generated_at = args.generated_at or __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds")
    DATA.mkdir(parents=True, exist_ok=True)

    sources = load_sources()
    evidence_index = build_evidence_index(sources)
    (DATA / "evidence_index.json").write_text(
        json.dumps(evidence_index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("evidence_index: validated and written first")

    families = load_families()
    if len(families) != 24:
        raise SystemExit(f"expected 24 families, got {len(families)}")
    cases = build_cases(families, evidence_index)
    if len(cases) != 48:
        raise SystemExit(f"expected 48 cases, got {len(cases)}")
    corrections = apply_state_policy(cases)
    dev = [case for case in cases if case["split"] == "development"]
    hold = [case for case in cases if case["split"] == "holdout"]
    if len(dev) != 32 or len(hold) != 16:
        raise SystemExit(f"bad split counts: dev={len(dev)} holdout={len(hold)}")
    if any(case["origin"].get("seed_ids") for case in hold):
        raise SystemExit("holdout must not contain v0 seed rewrites")

    rng = random.Random(args.seed)
    rng.shuffle(dev)
    rng.shuffle(hold)
    ordered = dev + hold
    with (DATA / "cases.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for case in ordered:
            fh.write(json.dumps(case, ensure_ascii=False, separators=(",", ":")) + "\n")

    split = write_split(ordered, families, args.seed, generated_at)
    (DATA / "split.json").write_text(json.dumps(split, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    docs = [doc_entry(path, sources[path]) for path in DOCS]
    revision_history = [
        {
            "revision": PREVIOUS_REVISION,
            "status": "superseded" if split["cases_sha256"] != PREVIOUS_CASES_SHA256 else "unchanged",
            "cases_sha256": PREVIOUS_CASES_SHA256,
            "holdout_cases_sha256": PREVIOUS_HOLDOUT_SHA256,
            "reason": "Initial v1 build; superseded after the data review corrected non-recharge state overreach, not_tried overreach, and expected_sources semantics.",
        },
        {
            "revision": "v1.1",
            "status": "current",
            "cases_sha256": split["cases_sha256"],
            "holdout_cases_sha256": split["holdout_cases_sha256"],
            "reason": "Data-only oracle correction; no product/runtime/scorer change, no holdout execution, holdout re-frozen under the new hash.",
        },
    ]
    suite = {
        "schema_version": "robot-support-eval/v1",
        "suite_id": "robot-support-v1",
        "revision": "v1.1",
        "cases_file": "cases.jsonl",
        "split_file": "split.json",
        "counts": {"development": 32, "holdout": 16},
        "source_snapshot": {
            "documents": docs,
            "generated_at": generated_at,
            "generator_version": GENERATOR_VERSION,
            "seed": args.seed,
            "cases_sha256": split["cases_sha256"],
            "holdout_cases_sha256": split["holdout_cases_sha256"],
            "evidence_index_sha256": sha256_file(DATA / "evidence_index.json"),
            "coverage_checks": COVERAGE_CHECKS,
            "note": "源文件只读；README 为 requirement，其余 6 份为 knowledge。",
        },
        "review_status": "pending_human",
        "denominators": {"qa": 44, "fault_injection": 4},
        "description": "48 条合成回归案例（32 development / 16 frozen holdout），24 个情境+oracle 家族；人工语义复核 pending。",
        "audit_history": revision_history,
        "oracle_corrections": {
            "count": len(corrections),
            "by_kind": {
                kind: sum(1 for item in corrections if item["kind"] == kind)
                for kind in sorted({item["kind"] for item in corrections})
            },
            "note": "Revision v1.1 data-only correction; see audit.json / docs/case-generation.md.",
        },
    }
    (DATA / "suite.json").write_text(json.dumps(suite, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_review_files(ordered)
    print("cases/split/suite/review files written; running validation")

    from validate_v1 import validate

    validation = validate()
    audit = audit_v0()
    audit_payload = {
        "schema_version": "robot-support-eval/v1",
        "generated_at": generated_at,
        "revision": "v1.1",
        "legacy_v0": audit,
        "v1_validation": validation,
        "revision_history": revision_history,
        "oracle_corrections": {
            "previous_revision": PREVIOUS_REVISION,
            "previous_cases_sha256": PREVIOUS_CASES_SHA256,
            "previous_holdout_cases_sha256": PREVIOUS_HOLDOUT_SHA256,
            "current_cases_sha256": split["cases_sha256"],
            "current_holdout_cases_sha256": split["holdout_cases_sha256"],
            "source": "state_policy.py (applied after seed expansion)",
            "items": corrections,
            "not_executed": "No Agent model/product/holdout run was used to decide these corrections; they are data-oracle fixes from the review.",
        },
        "new_case_fix_policy": {
            "old_file_unchanged": True,
            "old_v0_used_for_debugging_only": True,
            "seeds_and_rewrites_are_development_only": True,
            "holdout_seed_ids_empty": all(not (case["origin"].get("seed_ids") or []) for case in hold),
        },
    }
    (DATA / "audit.json").write_text(json.dumps(audit_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = build_report(suite, split, validation, audit, corrections, revision_history, generated_at, args.seed)
    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUT_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({
        "ok": validation["ok"],
        "cases": validation["counts"]["total"],
        "dev": validation["counts"]["development"],
        "holdout": validation["counts"]["holdout"],
        "holdout_sha256": split["holdout_cases_sha256"],
        "exact_quotes": validation["quotes"]["exact"],
        "normalized_quotes": validation["quotes"]["normalized"],
        "near_duplicates": len(validation["duplicates"]["near"]),
        "errors": validation["errors"][:10],
    }, ensure_ascii=False, indent=2))
    return 0 if validation["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
