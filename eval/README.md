# P1 评测入口（formal v1）

本目录只服务独立工程 `robot-vacuum-support`，不依赖主仓库 `PYTHONPATH`。正式数据是
`eval/datasets/v1/` 下的 `suite.json`、`cases.jsonl`、`split.json`、`audit.json`；
v1 是后续唯一权威。

当前“审核后开发集基线”见
[`docs/approved-development-baseline.md`](../docs/approved-development-baseline.md)：
24 条 passed QA 真实 live + 2 条 passed fault 真实 live；294 个语义项待人工审核，
不是整体成功率，也不是正式 holdout。

## 评测口径（2026-11-15）

本 README 是 P1 评测口径的权威说明。压测按来源与参与方式分为 AI 生成的刻意边界与对抗压测、人工探索性压测。来源、案例生成方式和评分方式分开保存，评分可以是机器评分或人工评审，AI 生成案例可人工复核，人工来源案例可机器评分，结果按测试来源与任务类型分别报告，开发压测结果不外推为真实用户总体成功率。AI 生成压测默认用现有 runner 与工具以有界、可复现输入程序化运行，保留状态、trace 与原始 JSON 证据。人工压测可选，带实际反馈，用户不必执行，未开展时记“未开展”，本身不阻塞开发或发布。单元、集成、故障测试仍按工程验证管理，不强制归入这两类来源。294 项待人工审核是历史待补证据，默认不设为前置门，既有 notes、数据集和 reviews 原样保留。当前自由体验反馈只记“在文档覆盖范围内未交互出明显问题”，不写运行次数、覆盖率或人工通过标签。

## 轮换主线验收集（development-next）

`eval/development-next/rotating-mainline-v1/` 是 12 session 的紧凑轮换验收集：6 条回充主线（复用已有 grounded development 场景）+ 6 条广度题（每份知识文档一条）。比例、文档覆盖、去重、引用 hash/quote 和 manual-only oracle 由程序在模型调用前验证；live 只读复用 `robot_vacuum-v4-6aa0ba49c6`，隔离端口 8949、并发 2、一次运行。程序只做结构与证据绑定评分，回答语义是 agent inspection advisory，不是 Judge 通过或成功率。入口：`python scripts/run_rotating_mainline.py --validate-only` / `--run`；说明见 [docs/rotating-mainline-acceptance.md](../docs/rotating-mainline-acceptance.md)。

## 与旧 16 条 fixture 的关系

- `eval/cases/v0/recharge-start.json` 与 `tests/recharge_cases.py` 是 P1-03 起步期的
  历史回归材料，**保留但不复制、不改名、不冒充 v1 留出集**。
- `eval/run_recharge_dev.py` 继续作为旧 16 fixture 的回归工具，不参与 v1 的
  development/holdout 指标。
- v1 的 48 条案例（32 development / 16 holdout）由数据契约规定；`audit.json`
  保存旧 16 条审计与校验证据。审查产出全体 case 不等于调用产品。

## 命令

在任意 cwd 下：

```powershell
$py = "C:/Users/28980/anaconda3/envs/langchain/python.exe"

# 1) 数据契约 + 证据逐条校验 + freeze hash（含 holdout，只读不执行）
& $py eval/runner.py validate-data

# 1b) 从 case_definition 人工审核冻结 approved-development 选样（不跑模型）
& $py eval/runner.py select --review-status passed
& $py eval/runner.py validate-selection --manifest outputs/approved-development-selection.json

# 2) 确定性状态序列；默认 development
& $py eval/runner.py run --split development --mode state
& $py eval/runner.py run --split development --mode state `
    --selection-manifest outputs/approved-development-selection.json

# 3) 真实实例；会从本工程 source 新建 artifact，使用隔离端口，跑完关闭
#    QA 默认最多 3 个 worker；每 case 独立 session/workspace，完成即删 session
& $py eval/runner.py run --split development --mode live `
    --selection-manifest outputs/approved-development-selection.json `
    --bucket qa --workers 3 --port 8943 --case-timeout 300

# 4) 汇总最近一次（或指定目录）
& $py eval/runner.py summary
& $py eval/runner.py summary --run eval/results/<run_id>

