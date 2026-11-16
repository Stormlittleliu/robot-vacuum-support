# Pending-question lifecycle + on-demand interpretation（P1-02 development slice）

> 日期：2026-11-13  
> 状态：development 纵向切片；不是 holdout、不是成功率、不是 P1 完成。  
> 结构化证据：[outputs/question-lifecycle-report.json](standalone.md#未包含)

## 1. 失败机制（先于实现）

真实 3 场景暴露的不是三个独立措辞问题，而是同一个机制：

1. `work_context.ambiguity` 已 `resolved`，但 `prompt_context` 仍把旧 `unknowns` 和旧澄清原文注入模型，模型于是再次追问 referent。
2. 问题“问过”只有 `clarification_history`，没有“是否回答 / 是否暂缓 / 是否被纠正重开”的 case-scoped 生命周期；程序没记录不等于模型没问。
3. 换主题后旧任务没有统一的“暂缓”决策快照，旧问题与旧未知项仍每轮进入 context。
4. 每轮无条件做一次 interpretation + 主回答 + RAG summary，trace 只看到部分层，无法说明成本与节约来自哪里。

## 2. Question contract（case-scoped，刻意窄）

每条问题记录：

```text
question_id / slot / target_case
status = pending | answered | deferred | superseded | cancelled
source（ambiguity/clarify/confirm/weak_closure/policy/legacy_migration）
evidence[]（message_id / turn / quote）
asked_turn / last_asked_turn / ask_count
resolved_turn / answer_value / answer_evidence
deferred_turn / defer_reason
reopen_reason / reopen_evidence / answer_history
```

状态规则：

| 迁移 | 条件 |
|---|---|
| pending → answered | 用户原文明确回答，或已确认事实满足 slot（resolved ambiguity、已试结果、明确成功）。不因沉默、助手问过或“谢谢”而 answered。 |
| pending → deferred | 用户换主题、忽略问题或同 case 出现新症状；保留“未回答”事实，不伪装成已答。 |
| deferred → pending | 有明确重开理由：回到旧任务且仍未回答、用户纠正、新事件；记录 reason + evidence，且受频率上限约束。 |
| answered → pending | 只允许用户纠正/新事件推翻旧答案；旧答案进 `answer_history`，不删除。 |
| pending → superseded | 同一 case 出现更优先的新问题；旧记录保留。 |
| pending → cancelled | 问题不再影响决策（如 case 已明确解决）。 |

频率策略：

- 同一 slot 最多自动重问 2 次（`MAX_ASKS=2`），两次之间至少隔 1 轮（`REASK_MIN_GAP=2`）。
- deferred 且 `ask_count=0`（例如刚切主题）默认不再问；没有 reopen reason 不重问。
- 达到上限仍未回答：记录 `blocked_unanswered`，快照明确写“已问两次仍不是已答事实”，不靠“问过就不再问”掩盖未回答。

## 3. Decision snapshot

`state.decision_snapshot(state)` 是 prompt context 与回答生成的同一对象：

- `current_question`：本轮唯一可问问题（没有则 null）
- `answered_scope`：已回答 slot + 值 + 轮次 + 证据
- `deferred_scope` / `must_not_ask`：不得再问的 slot 及理由
- `blocked_unanswered`：达到重问上限仍未回答
- `confirmed_facts`：型号、已试/未试、closure、typed work_context，均带 provenance
- `deferred_tasks`：旧任务只给摘要与 `reminder_allowed`（最多一次轻确认）
- `route` / `snapshot_id`：本轮路由与快照身份

`prompt_context(state, snapshot)` 只渲染这个快照；动态问题文案不进 system prompt。若 history 里还有已回答的旧问题，`answered_scope` 明确优先，但用户历史事实不删除。

## 4. actual_asked oracle

`audit_assistant_answer(case, answer, turn)` 检查回答文本：

- 回答提出当前 slot 问题：`actual_asked=true`，写 `asked_turn/ask_count` 与 quote。
- 回答再次提出已回答/已暂缓/重问上限的 slot：`unexpected_asked` + quote；answered 状态不回退。
- 当前问题未被回答：`missed_question=true`；问题仍是 pending/deferred。
- 因此“程序没记录提问”不能作为“模型没问”的证据。

## 5. 按需语义触发与成本计量

路由优先级（`routing.decide`）：

1. **replay_cache**：同 message 文本 + 同 fact-state version 且已有通过校验的 interpretation；复用不重复计 token，缓存按 session state 保存，不跨 session。
2. **protocol_ack**：纯致谢/寒暄且无事实更新。确定性回答，不调用语义模型，不虚构 resolved；weak signal 仍保留。
3. **deterministic_slot_answer**：当前 slot 是二选一 referent 或明确 weak-closure 结果，且可用预定义 schema 无歧义验证。只跳过 interpretation，不跳过事实 reducer。
4. **semantic**：歧义、新症状、更正、无法确定的事实仍走语义模型；keyword 不是语义主层。

成本计量（`core/observability/turn_usage.py`）：

- 每层 raw model response 按 `call_id` 只记一次：`interpretation` / `main_agent` / `rag_summary`。
- `main_agent` 取 trace `run_end` 的 model calls/tokens，并排除 `node="tools"` 的工具内模型调用；`rag_summary` 由 RAG 的 ModelPort 记录；`interpretation` 取自身 trace。三者不相加同一来源，避免 parent/child 或 budget/trace 双计。
- ledger event 与真实模型请求分开：主回答一个聚合 usage 行可以代表 2 个 trace model requests；`model_calls = sum(model_requests)`，`ledger_events = calls 行数`，token 只按聚合行计一次，不按请求数乘。
- 缺 provider usage：`usage=null`，整轮 `usage_status=partial/unavailable`，不估算。
- 零模型调用：`model_calls=0`，`usage_status=not_called`；纯 protocol ack 明确写 token=0，未知才写 null。
- 费用：`cost=null`，原因 `unavailable_no_authoritative_unit_price`。仓库 `observability.yml` 的 pricing 明确是示例价，不作为账单。
- interpretation 输入压缩为 `needed_facts + pending_question + snapshot_id`，不重放整段会话。

## 6. 本轮测试（development，不是 holdout）

新增 [tests/development-next/test_question_lifecycle.py](../tests/development-next/test_question_lifecycle.py) 与 [cases.json](../tests/development-next/cases.json)，覆盖：

- 已回答 slot 不再问；prompt 含 answered scope，无旧 clarification。
- 未回答必要澄清保留 pending/deferred；deferred 可带理由重开；达到上限显示 blocked，不标 answered。
- 用户纠正带证据重开，旧答案进 history。
- 换主题 defer，旧任务最多一次提醒；第二次不再提醒。
- 重启/JSON roundtrip 保留问题记录；session 状态隔离；request replay 命中缓存，fact-state 变化则失效。
- fallback 不写已试/已答假事实。
- routing 不吞否定、更正、明确成功；纯致谢确定性路由。
- usage 按层且按 call_id 去重；聚合 main 行区分 `model_requests` 与 ledger event；缺 usage 为 null；零调用为 not_called / token 0。
- 旧 resolved ambiguity 懒迁移为 answered question，保留 tried 事实。

受影响套件最终一次运行：`test_context_diagnosis.py` + `test_p1_recharge.py` + `tests/development-next` = **71 passed, 24 subtests passed**（2026-11-13，本地 conda `langchain`）。未改旧 oracle、用户 reviews/notes、v1/v1.1 数据或 holdout；未重跑全库。

## 7. 真实 5 轮证据与计量修正

- baseline：`robot_vacuum-v4-569ecbce9a`；after 真实运行：`robot_vacuum-v4-6a1eeea77f`；两者同题 5 轮、同 `deepseek-chat`、0 error。
- 重复 referent 追问：baseline 第 1、2 轮（第 2 轮是已回答后的多余重复）→ after 仅第 1 轮（必要初始二选一），多余重复 1→0。
- 旧任务轻确认：baseline 与 after 都只在第 4 轮最多一次；after 第 3、5 轮不重复。
- after 原始 run 的 turn4 counter 显示 `turn_metrics.model_calls=3`，但实际 interpretation 1 + rag 1 + main 2 = 4；主回答是一个聚合 usage 行代表 2 个 model requests。已修 counter 代码，并用离线 [usage correction](standalone.md#未包含) 记录推导；raw run 不重写。
- 修正后 after：model_calls=10，ledger_events=7，reported tokens=13,493；turn5 protocol ack 为 0 calls / token 0 / not_called。
- baseline 没有新 meter，RAG 已包含在旧 trace/budget 中且无法拆层；**不做 total comparison，不声称整体节约比例**。可证明的局部节约仅是 turn2 interpretation 0 次、turn5 0 次模型调用。
- 最新计量代码产物：`robot_vacuum-v4-af34e8f764`，外部 cwd `run.py --check` ok；未再跑模型，真实 after 回答仍属于前一版 counter 显示有缺陷的 `6a1eeea77f`。
- 剩余质量：after 第 2–4 轮仍有 4 个模型自行生成的观察/新症状问题，已进 `unplanned_questions` 审计，但未成为生命周期 slot，也未正式评分。

## 8. 边界

- 本 A/B 只采用 baseline 与最终 after 两次完整 5 轮运行；中间 after 因 counter 显示缺陷不作为 A/B 证据。baseline 必须同题同模型，否则只报 after。
- 结论只对“这组 development 场景”成立；不写成功率、整体提升或 holdout 结论。
- 未做全 agent loop 重构、未新增预算限额、未做跨 session 缓存；默认 token/工具次数预算仍不限。
