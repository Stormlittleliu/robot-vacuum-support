# Runtime / scene refactor v1（客服侧）

本文记录 2026-11-18 客服侧重构的契约、行为变化、生效策略、实测证据和缺项。范围只包含 `robot-vacuum-support` 的 `runtime/`、`robot_vacuum_support/`、测试与本文；没有修改编辑器仓库或 P2 的编码产品。

## 1. 执行契约

### 1.1 两种场景模式

- `agent`：普通 Agent 直接执行 `FrameworkAgent`，不构造 wrapper graph，不经过 `WorkflowRuntime`。
- `workflow`：保留真实 LangGraph；只有 workflow 才执行 `workflow_factory`。
- 旧 spec 没有显式 `mode` 时按结构保守推断：恰好一个 workflow、唯一 `react_loop`、恰好两条无条件边 `START → react_loop → END`，且 `stop_when` 缺省或为 `no_tool_calls` → `agent`；多节点、额外/条件边、`answer_has_citation` 等非默认停止语义 → `workflow`。没有图的原生配置仍为 `agent`。

`ScenarioSpec` 现在接受可选 `mode` 字段；`Scenario.mode` / `ExecutionSpec.mode` 保存解析结果。显式 `mode` 只接受 `agent` / `workflow`，`workflow` 必须有实际图；`ExecutionSpec` 在存在 `workflow_factory` 时派生为 `workflow`，`Scenario` 与执行面同步，不再由两处默认 `agent` 互相掩盖。

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

本轮（2026-11-18）把策略解析收敛到 `pipeline._resolved_specs`：`FrameworkAgent` 用 `build_pipeline_with_policies` 一次拿到真实中间件和审计行，`effective_policies()` 走同一入口，不再维护第二份默认表。`max_steps=0` 表示“无节点覆盖、继承场景 `harness.tool_call_limit`”，正值才覆盖；显式 `spec.policies[].enabled=false` 保持关闭。细节与实测见 5.2、6.1。

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
- 普通 Agent 不再生成 workflow wrapper trace / node 事件；`/api/instance` 的 workflow 信息仍为 `agent`。这是客服侧 `mode=agent` 扩展的行为，不是 P2 共享字段。
- `effective_policies` 新增在 direct-agent 的 worker result 中，并可由 `FrameworkAgent.last_effective_policies` / `pipeline.effective_policies()` 读取；未改变旧字段。其值须由真实装配派生，且该字段本身是 P1 扩展，P2 / 编辑器不识别。
- workflow 节点使用 `FrameworkAgent` 后，`react_loop` 的流式输出改为 `stream_mode="values"` + 去重，输出文本语义保持；节点仍有独立 trace namespace。
- 错误分类保持：provider/model 错误仍映射到 typed `code`；approval 失效仍是 HTTP 410 `approval_gone`；failed turn 的 `last_status=error`。
- `Server` 的 generic fallback（没有 `robot_vacuum_support` 的 artifact）仍可用，只是没有业务状态/证据 review。

### 5.1 P2 共享兼容差异核对（复核更正）

结论：**只到最小调用兼容，不是 API / 字段完全一致**。此前“`FrameworkAgent(scenario)` 兼容、`invoke()` / `execute_stream()` 保留”被表述成“API 一致”，现更正为下述有限兼容。当时（更正轮）只更新本记录，未改 runtime、未跑产品；修复见 5.2 / 6.1。

> 2026-11-18 后续：本节第 3、4 项已按 5.2 修复；下面保留修复前的原文与结论，不删除历史记录。

1. **共享最小 API 与客服扩展**：P2 共享最小 API 只有 `FrameworkAgent(scenario)`、`invoke()`、`execute_stream()`。客服侧 `FrameworkAgent.__init__` 还接受可选 `execution` / `tools` / `system_prompt` / `middleware` / `checkpointer` / `response_format` / `stop_when` / `node_overrides` / `trace_namespace` / `parent_trace_id`，这些是 P1 内部扩展，编辑器 / P2 没有对应字段。因此只保证“用 `scenario` 构造 + 调 `invoke` / `execute_stream`”这条最小路径可用；传扩展 kwargs 或依赖其语义不属于对 P2 的兼容承诺。
2. **`ExecutionSpec` / `Scenario.mode` 等字段**：客服 `ExecutionSpec` / `Scenario.mode` 默认 `agent`，以及 `stop_when`、`effective_policies`，都是 P1 扩展；编辑器 / P2 无这些字段，分发仍按 `workflow_factory` 走 factory，不读 `mode`。因此不能称为字段一致，也不能假设 P2 会识别 `mode`。
3. **`max_steps=0` 零保留语义（2026-11-18 已修复）**：修复前，客服 `runtime/modules/scenario_format/spec.py:296` 在显式 `max_steps <= 0` 时把 `tool_call_limit` 置 `None`，清掉 `harness.tool_call_limit`；`spec_nodes.py:1260` 同时构造 `enabled=false` 的 `limit` node override（`reason: react_loop.max_steps=0 disables the tool-call limit`）。这与 P2 “零保留场景限制”的冲突曾是真实执行路径。现状见 5.2：0 表示“无节点覆盖、继承场景限制”，不再清空或合成 disabled override。
4. **effective policy 与 mode 来源（2026-11-18 已修复）**：修复前，`effective_policies` 与 `build_pipeline` 各自从默认表重算，`mode` 有 spec 显式值、结构推断、节点构造等多处来源，`Scenario.mode` / `ExecutionSpec.mode` 的默认 `agent` 可能掩盖 workflow。现状见 5.2：两者共用一个 resolved specs 入口；mode 由规范化 spec / `workflow_factory` 派生并校验。

