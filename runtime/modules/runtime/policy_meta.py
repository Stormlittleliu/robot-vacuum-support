# -*- coding: utf-8 -*-
"""策略元数据：slot、顺序不变量、中文说明（**纯数据，不 import langchain**）。

为什么单独一层：spec 校验只需要"有哪些策略名"，而 ``available_policies()`` 在
``pipeline.py`` 里、会连带 import LangChain 中间件。于是"校验一份 spec"被迫加载
整个运行时；换中间件集合时历史 spec 还会变非法。

这里只放名字与顺序语义；真正的策略构建器仍在 ``modules.runtime.pipeline``。
"""
from __future__ import annotations

from dataclasses import dataclass

# slot 越小越靠外。同一个 slot 内保持声明顺序。
SLOTS: dict[str, int] = {
    "permission": 0,
    "custom": 10,
    "hooks": 20,
    "planning": 30,
    "todo": 40,
    "summarization": 50,
    "pii": 60,
    "hitl": 70,
    "timeout": 80,
    "error": 90,
    "retry": 100,
    "limit": 110,
    "budget": 120,
    "fallback": 130,
    "model_retry": 140,
}

# 只校验真正影响语义的顺序，其余只管默认值。
INVARIANTS: tuple[tuple[str, str, str], ...] = (
    ("permission", "hitl", "权限层必须在审批外层：被拒绝的工具不该进入审批"),
    ("permission", "timeout", "权限层必须在超时外层：被拒绝的工具不该执行"),
    ("hitl", "timeout", "审批必须在超时之外：人思考的时间不算工具耗时"),
    ("error", "retry", "error 必须在 retry 外层：重试耗尽后才转成 error ToolMessage"),
    ("planning", "todo", "policy 先决定是否委派，再进入常规 todo 流程"),
)

# 分层的顺序就是"从外到内"的直觉顺序；UI 按这个顺序分组，不按 slot 排。
LAYERS: dict[str, int] = {
    "守卫层": 0,
    "上下文层": 1,
    "自定义": 2,
    "可靠性层": 3,
    "成本层": 4,
}


@dataclass(frozen=True)
class PolicyDoc:
    """给人和 UI 看的策略说明；与实现写在同一个文件，避免文档漂移。"""

    key: str
    label: str
    desc: str
    layer: str


POLICY_DOCS: dict[str, PolicyDoc] = {
    doc.key: doc
    for doc in (
        PolicyDoc(
            "permission",
            "工具权限",
            "按 allow / deny / 风险等级拦截工具调用；被拒绝的工具不会进入后面的链路",
            "守卫层",
        ),
        PolicyDoc(
            "hitl",
            "人工审批",
            "高风险工具调用前暂停，等人工批准或拒绝；人思考的时间不算工具耗时",
            "守卫层",
        ),
        PolicyDoc(
            "hooks",
            "生命周期钩子",
            "把场景注册的钩子接进 run：用户提示改写、停止回调等",
            "上下文层",
        ),
        PolicyDoc(
            "planning",
            "规划策略",
            "先按规则决定要不要委派或换工具，再进入常规流程",
            "上下文层",
        ),
        PolicyDoc(
            "todo",
            "任务清单",
            "复杂任务自动维护 write_todos，长任务有可检查的中间状态",
            "上下文层",
        ),
        PolicyDoc(
            "summarization",
            "上下文压缩",
            "历史超过阈值先摘要再送模型，避免上下文无限增长",
            "上下文层",
        ),
        PolicyDoc(
            "pii",
            "敏感信息脱敏",
            "输入里的敏感信息在送模型前替换成占位符",
            "上下文层",
        ),
        PolicyDoc(
            "timeout",
            "工具超时",
            "按工具配置执行时限，超时中断并回注错误，不让一个工具拖死整个 run",
            "可靠性层",
        ),
        PolicyDoc(
            "error",
            "异常回注",
            "工具异常转成中文 ToolMessage，让模型换一种方式继续",
            "可靠性层",
        ),
        PolicyDoc(
            "retry",
            "工具重试",
            "工具失败后重试一次，仍失败再交给 error 层转成消息",
            "可靠性层",
        ),
        PolicyDoc(
            "limit",
            "调用上限",
            "单次 run 的工具调用次数上限，防止无限循环",
            "可靠性层",
        ),
        PolicyDoc(
            "fallback",
            "模型回退",
            "主模型失败时切到备用模型",
            "可靠性层",
        ),
        PolicyDoc(
            "model_retry",
            "模型重试",
            "模型调用失败后重试一次",
            "可靠性层",
        ),
        PolicyDoc(
            "budget",
            "Token 预算",
            "默认关闭；显式配置后累计父子任务 token，达到预算时停止后续模型调用，已在途调用仍计入消耗",
            "成本层",
        ),
        PolicyDoc(
            "custom",
            "场景自定义",
            "场景自己注册的中间件，按声明顺序插在权限层之后",
            "自定义",
        ),
    )
}


def policy_names() -> list[str]:
    """spec 可以引用的策略名（纯数据；不依赖任何中间件实现）。

    ``custom`` 不是可配置策略（场景自定义中间件走 ``Scenario.middleware``），
    所以不在这里暴露。
    """
    return sorted(name for name in POLICY_DOCS if name != "custom")
