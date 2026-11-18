"""Agent 框架层：只依赖 Scenario 抽象，不 import 任何具体业务工具。

在通用 ReAct 运行时之上，用 LangChain 1.x 内置中间件补齐 Harness 能力：
- TodoListMiddleware：复杂任务规划（参考 learn-claude-code s05/s12）
- SummarizationMiddleware：上下文压缩（s08）
- PIIMiddleware：敏感信息脱敏（s03）
- HumanInTheLoopMiddleware：高风险工具审批（s03）
- ToolRetry / ModelRetry / ModelFallback / ToolError：错误恢复（s11）
- ToolCallLimit：工具调用上限，防止循环
- ToolPermission / ToolTimeout：工具权限与执行超时
- TokenBudget：单次 run 的累计 token 上限（含工具内部模型调用）
- InMemorySaver / SqliteSaver：会话记忆与 checkpoint（s09）
- TraceCallbackHandler：模型/工具/token/耗时写入本地 trace
"""
import re
import time
import uuid
from modules.runtime.limits import engine_recursion_limit, token_limit

from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langgraph.types import Command

from modules.runtime.checkpoint import build_checkpointer
from modules.contracts.agent import ExecutionSpec, Scenario
from modules.runtime.hooks import HookManager
from modules.runtime.pipeline import (
    build_pipeline_with_policies,
    effective_policies,
)
from core.observability.budget import (
    RunBudget,
    budget_scope,
    current_budget,
    current_context_hint,
)
from core.observability.decisions import root_tracer_scope
from core.observability.tracer import TraceCallbackHandler
from modules.contracts.tool import ToolSpec

CITATION_PATTERN = r"\[[^\]]+\]"


def check_stop_when(stop_when: str | None, answer: str) -> str | None:
    """Return the validation gap for a stop condition, without failing the turn."""
    if str(stop_when or "no_tool_calls") == "answer_has_citation":
        if not re.search(CITATION_PATTERN, str(answer or "")):
            return "回答缺少 [来源] 引用"
    return None


def normalize_tools(tools) -> list:
    """装配边界：``ToolSpec`` 转回当前框架的工具对象，原对象原样通过。

    这是 S1 的"最后一道缝"：场景层可以逐步改成返回 ``ToolSpec``，而
    ``create_agent`` 仍然拿到它认识的工具；两边不需要同时切换。
    """
    if not tools:
        return []
    from core.tools.adapters.langchain import to_langchain

    return [
        to_langchain(tool) if isinstance(tool, ToolSpec) else tool
        for tool in tools
    ]


