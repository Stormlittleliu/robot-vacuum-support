# -*- coding: utf-8 -*-
"""Subagent：父 Agent 派生子 Agent，子 Agent 使用全新上下文。

设计要点：
- 子 Agent 只拿基础工具，默认排除 subagent 自身，避免递归；
- 子 Agent 不继承父对话 / checkpoint，只返回最终摘要；
- 通过场景层 RuntimeConfig.subagent 配置 allowed_tools / 预算 / 超时。
"""
from __future__ import annotations

import uuid
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from concurrent.futures import TimeoutError as FutureTimeout

from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from modules.contracts.agent import ExecutionSpec, Scenario
from modules.runtime.framework import FrameworkAgent
from modules.runtime.middleware import LimitStepsMiddleware
from core.observability.budget import context_hint
from core.runtime.config import RuntimeConfig, SubagentConfig


def _answer_text(messages) -> str:
    for message in reversed(messages or []):
        if isinstance(message, AIMessage):
            content = message.content
            if isinstance(content, list):
                content = "".join(
                    str(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            if isinstance(content, str) and content.strip():
                return content.strip()
    return ""


def select_subagent_tools(parent_tools, config: SubagentConfig):
    """子 Agent 工具：排除 subagent 自身；有 allowed_tools 时只保留白名单。

    名字与白名单判断走 ``ToolSpec``（框架无关描述），返回的仍是父工具对象。
    """
    from core.tools.adapters.langchain import as_spec

    specs = [as_spec(tool) for tool in parent_tools]
    names = [spec.name for spec in specs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"子 Agent 工具名重复：{', '.join(duplicates)}")
    allowed = set(config.allowed_tools) if config.allowed_tools else None
    return [
        tool for tool, spec in zip(parent_tools, specs)
        if spec.name != config.name and (allowed is None or spec.name in allowed)
    ]


@dataclass(frozen=True)
class SubagentPreset:
    """一个语义化 subagent 工具预设；复用同一个 executor。"""

    tool_name: str
    description: str
    system_prompt: str
    allowed_tools: tuple[str, ...] = field(default_factory=tuple)
    max_model_calls: int | None = None
    token_budget: int | None = None
    timeout_seconds: float | None = None

    def config(self) -> SubagentConfig:
        return SubagentConfig(
            name=self.tool_name,
            description=self.description,
            system_prompt=self.system_prompt,
            allowed_tools=list(self.allowed_tools),
            max_model_calls=self.max_model_calls,
            token_budget=self.token_budget,
            timeout_seconds=self.timeout_seconds,
        )


_GENERIC_ALIASES = {"subagent", "task", "delegate", "agent"}


def validate_subagent_presets(presets: list[SubagentPreset], *, max_aliases: int = 5) -> None:
    """语义化 alias 的静态约束，避免工具爆炸和泛化命名。"""
    if len(presets) > max_aliases:
        raise ValueError(f"subagent alias 超过 {max_aliases} 个")
    names = [item.tool_name.strip() for item in presets]
    if any(not name for name in names):
        raise ValueError("subagent alias 不能为空")
    if len(names) != len(set(names)):
        raise ValueError(f"subagent alias 重名：{names}")
    generic = sorted(set(names) & _GENERIC_ALIASES)
    if generic:
        raise ValueError(f"subagent alias 不能使用泛化名：{generic}")


def build_subagent_tools(
    model,
    parent_tools,
    presets: list[SubagentPreset],
    parent_runtime: RuntimeConfig | None = None,
    expose: list[str] | None = None,
):
    """一个 executor，多个语义 alias；expose 是场景级白名单。"""
    validate_subagent_presets(presets)
    names = [item.tool_name for item in presets]
    if expose is not None:
        unknown = sorted(set(expose) - set(names))
        if unknown:
            raise ValueError(f"未知 subagent alias：{unknown}")
        wanted = set(expose)
        presets = [item for item in presets if item.tool_name in wanted]
    return [
        build_subagent_tool(model, parent_tools, item.config(), parent_runtime)
        for item in presets
    ]


def build_subagent_tool(
    model,
    parent_tools,
    config: SubagentConfig,
    parent_runtime: RuntimeConfig | None = None,
):
    child_tools = select_subagent_tools(parent_tools, config)
    child_runtime = None
    if parent_runtime and parent_runtime.hooks:
        child_runtime = RuntimeConfig(hooks={
            event: list(callbacks)
            for event, callbacks in parent_runtime.hooks.items()
        })

    @tool(config.name, description=config.description)
    def subagent(description: str) -> str:
        child = Scenario(
            name=f"{config.name}_child",
            description="subagent",
            system_prompt=config.system_prompt,
            tools=child_tools,
            context={},
            cases_path=None,
            high_risk_tools={},
            fallback_model=None,
            enable_todo=False,
            enable_summarization=False,
            tool_call_limit=None,
            token_budget=config.token_budget,
            pii_types=[],
            tool_policy={"default": "allow"},
            tool_timeout_seconds=60,
            runtime=child_runtime,
            execution=ExecutionSpec(
                model=model,
                middleware=[LimitStepsMiddleware(config.max_model_calls)] if config.max_model_calls else [],
                checkpoint=False,
            ),
        )
        agent = FrameworkAgent(child)
        thread_id = f"subagent-{uuid.uuid4().hex}"
        # 不用 with：超时后 with 会 shutdown(wait=True)，仍然阻塞到子线程结束，
        # 让 timeout 形同虚设。这里只放弃等待，线程本身无法被 Python 强杀。
        pool = ThreadPoolExecutor(max_workers=1)
        timeout = config.timeout_seconds
        try:
            # 只给小上下文打标签；parent budget 与 token 统计仍由 FrameworkAgent
            # 的 current_budget() 继承，不在父子之间复制或重算。
            with context_hint(
                context_label=f"subagent:{config.name}",
                origin_tool=config.name,
            ):
                future = pool.submit(copy_context().run, agent.invoke, description, thread_id)
            state = future.result(timeout=timeout or None)
        except (FutureTimeout, TimeoutError):
            if timeout is None:
                return "Subagent 执行超时（未配置等待上限），任务未完成。"
            return f"Subagent 超时（>{timeout:.0f}s），任务未完成。"
        except Exception as exc:  # noqa: BLE001 - 子 Agent 失败不拖垮父 Agent
            return f"Subagent 执行失败：{type(exc).__name__}: {exc}"
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        answer = _answer_text(state.get("messages", []))
        if not answer:
            return "Subagent 没有返回文本结论。"
        return answer

    subagent.metadata = {**(subagent.metadata or {}), "timeout_seconds": config.timeout_seconds or 0}
    return subagent
