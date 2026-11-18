# Runtime / scene refactor v1（客服侧）

本文记录 2026-11-18 客服侧重构的契约、行为变化、生效策略、实测证据和缺项。范围只包含 `robot-vacuum-support` 的 `runtime/`、`robot_vacuum_support/`、测试与本文；没有修改编辑器仓库或 P2 的编码产品。

## 1. 执行契约

### 1.1 两种场景模式

- `agent`：普通 Agent 直接执行 `FrameworkAgent`，不构造 wrapper graph，不经过 `WorkflowRuntime`。
- `workflow`：保留真实 LangGraph；只有 workflow 才执行 `workflow_factory`。
- 旧 spec 没有显式 `mode` 时按结构保守推断：恰好一个 workflow、唯一 `react_loop`、恰好两条无条件边 `START → react_loop → END`，且 `stop_when` 缺省或为 `no_tool_calls` → `agent`；多节点、额外/条件边、`answer_has_citation` 等非默认停止语义 → `workflow`。没有图的原生配置仍为 `agent`。

`ScenarioSpec` 现在接受可选 `mode` 字段；`Scenario.mode` / `ExecutionSpec.mode` 保存解析结果。

### 1.2 普通单 `react_loop` spec 的无损映射

`robot_vacuum/v4.json` 仍是权威 spec，没有改 owner，也没有改 spec 内容。`build_from_spec` 在 `agent` 模式下把该节点映射为原生 Agent 配置：

| spec 字段 | 原生 Agent 落点 |
| --- | --- |
| `react_loop.args.tools` | `Scenario.tools` |
| `react_loop.args.prompt`（空则用场景提示词） | `Scenario.system_prompt` |
| `react_loop.args.max_steps` | `Scenario.tool_call_limit`（0 = 不启用） |
| `react_loop.args.stop_when` | 只有缺省/`no_tool_calls` 能投影到原生 Agent；`answer_has_citation` 等非默认语义整图保留 `workflow`，由 `FrameworkAgent` 节点回答后校验并写 `scratch.error`（validation gap，不改写成 provider error） |
| `harness.enable_summarization` | 场景 policy，原生 Agent 实际装配 `SummarizationMiddleware` |
| `harness.enable_todo` | 场景 policy |
| `harness.token_budget` | 场景 policy |

旧 `workflow` spec 仍保留 `workflow_factory`；`react_loop` 节点不再自己调用 `create_agent`，而是通过 `FrameworkAgent` 装配，保留节点边界、工具子集、prompt、max_steps、stop_when 和 trace 归属。

`robot_vacuum/v4.json` 的 `stop_when=answer_has_citation` 属于非默认停止语义，因此按此契约继续走 `workflow`，不再投影为 native Agent。`FrameworkAgent` 仍是唯一装配点，`robot_vacuum_support/execution.py` 的业务执行抽取保持不变。

## 2. 唯一装配点与 effective policy

### 2.1 FrameworkAgent

`runtime/modules/runtime/framework.py` 现在是唯一的 `create_agent` 调用点。`react_loop` 通过 `FrameworkAgent(..., node_overrides=..., trace_namespace=..., parent_trace_id=...)` 复用同一装配；`spec_nodes.py` 不再直接 `create_agent`。

节点装配默认关闭自己的 checkpoint（`checkpoint=False`），避免节点 checkpoint 污染 workflow 根 checkpoint；预算通过父 `RunBudget` 借用，不新建独立账本。

### 2.2 effective policy

`runtime/modules/runtime/pipeline.py` 输出 `effective_policies`，每项形如：

```json
{
  "name": "summarization",
  "enabled": true,
  "value": true,
  "source": "scenario_policy",
  "default": false,
  "args": {"model": "<runtime model>"},
  "slot": 50,
  "label": "上下文压缩"
}
```

`source` 只有三档，优先级从高到低：

1. `node_override`：仅真实 workflow 节点显式覆盖（例如 `react_loop.max_steps`、`react_loop.enable_summarization`）。
2. `scenario_policy`：`Scenario` 字段 / `spec.harness` / `spec.policies`。
3. `harness_default`：代码内唯一默认表 `_HARNESS_DEFAULTS`。

普通 Agent 没有节点覆盖层；单 `react_loop` 的配置在构建阶段已经落成场景字段，不再靠 wrapper graph 模拟节点覆盖。

已知差异修复：`enable_summarization=true` 在旧 `react_loop` 路径没有生效。现在原生 Agent 和 workflow 的 Agent 节点都会实际装配 summarization；这是行为变化，已纳入整产品验证，而不是只宣称“helper 已抽取”。

