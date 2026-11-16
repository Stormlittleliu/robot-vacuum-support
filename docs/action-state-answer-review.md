# 动作状态修复后真实回答评审（2 对话 / 6 轮）

> 固定输入：`outputs/action-state-contract-smoke-20261114-3/` 的两个真实 session，artifact `robot_vacuum-v4-cdf5aac89d`，契约 `docs/action-state-contract.md`。
> 性质：post-hoc、探索性 development 评审，**不是独立 benchmark、不是 holdout、不是成功率**；人工 run-answer 复核仍待用户填写。
> 机器：一次 Judge `jr-action-review-20261114-01`；随后单独恢复任务只补跑失败的 arbitrator（`../dsh-judge/outputs/action-review-recovery.json`）。本报告未重跑 Judge、未覆盖结果。

## 1. 结果

| case | Judge status | verdict | coverage | 未决 | 说明 |
|---|---|---|---|---|---|
| `asc-dev-contact-correction-01` | completed | **passed** | 11/11 | 0 | 11 criterion 全 passed；28/28 引用通过；无仲裁。 |
| `asc-dev-dock-correction-01` | completed | **uncertain** | 11/11 | 0 | 恢复后 arbitrator 判定 `case-evidence-support=passed`；但程序仍保留 **1 条被拒引用**（citation_compromised），故 case verdict 仍为 uncertain。无 system_errors。 |

- 历史初始批次 dock 是 `partial/uncertain`（arbitrator 无结构化输出），原始 partial 已归档到 `.history/`；另一任务只补跑该失败 arbitrator 一次，contact 整体结果字节一致复用、dock 两个已成功角色 checkpoint 原字节复用。恢复后 dock `completed/uncertain`，11/11 issue 全部 passed，`unresolved_issue_ids=[]`，`system_errors=[]`；程序仍因 1 条历史 citation rejection 保留 uncertain。
- **不是 1/2 成功率。** dock uncertain 不是 confirmed agent failure，也不是自动检查替代语义判断。
- 13/13 程序 checks 仅是结构检查，未进入 11 个语义 criterion。

## 2. 证据与逐轮事实

| case | turn | 用户意图 | 实际回答 | effective / retracted | retrieval | trace |
|---|---:|---|---|---|---|---|
| contact | 1 | 已擦触点、未看结果 | 保留“已做”，先确认结果 | `clean_contacts/unknown` | current 2，history 0 | `run-baeaa412...` |
| contact | 2 | 更正：没擦，只清障 | 撤销触点，确认清障 | `clear_dock_obstacles/unknown`；触点 not-tried + audit | current 2，history 2 | `run-1d3ce35b...` |
| contact | 3 | 回忆试过什么 | 只复述清障/unknown + 触点没做 | 同上；case active | current 0，history 4 | 无（0 model/0 RAG） |
| dock | 1 | 已换新座、未看效果 | 保留换座/unknown，先确认 | `replace_dock/unknown` | current 2，history 0 | `run-26cf5b85...` |
| dock | 2 | 更正：没换新座，只挪旧座 | 撤销换座，记录挪旧座 | `move_dock/unknown`；replace not-tried + audit；无 reseat 漂移 | current 2，history 2 | `run-8db6f043...` |
| dock | 3 | 回忆试过哪些 | 只复述挪旧座/unknown + 换新座没做 | 同上；case active | current 0，history 4 | 无（0 model/0 RAG） |

- 每轮 snapshot、current/history retrieval 原文、fact audit、request/trace/session/行号映射均在 `outputs/action-state-answer-review-v1/{source-map.json,evidence_bundles/}`；两处最关键的硬边界是：不用最终 session state 冒充每轮 snapshot，不把 session 累积 trace 当当前轮。
- 引用检查只验证 quote 真实性；语义支持仍由 Judge 判断。dock 被拒 quote 未保留，报告标 `not_retained`，不补造。

## 3. 逐 criterion

| case | criterion | turn | machine | coverage | unresolved | refs ok/bad |
|---|---:|---|---|---|---|---:|
| contact | turn-1 intent/outcome | 1 | passed | covered | no | 2/0 |
| contact | turn-1 single-step | 1 | passed | covered | no | 2/0 |
| contact | turn-1 evidence | 1 | passed | covered | no | 3/0 |
| contact | turn-2 correction-retract | 2 | passed | covered | no | 3/0 |
| contact | turn-2 negated-not-repeated | 2 | passed | covered | no | 2/0 |
| contact | turn-2 evidence | 2 | passed | covered | no | 3/0 |
| contact | turn-3 recall-current | 3 | passed | covered | no | 3/0 |
| contact | turn-3 unknown-not-success | 3 | passed | covered | no | 2/0 |
| contact | turn-3 citation-N/A | 3 | passed | covered | no | 2/0 |
| contact | case-task | 0 | passed | covered | no | 3/0 |
| contact | case-evidence | 0 | passed | covered | no | 3/0 |
| dock | turn-1 intent/outcome | 1 | passed | covered | no | 3/0 |
| dock | turn-1 single-step | 1 | passed | covered | no | 2/0 |
| dock | turn-1 evidence | 1 | passed | covered | no | 4/0 |
| dock | turn-2 correction-retract | 2 | passed | covered | no | 2/0 |
| dock | turn-2 negated-not-repeated | 2 | passed | covered | no | 2/0 |
| dock | turn-2 evidence | 2 | passed | covered | no | 4/0 |
| dock | turn-3 recall-current | 3 | passed | covered | no | 3/0 |
| dock | turn-3 unknown-not-success | 3 | passed | covered | no | 2/0 |
| dock | turn-3 citation-N/A | 3 | passed | covered | no | 3/0 |
| dock | case-task | 0 | passed | covered | no | 3/0 |
| dock | case-evidence | 0 | passed | covered | no | 3/0（citation_compromised=true） |

