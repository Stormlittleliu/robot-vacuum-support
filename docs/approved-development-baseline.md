# 审核后开发集当前基线（approved development baseline）

> 日期：2026-11-13（香港）  
> 状态：`approved_development_baseline_pending_human_semantics`  
> 范围：`robot-support-v1` / v1.1 的 32 条 development 中，人工 `case_definition` 审核为 `passed` 的 26 条（24 QA + 2 fault）。  
> **不是**正式 holdout，**不是**整体任务成功率，**不是** P1-06 全完成。  
> 结构化证据：[approved-development-baseline.json](standalone.md#未包含)、
> [selection](standalone.md#未包含)、
> [source drift](standalone.md#未包含)、
> [freeze](standalone.md#未包含)。

## 1. 一句话结论

在固定的 24 条 passed QA 上，真实模型 24/24 跑完、0 模型/传输错误；自动结构检查中
16 条没有自动 fail、8 条有自动 fail；**没有一条能写成“通过”**，因为 294 个语义项仍是
`pending_human`。2 条 passed fault 单独跑，restart 恢复检查 2/2 可读，但状态/引用自动项
仍 fail，fault 不混入 QA 分母。没有人工语义审核，所以本基线只能支撑“当前可复现的
自动结构结果 + 待人工审核的原始回答”，不能支撑整体任务成功率。

## 2. 选样：题目质量 filter，不是按 Agent 表现挑题

| 项 | 值 |
|---|---|
| 数据集 | `robot-support-v1` / v1.1；48 条 = 32 development + 16 holdout |
| `cases.jsonl` SHA-256 | `13095f203f4acf1223d1f0ae7af6585edaf579ff4dde31f9bc2bed50b4be77e3` |
| 审核快照 | 32 条 `case_definition`，聚合 SHA-256 `ef7360b73b1d77372da0eadba47e212b0ac31235772ca537552f995a6e02dc75` |
| 审核结果 | 27 passed / 2 failed / 4 uncertain；7 条 notes 原文保留 |
| 入选 | 26 = 24 QA + 2 fault；全部 caseHash 与当前数据集匹配 |
| 排除 | 2 failed + 4 uncertain，原样列出，不删、不改判定、不改 dataset |
| selection SHA-256 | `0a785e24c0a8d90b6564ddce522d6c11c5c0cd1e00d3f961696a87eac152107c` |

规则：只认 `reviewScope=case_definition`；`caseHash`、`datasetCasesSha256`、`suiteRevision`
必须与当前 v1.1 一致；`passed` 入选，`failed` / `uncertain` 排除；旧 hash 记录标
`historical`，不当 current。选样在跑模型前冻结，`run.json` 记录同一 manifest、dataset
hash、review aggregate hash 和 selection hash。

排除清单：

| case | 审核 | 原因 |
|---|---|---|
| `rsv1-dev-multi-recharge-closure-01` | failed | 用户主动报告成功不典型；结案证据分级待补 |
| `rsv1-dev-negation-filter-not-cleaned-02` | failed | 话术“别当成清过”测试味重；语义 oracle 保留 |
| `rsv1-dev-correction-mop-water-level-02` | uncertain | 与 01 同族，独立机制待证明 |
| `rsv1-dev-direct-recharge-triage-01` | uncertain | 新手单步引导/是否已试信息不足 |
| `rsv1-dev-multi-recharge-tried-advance-01` | uncertain | “找不到充电座”主体/经验者歧义待定 |
| `rsv1-dev-multi-topic-switch-mop-02` | uncertain | 旧回充任务状态与一次轻量确认待补 |

**这不是 26/32 的 Agent 成功率。** 26 只是题目质量 filter 后的当前合格集。

## 3. 源漂移与 schema 兼容：有范围的 waiver

README 在审核后新增了文档内容，requirement 源 hash 从
`f3fc1eada15a3e2fc21431ea6fe5a4fd9fae92c923155f5dd6cfc5c9e6f0e87d` 漂移到
`9a62eef5455f40bfdfdec633e2fe2747049d261887e18a8bc4ccebaa2f9857fc`。本次**没有**改
dataset 里的旧 hash，也没有改 32 条 review，而是生成独立审计：

- 6 个 knowledge 文件全部未变；knowledge 漂移不豁免，出现即 error。
- 4 条不同的 README requirement quote 仍全部存在：22+4+2+4=32 条证据行中，
  30 条逐字/空白归一命中，2 条仅空白压缩后命中，0 条缺失。
- 32 条 case 的 caseHash 仍与当前定义匹配；state schema v2、questions schema
  `case-question/v1`、stable state keys 均兼容。
- 因此 waiver 只覆盖“requirement-only hash drift + 原 quote 仍存在 + 语义 case 未变”；
  不覆盖 knowledge 漂移、quote 丢失、caseHash 变化或未知 stable key。

证据：[source-drift audit](standalone.md#未包含)。  
这不是“README 随便变都放行”：条件任一不满足就必须停下重建，而不是继续跑。

## 4. 冻结的构建与运行环境

| 项 | 值 |
|---|---|
| QA artifact | `robot_vacuum-v4-ed5c3138ca`（version 4） |
| runtime hash | `f4e331a612346d98678621e16a0b0d0b0ca85c2806785c3c87ceba8ebe5b5949` |
| content hash | `7410216e9b45e31af706da3a84615373dd2fd7386ab7488d60c9aa26f399b419` |
| spec SHA-256 | `4fc4d815658852ffd9fa0fa512494a5020d01d502512d8672df4555da4e8c412` |
| prompt SHA-256 | `4e471a30ceabddb5b0e5a6b9c028c627de1c6d9bcaa3b9e135598c770784087b` |
| 外部 cwd 检查 | `run.py --check` 通过；cwd 为 workspace 根，不依赖主仓库 `PYTHONPATH` |
| source/runtime/frontend/spec/bundle | freeze 文件记录 tree fingerprint（排除 `__pycache__`/`.pyc`） |
| 模型 | `deepseek-chat`；envfile 仅由正常实例进程显式注入，未读取/打印/复制密钥值 |

QA 与 fault 各 build 一次。fault run 的 artifact `robot_vacuum-v4-e4ea1624bb` 是 eval 代码
改动后单独 build 的，但 runtime/content/spec/prompt 四个产品 hash 与 QA artifact 完全相同；
不能把不同 artifact id 说成同一个包，也不能把产品 hash 相同说成“完全同一个 artifact”。

## 5. 确定性 state 基线（不是产品回答分）

运行：`v1-state-development-20261113-224038-e005ce`  
选样：26 条（24 QA + 2 fault）；每 case 新 state，逐轮调用现有 pure state。

| 分母 | 结果 |
|---|---|
| QA 24 | 3 passed / 8 failed / 13 not_applicable |
| fault 2 | 2 unsupported（state 模式没有真实 fault 注入） |

13 条 `not_applicable` 不是 pass：它们对应 `state_score=not_applicable_current_p1`
（非回充/边界类），当前 state 模块只覆盖回充流程。2 条 fault 在 state 模式没有真实
重启注入，统一 `unsupported`，不伪造恢复成功。

> 历史 `13 failed` 是更早一次 state run 的记录，分母/代码/选样与这次不同；不能再把
> 它当作“现最新产品结论”。这次 approved 24 QA 的 state 结果是
> **3 pass / 8 fail / 13 N/A**，仍只是 state 层证据，不是回答质量。

## 6. 真实 live QA（24 条，最多 3 并发）

运行：`v1-live-development-20261113-224057-3f8412`  
端口：8943（已释放，`port_released=true`）；每 case 独立 session/workspace，完成即删
session；共享一个 artifact 和实例；QA 完成后没有继续 spawn。

| 指标 | 结果 |
|---|---|
| attempted / completed | 24 / 24 |
| QA 完成 | 16 `pending_human` + 8 `failed` |
| 模型/传输错误 | 0 |
| 显式 passed | **0**（语义未审核，不能自动通过） |
| model requests | 107（不同于 ledger events 76） |
| reported tokens | 124,647（24/24 有 usage 样本；缺失不估） |
| interpretation | 35 requests / 33,265 tokens |
| main_agent | 64 requests / 84,408 tokens |
| rag_summary | 8 requests / 6,974 tokens |
| 费用 | `null`；无权威单价，不估 |
| trace ids | 33 |
| transient retry | 0 |

延迟（原始样本，不做显著性声明）：

- case 级 n=24：p50 25,122.101 ms，p95 33,002.357 ms，mean 25,312.842 ms，max 38,730.575 ms。
- turn 级 n=35：p50 20,318.920 ms，p95 30,919.964 ms，mean 17,101.616 ms，max 32,020.127 ms。
- 原始 `raw_ms` 在 `summary.json` / baseline JSON 中保留，未删慢样本。

`summary.json` 的 `usage/fault_usage/usage_all` 由已保存的 `cases.jsonl` 离线重聚合，
没有重新调用模型，也没有改原始 answer/trace/server.log；重聚合只把 QA 与 fault 的
usage 分母分开。

### 6.1 自动结构检查（不是语义支持）

| 检查 | pass | fail | not_applicable |
|---|---:|---:|---:|
| answer.nonempty | 35 | 0 | 0 |
| citation.present | 29 | 4 | 2 |
| retrieval.attempted | 31 | 4 | 0 |
| retrieval.tool_event | 31 | 4 | 0 |
| citation.filename_mapping | 27 | 2 | 6 |
| citation.source_fragment（非空 snippet/content） | 27 | 2 | 6 |
| expected_sources.retrieved | 29 | 4 | 2 |
| expected_sources.cited | 29 | 4 | 0 |
| state.case_status | 17 | 1 | 0 |
| state.scope | 17 | 1 | 0 |
| state.tried_actions | 11 | 4 | 1 unsupported |
| state.not_tried_actions | 4 | 1 | 1 unsupported |
| state.contract | 0 | 0 | 17 |
| runtime.no_error | 24 | 0 | 0 |

文件名出现/映射到非空 retrieved fragment **不等于**语义支持；required claims、
forbidden claims、required behaviors、source semantic support 共 294 项全部
`pending_human`（另有 8 项 fixture rubric `not_applicable`）。

### 6.2 24 条 QA 的 8 条自动 fail

| case | live variant | 主要自动 fail |
|---|---|---|
| `rsv1-dev-ambiguity-recharge-condition-01` | frozen | citation.present、expected_sources.cited、state.case_status/scope（无 active case） |
| `rsv1-dev-direct-base-power-01` | frozen | 无本轮 retrieval/tool event、无 citation、expected_sources |
| `rsv1-dev-multi-recharge-closure-02` | model prior | 无 retrieval、citation 无法映射、expected_sources、state.tried_actions |
| `rsv1-dev-multi-recharge-correction-01` | model prior | state.tried_actions（success vs unknown） |
| `rsv1-dev-multi-recharge-correction-02` | model prior | citation.present、expected_sources.cited |
| `rsv1-dev-multi-recharge-negation-01` | model prior | 无 retrieval、citation 无法映射、expected_sources、state.not_tried_actions |
| `rsv1-dev-multi-recharge-negation-02` | model prior | 无 retrieval、无 citation、expected_sources |
| `rsv1-dev-multi-recharge-tried-advance-02` | model prior | state.tried_actions（success vs unknown） |

按 variant 分开：

- frozen turns：16 条，14 `pending_human` + 2 `failed`。
- model-generated prior turn：8 条，2 `pending_human` + 6 `failed`。

**关键限制：** `assistant_fixture` 是数据里的预设历史 assistant 轮，本轮 live **没有**把它
当成真实模型输出注入。含 fixture 的 case 让模型自己生成前置回答，记录为
`fixture_policy=assistant_fixture_retained_in_dataset_but_not_replayed_live`、
`live_variant=model_generated_prior_turn`。因此这 8 条与冻结 fixture oracle 不是逐字同一
对话路径；其中 6 条 fail 不能直接写成“冻结题回归”，也不能挑 fixture 让数字好看。
若需精确复现 fixture，需要可 seed 的对话历史 API；本轮不为此改产品。

## 7. live fault（单独分母，不混 QA）

运行：`v1-live-development-20261113-224522-4ceb45`；端口 8944 已释放；2 条 passed fault
串行执行，真实进程 restart + 同 storage root。

| 项 | 结果 |
|---|---|
| fault 分母 | 2 |
| fault 整体自动状态 | 2 failed |
| `fault.restart_session_recovered` | 2/2 pass（重启后 session/support 仍可读） |
| 失败点 | state.tried_actions 期望 success，实际 unknown；部分轮 citation/expected_sources、scope |
| model requests / tokens | 12 / 15,362（2/2 有 usage 样本） |
| QA 分母 | 0，未混入 |

QA + fault 合计 119 次真实模型请求、140,009 reported tokens；两个分母仍然分开，
不把 fault 的请求/延迟/状态混进 QA 成功率。

结论：**restart 恢复本身有正向证据，但 fault case 整体不能算 pass**。fault 的状态动作
结果抽取、重启后引用/scope 仍要单独归因；不把“能继续聊/能读到 session”写成完整恢复成功。

## 8. failed / unsupported / 已知限制（必须一起讲）

**自动 fail / unsupported：**

- live QA 8 条 fail（上表）；0 条 pass 是因为语义 pending，不是“8/24 成功率”。
- state 8 条 fail、13 条 N/A；2 条 fault state `unsupported`。
- live fault 2 条整体 fail；restart 恢复检查 2/2 pass，但状态/引用项 fail。

**已知限制：**

1. 24 条是人工构造 development 小集；不保证统计显著，不能外推真实用户。
2. 294 个语义项未人工审核；没有整体任务成功率、准确率或满意度。
3. holdout 16 条未运行、未用于调参；本基线不是正式 holdout。
4. 8 条 fixture-variant QA 与冻结 oracle 不是同一对话路径，不能直接比较。
5. README requirement hash 漂移只在“quote 全在、knowledge 未变、case/review 未变、
   schema 兼容”范围内豁免；不是永久 waiver。
6. state 模块只覆盖回充；非回充 state 期望是 N/A，不是 pass。
7. 费用 null；没有权威单价。
8. 单次运行、单模型/provider、单环境；模型随机性仍在。
9. QA/fault artifact id 不同，但产品 runtime/content/spec/prompt hash 相同；
   不把不同 id 说成同一个包。
10. 并发最多 3，单 case 时限 300s；未改产品 token 预算。

## 9. 根因假设与下一步（本轮不启动新产品特性修复大环）

只给聚类假设，不把假设写成已证实根因：

1. **RAG/引用路径被绕过**：部分 frozen/变体回答没有本轮 retrieval 或没有 citation
   （direct-base-power-01、negation-02 等）。下一步按 trace 看是模型未调用工具、
   工具返回空、还是回答层漏引用；不在本轮加 prompt 规则。
2. **状态在澄清回答前未激活/未更新**：ambiguity-recharge-condition-01 出现
   `case_status=null`。下一步核对 reducer 的调用时机和 clarification 分支；不改 oracle。
3. **fixture-variant 与 oracle 不同路径**：model prior turn 的动作 outcome（unknown vs
   success）和 RAG 调用与 fixture 不同。下一步决定是否需要 seedable history；在此之前
   把这 8 条单列，不混入 frozen-case 结论。
4. **fault 恢复语义与任务成功分开**：restart 可读已 pass，但 state action outcome、
   引用/scope fail。下一步先做 fault 状态抽取归因，不把“session 可读”当完整恢复。

**下一优先项：** 用 GUI 8950 的 run-answer review 入口人工审核 294 个语义项；再按 trace
聚类 8 条 QA fail；完成前 P1-06 保持 `in_progress`，不跑 holdout，不宣称整体成功率。

GUI 8950 未修改、未重启；只读 `/api/cases?scope=run_answer` 已自动发现新 live run，
例如 24 QA 的 `defaultRunId=v1-live-development-20261113-224057-3f8412`，可直接进入
run-answer 审核草稿入口。本轮没有写入 GUI 的 review store，也没有把人工 draft 计入
自动分。

## 10. 复现命令

```powershell
$py = "C:/Users/28980/anaconda3/envs/langchain/python.exe"

& $py eval/runner.py validate-data
& $py eval/runner.py select --review-status passed
& $py eval/runner.py validate-selection --manifest outputs/approved-development-selection.json

& $py eval/runner.py run --split development --mode state `
    --selection-manifest outputs/approved-development-selection.json

& $py eval/runner.py run --split development --mode live `
    --selection-manifest outputs/approved-development-selection.json `
    --bucket qa --workers 3 --port 8943 --case-timeout 300

& $py eval/runner.py run --split development --mode live `
    --selection-manifest outputs/approved-development-selection.json `
    --bucket fault --workers 1 --port 8944 --case-timeout 300
```

测试：`python -m pytest tests/test_eval_selection.py tests/test_eval_v1.py -q` =
**27 passed**（2026-11-13；含 Inspect AI 真实框架 smoke）。未跑全库、未跑 holdout。

运行边界：GUI 8950 未改；临时端口 8943/8944 已停止；所有服务/浏览器隐藏窗口；
未使用先前被拒绝的 WMI 脱离进程树方法；未读取/打印/复制密钥值；未改
`eval/datasets/v1`、32 条 review/notes、v1.1 cases/split/holdout、旧 run/artifact/assets。
