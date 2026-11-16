# 扫地机器人客服评测案例 v1：生成、审计与冻结说明

> 生成日期：2026-11-13（香港）
> 数据集：`eval/datasets/v1/`（revision **v1.1**）
> 状态：**已生成、结构/引用/hash/分布校验通过；人工语义复核 `pending_human`**
> 本轮没有运行 Agent 模型、没有启动产品服务、没有使用真实用户数据，也不声明成功率。

## 1. 范围与产物

本轮只写以下范围：

- `eval/datasets/v1/**`：48 条新案例、suite/split/evidence/audit、人工审核待办与模板。
- `eval/generation/**`：可复用的 source 索引、证据/前提、家族 seed、构建、校验、旧集审计脚本。
- `docs/case-generation.md`：本说明。
- `outputs/case-generation-report.json`：构建报告（source references、seed provenance、冻结 hash、限制）。

未改：`assets/knowledge`、`assets/models`、`assets/rag_index`、旧 `eval/cases/v0/recharge-start.json`、eval runner/scorer/tests/runtime、`plans/LONG_TERM_PLAN.md`、主仓库、DSH。旧 v0 文件 hash 为 `32182e0b29bd7f8d4c1caaca8278c59ea8daf502c2f3506fc2ab0841ab2cdfaa`，本轮只读。

## 2. 先证据/前提，再问句和多轮

构建顺序固定在 `eval/generation/build_v1.py`：

1. 只读读取 6 份知识文件和 README；TXT 按 UTF-8，PDF 用 `pdftotext -enc UTF-8 -layout` 抽取，并把文件 sha256 写入索引。
2. 生成 `evidence_index.json`：documents + premises + coverage_checks + evidence；**所有 quote 先在源文件中校验**，失败即停止，不会先生成问句再补证据。
3. 只有在 evidence index 通过后，才展开 24 个 family × 2 个变体，共 48 条。
4. 写出 `suite.json` / `cases.jsonl` / `split.json` / 审核待办，最后运行 `validate_v1.py`，把校验结果写入 `audit.json` 和报告。

`kind` 严格区分：

- `knowledge`：6 份知识资料中的事实锚点。
- `requirement`：README 中的产品/能力/评测要求，**不是知识事实**，不能拿来证明“资料里有什么”。

## 3. Source snapshot（只读）

| 文件 | kind | 抽取 | sha256 |
| --- | --- | --- | --- |
| `assets/knowledge/故障排除.txt` | knowledge | UTF-8 | `a544e28e1b42ef4d5aca379e291298bc14dceb3dc71569824d4bb701eda7d68f` |
| `assets/knowledge/扫地机器人100问.pdf` | knowledge | pdftotext -layout | `f9ee0fea506436d8ea4a5ac6e1c272191023f41e95b677842e527f57745b4eb8` |
| `assets/knowledge/扫地机器人100问2.txt` | knowledge | UTF-8 | `bf4f52b30736c1432a2577009be7cef82b155bd3605cb9cd9c4fb14373514bfd` |
| `assets/knowledge/扫拖一体机器人100问.txt` | knowledge | UTF-8 | `6500ad32130a8068df6d04b6427c5ea7ffcea54ba38fbb8274be770f7965f54a` |
| `assets/knowledge/维护保养.txt` | knowledge | UTF-8 | `8aa7db7fd4050b1faa8b18f328ad550176f63c495d50563565818b3b6e43f4e7` |
| `assets/knowledge/选购指南.txt` | knowledge | UTF-8 | `08d3958c9fc24235905cf53a594bbe9041331761f2e93a309fbea4b7d190f9db` |
| `README.md` | requirement | UTF-8 | `f3fc1eada15a3e2fc21431ea6fe5a4fd9fae92c923155f5dd6cfc5c9e6f0e87d` |

校验口径：

- 先查逐字子串；PDF 因换行/空白差异允许 whitespace-normalized 命中，但会记录状态。
- 252 条 case-level evidence 引用中，250 条 exact、2 条 normalized；normalized 的两条是 README 同一行换行后的 `req-no-actions`，不是知识事实。
- **quote 存在 ≠ 语义支持**。`validate_v1.py` 只证明文件/位置/hash；引用是否真的支持结论仍由人工审核的 `source_semantic_support` 判定。

