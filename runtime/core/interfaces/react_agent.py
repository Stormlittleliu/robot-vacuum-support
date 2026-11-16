"""通用入口兼容层：按名字加载 spec 场景，交给通用 ReAct 运行时。

框架与场景的分离方式：
- core/agent/framework.py：通用 ReAct 运行时，不 import 具体场景工具；
- data/specs/<name>/：按需创建的场景 spec；
- 本文件只负责"按名字取场景 + 交给框架"，不再直接组装工具。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agent.framework import FrameworkAgent
from core.scenarios.registry import get_scenario


class ReactAgent:
    def __init__(self, scenario_name: str = ""):
        self.scenario = get_scenario(scenario_name)
        self.framework = FrameworkAgent(self.scenario)
        # 兼容旧代码：直接访问 .agent
        self.agent = self.framework.agent

    def execute_stream(self, query: str, thread_id: str = "default"):
        yield from self.framework.execute_stream(query, thread_id=thread_id)

    def resume(self, thread_id: str, decisions: list[dict] | None = None):
        return self.framework.resume(thread_id, decisions=decisions)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description="通用 ReAct 入口")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--input", default="你好")
    args = parser.parse_args()
    agent = ReactAgent(args.scenario)
    for chunk in agent.execute_stream(args.input):
        print(chunk, end="", flush=True)