未解决兼容项与下一步：

- 第 1–2 项仍是兼容边界：只保证最小调用路径，P1 扩展 kwargs / 字段不被 P2 识别。第 3–4 项已按 5.2 修复，并通过完整 artifact 的 HTTP/SSE 入口验证；不再列为未解决。
- 需在整产品入口（真实 artifact + HTTP/SSE）复验后才判断兼容范围；组件级最小调用通过不等于整产品兼容。5.2 已补做本轮整产品验证。
- `max_steps=0` 的取舍已定为：0 = 无节点覆盖、继承场景 `harness.tool_call_limit`；正值覆盖；负值由既有参数 schema（`minimum=0`）拒绝；显式 `spec.policies[].enabled=false` 保留关闭。停止 / 预算语义本轮没有扩展。
- 客服内部仍有 workflow 子 Agent 节点差异：checkpoint 关闭，thread 使用 `workflow:<name>/node:<id>` namespace 并带 parent trace；这是客服侧实现，不属于 P2 共享契约。

### 5.2 策略对齐修复（2026-11-18）

本轮只改实际执行路径，保留 P1 内部扩展，不要求 P2 增加公共字段：

1. **零值继承 / 正值覆盖 / 负值拒绝**：`spec.py` 的 agent 分支在 `max_steps=0`（或字段缺省）时使用 `harness.tool_call_limit`，正值使用节点值；workflow 分支不再因为存在 `react_loop` 就丢掉 `harness.tool_call_limit`。`spec_nodes.react_loop` 只在 `max_steps > 0` 时生成 `limit` node override；0 不生成 override，也不再生成旧的 `enabled=false` 合成覆盖。负值仍由现有参数 schema 的 `minimum=0` 拒绝，不做 clamp。
2. **显式关闭保留**：`spec.policies` 中 `{"name":"limit","enabled":false,"reason":...}` 仍是 `scenario_policy` 层关闭；`max_steps=0` 没有节点 override，因此不会把默认 0 重新解释成开启。正值节点覆盖仍按既有优先级高于场景层；本轮显式关闭配置使用 `max_steps=0`，没有把“显式关闭 + 正值覆盖”组合成同一场景。
3. **mode 一致派生与校验**：`spec_check` 拒绝非法 `mode` 和“`mode=workflow` 但没有 workflow 图”；`build_from_spec` 把同一个规范化 mode 写入 `Scenario` 和 `ExecutionSpec`。`ExecutionSpec.__post_init__` 在存在 `workflow_factory` 时派生为 `workflow`，`Scenario.__post_init__` 与执行面同步，避免默认 `agent` 掩盖 workflow。`workflow_node` 只保留为内部执行面值。
4. **effective policy 单源**：`pipeline._resolved_specs` 是默认表、场景层、显式 overrides、节点覆盖的唯一解析入口；`FrameworkAgent` 通过 `build_pipeline_with_policies` 一次拿到真实 `middleware` 和审计行，`effective_policies()` 也走同一入口，不再先建 pipeline 再从另一份默认表重算。显式传入自定义 `middleware` 时，审计行仍来自同一 policy 解析，但不声称覆盖任意自定义中间件的内部状态。
5. **可观测性边界**：workflow 节点级有效策略以节点 Agent 的 trace `middleware_decision` 为准；worker result 里的 `effective_policies` 仍是场景级投影，不作为节点 override 证据。

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

### 6.1 2026-11-18 策略对齐整产品验证

- 临时工程：`policy-alignment-work/case-inherit`、`case-override`、`case-disable`，每个都是完整产品副本；只隔离 data / storage / 端口 / 凭据配置，未拆组件。
- 产物：三个临时工程分别通过正式 `save_spec` 从 v4 保存 v5，再执行 `build_project(..., include_assets=True)`：
  - `robot_vacuum-v5-f6d613defd`（0 继承）
  - `robot_vacuum-v5-851b6e5b2b`（正值覆盖）
  - `robot_vacuum-v5-658735553c`（显式关闭）
  - 三个产物 `run.py --check` 均 `ok=true`、`service_ready=true`。
