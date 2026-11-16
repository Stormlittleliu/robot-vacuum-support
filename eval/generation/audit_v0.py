# -*- coding: utf-8 -*-
"""Read-only audit of the v0 16-case starter suite.

The v0 file is never modified.  This module records quote-level verification,
factual-support limits, and the design issues that v1 cases fix.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from source_index import ROOT, check_quote, load_sources

V0 = ROOT / "eval" / "cases" / "v0" / "recharge-start.json"

# Manual findings.  Quote existence is computed; support/design judgements are
# authored here so they are auditable instead of being presented as automatic.
FINDINGS: dict[str, dict[str, Any]] = {
    "rs-01-direct-recharge": {
        "factual_support": "supported_with_design_inference",
        "issues": [{
            "severity": "medium",
            "type": "expected_over_specification",
            "detail": "五项 facts 把清障、传感器、APP 设置写成较固定的排查顺序；behavior 要求先给顺序再询问，但证据中没有把该顺序标为产品需求。",
            "fix_refs": ["rsv1-dev-direct-recharge-triage-01", "rsv1-dev-direct-recharge-triage-02"],
        }],
    },
    "rs-02-after-cleared-and-wiped": {
        "factual_support": "supported_with_design_inference",
        "issues": [{
            "severity": "low",
            "type": "unsourced_service_inference",
            "detail": "第91条只写检测回充传感器/充电座信号，没有直接写“联系官方售后”；这是安全设计推断，应明确标注为行为要求。",
            "fix_refs": ["rsv1-dev-multi-recharge-tried-advance-01"],
        }],
    },
    "rs-03-denies-not-tried": {
        "factual_support": "supported_but_semantics_wrong",
        "issues": [{
            "severity": "high",
            "type": "negation_recorded_as_tried",
            "detail": "behavior 写“把否定信息作为已试状态的一部分”，但用户明确说没试过；正确语义是 not_tried，不能记成 tried。该错误会直接污染状态。",
            "fix_refs": ["rsv1-dev-multi-recharge-negation-01", "rsv1-dev-multi-recharge-negation-02"],
        }],
    },
    "rs-04-still-bad-no-context": {
        "factual_support": "supported_with_design_inference",
        "issues": [{
            "severity": "medium",
            "type": "over_specified_followup",
            "detail": "把“充电座指示灯/是否手动放回”写成示例追问，容易被当作唯一问题组；缺信息时应允许其他等价澄清。",
            "fix_refs": ["rsv1-dev-boundary-missing-context-01", "rsv1-dev-boundary-missing-context-02"],
        }],
    },
    "rs-05-success-close": {
        "factual_support": "supported",
        "issues": [{
            "severity": "low",
            "type": "closure_requirement_not_linked",
            "detail": "结案行为本身合理，但旧 schema 没有区分“自动回充恢复”和“手动放回能充电”；该假结案风险在 v1 被显式覆盖。",
            "fix_refs": ["rsv1-dev-multi-recharge-closure-01", "rsv1-dev-multi-recharge-closure-02"],
        }],
    },
    "rs-06-switch-topic-mop-water": {
        "factual_support": "supported_with_design_inference",
        "issues": [{
            "severity": "medium",
            "type": "unstable_state_key",
            "detail": "写“旧回充任务保留为暂停/未解决”，但“暂停”不是当前稳定公共状态值；v1 只用 active/resolved，避免把实现词写进 rubric。",
            "fix_refs": ["rsv1-dev-multi-topic-switch-mop-01", "rsv1-dev-multi-topic-switch-mop-02"],
        }],
    },
    "rs-07-user-correction": {
        "factual_support": "supported_with_design_inference",
        "issues": [{
            "severity": "low",
            "type": "correction_rule_uncited",
            "detail": "“以最后一次明确更正为准”是产品行为要求，旧 sources 没有对应的 requirement 证据；v1 将行为要求与知识事实分开。",
            "fix_refs": ["rsv1-dev-multi-recharge-correction-01", "rsv1-dev-multi-recharge-correction-02"],
        }],
    },
    "rs-08-recall-tried-steps": {
        "factual_support": "supported",
        "issues": [],
    },
    "rs-09-unknown-model-price": {
        "factual_support": "partly_supported_absence_unproven",
        "issues": [{
            "severity": "high",
            "type": "requirement_used_as_knowledge_and_absence_unproven",
            "detail": "README 的“只含6份资料”被当作价格缺失的证据，但它是 requirement，不是知识事实；旧集也没有全文覆盖检查，未区分“一次 top-k 未命中”和“语料确实没有 X99”。",
            "fix_refs": ["rsv1-dev-boundary-unknown-model-price-01", "rsv1-dev-boundary-unknown-model-price-02"],
        }],
    },
    "rs-10-out-of-domain-dress": {
        "factual_support": "supported_requirement_only",
        "issues": [{
            "severity": "medium",
            "type": "evidence_kind_missing",
            "detail": "README 引用准确且足以支持“不提供服装建议”，但旧 schema 没有 kind，无法阻止 requirement 被当作知识证据；v1 标为 requirement。",
            "fix_refs": ["rsv1-dev-boundary-out-of-domain-01", "rsv1-dev-boundary-out-of-domain-02"],
        }],
    },
    "rs-11-conflict-recharge-accuracy": {
        "factual_support": "quotes_supported_but_conflict_label_wrong",
        "issues": [{
            "severity": "high",
            "type": "false_conflict",
            "detail": "两处引文都真实，但 #36 是“充电座设计/支持自动回充精准度≥95%”，#84 是“复杂环境下回充精准度≥90%”；条件/语境不同，不是同对象同条件的矛盾，不能要求模型同时呈现为“资料冲突”。",
            "fix_refs": ["rsv1-dev-ambiguity-recharge-condition-02"],
        }],
    },
    "rs-12-network-timeout": {
        "factual_support": "intentional_no_sources",
        "issues": [{
            "severity": "medium",
            "type": "fault_schema_and_denominator",
            "detail": "超时语义合理，但旧用例把 fault_injection 放在顶层、没有 setup.kind 和“不混 QA 分母”标记；v1 统一为 setup.kind=fault_injection 并单列分母。",
            "fix_refs": ["rsv1-dev-fault-process-restart-01", "rsv1-hold-fault-empty-retrieval-01"],
        }],
    },
    "rs-13-session-resume-isolation": {
        "factual_support": "supported_with_design_inference",
        "issues": [{
            "severity": "medium",
            "type": "setup_schema",
            "detail": "setup 是字符串数组，重启/恢复没有被标成故障注入，也没有 steps 结构；v1 使用 setup.kind=fault_injection/fault=process_restart。",
            "fix_refs": ["rsv1-dev-fault-process-restart-01", "rsv1-dev-fault-process-restart-02"],
        }],
    },
    "rs-14-duplicate-request-idempotent": {
        "factual_support": "supported_with_design_inference",
        "issues": [{
            "severity": "low",
            "type": "internal_state_binding",
            "detail": "“状态中不产生重复的已建议步骤记录”是内部实现断言，无法由用户可见语义单独验证；v1 未把幂等内部计数作为新 case 的 oracle。",
            "fix_refs": [],
        }],
    },
    "rs-15-dangerous-diy-repair": {
        "factual_support": "mostly_supported_one_uncited_prohibition",
        "issues": [{
            "severity": "medium",
            "type": "uncited_original_cord_prohibition",
            "detail": "“不要建议用非原装电源线”在维护保养耗材第28条有依据，但旧 sources 没有列该条；引用清单不完整。",
            "fix_refs": ["rsv1-hold-direct-charge-heat-01"],
        }],
    },
    "rs-16-false-premise-mainboard": {
        "factual_support": "supported_with_design_inference",
        "issues": [{
            "severity": "low",
            "type": "unsourced_official_service_inference",
            "detail": "“建议官方售后”是安全行为要求，资料第91条只写检测传感器/信号；应在 rubric 中区分事实与安全要求。",
            "fix_refs": ["rsv1-dev-multi-recharge-closure-02", "rsv1-hold-multi-docking-alignment-02"],
        }],
    },
}


def audit_v0() -> dict[str, Any]:
    payload = json.loads(V0.read_text(encoding="utf-8"))
    sources = load_sources()
    rows = []
    for case in payload.get("cases") or []:
        results = []
        for index, src in enumerate(case.get("sources") or [], 1):
            source = sources.get(src.get("file"))
            status = "missing_file" if source is None else check_quote(src.get("quote", ""), source["text"])
            results.append({
                "index": index,
                "file": src.get("file"),
                "status": status,
                "quote_prefix": (src.get("quote") or "")[:60],
            })
        total = len(results)
        exact = sum(1 for row in results if row["status"] == "exact")
        normalized = sum(1 for row in results if row["status"] == "normalized")
        missing = total - exact - normalized
        finding = FINDINGS.get(case.get("id"), {"factual_support": "unclassified", "issues": []})
        rows.append({
            "id": case.get("id"),
            "title": case.get("title"),
            "check_mode": case.get("check_mode"),
            "quote_check": {
                "total": total,
                "exact": exact,
                "normalized": normalized,
                "missing": missing,
                "status": "pass" if missing == 0 else "fail",
                "results": results,
            },
            "factual_support": finding["factual_support"],
            "issues": finding["issues"],
            "verdict": "keep_v0_unchanged_dev_only",
        })
    all_quotes = sum(row["quote_check"]["total"] for row in rows)
    exact = sum(row["quote_check"]["exact"] for row in rows)
    normalized = sum(row["quote_check"]["normalized"] for row in rows)
    missing = sum(row["quote_check"]["missing"] for row in rows)
    return {
        "baseline": {
            "file": "eval/cases/v0/recharge-start.json",
            "sha256": __import__("hashlib").sha256(V0.read_bytes()).hexdigest(),
            "cases": len(rows),
            "modified": False,
        },
        "quote_summary": {
            "total": all_quotes,
            "exact": exact,
            "normalized": normalized,
            "missing": missing,
            "pdf_method": sources["assets/knowledge/扫地机器人100问.pdf"]["extraction"],
            "note": "逐字命中不等于语义支持；引用支持仍是人工判断。",
        },
        "summary": {
            "quote_accuracy": "50/50 可定位（PDF 使用 pdftotext -layout 抽取）",
            "material_issues": 3,
            "material_issue_ids": ["rs-03-denies-not-tried", "rs-09-unknown-model-price", "rs-11-conflict-recharge-accuracy"],
            "design_issues": ["固定首步/顺序过细", "README requirement 未分 kind", "暂停等不稳定状态词", "fault/setup schema 未统一"],
        },
        "cross_case_findings": [
            {
                "id": "audit-req-vs-knowledge",
                "detail": "rs-09/rs-10 把 README 作为来源但没有 kind。README 是产品需求，不能冒充知识证据；v1 用 kind=requirement，并要求知识事实至少有 knowledge 证据。",
            },
            {
                "id": "audit-clear-first",
                "detail": "旧集多条把“清理充电座周围”写成第一或唯一建议。v1 的 family oracle 明确：清障是低风险候选之一，不是所有回充问题的固定首步。",
            },
            {
                "id": "audit-90-95",
                "detail": "rs-11 的两条引文真实，但条件不同（充电座设计支持≥95% vs 复杂环境≥90%），不应标为真冲突；v1 改为条件/出处说明，不要求二选一或合并。",
            },
            {
                "id": "audit-schema",
                "detail": "旧集缺少 family_id/category/split/origin/review/scoring；fault_injection 在顶层、setup 为字符串数组；v1 统一共享契约，并保留旧文件不改。",
            },
        ],
        "cases": rows,
    }
