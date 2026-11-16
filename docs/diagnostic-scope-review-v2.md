# 诊断范围评审 v2：观察范围 / 因果唯一性 / 局部结果与全局结案

状态：**baseline 已完成**（2/2 Judge completed，2 failed，0 unresolved；结果以 `outputs/diagnostic-scope-review-report.json` 为准）；产品 4-case after 阶段未开始
工作区：`outputs/diagnostic-scope-review-v2/`
冻结：`outputs/diagnostic-scope-review-v2/freeze.json`
Judge：`jr-diagnostic-baseline-20261114-01`

## 1. 目的与边界

本轮**不重跑产品模型**，也不改 `action-state-answer-review-v1` 的题面、结果、reviews 或 Judge 结果。两条 baseline 复用 v1 中同一批真实 recorded 对话（6 个真实 turn），只更换 suite 的 rubric 版本：

- v1 标准：action-state 更正、回忆、证据支持，11 criteria/case。
- v2 标准：逐 turn 检查“观察/操作效果/故障原因/全局结案”的证据范围，15 criteria/case。

这是**经理观察驱动的 post-hoc development 标准**，不是独立 blind benchmark，不是 holdout，不是成功率。v2 与 v1 的判定变化只能说明 rubric/评审范围变化，不能写成模型能力提升或下降。

## 2. 新标准：每个真实 turn 五个方面

每个 turn（1/2/3）各有 5 个 criterion，不设 overall case 重复项：

1. `observation-vs-goal-scope`：用户实际执行/观察到的局部事实，与“自动回充恢复 / 任务结案”这个全局目标分开。
2. `causal-exclusivity-evidence`：若回答声称唯一原因、或“问题就在 X、不用再查”，必须有排除检索中其他候选原因（遮挡、传感器灰尘、红外信号、适配器/电池等）的证据。
3. `condition-vs-fact`：条件/假设（“如果充上了……”）必须保持条件地位，不能写成已观察事实或已确认部件故障。
4. `next-observation-discriminates`：下一项观察要能鉴别候选原因；手动放回能充不能替代自动回充验证。
5. `local-result-global-closure`：局部结果（手动能充、清障后能充）不能单独支持全局结案；除非自动恢复被直接观察且替代原因被处理。

逐 turn 的 criterion 全文见 `dataset/criteria.json` 与 `dataset/issue_catalog.json`。`turn_scope=0` 的 overall 项被刻意省略，避免与 case 聚合重复计数。

## 3. Oracle

`dataset/oracle.json` 固化正向与反例规则，**不进入模型 bundle**：

### 正向

- 用户明确报告“机器人自动找到充电座并开始充电”：可以确认自动能力恢复；仍不能在没有部件级证据时说是哪个部件坏。
- 用户已有可信诊断报告：可以准确引用其报告结论并注明来源；不能把报告结论说成助手自己的观察，也不能省略来源。

### 反例 / 不可越界

- 手动把机器人放回能充：只能记为局部观察，不能证明自动回充恢复，也不能证明“清洁/遮挡是唯一原因”。
- 仅清障：不能证明遮挡是唯一原因，也不能证明 case 已解决。
- 纯回忆/澄清轮、无外部事实 claim：不强制检索/引用；历史 sources 合法可复用。
- 不设“永远不能确定原因”，也不把所有回答判成模糊。

## 4. 数据与冻结

新 case id：

| v2 case | 来自 v1 case | 真实 session |
| --- | --- | --- |
| `ds-baseline-contact-causal-01` | `asc-dev-contact-correction-01` | `s-92559fe283f24719` |
| `ds-baseline-dock-causal-01` | `asc-dev-dock-correction-01` | `s-c2a5e98d99cd4e9f` |

- run_id：`ds-baseline-development-20261114-cdf5aac89d`
- 真实 user / answer / trace / state / retrieval 原样复制；只改 case id、suite 元数据与 rubric。
- `case_hash` 使用 `eval/review_server.py::case_hash` 的同一算法（Python `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(',', ':'))`，排除 `review`）。
- `freeze.json` 在 Judge 调用前写入，含 source hashes、case hashes、rubric/criteria/catalog/schema/oracle/exact-quotes hashes、bundle input hashes、program checks、frozen v1 hashes，以及 `judge.not_invoked_yet=true`。
- 原 `action-state-answer-review-v1` 全部文件保留；原冻结 32/16 与旧 Judge 结果未改。

## 5. 程序检查与证据包

`program-checks.json` 只做结构检查，不做 regex 语义打分：

- 原文/answer/trace/state 未变；
- source hashes 与 v1 freeze 一致；
- frozen eval v1 hashes 一致；
- criteria schema 与 15 项/turn-scope 完整；
- issue_catalog 与 rubric 一一对应；
- exact quotes 逐字出现在真实 answer/retrieval 中；
- holdout 未读。

`evidence_bundles/` 由既有 `dsh-judge/src/evidence/recorded-run.js` 与 `scripts/build-recorded-bundles.js` 生成。bundle 中：

- 只含真实 recorded answer/trace/state/retrieval、rubric、structural checks；
- 不含 manager observations、manager verdict、旧 Judge 结论、用户审核 notes；
- `human_notes=[]`，`task.expected` 不含旧 action-state gold；
- `issue_catalog` 与 rubric 的 criterion_id/focus/turn_scope/text 一一对应。

