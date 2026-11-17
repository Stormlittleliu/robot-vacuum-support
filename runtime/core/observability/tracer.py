# -*- coding: utf-8 -*-
"""本地 trace：记录一次 run 的模型/工具调用、token 与耗时。

不依赖 LangSmith / OpenTelemetry；每次调用追加写入
`outputs/traces/trace_YYYYMMDD.jsonl`。一行一个事件，便于 grep 和后续聚合。

用法：
    tracer = TraceCallbackHandler(trace_id="...", thread_id="...")
    agent.invoke(..., config={"configurable": {"thread_id": ...}, "callbacks": [tracer]})
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from langchain_core.callbacks import BaseCallbackHandler

from core.observability.budget import RunBudget, context_scope, current_budget
from core.observability import latency
from core.observability.context import segments_from_messages
from core.observability.redact import redact_value
from core.llm.errors import provider_error_summary, safe_exception_summary
from core.paths import OUTPUT_ROOT, output_path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRACE_DIR = "outputs/traces"
_WRITE_LOCK = threading.Lock()


def _load_config() -> dict:
    path = ROOT / "config" / "observability.yml"
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


class TraceCallbackHandler(BaseCallbackHandler):
    """把一个 run 的模型/工具事件写成 JSONL。"""

    def __init__(
        self,
        trace_id: str,
        thread_id: str | None = None,
        budget: RunBudget | None = None,
        scenario: str | None = None,
    ):
        self.trace_id = trace_id
        self.thread_id = thread_id
        self.budget = budget
        self.scenario = scenario
        self.started_at = time.time()
        self.model_calls = 0
        self.tool_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.cost_est = 0.0
        #: Per-model-call totals with the graph node.  Used to separate the
        #: answer layer from tool-internal model calls (e.g. RAG summarize) so
        #: the same tokens are not counted again by a tool meter.
        self.model_events: list[dict[str, Any]] = []

        self._model_start: dict[str, float] = {}
        self._tool_start: dict[str, float] = {}
        self._tool_names: dict[str, str] = {}
        # 结束事件拿不到 metadata，所以开始事件里先把节点名 / 上下文作用域记住。
        # 异步结束时 current_budget 可能已变化，不能靠结束时刻重新推断。
        self._model_node: dict[str, str | None] = {}
        self._tool_node: dict[str, str | None] = {}
        self._model_scope: dict[str, dict[str, Any]] = {}
        self._tool_scope: dict[str, dict[str, Any]] = {}
        self._tool_input: dict[str, dict[str, Any]] = {}
        self._tool_call_ids: dict[str, str] = {}
        self._last_model = "unknown"
        self._finalized = False
        self._model_span: dict[str, Any] = {}
        self._model_span_token: dict[str, Any] = {}
        self._tool_span: dict[str, Any] = {}
        self._tool_span_token: dict[str, Any] = {}
        self._run_span = latency.open_span(
            "main.run",
            call_id=None,
            scenario=scenario,
            thread_id=thread_id,
            trace_id=trace_id,
        )

        config = _load_config()
        configured = str(config.get("trace_dir") or DEFAULT_TRACE_DIR)
        candidate = Path(configured)
        explicit_output = bool(os.environ.get("AH_OUTPUT_ROOT", "").strip())
        if candidate.is_absolute():
            self._trace_dir = candidate
        elif explicit_output:
            # Artifact / instance / session worker: writable tree is explicit.
            if configured.replace("\\", "/").startswith("outputs/"):
                self._trace_dir = output_path(
                    configured.replace("\\", "/")[len("outputs/"):]
                )
            else:
                self._trace_dir = OUTPUT_ROOT / candidate
        else:
            # Compatibility for the repository default and tests patching ROOT.
            self._trace_dir = ROOT / candidate
        self._pricing = config.get("pricing") or {}

    # ===================== 写入 =====================
    def _write(self, event: str, **fields) -> None:
        row = redact_value({
            "trace_id": self.trace_id,
            "thread_id": self.thread_id,
            "scenario": self.scenario,
            "event": event,
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            **fields,
        })
        try:
            with _WRITE_LOCK:
                self._trace_dir.mkdir(parents=True, exist_ok=True)
                path = self._trace_dir / f"trace_{datetime.now():%Y%m%d}.jsonl"
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            # trace 不能影响主流程
            pass

    def _scope(self) -> dict[str, Any]:
        """优先当前 run 的 budget，回退到本 tracer 绑定的 budget。"""
        return context_scope(current_budget() or self.budget)

    def record_decision(self, *, middleware: str, decision: str, **fields) -> None:
        """共享观测 sink 的落点；只记录调用方真实拿到的 decision。"""
        self._write(
            "middleware_decision",
            middleware=middleware,
            decision=decision,
            **fields,
        )

    @staticmethod
    def _summarize(value: Any, limit: int = 500) -> dict[str, Any]:
        if value is None:
            text = ""
        elif isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except Exception:
                text = str(value)
        preview = text[:limit].replace("\r", "")
        if len(text) > limit:
            preview += "…"
        return {"preview": preview, "chars": len(text)}

    @staticmethod
    def _tool_input_fields(input_str: Any, inputs: Any) -> dict[str, Any]:
        """工具的输入是调用参数，不是聊天上下文；旧字段保留字符串形态。"""
        if isinstance(inputs, dict):
            summary = TraceCallbackHandler._summarize(inputs)
            return {
                "input": input_str,
                "input_args": summary["preview"],
                "input_kind": "tool_arguments",
                "input_chars": summary["chars"],
            }
        summary = TraceCallbackHandler._summarize(input_str)
        return {
            "input": input_str,
            "input_args": summary["preview"],
            "input_kind": "tool_arguments",
            "input_chars": summary["chars"],
        }

    @staticmethod
    def _tool_call_id(kwargs: dict, output: Any = None) -> str | None:
        metadata = kwargs.get("metadata") or {}
        candidate = (
            kwargs.get("tool_call_id")
            or (metadata.get("tool_call_id") if isinstance(metadata, dict) else None)
            or getattr(output, "tool_call_id", None)
        )
        return str(candidate) if candidate else None

    # ===================== 模型事件 =====================
    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        self.model_calls += 1
        mono_start = time.perf_counter_ns()
        wall_start = time.time_ns()
        self._model_start[str(run_id)] = time.time()
        model = self._model_name(serialized, kwargs)
        self._last_model = model
        node = self._node_of(kwargs)
        scope = self._scope()
        self._model_node[str(run_id)] = node
        self._model_scope[str(run_id)] = scope
        call_span = latency.open_span(
            "model.call",
            node=node,
            model=model,
            layer="main_agent" if node != "tools" else "rag_summary",
            parent_run_id=str(parent_run_id) if parent_run_id else None,
        )
        self._model_span[str(run_id)] = call_span
        if call_span is not None:
            self._model_span_token[str(run_id)] = latency.push_call_span(call_span.span_id)
        batch = messages[0] if messages and isinstance(messages[0], (list, tuple)) else (messages or [])
        segments = segments_from_messages(batch)
        self._write(
            "model_start",
            run_id=str(run_id),
            parent_run_id=str(parent_run_id) if parent_run_id else None,
            model=model,
            messages=len(batch),
            node=node,
            mono_start_ns=mono_start,
            wall_start_ns=wall_start,
            call_span_id=call_span.span_id if call_span else None,
            **scope,
        )
        self._write(
            "context_snapshot",
            run_id=str(run_id),
            parent_run_id=str(parent_run_id) if parent_run_id else None,
            step=self.model_calls,
            model=model,
            node=node,
            messages=len(batch),
            tokens_est=sum(item["tokens_est"] for item in segments),
            segments=segments,
            **scope,
        )

    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs):
        start = self._model_start.pop(str(run_id), None)
        mono_end = time.perf_counter_ns()
        wall_end = time.time_ns()
        node = self._model_node.pop(str(run_id), None) or self._node_of(kwargs)
        scope = self._model_scope.pop(str(run_id), None) or self._scope()
        latency_ms = int((time.time() - start) * 1000) if start else None
        prompt, completion, total = self._extract_usage(response)
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += total
        if self.budget is not None:
            self.budget.add(total, event_id=str(run_id))

        model = self._model_name_from_response(response) or self._last_model
        cost = self._estimate_cost(model, prompt, completion)
        if not cost and self._last_model != model:
            # 响应里的模型名可能和配置名不同（如 deepseek-flash vs deepseek-chat）
            cost = self._estimate_cost(self._last_model, prompt, completion)
        self.cost_est += cost
        self.model_events.append(
            {
                "run_id": str(run_id),
                "model": model,
                "node": node,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
                "latency_ms": latency_ms,
            }
        )
        del self.model_events[:-50]
        call_span = self._model_span.pop(str(run_id), None)
        if call_span is not None:
            call_span.end(
                status="ok",
                model=model,
                node=node,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
            )
        token = self._model_span_token.pop(str(run_id), None)
        if token is not None:
            latency.pop_call_span(token)
        self._write(
            "model_end",
            run_id=str(run_id),
            parent_run_id=str(parent_run_id) if parent_run_id else None,
            model=model,
            latency_ms=latency_ms,
            mono_start_ns=getattr(call_span, "start_mono_ns", None),
            mono_end_ns=mono_end,
            wall_start_ns=getattr(call_span, "start_wall_ns", None),
            wall_end_ns=wall_end,
            call_span_id=getattr(call_span, "span_id", None),
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            cost_est=round(cost, 6),
            node=node,
            **scope,
        )

    def on_llm_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        start = self._model_start.pop(str(run_id), None)
        node = self._model_node.pop(str(run_id), None) or self._node_of(kwargs)
        scope = self._model_scope.pop(str(run_id), None) or self._scope()
        latency_ms = int((time.time() - start) * 1000) if start else None
        call_span = self._model_span.pop(str(run_id), None)
        if call_span is not None:
            call_span.end(status="error", error=safe_exception_summary(error))
        token = self._model_span_token.pop(str(run_id), None)
        if token is not None:
            latency.pop_call_span(token)
        self._write(
            "model_error",
            run_id=str(run_id),
            parent_run_id=str(parent_run_id) if parent_run_id else None,
            model=self._last_model,
            latency_ms=latency_ms,
            call_span_id=getattr(call_span, "span_id", None),
            error=provider_error_summary(error),
            node=node,
            **scope,
        )

    # ===================== 工具事件 =====================
    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs):
        self.tool_calls += 1
        name = (serialized or {}).get("name", "unknown")
        mono_start = time.perf_counter_ns()
        wall_start = time.time_ns()
        self._tool_names[str(run_id)] = name
        self._tool_start[str(run_id)] = time.time()
        node = self._node_of(kwargs)
        scope = self._scope()
        self._tool_node[str(run_id)] = node
        self._tool_scope[str(run_id)] = scope
        input_fields = self._tool_input_fields(input_str, kwargs.get("inputs"))
        self._tool_input[str(run_id)] = input_fields
        tool_call_id = self._tool_call_id(kwargs)
        if tool_call_id:
            self._tool_call_ids[str(run_id)] = tool_call_id
        tool_span = latency.open_span(
            "tool.call",
            tool=name,
            node=node,
            tool_call_id=tool_call_id,
            parent_run_id=str(parent_run_id) if parent_run_id else None,
        )
        self._tool_span[str(run_id)] = tool_span
        if tool_span is not None:
            self._tool_span_token[str(run_id)] = latency.push_active_span(tool_span.span_id)
        self._write(
            "tool_start",
            run_id=str(run_id),
            parent_run_id=str(parent_run_id) if parent_run_id else None,
            tool=name,
            tool_call_id=tool_call_id,
            node=node,
            mono_start_ns=mono_start,
            wall_start_ns=wall_start,
            tool_span_id=tool_span.span_id if tool_span else None,
            **input_fields,
            **scope,
        )

    def on_tool_end(self, output, *, run_id, parent_run_id=None, **kwargs):
        name = self._tool_names.pop(str(run_id), "unknown")
        start = self._tool_start.pop(str(run_id), None)
        mono_end = time.perf_counter_ns()
        wall_end = time.time_ns()
        node = self._tool_node.pop(str(run_id), None) or self._node_of(kwargs)
        scope = self._tool_scope.pop(str(run_id), None) or self._scope()
        input_fields = self._tool_input.pop(str(run_id), {})
        started_call_id = self._tool_call_ids.pop(str(run_id), None)
        tool_call_id = self._tool_call_id(kwargs, output) or started_call_id
        latency_ms = int((time.time() - start) * 1000) if start else None
        status = getattr(output, "status", "success") or "success"
        content = getattr(output, "content", output)
        summary = self._summarize(content)
        tool_span = self._tool_span.pop(str(run_id), None)
        if tool_span is not None:
            tool_span.end(status="ok", latency_ms=latency_ms, output_chars=summary["chars"])
        token = self._tool_span_token.pop(str(run_id), None)
        if token is not None:
            latency.pop_active_span(token)
        self._write(
            "tool_end",
            run_id=str(run_id),
            parent_run_id=str(parent_run_id) if parent_run_id else None,
            tool=name,
            tool_call_id=tool_call_id,
            latency_ms=latency_ms,
            mono_start_ns=getattr(tool_span, "start_mono_ns", None),
            mono_end_ns=mono_end,
            wall_start_ns=getattr(tool_span, "start_wall_ns", None),
            wall_end_ns=wall_end,
            tool_span_id=getattr(tool_span, "span_id", None),
            status=status,
            node=node,
            output=summary["preview"],
            output_preview=summary["preview"],
            output_chars=summary["chars"],
            output_kind="tool_result",
            **input_fields,
            **scope,
        )

    def on_tool_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        name = self._tool_names.pop(str(run_id), "unknown")
        start = self._tool_start.pop(str(run_id), None)
        node = self._tool_node.pop(str(run_id), None) or self._node_of(kwargs)
        scope = self._tool_scope.pop(str(run_id), None) or self._scope()
        input_fields = self._tool_input.pop(str(run_id), {})
        tool_call_id = self._tool_call_id(kwargs) or self._tool_call_ids.pop(str(run_id), None)
        latency_ms = int((time.time() - start) * 1000) if start else None
        summary = self._summarize(str(error))
        tool_span = self._tool_span.pop(str(run_id), None)
        if tool_span is not None:
            tool_span.end(status="error", error=f"{type(error).__name__}: {str(error)[:160]}")
        token = self._tool_span_token.pop(str(run_id), None)
        if token is not None:
            latency.pop_active_span(token)
        self._write(
            "tool_error",
            run_id=str(run_id),
            parent_run_id=str(parent_run_id) if parent_run_id else None,
            tool=name,
            tool_call_id=tool_call_id,
            latency_ms=latency_ms,
            tool_span_id=getattr(tool_span, "span_id", None),
            status="error",
            error=str(error),
            output=summary["preview"],
            output_preview=summary["preview"],
            output_chars=summary["chars"],
            output_kind="tool_error",
            node=node,
            **input_fields,
            **scope,
        )

    # ===================== run 结束 =====================
    def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs):
        if parent_run_id is None:
            status = "interrupted" if isinstance(outputs, dict) and "__interrupt__" in outputs else "completed"
            self.finalize(status)

    def on_chain_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        if parent_run_id is None:
            self.finalize("error", error=safe_exception_summary(error))

    def finalize(self, status: str = "completed", error: str | None = None) -> None:
        """写 run_end；重复调用只写一次。"""
        if self._finalized:
            return
        self._finalized = True
        if self.budget is not None and self.budget.exceeded and status == "completed":
            status = "budget_exceeded"
        if self._run_span is not None:
            self._run_span.end(
                status=status,
                error=error,
                model_calls=self.model_calls,
                tool_calls=self.tool_calls,
                total_tokens=self.total_tokens,
            )
        self._write(
            "run_end",
            status=status,
            error=error,
            model_calls=self.model_calls,
            tool_calls=self.tool_calls,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.total_tokens,
            duration_ms=int((time.time() - self.started_at) * 1000),
            run_span_id=getattr(self._run_span, "span_id", None),
            cost_est=round(self.cost_est, 6),
            budget_limit=self.budget.max_tokens if self.budget else None,
            budget_used=self.budget.total_tokens if self.budget else None,
        )

    def finalize_if_needed(self) -> None:
        """在 invoke/stream 的 finally 中兜底，避免中断时没有 run_end。"""
        self.finalize("unknown")

    # ===================== 解析辅助 =====================
    @staticmethod
    def _node_of(kwargs: dict) -> str | None:
        """从回调 metadata 里取 LangGraph 节点名。

        LangGraph 会给节点内的每个 run 打上 ``langgraph_node``；拿不到时返回
        ``None``（例如直接调 agent 之外的模型），界面会显示"—"而不是瞎猜。
        """
        metadata = kwargs.get("metadata") or {}
        node = metadata.get("langgraph_node") if isinstance(metadata, dict) else None
        return str(node) if node else None

    @staticmethod
    def _model_name(serialized, kwargs: dict) -> str:
        params = kwargs.get("invocation_params") or {}
        return (
            params.get("model")
            or params.get("model_name")
            or (serialized or {}).get("name", "unknown")
        )

    @staticmethod
    def _model_name_from_response(response) -> str | None:
        llm_output = getattr(response, "llm_output", None) or {}
        return llm_output.get("model_name") or llm_output.get("model")

    @staticmethod
    def _extract_usage(response) -> tuple[int, int, int]:
        """兼容 OpenAI 的 token_usage 与 LangChain 的 usage_metadata。"""
        llm_output = getattr(response, "llm_output", None) or {}
        usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
        if not usage:
            try:
                message = response.generations[0][0].message
                usage = getattr(message, "usage_metadata", None) or {}
            except Exception:
                usage = {}
        prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        total = usage.get("total_tokens") or (prompt + completion)
        return int(prompt), int(completion), int(total)

    def _estimate_cost(self, model: str, prompt: int, completion: int) -> float:
        """按 config/observability.yml 的价格估算；未配置时返回 0。"""
        price = self._pricing.get(model) or {}
        if not price:
            return 0.0
        return (
            prompt / 1_000_000 * float(price.get("input_per_1m", 0))
            + completion / 1_000_000 * float(price.get("output_per_1m", 0))
        )
