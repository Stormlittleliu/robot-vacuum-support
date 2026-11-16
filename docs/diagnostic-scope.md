# 诊断范围 / 证据层级契约（oracle）

> 日期：2026-11-14（香港）
> 状态：development 纵向切片 oracle，先于本轮代码实现。不是 holdout、不是成功率，不改冻结 case/expected、旧 run、Judge、GUI、scorers、assets、8950/8955。
> 范围：回充/充电 case 内“用户观察 -> 能力范围 -> 诊断假设 -> 全局结案”的证据层级，以及 `observations` / `diagnostic_scope` 的结构、转移、幂等和消费边界。

## 1. 问题与共性机制

真实 S1 回答把“手动放回能充电”写成“问题就在触点接触/遮挡上，不用再折腾”。原文资料 `故障排除.txt` #91 明确存在“机器人无法自动回充，手动放回后可充电”的故障型态，检测方向仍是回充传感器、充电座信号等；#10/#11 也并列遮挡、传感器、红外信号、适配器、电池等多种可能。因此：

- **手动放回能充**只证明“手动放回时充电通路/触点在该局部条件下工作”，不证明自动回充恢复，也不排除遮挡、传感器、信号、供电、电池等其它原因。
- 结论必须按证据层级限定：观察到什么 -> 覆盖哪个能力 -> 哪些原因只是假设 -> 当前任务目标是否真正结案。
- 这不是要求回答一律模糊。有证据的明确判断要继续明确；只是不能把局部观察扩大成全局事实。

## 2. 证据层级（权威定义）

| 层 | 内容 | 可以由什么建立 | 不能推出 |
|---|---|---|---|
| L1 观察 | 用户直接观察到的能力结果，如“手动放回能充”“自动回充仍失败” | 当前用户原文 quote + 当前轮 message/turn；可选语义 proposal，经程序校验 | 不能把假设、疑问、引用他人、助手建议当成观察 |
| L2 能力范围 | 该观察覆盖 `manual_charging` / `automatic_return` / `dock_power` 中哪一个能力 | 程序按 capability enum + quote 作用域校验 | 一个能力的 positive 不能自动覆盖另一个能力 |
| L3 诊断假设 | 触点、遮挡/路径、回充传感器、充电座信号/供电、电池等候选原因 | 由失败能力派生，状态一律 `hypothesis` | 不是 root cause 事实；不得写成“就是/已经解决/唯一原因” |
| L4 全局结案 | 当前任务目标能力明确成功 | 当前任务目标能力有有效 `works` 观察 | 局部能力 positive、弱正向、全局 `closure_signal` 都不能单独结案 |

`root_cause.status` 默认且在本切片中保持 `unconfirmed`；候选原因只以 `hypothesis` 展示，不写成事实。

## 3. 能力与观察结构

能力 enum（保持小集合，不扩成故障词典）：

- `manual_charging`：手动把机器人放回/放到充电座后能否充电。
- `automatic_return`：机器人能否自动回充/自己回到充电座并对接充电。
- `dock_power`：充电座供电/指示灯等供电通路观察。

每条 observation（存于 `case.observations`）：

```json
{
  "observation_id": "obs-...",
  "capability": "manual_charging|automatic_return|dock_power",
  "result": "works|fails|unknown",
  "polarity": "positive|negative|unknown",
  "quote": "当前用户原文中的子串",
  "message_id": "m2",
  "turn": 2,
  "source": "user_message",
  "fact_status": "effective|retracted|superseded",
  "recorded_at": 0,
  "updated_at": 0,
  "superseded_turn": null,
  "retracted_turn": null,
  "retraction_reason": null
}
```

- `polarity`：`works -> positive`、`fails -> negative`、`unknown -> unknown`，只是结果的可读派生，不新增第二个权威维度。
- 同一 capability 最多一条 `effective` observation；新观察替代旧观察时，旧行标记 `superseded`。
- 用户撤回观察时旧行标记 `retracted`，保留原文/轮次/原因和 `observation_audit`；不得物理删除。
- `observation_audit` 记录 `record` / `supersede_observation` / `retract_observation` / `reject_observation`，按 `message_id + capability + quote + kind` 去重。
- 持久事实/审计不截断：`observations` 与 `observation_audit` 跨回合完整保留；只有 prompt 投影使用合理摘要，不静默丢弃已发生历史。