### 2.3 装配顺序

原有 slot / 不变量（权限最外、审批在超时外、error 在 retry 外）不变。`build_pipeline`、`describe_pipeline`、`describe_chain` 都从同一套 effective specs 派生，避免两套规则。

## 3. 生命周期、thread 与 checkpoint

- worker 通过 `AH_SESSION_ID` 获得会话身份；根 thread 为 `session-<id>`，同一会话的 stop/reopen 仍复用同一 checkpoint namespace。
- 普通 Agent 只在 checkpoint 为空时用最近 12 条 transcript 做一次 seed；checkpoint 已有消息时不再把 history 重复拼进 prompt。checkpoint 关闭时仍每轮使用最近 12 条。
- 真实错误会触发 checkpoint namespace 轮换（避免半写 checkpoint 污染下一轮），下一轮以 transcript 最近 12 条恢复；这是明确的错误恢复路径，不是静默成功。
- workflow 根 thread 为 `workflow-<session-id>`，同一会话的 stop/reopen 复用同一 thread；workflow 子 Agent 使用显式 namespace：`workflow:<name>/node:<node-id>`，带 `parent_trace_id`；trace 事件写 `namespace` / `parent_trace_id`。
- 预算以一次真实 model/tool request 计一次；子预算 `parent` 指向父预算，并用同一 `run_id` 去重，不重复计数。
- 部分中断、审批和 stream 行为保持：`interrupt` 仍通过 SSE `interrupt` 输出；审批恢复仍只恢复同一 live worker 的 pending runtime；typed failure 仍以 SSE `error` / `run_end error` 传播。

## 4. 客服业务执行入口

新增 `runtime/robot_vacuum_support/execution.py`，把原先散在 `server.py` 的业务生命周期收进场景侧：

- `prepare_run(store, session_id, body, ...) -> PreparedTurn`：语义抽取 → 事实更新 → 同一 `decision_snapshot` → `prompt_context` → 路由/direct answer；返回 worker request body。
- `prepare_resume(...)`：审批事件持久化 + 同一 snapshot 的 context。
- `finish_run(...)`：助手建议、来源证据 review、turn metrics、状态持久化。
- `fail_run(...)`：typed error 的 retrieval 证据与显式 error 行，不伪造 completed。

`server.py` 现在只负责 HTTP/SSE 传输、session 鉴权/存储门禁、handoff 和 worker 管理；不再内联 interpretation、reducer、snapshot、direct-answer 或 post-suggestion 逻辑。`support_state` 和 `decision_snapshot` 仍只有一份，prompt、routing 和答案后处理读同一个对象。

## 5. API / 行为差异

- HTTP 路由和 SSE 事件名不变：`answer` / `answer_delta` / `interrupt` / `handoff` / `error` / `done`。`/api` 与 `/api/v1` 双路由不变。
- 普通 Agent 不再生成 workflow wrapper trace / node 事件；`/api/instance` 的 workflow 信息仍为 `agent`。
- `effective_policies` 新增在 direct-agent 的 worker result 中，并可由 `FrameworkAgent.last_effective_policies` / `pipeline.effective_policies()` 读取；未改变旧字段。
- workflow 节点使用 `FrameworkAgent` 后，`react_loop` 的流式输出改为 `stream_mode="values"` + 去重，输出文本语义保持；节点仍有独立 trace namespace。
- 错误分类保持：provider/model 错误仍映射到 typed `code`；approval 失效仍是 HTTP 410 `approval_gone`；failed turn 的 `last_status=error`。
- `Server` 的 generic fallback（没有 `robot_vacuum_support` 的 artifact）仍可用，只是没有业务状态/证据 review。

### 5.1 P2 最终 API 对齐核对

- `FrameworkAgent(scenario)` 仍兼容位置参数；`invoke()` / `execute_stream()` 保留。
- workflow 子 Agent 节点：checkpoint 关闭，thread 使用显式节点 namespace（`workflow:<name>/node:<id>`），带 parent trace。
- **native 识别已与 P2 契约对齐**：只有 exact single `react_loop` + `START/END` 直连 + 无额外/条件边 + 默认 `stop_when=no_tool_calls` 才走 native agent；`answer_has_citation` 等非默认停止语义继续 `workflow`。
- `robot_vacuum/v4.json` 保持原内容（`stop_when=answer_has_citation`），因此现在走 `workflow`；节点仍由 `FrameworkAgent` 统一装配，业务执行抽取仍在 `runtime/robot_vacuum_support/execution.py`。
- 显式 `mode=agent` 与上述结构冲突时由 `agent_contract_issues` 报错，不静默丢节点。该差异已在本次改动和最终 artifact 整产品验证中消除。

