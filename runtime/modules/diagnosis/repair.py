# -*- coding: utf-8 -*-
"""纠错：从诊断生成可机械执行的修复方案，并落到候选 spec。

边界：
- 只生成"可验证的机械修改"（目前只有补引用指令）；其余给人工建议，不瞎改；
- 应用走 ``spec_edit.apply_ops``（整批或零），默认只写 ``candidates/``，不切 active；
- 修复是否有效，交给下一次评测复跑，而不是在这里自证。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from modules.editor.spec_edit import apply_ops, draft_issues, save_candidate

CITATION_INSTRUCTION = "\n\n每个结论必须用 [来源id] 标注来源；没有来源就明确说资料不足。"


@dataclass
class Remedy:
    code: str
    title: str
    detail: str
    blame: dict[str, str] = field(default_factory=dict)
    ops: list[dict[str, Any]] = field(default_factory=list)
    manual: bool = False
    confidence: str = "medium"  # low / medium / high

    @property
    def mechanical(self) -> bool:
        return bool(self.ops) and not self.manual

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "detail": self.detail,
            "blame": dict(self.blame),
            "ops": list(self.ops),
            "manual": self.manual,
            "confidence": self.confidence,
        }


@dataclass
class RepairResult:
    draft: dict[str, Any]
    issues: list[Any] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def _code(item: Any) -> str:
    return str(item.get("code") if isinstance(item, dict) else getattr(item, "code", ""))


def _blame(item: Any) -> dict[str, str]:
    return dict(item.get("blame") if isinstance(item, dict) else getattr(item, "blame", {}) or {})


def _nodes(spec: dict[str, Any], workflow: str) -> list[dict[str, Any]]:
    return list(((spec.get("workflows") or {}).get(workflow) or {}).get("nodes") or [])


def _last_llm_node(spec: dict[str, Any], workflow: str) -> dict[str, Any] | None:
    candidates = [node for node in _nodes(spec, workflow) if node.get("fn") == "llm_text"]
    return candidates[-1] if candidates else None


def plan_remedies(
    spec: dict[str, Any],
    workflow: str,
    diagnoses: list[Any],
) -> list[Remedy]:
    """按诊断生成去重后的修复方案。"""
    remedies: list[Remedy] = []
    seen: set[str] = set()

    for item in diagnoses:
        code = _code(item)
        if code in seen:
            continue
        seen.add(code)
        blame = _blame(item)

        if code == "missing_citation":
            node = _last_llm_node(spec, workflow)
            if node is None:
                remedies.append(Remedy(
                    code="append_citation_instruction",
                    title="补引用指令",
                    detail="工作流里没有 llm_text 节点，无法机械补提示词；请人工检查回答块。",
                    blame=blame,
                    manual=True,
                    confidence="low",
                ))
                continue
            prompt = str((node.get("args") or {}).get("prompt") or "")
            if "来源" in prompt and "[来源id]" in prompt:
                remedies.append(Remedy(
                    code="append_citation_instruction",
                    title="补引用指令",
                    detail=f"节点 {node.get('id')} 的提示词已包含引用要求，问题更可能在检索材料或模型；建议人工检查。",
                    blame=blame,
                    manual=True,
                    confidence="low",
                ))
                continue
            remedies.append(Remedy(
                code="append_citation_instruction",
                title=f"给节点 {node.get('id')} 补引用要求",
                detail="在最后一个 llm_text 节点的 prompt 末尾追加 [来源id] 引用要求。",
                blame=blame,
                ops=[{
                    "op": "set_arg",
                    "workflow": workflow,
                    "node_id": str(node.get("id")),
                    "key": "prompt",
                    "value": prompt + CITATION_INSTRUCTION,
                }],
                confidence="high",
            ))
        elif code == "no_retrieval":
            remedies.append(Remedy(
                code="check_retrieval",
                title="检查检索节点",
                detail="运行里没有调用检索工具：检查 retrieve 节点的工具名/参数，或工具是否被权限拒绝。",
                blame=blame,
                manual=True,
                confidence="medium",
            ))
        elif code == "unexpected_refusal":
            remedies.append(Remedy(
                code="check_refusal_branch",
                title="检查拒答分支",
                detail="回答退化成资料不足：检查 branch 的判空条件、检索 query 模板和知识库覆盖。",
                blame=blame,
                manual=True,
                confidence="medium",
            ))
        elif code == "refusal_missing":
            remedies.append(Remedy(
                code="check_refusal_branch",
                title="检查拒答分支",
                detail="应当拒答却没有拒答：检查 branch 条件，或在回答提示词里补充拒答要求。",
                blame=blame,
                manual=True,
                confidence="medium",
            ))
        elif code in {"workflow_error", "node_error"}:
            remedies.append(Remedy(
                code="inspect_node_error",
                title="检查节点报错",
                detail="工作流节点抛错：按 evidence 里的 step 查看节点参数和工具返回。",
                blame=blame,
                manual=True,
                confidence="high",
            ))
        elif code == "tool_error":
            remedies.append(Remedy(
                code="inspect_tool_error",
                title="检查工具报错",
                detail="工具调用失败：检查参数、超时和工具服务是否可用。",
                blame=blame,
                manual=True,
                confidence="high",
            ))
        else:
            remedies.append(Remedy(
                code="manual_review",
                title="人工复核",
                detail="当前规则无法自动定位到可机械修改的参数；按 blame 节点人工检查。",
                blame=blame,
                manual=True,
                confidence="low",
            ))
    return remedies


def apply_remedies(
    spec: dict[str, Any],
    remedies: list[Remedy],
    tool_names: set[str] | None = None,
) -> RepairResult:
    """应用机械修复；整批或零，默认返回新草稿（不改入参）。"""
    ops = [op for remedy in remedies if remedy.mechanical for op in remedy.ops]
    manual = [remedy.code for remedy in remedies if remedy.manual]
    applied = [remedy.code for remedy in remedies if remedy.mechanical]
    if not ops:
        return RepairResult(draft=copy.deepcopy(spec), manual=manual)
    draft, issues, _created = apply_ops(copy.deepcopy(spec), ops)
    if issues:
        return RepairResult(draft=copy.deepcopy(spec), issues=list(issues), manual=manual)
    validation = [
        issue for issue in draft_issues(draft, tool_names=tool_names)
        if "ERROR" in str(getattr(issue, "severity", "")).upper()
    ]
    if validation:
        return RepairResult(draft=copy.deepcopy(spec), issues=list(validation), manual=manual)
    return RepairResult(draft=draft, applied=applied, manual=manual)


def write_candidate(draft: dict[str, Any], root: str | None = None) -> str:
    return save_candidate(draft, root=root)


def verify_repair(
    draft: dict[str, Any],
    workflow: str,
    tasks: list[dict[str, Any]],
    model_factory: Any = None,
    scenario: str = "",
) -> list[dict[str, Any]]:
    """用修复后的 spec 复跑原失败任务：修复是否有效由结果回答。"""
    from core.evaluation.run import WorkflowRunner
    from core.evaluation.tasks import check_answer
    from core.scenarios.spec import ScenarioSpec

    spec = ScenarioSpec.model_validate(draft)
    runner = WorkflowRunner(
        scenario=scenario,
        workflow=workflow,
        spec=spec,
        model_factory=model_factory,
    )
    results = []
    for task in tasks:
        outcome = runner.run(str(task.get("input") or ""))
        passed, why = check_answer(task, outcome.answer)
        results.append({
            "task_id": task.get("id"),
            "passed": passed,
            "answer": outcome.answer,
            "reason": why,
            "error": outcome.error,
        })
    return results
