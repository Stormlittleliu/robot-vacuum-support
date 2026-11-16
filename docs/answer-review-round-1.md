# 24 QA 真实回答人工审核材料（round 1）

- 用途：给用户做 `run_answer` 语义审核的简明入口；**本文件不代替用户写任何 review**。
- QA run：`v1-live-development-20261113-224057-3f8412`
- fault run（另列，不混 QA）：`v1-live-development-20261113-224522-4ceb45`
- 自动结果：24 QA = **8 auto failed / 16 pending_human**；人工 run-answer 审核 **0 条**。
- 语义分母：**294 pending_human + 8 not_applicable = 302 导出项**；294 项按 case 整体判定即可，不要求逐条点 294 次。
- 失败归因与证据：[approved-development-failure-analysis.md](approved-development-failure-analysis.md)
- 结构化材料：[approved-development-failure-analysis.json](standalone.md#未包含)

## 1. GUI 现状与本轮边界

- 入口：<http://127.0.0.1:8950/>；执行者只读 `GET /api/health` / `GET /api/meta`，health=`ok`，reviewCount=32，`byScope.run_answer=0`。
- 主模型此前发现 8950 拒绝连接，用 `exec_command` 前台管道托管 session 95057 恢复 python server（PID 183548），无可见窗口、无 WMI；执行者检查时服务已恢复，执行者未启动/重启/停止服务。`docs/manual-review.md` 里的 WMI + `pythonw` 是历史启动记载，不代表本轮使用了 WMI。
- GUI 只读 dataset/run，人工状态写入 `eval/reviews`；它不调用产品 runtime、不重跑模型、不运行 holdout。本轮也没有代写记录。

## 2. GUI 操作（run-answer）

1. 打开 <http://127.0.0.1:8950/>，不要重启服务。
2. 在左侧/顶部把审核对象切到 **“运行回答”**（`run_answer`），不要停在“案例定义”。
3. 运行下拉框选择 **`v1-live-development-20261113-224057-3f8412`**。如果默认显示的是其他 run，必须手动切到这一条；旧 run 会标“历史快照”，不要用它评当前题。
4. 在列表选择 case，或直接打开下表的 `[GUI]` 深链（hash 已带 `case`、`scope=run_answer`、`run`）。
5. 看四块：逐轮 actual answer、延迟/usage、source/retrieval、trace 摘要。`expected` 只作对照，**绝不当 actual**。
6. 以整条回答判定 passed / failed / uncertain，可写多行 notes；保存是用户动作，会写 `reviewScope=run_answer` 记录。不要为了“好看”改自动分数。
7. 保存后记录带 `runId`、`caseHash`、`suiteRevision`、revision；改题后旧记录保留为历史快照。并发冲突返回 409，不要静默覆盖。

fault 两条不是 QA 分母：切 run 到 `v1-live-development-20261113-224522-4ceb45`，按 case 单独看“重启后是否恢复/是否串会话”；不要把 fault 通过算进 QA 成功率。

## 3. 判定口径（按 case，不必逐条 294 次）

每个 case 的 `semantic_counts.pending_human` 是 7–16 条语义项。建议一次看完整条回答，按四组判断：

- `required_claim`：应说的关键事实/下一步是否说到；
- `forbidden_claim`：是否出现明确禁止的断言（乱判硬件、编造已做步骤、把未做说成已做等）；
- `required_behavior`：追问/澄清/推进/不重复已试步骤等行为是否出现；
- `source_support`：引用是否支持回答中的 claim，而不只是“有文件名”。

一条回答可整体判 passed / failed / uncertain；不确定就写 uncertain 和具体疑点，不需要把 294 个 item 逐个点开。自动 failed 的 8 条也要人工看：其中有些是自动检查越界或 path variant，不应直接抄成人工 failed。

## 4. 路径可比性（先看这一列）

- **frozen/可比**：16 条，只回放 dataset user turns，可直接对照冻结题面。
- **variant/不可直接比**：8 条，dataset 的 `assistant_fixture` 保留但 live 未回放，模型生成了不同的上一轮建议；预期状态可能描述 live 从未发生的对话。包括 `correction-mop-water-level-01`、6 条 multi-recharge failed、`negation-filter-not-cleaned-01`。
- 对 variant：先看用户原文和 live 实际上一轮，再判断“这条回答本身是否对”；不要把 fixture 假设的已试动作当成用户事实。

## 5. 24 QA 清单

说明：`session` 是真实回答原文（JSONL，含 user/assistant 与 turn）；同目录 `outputs/traces/trace_20261113.jsonl` 是 trace（`trace_id` 见 JSON）。`GUI` 深链直接打开该 case 的 run-answer。

| case | 自动结果 | 路径 | 语义待审 | 自动失败检查 | 真实回答入口 | GUI |
|---|---|---|---|---|---|---|
| `rsv1-dev-ambiguity-recharge-condition-01` | auto failed | frozen/可比 | 8 | citation.present, expected_sources.cited, state.case_status, state.scope | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-ambiguity-recharge-condition-01&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-ambiguity-recharge-condition-02` | pending human | frozen/可比 | 8 | — | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-ambiguity-recharge-condition-02&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-boundary-missing-context-01` | pending human | frozen/可比 | 15 | — | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-boundary-missing-context-01&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-boundary-missing-context-02` | pending human | frozen/可比 | 14 | — | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-boundary-missing-context-02&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-boundary-out-of-domain-01` | pending human | frozen/可比 | 7 | — | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-boundary-out-of-domain-01&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-boundary-out-of-domain-02` | pending human | frozen/可比 | 9 | — | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-boundary-out-of-domain-02&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-boundary-unknown-model-price-01` | pending human | frozen/可比 | 13 | — | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-boundary-unknown-model-price-01&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-boundary-unknown-model-price-02` | pending human | frozen/可比 | 10 | — | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-boundary-unknown-model-price-02&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-correction-mop-water-level-01` | pending human | variant/不可直接比 | 15 | — | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-correction-mop-water-level-01&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-direct-base-power-01` | auto failed | frozen/可比 | 11 | citation.present, expected_sources.cited, expected_sources.retrieved, retrieval.attempted, retrieval.tool_event | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-direct-base-power-01&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-direct-base-power-02` | pending human | frozen/可比 | 10 | — | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-direct-base-power-02&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-direct-mop-no-water-01` | pending human | frozen/可比 | 11 | — | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-direct-mop-no-water-01&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-direct-mop-no-water-02` | pending human | frozen/可比 | 11 | — | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-direct-mop-no-water-02&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-direct-recharge-triage-02` | pending human | frozen/可比 | 11 | — | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-direct-recharge-triage-02&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-direct-suction-decline-01` | pending human | frozen/可比 | 12 | — | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-direct-suction-decline-01&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-direct-suction-decline-02` | pending human | frozen/可比 | 12 | — | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-direct-suction-decline-02&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-multi-recharge-closure-02` | auto failed | variant/不可直接比 | 14 | citation.filename_mapping, citation.source_fragment, expected_sources.retrieved, retrieval.attempted, retrieval.tool_event, state.tried_actions | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-multi-recharge-closure-02&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-multi-recharge-correction-01` | auto failed | variant/不可直接比 | 14 | state.tried_actions | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-multi-recharge-correction-01&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-multi-recharge-correction-02` | auto failed | variant/不可直接比 | 14 | citation.present, expected_sources.cited | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-multi-recharge-correction-02&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-multi-recharge-negation-01` | auto failed | variant/不可直接比 | 16 | citation.filename_mapping, citation.source_fragment, expected_sources.retrieved, retrieval.attempted, retrieval.tool_event, state.not_tried_actions | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-multi-recharge-negation-01&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-multi-recharge-negation-02` | auto failed | variant/不可直接比 | 14 | citation.present, expected_sources.cited, expected_sources.retrieved, retrieval.attempted, retrieval.tool_event | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-multi-recharge-negation-02&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-multi-recharge-tried-advance-02` | auto failed | variant/不可直接比 | 15 | state.tried_actions | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-multi-recharge-tried-advance-02&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-multi-topic-switch-mop-01` | pending human | frozen/可比 | 15 | — | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-multi-topic-switch-mop-01&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |
| `rsv1-dev-negation-filter-not-cleaned-01` | pending human | variant/不可直接比 | 15 | — | [session](standalone.md#未包含) | [GUI](http://127.0.0.1:8950/#case=rsv1-dev-negation-filter-not-cleaned-01&scope=run_answer&run=v1-live-development-20261113-224057-3f8412) |

## 6. 审核前提醒

- 自动 failed **不等于**人工 failed；本报告已把 8 条分为“确认产品缺陷 / 路径差异 + 独立缺陷 / 自动检查越界 / outcome 语义歧义”。
- 不要把 fixture variant 的“上一轮建议”当成用户做过的事；不要用 expected state 反推用户事实。
- 不要修改 dataset expected、旧 run、旧 review 来让数字通过；有不同意见写 notes + 证据，保留原记录。
- 现有 7 条 case_definition notes 原文和回应见分析文档第 6 节；当前没有任何 run_answer 记录，第一轮人工回答审核从这张表开始。
- 本轮不跑 holdout、不重跑模型；P1-06 仍是 `in_progress`，不能写成整体成功率或已验收。
