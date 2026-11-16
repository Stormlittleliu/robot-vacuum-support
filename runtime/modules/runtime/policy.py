# -*- coding: utf-8 -*-
"""策略对象与注册表：名字 / slot / 中文说明 / 构建器在同一处。

之前这三类信息分散在 `SLOTS`、`POLICY_DOCS` 和 `pipeline._BUILDERS` 三个 dict 里，
同一个字符串要改三处。现在合成一个 ``Policy``：

- 本模块纯数据 + 可调用引用，**不 import langchain**；
- 真正的 LangChain 中间件类由 ``pipeline`` 里的 builder 返回，属于 adapter 侧；
- ``Scenario.policies`` 只引用策略名，不持有实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from modules.runtime.policy_meta import POLICY_DOCS, SLOTS, PolicyDoc

PolicyFactory = Callable[[Any, dict[str, Any]], Any]


@dataclass(frozen=True)
class Policy:
    """一个可装配策略的声明。"""

    name: str
    slot: int
    label: str
    desc: str
    layer: str
    factory: PolicyFactory | None = None

    def build(self, scenario: Any, args: dict[str, Any] | None = None) -> Any:
        if self.factory is None:
            raise TypeError(f"策略 {self.name} 没有绑定构建器")
        return self.factory(scenario, dict(args or {}))

    @property
    def doc(self) -> PolicyDoc:
        return PolicyDoc(self.name, self.label, self.desc, self.layer)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slot": self.slot,
            "label": self.label,
            "desc": self.desc,
            "layer": self.layer,
        }


class PolicyError(ValueError):
    """策略注册/查找失败。"""


_REGISTRY: dict[str, Policy] = {}


def register(policy: Policy) -> Policy:
    if policy.name in _REGISTRY:
        raise PolicyError(f"策略重复注册：{policy.name}")
    _REGISTRY[policy.name] = policy
    return policy


def register_builder(name: str) -> Callable[[PolicyFactory], PolicyFactory]:
    """注册一个策略构建器；slot 与中文说明从 ``policy_meta`` 取。"""

    def _wrap(fn: PolicyFactory) -> PolicyFactory:
        doc = POLICY_DOCS.get(name)
        if doc is None:
            raise PolicyError(f"策略 {name} 缺少 POLICY_DOCS 文案")
        if name not in SLOTS:
            raise PolicyError(f"策略 {name} 缺少 slot")
        register(Policy(
            name=name,
            slot=SLOTS[name],
            label=doc.label,
            desc=doc.desc,
            layer=doc.layer,
            factory=fn,
        ))
        return fn

    return _wrap


def get(name: str) -> Policy:
    policy = _REGISTRY.get(str(name))
    if policy is None:
        raise PolicyError(f"未知策略：{name}")
    return policy


def find(name: str) -> Policy | None:
    return _REGISTRY.get(str(name))


def names() -> list[str]:
    return sorted(_REGISTRY)


def all_policies() -> list[Policy]:
    return [_REGISTRY[name] for name in names()]