### 3.1 90%/95% 不是真冲突

旧 `rs-11` 的两条引文都真实，但：

- `选购指南.txt` #36：充电座设计，“支持自动回充精准度≥95%”。
- `选购指南.txt` #84：“在复杂环境下回充精准度≥90%”。

两者条件/语境不同，不是同对象、同条件、同时间的矛盾。v1 **不把它写成“资料冲突/必须二选一”**，而是用 `rsv1-dev-ambiguity-recharge-condition-02` 要求分别说明出处和条件，不合并成唯一数值。旧 16 条不改，问题记录在 `audit.json`。

### 3.2 未知型号价格：先查语料覆盖

`coverage-x99` 对 6 份知识 + README 做只读全文扫描，`X99/Galaxy` 命中 0。因此 v1 的结论是“**本语料没有 X99 型号专属价格/保修信息**”，不是“现实中不存在”，也不是“一次 top-k 没命中所以不存在”。价格可用 `选购指南` 的泛化区间和 PDF 的 Avva 30 历史例子，但明确不能套成 X99 售价；保修只给一般 1–2 年/电池 6 个月的说明，并要求以说明书/发票/APP/官方为准。

## 4. 旧 16 条逐条审计（只读，不改基线）

证据：`eval/datasets/v1/audit.json` → `legacy_v0`。

结果：

- 16 条、50 处 sources；**50/50 可定位，0 missing**。PDF 引文用 `pdftotext -layout` 核对。
- 但“引文准确”不等于“expected 设计正确”。发现 3 个 material issues：
  1. `rs-03-denies-not-tried`：behavior 写“把否定信息作为已试状态的一部分”，与用户“都没试过”矛盾；正确语义是 `not_tried`，不能记成 tried。v1 用 `fam-dev-multi-recharge-negation` 修正。
  2. `rs-09-unknown-model-price`：README 被当作“资料里没有价格”的知识证据；README 实际是 requirement，旧集也没有全文覆盖检查。v1 用 `fam-dev-boundary-unknown-model-price` + `coverage-x99` 修正。
  3. `rs-11-conflict-recharge-accuracy`：把 95%/90% 标成真冲突；条件不同。v1 用条件/出处说明，不要求二选一。
- 其它设计问题：清障被写成部分案例的固定首步；`rs-06` 用“暂停/未解决”这种不稳定状态词；`rs-12` 的 fault_injection 在顶层、没有分母标记；`rs-13` setup 是字符串数组；`rs-04` 把追问示例写得过细；`rs-15` 的“非原装电源线”未把维护保养耗材 #28 列入 sources。
- 这些都不影响旧文件作为开发基线；旧 16 条继续只作 development，不迁 holdout。

## 5. v1 数据契约与分布

`suite.json`：

```json
{
  "schema_version": "robot-support-eval/v1",
  "suite_id": "robot-support-v1",
  "cases_file": "cases.jsonl",
  "split_file": "split.json",
  "counts": {"development": 32, "holdout": 16},
  "review_status": "pending_human"
}
```

`cases.jsonl` 每行一条 case，包含约定的 `id/family_id/category/split/origin/review/turns/evidence/setup/scoring`。

### 5.1 State 范围（v1.1 更正）

P1 只为 **recharge-failure** 维护结构化状态。v1.1 审查后：

- 回充/充电状态 case 保留 `state`：`case_status` 只用 `active` / `resolved`；`tried_actions` 只记录用户明确说做过的动作，`not_tried_actions` 只记录用户明确说**没做**的动作。沉默、助手建议、未提及都算 unknown，不写事实。
- 拖地、吸力、边刷、价格/保修、域外、安全、开荒等 QA case：`state={}`，标 `state_scope="planned_out_of_scope_current_p1"`、`scoring.state_score="not_applicable_current_p1"`；QA rubric 原样保留，不纳入当前 state score，也不要求 runtime 立即实现这些状态。
- 主题切换（回充→拖地/吸力）保留**当前回充 state**，不新建 mop/suction 结构化状态。
- `state` 不写唯一 `next_action`；不为迁就当前 regex 猜答案，也不强制固定 action 顺序。
- 回充动作 ID 使用当前稳定公共语义（如 `clear_dock_obstacles`、`clean_contacts`）；非回充状态已移除，因此不再把 `check_water_tank` 等当作 runtime 已实现动作。
- 合法回充状态缺陷没有被删除：M1/M2/M3/M4/F1 等仍保留“已试推进、未试否定、更正、假结案、重启恢复”的实质要求。

