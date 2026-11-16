# 动作执行 / 效果 / 否定更正状态契约（oracle）

> 日期：2026-11-14（香港）
> 状态：development 纵向切片。先定 oracle 再实现；不是 holdout、不是成功率，不改冻结 case/expected、旧 run、Judge、GUI、assets。
> 范围：回充故障 case 内的 action identity、`user_executed` + `outcome`、否定/更正 fact reducer、局部成功 vs 结案、消费一致性。

## 1. 两个维度，不重命名、不合并

`schema` 继续使用 `tried_actions` 行上的 `user_executed` 与 `outcome`：

| 维度 | 值 | 含义 | 不能推出 |
|---|---|---|---|
| `user_executed` | `true` | 用户原文明确说**自己执行过**该动作 | 不能推出动作目标达到、故障恢复、case 结案 |
| `outcome` | `unknown` | 已执行，但结果/效果未说明 | 不能说“没用”，不能当成成功 |
| `outcome` | `failed` | 该动作之后故障仍在/未恢复（原文有失败或未恢复证据） | 不能说动作没做过 |
| `outcome` | `success` | 明确效果恢复或该动作目标达到，**且作用范围清楚** | 不能自动推出全局 case 结案（见 §4） |

`case.resolved` 是独立字段，只在“明确恢复 + 范围足以覆盖当前 case”时置为 `resolved`。“手动放回能充，但自动回充仍失败”是 `partial_success`，case 必须保持 active。不得把 `user_executed=true` 改写成 `success`，也不得把 `outcome=success` 反推成“用户做过”。

## 2. 动作身份（至少这三类必须分开）

| action_id | 含义 | auto_next | 说明 |
|---|---|---|---|
| `replace_dock` | 更换新的充电座/新基座 | **false** | 只记录用户观察事实；不能自动建议更换硬件 |
| `move_dock` | 挪动/搬动原来的充电座 | false | 只记录观察事实；与“换新座”不同 |
| `reseat_dock` | 手动把机器人放回/对准充电座 | true | 既有 id，语义保持“把机器人放回座”，不是“换座” |

- 同一份 action 字典由 `actions.py` 提供，`interpretation.py` 与 `state.py` 都消费它；测试检查 catalog / patterns / interpretation ACTION_IDS 不漂移。
- “换座”“换了座”单独出现时对象/动作不明确，**不映射**到 replace、move 或 reseat；宁可不记，也不猜。
- 语义层明确给出 action + 当前原文 quote 时优先；句法 fallback 只保留同族显式短语，并配反例。
- 动作/对象 scope：`replace/move/reseat` 的 quote 必须含充电座/基座/座对象；`reset_robot` 遇到“服务/服务器/后台/会话/进程/系统/APP 重启”不得记为机器人重启。用户明确“重启了机器人/机器”仍可记录。
- `replace_dock` / `move_dock` 不能进入自动 next_step 或 suggestion；“没换新座”也不能把 replace 变成下一步建议。

## 3. 否定 / 更正统一 fact reducer

### 3.1 权威事实

`tried_actions` 与 `not_tried_actions` 的每一行有 `fact_status`：

- `effective`：当前有效事实（旧 schema 无该字段时按 effective 读取）；
- `retracted`：被后续否定/更正撤销的 tried；
- `superseded`：被后续执行/更正的 not_tried。

同一 action 在 `effective` tried 与 `effective` not_tried 中**不得同时存在**。所有 prompt/snapshot/direct query/路由 cache/question lifecycle/handoff 只消费 effective 行。

### 3.2 转移

| 事件 | tried | not_tried | 审计 |
|---|---|---|---|
| 用户说做过（未说结果） | effective, unknown | 撤销同 action 的 effective not_tried（superseded） | `supersede_not_tried` |
| 用户说做过且明确失败 | effective, failed | 同上 | 同上 |
| 用户说做过且明确恢复（范围清楚） | effective, success | 同上 | 同上 |
| 用户说没做/纠正没做 | 撤销同 action 的 effective tried（retracted，含推导 outcome） | effective | `retract_tried` |
| 同一动作先没做、后做过 | 新增/恢复 effective | 旧行 superseded | 两条审计 |
| 同一动作先做过、后纠正没做 | 旧行 retracted | effective | 一条审计 |

