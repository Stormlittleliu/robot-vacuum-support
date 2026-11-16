# 首轮状态激活与反馈捷径约束（机制切片）

> 日期：2026-11-14（香港）
> 状态：development 切片；不是 holdout，不是成功率，也不改 case 数据、旧 run、Judge、GUI 或 assets。
> 范围：只修两个机制：首轮/新 case 的 `direct_answer` 反馈捷径，以及语义已明确为回充故障但字面 failure 词不同的 case 激活。

## 1. 症状与共同机制

原始 approved development live run `v1-live-development-20261113-224057-3f8412` 有两类首轮问题：

| case | 用户首轮 | 实际 | 机制 |
|---|---|---|---|
| `direct-base-power-01` | 充电座指示灯不亮，机器人放上去没反应，怎么办？ | 60 字“你说的‘还是不行’是指哪一步之后…”，1 次 interpretation、0 次 agent、0 次 RAG | 首轮症状里的“没反应”命中 `_AMBIGUOUS_FOLLOWUP`；新 case 刚创建、既没有先前建议也没有用户执行事实，却被当成“对上一轮建议的含糊反馈” |
| `ambiguity-recharge-condition-01` | 它回充不行，帮我看看。 | scope=recharge，active_case_id=null、cases=[]；RAG 实际发生 | `failure_signal` 依赖 `_RECHARGE_FAILURE`/`outcome_is_failure` 的具体字面；semantic interpretation 已给出 `topic=recharge_failure`、`observable_symptom` 和 open ambiguity，但没有进入 case 创建条件 |

共同点不是“某句话写错了”，而是**状态阶段与语义证据没有参与路由/激活判断**：反馈捷径没有先问“这是不是新 case 的首次报告”，case 创建也没有接受经验证语义的故障证据。

## 2. Oracle（先定义，再实现）

### 2.1 第一个用户轮（新 case / 首次症状）

1. 首轮用户报告一个可观察症状时，`apply_user_turn` **不得**返回“你说的‘还是不行’是指哪一步”这类只适用于后续反馈的 `direct_answer`。
2. 首次症状进入正常 agent/RAG 路径：`direct_answer is None`，`scope=recharge`，创建 `status=active` 的 recharge case，`tried_actions=[]`（除非用户在同一句里明确说自己做过）。
3. “没反应”“不行”等词在首轮是**症状描述**，不是“对上一轮建议的反馈”。
4. 如果首轮本身是一个明确的二选一歧义（例如“回充失败，找不到充电座”），仍允许已有的“机器人找不到座 / 用户找不到座”一次澄清；这不是本次要削掉的反馈捷径。
5. 首轮包含用户自己的历史执行事实（例如“我已经擦了充电触点，还是不行”）时：记录该 `tried` 事实和结果，`direct_answer is None`，走正常 agent 路径；不得把用户事实丢掉，也不得假设用户做过未说过的步骤。

### 2.2 后续反馈

只有同时满足以下条件，才允许通用“哪一步”反馈捷径：

- 当前 case 不是本轮新建的（`_source_turn < turn`）；
- 存在先前用户事实或助手建议证据（tried / not_tried / suggested / feedback / clarification / 交互阶段）；
- 消息本身是**裸反馈**（如“还是不行”“还是没反应”），没有夹带新的具体症状/对象细节。

状态转移：

- 唯一 pending 建议 + 裸反馈 → 绑定该建议为 `failed`，`direct_answer is None`，继续 agent。
- 多个 pending 建议 + 裸反馈 → 返回一次“哪一步”确认，不猜测、不把多个建议都记成 tried。
- 没有可绑定建议 → 不凭空编造 tried；可以走正常 agent，或在满足上述条件时做一次低成本确认。
- 新症状 + 旧 case → 正常 agent/诊断推进，不复用上一轮的“还是不行”澄清。

### 2.3 语义明确的回充故障激活

经验证（quote 必须来自本轮用户原文、schema 合法）的 interpretation 满足以下任一条件，且 `scope=recharge` 时，应创建/激活 recharge case：