`required_claims` / `forbidden_claims` 是语义 rubric，不是正则真值。

`expected_sources` 语义（v1.1 更正）：

- 它是**可接受/最小证据覆盖候选**，不是“所有 candidate 都必须检索到并引用”。
- 每个 case 的 `scoring.expected_sources_mode="any_of"`；单一充分来源即可，引用存在性与语义支持另评。
- 候选文件经过出处复核；不要求模型引用某一份 PDF，也不把“候选全部出现”当通过条件。

分布：

| 类别 | development | holdout | 合计 |
| --- | ---: | ---: | ---: |
| `direct_qa` | 8 | 4 | 12 |
| `multi_turn` | 10 | 6 | 16 |
| `missing_or_out_of_domain` | 6 | 2 | 8 |
| `negation_correction_ambiguity` | 6 | 2 | 8 |
| `fault_recovery` | 2 | 2 | 4 |
| **合计** | **32** | **16** | **48** |

- 24 个 family，**每个 family 恰好 2 个变体**，family 与 split 严格绑定，不跨集。
- QA 分母 44；4 条 fault injection 单列，不混 QA 成功率。
- 回充多轮为主：16 条 multi-turn 中回充/充电相关 10 条；其它主题按自身状态语义，不强迫建回充 case。
- 覆盖 6 份文档的不同 topic：回充、充电安全、拖地水路、吸力/滤网、主刷/边刷、保修/价格、装修开荒、域外/账号边界等。

### 5.1 24 个 family

| family | category | split | 情境 + oracle |
| --- | --- | --- | --- |
| `fam-dev-direct-recharge-triage` | direct_qa | dev | 回充多分支低风险排查；清障不是唯一首步 |
| `fam-dev-direct-mop-no-water` | direct_qa | dev | 拖地不出水；水箱/水量/出水口优先，不判水泵 |
| `fam-dev-direct-suction-decline` | direct_qa | dev | 吸力下降；滤网/风道/主刷优先，不判电机 |
| `fam-dev-direct-base-power` | direct_qa | dev | 充电座灯不亮；供电链路，不拆座 |
| `fam-dev-multi-recharge-tried-advance` | multi_turn | dev | 已清障/擦触点仍失败；推进不重复 |
| `fam-dev-multi-recharge-negation` | multi_turn | dev | 明确未试；不能记成已试 |
| `fam-dev-multi-recharge-correction` | multi_turn | dev | 用户更正；最后一次明确陈述为准 |
| `fam-dev-multi-recharge-closure` | multi_turn | dev | 成功结案 vs 手动能充假结案 |
| `fam-dev-multi-topic-switch-mop` | multi_turn | dev | 回充 → 拖地；来源不串、旧任务保留 |
| `fam-dev-negation-filter-not-cleaned` | neg/amb | dev | 滤网“没清”被误当已清；对象区分 |
| `fam-dev-correction-mop-water-level` | neg/amb | dev | 水箱满/装好前提被更正 |
| `fam-dev-ambiguity-recharge-condition` | neg/amb | dev | 指代/症状不清；90%/95% 条件差异 |
| `fam-dev-boundary-unknown-model-price` | boundary | dev | X99 价格/保修；语料覆盖而非 top-k |
| `fam-dev-boundary-out-of-domain` | boundary | dev | 服装/账号订单；能力边界 |
| `fam-dev-boundary-missing-context` | boundary | dev | 开不了机/突然不动；先追问 |
| `fam-dev-fault-process-restart` | fault | dev | 进程重启；同 session 恢复、跨 session 隔离 |
| `fam-hold-direct-charge-heat` | direct_qa | holdout | 充电座过热/焦味；安全优先 |
| `fam-hold-direct-edge-brush-wear` | direct_qa | holdout | 边刷磨损判据；2-3 月 vs 6-12 月差异 |
| `fam-hold-multi-base-moved-remap` | multi_turn | holdout | 搬家/挪座；摆放、地图、传感器 |
| `fam-hold-multi-switch-to-suction` | multi_turn | holdout | 回充 → 吸力；来源不串 |
| `fam-hold-multi-docking-alignment` | multi_turn | holdout | 能到座前但对不准/碰撞 |
| `fam-hold-boundary-renovation-debris` | boundary | holdout | 装修开荒；先人工清沙粒/硬物 |
| `fam-hold-negation-battery-vs-base` | neg/amb | holdout | 电池/充电座换错对象；更正覆盖 |
| `fam-hold-fault-empty-retrieval` | fault | holdout | 空检索；空≠资料不存在，可重试 |

