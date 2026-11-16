# 诊断范围 after 评审 v2：最终结果

状态：`completed_after_judge`。source 为 `outputs/diagnostic-scope-smoke-v2/run.json`，artifact `robot_vacuum-v4-6aa0ba49c6`，run id `ds-after-development-20261114-6aa0ba49c6`。本阶段未跑产品模型、未启动服务、未改旧 freeze。

## 数据与冻结

- 4 个新 after session / 5 turn：`ds-live-manual-works-auto-fails-01`（1）、`ds-live-clearance-manual-only-01`（1）、`ds-live-manual-and-auto-fail-01`（1）、`ds-live-auto-recovered-01`（2）。
- 每 turn 五维 criterion：观察范围、因果排他证据、条件/事实区分、下一鉴别观察、局部/全局结案。criteria 数为 `5/5/5/10`；Judge 原生 overall 另计 2 项，coverage 为 `7/7/7/12`。
- 每 turn 的 `state/semantic/observations/diagnostic_scope` 取 source per-turn snapshot；trace/transcript 按同 turn 投影；actual answer 与全文 retrieval 来自 source，不造 state。
- source 质量：5 turn / 7 条语义 observations accepted、0 validation errors；19 structural + 3 semantic checks 全通过；8949 已释放。
- `freeze.json` 写于 Judge 前：freeze `21:58:24+08:00`，Judge run-spec `13:58:33.456Z`（同一时刻 21:58:33+08），`freeze_before_judge=true`。source run/input-freeze hash 前后一致。
- 旧 `diagnostic-scope-after-review-v2/`（v1 准备版）及其 freeze/report 原样保留；baseline/v1 冻结未改。

## Judge 结果

`jr-diagnostic-after-20261114-02`，一次并发 2，**4/4 completed、4 passed、0 failed、0 uncertain、0 unresolved、0 system errors、0 citation rejected**；无 partial history，无 resume。

| case | turns | verdict | coverage | 说明 |
| --- | --- | --- | --- | --- |
| `ds-live-manual-works-auto-fails-01` | 1 | passed | 7/7 | 手动能充只作局部观察，要求自动回充验证；未说自动恢复/唯一原因 |
| `ds-live-clearance-manual-only-01` | 1 | passed | 7/7 | 明确“还不能确定是遮挡”，要求自动回充验证 |
| `ds-live-manual-and-auto-fail-01` | 1 | passed | 7/7 | 先查充电座供电/指示灯，不重复要求手动放回；但见下方 candidate |
| `ds-live-auto-recovered-01` | 2 | passed | 12/12 | T2 明确自动找到座并开始充电，可结案；明确原因仍未确认 |

逐 case/turn/criterion 的真实 judgment、coverage、citation 校验、findings/evidence refs 见 `outputs/diagnostic-scope-after-validated-report.json`。

## 经理关心点（post-hoc，不改 Judge 判定）

- **manual works 是否被当成自动恢复/唯一原因**：否。回答明确“手动放上去能充电，只说明……这一局部能力是好的”，自动回充失败原因“仍未确认”。
- **manual + auto 均 fail 是否重复问已知 manual**：没有重复要求手动放回；先问充电座供电/指示灯。**但回答虚构了用户未说过的“重新对准充电座”已试过**，Judge 仍 passed。该点单列为 `manager_candidate`，不代用户改判。
- **D4 自动恢复是否可结案但不说原因确认**：是。回答写“当前任务目标已达成，可以结案”，同时明确“不代表之前失败的原因已经查清。原因目前仍未确认”。

manager_candidate：`mc-after-manual-auto-fail-fabricated-realign`（`candidate_needs_counterexample`，not_user_review/not_gold，exact quote 已保留）。它不影响冻结 rubric 或 Judge verdict。

## 边界

- 原 baseline 2 对话与 after 4 个新例输入不同，**不做成功率、不做 A/B 能力提升**。
- baseline contact 条件句因果子项仍漏判是已知限制，已在 report 的 `input_comparison` 明示，不隐藏。
- Judge 结果是机器建议，不是人工 review；`reviews/` 仍为 0，未人工 seed。
- TestClient 只读核验：4 case、caseHash、Judge selected 4 passed、0 historical、0 review、无服务启动；证据 `outputs/diagnostic-scope-after-review-v2-validated/gui-readonly-testclient.json`。
- 隐藏 8956 + Chrome headless 已核验 `ds-live-manual-and-auto-fail-01` / `ds-live-auto-recovered-01`：实际 answer 可见、选中 case 高亮、selected Judge `passed`、coverage complete、caseHash 当前、0 review、0 holdout；证据 `gui-headless.json` + 两张 screenshot。8956 已停止；未跑 Inspect；8950/8955 未动；未改产品 runtime/GUI/Judge core/父计划。
- 测试：after 定向 **15 passed**（最终报告逻辑变更后）；此前两文件一轮 **27 passed**。

## 下一步

1. 可选人工 review 4 case/5 turn；不代写。
2. 不重跑 Judge；若经理明确要求复跑，使用同一 judge id/output-dir 的 resume，成功 role 复用，不重跑整批、不改 rubric。
3. 报告/工作区入口：
   - `outputs/diagnostic-scope-after-validated-report.json`
   - `outputs/diagnostic-scope-after-review-v2-validated/`
   - `outputs/diagnostic-scope-after-review-v2-validated/freeze.json`
   - `outputs/diagnostic-scope-after-builder-readiness.json`（已更新为真实完成状态）