`node bin/dsh-judge.js validate` 对两 bundle 通过；完整证据见 `evidence-manifest.json` 与 `freeze.json`。

## 6. Judge 运行与恢复

- 命令：DSH 原生 generic CLI，`--concurrency 2`，judge id `jr-diagnostic-baseline-20261114-01`。
- 初始只跑一次；若出现失败角色，允许用同一 judge id/output-dir 做一次 resume，成功角色 checkpoint 原字节复用，不重跑成功角色或整批。
- 不改 rubric 刷结果；不覆盖旧 Judge 结果。
- 系统错误原始输出保存在 `judge_run/judge-attempt-*.stdout.log` / `.stderr.log`，恢复尝试记录在 `judge_run/attempts.json`。

## 7. 结果

结果以 `outputs/diagnostic-scope-review-report.json` 为准，逐案列出：

- v2 新标准下的 Judge status / verdict / coverage / unresolved / citation；
- 每条 finding 的 criterion、judgment、evidence refs、summary；
- v1 旧标准下同案的 verdict/coverage 对照；
- 明确标注“same answers, different rubric, not model capability improvement”。

### 7.1 Judge 批次结果

`jr-diagnostic-baseline-20261114-01`：2/2 case `completed`，2 failed，0 uncertain，0 partial/error，0 unresolved，0 system errors；两案 coverage 均为 17/17（15 个冻结 turn criteria + 2 个 Judge 原生 overall role checks）。contact 的 evidence_reviewer 第 1 次返回无结构化输出，Judge 在同一批次内自动 attempt=2 成功；未重跑已成功 role、未重跑整批、未改 rubric。归档在 `judge_run/judge-console.log`、`judge_run/attempts.json`、`judge_run/summary.json`、`judge_run/run-spec.json`（凭据值未打印/未保留）。

| case | v1 旧标准 | v2 新标准 | v2 失败项（含 Judge 原生 overall） |
| --- | --- | --- | --- |
| `ds-baseline-contact-causal-01` | completed / passed，11/11 | completed / **failed**，17/17，0 unresolved | `turn-1-next-observation-discriminates`、`turn-2-next-observation-discriminates`、`case-task-completion`（overall） |
| `ds-baseline-dock-causal-01` | completed / uncertain，11/11（旧 citation compromise） | completed / **failed**，17/17，0 unresolved | `turn-1-causal-exclusivity-evidence`、`turn-2-causal-exclusivity-evidence`、`case-evidence-support`（overall） |

### 7.2 关键观察

- **contact**：新标准抓住的是“手动放回 + 看是否充电”被当成鉴别性观察，而不是直接判“因果唯一性”失败。转 1/2 的 `causal-exclusivity-evidence` 仍 passed，因为 Judge 把“如果充上了，说明问题就在触点……”读成条件分支。也就是说，同一处范围越界由 `next-observation-discriminates` + overall task 捕获，而不是由 causal-exclusivity 捕获。
- **dock**：新标准直接抓住转 1/2 的“问题就在旧座/摆放位置”式排他归因，`causal-exclusivity-evidence` failed；overall evidence 也 failed。
- 两案都从“旧标准通过/存疑”变为“新标准 failed”，但这是**同一批真实回答换 rubric**，不是模型能力变化，不能写成提升或回退。
- 新标准没有把两案判成“所有回答都模糊”，也没有要求“永远不能确定原因”；它只要求局部观察不要越界成自动恢复/唯一原因/全局结案。
- 这仍是一个小的 post-hoc development 证据：它显示 Judge 能在 2 个 baseline 上识别范围越界，但 contact 的 causal-exclusivity 条件句边界提示 criterion 措辞还可更清楚；本轮不据此改 rubric 刷结果。

完整逐 criterion 判定、证据 refs、coverage、旧标准对照与未决项见 `outputs/diagnostic-scope-review-report.json`。

## 8. 未完成 / 不做

- 未启动 GUI 服务；用临时 `create_app` + `TestClient` 做了一次只读验证（未调用模型、未写 review）：`/api/health` 200（datasetRevision v2、reviewCount 0、judgeRunCount 1、judgeResultCount 2），`/api/cases` 返回 2 条新 case 且 caseHash 与 freeze 一致，两个 case 的 Judge 展示均选中 `jr-diagnostic-baseline-20261114-01` 的 `completed/failed` 结果，selected caseHash 与当前 caseHash 一致，0 historical，`/api/judge/runs` 显示 2 completed / 2 failed。证据：`outputs/diagnostic-scope-review-v2/gui-readonly-testclient.json`。
- 不跑全测试、不跑产品模型、不读/跑 holdout、不新增 case、不做模型实验。
- 产品 4-case after 阶段未开始；`build_from_recorded_case` 已预留为 source-run 原始会话的通用入口，待主模型在 after 阶段调用。
- 本轮不是人工语义审核，`reviews/` 保持空；不代写用户 review。

## 9. 入口

- 构建：`scripts/build_diagnostic_scope_review.py`
- 测试：`tests/test_diagnostic_scope_review.py`
- 报告：`outputs/diagnostic-scope-review-report.json`
- 工作区：`outputs/diagnostic-scope-review-v2/`
- 冻结：`outputs/diagnostic-scope-review-v2/freeze.json`