### 5.2 Revision v1.1 数据审查更正

本轮只改数据 oracle，不改产品/runtime/scorer/tests/plans，不运行 holdout。`suite.json` 与 `audit.json` 保留 revision history：

| revision | 状态 | cases hash | holdout hash |
| --- | --- | --- | --- |
| v1.0 | superseded | `e766dda85854da9f90ffa24889139b40669b8eec04baf5531015330b87a2bf4f` | `558918e7dca2c5854428b18c6add6315a30c585b79419a1e13099ea8124f269e` |
| v1.1 | current | `f6972782c478fa799453fd83058533aca7efe892d3507c6abe63f2ee8402bd94` | `9aa281ffe0b0f4c3ef0ef63b9c81de8727f3ea4dbf25371f540def9511a12db1` |

更正原因（`state_policy.py`，共 67 条修正记录）：

- **22 条非回充/单轮 QA 的强制 state 被删除**：mop、suction、边刷、价格/保修、域外/账号、开荒、充电安全、mop 空检索等。改为 `state={}` + `state_score=not_applicable_current_p1`，QA rubric 保留。
- **38 条 `not_tried_actions` 删除**：原先把“未提及/建议过/未推荐”的步骤写成未试。现在只保留用户明确否定：M2 的“都没试过/还没清障”、M3 的“没擦/没换座”、H7 的“没换电池/没换座”（共 6 条 case 仍有 not_tried）。
- **7 条 state override**：主题切换保留当前回充 state；删除仅由助手建议或观察推断出的 tried action（如手动放回、挪座、擦传感器）。
- `expected_sources_mode=any_of` 全量补齐；不再把候选文件当必引用清单。

旧 hash 进 `audit.json` / 报告；新 hash 冻结。**没有根据 holdout 跑分或失败调题**，因为 holdout 未执行；这些是初版资料审查阶段的同类 oracle 修正。

## 6. 生成/校验脚本（轻量、可复用、无模型 API 依赖）

```powershell
C:/Users/28980/anaconda3/envs/langchain/python.exe eval/generation/build_v1.py --seed 20261113
C:/Users/28980/anaconda3/envs/langchain/python.exe eval/generation/validate_v1.py
```

脚本职责：

- `source_index.py`：只读 source、PDF 抽取、sha256、quote exact/normalized 检查。
- `evidence_data.py`：证据、前提、覆盖检查的清单；是“先证据”的单一来源。
- `families_dev.py` / `families_holdout.py`：手写 family + 2 变体；每族声明 `variant_budget: 2`。
- `state_policy.py`：v1.1 oracle 更正（非回充 state 移除、not_tried 显式否定、主题切换保留回充 state），并把 67 条修正写入 audit。
- `build_v1.py`：先校验证据，再展开；`--seed` 仅用于**可复现的 case 排序**，不做随机同义替换；不调用任何 LLM/API。
- `validate_v1.py`：目录/JSONL/unique ID/32+16/类别分布/family 不跨集/每族 2 条/normalized 重复/2-gram 近似重复/source hash/quote/injection 标签/预期非空/审核 pending。
- `audit_v0.py`：只读旧 16 条并计算 50 处引文命中；设计问题为人工记录，不伪装成自动语义判断。

**哪些是自动、哪些是人工/Agent 编写**：

- 自动：结构、计数、hash、quote 位置、family/split、重复、fault 标签、review pending。
- 手写（本次 DeepSeek executor 依据证据前提）：所有问法、多轮、`required_claims`、`forbidden_claims`、状态语义、行为要求、来源期望。
- 未自动：语义 claim 支持、引用支持、状态是否真的正确、是否安全；这些在 `scoring.human` 和审核待办中，状态仍 pending。脚本没有“自动生成全部 rubric”，也没有把随机同义替换当高质量生成。

