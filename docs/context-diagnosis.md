# 工作语境 + 歧义 + 渐进诊断（IP-1 开发切片）

> 日期：2026-11-13（香港）  
> 状态：development 纵向切片已实现并构建；P1-02 仍 `in_progress`，不是 holdout 结果，不是成功率。  
> 结构化报告：[outputs/context-diagnosis-report.json](standalone.md#未包含)  
> 真实短场景：[outputs/context-diagnosis-real-run.json](standalone.md#未包含)

## 1. 本轮解决什么

把“回充/找不到充电座/回不去”这类自然表述从**单一正则判词**改成两层：

1. **语义层**：`runtime/robot_vacuum_support/interpretation.py` 产出受 schema 校验的 `turn-interpretation/v1`，描述领域、对象、经验者、可观察症状、歧义候选、已执行操作、交互阶段、熟悉度、信息来源和未知项。
2. **事实层**：`state.py` 的确定性 reducer 只接受能追溯到用户原文的事实，负责作用域、否定/更正、幂等、建议/执行/反馈分离和状态转移。

LLM 产出只是 proposal，不是事实；正则仍保留在 fallback 与显式短语适配层，但不再声称它是“理解主层”。

## 2. 关键契约

### 2.1 typed work context

- `domain` / `topic` / `objects` / `experiencer`
- `observable_symptom.text` / `safety`
- `ambiguity`：例如 `dock_find_referent` 的 `robot_cannot_return` / `user_cannot_find_dock` 候选
- `executed_actions` / `not_tried_actions`：必须有用户原文 `evidence_quote`
- `interaction_stage` / `familiarity` / `closure_signal`
- `information_sources` / `unknowns`
- 每个案例的 `work_context.source` 记录消息、轮次、抽取方式

### 2.2 reducer 只校验事实

- `evidence_quote` 必须是当前用户消息的子串，否则丢弃。
- 用户“我做了/试过/已经…”才算执行；助手建议永远不写成用户已做。
- 否定/更正按 clause 作用域；动作之后没有明确结果就是 `unknown`，不把初始“回充失败”倒推成该动作失败。
- 重复 request / 同一消息 replay 不重复写 tried。
- 旧 schema v1 懒迁移到 v2，保留原 case、tried/not_tried/suggested、来源和型号。

### 2.3 渐进诊断与歧义

- “回充失败，找不到充电座”：语境不明时只问一次二选一；问过一次不再循环。
- “机器人老在座前打转/对不上”：语境已明确，不再问主体，按传感器/对准方向推进。
- “我搬完家找不到充电座放哪”：按用户找座/摆放处理，不套传感器清单。
- 首轮不强求全量清单；按安全、指示灯、触点、传感器、用户侧摆放等可观察线索选一个最低成本下一步，说明观察点。
- 安全风险只给策略标记；具体安全建议必须来自本轮检索资料，state 不硬编码维修步骤，也不自动建议恢复出厂。

### 2.4 topic 切换与结案

- 用户换问其他主题：旧回充任务保留 `open`，先回答新问题；旧任务未确认过时最多一次轻确认。
- “谢谢/好了”是弱正向信号：可礼貌收束，但不把 case 标 `resolved`；明确“能自己回充了”才结案。
- 沉默不产生事实。

## 3. 改了哪些文件

| 文件 | 内容 |
|---|---|
| `runtime/robot_vacuum_support/interpretation.py` | schema、校验、fallback、LLM adapter、trace |
| `runtime/robot_vacuum_support/state.py` | schema v2 懒迁移、work_context、歧义、渐进下一步、feedback、open_tasks、weak closure |
| `runtime/modules/instances/runtime/server.py` | 正常请求先做 interpretation，再 apply_user_turn |
| `runtime/robot_vacuum_support/__init__.py` | interpretation API 导出 |
| `robot_vacuum/v4.json` | 仅用 project `spec_edit` 加稳定职责；owner 未手改 |
| `tests/test_context_diagnosis.py` | 新迁移测试（development） |
| `eval/development-next/cases.json` | 13 个 development migration cases，不是 holdout |
| `eval/development-next/run_real_diagnosis.py` | 3 个真实短场景的临时 8941 运行脚本 |
| `outputs/context-diagnosis-*.json` | 报告与真实运行证据 |

## 4. 真实验证范围

- 真实 run 使用 artifact `robot_vacuum-v4-e6b49a5880`，临时独立端口 `8941`，模型 `deepseek-chat`，3 个短场景，0 error。
- 3 次 interpretation 均为 `llm_structured`，没有走 fallback；usage 分别 945 / 936 / 1065 tokens（合计 2946），attempts=1，validation_errors=[]。
- 主回答 usage：两个走模型的场景 3057 / 3985 tokens；歧义场景是 deterministic direct clarification，没有主回答 token，不是 0 token。
- 观察结果：
  - 含糊“找不到充电座”：正确进入二选一澄清，未套传感器清单。
  - 新手“回不去了，一步步告诉我”：给单步清障 + 观察点；但末尾多了一次 referent 轻确认，state 中歧义已 resolved，属于模型层多余澄清，需后续复核。
  - “已清障/已擦触点，还是回不去 + 水箱漏水”：先答水箱，回充两步记 `failed`，旧任务保持 open，推进到传感器；未假结案。
- **语义正确性未正式评分**；3/3 只做结构检查和人工快速阅读，不能写成成功率。

## 5. 失败与 fallback 边界

- adapter 缺网络/模型/JSON/校验失败时：`trace.method=fallback`，记录 `fallback_reason`，保留 `unknown`；不产生 `executed_actions`，不伪造 usage，不把建议升级为事实。
- 真实 run 前曾因 `NO_PROXY` 含 `[::1]` 导致 interpreter 全部 fallback；已记录并在 adapter 内清理该 host 环境问题，重跑后才得到 `llm_structured`。没有把 fallback 冒充成语义理解。

## 6. artifact 对应关系

- **真实 run 对应**：`E:/CODE/Myproject/agent-workspace/robot-vacuum-support-built/robot_vacuum-v4-e6b49a5880`
- **latest 构建**：`E:/CODE/Myproject/agent-workspace/robot-vacuum-support-built/robot_vacuum-v4-569ecbce9a`
  - 外部 cwd `run.py --check`：`ok`，`service_ready=true`
  - 与当前 source 的 state/interpretation/server/spec v4 哈希匹配
  - 与真实 run artifact 仅差 `_SAFETY_STEP.detail` 文案收紧；未重跑模型，不能把这段差异算作已实测
- v2/v3 旧 artifact 未覆盖。

## 7. 数据与边界

- `eval/reviews` 32 条：aggregate `ef7360b7...e02dc75`，与审核快照一致（只读复算）。
- `cases.jsonl`：`13095f20...77e3`，与快照一致（只读复算）。
- holdout 未运行、未用于调参；不改已审数据或用户 notes。
- GUI 8950 未停止/重启/修改；临时 8941 已停止；source assets 未写。

## 8. 未完成

- 3 个真实回答的正式语义复核与追问接受度。
- 用 latest 569ecbce9a 覆盖 safety 文案差异的真实 run。
- 弱结案阈值、真实用户语言分布、interpretation 延迟/成本分布。
- P1-05 评测入口、P1-06 基线未完成；P1-02 仍 `in_progress`。

## 9. 后续切片（2026-11-13，问题生命周期与按需解释）

本文件记录的“新手场景末尾多余 referent 澄清”“换主题后旧问题仍进入 context”已在下一 development 切片中按机制处理，而不是改本文件的历史观察：

- case-scoped 问题记录 + `decision_snapshot` + actual-asked 审计：见 [question-lifecycle.md](question-lifecycle.md)。
- 固定 5 轮 before/after：baseline 重复 referent 追问 1 次，after 0 次；仍不是 holdout/成功率。
- 新产物 `robot_vacuum-v4-6a1eeea77f`，旧 artifact 未覆盖；真实 A/B 证据与限制见 [report](standalone.md#未包含)。
