# 第一轮案例定义人工审核分析（32 条）

> 日期：2026-11-13（香港）  
> 审核对象：`reviewScope=case_definition`，不是 Agent 回答评分  
> 固定快照：2026-11-13T19:10:47+08:00；32 条审核记录；最后记录更新 19:00:39；聚合 SHA-256 `ef7360b73b1d77372da0eadba47e212b0ac31235772ca537552f995a6e02dc75`  
> 数据：v1.1；`cases.jsonl` SHA-256 `13095f203f4acf1223d1f0ae7af6585edaf579ff4dde31f9bc2bed50b4be77e3`；holdout 16 条未运行  
> 本轮性质：只写文档；不改 prompt/runtime/dataset/reviews/GUI；不启动服务；不运行 holdout；不把审核结果当成功率

## 1. 结论先行

- **32/32 条案例定义已完成人工判定**：`26 passed / 2 failed / 4 uncertain`，其中 **7 条有 notes**。
- 这 32 条是“题目 + oracle + 证据”的审核，不是 Agent 回答通过率。通过只表示题面/期望在人工看来看得过去，不表示模型答得好，更不能写成成功率、准确率或任务完成率。
- 32 条中 **30 条 QA + 2 条 fault**；fault 必须单列，不能混进用户体验 QA 分母。当前是 `case_human_32_review_done`，但 **P1 全量评测（P1-05/P1-06）仍未完成**，holdout 仍未运行。
- 7 条 notes 指向的不是 7 个孤立措辞问题，而是四类共性机制：**工作语境缺失、渐进诊断缺失、歧义/未结任务未建模、测试真实性与分层不足**。
- 下一最优共性改进不是“再改一版 system prompt”，而是先定义 **work_context + 渐进诊断 + 歧义/未结任务 policy/oracle**，用一个回充纵向切片验证，再迁移到同族新例子和反例。

### 1.1 判定统计

| 类别 | 总数 | passed | failed | uncertain | 有 notes |
|---|---:|---:|---:|---:|---:|
| `direct_qa` | 8 | 7 | 0 | 1 | 1 |
| `multi_turn` | 10 | 7 | 1 | 2 | 3 |
| `negation_correction_ambiguity` | 6 | 4 | 1 | 1 | 2 |
| `missing_or_out_of_domain` | 6 | 6 | 0 | 0 | 0 |
| `fault_recovery` | 2 | 2 | 0 | 0 | 1 |
| **合计** | **32** | **26** | **2** | **4** | **7** |

### 1.2 七条 notes 一览

> **公开仓库版说明：** 7 条个人审核 notes 的逐字原文已移除，替换为 `[个人审核原文已从公开仓库移除]`；用户判定、caseId 和分析意见保持不变。原始记录只保留在本地 `eval/reviews/`，不随仓库发布。下文“不删改 notes”指的是原工作区历史记录，不是公开副本。

| caseId | 用户判定 | 原文 notes | 本分析意见 | 是否需补信息 |
|---|---|---|---|---|
| `rsv1-dev-direct-recharge-triage-01` | uncertain | [个人审核原文已从公开仓库移除] | 同意；改为单步引导 + 熟悉度前提 | 是：是否已试/是否愿意动手 |
| `rsv1-dev-multi-recharge-tried-advance-01` | uncertain | [个人审核原文已从公开仓库移除] | 同意；区分机器人找不到 vs 用户找不到实体座 | 是：出题人 intended reading |
| `rsv1-dev-correction-mop-water-level-02` | uncertain | [个人审核原文已从公开仓库移除] | 部分不同意“完全重复”；但独立性待证明 | 是：02 的独立机制 |
| `rsv1-dev-multi-topic-switch-mop-02` | uncertain | [个人审核原文已从公开仓库移除] | 同意；答新问题后一次轻量确认 | 是：旧任务当前状态 |
| `rsv1-dev-negation-filter-not-cleaned-02` | failed | [个人审核原文已从公开仓库移除] | 同意；话术测试味重，改自然说法 | 否 |
| `rsv1-dev-multi-recharge-closure-01` | failed | [个人审核原文已从公开仓库移除] | 部分同意；弱结案信号要分层，不能把沉默当解决 | 是：真实结案话术样本 |
| `rsv1-dev-fault-process-restart-01` | passed | [个人审核原文已从公开仓库移除] | 同意；restart 必要但应进 fault 套件，程序化生成 | 否 |

**所有 7 条的用户判定均保持原样。** 本分析只提出后续处理意见；不替用户改 passed/failed/uncertain，不把“分析意见”写成新的用户判定。公开仓库只隐去 notes 原文，未改判定或分析结论。

## 2. 审核口径、快照与证据边界

### 2.1 这是题目审核，不是回答评分

`case_definition` 审的是：题目、逐轮用户输入、`required_claims`、`forbidden_claims`、`required_behaviors`、`expected.state`、`expected_sources` 和证据 quote 是否合理。它不评价模型实际回答，也不把人工审核写回自动分数。

因此：

- `26 passed` 不能写成“26 条 Agent 回答通过”。
- `2 failed` 不能写成“产品失败率 2/32”。
- `4 uncertain` 不能写成“模型不确定”。
- 7 条 notes 是产品/数据/测试信号，不是统计样本。