- `topic == recharge_failure` 且 `closure_signal in {failure, partial_success}`；
- `topic == recharge_failure` 且存在 `dock_find_referent` 歧义记录（open/deferred 表示症状/指代未定并保留可问；resolved 仍证明本轮在说回充对接故障，而不是设置 how-to）；
- `topic == recharge_failure` 且 `interaction_stage in {tried, diagnosing, awaiting_result}`，或 interpretation 里有用户执行/未执行动作；
- `domain == robot_vacuum_after_sales` 且 `observable_symptom` 明确命中既有的失败/电源症状语义（`outcome_is_failure`、`_POWER_SYMPTOM` 等稳定适配器）。

激活不等于编造事实：case 的 `tried_actions` 仍只来自用户原文；未确定症状可以保持 open/need_info。

### 2.4 反例：设置/使用类咨询

“怎么设置自动回充”“自动回充怎么开启”“APP 里自动回充在哪设置”等**设置/使用咨询**不是故障报告：

- 即使消息含“回充”，也不得仅凭字面创建故障 case；
- 不扩一条“回充关键词 → 故障”的新主规则；
- 语义层给出 `topic=recharge_failure` 但 `closure_signal=none`、无歧义、无执行事实、无故障症状时，不激活；
- 返回 scope 可以是 recharge（话题相关），但 `cases=[]`、`active_case_id=null`，交给正常 agent 回答设置问题。

### 2.5 一致性与幂等

- 同一 `message_id` 重放：`changed=False`，state 字节级一致。
- 换主题：旧 case 保留 open，`pending_confirmation` 最多一次；回到旧主题仍是同一个 `case_id`。
- 纯致谢：弱正向，不标 resolved。
- 更正：撤销被否定的 tried，写入 not_tried；不保留错误执行事实。
- 重启/重新加载：active case、stage、next_step、tried/not_tried 保持一致。

## 3. 同族新例与反例（development，不是 holdout）

| 类型 | 输入 | 期望 |
|---|---|---|
| A 同族 | 充电座指示灯不亮，机器人放上去没反应，怎么办？ | 无“还是不行”direct；active/recharge；首次 agent/RAG |
| 新症状 | 回充失败，机器人老在充电座前面打转，怎么办？ | 无反馈澄清；action（sensors/reseat） |
| 安全新症状 | 回充时充电座冒烟了，还有焦味 | 无反馈澄清；safety 下一步 |
| 首轮含执行事实 | 回充失败，我已经擦了充电触点，还是不行 | tried=clean_contacts/failed；无反馈澄清 |
| B 同族 | 它回充不行，帮我看看。 | active/recharge；tried=[]；未确定症状/指代可问 |
| 语义无字面 failure | 回充这块它不太灵，帮我看看 | 经验证 closure=failure 时 active/recharge |
| 反例-设置 | 怎么设置自动回充？ | 不建故障 case |
| 反例-设置+语义 | 同上，interpretation 给 topic=recharge_failure、closure=none、无歧义 | 不建故障 case |
| 后续裸反馈 | 回充失败 → 建议 A →“还是不行” | 唯一建议记为 failed；direct=None |
| 多建议反馈 | 两个 pending 建议 →“还是不行” | 一次“哪一步”确认；tried 不增加 |
| 新症状后续 | 旧 case 已确认一步失败 →“现在指示灯不亮，放上去没反应” | 不返回旧“哪一步”澄清 |
| 提问旧步骤 | “我刚才试过什么？” | 只按服务端 tried 回答，不编造 |
| 幂等 | 同 message_id 重放 | changed=False，state 不变 |
| 恢复 | 保存/重载 | active case/next_step/tried 一致 |
| 换主题/致谢/更正 | 见 2.5 | 状态一致，不结案、不污染旧 case |

## 4. 实现层与边界