class FrameworkAgent:
    """通用的 ReAct Agent 运行时，也是唯一的 ``create_agent`` 装配点。

    业务差异全部由 Scenario 提供；Harness 能力由本层统一装配。Workflow 的
    ``react_loop`` 节点同样通过本类装配，只是传入节点级 tools / prompt /
    limit 覆盖和显式 trace namespace，不再自己调用 ``create_agent``。
    """

    def __init__(
        self,
        scenario: Scenario,
        *,
        execution: ExecutionSpec | None = None,
        tools: list | None = None,
        system_prompt: str | None = None,
        middleware: list | None = None,
        checkpointer: object | None = None,
        response_format: object | None = None,
        stop_when: str | None = None,
        node_overrides: list | None = None,
        trace_namespace: str | None = None,
        parent_trace_id: str | None = None,
    ):
        self.scenario = scenario
        self.last_thread_id = None
        self.last_trace_id = None
        self.last_interrupt = None
        self.last_usage_summary = None
        self.last_effective_policies: list[dict] = []
        self.trace_namespace = trace_namespace
        self.parent_trace_id = parent_trace_id
        self._pending_budgets = {}
        runtime = getattr(scenario, "runtime", None)
        self.hook_manager = HookManager(getattr(runtime, "hooks", None))
        # 活对象从 execution 取；旧场景没有 execution 时由 resolve_execution 回退。
        resolve = getattr(scenario, "resolve_execution", None)
        self.execution = execution or (
            resolve() if callable(resolve) else ExecutionSpec(model=getattr(scenario, "model", None))
        )
        self.stop_when = str(
            stop_when
            if stop_when is not None
            else getattr(self.execution, "stop_when", None) or "no_tool_calls"
        )

        # 装配逻辑统一在 modules.runtime.pipeline：slot 决定顺序，spec 的 policies
        # 只做场景级覆盖；node_overrides 只允许真实 workflow 节点传入。
        # effective_policies 与 middleware 来自同一次解析，避免审计行与实际装配分叉。
        if middleware is not None:
            self.middleware = middleware
            self.effective_policies = effective_policies(
                scenario,
                self.hook_manager,
                self.execution,
                node_overrides=node_overrides,
            )
        else:
            self.middleware, self.effective_policies = build_pipeline_with_policies(
                scenario,
                self.hook_manager,
                self.execution.policies,
                execution=self.execution,
                node_overrides=node_overrides,
            )
        self.last_effective_policies = list(self.effective_policies)
        if checkpointer is not None:
            self.checkpointer = checkpointer
        else:
            self.checkpointer = (
                build_checkpointer(self.execution.checkpoint_backend, self.execution.checkpoint_path)
                if self.execution.checkpoint
                else None
            )
        self.tools = normalize_tools(
            tools if tools is not None else getattr(scenario, "tools", None)
        )
        self.system_prompt = (
            system_prompt
            if system_prompt is not None
            else getattr(scenario, "system_prompt", "")
        )
        self.agent = create_agent(
            model=self.execution.model or getattr(scenario, "model", None),
            system_prompt=self.system_prompt,
            tools=self.tools,
            middleware=self.middleware,
            checkpointer=self.checkpointer,
            response_format=response_format
            if response_format is not None
            else self.execution.response_format,
        )

    @staticmethod
    def _config(
        thread_id: str,
        tracer: TraceCallbackHandler | None = None,
        namespace: str | None = None,
    ) -> dict:
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": engine_recursion_limit()}
        if tracer is not None:
            config["callbacks"] = [tracer]
            config["metadata"] = {
                "trace_id": tracer.trace_id,
                "thread_id": thread_id,
                "trace_namespace": namespace or getattr(tracer, "namespace", None),
                "parent_trace_id": getattr(tracer, "parent_trace_id", None),
            }
        return config

    def _start_trace(self, thread_id: str, budget: RunBudget | None = None) -> TraceCallbackHandler:
        tracer = TraceCallbackHandler(
            trace_id=f"run-{uuid.uuid4().hex}",
            thread_id=thread_id,
            budget=budget,
            scenario=self.scenario.name,
            namespace=self.trace_namespace,
            parent_trace_id=self.parent_trace_id,
        )
        self.last_trace_id = tracer.trace_id
        return tracer

    def _run_context(self, budget: RunBudget) -> dict:
        context = dict(self.scenario.context)
        context["_run_budget"] = budget
        return context

    def _new_budget(self) -> RunBudget:
        hint = current_context_hint()
        return RunBudget(
            token_limit(self.scenario),
            parent=current_budget(),
            context_label=hint.get("context_label"),
            origin_tool=hint.get("origin_tool"),
        )

    def invoke_tool(self, name: str, args: dict | None = None):
        """能力层入口：直接调用场景工具，不经过模型与中间件。

        工具可以是 LangChain 工具（返回 ToolMessage）或 ``ToolSpec``
        （返回 ``ToolResult``）；两者都有 ``.content`` / ``.artifact``，
        能力层评测不需要知道当前工具用哪种表示。
        仅用于工具级评测/调试；权限、HITL、超时等 Harness 逻辑只在 `invoke` 中生效。
        """
        tool = self.scenario.get_tool(name)
        if isinstance(tool, ToolSpec):
            return tool(args or {})
        return tool.invoke({
            "name": name,
            "args": args or {},
            "id": f"invoke-{uuid.uuid4().hex}",
            "type": "tool_call",
        })

    def invoke(self, query: str, thread_id: str | None = None):
        """执行一次对话；thread_id 相同时会保留历史（checkpoint 记忆）。

        不传 thread_id 时生成一次性 ID，保证测试/批量调用互不污染。
        """
        thread_id = thread_id or f"run-{uuid.uuid4().hex}"
        self.last_thread_id = thread_id
        rewritten = self.hook_manager.trigger("UserPromptSubmit", query)
        if isinstance(rewritten, str) and rewritten.strip():
            query = rewritten
        budget = self._new_budget()
        self.last_budget = budget
        tracer = self._start_trace(thread_id, budget)
        try:
            with root_tracer_scope(tracer), budget_scope(budget):
                result = self.agent.invoke(
                    {"messages": [{"role": "user", "content": query}]},
                    config=self._config(thread_id, tracer),
                    context=self._run_context(budget),
                )
            self.hook_manager.trigger("Stop", result.get("messages", []))
            if result.get("__interrupt__"):
                self._pending_budgets[thread_id] = budget
            else:
                self._pending_budgets.pop(thread_id, None)
            return result
        finally:
            tracer.finalize_if_needed()

    def resume(self, thread_id: str, decisions: list[dict] | None = None):
        """恢复被 human-in-the-loop 中断的 run。

        decisions 形如 [{"type": "approve"}] 或 [{"type": "reject", "message": "..."}]。
        """
        decisions = decisions or [{"type": "approve"}]
        self.last_thread_id = thread_id
        budget = self._pending_budgets.get(thread_id) or self._new_budget()
        self.last_budget = budget
        tracer = self._start_trace(thread_id, budget)
        try:
            with root_tracer_scope(tracer), budget_scope(budget):
                result = self.agent.invoke(
                    Command(resume={"decisions": decisions}),
                    config=self._config(thread_id, tracer),
                    context=self._run_context(budget),
                )
            self.hook_manager.trigger("Stop", result.get("messages", []))
            if result.get("__interrupt__"):
                self._pending_budgets[thread_id] = budget
            else:
                self._pending_budgets.pop(thread_id, None)
            return result
        finally:
            tracer.finalize_if_needed()

    def get_state(self, thread_id: str):
        return self.agent.get_state(self._config(thread_id))

    def execute_stream(self, query: str, thread_id: str = "default"):
        self.last_thread_id = thread_id
        self.last_interrupt = None
        self.last_answer = ""
        self.last_stop_error = None
        rewritten = self.hook_manager.trigger("UserPromptSubmit", query)
        if isinstance(rewritten, str) and rewritten.strip():
            query = rewritten
        budget = self._new_budget()
        self.last_budget = budget
        tracer = self._start_trace(thread_id, budget)
        tracer.record_decision(
            middleware="effective_policy",
            decision="assembled",
            policies=self.effective_policies,
        )
        input_dict = {
            "messages": [
                {"role": "user", "content": query},
            ]
        }
        seen_message_ids: set[str] = set()
        last_messages: list = []
        pieces: list[str] = []

        try:
            with root_tracer_scope(tracer), budget_scope(budget):
                for chunk in self.agent.stream(
                    input_dict,
                    config=self._config(thread_id, tracer, self.trace_namespace),
                    stream_mode="values",
                    context=self._run_context(budget),
                ):
                    # values 模式在中断时会单独给出 __interrupt__，界面需要据此发起审批
                    if "__interrupt__" in chunk:
                        self.last_interrupt = chunk["__interrupt__"]

                    messages = chunk.get("messages") or []
                    if not messages:
                        continue
                    last_messages = messages
                    latest_message = messages[-1]

                    # 只输出模型回复：跳过 HumanMessage、ToolMessage 和只有工具调用的空 AI 消息
                    if not isinstance(latest_message, AIMessage):
                        continue

                    # values 模式每步都会重放整个 state，同一条 AI 消息会出现多次，按 id 去重
                    message_id = getattr(latest_message, "id", None)
                    if message_id:
                        if message_id in seen_message_ids:
                            continue
                        seen_message_ids.add(message_id)

                    content = latest_message.content
                    if isinstance(content, list):
                        content = "".join(
                            str(block.get("text", ""))
                            for block in content
                            if isinstance(block, dict) and block.get("type") == "text"
                        )
                    if isinstance(content, str) and content.strip():
                        pieces.append(content.strip())
                        yield content.strip() + "\n"
        finally:
            self.last_answer = "\n".join(pieces).strip()
            self.last_stop_error = check_stop_when(self.stop_when, self.last_answer)
            if self.last_interrupt:
                self._pending_budgets[thread_id] = budget
            else:
                self._pending_budgets.pop(thread_id, None)
            self.last_usage_summary = {
                "model_calls": tracer.model_calls,
                "tool_calls": tracer.tool_calls,
                "prompt_tokens": tracer.prompt_tokens,
                "completion_tokens": tracer.completion_tokens,
                "total_tokens": tracer.total_tokens,
                "cost_est": tracer.cost_est,
                "latency_ms": int((time.time() - tracer.started_at) * 1000),
                "budget_total_tokens": budget.total_tokens,
                "model_events": list(tracer.model_events),
            }
            self.hook_manager.trigger("Stop", last_messages)
            tracer.finalize_if_needed()
