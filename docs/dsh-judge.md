# DSH Judge 只读接入（Gui + Inspect AI）

本文件是 `robot-vacuum-support` 侧对独立 DSH Judge 结果的集成契约，供主模型把
另一任务产出的 6 个真实 case 结果接进人工审核 GUI 与 Inspect AI。

> **本轮验证范围：模拟契约验证。** 本文和 `tests/test_judge_*.py` 使用临时合成的
> `dsh-judge/v1` 结果验证路径白名单、hash/run 绑定、历史标记、GUI 空态/错误态和
> Inspect 分母策略；**不是**真实 DSH 6 case 运行，不代表模型质量、成功率或正式评测。
> 真实 6 case 由另一并行任务运行，结果落到本文约定的只读路径后即可被本接入读取。

## 0. 工程原则：LLM 聚焦判断，程序负责跨任务聚合

这是设计原则，不是“数学上 LLM 不能做多对多”的断言：

- 在**明确语境 / prompt**内，LLM 适合承担两类聚焦工作：
  1. **多证据 → 一个聚焦判断**（例如 evidence reviewer 对本轮回答的证据支持给出
     一个 verdict/reason）；
  2. **一个已确定问题 → 多个候选展开**（例如基于已知症状生成候选排查方向）。
- **跨任务的多对多关联、调度、合并、版本与执行责任**由确定性程序承担。程序负责
  选择哪个 case/run、绑定哪个 caseHash、聚合哪些角色、何时重跑、结果写到哪；LLM
  不承担跨任务全局编排。
- 本接入中模型角色保持单 focus：`task_reviewer` 判 task completion，
  `evidence_reviewer` 判 evidence support；**case verdict 由程序按规则聚合**，
  只有**同一 focus 内部矛盾**或**证据不足**才进入仲裁；默认**不**因为跨维度
  （task vs evidence）分歧就触发仲裁。
- 这样做的收益是可解释、可复现、可审计：hash/run 绑定与分母策略在程序侧，模型
  只对局部语义给出聚焦判断；这也是为什么 `error` / `partial` / `uncertain` 不会被
  程序当成通过。

当前接入对应实现：roles 逐角色单 focus；case verdict 由结果字段与程序策略使用；
`arbitration.required` 只在同 focus 冲突或证据不足时由 Judge 置真；接入侧不额外
制造跨维度仲裁。原生插件已产出 `jr-native-20261114-05`（v1，6/6，全部
legacy_unverified）和新 run `jr-audited-20261114-01`（v2，6/6 machine observed）。
**旧 4 included / 2 excluded 已作废为历史**；新 v2 6/6 中 3 条 trusted failed、
3 条 provisional_unresolved，均未经人工语义裁决，不是成功率或最终产品结论。

## 1. 结果位置与契约

只读路径：

```text
eval/judge_results/<judge_run_id>/<case_id>.json
```

- 目录名 `judge_run_id` 与文件名 `case_id` 都只允许
  `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`，不允许 `.` / `..` / 路径分隔符；
- 服务端不接受调用方传入任意路径，只按这两个 id 拼接受限根目录；
- 符号链接指向根目录外的文件不会被当作结果；
- 结果目录不存在是合法空态，不是错误，更不是通过。

单个结果的最小契约：

```json
{
  "schema_version": "dsh-judge/v1",
  "case_id": "rsv1-dev-...",
  "run_id": "v1-live-development-...",
  "case_hash": "<当前 case 定义 sha256>",
  "input_hash": "<judge 看到的输入 hash>",
  "judge_run_id": "<与目录名一致>",
  "status": "completed",
  "verdict": "passed",
  "roles": [
    {
      "role": "semantic_support",
      "verdict": "passed",
      "findings": [
        {
          "kind": "agent",
          "severity": "medium",
          "summary": "一句话结论",
          "evidence_refs": [{"id": "doc#chunk", "quote": "原文逐字引用"}],
          "suggested_layer": "validation"
        }
      ],
      "reason": "为什么这样判"
    }
  ],
  "arbitration": {"required": false, "reason": "角色一致"},
  "limitations": ["只覆盖本轮回答"],
  "provenance": {
    "dsh_version": "...",
    "model": "...",
    "prompt_version": "...",
    "workflow_version": "..."
  }
}
```