### 2.2 固定快照

- 读取时间：`2026-11-13T19:10:47+08:00`
- 审核文件：32 个，`eval/reviews/reviews/*.json`
- 最后记录更新时间：`2026-11-13T19:00:39+08:00`
- 聚合方式：按文件名排序，对“文件名 + 换行 + UTF-8 原文 + 换行”整体做 SHA-256，得到 `ef7360b7...e02dc75`
- 数据集：`robot-support-v1` / `v1.1`
- `cases.jsonl`：`13095f20...77e3`
- `suite.json`：`97761a3d...bfc`
- `split.json`：`99389938...d70e`
- `audit.json`：`328f5ff8...dada`
- holdout cases hash：`9aa281ff...12db1`
- 已找到的 holdout run：**0**

后台审核服务可能在用户继续审核时继续写入；本报告只认上述快照。快照之后的新增/修改不进入本轮统计，也不反向改写本报告。

### 2.3 与既有评测证据分开

既有 state run 的记录是 `2 passed / 13 failed / 15 not_applicable`，2 条 fault 为 `unsupported`；3 条真实模型记录离线重评分是 `1 条自动检查失败、2 条待人工复核`。这些是**另一层证据**，不能拿来反推本次题目审核结论，也不能和 26/2/4 相加。

证据：

