# 事实边界收口（diagnostic detector slice）

**状态：`closed_with_known_limits`；P1 整体仍 `in_progress`。** 日期：2026-11-14。  
本批只做诊断 detector 收口、固定 4 sessions / 6 turns 真实批次与只读归因；未改产品 runtime / prompt / spec / Judge core / rubric / UI / assets，未跑 holdout，未覆盖旧冻结结果。**这是固定批次计数，不是成功率。** 父计划已同步更新 CURRENT STAGE / CURRENT BEST NEXT 与 P1-02/03/05/06 任务行。

证据入口：

- 真实批次：[run.json](standalone.md#未包含)、[compact-results.json](standalone.md#未包含)
- 批次状态：[batch-status.json](standalone.md#未包含)
- 冻结输入：[input-freeze.json](standalone.md#未包含)
- 残余 trace：[residual-trace.json](standalone.md#未包含)
- 脚本/测试：[fact_boundary_closeout.py](../scripts/fact_boundary_closeout.py)、[test_fact_boundary_closeout.py](../tests/test_fact_boundary_closeout.py)

## 1. 真实批次实际结果

- `2026-11-14T22:56:17`–`22:57:16`：**4/4 sessions、6/6 turns、0 turn error、0 transport retry**。
- 复用 artifact `robot_vacuum-v4-6aa0ba49c6`；`run.py --check` 一次：`ok=true / service_ready=true / checksums_only=true`。
- source/artifact hash match；review 计数前后一致（32 个 eval review 文件、0 eval store 记录、0 after-v2 记录、0 action-state 记录）。
- 新定向测试 `tests/test_fact_boundary_closeout.py` 一次：**8 passed**。
- 隔离端口 8949 已释放；8950 / 8955 / 8956 前后均在，未触碰。
- 固定批次边界结果：**3 observed / 3 unobserved**，不计算 success rate。

## 2. 残余 trace 复验：不是“模型凭空编动作”

`ds-live-manual-and-auto-fail-01` 的实际链路：

| 环节 | 实际内容 |
|---|---|
| 用户原话 | 机器人自己回不到充电座，我把它放上去也充不上电。该先确认什么？ |
| 语义层 | `reseat_dock`, `outcome=failed`, quote=我把它放上去也充不上电 |
| 状态层 | `reseat_dock` / label=重新对准充电座 / `outcome=unknown` |
| prompt | “重新对准充电座：已做但结果未知（用户第1轮，消息 m-67da3bd57205）” |
| 回答 | “你提到‘重新对准充电座’已经试过，结果还不清楚……” |

**直接复验（不调用模型）：**

- `state._outcome_for_action` 对 v2 原句：动作 pattern positions=`[]` → 返回 `None`。
- 新 case T1 “随手放上……”：唯一 pattern match 在后面的问句“算试过重新对准吗”（position 36），之后无结果 → 返回 `None`；语义 failed 再次变成 state unknown。
- 新 case T2 “特意对准…放回充电座”：pattern 匹配 position 7，之后有“充不上电” → 返回 `failure`；state 保留 failed。

**归因：** 动作族有用户原话依据（`reseat_dock` 的 catalogue detail 包含“把机器人放回充电座”），不是完全虚构；但共享 label 写成“重新对准”比用户“把它放上去”多了一层对准语义。真正的错误是 **state/execution 没有采用语义 failed**，而是用动作 regex 重算，且可能匹配到问句；prompt/answer 只是忠实放大。**不能写成 confirmed model fabrication。**

## 3. 逐 case 结果（state check 与 answer semantic 分开）

| case / turn | boundary | state check | answer semantic finding | 归因 |
|---|---|---|---|---|
| `fb-partial-place-vs-align-01` T1 | unobserved | manual fails ✓；tried failed ✗（实际 unknown）；auto fails ✗（用户未说） | 正确：这不算“重新对准”，手动放上仍失败 | 真实 state 丢失 failed + 冻结 oracle 要求未说的 `automatic_return=fails` |
| `fb-partial-place-vs-align-01` T2 | unobserved | tried failed ✓；manual fails ✓；auto fails ✗（用户未说） | 正确：这次算明确对准，结果仍失败 | **仅 oracle 过强**；state/answer 正确，不是产品失败 |
| `fb-third-party-hypothetical-01` | unobserved | forbid tried ✓；forbid manual observation ✗ | 正确：不能证明你家充电座没问题；朋友家是另一台设备 | 语义层无观察，但 deterministic fallback 把“手动放上去能充”记成当前用户 `manual_charging=works` |
| `fb-correction-execution-outcome-01` T1 | observed | tried reseat failed ✓ | 记录手动失败，保持 case active | 正常 |
| `fb-correction-execution-outcome-01` T2 | observed | tried 撤回 ✓；not_tried reseat ✓ | 跟随“说错了/没有放回”的更正 | 正常 |
| `fb-explicit-auto-recovery-01` T1 | observed | resolved ✓；automatic_return works ✓ | 结案且原因未确认 | 正例；但 action label 另见下 |

**结论：** 更正与明确自动恢复两条边界 observed；部分放置/明确对准与第三方/假设三条 turn unobserved。T2 的 unobserved 是 oracle 过强，不是产品失败；T1 和第三方是真实产品问题。

## 4. 已确认的产品 issue（bounded follow-up，本批不修）

### `pi-reseat-outcome-lost`（高）

- 语义 `reseat_dock/failed` 被 `_outcome_for_action` 忽略；它用 `ACTION_PATTERNS[reseat_dock]` 重算，`放上去` 匹配不到，或匹配到后文问句“算试过重新对准吗”，返回 `None`；state 写 unknown，prompt 输出“结果未知”，回答转述“结果还不清楚”。
- 来源：[state.py:1021](../runtime/robot_vacuum_support/state.py)、[state.py:2863](../runtime/robot_vacuum_support/state.py)、[actions.py:115](../runtime/robot_vacuum_support/actions.py)。
- 证据：v2 residual + 新 T1；T2 明确“放回充电座”可保留 failed，说明是短语/重算路径问题，不是模型总体不会理解。

### `pi-third-party-fallback-observation`（高）

- 语义层对“如果朋友家的机器人……手动放上去能充”正确返回空观察；deterministic fallback 只按子句判断 `_invalid_clause`，把后半句“手动放上去能充”当成当前用户的 `manual_charging=works`，并写 `partial_success`。
- 回答本身是谨慎的（明确说不能证明“你家充电座没问题”），问题在 state 事实绑定。
- 来源：[diagnostic_scope.py:60](../runtime/robot_vacuum_support/diagnostic_scope.py)、[diagnostic_scope.py:149](../runtime/robot_vacuum_support/diagnostic_scope.py)、[diagnostic_scope.py:329](../runtime/robot_vacuum_support/diagnostic_scope.py)。

### `pi-recovery-action-label-overreach`（中/观察）

- “我让机器人自己回充，它已经自己找到充电座并开始充电”被语义层记成 `check_auto_return=success`；state label 为“检查自动回充设置”；回答进一步说“你此前已检查过自动回充设置”，用户并未说。
- 自动恢复结案本身正确，但 action label 会在转人工摘要里制造“已检查设置”的假事实。来源：[actions.py ACTION_CATALOG](../runtime/robot_vacuum_support/actions.py)、[state.py prompt_context](../runtime/robot_vacuum_support/state.py)。

**本批不修复以上 issue；它们是下一产品切片的事实输入，不是本批的改代码任务。**

## 5. Judge 边界：复用，不新增

不新跑 Judge、不改 rubric。复用 `jr-diagnostic-after-20261114-02`：

- 4/4 completed、4 passed、0 failed、0 uncertain、0 unresolved、0 citation rejected、0 system errors；coverage `7/7/7/12`。
- baseline contact 条件句排他因果子项仍 passed：已知漏判，不隐藏、不改旧结果。
- manual+auto 失败案中“重新对准充电座”仍只作 manager candidate，不是 user review/gold，不改 verdict。
- baseline 与 after 输入不同：**不做 A/B、不做成功率**。

## 6. 测量限制

- 本批是固定 4/6 批次，不是 holdout，不是成功率，不证明同类措辞的总体频率。
- `fb-partial-place-vs-align-01` 冻结 oracle 要求 `automatic_return=fails`，但用户只报告了手动放置失败；T2 的 unobserved 是 oracle 过强。冻结保留原样，没有为了变绿重跑或改判。
- `fb-correction-execution-outcome-01` T1 的语义是 `llm_structured_partial`，带一条 `capability_observation: quote_capability_or_result_mismatch`；但 executed action 与后续更正仍正确，两轮 observed。该 validation error 保留在 run.json。
- 新 case T1 的 state 丢失 failed 是独立真实问题；T2 正好证明“放回充电座”短语能保留 failed。
- prompt 行由回合后 state 重建，不是 raw provider prompt 抓包；tried/observation 行对应回答前事实，但 `suggested_actions` 可能含回答后的写入。
- 294 项 run-answer 人工语义审核仍为未完成历史，**保留为待补证据，但不再是当前必须前置门**。
- 本批不证明产品整体任务成功率，也不证明 Judge 语义质量。

## 7. STOP：当前 detector 工作到此为止

停止继续加 detector / prompt 补丁 / 新评测框架 / 新 Judge 规则。若未来重开，只允许用同一冻结输入复跑并保留失败输出；不要为了变绿重跑模型。

## 8. 下一产品步：完整可用会话 + 有证据的转人工摘要

**范围：** 一个可完成、可继续、可重启的售后支持 session；成功时正确结案；未解决时生成“只基于当前 state”的转人工摘要。**不做**新编辑器、新框架、新评测扩张、holdout 或真实外部工单。

**验收：**

1. **继续 / 重启**：同一 session 继续后，问题、已问、已试、未试、来源轮次、未解决项保持一致；重启后仍可读回；无重复副作用。
2. **成功结案**：只有明确自动回充成功才关闭任务；结案时原因仍标未确认；部分成功/手动成功不能误结案。
3. **未解决摘要**：只含当前 state 能支撑的字段——当前问题、已试动作及结果、未试/下一步、来源片段与轮次、未知项、仍需观察项；不生成真实外部工单，不编造未出现的动作/原因/成功。
4. **证据边界**：摘要区分用户已执行、助手建议、观察结果、假设；不把建议写成已做，不把局部成功写成全局恢复。
5. **展示口径**：development 验收，不是 holdout，不是成功率；不对外承诺正式售后或统计显著。

## 9. 清理与复跑

- 8949 已停止并释放；8950 / 8955 / 8956 未动；artifact、旧 run、旧 review 未改。
- `.probe` 已按精确路径删除。
- 复跑命令与边界见 [batch-status.json](standalone.md#未包含)；不要重建 artifact，不要为了变绿重跑模型。