- 入口：`serve.py --instance-config`，正常 HTTP/SSE，`http://127.0.0.1:18083`；storage 为 `outputs/policy-alignment-v1/storage/<case>`。18080 / 18082 未启动、未触碰。
- 模型：复用 `outputs/runtime-refactor-v1/instance.env` 的合法凭据，未打印值；每个配置只跑 1 个真实模型 turn。

三种配置的实际 trace（节点 `workflow:agent/node:agent` 的 `middleware_decision`）：

| 配置 | spec | effective `limit` | 顶层工具调用 | 检索结果 | turn |
| --- | --- | --- | --- | --- | --- |
| 0 继承 | `harness.tool_call_limit=6`，`max_steps=0` | `enabled=true`，`source=scenario_policy`，`run_limit=6` | 2 | 5 | answered |
| 正值覆盖 | `harness.tool_call_limit=6`，`max_steps=2` | `enabled=true`，`source=node_override`，`run_limit=2` | 2 | 6 | answered |
| 显式关闭 | `harness.tool_call_limit=6`，`max_steps=0`，`policies=[limit.enabled=false]` | `enabled=false`，`source=scenario_policy` | 2 | 8 | answered |

工具计数是顶层 Agent 工具调用（排除 `ToolSpec` 包装产生的 `spec-*` 内层 trace）；原始 trace 同时记录包装内外两层，不能直接当成 Agent 工具次数。正值覆盖配置达到 `2/2`，但没有观察到被阻断的调用，因此只证明 effective `run_limit=2` 实际装配，不宣称“限制阻断已证”。

主线回归只跑 2 cases / 4 turns（inherit 产物）：

- `ml-recharge-normal-closure`：turn 1 `retrieval_status=ok`、有引用；turn 2 为 `not_retrieved`，与 12 案运行中同一条已知结构失败一致，冻结预期未改，不归因于本轮策略改动。
- `ml-suction-correction-third-party`：两轮 `retrieval_status=ok`，均有引用。
- 4 个 turn 均 `answered`；这是结构观察，不是语义成功率。

定向与相邻回归：`tests/test_runtime_refactor.py` 17 passed；`tests/test_startup_profile.py`、`tests/test_demo_readiness.py` 11 passed；`tests/test_product_closed_loop.py`、`tests/test_p1_recharge.py` 39 passed / 24 subtests passed。未重新跑全量 pytest。

验证口径：上述 3 个配置各 1 个真实 turn + 2 个主线 case / 4 turn，是完整 artifact 的正常 `serve.py` HTTP/SSE 验证，**已跑**；组件测试只作定位辅助，不能顶替整产品验收。**未跑**：完整 12 案、Docker/容器、全量 pytest、语义成功率、限制阻断证明。

Docker：本轮没有启动 Docker Desktop、daemon 或 GUI，因此没有容器验证。上表是完整 artifact 的正常 `serve.py` 整产品入口，不等于容器 / compose 证据。

## 7. 缺项与边界

- 本轮没有启动 Docker Desktop、daemon 或 GUI，因此没有镜像缓存构建、compose 或独立 Docker volume 证据。完整 artifact 的 `serve.py` HTTP/SSE 验证仍是整产品入口，但不是容器验证。
- 12 cases 是开发集，不是 holdout，不是成功率；语义判断仍需人工，不把结构通过写成语义正确。本轮只复跑 2 cases / 4 turns 做主线回归，没有跑完整 12 案。
- 冻结 suite/cases 预期未改；`ml-recharge-normal-closure` 第二轮 `retrieval_status=not_retrieved` 仍原样保留。它在本轮 2-case 回归中再次出现；`ml-suction-correction-third-party` 两轮检索均 ok。这是单次实测，不写成缺陷已修复。
- 策略对齐的 3 个配置各只跑 1 个真实模型 turn；正值覆盖达到 `2/2` 但没有观察到被阻断的调用，因此“限制阻断”未证明，只证明了 effective policy 与真实装配一致。显式关闭配置没有与“正值节点覆盖”组合跑同一场景。
- workflow 的节点级有效策略以节点 trace 为准；worker result 的 `effective_policies` 仍是场景级投影。不能拿场景级字段替代节点 override 证据。
- 全量 pytest 未在本轮重跑；历史 8 项失败没有做 base 对比，不能证明“既存”，按未证既存/未解决保留。本轮定向与相邻测试通过只证明被测试行为。
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

2026-11-18 策略对齐证据在 `outputs/policy-alignment-v1/`（不提交）：`report.json`、`policy-summary.json`、每个配置的 SSE 原始事件与 trace；三个完整 artifact 在本地 `policy-alignment-built/`。
