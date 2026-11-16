# -*- coding: utf-8 -*-
"""按需创建空白 spec 场景。

仓库不再内置或自动创建任何保存的场景；只有用户在编辑器里点"新建"时，
才按空白 Agent / 空白工作流模板写一份新 spec。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.paths import SPEC_ROOT
from core.scenarios import pools
from modules.scenario_format.edges import END, START
from modules.scenario_format.spec import (
    NodeSpec,
    ScenarioSpec,
    WorkflowSpec,
    save_spec,
)

BLANK_AGENT = "blank-agent"
BLANK_WORKFLOW = "blank-workflow"
TEMPLATES = (BLANK_AGENT, BLANK_WORKFLOW)

_AGENT_PROMPT = "你是通用助手。根据用户输入回答；需要时使用已提供的工具，不要编造工具结果。"
_WORKFLOW_PROMPT = "你是通用助手。根据用户输入回答。"


def _blank_agent_spec(name: str, tools: list[str], description: str) -> ScenarioSpec:
    return ScenarioSpec(
        name=name,
        description=description,
        tools=tools,
        system_prompt=_AGENT_PROMPT,
        workflows={
            "agent": WorkflowSpec(
                nodes=[
                    NodeSpec(
                        id="agent",
                        fn="react_loop",
                        args={
                            "prompt": "",
                            "tools": tools,
                            "max_steps": 0,
                            "stop_when": "no_tool_calls",
                        },
                    )
                ],
                edges=[[START, "agent"], ["agent", END]],
            )
        },
    )


def _blank_workflow_spec(name: str, description: str) -> ScenarioSpec:
    return ScenarioSpec(
        name=name,
        description=description,
        tools=[],
        system_prompt=_WORKFLOW_PROMPT,
        workflows={
            "workflow": WorkflowSpec(
                nodes=[NodeSpec(id="answer", fn="llm_answer", args={"prompt": ""})],
                edges=[[START, "answer"], ["answer", END]],
            )
        },
    )


def create_from_template(
    name: str,
    template: str = BLANK_AGENT,
    *,
    root: str | Path = SPEC_ROOT,
) -> dict[str, Any]:
    """创建一份空白场景 spec；已存在则报错，不覆盖。"""
    directory = Path(root) / name
    if directory.exists():
        raise FileExistsError(f"场景已存在：{name}")
    description = "从编辑器空白模板创建"
    if template == BLANK_AGENT:
        spec = _blank_agent_spec(name, pools.tool_names("office"), description)
        pool = "office"
    elif template == BLANK_WORKFLOW:
        spec = _blank_workflow_spec(name, description)
        pool = "office"
    else:
        raise ValueError(f"未知模板：{template}（可用：{', '.join(TEMPLATES)}）")
    save_spec(spec, root=root, expected_version=None)
    (directory / ".pool").write_text(pool, encoding="utf-8")
    return {"name": name, "template": template}