## 6. 实测

完整整产品入口（非组件）使用最终构建的完整 artifact：

- 已验证 artifact：`robot_vacuum-v4-532c735eec`（`run.py --check` ok）
- 入口：`serve.py --instance-config`，正常 HTTP/SSE，`http://127.0.0.1:18082`
- 隔离：`outputs/runtime-refactor-v2/storage`，独立实例配置；18080 未启动、未触碰
- 模型：复用既有有效 `outputs/runtime-refactor-v1/instance.env` 的 `DEEPSEEK_API_KEY`，值未打印；实例另生成隔离 access/admin token
- 冻结：`eval/development-next/three-mainlines-v1` 的 12 cases / 18 turns 原样运行，并发 2，case/suite hash 与预期未改

结果（详见 `outputs/runtime-refactor-v2/report.json` 与 `closeout.json`，不入库）：

- 12 cases / 18 turns 全部执行：11 个 `passed_structural`，1 个 `failed_structural`。失败点是 `ml-recharge-normal-closure` 第二轮 `retrieval_attempted=false`；冻结预期未改。此前同样失败过的 `ml-suction-correction-third-party` 本轮结构通过（第二轮 retrieval attempted=true）；这是单次实际结果，不改写为缺陷已修复。
- checkpoint/restart：2 个额外用户 turn，中间真实停止并重启；`thread_id` 前后同为 `workflow-s-2909ea1266664004`，transcript 为 `user/assistant/user/assistant`，第二问保留“我叫小明”的历史，无重复用户消息。
- typed error：approval 失效返回 HTTP 410 `approval_gone`；另一个隔离整产品实例用无效 provider key 触发 SSE `error`，`code=model_auth_failed`、`requires_configuration=true`、`last_status=error`、trace `run_end status=error`。该探针是错误路径检查，不计入 QA 成功率。
- handoff：真实 HTTP 用户端/管理端生命周期通过（申请、排队、接手、回复幂等、结案）。
- 全量 pytest：340 passed / 8 failed / 26 subtests passed。失败集中在 `tests/test_eval_selection.py`（2）与 `tests/test_eval_v1.py`（6），涉及 dataset README quote / state-runner 等检查；本轮没有对 base 版本做同口径复跑，因此只记为未证既存/未解决，不武断归因于本次 runtime 改动。上一轮记录的 manual-review UI selection flake 本轮通过，未复现。
- Docker：已有 engine 不可用且按约束未启动，所以没有镜像缓存构建、compose 或独立 Docker volume 证据。

`robot_vacuum-v4-d30692614a` 的中断复跑记录不再作为最终证据；最终结论只绑定上述 `532c735eec` 的本次运行。

## 7. 缺项与边界

- 本机已有 Docker engine 不可用；按任务约束没有启动 Docker Desktop、daemon 或 GUI，因此没有新镜像缓存构建，也没有 Docker compose 运行。已用完整 artifact 的正常 HTTP/SSE 入口在独立端口/存储上做整产品验证；Docker 镜像/volume 证据仍缺。
- 12 cases 是开发集，不是 holdout，不是成功率；语义判断仍需人工，不把结构通过写成语义正确。
- 冻结 suite/cases 预期未改；本轮 1 条 `retrieval_attempted` 失败原样保留。此前同样失败、本轮通过的另一条只作为单次实测记录，不写成缺陷修复。
- 全量 pytest 8 项失败没有做 base 对比，不能证明“既存”，按未证既存/未解决保留；本次不据此归因。
- 未做 40–60 集扩展、真实用户量、SLA、线上部署；没有外部队列工单。

## 8. 复现

```powershell
$env:PYTHONPATH = "runtime"
python -m pytest tests/test_runtime_refactor.py -q

# 构建新 artifact（已有目录不覆盖）
python -c "import sys; sys.path.insert(0, 'runtime'); from modules.artifacts.project import build_project; import json; print(json.dumps(build_project('.', r'..\robot-vacuum-support-built', include_assets=True), ensure_ascii=False))"

# 冻结 12 case / 18 turn + 2 checkpoint turns + 错误路径 + 人工接管
python scripts/run_runtime_refactor_acceptance.py --artifact <artifact> --env-file outputs/runtime-refactor-v1/instance.env --out-dir outputs/runtime-refactor-v2
```

证据目录 `outputs/runtime-refactor-v2/` 不提交。