- 修改层：`runtime/robot_vacuum_support/state.py`（reducer 激活条件、反馈捷径门槛、激活证据）。
- 复用：`state.apply_user_turn` 与现有 server 路径；不改 system prompt、spec、case 数据、旧 run/结果、Judge、GUI、assets。
- 不做：新增关键词主规则、对所有“回充”咨询建 case、用单 case 推导成功率、运行 holdout/24-case/Judge。
- 验证：新增 `tests/test_first_turn_context.py`（oracle 同族 + 反例 + 幂等/恢复），再定向运行原有 state/context/question-lifecycle 测试；真实小 smoke 用隔离端口 8947，仅验证 A 首轮 agent/RAG 与 B 首轮 active/recharge。

## 5. 实现

`runtime/robot_vacuum_support/state.py`：

- 新增 `_BARE_AMBIGUOUS_FOLLOWUP`：只把“还是不行 / 现在没反应 / 不行”这类**裸反馈**视为可走通用“哪一步”捷径；带新症状/对象细节的消息不算。
- 新增 `_has_prior_feedback_context(case, turn)`：要求 `_source_turn < turn`，且 case 已有 tried / not_tried / suggested / feedback / clarification / 交互阶段证据。新 case 首轮不满足。
- 通用“哪一步”分支同时要求 `_AMBIGUOUS_FOLLOWUP`、`_is_bare_ambiguous_feedback`、`_has_prior_feedback_context`，并保留既有“dock 歧义 deferred 不再问”的条件。
- 新增 `_semantic_recharge_fault(proposal, text, recharge)`：`topic=recharge_failure` 且满足 closure failure/partial、任一已验证 `dock_find_referent` 记录（open/deferred 保留可问，resolved 仍作为回充对接语境证据）、tried/diagnosing/awaiting_result 阶段、用户执行/未执行事实、或既有症状适配器命中时，作为 activation 证据；不写 tried。
- 新增 `_SETTINGS_INTENT` 负向 guard：显式 how-to/配置请求（“怎么设置自动回充”“自动回充怎么开启”“自动回充设置在哪”）在无显式 failure/safety 时压制语义 activation；即使语义层误挂了一个 dock 指代，也不把设置问题变成故障 case。它是显式短语适配器，不是理解主层。
- `failure_signal` 接受上述语义证据；新 case 写 `activation_evidence`（kind / semantic topic/closure/stage / ambiguity ids / source_turn）便于 trace 与 smoke 核对。
- “我都试过哪些”这类旧步骤提问此前会被 scope 早退拦住；现在把 `_is_tried_question` 加入早退条件，仍只从服务端 tried 回答。

`tests/test_first_turn_context.py`：20 个 development 测试；`scripts/first_turn_context_smoke.py`：复用 `scripts/run_latency_diagnosis.py` 的真实 build/server/SSE 机制，端口 8947，隔离 storage root，A 1 轮 + B 2 轮。

## 6. 离线证据

- `python -m pytest tests/test_first_turn_context.py -q`：**20 passed**。
- `python -m pytest tests/test_context_diagnosis.py tests/test_p1_recharge.py tests/development-next/test_question_lifecycle.py tests/test_first_turn_context.py -q`：**91 passed, 24 subtests passed**（最终代码后一次定向运行；未跑全库）。
- 反例覆盖：文本层“怎么设置自动回充 / 自动回充怎么开启 / APP里自动回充在哪设置”不建 case；语义层 topic=recharge_failure 但 closure=none、无歧义、无执行事实、无故障症状时也不建 case；语义层误挂 dock 指代的设置问题同样不建 case（除非同一轮有显式 failure/safety）。
- 同族覆盖：A 首轮、不同症状、安全症状、首轮历史执行事实、唯一/多 pending 后续反馈、后续新症状、B open/resolved 指代、无字面 failure 的语义故障、旧步骤提问、幂等、重载、换主题/回到旧 case/致谢/更正。
- 未改 case 数据、旧 run、Judge、GUI、assets；未跑 24/6/holdout。

## 7. 真实小 smoke