约束：

- `status ∈ {completed, error, partial}`；`verdict ∈ {passed, failed, uncertain,
  not_applicable}`；
- `findings[].kind ∈ {agent, evaluation, insufficient_evidence}`；
- `evidence_refs[].id` 和 `.quote` 都非空；GUI 直接展示 quote 原文；
- `arbitration.required` 必须是布尔；`roles`、`limitations`、`provenance` 必须是
  正确类型；
- 可选原生字段（旧结果可缺省）：`findings[].criterion_id` / `turn_scope` /
  `judgment`；`aggregation.source` / `case_verdict` / `issues[]` /
  `disputed_issue_ids` / `unresolved_issue_ids` /
  `cross_focus_verdict_mismatch_is_not_dispute`；v2 还可选
  `aggregation.coverage` / `aggregation.counts`（或 flat `*_count`）和顶层
  `system_errors[]`。适配器原样保留这些字段，GUI 展示 criterion、轮次、单 focus
  判断、程序聚合依据、coverage/missing/unbound/uncertain/unresolved、counts 与
  system_errors；缺省时显示“旧结果未提供 aggregation/coverage”，不推断；
- **coverage 是 v2 可信评分的必要条件，不是版本号自动信任。** `coverage` 至少要能
  看出 `expected/covered` 或 `issues_total/issues_covered`，`missing_issue_ids` /
  `unbound_issue_ids` / `uncertain_issue_ids` / `unresolved_issue_ids` 为空（或对应
  `*_count == 0`），`binding_errors` 为空；缺 coverage → `coverage_missing`，不完整 →
  `coverage_incomplete`，两者都不进可信分母；
- `provenance.workflow_version` 为 v1（或缺 version 且无 coverage）→
  `legacy_unverified`；旧结果仍可查看 verdict/roles，但不作为当前可信评分；
- `system_errors` 非空 → `system_errors`，机器结果不完整，不当作通过；
- `case_hash` 必须是**当前** case 定义 hash（与人工审核 GUI 的 `caseHash` 同一算法，
  排除可变的 `review` 字段）；旧 hash 只作历史，不作为当前建议；
- `run_id` 必须与 GUI 当前选择的 run 一致；
- `input_hash` 只展示/记录，用于来源绑定；当前 run 行没有可独立重算的 input hash，
  因此接入不伪造该值的二次校验。

## 2. 只读 API

服务端新增/扩展的接口全部是 `GET`，只读文件，不触发模型、不运行 case、不写人工
状态、不覆盖 notes、不运行 holdout：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 存活 + `judgeRunCount` / `judgeResultCount` / `judgeSchemaVersion` / `canRunJudge=false` |
| GET | `/api/meta` | 增加 `judge` 只读边界与 overview |
| GET | `/api/judge/runs` | 列出白名单根下的 judge run、case 数、status/verdict 计数、malformed 数、provenance |
| GET | `/api/cases/{case_id}/judge?runId=...` | 当前 case + run 的 Judge 建议、历史结果、格式错误 |
| GET | `/api/judge/results/{judge_run_id}/{case_id}` | 按两个安全 id 读取单个结果（调试/集成） |

`/api/cases/{case_id}/judge` 返回要点：

```json
{
  "available": true,
  "currentCaseHash": "...",
  "selected": {
    "judgeRunId": "...",
    "runId": "...",
    "caseHash": "...",
    "status": "completed",
    "verdict": "passed",
    "roles": [],
    "arbitration": {},
    "limitations": [],
    "provenance": {},
    "scorePolicy": {
      "included": true,
      "denominator": "included",
      "reason": "completed_verdict_with_evidence",
      "reviewTrust": "trusted"
    },
    "evidenceTrust": {
      "checked": 30,
      "verified": 29,
      "unverified": 1,
      "unverifiedIds": ["ev-rubric-..."]
    }
  },
  "results": [],
  "historical": [],
  "errors": [],
  "message": null,
  "boundaries": {
    "readOnlyJudgeResults": true,
    "noModelCall": true,
    "doesNotWriteHumanReview": true,
    "doesNotOverwriteNotes": true,
    "evidenceQuotesLocallyVerified": true,
    "judgeArtifactUntrusted": true
  }
}
```

