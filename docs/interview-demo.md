# 岗位 A 面试演示与录制准备（P1-07/P1-08 材料）

> 日期：2026-11-15（香港）  
> 状态：release v1 bounded 交付完成，进入维护。当前产品入口是 8902，实例 `robot-vacuum-support-release-v1`，产物 `robot_vacuum-v4-24f3a3db39`。P1 整体仍未完成，正式 40–60 集、holdout、真实用户和权威 Judge 都未做。  
> 成品回放：[outputs/delivery-closeout-v1/demo/replay.html](standalone.md#未包含)。这是录屏回放，不是实时录屏，不新增模型调用。逐字稿在 [transcript.json](standalone.md#未包含)，路径清单在 [routes.json](standalone.md#未包含)。  
> 用法：3 分钟面试口播用第 0 节。详细文字走查用第 10 节。第 1–9 节保留为 2026-11-13 的机制材料与历史证据，不是当前交付状态。  
> 当前交付状态以 [release-v1.md](release-v1.md) 和 [outputs/delivery-closeout-v1/report.json](standalone.md#未包含) 为准。

## 0. 3 分钟终版讲稿（2026-11-15）

**0:00–0:30 问题与范围。**  
“这是一个扫地机器人售后知识问答 Agent。范围是 6 份通用资料，覆盖选购、使用、保养、故障排查和回充。用户不需要懂术语，系统先检索再回答，关键事实带来源文件名，资料不足先追问，不编型号价格和设备控制。”

**0:30–1:30 三条真实路径。**  
打开 [回放](standalone.md#未包含)，依次点三条路径。  
“第一条是明确恢复，系统只按用户确认的自动回充成功结案，不顺手推断故障原因。第二条是未解决并更正，用户先说已放回充电座，随后更正为没放回。系统撤回上一轮已试事实，把动作放回未试，仍保持处理中，转人工摘要保留原话、轮次和更正审计。第三条是广度问答，没有回充故障案例，摘要走 transcript-only，不硬造设备状态。”

**1:30–2:30 架构取舍。**  
“这里没有把每个观察到的措辞补进 system prompt。动态事实放会话状态和 context，包括当前问题、已试与未试、来源作用域、结案证据。prompt 只保留稳定职责，比如先检索、带来源、资料不足先追问。转人工摘要只读，GET 和 POST 返回同一投影，不写状态，不生成外部工单。产物是独立 artifact 和 instance，每个会话有独立 worker、data/workspace 和 Chroma 副本，模型目录共享。”

**2:30–3:00 真实失败与边界。**  
“一个真实失败是 PDF 主文档检索被页级 8 个块稀释，Q50 事实答不出来。根因不是 PDF 抽取失败，而是编号条目切片只对 txt 白名单生效。修复改成通用编号结构切片加实例私有索引 coverage，PDF 从 8 块到 101 块，私有索引 823 到 916，源索引 hash 不变。另一个失败是跨项目 trace 重复，实际 9 次工具调用被记成 18 个事件，修复后另一次运行 3 次调用对应 3 个 `tool_start`，这不是 9→3 提效。当前边界是回充单流程状态，没有正式 40–60 集、holdout、真实用户量或生产 SLA，12 案里 1 条原始 `retrieval_attempted=false` 原样保留。”

**不要讲：** 成功率、用户量、满意率、9→3 提效、全部引用正确、官方授权售后、真实外部工单。

---

## 1. 先看结论

1. **演示主题不是“框架很多”，而是一条售后任务链**：自然用户回不去 → 低成本澄清 → 一步排查 → 用户已试反馈 → 换问保养/水箱 → 保留旧任务 → 礼貌结束。
2. **现有可讲的是机制与固定小样本观察**：case-scoped 问题生命周期、同一 `decision_snapshot`、actual-asked 审计、按需路由、分层计量；固定 5 轮 development 中“已回答后重复追问”从 baseline 1 次降到 after 0 次。
3. **不能讲成成绩**：没有 holdout 结果、没有成功率、没有真实用户量、没有完整恢复、没有“全部引用正确”、没有 9→3 提效。固定 5 轮只是 development 观察。
4. **录制分两条路**：无网络可演示结构（只读报告/JSON/artifact `--check`）与真实问答（显式模型配置 + 隐藏启动）。两条路都不使用一键弹窗脚本。
5. **当前服务默认未启动**：本材料不启动产品服务；最终用户验收时的启动由主模型按需操作。GUI 8950 只是人工审核工具，不是客服产品。
6. **最新计量修复产物尚未跑模型**：`robot_vacuum-v4-af34e8f764` 只做了构建/外部 cwd `run.py --check` 与离线校验；真实 after 回答属于 `robot_vacuum-v4-6a1eeea77f`。不要把 af34 说成“已用新计量代码重跑模型”。

---

## 2. 一条演示主线（用户会说什么 / 我讲技术什么）

> 这张表是**录制脚本与期望行为**，不是 actual 记录。实际录制时必须用真实运行输出或已保存的原始 run；不得把 `expected` 当 `actual`。

| 轮次 | 用户会说什么（录制台词） | 产品期望做什么 | 我讲技术什么（旁白/追问） | 证据与边界 |
|---|---|---|---|---|
| 1 | “回充失败，找不到充电座。” | 只做一次低成本二选一澄清：机器人回不去，还是人找不到座？ | “这里不是靠关键词硬判，而是 typed work context 保留歧义候选；不同解释会改变下一步，所以只问一次。” | after run turn1 实际问出二选一；不是成功率 |
| 2 | “我不太懂这些，机器人回不去了，你一步步告诉我先做什么。” | 记录 slot 已答；给一个最低成本步骤 + 观察点 | “answered 后进入 `must_not_ask`；`decision_snapshot` 同时给 prompt context 和回答生成，避免历史旧问题再次注入。” | after turn2 route=`deterministic_slot_answer`；turn1 的 referent 问题不再重复 |
| 3 | “我已经清理了充电座周围，也擦了触点，还是回不去。” | 只把用户明确说过的两步记为已试/失败；不重复建议；推进下一步 | “助手建议不等于用户已做；reducer 只接受能回到用户原文的事实。回答文本还会做 actual-asked 审计。” | after 记录 clear_dock_obstacles / clean_contacts 为 failed；下一步 clean_sensors |
| 4 | “对了，拖地时水箱漏水怎么办？” | 先回答新主题；旧回充任务保留 open，最多一次轻确认 | “换主题是 defer，不是 answered；旧任务摘要和 `reminder_allowed` 进快照，不每轮提醒。” | after 先答水箱，末尾一次轻确认；旧任务仍 open |
| 5 | “谢谢。” | 弱正向信号只礼貌收束，不宣称已解决 | “结案只认明确/可验证成功；沉默和‘谢谢’不写成 resolved。” | after turn5 route=`protocol_ack`，0 model calls；回答明确说“还没有确认解决” |

**录制时不要把这张表当 actual。** 若用 after run 的已保存回答，明确说“这是 2026-11-13 固定 5 轮 development run 的实际输出”；若现场重跑，必须用当前 artifact 和显式模型配置，并接受输出可能不同。

### 2.1 固定 5 轮实际观察（可引用）

- 对话固定为：`回充失败，找不到充电座。` → `我不太懂这些，机器人回不去了，你一步步告诉我先做什么。` → `我已经清理了充电座周围，也擦了触点，还是回不去。` → `对了，拖地时水箱漏水怎么办？` → `谢谢`。
- baseline `robot_vacuum-v4-569ecbce9a` 与 after `robot_vacuum-v4-6a1eeea77f` 同题、同 `deepseek-chat`、各 5 轮、0 error。
- 重复 referent 追问：baseline 第 1、2 轮（第 2 轮是用户已回答后的多余重复）→ after 仅第 1 轮（必要的初始二选一），**多余重复 1→0**。
- after 第 5 轮纯致谢是 `protocol_ack`：0 model calls / 0 ledger events / `usage_status=not_called`。
- after 第 2–4 轮仍有 4 个模型自行生成的观察/新症状问题，已进 `unplanned_questions` 审计，但**未成为生命周期 slot，也未正式评分**。
- 这些只对“这组 development 场景”成立；不写成功率、整体提升或 holdout 结论。详见 [docs/question-lifecycle.md](question-lifecycle.md) 与 [report](standalone.md#未包含)。

---

## 3. 3 分钟演示路线

> 目标：让面试官记住“一个真实售后问题如何被接住、如何不重复问、如何守住事实边界”，而不是记住框架名。

| 时间 | 画面/动作 | 口播要点 |
|---|---|---|
| 0:00–0:25 | 标题页或一张对话截图 | “这是扫地机器人售后知识问答 Agent。用户不是来考试，是来求助；他不会术语，也可能说不清‘找不到充电座’是谁找不到。” |
| 0:25–1:15 | 展示固定 5 轮对话（建议用已保存的真实 after 输出，不现场启动模型） | “第 1 轮只问一次二选一；第 2 轮用户答了，系统把问题标 answered，不再重复；第 3 轮只把用户明确做过的清障、擦触点记为已试，不把建议当事实；第 4 轮换问水箱，旧任务保留但只轻确认一次；第 5 轮谢谢，只礼貌收束，不宣称解决。” |
| 1:15–2:05 | 展示 `outputs/question-lifecycle-report.json` 或 `docs/question-lifecycle.md` 的 question lifecycle / decision snapshot 两段 | “实现上不是再堆提示词，而是问题状态 + 同一份决策快照 + 回答审计；回答里问没问、问错没问错，会被审计，而不是假设程序没记录就等于模型没问。” |
| 2:05–2:40 | 展示 baseline/after 的重复追问对比，再展示 usage correction | “固定 5 轮里，已回答后的多余 referent 重复从 1 次降到 0 次；第 5 轮纯致谢 0 次模型调用。但这是 development 小样本，不是成功率，也没有整体节约百分比——baseline 没有分层 meter，RAG 也拆不出来。” |
| 2:40–3:00 | 边界页 | “P1-05/P1-06 尚未完成，holdout 没跑；af34 只是计量修复后的构建，没有重跑模型；GUI 8950 是审核工具，不是客服产品。今天展示的是可复现的机制与证据，不是完成品成绩。” |

**3 分钟不要讲**：LangChain/LangGraph 细节、RAG 参数大全、9→3、用户量、满意度、硬件、完整恢复、全部引用正确。

---

## 4. 10 分钟技术追问路线

### Q1. 为什么不是改 system prompt？

可讲：先看失败机制，再选层。动态事实（歧义、已试、旧任务、当前问题）放 `context/state`，prompt 只保留稳定职责；`decision_snapshot` 是 prompt context 与回答生成的同一对象，避免“resolved 了但旧 unknowns 还注入”的重复追问。  
证据：[docs/context-diagnosis.md](context-diagnosis.md)、[docs/question-lifecycle.md](question-lifecycle.md)。  
边界：不是所有问题都解决了；4 个 unplanned questions 仍在审计中。

### Q2. 问题生命周期怎么定义？deferred 和 answered 有什么区别？

可讲：case-scoped question record，状态 `pending / answered / deferred / superseded / cancelled`；answered 只认用户原文明确回答或已确认事实满足 slot；换主题是 deferred，不伪装已答；纠正/新事件带证据重开；同一 slot 最多自动重问 2 次，达到上限显示 `blocked_unanswered`。  
证据：[docs/question-lifecycle.md](question-lifecycle.md) 第 2 节。  
边界：这是单流程 development 切片，不是完整售后任务完成。

### Q3. `decision_snapshot` 为什么重要？

可讲：它同时供 `prompt_context` 和回答生成，只给当前唯一可问问题、confirmed facts、answered/deferred scope 和旧任务摘要；动态问题文案不进 system prompt。  
边界：snapshot 不能替代语义质量评分；本轮没有正式评分。

### Q4. 怎么证明“不是程序没记录，模型就没问”？

可讲：`audit_assistant_answer` 检查回答文本，输出 `actual_asked / unexpected_asked / missed_question / unplanned_questions`；已回答/已暂缓/达到重问上限的 slot 再问会被标 unexpected；当前问题没被回答会标 missed。  
证据：after run 的 `last_question_audit`。  
边界：审计是规则/文本层，不是语义 judge；4 个 unplanned 问题仍未进生命周期。

### Q5. 成本和路由怎么算？为什么不讲整体节约？

可讲：路由优先级 `replay_cache → protocol_ack → deterministic_slot_answer → semantic`；计量按层 `interpretation / main_agent / rag_summary` 按 `call_id` 计一次；主回答聚合行可以代表 2 个 model requests，`model_calls=sum(model_requests)`，`ledger_events` 是 calls 行数，token 不按请求数乘；缺 usage 写 null，费用无权威单价写 null。  
可引用：after 修正后 `model_calls=10`、`ledger_events=7`、reported tokens `13,493`；turn5 0 calls。  
边界：baseline 没有新分层 meter，RAG 已含在旧 trace/budget 中无法拆层，所以 **`total_comparison=no_total_comparison`**；只能讲 turn2/turn5 的局部节约，不讲整体百分比。af34 未重跑模型。  
证据：[question-lifecycle-usage-correction.json](standalone.md#未包含)。

### Q6. 引用和 RAG 到什么程度？

可讲：6 份通用资料、823 条向量、混合检索、来源文件名回传、零命中明确提示；引用只做“有没有/是否在本轮检索中出现”的结构检查。  
边界：**有引用 ≠ 引用正确**；没有引用支持度评分，也没有“全部引用正确”。资料是通用资料，不是品牌官方授权售后。  
证据：[eval/README.md](../eval/README.md)、[岗位A项目边界与简历描述.md](standalone.md#5-父工作区链接的处理)。

### Q7. 32 条人工审核 26/2/4 是什么？算成功率吗？

可讲：这是 `case_definition` 的题目/ oracle / 证据审核，不是 Agent 回答评分；32 条中 30 QA + 2 fault，fault 不混 QA 分母；7 条 notes 指向工作语境、渐进诊断、歧义/未结任务、测试真实性四类机制。  
边界：**不能写 26/32 成功率**；holdout 16 条未运行；P1-05/P1-06 尚未完成；并行主任务的 approved development baseline 不拿来当本轮最终统计。  
证据：[review-round-1-analysis.md](review-round-1-analysis.md)、[review-round-1-analysis.json](standalone.md#未包含)。

### Q8. 讲一个真实失败案例。

可讲三个，分开归属：

1. **trace 重复（跨项目可观测性，不是客服性能）**：某次运行实际 9 次工具调用被记成 18 个事件；根因是适配层 call 与 spec 嵌套重复；修复后另一次运行 3 次调用对应 3 个 `tool_start` 和 3 个 context。**不是同一任务 A/B，不是 9→3 提效。**
2. **模型重复追问（客服 development）**：baseline 在用户已回答 referent 后的第 2 轮仍重复问；after 通过 answered scope + `must_not_ask` + 同一 snapshot 去掉这层重复；固定 5 轮多余重复 1→0，但仍有 4 个 unplanned 问题。
3. **用户审核驱动机制（数据→context/state）**：7 条 notes 不是 7 个措辞补丁，而是工作语境/渐进诊断/歧义与未结任务（含 closure 分级）/测试分层四类机制；这推动了 typed context、case-scoped question lifecycle、actual-asked 审计，而不是继续散点改 prompt。

### Q9. 为什么不做完整恢复、真实用户、硬件？

可讲：没有就是没有。当前没有真实试用者/用户量/满意率；没有设备控制/账号/订单工具；没有语音/多模态/硬件；没有完整故障恢复体系；没有生产多租户/SLA。P1 本月只做单流程 + 起步案例 + 证据收口，不拿未来功能当当前成果。  
证据：[岗位A项目边界与简历描述.md](standalone.md#5-父工作区链接的处理) 第 4 节。

### Q10. 部署和演示边界？

可讲：独立 artifact 有 manifest/checksums/runtime hash；外部 cwd `run.py --check` 可离线校验；真实服务用 `serve.py --instance-config` 显式实例配置；模型凭据只通过实例 `environment_file` 注入，不复制进 artifact。  
边界：当前服务默认未启动；最新 af34 没有固定现成 instance 配置；仓库里 `outputs/p1-instance/instance.json` 指向旧 v3，不能当 af34 的最新配置；workspace 主 artifact registry 不能当独立 source project 的运行依赖。  
证据：[demo-readiness-report.json](standalone.md#未包含)、[README.md](../README.md)。

---

## 5. 两条演示路线

### 5.1 无网络可演示结构（推荐录制基线）

适用：面试现场无网络、不能调用模型、只想稳定讲机制。

可展示：

- 固定 5 轮真实 after 输出：[question-lifecycle-after-run.json](standalone.md#未包含)（actual answers，不是 expected）。
- baseline 对比：[question-lifecycle-baseline-run.json](standalone.md#未包含)。
- 计量修正：[question-lifecycle-usage-correction.json](standalone.md#未包含)。
- 32 条审核分析：[review-round-1-analysis.json](standalone.md#未包含) 与 [review-round-1-analysis.md](review-round-1-analysis.md)。
- 人工审核截图：[manual-review.png](standalone.md#未包含)（只作审核工具截图，不作为客服产品界面）。
- 离线 artifact 校验与就绪检查。

命令（在现有终端执行，不弹新窗口）：

```powershell
$py = "C:/Users/28980/anaconda3/envs/langchain/python.exe"
$artifact = "E:/CODE/Myproject/agent-workspace/robot-vacuum-support-built/robot_vacuum-v4-af34e8f764"

# 只读 readiness：不启动服务、不调用模型；JSON 用于录制前检查
& $py scripts/demo_readiness.py --artifact $artifact --mode offline `
  --output outputs/demo-readiness-report.json

# 复用 artifact 自带 manifest/checksum/runtime hash 校验；不调用模型
& $py "$artifact/run.py" --check

# 最小 readiness 单元测试：tmp dummy 资产/模型/索引/artifact，不联网、不跑模型
& $py -m pytest tests/test_demo_readiness.py -q
```

预期：readiness exit 0 且 `ready_for_offline=true`；`run.py --check` 输出 `ok=true`、`checksums_only=true`；`tests/test_demo_readiness.py` 4 passed。  
注意：`ready_for_offline=true` 只表示资产/产物/入口结构就绪，不表示模型/网络/回答质量已就绪。报告明确写 `not_a_live_test=true`、`service_started=false`、`live_model_invoked=false`、`source_read_only=true`。  
本仓库当前保存的 [demo-readiness-report.json](standalone.md#未包含) 是用 `--mode offline --env-file <显式文件>` 跑的，用于同时检查 `ready_for_live` 的配置名；它仍然是 `not_a_live_test=true`，不是模型调用。

### 5.2 真实问答需显式模型配置

适用：需要现场真实问答；必须满足网络 + 显式模型凭据 + 隐藏启动。

1. **显式 env 文件**：只通过实例 `environment_file` 注入；不要把 `.env` 复制进工程/artifact/zip；不要打印或截图值。readiness 可用 `--env-file` 只检查变量名与非空（boolean），不会向上搜索 `.env`。
2. **显式 instance 配置**：当前没有为 af34 固定好的 instance 配置。用 `--instance-config <显式路径>` 指向一份仓库外/临时配置；不要复用指向 v3 的旧配置，也不要把 workspace 主 artifact registry 当独立 source project 的运行依赖。
3. **隐藏启动**：在现有终端用 `Start-Process -WindowStyle Hidden` 或 Python `CREATE_NO_WINDOW`；**不使用先前被审批拒绝的 WMI 脱离进程树方法**；不要做“一键脚本弹 PowerShell 窗口”。
4. **临时服务结束关闭**：录制/验收后停止，确认端口释放；不留下常驻服务。
5. **当前服务默认未启动**：以下命令是“需要时由主模型按需执行”的说明，不是本材料已经启动的事实。若并行任务正在使用临时端口，先确认端口归属，不要复用或停止别人的服务；本任务不启动/停止任何产品服务。

instance 配置字段示例（**占位符，不是密钥；`environment_file` 用仓库外显式路径**）：

```json
{
  "schema_version": 1,
  "instance_id": "robot-vacuum-demo",
  "artifact_id": "robot_vacuum-v4-af34e8f764",
  "artifact_root": "<absolute path to robot_vacuum-v4-af34e8f764>",
  "name": "robot-vacuum-demo",
  "scenario": "robot_vacuum",
  "version": 4,
  "workflow": "agent",
  "host": "127.0.0.1",
  "port": 8944,
  "storage_root": "<writable directory outside artifact>",
  "environment_file": "<explicit local env file outside repo>",
  "asset_bindings": {}
}
```

启动（示例；先确认端口空闲，再执行；不要用 WMI）：

```powershell
$py = "C:/Users/28980/anaconda3/envs/langchain/python.exe"
$artifact = "E:/CODE/Myproject/agent-workspace/robot-vacuum-support-built/robot_vacuum-v4-af34e8f764"

# 录制前：live 模式只检查凭据名是否存在，不读取/输出值
& $py scripts/demo_readiness.py --artifact $artifact `
  --env-file "<explicit local env file outside repo>" --mode live `
  --output outputs/demo-readiness-report.json

# 真实问答：显式 instance 配置 + 隐藏启动；这是按需操作，不是默认常驻
Start-Process -FilePath $py `
  -ArgumentList @("$artifact/serve.py", "--instance-config", "<explicit instance.json>") `
  -WindowStyle Hidden
```

真实问答前确认 `GET /api/health` 的 `ready` 与 `needs_configuration`；**不要写公网在线 URL**。只在本机 `127.0.0.1:<port>` 演示；没有公网在线产品这个事实。  
GUI 8950 是人工审核工具，只读数据集/运行快照，不是客服产品；不要把它的页面当作产品演示，也不要在本任务里停止/重启它。

---

## 6. 录制前检查清单

- [ ] 运行 readiness，exit 0 / 非 0 明确；JSON 里 `not_a_live_test=true`。
- [ ] 指定了 `--artifact`；没有指定时报告显示“待指定”，**不按 mtime 猜最新，不启动旧产物**。
- [ ] 6 份知识原文、模型、索引只读存在；project.json `scenario=robot_vacuum`；spec owner=`spec`。
- [ ] `run.py --check` 通过，`checksums_only=true`；不把 checksum 通过说成模型可用。
- [ ] 真实问答额外检查：显式 env 文件变量名存在（boolean）、显式 instance 配置、端口空闲、隐藏启动、结束后关闭。
- [ ] 录屏素材里没有 env 值、密钥、完整 env 路径、用户隐私；没有把 GUI 8950 当客服产品。
- [ ] 所有实际回答都标明来源 run/artifact；expected 与 actual 分屏或分色，不混写。
- [ ] 失败案例归属正确：trace 重复是跨项目可观测性，不是客服性能；重复追问是 development 小样本；32 条审核不是成功率。
- [ ] 不出现：9→3、用户量、满意率、成功率、完整恢复、全部引用正确、官方授权售后、硬件/语音/多模态已完成、公网在线 URL。
- [ ] 临时服务已停止，端口释放；没有可见窗口；没有 WMI 脱离进程树。
- [ ] 当前可投简历以 [岗位A项目边界与简历描述.md](standalone.md#5-父工作区链接的处理) 1.3 节的三条 bullet 为准，升级候选只放在本文/讲稿的“待验证”区。

---

## 7. 简历升级候选（不改当前可投版）

> 当前可投版原文在 [岗位A项目边界与简历描述.md](standalone.md#5-父工作区链接的处理)，最新三条 bullet 以该文件 1.3 节为准。下表保留历史升级候选。没有满足待验证条件前，不替换当前 bullet。

| 候选表述（将来可考虑） | 现有证据 | 待验证条件 | 现在能投吗 |
|---|---|---|---|
| 为多轮售后实现 case-scoped 问题生命周期与 decision snapshot，固定 5 轮 development 中“已回答后重复追问”多余次数 1→0 | [question-lifecycle-report.json](standalone.md#未包含)；baseline/after 原始 run；after turn1 必要二选一、turn2 不再重复 | 扩到更多 development 场景；holdout 未运行；正式 P1-05/P1-06 评分；4 个 unplanned questions 的处理结论 | 否；只能作为面试口述的 development 观察 |
| 按需路由 + 按层计量：after 修正后 10 model calls / 7 ledger events / 13,493 reported tokens；纯致谢 turn 0 calls | [question-lifecycle-usage-correction.json](standalone.md#未包含)；[docs/question-lifecycle.md](question-lifecycle.md) 第 7 节 | af34 未重跑模型；baseline 缺分层 meter，不能做 total comparison；需要同层 baseline 与正式评测 | 否；不能写“整体降本 x%” |
| 完成 32 条案例定义人工审核（26 passed / 2 failed / 4 uncertain，7 notes），把 notes 归因为工作语境/渐进诊断/歧义与未结任务/测试分层 | [review-round-1-analysis.md](review-round-1-analysis.md)；[review-round-1-analysis.json](standalone.md#未包含) | 这是题目审核，不是回答成功率；holdout 未运行；人工语义复核与 P1-05/P1-06 未完成 | 否；不能写“26/32 成功率” |
| 独立 artifact/instance：manifest/checksums/runtime hash 离线校验通过，外部 cwd 可 `run.py --check` | [demo-readiness-report.json](standalone.md#未包含)；af34 manifest；README | 需要显式 instance 配置与一次真实服务 health；本材料没有启动服务，没有 live test | 当前简历已有“独立 artifact/instance”；只能补“离线校验”证据，不能升级为“部署验收完成” |
| typed work context + 渐进诊断：把“找不到充电座”的指代歧义做成一次二选一，把新手引导做成一步 + 观察点 | [context-diagnosis.md](context-diagnosis.md)；3 个真实短场景 0 error，3 次 `llm_structured` | 语义正确性未正式评分；曾出现一次多余 referent 轻确认；样本只有 3 个短场景 | 否；只能作为机制说明 |
| 真实 trace 重复失败案例：9 次工具调用被记成 18 个事件，修复后另一次运行 3 次调用对应 3 个 `tool_start` | [trace-dedup-v3.json](standalone.md#5-父工作区链接的处理)；[LONG_TERM_PLAN.md](standalone.md#5-父工作区链接的处理) 第 3 节 | 归属跨项目运行时/可观测性；不是同一任务 A/B；不能写成客服性能改善或 9→3 提效 | 可作为辅助项目/可观测性案例，不可放客服性能 bullet |

**一句话口径**：当前可投版只写已实现能力；上述候选在“待验证条件”满足前，全部不进可投版，也不口头说成已完成。

---

## 8. 7 条用户 notes 如何驱动机制（讲“用户审核 → context”时用）

> 用户判定和 notes 原文保留在 [review-round-1-analysis.md](review-round-1-analysis.md)；这里只做机制归因，不改用户判定。

| case | 用户判定 | 机制归因 | 当前处理/剩余 |
|---|---|---|---|
| `direct-recharge-triage-01` | uncertain | 不能要求用户先会排查 | typed context + 单步引导 + 观察点；正式人工复核待做 |
| `multi-recharge-tried-advance-01` | uncertain | “找不到充电座”主体/经验者歧义 | 一次二选一澄清；intended reading 仍待出题人确认 |
| `correction-mop-water-level-02` | uncertain | 同族变体独立性不清 | 保留原记录；是否补 `variant_mechanism_id`/合并待数据决策 |
| `multi-topic-switch-mop-02` | uncertain | 旧任务未结时缺少轻量确认 | 先答新主题，旧任务 open，最多一次轻确认；验收待补 |
| `negation-filter-not-cleaned-02` | failed | 话术测试味重，不像真实用户 | 原记录保留；自然说法变体待数据修订 |
| `multi-recharge-closure-01` | failed | 依赖用户主动报告成功，弱信号/沉默未分层 | closure 分级：弱信号礼貌收束不标 resolved；真实结案样本待补 |
| `fault-process-restart-01` | passed | restart 应进工程 fault 套件，不应混 QA | fault 分母单列；faultpoint+seed 程序化生成待做 |

**讲法**：不要说“7 条都修好了”。说“7 条 notes 被归并为四类机制，其中工作语境、歧义/未结任务、渐进诊断已做 development 切片；数据变体、closure 真实样本、fault 生成器仍待验证”。

---

## 9. 证据索引

| 结论 | 证据 |
|---|---|
| 只读就绪检查与 `not_a_live_test` | [demo-readiness-report.json](standalone.md#未包含)、[scripts/demo_readiness.py](../scripts/demo_readiness.py)、[tests/test_demo_readiness.py](../tests/test_demo_readiness.py) |
| 问题生命周期、decision snapshot、actual-asked | [docs/question-lifecycle.md](question-lifecycle.md)、[question-lifecycle-report.json](standalone.md#未包含) |
| 固定 5 轮 baseline/after 与计量修正 | [baseline run](standalone.md#未包含)、[after run](standalone.md#未包含)、[usage correction](standalone.md#未包含) |
| 32 条案例定义审核与 7 notes | [review-round-1-analysis.md](review-round-1-analysis.md)、[review-round-1-analysis.json](standalone.md#未包含) |
| work context / 歧义 / 渐进诊断 | [context-diagnosis.md](context-diagnosis.md)、[context-diagnosis-report.json](standalone.md#未包含) |
| 48 条案例、32 dev / 16 holdout、holdout 未运行 | [eval/README.md](../eval/README.md)、[eval-integration-report.json](standalone.md#未包含) |
| 当前可投简历（1.3 节） | [岗位A项目边界与简历描述.md](standalone.md#5-父工作区链接的处理) |
| 进度唯一权威 | [LONG_TERM_PLAN.md](standalone.md#5-父工作区链接的处理) |

---

## 10. v1 发布走查（2 条售后路径 + 1 条广度问答）

本节是 v1 收尾演示，旧章节保留为历史材料。人工走查可选，不要求先完成审核。演示输入复用已有 development cases，实际回答以现场运行和 [最终报告](standalone.md#未包含) 为准。

三条输入来自已有 development cases，属于 AI 生成或复用的 grounded 输入。人工走查可选，不作为人工审核或真实用户反馈。

### 10.1 入口

最终 artifact 是 `robot_vacuum-v4-24f3a3db39`，配置是 [instance-8902.json](standalone.md#未包含)。当前 8902 已在运行并保持为正式入口。看成品直接打开 [回放](standalone.md#未包含)。要现场走查，在 8902 新建会话按下面三条路径操作，服务管理命令见 [release-v1.md](release-v1.md) 第 6 节。

演示前不需要准备真实工单、真实用户或公网地址。回放使用 final12 持久化记录，不是实时模型调用。

### 10.2 路径 A 已解决

| 步骤 | 操作 | 预期结果 |
|---|---|---|
| 1 | 新建会话 | 生成新的会话，状态面板初始隐藏或为空白 |
| 2 | 发送“刚才我让机器人自己回充，它已经自己找到充电座并开始充电了。” | 回答确认恢复，售后状态显示“已结案” |
| 3 | 查看售后状态 | 当前问题保留回充描述，下一步显示“已恢复，无需继续处理”，没有用户执行动作时已试步骤显示暂无记录 |
| 4 | 点击“整理转人工摘要” | 摘要走回充结构化案例，保留自动回充恢复观察，同时写明原因未确认 |
| 5 | 可选点击“复制摘要”或“下载 .txt” | 状态提示已复制或已导出，摘要内容不自动发送 |

这条路径展示“明确恢复可以结案，原因仍不确认”。回答不应说某个零件故障已被证明，也不应把结案写成所有潜在问题都已排除。界面截图见 [product-ui-release-v1.png](standalone.md#未包含)。

### 10.3 路径 B 未解决并更正

| 步骤 | 操作 | 预期结果 |
|---|---|---|
| 1 | 新建会话 | 生成新的会话 |
| 2 | 发送“我已经把它放回充电座了，但还是充不上电。” | 回答按“手动放回已试且失败”给出下一步，不把原因定死 |
| 3 | 查看售后状态 | 状态显示“处理中”，已试步骤显示“重新对准充电座”，结果失败 |
| 4 | 发送“更正一下，刚才说错了：其实我没有把它放回充电座，只是按了回充键，自动回充也还没再试。” | 回答先更正记录，撤回上一轮已试事实 |
| 5 | 再次查看售后状态 | 已试步骤清空，未试步骤显示“重新对准充电座”和“检查自动回充设置”，下一步是手动放回并观察 |
| 6 | 点击“整理转人工摘要” | 摘要保留第 1 轮原话、第 2 轮更正、轮次和 message id，保持未解决 |
| 7 | 可选复制或下载摘要 | 得到本地 `.txt` 文本，不生成外部工单 |
| 8 | 可选刷新页面或按需重启服务后重新打开会话 | 更正后的状态和摘要保持稳定，重复读取摘要不改变状态 |

这条路径展示“用户更正会撤回旧事实，而不是继续按错误前提排查”。旧事实保留在审计中，当前状态不再把它当作已试步骤，摘要也不能写成已确认原因。

### 10.4 广度问答

| 步骤 | 操作 | 预期结果 |
|---|---|---|
| 1 | 新建会话 | 生成新的会话 |
| 2 | 发送“卧室用，扫地机器人的工作噪音选多少比较合适？” | 回答给出卧室噪音参考，并带 `[选购指南.txt]` 来源 |
| 3 | 查看页面 | 没有回充售后状态面板，本轮来源片段显示 `选购指南.txt` |
| 4 | 点击“整理转人工摘要” | 摘要走 transcript-only，明确没有形成回充故障案例，不推断设备状态 |

回答应把 55dB 写成资料中的选购参考，不写成所有模式或所有机型的绝对保证，也不编造具体型号的实测噪音。

### 10.5 来源轮次怎么找

- 售后状态里的已试步骤每行显示第 N 轮和用户原话，可以直接对应到具体用户消息。
- 转人工摘要会列出用户第 N 轮和助手第 N 轮，并带 `m-...` 消息 id，复制或下载后仍可核对。
- 知识来源先看回答中的方括号文件名，再看“本轮来源片段”的文件名和片段。
- 需要原始 trace 时通过会话 API 读取 `trace_id`、`trace` 和 `context`，产品界面不直接展示 trace。

### 10.6 演示边界

- 回放是正式标注的录屏回放，不是实时录屏，也不是新的模型调用。
- 回放只覆盖 3 条路径共 4 轮，不是成功率，也不是完整 12 案展示。
- 12 案结构验收可接受，其中 `rm-recharge-failed-outcome` 第 2 轮原始 `retrieval_attempted=false` 保留，不声称 12/12 原始检查通过。
- 不把结构通过写成语义正确，也不声称逐条语义蕴含已证明。
- 不把本地摘要写成真实外部工单。
- 不把 5 条 selected case 写成 final12。
- 回放重新点击一次下载并核对文件与摘要一致，没有重复做操作系统剪贴板审计。final12 的复制证据仍按原本范围引用。
- 不把演示输入写成人工审核或真实用户反馈。