- artifact：`E:/CODE/Myproject/agent-workspace/robot-vacuum-support-built/robot_vacuum-v4-966ba99a31`（新产物，未覆盖旧产物）；外部 cwd `run.py --check` 为 `ok / service_ready=true / checksums_only=true`。
- run：`outputs/first-turn-context-smoke-20261114-174314/run.json`；端口 8947；3 轮（A 1 轮 + B 2 轮）；0 error；结束后端口已释放；8950 未动。
- A：scope=recharge、case active/diagnose、tried=[]、next=check_power；semantic `llm_structured` 926 tokens；trace 15 events / 2 model_start / 4 tool_start / 4 tool_end，工具 `rag_retrieve` 全部 success；main agent 2 requests；回答 319 字，含“参考来源：故障排除.txt”，没有“还是不行”旧澄清。
- B 首轮：scope=recharge、case active/need_info、tried=[]；semantic `llm_structured` 972 tokens，topic=recharge_failure，`dock_find_referent` open；问题记录 pending/ask_count=1；因现有一次二选一策略返回了 dock 指代澄清（没有进 main agent），但未编造 tried，指代保持可问。B 第 2 轮回答“是机器人自己回不到充电座…”后仍为同一个 case，歧义 resolved=robot_cannot_return，main agent 2 requests、`rag_retrieve` success，next=clean_sensors。
- 结论只覆盖这 3 个真实 turn：A 首轮确实回到 agent/RAG；B 首轮确实 active/recharge 且保留未定指代。**不是成功率，不是分布。**

第一次 smoke 用中间产物 `robot_vacuum-v4-4a2cc3f9c1` 时 B 未激活：真实 LLM 把 dock 指代直接标成 resolved，而第一版只把 open/deferred 当证据。修正为“任一已验证 dock_find_referent 记录都作为回充对接故障语境证据”后，`robot_vacuum-v4-4bcfe1e6db` 重跑通过；随后又加设置类负向 guard，最终重建 `966ba99a31` 并重跑通过。中间失败 run 保留在 `outputs/first-turn-context-smoke`，中间通过 run 在 `outputs/first-turn-context-smoke-20261114-173849`，均不作为最终证据。

## 8. 边界与下一步

- B 首轮的答法仍是现有歧义策略：真实 LLM 给 open 指代时先问一次二选一，因此这一轮没有 agent/RAG。若后续要求“任何首轮都先进 agent”，需要单独定义“首轮是否延后 dock 二选一”的 oracle，不能顺手改。
- 如果未来模型只给 `topic=recharge_failure`，既无 closure、歧义、阶段、执行事实，也无明确症状，case 仍不激活；这是为了不把所有“回充”设置问题变成故障 case。
- 设置类反例只有离线覆盖（含“语义层误挂指代”这一反例），尚未用真实 LLM 跑；多 pending 真实反馈也待补。语义回答质量、引用质量、时延、用户接受度未评分。
- P1-02/P1-05/P1-06 状态不因本切片升级；不据单 case 推导成功率。证据 JSON：[outputs/first-turn-context-report.json](standalone.md#未包含)。

## 9. 收口状态

- 本轮是 2026-11-14 Judge 机制总审修复后的推进，完成了首轮 state/execution 切片（A 反馈捷径约束、B 语义激活、旧步骤提问一致性）。不是新一轮仪表化，也不是 P1 完成。
- 主模型已重启 8950，PID `167188`；只读核对 `jr-audited-20261114-01`：**6/6 selected、coverage complete（113）、3 条 provisional_unresolved**；旧 `jr-native-20261114-05` 标 `legacy_unverified`，不作为当前可信结论或成功率。8950 本轮只读，未改 Judge/UI 数据。
- 本轮真实 smoke `outputs/first-turn-context-smoke-20261114-174314/run.json` 为 **13/13 checks passed**、端口 8947 已释放；3 个真实 turn 的语义质量**未正式评分**，只作结构证据。
- 设置类 how-to 反例继续只保留离线覆盖（含“语义层误挂 dock 指代”反例）；没有在本次收口里补真实 LLM 反例。
- **下一步主线：** 继续 context 否定/更正，以及动作执行/结果语义统一（executed / failed / unknown / resolved）；先定 oracle 再改，不代替人工修改 oracle，也不把该机制的继续仪表化当成主要任务。P1-02/P1-05/P1-06 保持 `in_progress`。