选择规则（实现见 `eval/judge_adapter.py`）：

1. 只考虑 `case_hash == 当前 caseHash` 且 `run_id == 请求 runId` 的结果；
   `runId` 为空时即使 hash 匹配也不成为 `selected`；
2. 多个匹配时按 `finished_at` / `created_at` 再按 `judge_run_id` **稳定倒序**取最新；
   缺少顶层时间戳时回退到原生 `provenance.completed_at`，不按文件系统枚举顺序取旧结果；
3. hash 不匹配的归入 `historical`；绑定其他 run 的归入 `results` 但不成为
   `selected`；
4. 无匹配 → `available=false` + 明确空态消息，语义评分保持 pending；
5. JSON/schema 错误 → `errors[]`（`invalid_json` / `unsupported_schema_version` /
   `case_id_mismatch` / `invalid_*` 等），**不当通过**；
6. `status=error|partial` 即使文本 verdict 写 `passed`，也显示红色“不足以判定”，
   并且不进评分分母；
7. `system_errors` 非空 → `system_errors`；
8. `provenance.workflow_version` v1 / 缺 version 且无 coverage →
   `legacy_unverified`（旧 6/6 即属此类；旧 4/2 仅历史）；
9. v2 但缺 `aggregation.coverage` → `coverage_missing`；coverage 不完整
   （missing/unbound/uncertain/unresolved 非空或 expected>covered）→
   `coverage_incomplete`；**不能只凭 v2 版本号信任**；
10. 有 `aggregation.unresolved_issue_ids` 的结果保留展示，但 `scorePolicy.reason=
   unresolved_arbitration`、`reviewTrust=provisional_unresolved`，不进可信分母；
11. `aggregation.case_verdict` 与顶层 `verdict` 不一致时 `reason=
   aggregation_verdict_mismatch`；
12. verdict=failed 但失败 finding 只有 `kind=evaluation`（rubric/检查器问题）时
   `reason=failed_evaluation_only`，不把 evaluation 错误算成 Agent 失败；
13. `evidenceTrust` 用当前 dataset case + 选中 run 原文做空白无关的包含/结构化
   key-value 校验；未命中的 Judge quote 仍展示但明确标“本地未追溯到”。**未追溯
   ≠ 伪造**：本地白名单不包含 Judge prompt/证据包中的派生 rubric，可能是合法文本，
   保持存疑，不据此判 Agent 错，也不信任 artifact 自报的
   `provenance.citation_validation`。

## 3. GUI 行为

- 新增独立 `#judge-panel`，位于实际回答面板 `#run-panel` **之后**；
- `run_answer` scope 下主列顺序仍是：实际回答 → Judge → 对话/预期 → 来源，
  保证主回答首屏可见；`case_definition` 下 Judge 仍紧跟运行快照之后；
- 展示：总体判定、status、每角色 verdict/reason/findings、finding 的 kind/severity/
  summary、`evidence_refs` 原文 quote 与 id、`suggested_layer`、分歧仲裁（含额外字段）、
  limitations、`dsh_version/model/prompt_version/workflow_version/run_id/case_hash/
  input_hash`、错误与 malformed 信息；
- `error` / `partial` 明确红色提示，绝不渲染为“通过”；
- 历史 caseHash 明确“历史快照”，不作为当前建议；
- 无结果显示“暂无 Judge 建议”空态，并说明需由独立 Judge 任务生成；
- `unresolved_arbitration` / `failed_evaluation_only` / `aggregation` 不一致 / 本地
  未追溯到的 quote 都有独立警示，不把 Judge 建议写成最终 Agent 结论；
- 快速切换 case/run 时使用单调 `state.loadSeq` 守卫：发起新请求前先清空旧 Judge
  卡片，旧响应（包括故意延迟的 judge 请求）返回后因 seq 不匹配被丢弃；有 headless
  回归测试 `test_stale_judge_response_cannot_overwrite_newer_case`；
- 面板纯只读，不写人工通过/不通过、不覆盖 notes、不改 dataset/run/holdout；
- 全局 CSP 仍为 `default-src 'self'`，无外部依赖。

## 4. Inspect AI 薄入口

`eval/inspect_tasks.py` 增加可选只读参数：