## 7. Holdout 冻结

- `split.json` / `suite.json` 记录（v1.1）：
  - holdout cases hash：`9aa281ffe0b0f4c3ef0ef63b9c81de8727f3ea4dbf25371f540def9511a12db1`
  - 全量 cases hash：`f6972782c478fa799453fd83058533aca7efe892d3507c6abe63f2ee8402bd94`
  - 旧 v1.0 hash 保留在 `suite.json.audit_history`、`audit.json.oracle_corrections` 和报告中，不覆盖历史。
- 所有 holdout 的 `origin.seed_ids` 为空；没有把旧 16 条或其改写放进 holdout。
- 本轮**只做 schema/证据完整性检查和 hash 冻结，不运行 holdout，也不据 holdout 调参**。
- 如果未来有人在开发中看了 holdout 答案/失败并据此调产品，必须把该场景迁回 development，并补新的 holdout 场景；不能继续用已暴露的 holdout 报成绩。

## 8. 人工审核（未完成）

- `review-todo.csv`：48 行，全部 `pending_human`；字段包含 claims/forbidden/`state_scope`/`state_score`/source_semantic_support/overall。
- `review-template.md`：双人独立复核、分歧裁决、证据要求。
- **`clauses` 没有该维度时填 `N/A`，不要填 `pass`**；N/A 不计入通过率分子/分母。
- 来源存在性可自动查；来源是否真的支持结论、状态是否更新正确、是否越界安全，必须人工判断。
- 当前只能说“案例已生成并通过结构校验”，不能说“人工审核通过”“真实用户验证”“全量成功率”。

## 9. 后续加入真实失败（方法）

1. 只使用已授权、去标识、可复查的真实失败记录；没有就写“未具备”，不编。
2. 先把失败整理成“情境 + oracle + 证据前提”，放入新的 development family；不要直接改冻结 holdout。
3. 若失败来自线上/真实会话，新增 case 的 `origin.seed_ids` 记录来源 ID；`review.status` 仍从 `pending_human` 开始。
4. 先补证据 quote/hash 和语义 rubric，再改问法；不要用一次 top-k 未命中当“资料不存在”。
5. 如果失败暴露了 holdout 场景，按第 7 节迁移并补新 holdout。
6. 重新运行 `build_v1.py` → `validate_v1.py`；更新 `audit.json` 和报告，不手改 hash。

## 10. 已知限制

- 48 条全部是合成案例，不是真实用户样本；不宣称统计显著、覆盖率或成功率。
- 语义 rubric 是手写的，仍可能过严/过松；自动校验不能替代人工。
- v1.1 删除了非回充主题的强制结构化 state；这不代表这些 QA 不需要状态，而是它们不在 P1 本月 recharge-failure state 范围。回充状态缺陷仍保留，不因 case failed 而删除。
- `not_tried_actions` 只在用户明确否定时出现；沉默/未提及不写事实，因此不能把“没有 not_tried”当成“用户已做”。
- `expected_sources` 是 `any_of` 候选；通过不代表所有候选都被检索或引用，引用存在也不等于语义支持。
- 本轮没有运行 Agent 模型/产品服务，因此没有回答质量、延迟、token、引用支持度的实测结果。
- PDF quote 依赖 `pdftotext -layout`；换工具可能有空白差异，已用 normalized 兜底并记录，但不能保证跨工具逐字一致。
- `README.md` 是 requirement，不是知识证据；域外/能力边界结论不证明模型的一般知识能力。
- 6 份资料是通用文档，不是品牌官方授权售后；价格/保修只能作一般说明。
- X99 的“无资料”是语料全文覆盖结论，不是现实世界不存在，也不是检索 top-k 未命中的证明。
- 边刷周期等资料存在跨文档差异，v1 作为条件/范围差异处理，未强行合并为唯一值。
- 故障注入只定义 setup/steps 和预期，没有真实注入器运行记录；4 条 fault case 不进入 QA 分母。
- 近重复检查是 normalized exact + 2-gram Jaccard；本轮 0 个 ≥0.90 的对，但这不等于语义上完全没有重复。
- 旧 16 条仍是开发基线，未修改；其语义复核也仍是 pending，不能把本轮的 v1 构建当作旧集已审核。
