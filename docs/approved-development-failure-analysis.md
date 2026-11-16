# Approved development 8 条自动失败归因（24 QA + 2 fault）

- 日期：2026-11-14（香港）
- QA run：`v1-live-development-20261113-224057-3f8412`
- fault run：`v1-live-development-20261113-224522-4ceb45`
- 结构化证据：[approved-development-failure-analysis.json](standalone.md#未包含)
- 抽取脚本：[analyze_approved_failures.py](../scripts/analyze_approved_failures.py)
- 人工回答审核材料：[answer-review-round-1.md](answer-review-round-1.md)
- 基线：[approved-development-baseline.json](standalone.md#未包含)、[selection](standalone.md#未包含)、[freeze](standalone.md#未包含)

本轮由执行者只读核对；主模型发现 8950 拒绝连接后，用 `exec_command` 前台管道托管 session 95057 恢复 python server（PID 183548），无可见窗口、无 WMI；执行者检查时服务已恢复。未调用模型、未跑 holdout/全测试、未改 runtime/GUI/dataset/旧 run/artifact/assets/`eval/reviews`，未读取密钥值。下文“确认/排除/未知”只针对当前证据，不把自动检查当成语义正确率，也不把 fault 混入 QA 分母。

## 1. 先纠正计数

当前只读证据一致：

- `outputs/approved-development-selection.json`：32 条 development 已做 `case_definition` 审核，**26 passed / 2 failed / 4 uncertain**；included 26（24 QA + 2 fault），excluded 6（2 failed + 4 uncertain）。
- `GET http://127.0.0.1:8950/api/meta`：`reviewCount=32`，`byStatus={passed:26, failed:2, uncertain:4}`，`byScope={case_definition:32, run_answer:0}`；health 为 `ok`、`readOnlyDataset=true`、`canRunHoldout=false`。
- 24 QA run：8 failed / 16 pending_human；语义项 **294 pending_human + 8 not_applicable = 302 导出项**。294 是待人工回答审核的分母，不是 302 条都要逐条点。

因此后续记录写 **26 passed / 2 failed / 4 uncertain**。此前计划里的 “27 passed / 2 failed / 4 uncertain” 是总数笔误；历史用户审核记录和旧分析原文不改，只在后续记录纠正。注意：计划中“27 项评测测试”是另一个事实（测试数），不是这个审核计数，不能一起改。

## 2. 证据口径与路径可比性

24 QA 中：

- **16 条 `frozen_turns`**：只回放 dataset 的 user turns，路径与冻结题面可比。
- **8 条 `model_generated_prior_turn`**：dataset 里的 assistant fixture 保留但未回放，live 模型生成了不同的上一轮建议；预期状态可能描述了一条 live 从未发生的对话。**不可直接当作 frozen 题回归**。8 条是：`correction-mop-water-level-01` + 6 条 multi-recharge failed + `negation-filter-not-cleaned-01`。
- 8 条 QA 自动失败中，2 条 frozen（`ambiguity-recharge-condition-01`、`direct-base-power-01`），6 条 variant（closure-02、correction-01/02、negation-01/02、tried-advance-02）。

每个 case 的逐轮用户原文、真实回答、support state、retrieval、trace 和文件位置都写在 JSON 的 `qa_attributions[].evidence` / `qa_answer_review.rows`。trace 目录统一为：

```text
eval/results/v1-live-development-20261113-224057-3f8412/instance-storage/sessions/<session_id>/transcript.jsonl
eval/results/v1-live-development-20261113-224057-3f8412/instance-storage/sessions/<session_id>/outputs/traces/trace_20261113.jsonl
eval/results/v1-live-development-20261113-224057-3f8412/instance-storage/sessions/<session_id>/support_state.json
```

## 3. 8 条 QA 自动失败逐项归因

| case | 路径 | 自动失败检查 | 归因 |
|---|---|---|---|
| `ambiguity-recharge-condition-01` | frozen | citation.present、expected_sources.cited、state.case_status、state.scope | **确认产品缺陷**：recharge 状态未激活 + RAG 后未引用 |
| `direct-base-power-01` | frozen | citation、retrieval、expected_sources | **确认产品缺陷**：首轮 `没反应` 触发确定性澄清，绕过 agent/RAG，回答是陈旧“还是不行” |
| `multi-recharge-closure-02` | variant | retrieval/citation/source、state.tried_actions | 路径差异 + **独立缺陷**：忽略“手动放回能充”的部分成功；引用文件均可追溯到 T1 检索，当前评分器只支持本轮 provenance |
| `multi-recharge-correction-01` | variant | state.tried_actions | **预期 outcome 语义歧义**，不是已确认产品失败：自动/状态项符合更正，仅 expected success vs actual unknown |
| `multi-recharge-correction-02` | variant | citation.present、expected_sources.cited | **确认产品缺陷（另有独立 state 缺陷）**：换座被映射成 reseat_dock；更正后 replace_dock 未进 not_tried；T1 检索成功但无引用 |
| `multi-recharge-negation-01` | variant | retrieval/citation/source、state.not_tried_actions | 路径差异 + **独立缺陷**：`都没试过` 未绑定到 pending suggestions；引用文件均可追溯到 T1 检索，非无来源 |
| `multi-recharge-negation-02` | variant | citation/retrieval on T1 | **自动检查越界**：T1 只是 43 字二选一澄清，无事实 claim；T2 自动/状态项无失败，语义仍待人审 |
| `multi-recharge-tried-advance-02` | variant | state.tried_actions | **预期 outcome 语义歧义**：三动作都在，仅 clear 的 success vs unknown 冲突；自动/文本项符合，语义未人审 |

### 3.1 `rsv1-dev-ambiguity-recharge-condition-01`（frozen，确认两个独立缺陷）

- dataset：`eval/datasets/v1/cases.jsonl:5`；单轮用户原文“它回充不行，帮我看看。”；expected state `active/recharge`，expected sources 含 `故障排除.txt`、`扫地机器人100问.pdf`。
- turn 1 实际：2 次 model call；2 个检索 query；`retrieval.attempts=2`；实际 sources 有 `故障排除.txt`、`维护保养.txt`、`扫地机器人100问2.txt`。trace `run-5555cc3086cd4753928dedd228b0e51c` 有 4 个 `tool_start`（call/spec 两套表示，2 个 query），不是没有 RAG。
- `citation.present`：expected `[知识库文件名]`，actual `[]`；`expected_sources.cited` 0/2。回答 656 字，实质使用了检索内容，却没有一个知识库文件名。
- `state.case_status`：expected `active`，actual `null`；`state.scope`：expected `recharge`，actual `null`。support: `last_turn_scope=recharge`、`last_decision.scope=recharge`，但 `active_case_id=null`、`cases=[]`。
- **确认**：RAG 已发生但引用缺失；recharge scope 已识别但 case 未激活。
- **排除**：不是 RAG bypass，不是 model error（usage reported、runtime.no_error pass），也不是题面路径差异。
- **代码定位边界**：本轮未把 case 未激活定位到具体行号；state.py 与 run artifact 哈希一致（`3da14594...`），但这里只作为 state 层证据，不作行号缺陷结论。
- **修复层**：state 激活 + validation 引用门禁；prompt 只保留“用了来源必须写出处”的稳定职责。

### 3.2 `rsv1-dev-direct-base-power-01`（frozen，确认执行层绕过）

- dataset：`eval/datasets/v1/cases.jsonl:8`；用户首轮“充电座指示灯不亮，机器人放上去没反应，怎么办？”；expected `active/recharge`、sources 含 `故障排除.txt` 等。
- 实际回答只有 60 字：**“你说的‘还是不行’是指哪一步之后？请告诉我你刚才实际试了哪个操作（例如清洁充电触点、检查充电座电源），我才能判断下一步。”**
- usage 只有 **1 次 model request，且是 interpretation 层（930 tokens）**；0 agent call、0 retrieval、`trace_ids=[]`；latency 1702 ms。support `last_decision.route=semantic`、`scope=recharge`，case active。
- 代码定位（边界）：`runtime/robot_vacuum_support/state.py` 与 run artifact 的 state.py 哈希一致（`3da14594...`），`_AMBIGUOUS_FOLLOWUP` 的 `没反应` 在该版本同样存在（当前源 `state.py:169-172`，分支约 2165/2170）；`runtime/modules/instances/runtime/server.py:590-624` 是**当前源**的佐证，但 run artifact 的 server.py 哈希不同（artifact `0a60d344...` vs 当前 `ca60ab0b...`），artifact 中对应 `direct = applied.get("direct_answer")` 在 **line 556**，未逐行 diff 验证同一行为。
- **确认**：首轮 `没反应` 走了确定性澄清，绕过主 agent/RAG，回答答非所问；citation/retrieval/source 失败是这一绕过的后果。
- **排除**：不是模型答错、不是无依据编造、不是路径差异。
- **修复层**：state/execution 先修：首轮 report 不应命中 follow-up direct answer；只有已有 case + 先前建议/后续轮才允许澄清捷径。prompt 不是首选层。

### 3.3 `rsv1-dev-multi-recharge-closure-02`（variant，路径差异 + 独立缺陷）

- dataset `cases.jsonl:4`；fixture 上一轮是 `assistant_fixture: 先清障、擦传感器。`，**没有回放**；live T1 自己生成了“清理充电座周围障碍物”的建议。
- T2 用户“手动放回能充电，但自动回充还是失败。”；实际 `retrieval.attempts=0`，回答引用 `扫地机器人100问2.txt`、`故障排除.txt`、`选购指南.txt`。**三个文件都出现在 T1 的检索来源中**（T1 sources 含这四个文件）；`citation.filename_mapping` / `citation.source_fragment` 全部 unmapped，是因为当前评分器只支持本轮 provenance，不等于这些文件从无来源。
- state 只记录 `clear_dock_obstacles=failed`，没有记录“手动放回能充电”这个部分成功；expected `reseat_dock=success`。回答转向“清洁充电触点”，没有利用“手动能充、自动不能”的定位价值。
- **确认**：忽略用户给出的部分成功证据，这是独立于 fixture 的产品/状态缺陷。引用文件属于**已知可追溯的历史复用**；当前评分器不能证明语义支持，但也不能据此判定“无任何来源支持”。
- **路径差异**：expected `reseat_dock` 与 live 先前建议的 `clear_dock_obstacles` 不同；不能把整个失败说成 frozen 回归。
- **未知**：expected `reseat_dock` 是否就是“手动放回”的规范 action id，题面没有定义；历史复用片段的语义支持度未人审。
- **修复层**：state/context 记录 manual-vs-auto；tools/validation 允许有记录的历史来源复用并扩展 provenance 映射，不能要求每轮强制检索，也不能对 0 检索引用一刀切禁止。

### 3.4 `rsv1-dev-multi-recharge-correction-01`（variant，预期 outcome 歧义）

- dataset `cases.jsonl:19`；T1 用户“我已经擦过充电触点了”，state `clean_contacts=unknown`；T2 更正“触点没擦，只清了充电座周围的障碍物”，actual state `clear_dock_obstacles=unknown`、`not_tried=[clean_contacts]`，回答明确接受更正并说明只做了清障。
- 唯一自动失败：`state.tried_actions` expected `clear_dock_obstacles=success`，actual `unknown`。两轮 RAG 都成功，回答包含 `维护保养.txt` / `故障排除.txt` 等引用。
- **确认**：更正后的动作集合、not-tried 与自动检查项符合；回答文本接受更正，但语义质量仍未人工审核，不能写“回答正确”。这不是状态错乱。
- **排除**：不是检索/引用缺陷，不是漏掉更正。
- **未知/待定义**：用户只说“只清了……”，同时仍说“回充失败”；`outcome=success` 到底表示“动作做了”还是“问题解决了”，题面没有契约。
- **修复层**：validation 先定义 outcome 词汇（executed / failed / unknown / resolved），不要用改 expected 掩盖。

### 3.5 `rsv1-dev-multi-recharge-correction-02`（variant，确认 state + citation）

- dataset `cases.jsonl:10`；T1 用户“我已经换了一个新的充电座。” expected `replace_dock=unknown`；actual `reseat_dock=unknown`，回答写“你已做过「重新对准充电座」”。这是 action taxonomy 混淆。
- T1 实际有 2 个检索 query、sources `故障排除.txt`/`选购指南.txt`，但 `citation.present` fail、`expected_sources.cited` 0/1。
- T2 更正“没换新座，只是把旧座挪了个位置。” expected `not_tried=[replace_dock]`；actual `not_tried=[]`，且 support 仍把 T1 的“换了一个新的充电座”作为 `reseat_dock` 的 evidence。`state.not_tried_actions` 是 `unsupported`，不是 fail。
- **确认**：换座/挪座动作混淆；更正后旧证据未撤销、被替换的动作未进入 not-tried；T1 检索成功但未引用。
- **路径差异**：fixture 上一轮“既然换过座，检查下传感器和供电”未回放；但上述映射和更正缺陷可由用户原文独立证实。
- **修复层**：state/interpretation 区分 replace_dock/reseat_dock；更正时 supersede 旧 evidence 并把被撤销动作放入 not_tried；validation 对实质回答强制引用。

### 3.6 `rsv1-dev-multi-recharge-negation-01`（variant，路径差异 + 独立缺陷）

- dataset `cases.jsonl:11`；fixture 上一轮“你清理过充电座周围、擦过充电触点吗？”未回放；live T1 建议清理充电座周围障碍物，并追问现象。
- T2 用户“都没试过，别让我拆机。” expected `not_tried=[clear_dock_obstacles, clean_contacts]`，actual `not_tried=[]`；回答重复建议清障。
- T2 `retrieval.attempts=0`，引用 `故障排除.txt`、`扫地机器人100问2.txt`；两个文件都出现在 T1 检索来源中。当前评分器只做本轮 mapping，所以 citation 项 fail，但这是**已知可追溯的历史复用**，不是“无任何来源支持”。
- **确认**：至少 `clear_dock_obstacles` 是 live T1 明确建议过的 pending action，用户说“都没试过”后仍未记为 not-tried，这是独立 state 缺陷；引用项属于 provenance/评分边界，不能单独升级为“无依据”产品缺陷。
- **路径差异**：`clean_contacts` 只由 fixture 提问带出，live T1 没有明确问触点，所以这一项不能完全归到产品。
- **修复层**：state 把泛化否定绑定到 pending suggestions；validation 扩展历史 provenance 映射；不要求每轮强制检索，也不一刀切禁止 0 检索引用。

### 3.7 `rsv1-dev-multi-recharge-negation-02`（variant，自动检查越界）

- dataset `cases.jsonl:32`；T1 回答只有 43 字：“先确认一下：是机器人回不到充电座（例如在座前打转、对不上），还是您找不到充电座的位置？”这是纯二选一澄清，没有事实 claim。
- T1 没有 retrieval，也没有引用；`citation.present`、`retrieval.attempted`、`expected_sources.*` 全部 fail。T2 有成功 RAG，state `clean_contacts=unknown`、`not_tried=[clear_dock_obstacles]` 与 expected 一致。
- **确认**：case 的自动 fail 完全来自 T1 的 citation/retrieval 规则；T2 的自动/状态项无失败，语义仍待人审。
- **排除**：不能据此说产品“不检索/不引用”；该轮不应被要求引用。
- **未知**：dataset 的 `expected_sources` 是否要求纯澄清轮也检索；行为 rubric 没有这个要求。
- **修复层**：validation 将纯澄清轮标为 citation/retrieval `not_applicable`，实质 claim 轮保持严格。

### 3.8 `rsv1-dev-multi-recharge-tried-advance-02`（variant，预期 outcome 歧义）

- dataset `cases.jsonl:16`；T1 用户“充电座周围已经清了”，expected `clear_dock_obstacles=success`，actual `unknown`；T2 用户“触点擦了，传感器也擦了，还是不行”，actual 三个动作齐全：clear=unknown、clean_contacts=failed、clean_sensors=failed；expected clear=success。
- 两轮 RAG 都成功，回答有引用；T3 回答文本不再重复三步，转向问充电座指示灯/手动放回，与 required behavior 的文本项相符；语义质量未人审。
- **确认**：动作没有丢失，失败只集中在 clear 的 outcome 值。
- **排除**：不是漏动作、不是引用缺失、不是答非所问。
- **未知**：`已经清了` 是否足以把 outcome 定为 success，而用户同时仍报告失败；这与 correction-01 是同一契约缺口。
- **修复层**：validation 定义 executed-vs-resolved；state 不要用单一 `success` 同时表示“做了”和“解决了”。

## 4. 两个 frozen fault 独立诊断

fault run 分母独立，不混入 24 QA。`fault.restart_session_recovered` 为 **pass 2/2**；`server.log` 显示进程重启后仍 `GET /api/sessions/<同一 session>`，T2 能回忆 T1 的两个动作。**重启后同会话状态可读，这一点成立**。

fault 文件定位：`eval/results/v1-live-development-20261113-224522-4ceb45/instance-storage/sessions/s-6996bb7e8c8c43f4/transcript.jsonl`（restart-01，trace 在同目录 `outputs/traces/trace_20261113.jsonl`）；`.../sessions/s-11e206b0ab4843f4/transcript.jsonl`（restart-02）。fault 的 `fault` 对象和 `recovered_support` 在 `eval/results/v1-live-development-20261113-224522-4ceb45/cases.jsonl` 对应行。

### 4.1 `rsv1-dev-fault-process-restart-01`（frozen fault）

- dataset `cases.jsonl:26`；T1“回充失败，我已经清理了充电座周围，也擦了充电触点。” expected `clear_dock_obstacles=success`、`clean_contacts=unknown`，actual 两者都 `unknown`。
- T2“服务重启后，我刚才试过什么？”回答文本列出两项及“结果未说明”（文本/状态对照，语义未人审）；T2/T3 无 model/tool trace，citation 失败，来源不可映射。
- T3“我刚才试过什么？”expected state 是空 tried（题面 step 4 的**新会话 B**），actual 仍是同一 session A，保留两项 tried，因此 `state.tried_actions` fail。
- `server.log` 只有 **每个 fault case 一次 `POST /api/sessions`**；没有创建 session B 的请求。T3 的 expected 空状态无法由产品失败解释。
- **确认**：同会话重启恢复成功；T1 的 success/unknown 是 outcome 语义问题；T3 的隔离断言没有被执行。
- **排除**：不是重启恢复失败；不是产品 session 串话（没有 B 的证据）。
- **未知**：runner 为什么跳过 setup 里写的新会话步骤；这是 harness/execution 缺口。
- **修复层**：validation/execution 要么实现并记录 session B，要么删掉空状态断言；restart 恢复与任务状态质量分开。

### 4.2 `rsv1-dev-fault-process-restart-02`（frozen fault，确认 state 缺陷）

- dataset `cases.jsonl:28`；T1“回充失败，充电座灯不亮，我已经换过插座。” expected `check_power=failed`，actual `unknown`。
- T2“重启后还在吗？我刚才查到哪了？”support 新增 `reset_robot=unknown`，evidence 正是“重启后还在吗？我刚才查到哪了？”——用户说的是**服务重启**，不是重启机器人。这是确认的 state/interpretation 误抽。
- T3“我试过什么？”`state.scope` expected `recharge`、actual `None`（recall 路径丢 scope）；tried 仍保留 check_power/reset_robot。
- 同样没有第二个 `POST /api/sessions`；T3 仍是 session A，隔离断言未执行。
- **确认**：服务重启被误当机器人重置；recall 轮丢 scope。重启恢复本身仍通过。
- **排除**：不是 restart recovery 失败；不是 session 隔离失败（B 未执行）。
- **未知**：`check_power` 应为 failed 还是 unknown，仍受 outcome 契约影响。
- **修复层**：state/context 不把 meta/service restart 问题抽成用户动作；recall 保留 scope；validation 执行 session B 后再评隔离。

## 5. 共性机制与正确修复层

| 机制 | 证据 | 正确层 | 不应做什么 |
|---|---|---|---|
| 首轮状态未激活 / 首轮症状被当 follow-up | ambiguity-01 active null；direct-base `没反应` 触发 direct answer | state + execution | 不写 case 专属 prompt，不把首轮症状当“还是不行” |
| 更正/否定生命周期不完整 | correction-02 stale evidence + replace 未进 not_tried；negation-01 `都没试过` 未绑定 | state/context | 不用正则把“没试过”硬编码成某题答案 |
| 引用 provenance 边界 | closure T3/negation T2 0 本轮检索，但引用文件都在 T1 sources；当前评分器只支持本轮 mapping | validation + tools | 不要求每轮强制检索，也不把“0 本轮检索”直接判成无依据 |
| 引用存在性 ≠ 引用支持 | ambiguity-01 RAG 成功但 0 引用；closure/negation 历史来源未映射 | validation | 不把“有文件名”当语义支持，也不改 expected 洗白 |
| outcome 语义混用 | correction-01/tried-advance/fault-01 的 success vs unknown | validation + state | 不直接把 expected 改掉；先定义 executed/resolved/failed |
| fixture 路径差异 | 8 条 variant 的 assistant_fixture 未回放 | data/context + validation | 不把 variant fail 当 frozen 回归，也不同时放过独立缺陷 |
| fault 断言未执行 | 两个 fault T3 都是 session A，无 B 创建 | execution/validation | 不把“未执行”写成“隔离通过/失败” |

引用四类判定：closure-02/negation-01 属**已知可追溯历史复用**（文件在 T1 sources）；当前评分器**只支持本轮 provenance**；没有证据判为**确实无任何来源支持**；历史片段的**语义支持度仍未知**，需人审。不能据此要求每轮强制检索，也不能一刀切禁止 0 本轮检索引用。

建议修复顺序（不是本轮实施）：

1. **先补 oracle/契约**：action outcome、纯澄清轮的 citation/retrieval applicability、fault session B 断言。没有契约不改代码。
2. **state/execution**：首轮激活、direct-answer gate、否定/更正生命周期、service-restart 误抽、recall scope。
3. **tools/validation**：扩展 history provenance 映射；只有既无本轮也无历史来源时才判无依据；citation 只对 claim 检查并与本轮/显式历史片段映射，不强制每轮检索。
4. **context/prompt**：结构化传递 case 状态、已试/未试、manual-vs-auto、问题生命周期；prompt 只保留稳定职责。
5. **data**：若 fixture 必须逐字回放，补可 seed 的历史 API 或明确 variant 标记；不改历史 expected 掩盖错误。

## 6. 现有审核记录与 notes（公开版已隐去原文）

> **公开仓库版说明：** `eval/reviews/` 不发布；7 条个人审核 notes 的逐字原文已替换为 `[个人审核原文已从公开仓库移除]`。原始记录和判定保留在本地，不随仓库发布。

- `eval/reviews` 当前 **只有 32 条 `case_definition`**，**没有 `run_answer` 记录**；`/api/meta` 的 `byScope.run_answer=0` 与之一致。本轮没有代写任何人工 review。
- 7 条 notes 原文在此公开副本中已移除（其中 6 条对应 excluded 的 2 failed + 4 uncertain，1 条是 fault-01）：

| case | note 原文 |
|---|---|
| `multi-recharge-tried-advance-01` | `[个人审核原文已从公开仓库移除]` |
| `correction-mop-water-level-02` | `[个人审核原文已从公开仓库移除]` |
| `multi-topic-switch-mop-02` | `[个人审核原文已从公开仓库移除]` |
| `direct-recharge-triage-01` | `[个人审核原文已从公开仓库移除]` |
| `negation-filter-not-cleaned-02` | `[个人审核原文已从公开仓库移除]` |
| `multi-recharge-closure-01` | `[个人审核原文已从公开仓库移除]` |
| `fault-process-restart-01` | `[个人审核原文已从公开仓库移除]` |

对 fault-01 note 的回应：**部分同意**。重启恢复值得保留，但应程序化、可 seed、带 session A/B 与副作用断言；当前 runner 恰好漏掉了 B，说明“测试味”背后是执行路径未被验证，而不是要把该 fault 删掉。其余 6 条是题目质量 note，保留原记录；它们对应 6 条 excluded，不进入 24 QA 分母。

## 7. GUI 与边界

- 执行者只读 `GET /api/health` / `GET /api/meta`：8950 `ok`，pid 183548，reviewCount 32。**主模型此前发现 8950 拒绝连接，用 `exec_command` 前台管道托管 session 95057 恢复 python server；执行者未启动/重启/停止服务，且无 WMI、无可见窗口。**
- `docs/manual-review.md` 里“WMI + pythonw 隐藏启动”是历史启动方式记录，**不代表本轮使用了 WMI**；也不是执行者启动了 GUI，执行者只做 health/scope 只读检查。
- 旧 run、artifact、dataset、`eval/reviews`、assets 未改；没有模型调用、holdout、全测试、时延优化。
- P1-06 仍是 `in_progress`：已有 24 QA live 原始运行，不等于整体成功率或人工语义审核完成。

## 8. 下次入口

1. 用户用 [answer-review-round-1.md](answer-review-round-1.md) 的 GUI 入口逐 case 做 run-answer 审核；16 条 pending + 8 条 auto failed 都要看真实回答，294 项按 case 整体判定即可。
2. 先裁决 outcome 契约和“纯澄清轮是否要求引用”两个 oracle 问题；未裁决前不改 expected、不改 dataset。
3. 再按上表顺序修 state/execution，随后 tools/validation；fixture 是否要可 seed 回放另做设计决定。
4. 修完只用同族新例 + 反例验收；不拿 holdout 调参。下一次 live 重跑前，保持本报告的 run ID 和只读边界。