- [eval-integration-report.json](standalone.md#未包含)
- [p1-recharge-report.json](standalone.md#未包含)
- [p1-recharge-real-run.json](standalone.md#未包含)
- [eval/README.md](../eval/README.md)

## 3. 七条 notes 逐条分析

> 说明：7 条都不主要由“检索工具”造成；工具层问题属于回答运行层，不是本次题目定义审核的主因。下面“判定层级”按 data / context / tools / state / interaction / test mechanism / execution / validation 标注，未涉及的层不硬凑。

### 3.1 `rsv1-dev-direct-recharge-triage-01`

- **原文**：`[个人审核原文已从公开仓库移除]`
- **判定**：用户 `uncertain`；family `fam-dev-direct-recharge-triage`；category `direct_qa`
- **题目在测什么**：用户说“回充总是失败，充电座在客厅，周围有拖鞋，应该怎么排查？”expected 要求给低风险排查项，并询问已经试过哪些。
- **根因假设**：oracle 奖励“多分支清单 + 追问已试”，隐含用户已经会观察、会动手。note 不是说题目完全没有引导，而是说**引导粒度不对**：对不会排查的用户，应先建立熟悉度，再给一个最低成本步骤和观察点，等反馈后分支；一次给多项会让用户不知道从哪开始。
- **判定层级**：`data`（案例前提/oracle 粒度）、`interaction`（单步引导）、`validation`（人工真实性复核）。
- **处理意见**：保留 `uncertain`。下一轮把“是否已试/是否愿意动手/是否懂术语”作为前提或独立变体；oracle 改为“先一个最低成本安全检查（如清出充电座前方空间）→ 说明看什么结果 → 等反馈再分支”。不要求用户先会排查，不把完整清单当唯一正确答案。
- **是否需补信息**：是。当前案例没有说明用户是否已试、是否愿意动手；不能由 Agent 臆测。
- **需要代码吗**：后续需要交互/状态层的“熟悉度未知时单步引导”；本轮不改 runtime，不先写 prompt 规则。
- **新同类例子/反例验收**：
  - 新手说“我不懂，你一步步告诉我” → 只给一个低风险步骤，并说明观察点。
  - 用户说“拖鞋挪开了还是回不去” → 记录已试清障，给下一个不同步骤。
  - 用户主动要“完整排查清单” → 可以给多项，但标明顺序和停止条件。
  - 反例：用户已说“我清过了”，仍重复清障作为唯一建议；或一次倾倒全部步骤，却不问用户看到什么。

### 3.2 `rsv1-dev-multi-recharge-tried-advance-01`

- **原文**：`[个人审核原文已从公开仓库移除]`
- **判定**：用户 `uncertain`；family `fam-dev-multi-recharge-tried-advance`；category `multi_turn`
- **题目在测什么**：用户说“回充失败，找不到充电座”；fixture 建议清障/擦触点和传感器；用户说“都做了，还是回不去”；expected 按“机器人导航找不到座”给下一步。
- **根因假设**：“找不到”缺主体/经验者。至少有两种读法：
  1. **机器人**回不到座（导航/红外/摆放/传感器）；
  2. **用户**找不到实体充电座（被挪走/不知道放哪/要重新摆放）。
  
  当前 `issue` 只存原始文本，expected 默认第 1 种，没有把“歧义”作为需要低成本澄清的状态。
- **判定层级**：`data`（expected 默认解释）、`context`（主体/经验者）、`state`（ambiguity 表示）、`interaction`（二选一澄清）。
- **处理意见**：保留 `uncertain`，原 expected 不代改。后续在 context/state 增加 ambiguity 候选；仅当不同解释会改变下一步时，问一个二选一低成本问题（“是机器人回不到座，还是您找不到座的位置？”），再分别分支。若上下文已明确“机器人在座前打转/对不准”，不重复问主体。
- **是否需补信息**：是。需要出题人确认 intended reading；当前案例没有明确，属于待核实，不应由分析替用户判定。
- **需要代码吗**：后续需要 context/state 的 ambiguity 表示与澄清动作；不用新增关键词正则硬判“找不到”。
- **新同类例子/反例验收**：
  - “回充失败，找不到充电座” → 先问二选一，再按解释分支。
  - “机器人老在充电座前面转，就是对不上” → 不问主体，直接问对准/信号/摆放。
  - “我搬完家找不到充电座放哪了” → 按用户找座/摆放处理，不套机器人传感器排查。
  - 反例：两种解释都给同一套“擦传感器”步骤；或每轮都问“谁找不到”造成打断。

### 3.3 `rsv1-dev-correction-mop-water-level-02`

- **原文**：`[个人审核原文已从公开仓库移除]`
- **判定**：用户 `uncertain`；family `fam-dev-correction-mop-water-level`；category `negation_correction_ambiguity`
- **题目在测什么**：
  - 01：用户先说“水箱是满的”，后更正“只剩一点水”；下一步是补水，再查水路。
  - 02：用户先说“水箱有水”，后更正“其实没装到位，里面没水”；下一步是重新安装并加注清水。
- **根因假设**：两条**不是逐字重复**，也不是完全同一个 oracle：01 更正水位，02 更正安装/无水，后续动作不同。但两者同族、同“最后一次明确陈述为准”机制，表面差异小；数据里没有 `variant_mechanism_id` 之类字段说明“本变体到底验证哪个不同决策边界”，所以用户合理感到重复。
- **判定层级**：`data`（变体定义/oracle）、`test_mechanism`（同族变体独立性）、`validation`（重复性校验）。
- **处理意见**：保留 `uncertain` 和两条原文。**部分不同意“完全重复”**，举证如上；但若 02 不能证明“安装/无水”分支与 01 有独立失败机制，下一轮数据修订应合并或补强 02。先加变体机制标签，审核后再决定，不现在改题。
- **是否需补信息**：是。需要出题人确认 02 的独立目的：是验证“安装问题”，还是只验证“最新前提覆盖”。若只是后者，边际价值确实低。
- **需要代码吗**：本轮不需要 runtime；后续是数据/oracle 与重复性校验，不是 prompt 或正则。
- **新同类例子/反例验收**：
  - 只按“水位不足”回答的 Agent 必须在 02 失败（没处理安装/无水）。
  - 只重复“重新安装水箱”的 Agent 必须在 01 失败（没处理水位不足）。
  - 若合并为一条，必须同时覆盖“水位”和“安装”两个不同下一步，并有反例。
  - 反例：两条用户话术、expected claims 和下一步几乎相同，仅换一个词；或为制造差异硬加与故障机制无关的细节。

### 3.4 `rsv1-dev-multi-topic-switch-mop-02`

- **原文**：`[个人审核原文已从公开仓库移除]`
- **判定**：用户 `uncertain`；family `fam-dev-multi-topic-switch-mop`；category `multi_turn`
- **题目在测什么**：用户先说“回充失败，已经清了充电座周围”，再说“对了，拖地时水箱漏水，怎么办？”expected 要求切换到漏水主题、来源不串、保留旧回充任务。
- **根因假设**：expected 只要求“保留旧任务”，没有定义“旧任务未结时如何轻量确认”。状态里保留 case 不等于交互上会确认；用户切换主题后，旧问题可能被静默悬挂。note 指向的是**未结任务的交互收口**，不是来源作用域。
- **判定层级**：`data`（oracle 缺 required behavior）、`state`（open task/old status）、`interaction`（轻量确认）。
- **处理意见**：保留 `uncertain`。后续 oracle 加：先完整回答新主题，再在结尾用一句轻量确认旧任务状态（“另外，之前的回充问题现在解决了吗？”）。旧任务已解决、用户说“先不说回充”、或刚确认过时不得重复问；不能为了确认旧问题打断新请求。
- **是否需补信息**：是。需要补旧回充任务当前是“已解决/未解决/未知”；案例只说了已清障，没有说结果，状态应显式为 unknown。
- **需要代码吗**：后续在 state 增加 `open_tasks` / `old_task_status` / `last_confirmed_turn`；交互层实现“答新问题后一次轻量确认”。本轮不改 runtime。
- **新同类例子/反例验收**：
  - 切换新主题且旧任务未结 → 先答新问题，末尾一次轻量确认。
  - 用户已说“回充好了” → 不再问旧问题。
  - 用户说“先不说回充” → 不确认、不追问。
  - 连续切换两次 → 旧任务最多一次确认，有冷却/去重。
  - 反例：为了确认旧问题先打断新问题或不回答新问题；或每轮都问“之前的问题解决了吗”。

### 3.5 `rsv1-dev-negation-filter-not-cleaned-02`

- **原文**：`[个人审核原文已从公开仓库移除]`
- **判定**：用户 `failed`；family `fam-dev-negation-filter-not-cleaned`；category `negation_correction_ambiguity`
- **题目在测什么**：用户首句“吸力下降，过滤网没清过，别当成清过”，第二句“我清的是尘盒，不是滤网”。expected 要求区分尘盒与滤网，记录滤网未清。
- **根因假设**：为了逼出否定/对象区分，话术写成测试指令（“别当成清过”），暴露评测意图。真实用户更可能说“滤网一直没洗，就倒了尘盒”。这不是语义 oracle 错，是**语言真实性/生成方式**问题。
- **判定层级**：`data`（问法）、`test_mechanism`（用测试旁白逼 oracle）、`validation`（自然语言审核）。
- **处理意见**：保留 `failed` 和原文。下一轮数据修订把首句改为自然说法，保留“尘盒已清、滤网未清”的语义 oracle；另建语言鲁棒变体（口语、省略、错别字、方言词），但不把变体数量当成功率。
- **是否需补信息**：否。失败原因已由原文和话术直接支持。
- **需要代码吗**：数据与测试机制修改；不需要 runtime/prompt 补丁。
- **新同类例子/反例验收**：
  - “吸力小，滤网一直没洗，就倒了尘盒” → 记录滤网未清、尘盒已清。
  - “我没洗过滤网，只把尘盒倒了” → 同上。
  - “滤网洗过了，晾了一天” → 不得标成未清。
  - 反例：用户说“滤网洗了”，Agent 仍说“你没清过滤网”；或把“尘盒已清”当“滤网已清”。

### 3.6 `rsv1-dev-multi-recharge-closure-01`

- **原文**：`[个人审核原文已从公开仓库移除]`
- **判定**：用户 `failed`；family `fam-dev-multi-recharge-closure`；category `multi_turn`
- **题目在测什么**：用户说“回充失败，充电座周围有杂物”，fixture 建议清理；用户说“清干净后已经能自己回充了，谢谢”。expected 要求识别成功、结案、不继续追加故障步骤。
- **根因假设**：oracle 依赖“用户主动、明确报告成功”这一便利信号；真实用户可能只说“好了/可以了/谢谢”，或不再回复，或只报告部分成功。当前只有 explicit success 与 partial success 两个变体，缺少对**弱结案信号**和**沉默**的分层。
- **判定层级**：`data`（结案话术/oracle）、`state`（resolved 证据）、`interaction`（收束）、`test_mechanism`（成功/部分成功/沉默）。
- **处理意见**：保留 `failed` 和原文。**部分同意**“主动报告成功不典型”；但不能因此把沉默/一句谢谢自动判 resolved。后续定义 closure evidence 分级：明确成功 > 可验证成功 > 弱正向信号（谢谢/好了）> 沉默。只有明确/可验证成功才结案；弱信号可礼貌收束但不宣称已解决；沉默不作事实。补“手动能充但自动不行”反例，防止假结案。
- **是否需补信息**：是。需要补真实/模拟的结案话术样本；当前没有真实用户数据，弱信号如何处理仍待核实。
- **需要代码吗**：后续 state/interaction 的 closure policy；本轮不写代码。
- **新同类例子/反例验收**：
  - “清完能自己回去了” → 明确成功，结案。
  - “好了，谢谢” → 礼貌收束，但不把 case 标 resolved；必要时一次轻确认。
  - “手动放回能充，自动还是不行” → 保持 active，不结案。
  - 用户不再回复（模拟）→ 不推断成功。
  - 反例：用户只说“谢谢”，Agent 就写“已解决”；或用户说“手动能充”，Agent 当成自动回充已解决。

### 3.7 `rsv1-dev-fault-process-restart-01`

- **原文**：`[个人审核原文已从公开仓库移除]`
- **判定**：用户 `passed`；family `fam-dev-fault-process-restart`；category `fault_recovery`
- **题目在测什么**：会话 A 记录已清障/擦触点；服务重启后同会话追问“我刚才试过什么”；另开新会话 B 验证隔离。
- **根因假设**：case 把工程故障注入写成用户对话（“服务重启后”），测试意图暴露。restart 本身**必要**，但它验证的是状态持久化/隔离，不是自然对话体验，不应和 QA 回答质量混在一个分母里。当前 fault 在 state run 仍 `unsupported`，说明执行机制未落地。
- **判定层级**：`test_mechanism`（fault 应单列/生成）、`data`（对话话术）、`state`（恢复语义）、`execution`（注入）、`validation`（分母分离）。
- **处理意见**：保留 `passed`（案例定义层面）和原文。后续把 restart 移入工程 fault 套件：用 faultpoint + seed 程序化生成（重启时机、同/跨 session、重复请求、存储刷盘），oracle 验状态连续性/隔离/无重复副作用；用户可见话术只保留自然的“我刚才试过什么”，去掉“服务重启后”这种测试旁白。**不是单纯压测**：压测证明吞吐/稳定，不证明恢复语义。
- **是否需补信息**：否。需要的是测试机制决策，不是补事实。
- **需要代码吗**：后续 fault 生成器/runner 与状态持久化验证；不改产品 prompt，也不把 restart 混入 QA 分母。
- **新同类例子/反例验收**：
  - seed 生成：首轮后杀进程 → 同 session 恢复 → 复述已试步骤。
  - seed 生成：重启后新 session → 不得读到旧 session 事实。
  - seed 生成：重启后重复提交同一条消息 → 不重复记 tried。
  - 重启后继续排查 → 不把“重启”当“恢复成功”。
  - 反例：只测“服务还活着/能继续聊”，不验状态；把 restart 结果算进 QA 回答成功率。

## 4. 跨 32 条的六类共性问题

### 4.1 语言真实性与先验术语负担

**证据案例**：`rsv1-dev-negation-filter-not-cleaned-02`、`rsv1-dev-correction-mop-water-level-01`、`rsv1-dev-boundary-unknown-model-price-02`、`rsv1-dev-fault-process-restart-01`、`rsv1-dev-direct-recharge-triage-01`。

**机制**：部分问句从 oracle/术语反推，出现“别当成清过”“更正：”“给我个确定答案”“服务重启后”等测试旁白。32 条开发案例中，用户话术含“回充”16 条、含“充电座”14 条、含“触点”4 条。这个计数不证明“不自然”，但说明题集默认用户熟悉产品词；没有 `user_familiarity` 字段，也没有口语/省略/错别字变体层。

**层级**：`data`、`test_mechanism`。

**风险**：把“懂术语、会主动排查、会报告成功”当默认用户，通过只代表会做题，不代表真实体验。

### 4.2 Agent 引导排查，而不是要求用户先会排查

**证据案例**：`direct-recharge-triage-01/02`、`direct-mop-no-water-01/02`、`direct-suction-decline-01/02`、`direct-base-power-01/02`。

**机制**：rubric 奖励完整多分支清单，并常问“你试过哪些”；对新手缺少“症状 → 一个最低成本澄清/安全检查 → 记录反馈 → 分支”的顺序，也没有“不要每轮都问同一个问题”的约束。

**层级**：`data`、`interaction`。

**风险**：Agent 像 FAQ 清单而不是售后引导；用户不会排查时卡住，或一次被塞太多步骤。

### 4.3 指代/主体/经验者歧义

**证据案例**：`multi-recharge-tried-advance-01`、`ambiguity-recharge-condition-01`、`multi-recharge-closure-02`。

**机制**：“它回充不行”“找不到充电座”既可指机器人导航失败，也可指用户找不到实体基座；“找不到”缺主体。当前 `issue` 只存原文，expected 默认一种解释。

**层级**：`context`、`state`、`interaction`。

**风险**：在错误解释上继续排查，越答越偏；或每轮重复问主体，打断用户。

### 4.4 未结任务与话题切换

**证据案例**：`multi-topic-switch-mop-01/02`、`multi-recharge-closure-01/02`。

**机制**：状态保留旧 case，但 oracle 只要求“来源不串/旧任务保留”，没有定义答完新问题后的一次轻量确认；结案又依赖明确成功报告，缺少弱信号/部分成功的分层。

**层级**：`state`、`interaction`、`data`。

**风险**：旧问题静默悬挂；或为了确认旧问题反复打断新请求；或把部分成功/沉默当结案。

### 4.5 同族变体的独立机制不清

**证据案例**：`correction-mop-water-level-01/02`；以及 24 个 family 固定 2 变体的设计。

**机制**：部分变体只换表面情境，未声明“本变体验证哪个不同决策边界”，用户会合理感到重复。01/02 实际有水位 vs 安装/无水的差异，但缺少机制标签，边际价值不易证明。

**层级**：`data`、`test_mechanism`、`validation`。

**风险**：题量看起来增加，实际重复测同一行为；为凑差异而硬加无关细节。

### 4.6 工程 fault 与用户体验 QA 混层

**证据案例**：`fault-process-restart-01/02`；2 条 fault 在 state run 均为 `unsupported`。

**机制**：restart 是必要的工程故障验证，但当前以自然对话 case 形式出现，且案例审核表与 QA 同表；执行机制未落地。

**层级**：`test_mechanism`、`execution`、`validation`。

**风险**：把“能继续聊”当恢复成功；把 fault 结果混进 QA 成功率；测试话术污染真实对话集。

## 5. 共性机制建议

### 5.1 工作语境模型（work context）

不要从“这句话该配什么 prompt 规则”开始，而是先问：**Agent 是否知道它在什么工作语境里？** 建议每条任务维护可审计的语境对象：

| 字段 | 含义 | 例子 | 规则 |
|---|---|---|---|
| `domain` | 业务领域 | 扫地/扫拖机器人售后 | 不从其他领域借事实 |
| `object` | 作用对象 | 机器人/充电座/水箱/滤网/尘盒 | 区分“清尘盒”与“清滤网” |
| `symptom` | 用户观察到的现象 | 回充失败、找不到座、拖地不出水 | 用用户的话，不先替用户下诊断 |
| `experiencer` | 谁遇到 | 机器人回不到座 / 用户找不到座 | 歧义时保留候选 |
| `user_familiarity` | 用户熟悉度 | 新手/熟练/未说明 | **只在用户明说时记录，否则 unknown** |
| `interaction_stage` | 交互阶段 | 首次描述/已试/待确认/结案 | 阶段决定问什么、给多少 |
| `constraints` | 约束/风险 | 不拆机、租房、木地板、安全风险 | 来自用户原话或资料 |
| `evidence_refs` | 证据 | 用户原话、检索片段、来源文件名 | 可追溯，不把建议当事实 |
| `unknowns` | 未知 | 是否已试、旧任务是否解决 | 不臆测，不默认 |

**关键原则**：

- 信息无依据不臆测；`unknown` 就是 `unknown`，不用默认值补。
- 动态事实进 context/structured state，不写进系统 prompt。
- **Python `^` 类比**：如果 Agent 在 Python 的 `^` 上出错，正确抽象不是把“`^` 是异或/幂”塞进系统 prompt，而是让 Agent 获得语言、版本、任务、文件、运行环境，并用工具/文档验证。本客服同理：不把每个故障词、句式、排查步骤写成系统 prompt 规则；代码知识也不应塞进本客服 prompt。
- 工作语境不是“让模型自由发挥”，而是可审计的结构化事实 + 明确的未知。

### 5.2 渐进诊断（progressive diagnosis）

建议把“排查”从“一次给完整清单”改成一条可重复的策略：

```
用户观察到的症状
  → 最低成本澄清或安全检查
  → 记录用户反馈（已做/未做/结果）
  → 分支到下一步
```

规则：

1. **不要求用户已经会排查**。用户说“不知道”“不懂”“你一步步来”时，Agent 负责翻译成可观察、可执行的一步。
2. **首轮只问推进下一步所必需的一个问题**；有安全风险（过热、焦味、冒烟、漏电）先安全。
3. **不强制所有首轮都问同一个问题**。首问取决于症状、已知约束、风险和信息缺口；不能把“先问型号/先问已试”做成固定脚本。
4. 给步骤时说明**观察点**和**停止条件**；用户反馈后再给下一步。
5. 用户明确要完整清单/是熟练用户时，可以给多项，但标明顺序与分支，不把清单当保证。
6. 记录“已做/未做/结果未知”，不把助手建议当用户已做。

### 5.3 指代/歧义与未结任务

**歧义**用候选解释表示，不用单一默认值：

| 用户话 | 候选 A | 候选 B | 何时问 |
|---|---|---|---|
| “找不到充电座” | 机器人回不到座（导航/信号） | 用户找不到实体基座（摆放/被挪走） | 不同解释会改变下一步时，问一个二选一 |
| “它回充不行” | “它”=机器人 | “它”=充电座/其他 | 指代不明且影响判断时 |
| “水箱没水/没装好” | 水位不足 | 安装不到位 | 下一步不同（补水 vs 重装） |

规则：

- 问**关键低成本问题**，不要求用户懂“回充传感器/红外”等术语。
- 若上下文已经明确（如“机器人在座前打转”），不重复问主体。
- 歧义未解决时，不要把某一种解释写成事实；可以给一个安全观察项，但标明假设。

**未结任务**用任务栈/`open_tasks` 表示，而不是只看“当前 active case”：

- 主题切换：**先完整回答新主题**，保留旧任务；旧任务未结且未刚确认时，末尾最多一次轻量确认。
- 用户说“先不说/已经好了”或刚确认过 → 不问。
- 不能为了确认旧问题打断新请求；不能每轮都问。
- 结案分级：明确成功/可验证成功才 `resolved`；弱正向信号可礼貌收束但不宣称解决；沉默不作事实。

### 5.4 测试真实体验分层

建议分三套，不混分母：

| 套件 | 目的 | 输入 | 分母 | 不做什么 |
|---|---|---|---|---|
| `dialogue_qa` | 真实对话体验 | 自然用户话术、多轮、信息缺失 | 用户体验 QA | 不放工程 fault、不把变体数当成功率 |
| `language_robustness` | 语言鲁棒性 | 口语、省略、错别字、术语替换 | 单独报告 | 不混入主成功率、不要求句句逐字 |
| `engineering_fault` | 恢复正确性 | restart/timeout/replay/空检索/重复请求 | 故障恢复 | 不混 QA、不把“能继续聊”当成功 |

**restart 仍然必要**，但应：

- 用 **faultpoint + seed** 程序化生成：杀进程时机（首轮后/建议后/写状态后）、同 session/跨 session、重复请求、存储刷盘。
- oracle 验：状态连续性、隔离、无重复副作用、错误可恢复、可重放。
- **不是单纯压测**：压测证明吞吐/稳定，不证明恢复语义；故障注入要能复现、能判定。
- 用户体验 QA 分母不混恢复；恢复结果也不写成产品成功率。

## 6. 三个优先改进包（不超过 3 个）

### IP-1（优先级 1）工作语境 + 指代/歧义 + 渐进诊断

- **用户目标**：用户用自然话说不清对象/症状时，Agent 先理解语境，问一个关键低成本问题，再给一个可执行步骤，而不是要求用户懂术语或一次排查完。
- **普遍失败机制**：当前 rubric 默认“机器人找不到座=导航故障”、默认用户会排查；`issue` 只存原文，缺 `experiencer/familiarity/ambiguity`；回答容易变成完整清单。
- **应改层**：`data`、`context`、`state`、`interaction`。
- **最小实现**：先写 policy/oracle：`work_context` 字段 + ambiguity 候选 + progressive diagnosis 规则；只做“回充失败”一个纵向切片：识别“找不到充电座”的两种解释，必要时问一个二选一，再按解释给一个步骤并记录反馈。结构化 context 进 state，不塞系统 prompt；不新增关键词正则硬判。
- **同类迁移测试**：新手/熟练用户、机器人找不到 vs 用户找不到、有/无安全风险；反例：两种解释给同一套步骤、每轮重复问主体、用户已说已做仍重复建议。
- **未证明部分**：歧义识别准确率、追问是否让用户觉得啰嗦、结构化 context 的提取成本/延迟；没有真实用户样本。

### IP-2（优先级 2）未结任务、话题切换与结案证据

- **用户目标**：用户切换话题时，新问题先被回答，旧问题不被静默丢掉；只有明确成功才结案，不把部分成功/沉默当解决。
- **普遍失败机制**：state 保留 case 但缺 `open_tasks/old_task_status`；oracle 只要求保留旧任务，没有轻量确认；closure 依赖用户主动报告成功。
- **应改层**：`data`、`state`、`interaction`。
- **最小实现**：state 增加 `open_tasks`（topic/status/last_confirmed_turn）和 closure evidence 分级；策略：答新问题后最多一次轻量确认；用户说“先不说/好了”或刚确认过则不问；明确成功/可验证成功才 `resolved`。先写 oracle，再改状态机。
- **同类迁移测试**：旧任务未结/已结/用户叫停/连续切换；明确成功/弱信号/部分成功/沉默；反例：先打断新问题、每轮都确认、谢谢即结案、手动能充当自动成功。
- **未证明部分**：真实用户结案话术分布、弱信号阈值、沉默场景能否在产品中观测；需真实/模拟样本。

### IP-3（优先级 3）变体独立性与测试分层（含 restart 生成）

- **用户目标**：题集测到不同失败机制，不靠重复变体凑数；工程故障可复现生成，且不污染用户体验分母。
- **普遍失败机制**：同族 2 变体缺 `mechanism_id`；部分只换表面情境；restart 以对话 case 出现且执行未落地；QA/fault 分母边界不够显式。
- **应改层**：`data`、`test_mechanism`、`execution`、`validation`。
- **最小实现**：给每个变体加 `variant_mechanism_id/realism` 标签；建立三套清单（`dialogue_qa` / `language_robustness` / `engineering_fault`）；写 faultpoint+seed 生成器（先 restart：杀进程时机、同/跨 session、重复请求）；QA 分母排除 fault；fault 只验恢复语义。
- **同类迁移测试**：同族两条，只修 A 的 Agent 必须在 B 失败，反之亦然，否则合并；restart 生成矩阵可重放；反例：只验存活、不验状态，把 fault 算进 QA；语言变体单独报告。
- **未证明部分**：生成器的真实故障覆盖、进程杀死的确定性、存储刷盘/重复副作用边界；需要实际注入运行证据。

## 7. 先 oracle/policy，再实现：本轮的“不盲改”约束

后续实现顺序必须是：

1. **Policy 定义**：work context 字段、歧义分类、渐进诊断规则、open task/closure 分级、三套测试分母。
2. **Oracle 定义**：每个 case 期望的是“观察到什么语境/做了什么分支”，不是逐字复述；required claims/behaviors 写成可判定的语义。
3. **反例**：每个新 policy 至少配 1 个反例，防止过度澄清、过度确认、假结案。
4. **同族新例子**：在 development 补，不动 holdout；用新例子验收，不用 holdout 调参。
5. **最小实现**：只改必要的 state/interaction；结构化事实进 context/state，不先改 system prompt。
6. **人工复核**：保留用户原判定；有分歧标“部分同意/待核实”，不替用户改记录。

**明确不做**：

- 不因为 7 条 notes 就重写 system prompt。
- 不新增一堆正则/keyword 规则来“保证泛化”。
- 不为让案例通过而改测试预期、改用户判定或改冻结数据。
- 不把 26/2/4 写成 Agent 成功率或产品质量结论。
- 不把本轮分析当新的审批门；已有授权不重复确认。

**合法例外**：如果某个 API/协议确实要求特定语法（例如工具调用格式、字段名），可以在**局部 tool instructions** 里写特定规则；这不是“绝对禁止 specific 规则”，而是不要把局部协议要求升级成全局 system prompt 补丁。

## 8. 本轮边界与未完成项

- 本轮只写文档：`docs/review-round-1-analysis.md`、`outputs/review-round-1-analysis.json`、workspace 级 `../../AGENTS.md`、仓库级 `../AGENTS.md`、`plans/LONG_TERM_PLAN.md`。
- 本任务未写 `eval/reviews`、`eval/datasets`、`runtime`、`eval/review_server.py` 或 GUI 同文件（GUI 由另一任务并发处理）；未启动/重启服务；无可见窗口。
- 未运行 holdout；未运行模型；未跑 pytest；未改产品行为。
- **P1 全量评测未完成**：P1-02 仍是 `in_progress`，P1-05/P1-06 仍未完成；不能写“评测完成”。
- 7 条 notes 中，部分需要出题人补信息（triage 熟悉度、ambiguity intended reading、02 独立机制、旧任务状态、真实结案话术）；资料不足的标“待核实”，不假装已定。

## 9. 下一任务与计划更新

下一任务（不新增任务编号，归入 P1-02 的下一纵向切片）：

1. 先写 policy/oracle 说明（work_context、歧义候选、渐进诊断、closure 分级、三套测试分母）。
2. 在 development 补同类新例子和反例，不改 holdout。
3. 只实现最小状态/交互切片；context 进结构化 state，不先改 prompt。
4. 用新例子+反例验收，再人工复核；保留原用户判定。
5. 之后再进 P1-05 评测入口与 P1-06 基线。

计划记录见父工作区长期计划（未随仓库发布，见 [standalone.md](standalone.md#5-父工作区链接的处理)）的“2026-11-13 第一轮案例定义人工审核”一节。

## 10. 证据与限制

**证据**：

- 审核快照：`eval/reviews/reviews/*.json`（32 条，快照 hash 见第 2 节）
- 数据集：[cases.jsonl](../eval/datasets/v1/cases.jsonl)、[suite.json](../eval/datasets/v1/suite.json)、[split.json](../eval/datasets/v1/split.json)
- 生成/口径：[case-generation.md](case-generation.md)、[eval/README.md](../eval/README.md)、[manual-review.md](manual-review.md)
- 既有运行：[eval-integration-report.json](standalone.md#未包含)、[p1-recharge-report.json](standalone.md#未包含)、[p1-recharge-real-run.json](standalone.md#未包含)
- 审核工具：[manual-review-report.json](standalone.md#未包含)、[manual-review-browser.json](standalone.md#未包含)

**限制**：

- 48 条全为合成案例，本轮没有真实用户样本。
- 7 条 notes 是用户个人审核意见，不是统计结论；部分判断需出题人确认。
- 既有 state run 的失败与本次 `case_definition` 审核不是同一层证据。
- 没有运行 holdout/模型，没有启动服务，没有改 runtime。
- 未证明：真实用户语言分布、追问接受度、结案弱信号阈值、fault 生成器覆盖。

## 附录 A：32 条判定清单（快照）

| # | caseId | 判定 | notes |
|---:|---|---|---|
| 1 | `rsv1-dev-ambiguity-recharge-condition-01` | passed | |
| 2 | `rsv1-dev-ambiguity-recharge-condition-02` | passed | |
| 3 | `rsv1-dev-boundary-missing-context-01` | passed | |
| 4 | `rsv1-dev-boundary-missing-context-02` | passed | |
| 5 | `rsv1-dev-boundary-out-of-domain-01` | passed | |
| 6 | `rsv1-dev-boundary-out-of-domain-02` | passed | |
| 7 | `rsv1-dev-boundary-unknown-model-price-01` | passed | |
| 8 | `rsv1-dev-boundary-unknown-model-price-02` | passed | |
| 9 | `rsv1-dev-correction-mop-water-level-01` | passed | |
| 10 | `rsv1-dev-correction-mop-water-level-02` | uncertain | [个人审核原文已从公开仓库移除] |
| 11 | `rsv1-dev-direct-base-power-01` | passed | |
| 12 | `rsv1-dev-direct-base-power-02` | passed | |
| 13 | `rsv1-dev-direct-mop-no-water-01` | passed | |
| 14 | `rsv1-dev-direct-mop-no-water-02` | passed | |
| 15 | `rsv1-dev-direct-recharge-triage-01` | uncertain | [个人审核原文已从公开仓库移除] |
| 16 | `rsv1-dev-direct-recharge-triage-02` | passed | |
| 17 | `rsv1-dev-direct-suction-decline-01` | passed | |
| 18 | `rsv1-dev-direct-suction-decline-02` | passed | |
| 19 | `rsv1-dev-fault-process-restart-01` | passed | [个人审核原文已从公开仓库移除] |
| 20 | `rsv1-dev-fault-process-restart-02` | passed | |
| 21 | `rsv1-dev-multi-recharge-closure-01` | failed | [个人审核原文已从公开仓库移除] |
| 22 | `rsv1-dev-multi-recharge-closure-02` | passed | |
| 23 | `rsv1-dev-multi-recharge-correction-01` | passed | |
| 24 | `rsv1-dev-multi-recharge-correction-02` | passed | |
| 25 | `rsv1-dev-multi-recharge-negation-01` | passed | |
| 26 | `rsv1-dev-multi-recharge-negation-02` | passed | |
| 27 | `rsv1-dev-multi-recharge-tried-advance-01` | uncertain | [个人审核原文已从公开仓库移除] |
| 28 | `rsv1-dev-multi-recharge-tried-advance-02` | passed | |
| 29 | `rsv1-dev-multi-topic-switch-mop-01` | passed | |
| 30 | `rsv1-dev-multi-topic-switch-mop-02` | uncertain | [个人审核原文已从公开仓库移除] |
| 31 | `rsv1-dev-negation-filter-not-cleaned-01` | passed | |
| 32 | `rsv1-dev-negation-filter-not-cleaned-02` | failed | [个人审核原文已从公开仓库移除] |