审计写在 `case.fact_audit`，每条形如：

```json
{
  "kind": "retract_tried",
  "action_id": "clean_contacts",
  "message_id": "m3",
  "turn": 3,
  "text": "说错了，我没擦触点",
  "reason": "user_correction_not_tried",
  "replaced_fact": {"action_id": "clean_contacts", "user_executed": true, "outcome": "failed", "...": "..."},
  "evidence_id": "..."
}
```

- 旧建议永远不会因为“建议过”变成 `tried`；助手文本只在 `apply_assistant_turn` 里形成 suggestion。
- 只有当前用户原文子串可作为 semantic action 的 quote；伪造 quote、旧轮 quote、未知 action、对象 scope 不匹配一律丢弃并记录 validation error。
- 重复 message 恢复/重放：`changed=false`，state 字节级一致；审计按 `evidence_id` 去重，不重复撤销。

## 4. 成功 / 局部成功 / 结案

- `success`：仅显式“充上了/能自己回充了/恢复正常/已解决”等，且不是假设、否定或 partial。
- `partial_success`：明确局部有效但整体失败，例如“手动放回能充，自动回充还是不行”。`case.status=active`、`resolution=null`，不回写成功结案。
- 弱正向（“好了/谢谢/正常了”）仍是 `weak_positive`，不结案、不产生 success tried。
- 用户只说“现在能回充了”但没有说自己执行了哪个动作：case 可以结案，**不能**把上一条助手 suggestion 写成用户 tried。
- 历史 case 的 `outcome=success` 在没有新证据时原样保留；旧 schema load 只补 `provenance=legacy_pre_contract`/迁移说明，不暗改成新含义、不伪造结果。

## 5. 泛化否定 / 指代

“都没试过 / 都没做 / 一个都没试”这类泛化否定：

1. 先看用户本轮是否带明确动作对象；带对象时只否定对应 action(s)，不能否定所有历史 tried。
2. 没有明确对象时，绑定**上一轮明确问过的 actions**（assistant next_step / step_outcome question）或当前 pending suggestions；绑定集合写入审计（来源 + turn）。
3. 没有任何可指代集合：不改事实，保持 unknown，提出一个澄清（`negation_ambiguity.status=needs_referent`）。
4. 集合太多（>3）或与已知 success 冲突：记录歧义（`too_many` / `conflicting_success`），不强行改事实，提一个澄清。
5. 这是程序上下文关联 + 有限显式短语 fallback；不扩关键词词典当理解主层。语义 proposal 的 `negation_scope=all_pending` 与显式 phrase 都可触发，但 reducer 仍是权威。

## 6. 兼容与消费一致

- `load_state` 兼容 v1/v2：补 `fact_status`、`fact_audit`、`last_asked_actions`、`negation_ambiguity` 默认值；旧行内容保留。
- 旧状态若同一 action 同时有 effective tried + not_tried，按更晚 source_turn 选一个 effective，另一个标 superseded/retracted，并写 `legacy_fact_reconciliation` 审计；不静默改 outcome。
- `decision_snapshot.confirmed_facts`、`prompt_context`、`query_tried_answer`、`routing.state_version`、`questions.reconcile_questions`、`handoff_summary`、public view 全部只读 effective 行。
- 路由 cache 的 state version 包含 effective tried/not_tried；撤销后旧 replay 不得命中。

## 7. 验证 oracle（至少 20 个）

同族与反例至少覆盖：

