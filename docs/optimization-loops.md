# 两个优化回路：Prompt Engineering 与 Context Engineering

> 日期：2026-11-15（香港）  
> 范围：对已有源码、提示词版本、真实运行和报告做**证据归因**；只写文档，不改 runtime / prompt / spec / RAG / model / index，不构建 artifact，不跑新模型。  
> 结论先行：本仓库存在 **Prompt 架构设计与版本迭代回路**，也存在 **Context 修复的 old→new 证据链**；但没有“同模型、同上下文、prompt-only 的对照实验”，不能声称 prompt 带来了因果语义提升。  
> 两个 loop 之外不另立第三个 loop。PDF 8→101 是 RAG ingestion/coverage 修复，单独引用，不计入本文两个 loop；`reseat_dock` 标签修正属于 state/display 邻接修复，也不冒充 prompt engineering。

## 0. 证据状态摘要

| 回路 | 状态 | 能证明 | 不能证明 |
|---|---|---|---|
| Loop 1 Prompt Engineering | `existing_architecture_loop_no_causal_experiment` | v1→v2→v3→v4 的 prompt 确实发生稳定职责扩展；v4 prompt hash 与冻结基线/最终 spec 可核对；final12 有引用与边界行为的同期记录 | 不能证明这些行为是 prompt 单独造成的；没有 prompt-only A/B；没有语义提升数字 |
| Loop 2 Context Engineering | `confirmed_old_new_fix_with_negative_cases` | 旧 6aa0 把语义 `failed` 写成 `unknown`；6b90 与最终 24f3 改为 `failed`；第三方/更正/明确恢复反例仍守住 | 不是 holdout、不是成功率、不是权威 Judge；不证明所有措辞都泛化 |

**关键口径：** 两个 loop 都是工程设计与证据归因，不是“模型能力提升 x%”。测试通过、结构验收和固定样本都只证明对应范围。

---

## 1. Loop 1：Prompt Engineering —— 从单段流程说明到稳定职责契约

### 1.1 症状：职责混在一段 prompt 里，证据/安全/动态状态边界不够显式

可核对的事实不是“模型答错了一句”，而是 prompt 版本本身的演进：

| 版本 | 字符/行数 | SHA-256（UTF-8 system_prompt） | 新增的稳定职责 |
|---|---:|---|---|
| [v1.json](../robot_vacuum/v1.json) | 814 / 15 | `f8eb016c59ef9e9d3a07f18eeb88e67841553502c193aae0f9cae0f806a00dc6` | 身份、先检索、只用资料、中文、来源文件名、资料不足追问 |
| [v2.json](../robot_vacuum/v2.json) | 1523 / 29 | `664d27d3dbbb9f86dff48e6b2fcbcde05f0edc58fef6f07fad8ede4ccc36ff8c` | 服务端售后状态消费、证据与缺依据兜底 |
| [v3.json](../robot_vacuum/v3.json) | 1601 / 30 | `cbebcddbbd8a4e8e7a8d84ee4c2c3f52860f37600256f6a9525a5e39f917304e` | 增加安全维修边界（不主动建议恢复出厂/拆机/打磨触点） |
| [v4.json](../robot_vacuum/v4.json) | 2316 / 41 | `4e471a30ceabddb5b0e5a6b9c028c627de1c6d9bcaa3b9e135598c770784087b` | 工作语境/typed context 的稳定消费规则、渐进诊断与更明确的证据边界 |

旧版 v1 已经写了“只根据工具返回的资料回答”“每个关键事实后用方括号标注来源文件名”，但有两个可指出的空缺：

- 没有明确写“引用只能使用本轮工具实际返回的文件名，且必须能在本轮检索结果中找到”。
- 没有把“检索片段是候选证据，不是已验证安全结论”“有引用不等于引用正确”写成稳定职责。
- 没有安全维修边界；在售后场景里，这会把“外部清洁/内部拆机/恢复出厂”留给模型临场判断。

