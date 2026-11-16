# -*- coding: utf-8 -*-
"""失败诊断：把"一条任务没通过"翻译成 workflow/node/tool 的可行动结论。

评分可以交给 Inspect，但"为什么失败、该改哪里"必须由我们自己的语义层回答。
诊断只依赖任务判据 + attribution，不依赖模型，因此可离线测试。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.evaluation.tasks import REFUSAL_HINTS


@dataclass
class Diagnosis:
    code: str
    message: str
    severity: str = "medium"  # low / medium / high
    blame: dict[str, str] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    manual: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "blame": dict(self.blame),
            "evidence": list(self.evidence),
            "manual": self.manual,
        }


def _steps(attribution: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list((attribution or {}).get("steps") or [])


def _nodes(attribution: dict[str, Any] | None) -> list[str]:
    return [str(item) for item in (attribution or {}).get("nodes") or []]


def _first_node(attribution: dict[str, Any] | None, *names: str) -> str:
    nodes = _nodes(attribution)
    for name in names:
        if name in nodes:
            return name
    return nodes[-1] if nodes else ""


def _evidence_for(attribution: dict[str, Any] | None, *, node: str = "", tool: str = "") -> list[str]:
    ids = []
    for step in _steps(attribution):
        if node and step.get("node") == node:
            ids.append(str(step.get("step_id") or ""))
        elif tool and step.get("tool") == tool:
            ids.append(str(step.get("step_id") or ""))
    return [item for item in ids if item]


def _has_refusal(text: str) -> bool:
    return any(hint in text for hint in REFUSAL_HINTS)


def diagnose_task(
    task: dict[str, Any],
    answer: str,
    passed: bool,
    attribution: dict[str, Any] | None,
    run_error: str = "",
) -> list[Diagnosis]:
    """一条任务的全部诊断，按严重度排序。"""
    context = {
        "workflow": str((attribution or {}).get("workflow") or task.get("workflow") or ""),
    }
    results: list[Diagnosis] = []

    if run_error:
        last = _steps(attribution)[-1] if _steps(attribution) else {}
        node = str(last.get("node") or "")
        results.append(Diagnosis(
            code="workflow_error",
            message=f"工作流执行失败：{run_error}",
            severity="high",
            blame={**context, "node": node},
            evidence=_evidence_for(attribution, node=node) if node else [],
        ))
        return results

    for error in (attribution or {}).get("errors") or []:
        name = str(error.get("step") or "")
        step = next(
            (
                item for item in _steps(attribution)
                if item.get("name") == name or item.get("tool") == name
            ),
            {},
        )
        kind = str(step.get("kind") or "")
        results.append(Diagnosis(
            code="tool_error" if kind == "tool" else "node_error",
            message=f"{kind or '步骤'} {name} 报错：{error.get('error') or ''}",
            severity="high",
            blame={
                **context,
                "node": str(step.get("node") or ""),
                "tool": str(step.get("tool") or ""),
            },
            evidence=[str(step.get("step_id") or "")],
        ))

    text = str(answer or "")
    expect_refusal = bool(task.get("expect_refusal"))
    if passed:
        # 通过也要给质量提示：分数由 Inspect 判，质量由诊断层补充。
        if expect_refusal or _has_refusal(text):
            return []
        if "[" in (task.get("expect_contains") or []) and "[" not in text:
            node = _first_node(attribution, "cite", "answer", "write", "review")
            results.append(Diagnosis(
                code="missing_citation",
                message="回答缺少 [来源id] 引用（判定通过，但引用质量不足）",
                severity="low",
                blame={**context, "node": node},
                evidence=_evidence_for(attribution, node=node),
            ))
        expected = [item for item in task.get("expect_any") or [] if item not in text]
        if expected and not results:
            node = _first_node(attribution, "answer", "write", "review")
            results.append(Diagnosis(
                code="missing_content",
                message=f"回答没有覆盖期望着关键词：{', '.join(expected)}",
                severity="low",
                blame={**context, "node": node},
                evidence=_evidence_for(attribution, node=node),
            ))
        return results

    if expect_refusal:
        if not _has_refusal(text):
            node = _first_node(attribution, "branch", "retrieve", "answer")
            results.append(Diagnosis(
                code="refusal_missing",
                message="应当拒答，但回答没有明确说明资料不足",
                severity="medium",
                blame={**context, "node": node},
                evidence=_evidence_for(attribution, node=node),
                manual=True,
            ))
        return results

    if _has_refusal(text):
        node = _first_node(attribution, "branch", "retrieve", "answer")
        results.append(Diagnosis(
            code="unexpected_refusal",
            message="不应拒答，但回答退化成资料不足",
            severity="medium",
            blame={**context, "node": node},
            evidence=_evidence_for(attribution, node=node),
            manual=True,
        ))
        return results

    if "[" in (task.get("expect_contains") or []) and "[" not in text:
        node = _first_node(attribution, "cite", "answer", "write", "review")
        results.append(Diagnosis(
            code="missing_citation",
            message="回答缺少 [来源id] 引用",
            severity="medium",
            blame={**context, "node": node},
            evidence=_evidence_for(attribution, node=node),
        ))

    expected = [item for item in task.get("expect_any") or [] if item not in text]
    if expected and not results:
        node = _first_node(attribution, "answer", "write", "review")
        results.append(Diagnosis(
            code="missing_content",
            message=f"回答没有覆盖期望着关键词：{', '.join(expected)}",
            severity="medium",
            blame={**context, "node": node},
            evidence=_evidence_for(attribution, node=node),
        ))

    if not (attribution or {}).get("tools") and "[" in (task.get("expect_contains") or []):
        node = _first_node(attribution, "retrieve")
        results.append(Diagnosis(
            code="no_retrieval",
            message="任务要求引用来源，但整次运行没有调用检索工具",
            severity="high",
            blame={**context, "node": node},
            evidence=_evidence_for(attribution, node=node),
        ))

    if not results:
        node = _first_node(attribution, "answer", "write", "review")
        results.append(Diagnosis(
            code="answer_mismatch",
            message="回答没有满足成功判据",
            severity="low",
            blame={**context, "node": node},
            evidence=_evidence_for(attribution, node=node),
        ))
    return results