```python
from eval import inspect_tasks

task = inspect_tasks.build_inspect_task(
    cases,
    judge_results_root="eval/judge_results",
    run_id="<selected run>",       # 绑定当前回答
    judge_run_id=None,             # 可选：固定某个 judge run
    run_answers={case_id: actual_answer},  # 让 judge scorer 的 answer 是真实回答
)
```

未提供 `run_answers` 时，judge scorer 的 `answer` 会标明
`answer_source=state_completion_fallback`，不得把它误读成 Judge 实际看到的回答。
```

也可直接跑真实框架 smoke：

```python
logs = inspect_tasks.run_inspect_state_smoke(
    cases,
    log_dir="<temp>/inspect-logs",
    judge_results_root="eval/judge_results",
    run_id="<selected run>",
)
```

评分分层：

- `robot_support_v1_structural`：原有程序 checks（state/answer/citation 等），值仍是
  结构状态 1/0；不吸收 Judge 结论；
- `robot_support_v1_judge_semantic`：只在提供 `judge_results_root` 时加入；值：

| 情况 | value | denominator | reason（metadata） |
|---|---|---|---|
| v2 complete coverage + passed/failed + 当前 hash/run + 无未决仲裁 + 含原文证据（failed 至少有 agent finding） | 1.0 / 0.0 | included | `completed_verdict_with_evidence` |
| v1 / 缺 version 且无 coverage | `{"score": null, ...}` | excluded | `legacy_unverified`（旧 6/6 即此） |
| v2 但缺 coverage | `{"score": null, ...}` | excluded | `coverage_missing` |
| coverage 不完整 | `{"score": null, ...}` | excluded | `coverage_incomplete` |
| system_errors 非空 | `{"score": null, ...}` | excluded | `system_errors` |
| unresolved_issue_ids 非空 | `{"score": null, ...}` | excluded | `unresolved_arbitration`（`reviewTrust=provisional_unresolved`） |
| failed 但只有 evaluation findings | `{"score": null, ...}` | excluded | `failed_evaluation_only` |
| aggregation.case_verdict ≠ 顶层 verdict | `{"score": null, ...}` | excluded | `aggregation_verdict_mismatch` |
| uncertain | `{"score": null, ...}` | excluded | `verdict_uncertain` |
| not_applicable | `{"score": null, ...}` | excluded | `verdict_not_applicable` |
| error | `{"score": null, ...}` | excluded | `status_error` |
| partial | `{"score": null, ...}` | excluded | `status_partial` |
| completed passed/failed 但无 quote 证据 | `{"score": null, ...}` | excluded | `missing_evidence` |
| 旧 caseHash / 其他 run / 未绑定 run | `{"score": null, ...}` | excluded | `historical_case_hash` / `run_mismatch` |
| 无结果 | `{"score": null, ...}` | excluded | `no_result` |
| malformed / schema 不支持 | `{"score": null, ...}` | excluded | `malformed_result` |

- judge scorer 的 `metrics=[]`，排除项不会以 0.0 混入准确率分母；
- 排除项仍把 status/verdict/roles/evidence/errors/provenance 放在 metadata；
- `programChecksSeparate=true` 和 `score_kind` 明确程序 checks 与 judge 语义分开；
- 无结果时 semantic review 仍是 pending，不是 pass。

## 5. 主模型集成步骤

1. 由并行 DSH 任务跑 6 个真实 case，确认 `schema_version=dsh-judge/v1`，写入
   `eval/judge_results/<judge_run_id>/<case_id>.json`；
2. 只读核对 `case_hash` 与当前 `eval/datasets/v1/cases.jsonl` 的 case 定义 hash 一致；
3. 8950 已由主模型恢复为 `manual-review/v2`（PID `162044`），Judge API/UI 已加载；
   本轮执行者**没有**重启、停止或启动 8950。未来若再改 Python 侧，仍需主模型安排
   滚动重启；当前只读健康检查见第 8 节；
4. 重启后打开 GUI，在运行回答 scope 选择 run，Judge panel 会出现在实际回答之后；
5. 需要 Inspect 时用第 4 节参数，不把 judge 语义分并入 structural 指标。

启动命令（含只读 judge 根）：

```powershell
$py = "C:/Users/28980/anaconda3/envs/langchain/python.exe"
& $py eval/review_server.py --host 127.0.0.1 --port 8950 `
  --store eval/reviews --state-file eval/reviews/server_state.json `
  --judge-results eval/judge_results