- contact：28 checked / 28 accepted / 0 rejected。
- dock：38 checked / 37 accepted / 1 rejected；剩余引用全部通过。`case-evidence` 虽由仲裁判 passed，程序仍因该次 rejection 保留 `citation_compromised`，这是 case verdict 仍 uncertain 的唯一原因。

## 4. 争议

| 争议 | 当前 contract 立场 | 真实依据 | Judge 意见 | 仍需验证 | 建议修复层 |
|---|---|---|---|---|---|
| 旧题 success 表示执行还是效果 | `user_executed` 与 `outcome` 两维；执行本身是 unknown，明确恢复且范围清楚才是 success（§1/§4） | contact/dock T1 均 unknown；更正后仍 unknown | T1/T3 outcome 分离 passed；未把执行写成 success | 旧 frozen baseline expected 未改，需人审裁决历史语义 | state + data/oracle |
| 纯回忆/澄清轮 citation | 无外部事实 claim → `not_applicable`；历史检索可复用；不强制每轮 RAG | 两案 T3 均 0 current RAG、0 main-agent，只复述 effective facts | T3 citation-N/A 均 passed | 未来回忆轮若顺带加维护断言，是否稳定切换 source-supported | validation + prompt-context |
| 手动放回能充但自动失败 | `partial_success`，case 保持 active，不结案 | 本批 6 轮没有该 exact utterance | 不能据此外推 Judge 结论 | 需补一条真实 partial-success 对话 | state/execution + data |
| unknown vs failed | 未说结果 = unknown；明确仍失败才是 failed；unknown 不是 fail | 两案 T1/T2 unknown；T3 明说“结果未说明” | T1/T2/T3 unknown-not-success passed | 缺“更正后明确仍失败”的真实语句 | state/execution |
| 建议颗粒/跟进 | 一个合适下一步或必要澄清；不重复被否定假设；不强制结案 | T1/T2 只问有效步骤结果；T3 重复同一 confirm | 单步/不重复/不结案 criterion 全 passed | 纯回忆轮是否仍应给 confirm 下一步，属 UX policy | context/interaction policy |

**经理分析候选（非用户人审、非 gold）**：S1 T1 `如果充上了，说明问题就在触点接触上，不用再折腾；`、S1 T2 `如果充上了，说明问题就在遮挡或回充路径上，不用再折腾；`。用户最初是回充失败，测试动作是手动放回看充电；手动能充不能充分证明自动回充恢复或排除其它原因。相关 RAG 同时列出遮挡、传感器灰尘、红外信号、适配器/电池、手动放回可充等方向，并不支持排他因果。Judge 对应 turn/task/evidence 均 passed，故记为候选漏判/评测局限，不改冻结 rubric、Judge 结果或用户 reviews。详见 `outputs/action-state-manager-observations.json`；共性抽象为“观察 / 操作效果 / 故障原因 / 全局结案”的证据范围，下一步用“手动能充但自动仍失败”新例与反例验证，先定 context/validation scope，禁止单句 prompt 补丁。

共性判断：本批支持“执行/效果分离、更正撤销、纯回忆 citation N/A、历史检索复用”。仍需产品/人审决定的是旧 baseline 的 success 语义、纯回忆轮是否继续 confirm、以及 partial-success 的真实样本；本轮不据此改产品或题面。

## 5. 入口与只读验收

- 工作区：`outputs/action-state-answer-review-v1/`；`reviews/` 仍为 0 条，供用户填写；`freeze.json` 未改。
- 报告：`outputs/action-state-answer-review-report.json`；经理分析候选：`outputs/action-state-manager-observations.json`；GUI：`outputs/action-state-answer-review-gui-readonly.json`；headless：`outputs/action-state-answer-review-headless.json`；Inspect：`outputs/action-state-answer-review-inspect.json`（恢复后 adapter 读取：contact `1.0/included`，dock `null/excluded/verdict_uncertain`，无 system_errors；初始 system_errors 仅历史快照）。
- 当前 8955：管道托管 session `28629`，PID `160668`；`/api/health` 活着，`2 cases / 0 review / 0 exposure`。8950 仍原 PID `167188`，未动。
- GUI 动态 holdout 修复：`0` 时隐藏 split 按钮、gate、reveal，`#split=holdout` 回退 development；`16` 时保留原保护/曝光确认；用 fixture counts 0/16 + 真实 `index.html/app.js` headless 验证，未读真实留出题、未写真实 reviews。证据：`outputs/action-state-answer-review-holdout-ui.json`。
- 人审可与针对已确认问题的修复并行；无需把“先完成人审”当作继续工作的审批门。dock 仲裁已恢复成功；机器 verdict 仍受 1 条历史引用拒绝约束，冻结与 oracle 争议不因本报告擅改。

## 6. 复现/核对

```powershell
$py = "C:/Users/28980/anaconda3/envs/langchain/python.exe"
& $py scripts/summarize_action_answer_review.py
& $py scripts/check_review_ui_holdout_counts.py
& $py -m pytest tests/test_action_answer_review.py -q
node ..\dsh-judge\bin\dsh-judge.js validate --bundle-dir outputs/action-state-answer-review-v1/evidence_bundles
```

Judge 恢复记录：`../dsh-judge/outputs/action-review-recovery.json`（status completed，只补 arbitrator，contact 结果字节一致复用）。