## 4. `diagnostic_scope`（由程序从 case 事实派生）

每次 `decision_snapshot` 动态派生，不把候选原因无限追加到 case：

```json
{
  "schema_version": "diagnostic-scope/v1",
  "scope_id": "dscope-...",
  "task_goal": "manual_charging|automatic_return|dock_power|unknown",
  "goal_quote": "用户最初/当前任务目标原文",
  "task_goal_status": "works|fails|unknown",
  "verified_capabilities": [
    {"capability": "...", "result": "...", "observation_id": "..."}
  ],
  "unverified_capabilities": ["..."],
  "allowed_conclusion_strength": "insufficient|scoped_positive_only|task_goal_failed|resolved",
  "root_cause": {
    "status": "unconfirmed",
    "candidates": [{"id": "...", "status": "hypothesis", "reason": "..."}]
  },
  "still_needed": ["..."],
  "resolved": false,
  "source_observation_ids": ["..."]
}
```

派生规则：

1. `task_goal`：case 原文/症状明确自动回充/自己回座/回不到充电座 -> `automatic_return`；明确充电座供电/指示灯 -> `dock_power`；明确只手动放回/放上充电 -> `manual_charging`；回充故障无更具体线索时默认 `automatic_return`。
2. `task_goal_status`：取当前任务目标 capability 的 effective observation；无则 `unknown`。
3. `verified_capabilities` 包含所有 effective 的 works/fails；`unverified_capabilities` 只列与当前任务相关但还没有 effective 结果的能力（至少包含未验证的任务目标能力；需要区分手动/自动时包含另一能力）。
4. `allowed_conclusion_strength`：
   - 目标能力 `works` -> `resolved`；
   - 目标能力 `fails` -> `task_goal_failed`；
   - 目标未定但存在非目标 positive -> `scoped_positive_only`；
   - 其余 -> `insufficient`。
5. `root_cause.candidates` 只由失败能力派生（如自动回充失败 -> 回充传感器/充电座信号/遮挡路径；手动充电失败 -> 触点/供电/电池），全部 `hypothesis`。无失败则空。
6. `still_needed` 给出与缺口对应的鉴别观察；不替代模型回答，也不固定成逐 case 文案。
   - 目标已 `resolved`：必需 `still_needed=[]`，不得再强制手动/自动复测。
   - 目标未 resolved：已知 `manual_charging` works/fails 不能在原地问“现在能正常充电吗”重复同测量；优先当前目标未验证的 `automatic_return` 观察，或未知的 `dock_power` 供电观察。
   - 两项能力都已知失败时，不再重复手动测试；改用未知供电观察或明确的新干预后复测。
   - 明确新干预（如刚清触点/清障/换座）后的复测可以保留，但 next_step/question 必须带 `after_new_intervention` / `retest_after_new_intervention` 依据，不能把旧事实当未知。

## 5. 观察写入与校验

### 5.1 语义 proposal（可选、向后兼容）

`turn-interpretation/v1` 增加可选字段（旧 proposal 缺字段照常工作）：

```json
{
  "capability_observations": [
    {
      "capability": "manual_charging|automatic_return|dock_power",
      "result": "works|fails|unknown",
      "evidence_quote": "当前用户原文子串",
      "source": "user_message"
    }
  ],
  "observation_retractions": [
    {
      "capability": "manual_charging|automatic_return|dock_power",
      "evidence_quote": "当前用户原文子串"
    }
  ]
}
```

模型只做这一层聚焦语义提取；程序负责 enum、quote 来源、作用域、替代/撤回、幂等和有界。

**Canonical / 幂等：** 规范化后的 observation 同时输出 `evidence_quote` 与 `quote`（同值）；再次把已规范化对象送入 `normalize_interpretation` 必须无 error、无丢失。输入只带 `quote` 时按同一协议接受；若两者同时存在且不相等，拒绝（`conflicting_evidence_quote_and_quote`）。不允许用助手文本、其它轮或猜测补 quote。撤回字段同样处理。

### 5.2 程序校验（拒绝优先，不猜）

一条 proposal 观察被接受需同时满足：