1. 执行未知：`我擦了触点` → tried clean_contacts/unknown，case active。
2. 明确失败：`擦了触点还是不行` → failed。
3. 局部成功 vs 全局：`手动放回能充，自动回充还是不行` → partial，case 不结案。
4. 未做 → 做过：`没擦触点` → `擦了触点`，not_tried superseded。
5. 做过 → 撤销：`擦了触点还是不行` → `说错了，我没擦触点`，tried retracted + audit。
6. 复合更正：`换了新座` → `没换新座，只挪了旧座`，replace retracted、move effective、replace not_tried。
7. 动作身份：replace / move / reseat 不互相串。
8. replace 不自动建议：next_step ≠ replace_dock。
9. 服务重启：`服务重启了，还是不行` 不记 reset_robot。
10. 设备重启：`重启了机器人，还是不行` 记 reset_robot。
11. 语义 quote 伪造：quote 不在当前消息 → 丢弃。
12. 旧轮 quote：当前消息没有该动作原文 → 丢弃。
13. 旧 schema load：success 保留 + legacy provenance。
14. 旧 schema 冲突：tried/not_tried 冲突按更晚记录收敛 + 审计。
15. 重复 message：无重复撤销、state 不变。
16. 泛化否定有 pending：只绑定 pending/last-asked，不否定全部历史。
17. 泛化否定无 pending：不猜、澄清。
18. 泛化否定集合太多：记歧义、不改事实。
19. 泛化否定与 success 冲突：记歧义、不改事实。
20. 明确对象否定：只撤销对应 action，其他 tried 保留。
21. 成功后不把旧建议写成 tried。
22. effective facts 一致：snapshot/prompt/query/routing/questions 不消费 retracted。
23. 同 action 不能同时 effective tried + not_tried。
24. 历史 `success` 无新证据不被暗改。

## 8. 边界

- 本轮不改 system prompt 的稳定职责之外的题目补丁，不扩全量关键词，不改冻结 oracle expected。
- 真实 smoke 只覆盖两个短 session 的约 6–8 turn；不是分布/成功率，不重跑 24/6/Judge/holdout。
- 动作效果语义仍由用户原文/经验证 semantic proposal 决定；程序负责 scope、转移、证据和幂等。

## 9. 实现（2026-11-14）

- `runtime/robot_vacuum_support/actions.py`：唯一 action catalogue / id / fallback patterns / `claim_rejection`；新增 `replace_dock`、`move_dock`，均 `auto_next=False`；`reseat_dock` 保持旧 id 与“把机器人放回/对准座”语义。`interpretation.py` 与 `state.py` 共享该字典，测试检查无 enum 漂移。
- `state.py`：tried/not_tried 行增加 `fact_status=effective|retracted|superseded`；`fact_audit` 记录 message_id/turn/原文/原因/被替代事实；`_record_attempt` 后续执行撤销旧 not_tried；`_add_not_tried` 撤销旧 tried 及其推导 outcome。旧 schema load 只补字段/`provenance`，历史 success 原样保留；旧冲突按更晚事实收敛并写审计。
- 泛化否定：`_GENERALIZED_NEGATION` 或语义 `negation_scope=all_pending` 触发；绑定 `last_asked_actions` / pending suggestions；无指代、集合 >3、与 success 冲突分别记 `needs_referent` / `too_many` / `conflicting_success`，不改事实并提一个澄清。明确动作对象的否定优先，只动对应 action。
- 服务重启：`claim_rejection` 对 `reset_robot` 检查服务/会话/进程等对象；明确“重启了机器人/机器”仍记录。`replace/move/reseat` 检查充电座对象。
- 消费一致：`decision_snapshot`、`prompt_context`、`query_tried_answer`、`routing.state_version`、`questions.reconcile_questions`、handoff 与 public view 只消费 effective 行；被撤销事实对应的 `step_outcome` 问题标 superseded，不把旧回答当现行答案。
- 成功语义：明确成功且无 confirmed action 时 case 可以 resolved，但不再把上一条助手 suggestion 写成 tried；局部成功仍 active。

## 10. 离线与真实证据