这些是**prompt 层可观察的空缺**。注意：它们不是所有失败的根因；已确认的引用缺失/状态错误在别的层也有修复（见 1.5 与 Loop 2）。

### 1.2 假设：动态事实不应写进 prompt，prompt 只保留稳定职责

假设不是“再补几条万能规则”，而是：

1. 身份、范围、先检索、来源、资料不足、安全边界是稳定职责，可以留在 prompt。
2. 型号、已试步骤、旧任务、当前问题、歧义候选是动态事实，必须由服务端 state/context 提供；prompt 只写“怎么消费”，不写具体用户事实。
3. 不按单个 case 添加措辞黑名单；引用规则只表达“来源必须来自本轮真实工具结果”，具体来源由工具返回。
4. 安全边界表达为稳定职责，而不是逐条故障话术。

这与仓库 AGENTS 的“prompt 只表达稳定职责；动态事实进 context/state”一致。

### 1.3 干预：v2–v4 增加稳定契约，而不是写 case 补丁

实际改动在版本化 `system_prompt`，不是另建一套隐式注入：

- v2 增加“服务端售后状态（回充失败单流程）”：明确助手建议不是用户已做；用户明确没试的不能标成已试；只问推进下一步必要的一个问题。
- v2 增加“检索证据与缺依据兜底”：引用只能使用本轮工具实际返回的文件名；空结果/超时/工具报错要说明，不伪装成正常回答。
- v3 增加安全边界：不主动建议恢复出厂、清空地图、拆机或打磨充电触点；恢复出厂不等同于重启。
- v4 增加工作语境消费规则：unknown 不补默认值；歧义候选只做一次低成本澄清；建议/执行/结果三件事分开；结案只认明确可验证成功。

可直接核对的 v4 原句：

> “引用只能使用本轮工具实际返回的文件名，不得虚构来源。回答中的方括号来源必须能在本轮检索结果中找到。”

> “检索片段只是候选证据，不是‘已验证安全’的结论；不要用相似度分数或‘有引用’外推为引用完全正确。”

> “安全边界：不要主动建议恢复出厂设置、清空地图、拆机或打磨充电触点；恢复出厂不等同于重启。清障、清触点、清传感器是不同步骤，必须分别确认是否由用户执行。”

> “用户消息前可能有【工作语境】结构块。它来自用户消息的服务端抽取……它是本轮事实边界：unknown 不要补默认值；不要把助手之前的建议当成用户已做。”

**没有做的事：** 没有把案例原话、具体型号、已试步骤或“遇到某词就答某句”写进 system prompt；没有用 prompt 替代 state/tool/validation 的确定性修复。  
**架构说明：** 仓库有通用 `PromptBook`/`assemble` 块机制（[blocks.py](../runtime/core/prompts/blocks.py)、[assemble.py](../runtime/core/prompts/assemble.py)），但 `robot_vacuum` 这个 spec 当前仍把 prompt 放在单个 `system_prompt` 字段里；不能把它说成“已经在线块化”。

### 1.4 验证：版本可核对、行为有同期记录，但不是因果 A/B

**静态验证：**