1. `capability` / `result` / `source` 在 enum 内，`source=user_message`；
2. `evidence_quote` 非空且是**当前轮**用户原文子串；
3. quote 中能找到对应能力的明确线索：`automatic_return` 需自动/自己回/自己找/从房间等；`manual_charging` 需手动/放回/放上/放到等；`dock_power` 需充电座供电/指示灯等；
4. 该能力线索所在子句不能是条件/假设/疑问（如果、要是、假如、是否、能不能、是不是、吗 等），也不能是引用他人/资料（别人说、客服说、网上说、说明书说、听说、据说 等）；
5. 手动放回/放到充电座的 quote 不得被归成 `automatic_return=works`；自动回充 quote 不得被归成 `manual_charging=works`；
6. 结果子句中出现正负冲突且无法按最后明确结果判定时，保守为 `unknown` 并记录 ambiguity，不硬写 works。

不满足的条目丢弃并写入 validation error；quote 不在当轮、未知能力、条件句 works、手动原文标自动成功都不得进入 state。

### 5.3 确定性 fallback

语义缺失/失败时，只为稳定显式短语提供 fallback（不是理解主层）：按子句找能力线索 + 明确结果；条件句、引用他人、纯设置/感谢、仅“擦了触点/清了障”不产生能力事实。fallback 与语义观察按 capability 合并；同一轮同能力结果冲突时保守为 `unknown` 并记录。

## 6. 结案门（resolution gate）

1. 当前任务目标为 `automatic_return` 时，只有当前轮或有效自动回充 `works` 观察足以结案。
2. 只有 `manual_charging=works`、自动回充未验证/失败时：case 必须保持 `active`，`resolved_at=null`，`closure_evidence.level=partial_success` 或 `scoped_success_not_global`；不得写 `resolved`。
3. 语义 `closure_signal=explicit_success` 不能越过能力范围门；若本轮只有手动充电 positive，仍按局部成功处理，并记录作用域原因。
4. 用户明确“自动找到座并开始充电/能自己回充了” -> `automatic_return=works`，可以正常 `resolved`。
5. 局部的 `reseat_dock` / 充电 action outcome 与 case 结果独立：动作可以不写成全局 success；不得把上一条助手建议写成用户已做。

## 7. Prompt context 职责

`prompt_context` 从同一个 `decision_snapshot.diagnostic_scope` 动态生成，不写进 system prompt，不做逐 case 原话黑名单：

- 当前任务目标；
- 已观察到的能力（每条带 turn/quote/observation_id）；
- 仍未验证的能力；
- 允许的结论强度；
- root cause=unconfirmed + 候选假设；
- 合适的下一鉴别观察；
- 通用证据原则：局部能力 positive 不能扩大为全局恢复或排他原因；有证据的明确判断仍要明确。

不替换、不 regex 删除模型回答中的“说明/证明/就是”；回答因果质量由人工/Judge 评价，程序 checks 只查状态/数据契约。

## 8. 撤回、更正、幂等、旧 schema

- 同 capability 新 observation：旧 effective 标 `superseded`，写审计；不物理删除。
- 明确撤回（“说错了/收回/更正” + 否定该能力/该观察）且无替代观察：旧 effective 标 `retracted`，写审计；当前 scope 不再把旧 positive 当事实。
- 同 message 重放：`apply_user_turn` 在入口返回 `duplicate`，observation/audit 不变；load_state 重载不重复写、不复活 retracted/superseded。
- 旧 v1/v2 state：`load_state` 补 `observations=[]`、`observation_audit=[]`；原有 tried/not_tried/success 语义不改；旧 success 行保留 provenance。
- 纯设置/感谢/条件句/引用他人不产生 observation，也不改变 resolved。

## 9. 验证矩阵（同族 + 反例）

离线（`tests/test_diagnostic_scope.py`）：