```

## 6. 验证命令与证据

```powershell
$py = "C:/Users/28980/anaconda3/envs/langchain/python.exe"
& $py -m pytest tests/test_judge_integration.py -q
& $py -m pytest tests/test_judge_ui.py -q   # 临时 8952，隐藏窗口，结束自动清理
```

- 适配器/API/Inspect：`tests/test_judge_integration.py` 16 passed（含 v2
  coverage/counts/system_errors、legacy_unverified、run 绑定、unresolved/
  evaluation-only 分母、provenance 时间选择、quote 校验、Inspect 实际回答）；
- headless GUI：`tests/test_judge_ui.py` 2 passed（coverage 展示 + 旧 judge 响应竞态），
  证据 [`judge-ui-acceptance.json`](standalone.md#未包含)、
  [`judge-race-acceptance.json`](standalone.md#未包含)、
  [`judge-ui-top.png`](standalone.md#未包含)（实际回答首屏）与
  [`judge-ui.png`](standalone.md#未包含)（Judge 面板）；
- 汇总报告：[`judge-integration-report.json`](standalone.md#未包含)；
  本轮机制审计：[`judge-mechanism-audit.json`](standalone.md#未包含)。

## 7. 边界

- 只读 `eval/judge_results`，不开放任意文件；路径穿越/非法 id 返回 400/404；
- 不调用模型、不跑 Judge、不跑 holdout、不写 `eval/reviews`、不改人工 notes；
- 不使用真实 judge 结果调参，不把 uncertain/error/partial/not_applicable 当通过；
- 模拟结果只验证契约；真实 DSH 6 case 的语义质量与结论由另一任务和主模型验收；
- 不修改 `../dsh-judge`、`plans/`、生产 dataset/holdout/旧 runs/runtime/assets。

## 8. 当前入口与真实验收状态

- 当前 GUI：`http://127.0.0.1:8950`，`manual-review/v2`，PID `169856`，本轮未重启/
  停止。本轮改 Python/UI 后，**新 coverage/legacy 规则需主模型重启 8950 才生效**。
- **历史结论（已作废，不可当当前）**：`jr-native-20261114-05` 旧读数为 6/6 completed、
  6/6 failed、4 included / 2 excluded。新策略下它 workflow_version=v1 且无 v2
  coverage，**6/6 全部 `legacy_unverified`**，只作历史展示；旧 4/2 不再引用。
- **当前 v2 machine 结果**：新 run `jr-audited-20261114-01` 在审计开始时 4/6，交付前
  已出 **6/6** artifact；全部 v2 coverage complete。3 条 trusted failed
  （ambiguity01、direct-base-power01、closure02）；3 条 provisional_unresolved
  （correction01 `case-evidence-support`；correction02/negation02
  `turn-3-expected-state-1`）。**这是机器结果，未经人工语义裁决，不写成成功率或
  最终产品结论，也不把 6 failed 当 6 个产品 bug。**
- v2 契约字段：`aggregation.coverage`（expected/covered/missing/unbound/binding_errors
  与 *_issue_ids）、`aggregation.counts`/flat `*_count`、`system_errors` 已保留并在
  GUI 显示；缺 coverage 或 coverage 不完整一律不进可信分母。
- quote 本地校验：旧 6 条共 268 条 quote，266 条可追溯到 dataset case + 当前 run；
  2 条 `ev-rubric-*` 本地未追溯。按本轮口径，**未追溯≠伪造**，可能是输入证据包中
  合法派生 rubric，保持存疑，不据此判 agent 错。
- 回归：`test_judge_integration` 16 passed、`test_judge_ui` 2 passed（共 18 passed）。
  未跑全库、未跑 holdout、未调用模型、未改旧 run/原始结果。
- 主模型入口：新 6/6 出来后，重启 8950，只读核对每 case `coverageComplete`、
  missing/unbound/uncertain/unresolved、`systemErrors`、`reviewTrust`；旧
  `jr-native-05` 应显示 `legacy_unverified`，新 v2 结果才按 coverage 计分。