# 5) 对已完成的 live run 做离线重评分：复用原 answer/trace/artifact，不调用模型、不 build
& $py eval/runner.py regrade --run eval/results/<completed-live-run>
```

`--review-status passed` 和 `--selection-manifest` 都是“先固定清单再跑”：

- 只认 `reviewScope=case_definition`、`caseHash` 仍匹配当前 case、`datasetCasesSha256`
  仍匹配当前 `cases.jsonl`、`suiteRevision` 为当前 v1.1 的审核记录；
- `passed` 进入选样；`failed` / `uncertain` 原样列出但排除，不删、不改判定；
- 旧 caseHash 或旧 dataset hash 的记录标 `historical`，不当 current；
- 选样是题目质量 filter，不是按 Agent 表现挑题；QA 与 fault 分母分开；
- manifest 记录 dataset file hashes、review aggregate hash、每 case 的 review
  id/revision/hash 和 `selection_sha256`；运行目录的 `run.json` 再次记录同一 selection。

`validate-data` 对 requirement 源（README 策略文本）的 hash 漂移做**有范围 waiver**：
只有 knowledge 源全部未变、原 quote 仍逐字/空白归一存在、case/review hash 未变、
当前 state/question schema 仍兼容时才放行；knowledge 漂移或 quote 丢失仍报错。
独立审计写到 `outputs/approved-development-source-drift.json`，不重写 dataset 里的旧 hash。
`--no-evidence` 只跳过 quote 提取，因此**不能**证明漂移 quote 仍存在，遇到 requirement
hash 漂移会 fail closed；approved baseline 必须用完整的 `validate-data`。

`--split holdout` 只是显式参数，不另造审批。当前正式流程要求：holdout 本轮只做
schema/证据完整性和 freeze hash，不运行、不据之调参。若未来为了调产品而看过
holdout 答案，必须把受影响 case 迁入 development 并补新的 holdout，不能继续把
旧 holdout 当未污染留出。

运行产物只写 `eval/results/<run_id>/`；不写
`assets/knowledge`、`assets/models`、`assets/rag_index`。

## 数据契约（摘要）

`suite.json`：

```json
{
  "schema_version": "robot-support-eval/v1",
  "suite_id": "robot-support-v1",
  "cases_file": "cases.jsonl",
  "split_file": "split.json",
  "counts": {"development": 32, "holdout": 16},
  "source_snapshot": {"...": "..."},
  "review_status": "pending_human"
}
```

`cases.jsonl` 一行一个 case：`id / family_id / category / split / origin /
review / turns / evidence / setup / scoring`。`turns[].expected` 包含
`required_claims / forbidden_claims / state / required_behaviors / expected_sources`。
`state` 只使用稳定公共语义键：`case_status`、`tried_actions`、`not_tried_actions`、
`model`、`case_count`、`scope` 的可选 subset；不要求唯一 `next_action`。
新增字段保留并容错，未知 `state` 键只标记为 unsupported，不伪造自动分。

`evidence` 的 `file` 必须在工程内，`sha256` 必须匹配文件，`quote` 须能在原文中
逐字（或仅空白归一后）找到。`expected_sources` 只做“本轮检索中存在 / 回答中出现”
的结构检查；**文件名出现不等于语义支持**。

`setup.kind = "fault_injection"` 的 case 使用
`{"fault": "timeout|restart|replay", "steps": []}`，单独进入 fault 桶，**不混入 QA 分母**。

## 两种模式

### `state`

- 每 case 新建 `empty_state()`，逐轮调用现有
  `robot_vacuum_support.apply_user_turn` / `apply_assistant_turn`。
- `assistant_fixture` 只在 state 模式回放，用于测试后续“还是不行”等确定性状态；
  它不是真实模型输出。
- 只对 `expected.state` 中给出的稳定键做 subset 比较；空 `tried_actions` 表示必须为空。
- 没有 state 期望的 case 标 `not_applicable`，不把“跑过”当通过。

### `live`

- 先验证 `source_snapshot` 里的 P1 `state.py` hash 与当前源一致；不一致直接
  `needs_configuration`，不跑旧 artifact。
- 从本工程 source 用 `build_project` 新建 artifact 到
  `../robot-vacuum-support-built/`，一次 run 只 build 一次，记录
  `artifact_id/version/runtime_hash/content_hash/spec_hash/prompt_hash/model` 和
  source/runtime/frontend/spec/bundle 的 tree fingerprint；旧产物不覆盖。
- 用实例 `serve.py` + 隔离端口（默认 8940，本轮实测用 8943）启动，隐藏窗口；
  每 case 独立 session 和 workspace，完成即 `DELETE` session；服务在 `finally`
  中停止，并记录端口是否释放。
- QA 并发受 `--workers` 限制（默认 1，本轮最多 3），不会无限 spawn；每个 case
  独立异常收集，不丢样本。restart/replay fault 共享服务进程，必须在 QA 批次后
  串行执行。
- 每完成一个 case 写 `cases.partial.jsonl` 和 `progress.json`；超时或中断时已完成
  记录仍保留。最终 `cases.jsonl`/`summary.json` 在 run 结束时写。
- 模型通过真实 RAG 工具取资料；`case.evidence` 只给评分器，不进入 prompt。
- `assistant_fixture` 是数据里的预设历史 assistant 轮，**不会**被当成真实模型输出
  注入 live。含 fixture 的 case 在 live 中以“模型自己生成前置回答”的 variant 运行，
  记录 `fixture_policy` / `live_variant`；fixture 自身 rubric 标 `not_applicable`，
  不把 fixture 当 live 回答。这样 24 条 passed QA 都能真实调用模型，同时不伪造
  前置回答。
- 没有可用实例 `environment_file` 时明确 `needs_configuration`，不 mock success；
  不读取/打印/复制密钥值，envfile 只由正常实例进程显式注入。真实模型传输失败
  最多重试 1 次并记录；case 级只对初始 transient error 重试一次，两次 attempt
  都保存在 `transient_retry.attempts`。
- `timeout` fault 当前没有真实注入接口，标 `unsupported`，不伪造通过；
  `restart` / `replay` 通过真实实例 API 执行，仍只算 fault 桶。
- 每轮记录 latency、`usage`（有则记录）、retrieval records/tool、support 状态、
  trace id、errors。usage 同时保留 `model_requests`（真实请求）和
  `ledger_events`（聚合行数），token 只按聚合行计一次；费用没有配置单价时为
  `null`，不猜。

## 评分分层

自动（结构化）：

- `answer.nonempty`
- `citation.present`（case 声明需要引用时）
- `retrieval.attempted` / `retrieval.tool_event`
- `citation.filename_mapping`：引用文件名是否在本轮真实 retrieval 中
- `citation.source_fragment`：引用文件名能否映射到本轮非空 retrieved snippet/content；
  仍**不是**语义支持
- `expected_sources.retrieved` / `expected_sources.cited`：存在性，不是语义支持
- `state.*`：稳定状态 subset
- `runtime.no_error`

人工/语义：

- `required_claims`、`forbidden_claims`、`required_behaviors`、source semantic support
  一律导出为 `pending_human`，不跑正则、不启用 LLM judge、不把模型自评当最终通过。
- 人工模板：`review-template.jsonl` / `review-template.csv`。审查者填写
  reviewer/verdict/reason 后另行汇总；当前没有任何人工复核结果。

case 级状态分开计数：`passed / failed / pending_human / error / skipped /
not_applicable / unsupported`。`pending_human` 不是 pass；fault 桶不计入 QA
成功率。`summary` 同时给出 attempted/completed coverage、p50/p95 的样本 `n`、
token 可用样本数，费用默认 `null`。

## Inspect AI

`eval/inspect_tasks.py` 是薄适配：一个 v1 case 一个 Inspect `Sample`，自定义
deterministic solver 复用 `run_state_case`，自定义 scorer 输出结构性 `Score`；
语义 rubric 只放 metadata。真实框架的小例验证见 `tests/test_eval_v1.py`。
live 需要 source rebuild + 实例 + 端口 + 人工导出，仍由 `runner.py` 执行，
不把 Inspect mock 当真实产品结果。

## 人工审核 GUI

本地入口 `http://127.0.0.1:8950`，启动、数据位置、导出和安全边界见
[`docs/manual-review.md`](../docs/manual-review.md)。服务只读本目录的 v1 数据集和
`eval/results`，人工记录独立写入 `eval/reviews/`；案例定义与运行回答分开保存，
留出集默认隐藏并记录曝光，不运行 holdout，也不把人工状态写回 run 或自动评分。

## 当前边界

- 48 条是人工构造初期回归集，不是真实用户数据，不保证统计显著。
- 引用文件存在 ≠ 引用支持结论；语义支持仍待人工。
- 没有人工复核、真实试用或全量成功率；不得从测试数/通过数外推。
- fire-and-forget 的单次真实运行只是观察，不构成 baseline 或成功率声明。