- 新测试 `tests/test_action_state_contract.py`：**29 passed, 2 subtests passed**；覆盖执行未知、明确失败、局部成功、未做→做过→撤销、复合更正、action 身份、replace 不自动建议、服务/设备重启、语义 quote 伪造/旧轮 quote/动作-原文不匹配、旧 schema success/冲突、重复 message、泛化否定有/无/过多/冲突、明确对象、旧建议不 become tried、snapshot/prompt/query/routing/questions 一致性。
- 受影响定向一次运行：`test_action_state_contract + test_p1_recharge + test_context_diagnosis + test_first_turn_context + development-next/test_question_lifecycle + test_eval_selection` = **131 passed, 1 warning, 26 subtests passed**（未跑全库）。此前修复过程中出现的两项旧测试失败已按 oracle 修正（move 误匹配“搬家”、quote 错误文案），不是改旧期望掩盖。
- 新 artifact：`E:/CODE/Myproject/agent-workspace/robot-vacuum-support-built/robot_vacuum-v4-cdf5aac89d`（旧产物未覆盖）；外部 cwd `run.py --check` = `ok=true / service_ready=true / checksums_only=true`。
- 真实 LLM smoke：`outputs/action-state-contract-smoke-20261114-3/run.json`，隔离端口 8948，2 sessions / 6 real turns，0 turn error，**13/13 checks passed**，结束后无 LISTEN、8950 PID 167188 未动。
  - S1：T1 `clean_contacts/unknown`；T2 更正后 `clear_dock_obstacles/unknown`、`clean_contacts` not-tried、retract_tried 审计；T3 “刚才试过什么”只复述清障/unknown + 触点没做过，0 main agent / 0 RAG。
  - S2：T1 `replace_dock/unknown`；T2 更正后 `move_dock/unknown`、`replace_dock` not-tried、retract_tried 审计，未漂移成 reseat；T3 “试过哪些”只复述 move/unknown + replace 没做过，0 main agent / 0 RAG。
  - 纯服务重启 meta（非模型）：`服务重启了，还是不行` 不记 reset_robot；`重启了机器人，还是不行` 记 `reset_robot/failed`。
- 真实失败保留：第一次 smoke `outputs/action-state-contract-smoke-20261114/run.json` 失败；根因是 `_TRIED_QUESTION` 把“我刚才擦了触点”误判成回忆问题（已修为需“什么/哪些/步骤”，并加回归测试），以及 smoke 用 session 累计 trace 判断单轮无 RAG（已改为逐轮 trace slice）。t2 审计缺失是 t1 未记事实的下游结果，不是审计机制缺失。
- 边界：S1/S2 回忆轮的语义解释各出现一次伪造/越界 executed action（quote 不在本轮 / 对象 scope 不符），均被 reducer 拒绝且未写入 state；这说明语义层仍可能幻觉，程序证据门有效。真实回答语义质量未正式评分；未跑 24/6/Judge/holdout。

## 11. 评分器接入与正确环境验收（2026-11-14 收口）

- 评测动作单一来源：`eval/scorers.py` 的 `SCORER_VERSION = "v1.2-shared-actions"`，直接 import `runtime/robot_vacuum_support/actions.py` 的 `ACTION_IDS`；catalog 仍只有一份，评分器不复制 action enum。
- 正确环境验收：`C:/Users/28980/anaconda3/envs/langchain/python.exe` 下 `test_action_catalog_scoring + test_eval_v1` = **23 passed in 4.86s**。此前 py -3.11 缺 `inspect_ai` 的 collection failure 保留在 scoring report，已由正确环境排除，不是评分断言失败。
- 主模型独立验收：state/context/firstturn/questionlife/action_catalog = **127 passed, 26 subtests in 12.23s**；artifact `robot_vacuum-v4-cdf5aac89d` 五个 runtime 文件（actions/state/interpretation/routing/questions）与 source 哈希一致；smoke-3 为 13/13 passed、port released。
- 语义边界：局部“手动放回能充、自动回充仍失败”仍 `case.status=active`，`reseat_dock` 的 action outcome 保守为 `unknown`，`partial_success` 原文保留；不宣称该局部效果已被完整建模，也不把局部成功写成结案。
- **下一轮入口：** 基于当前契约评审并修复后的真实对话，集中处理 oracle 争议（尤其 success vs unknown / 局部效果、旧 approved baseline 期望），避免长期只写规则；不是新增人工审批门，P1-02/P1-05/P1-06 仍 `in_progress`。