- v1→v4 的字符/行数/SHA-256 如上表；v4 的 `4e471a30...` 与冻结基线报告的 `prompt SHA-256` 一致：[approved-development-baseline.md](approved-development-baseline.md) 第 4 节。
- 最终 spec 的 hash `4fc4d815...` 与最终 artifact manifest checksums 中的 `robot_vacuum/v4.json` 一致：[build-robot_vacuum-v4-24f3a3db39.json](standalone.md#未包含)、[release-v1 report](standalone.md#未包含)。
- v4 的 prompt 明确包含“本轮实际返回”“候选证据”“安全边界”“工作语境消费”四类稳定职责；这是源码事实，不是推断。

**运行/报告的同期记录：**

- final12 的 PDF 回答实际带 `扫地机器人100问.pdf` 来源，并在同族追问中明确“资料没有覆盖点阵解算细节，不做推测性补充”：[PDF transcript](standalone.md#未包含)。
- final12 的“手动能充但自动回充失败”回答明确拒绝“已经解决/不用管”：[transcript](standalone.md#未包含)。
- final12 结构验收 12/12 cases 完成，0 runtime error / 0 not_run，但其中 1 条原始 `retrieval_attempted=false` 保留；结构完成不等于语义通过：[acceptance12 report](standalone.md#未包含)、[release-v1.md](release-v1.md)。
- 历史定向测试 `139 passed`、label 修正 `14 passed`、PDF 相关 `13 passed` 只作工程回归证据，不作成功率：[product-closed-loop.md](product-closed-loop.md)、[pdf-retrieval-fix.md](pdf-retrieval-fix.md)。

**为什么这不是因果证明：**

- 没有同一旧 prompt / 新 prompt、同模型、同上下文、预注册 case 的 paired A/B。
- 许多已确认问题实际修在 state/execution、tool、validation、RAG ingestion；final12 的改善不能归给 prompt。
- 12 案是固定 development 样本，不是 holdout；语义未由权威 Judge 重评。
- 因此这里只能证明“prompt 架构和契约发生了可核对的变化”，不能证明“prompt 让语义正确率提升”。

### 1.5 限制

- **没有 prompt-only 实验数字。** 不能写“提升 x%”“引用准确率提升”“回答更准确”。
- **不能把 state 修复算进 prompt loop。** `semantic failed → unknown` 的修复在 state/execution（见 Loop 2），不是 prompt 改好了。
- **不能把 PDF 修复算进 prompt loop。** PDF 8→101 是编号切片 + 私有索引覆盖，不是 prompt。
- **不能把 final12 结构完成写成语义正确。** 12/12 cases 完成，但 1 条原始 `retrieval_attempted=false` 保留，逐 claim 蕴含未证明。
- **prompt 仍是单字段。** 没有把 `PromptBook` 说成这个场景的在线事实。
- **没有真实用户或 holdout。** 不写满意率、生产 SLA、真实售后授权。

### 1.6 90 秒口述版

> 这个项目的 prompt 优化，不是“发现一句错话就补一条规则”。我先看实际版本：v1 只有 814 字、15 行，能说“先检索、只用资料、写来源”，但没有明确“来源必须是这一轮工具真正返回的”“引用不等于证据正确”，也没有安全维修边界。  
> 我的假设是：身份、范围、引用、资料不足、安全这些稳定职责可以留在 prompt；型号、已试步骤、当前问题、歧义这些动态事实必须进 state/context。  
> 于是 v2 加了售后状态消费和证据兜底，v3 加了安全边界，v4 加了工作语境的消费规则；到 2316 字、41 行。v4 的 hash 与冻结基线、最终 spec 都能对上。  
> 但我要说清楚：这不是 prompt-only 的 A/B。final12 有真实回答和结构验收，可是状态、工具、校验、PDF 切片也都在改。所以我只能证明“prompt 架构按稳定职责演进了”，不能证明“prompt 单独提升了正确率”。这也是我面试时不会报一个虚的百分比的原因。

### 1.7 面试追问与诚实回答

**问：所以你的 prompt 优化提升了多少？**  
答：没有可归因的提升数字。我做的是 prompt 架构迭代：把稳定职责留在 prompt，把动态事实移出 prompt；版本和 hash 能核对，final12 有同期行为记录。但没有 prompt-only 的 paired A/B，也没有权威语义评分，所以我不会说“提升 x%”。已确认的 state/PDF 问题也不能算成 prompt 的功劳。

**问：那你怎么知道新 prompt 没变差？**  
答：有版本 diff、hash 一致性和固定样本的结构/证据检查；final12 有真实回答。但这只能说明没有明显结构退化，不能说明语义全面提升。要证明因果，需要同模型、同上下文、预注册 case 的 old/new 对照和人工/权威判定；这次没有做。

---

## 2. Loop 2：Context Engineering —— 从“语义 failed 被写成 unknown”到可追溯修复

### 2.1 症状：同一个动作，语义层说 failed，状态层说 unknown

旧产物 `robot_vacuum-v4-6aa0ba49c6` 的真实链路（[residual-trace.json](standalone.md#未包含)、[fact-boundary-closeout.md](fact-boundary-closeout.md) 第 2 节）：

| 环节 | 实际内容 |
|---|---|
| 用户原话 | “机器人自己回不到充电座，我把它放上去也充不上电。该先确认什么？” |
| 语义层 | `reseat_dock`，`outcome=failed`，证据 quote“我把它放上去也充不上电” |
| 状态层 | `reseat_dock` / label“重新对准充电座” / `outcome=unknown` |
| prompt 投影 | “重新对准充电座：已做但结果未知（用户第1轮，消息 m-67da3bd57205）” |
| 实际回答 | “你提到‘重新对准充电座’已经试过，结果还不清楚……” |

这不是“模型凭空编动作”。动作族有用户原话依据，但状态 reducer 没有采用语义 `failed`，而是用动作 regex 重算；regex 在“放上去”上匹配不到，或在后文问句上匹配到但之后无结果，于是返回 `None`，状态写成 `unknown`。prompt 和回答只是忠实放大了错误状态。

**症状的本质：** 语义已有 evidence-bound 结果，但 context/state 的表示与 reducer 没有把它传下去；失败的是 context/state 层，不是 prompt wording。

### 2.2 假设：状态应消费已验证的语义结果，regex 只做 fallback；来源/范围要绑定

假设分三点：

1. `executed_actions.outcome` 已由语义层给出并带用户 quote 时，state 应优先采用，不用动作 regex 重算。
2. regex 只在没有语义结果时兜底；匹配到问句或没有结果时不能猜成功/失败。
3. 观察也要按来源范围绑定：条件句、第三方、假设不能变成当前用户的 `manual_charging=works`；更正必须撤回旧 tried 并保留审计。

这是典型的 context/state 修复，不是“再写一句 prompt 让模型注意”。

### 2.3 干预：state/execution 优先消费语义 outcome，scope 与显示标签分开

- `pi-reseat-outcome-lost`：state 优先消费已验证、证据绑定的 `executed_actions.outcome`；regex 只在无语义结果时兜底。见 [product-closed-loop.md](product-closed-loop.md) 第 1 节。
- `pi-third-party-fallback-observation`：条件/第三方子句的 source-scope 向后传递，遇到第一人称、转折或句界重置；语义观察校验与 deterministic fallback 共用。反例：当前用户自己的手动观察仍保留。
- 共享 `reseat_dock` 的 tried 显示统一为“手动放回充电座”，raw quote 保留；动作 id/详情不变。这是显示/投影修正，不是把“随手放上”升级成“已对准”。
- 旧持久化行用中性“自动回充（用户原话）”标签，避免把明确恢复写成“已检查自动回充设置”。

**修复后的真实产物：**

- `robot_vacuum-v4-6b90c44acf`：历史 LLM smoke 实际跑在这个产物上；5 selected cases / 7 initial turns + 2 deterministic follow-ups。
- `robot_vacuum-v4-24f3a3db39`：最终交付产物；12 案 final12 结构验收使用该产物。

### 2.4 验证：old 6aa0 → new 6b90 → final 24f3，另有反例

**A. 旧 6aa0（问题证据）**

- `semantic_outcome=failed`，`state_outcome=unknown`；`residual-trace.json` 的 `verification.semantic_outcome` / `state_outcome` 明确记录。
- prompt 行“重新对准充电座：已做但结果未知”由保存的 state 和确定性模板重建；不是 raw provider prompt 抓包。
- 归因写明 `confirmed_fabrication=false`；是 state/execution 丢失语义 failed，不是模型故意编造。

**B. 新 6b90（修复后的真实 smoke）**

- [`rm-recharge-failed-outcome`](product-closed-loop.md) 第 1 轮真实 state：`tried_actions[0] = reseat_dock / failed`，`observations = manual_charging / fails`。
- 同一 product closed-loop report 的 T1/T2 结构化 checks 均通过，5/5 selected cases 结构通过；这是 development smoke，不是成功率。
- 真实回答仍正确区分“随手放上”与“特意对准”，没有把原因定死。

**C. 最终 24f3（final12）**

- [`rm-recharge-failed-outcome`](standalone.md#未包含) 状态 `passed_structural_with_lineage_finding`；第 1 轮 state `reseat_dock / outcome=failed`，第 2 轮仍为 `failed`。
- 第 2 轮原始 `retrieval_attempted=false` 保留为 lineage finding，不改写成通过。
- final12 的 `rm-recharge-multiround-correction` 状态通过：第 2 轮撤回旧 tried，进入未试，保持 active；不是“继续按错误前提排查”。

**D. 负例/反例（不只挑成功样本）**

| 反例 | 期望 | 证据 |
|---|---|---|
| 第三方/假设：“朋友家机器人手动能充” | 不写成本用户 `manual_charging=works`；不写 tried；不写 partial_success | final12 `rm-recharge-hypothetical-third-party` 结构通过；6b90 第三方 case 无观察/无 tried/无 partial |
| 明确更正：“我没放回，只是按了回充键” | 撤回旧 tried；进入 not_tried；保持 active | final12 `rm-recharge-multiround-correction`；6b90 同 case 通过 |
| 明确自动恢复：“它自己找到座并开始充电” | resolved；`automatic_return=works`；tried 为空；原因仍不确认 | final12 `rm-recharge-explicit-recovery`；6b90 同 case 通过 |
| 明确对准后仍失败 | 保留 failed；不把“随手放上”混入“已对准” | final12 `rm-recharge-failed-outcome` 第 2 轮；6b90 同 case |

**E. provenance 可追溯**

- 6aa0 的 quote/turn/message id、state 字段和代码符号都在 `residual-trace.json`；不是只靠总结。
- 6b90/24f3 的 state 带 `source_message_id`、`source_turn`、`provenance`、`evidence`；final12 的 transcript 和 support state 都能回到用户原话。
- 这不是“引用语义正确率”，而是事实绑定与来源可追溯。

### 2.5 限制

- 旧 6aa0 是固定 6-turn 批次 + 单个 residual trace；不是 holdout，不估计同类措辞的总体频率。
- 6b90 是 5 条 selected case 的真实 smoke；最终 24f3 的 12 案是结构验收，不是权威 Judge 语义通过。
- 12/12 cases 完成不等于所有 raw checks 通过；`retrieval_attempted=false` 的 lineage finding 原样保留。
- `not_retrieved` 只表示该轮没有当前检索记录，不等于历史引用一定伪造；不能把 lineage 解释扩成“引用都正确”。
- state 原始 `/label` 仍可能是旧显示值，UI/prompt/handoff 走中性投影；不要把“显示修正”说成“所有持久化字段都已重写”。
- 没有真实用户、成功率、生产 SLA；没有外部工单。

### 2.6 90 秒口述版

> 这个 context 修复的起点是一条真实链路：用户说“我把它放上去也充不上电”，语义层已经给出 `reseat_dock / failed`，还带用户原话；但旧 6aa0 的状态层用动作 regex 重算，没匹配到“放上去”，就把 failed 写成 unknown。prompt 于是投影“已做但结果未知”，回答也说“结果还不清楚”。  
> 我的假设是：状态应该优先消费已验证、带 quote 的语义 outcome；regex 只做 fallback，匹配到问句不能猜。改在 state/execution，不是加一句 prompt。  
> 验证分三层：旧 6aa0 有 semantic failed → state unknown 的 trace；新 6b90 的真实 smoke 里，同一 case 的 tried 是 reseat_dock / failed；最终 24f3 的 final12 里，这个 case 通过，第二轮的 failed 也保留。  
> 我还看反例：第三方/假设不会被写成本用户手动成功；明确更正会撤回旧 tried；明确自动恢复才结案。边界是：这是 development 证据，不是 holdout，不是成功率，也没有权威语义 Judge。

### 2.7 面试追问与诚实回答

**问：你怎么证明不是挑了一个成功例子？**  
答：我没有只讲一个成功答案。旧 6aa0 有明确的 semantic failed → state unknown trace；新 6b90 和最终 24f3 是不同阶段的产物；同时有第三方/假设、明确更正、明确自动恢复、明确对准后仍失败四个反例。它们都只支持“机制在这个范围内成立”，不支持“全部措辞都泛化”。  
**问：那这算语义正确率提升吗？**  
答：不算。final12 是结构验收，12/12 cases 完成但 1 条 lineage finding 保留；没有 holdout、没有权威 Judge、没有逐 claim 蕴含。所以我讲的是 old→new 的证据链和反例边界，不是成功率。

---

## 3. 不属于这两个 loop 的相邻修复

以下证据真实，但**不能拉进 Prompt Engineering 或 Context Engineering 的因果叙事**：

| 修复 | 实际层 | 为什么不算 |
|---|---|---|
| PDF 8→101 块、私有索引 823→916 | RAG ingestion / coverage | 是编号条目切片与实例私有索引覆盖，不是 prompt 或 context 修复；[pdf-retrieval-fix.md](pdf-retrieval-fix.md) |
| `reseat_dock` 显示标签“重新对准”→“手动放回” | state/display projection | 是标签范围修正，不是 prompt 改写；raw quote 保留 |
| trace 9 次调用记成 18 个事件 | 跨项目可观测性 | 不是客服 prompt/context 效果，也不是 9→3 提效 |
| 139 / 14 / 13 条定向测试 | 工程回归 | 只证明被测试行为，不是成功率、不是用户体验通过率 |

**不设第三个 loop。** 如果面试需要讲 PDF，单独作为“RAG 检索根因与切片修复”讲，不要借它证明 prompt/context 优化。

## 4. 可复查证据索引

| 结论 | 证据 |
|---|---|
| prompt 版本、hash、行数 | [v1.json](../robot_vacuum/v1.json)、[v2.json](../robot_vacuum/v2.json)、[v3.json](../robot_vacuum/v3.json)、[v4.json](../robot_vacuum/v4.json) |
| v4 prompt 与冻结基线一致 | [approved-development-baseline.md](approved-development-baseline.md) 第 4 节（`4e471a30...`） |
| 最终 spec / artifact 对应 | [build-robot_vacuum-v4-24f3a3db39.json](standalone.md#未包含)（`robot_vacuum/v4.json` = `4fc4d815...`） |
| 旧 6aa0 的 failed→unknown 链路 | [residual-trace.json](standalone.md#未包含)、[fact-boundary-closeout.md](fact-boundary-closeout.md) 第 2 节 |
| 6b90 修复后 state= failed | [product-closed-loop.md](product-closed-loop.md) 第 1/3 节、[product-closed-loop report](standalone.md#未包含) |
| 最终 24f3 / final12 | [acceptance12 report](standalone.md#未包含)、[release-v1 report](standalone.md#未包含) |
| 反例：第三方/更正/恢复/明确对准 | final12 对应 transcript 与 support state；[rotating-mainline-acceptance.md](rotating-mainline-acceptance.md) |
| 不能当成功率的测试数字 | [product-closed-loop.md](product-closed-loop.md)（139/14）、[pdf-retrieval-fix.md](pdf-retrieval-fix.md)（13） |
| 本批维护报告与边界 | [outputs/demo-kit-v1/report.json](standalone.md#未包含) |

**一句话总口径：** Prompt loop 是“稳定职责架构演进，但无因果实验”；Context loop 是“语义 failed 被状态层丢失后的 old→new 修复，有反例和 provenance，但不是成功率”。