1. 手动可充 + 自动失败（plain）-> manual works + auto fails，case active，scope `task_goal_failed`，root cause unconfirmed。
2. 手动可充 + 自动失败（诱导式“是不是证明…不用管”）-> 同上；条件句不生成 works，scope 不 resolved。
3. 手动也失败 -> manual fails + auto fails，active。
4. 清障后手动可充但自动未测 -> manual works，auto unverified，active，不能断言遮挡原因。
5. 只擦触点/清障完成 -> 无 capability works，scope 不 resolved。
6. 明确自动找到座并开始充电 -> auto works，可 resolved。
7. 假设/疑问/引用他人（别人说/客服说/如果/是不是）-> 不产生 works 事实。
8. 手动观察后撤回/更正 -> 旧行 retracted 或 superseded + audit，当前 scope 不把旧 positive 当事实；恢复幂等。
9. 同 message 幂等 -> observation/audit/state 不重复。
10. 旧 v1/v2 load -> 补字段，旧事实保留。
11. 语义伪造 quote/未知能力/手动原文标自动成功 -> 拒绝并记录 validation。
12. 先前 first-turn / 否定更正 / query / cache / question-lifecycle 回归。

真实 smoke（`scripts/diagnostic_scope_smoke.py`，隔离 8949，4 sessions / 5 turns）：

- `ds-live-manual-works-auto-fails-01`：U1 手动能充 + 自动仍失败 + 诱导“是不是证明触点问题已解决、不用管”。
- `ds-live-clearance-manual-only-01`：U1 清障 + 手动能充 + 自动未测 + “能确定就是遮挡吗”。
- `ds-live-manual-and-auto-fail-01`：U1 自动回不到 + 手动也充不上 + “该先确认什么”。
- `ds-live-auto-recovered-01`：U1 自动回充失败；U2 明确“自己找到充电座并开始充电”，应 resolved。

程序 checks 只查 observation/scope/status/snapshot/trace/幂等/端口等结构；回答因果质量未评分，交人工/Judge。v2 冻结输入在 `outputs/diagnostic-scope-smoke-v2/input-freeze.json`，先于模型调用；v1 目录只作历史保留并在 `attribution-correction.md` 更正归因。新 artifact 不覆盖旧产物。

## 10. 边界

- 本轮不改冻结 case/expected、旧 run、Judge 核心、旧 review 资料、GUI、scorers、assets，不操作 8950/8955。
- 不新增 case 专属 system prompt 补丁，不用大量 keyword 作为理解主层；fallback 只覆盖稳定显式短语。
- 真实 smoke 是 4 个 development case 的机制验证，不是 holdout、不是成功率；人工/Judge 未完成前不写语义通过。
- 若真实模型仍过度因果，记录原文并保留 oracle，不通过改 oracle 变绿。

## 11. 收尾修复与 v2 真实验收（2026-11-14）

- **canonical / 幂等：** `normalize_interpretation` 输出同时带 `evidence_quote` 与 `quote`；已规范化对象再次输入不再丢观察。只带 `quote` 的输入按同一协议接受；两者冲突拒绝。撤回字段同样处理。新增 `interpret_turn(fake payload) -> state.apply_user_turn` 集成测试，语义观察实际被接纳；条件句/引用他人/越轮 quote 仍拒绝。
- **持久事实不截断：** 删除 observation/audit 的 24/80 持久截断；只有 prompt 投影做摘要。测试覆盖 45 条 observation、>80 条 audit 跨回合保留、reload/duplicate 幂等。
- **next_step / question 一致：** 已知 `manual_charging` works/fails 不再原样重复“现在能正常充电吗”；目标未验证时指向 `automatic_return` 观察，两项都失败时指向 `dock_power` 供电观察；resolved 时 `still_needed=[]`；明确新干预后的复测带 `after_new_intervention` / `retest_after_new_intervention` 依据。新增 `capability_observation:<capability>` 问题 slot，已知观察会关闭旧 `step_outcome` slot。
- **v2 真实 smoke：** `outputs/diagnostic-scope-smoke-v2/run.json`，4 sessions / 5 turns，8949，冻结先于调用；19/19 结构 checks，3/3 semantic contract checks，7 条有效观察全部 `semantic` accepted、0 fallback、0 `missing_evidence_quote`。回答因果质量仍未评分；S3 仍有“重新对准结果还不清楚”的候选措辞问题，记 manager observation，不写人审结论。
- **v1 保留/更正：** `outputs/diagnostic-scope-smoke-v1/` 的 freeze/run 不重写；`attribution-correction.md` 撤回“真实模型缺 quote”的归因，记录内部 canonical 非幂等足以解释丢失，raw provider 未存故不能断定当时字段责任。
