# 扫地机器人信息问答客服（独立项目）

本工程由编辑器导出的 schema v2 产物 fork 而来，可独立编辑、构建和运行。
场景名：`robot_vacuum`；默认工作流：`agent`（ReAct）；工具盒：`rag`（项目通用盒）。
只读取本工程 `assets/knowledge/` 里的 6 份扫地/扫拖机器人资料，不包含服装资料。

> **GitHub 独立发布版** 本仓库不依赖父工作区 `agent-workspace`，也不包含原开发机的构建产物、运行会话、人工审核原文和未授权资料。6 份知识原文因再分发授权不明**未上传**；模型、向量索引、`outputs/`、`eval/results/`、`eval/reviews/`、`eval/judge_results/` 等大资产或运行证据同样不发布。克隆后如何本地恢复、如何区分历史父目录链接、以及本次发布排除了什么，见 [docs/standalone.md](docs/standalone.md)。下文所有指向 `outputs/...` 的链接统一指向该说明，表示“本地证据，未随仓库发布”。

> **Release v1 交付说明（2026-11-15）** 面向扫地机器人售后咨询的 6 份通用资料闭环，覆盖选购、使用、保养、故障排查与回充深挖，支持检索证据回传、会话内回充状态、来源作用域、更正撤回、重启恢复和未解决转人工摘要的本地复制下载。最终产物为 `robot_vacuum-v4-24f3a3db39`，配置为 [instance-8902.json](docs/standalone.md#未包含)；运行状态以 `status --json` 为准（2026-11-15 本批只读检查时为未运行，未启动/未重启）。

## 当前入口（2026-11-15）

当前推荐入口是 **8902**，实例 `robot-vacuum-support-release-v1`，产物 `robot_vacuum-v4-24f3a3db39`，配置 [instance-8902.json](docs/standalone.md#未包含)。它保持为唯一正式入口；若 status 显示未运行，演示前用下方 `start` 启动。旧 8900 和 8901 已停止，存储与旧产物原样保留。

产品是本地售后知识问答会话。范围是 `assets/knowledge/` 里的 6 份通用扫地/扫拖资料，混合检索后返回带来源文件名的中文回答，资料不足先追问。回充主线按会话保存当前问题、已试与未试步骤、下一步、来源作用域与结案状态，支持更正撤回、重启恢复和只读转人工摘要。摘要可本地复制或下载 UTF-8 `.txt`，不生成外部工单。

固定入口管理脚本在冻结产物之外，只管理这一份配置，不重建 artifact，也不接受任意配置路径。

```powershell
$py = "python"
Set-Location <path-to-this-repo>
& $py scripts/manage_product.py status --json
& $py scripts/manage_product.py start
& $py scripts/manage_product.py stop
```

`start` 发现同一实例已在运行时会采纳它，不重启。日志与 PID 在 `outputs/release-v1/service/`。`stop` 先核对 `/api/instance` 身份和进程命令，任一不匹配就拒绝停止，不杀外来进程。

- 演示回放：[outputs/delivery-closeout-v1/demo/replay.html](docs/standalone.md#未包含)，3 条路径共 4 轮 final12 实际记录，明确标注录屏回放而非实时录屏
- 6 场景演示台本：[docs/demo-prompts.md](docs/demo-prompts.md)；离线交互演示卡：[outputs/demo-kit-v1/index.html](docs/standalone.md#未包含)
- Prompt/Context 工程复盘：[docs/optimization-loops.md](docs/optimization-loops.md)（Prompt 回路无因果 A/B；Context 回路有 old→new 与反例）
- 本批维护报告：[outputs/demo-kit-v1/report.json](docs/standalone.md#未包含)
- 交付说明：[docs/release-v1.md](docs/release-v1.md)
- 收口报告：[outputs/delivery-closeout-v1/report.json](docs/standalone.md#未包含)
- 最终产物报告：[outputs/release-v1/report.json](docs/standalone.md#未包含)
- 12 案验收：[outputs/release-v1/acceptance12/report.json](docs/standalone.md#未包含)
- 面试材料：[docs/interview-demo.md](docs/interview-demo.md)

已知限制：没有正式 40–60 集、holdout、真实用户量、满意率或生产 SLA。回充状态只覆盖回充单流程。引用只检查来源存在性，不声称语义正确率。PDF 修复只覆盖当前 6 份文档的编号结构。12 案结构验收可接受，其中 1 条原始 `retrieval_attempted=false` 保留。

## 目录

```text
robot-vacuum-support/
  project.json                  # 源工程声明（spec/runtime/frontend/assets）
  robot_vacuum/                 # spec（owner=spec，v1.json 是权威）
  runtime/                      # 独立运行时源码（core + modules + config）
  product_ui/                   # 独立产品前端
  scripts/manage_product.py     # 固定 8902 启停状态包装，产物外代码
  requirements.txt
  assets/
    knowledge/                  # 扫地机器人原文（txt/pdf；本地提供，不入库）
    models/bge-small-zh-v1.5/   # 本地 embedding 模型（本地提供，不入库）
    rag_index/                  # 823 条向量的 Chroma 索引（本地提供，不入库）
  README.md
```

## 运行前提

- Python 3.10+，安装 `pip install -r requirements.txt`。
- 聊天模型通过 OpenAI 兼容接口调用（默认 `deepseek-chat`）。密钥只放在
  **实例 environment_file** 中（如仓库外的本地 `.env`），不要写进本工程、构建产物或 zip。
- 本地 embedding / Chroma 不需要联网；知识只来自 `assets/`。
- 不要把 `PYTHONPATH` 指向原仓库；运行时只使用本工程/产物内的 `runtime/`。

## 运行与构建

在任何外部 cwd 下，先做离线完整性校验：

```powershell
python <artifact>/run.py --check
```

从本工程重新构建 v2 产物（使用工程自己的 spec/runtime/前端/assets，不读取原仓库）：

```powershell
cd <此工程目录>
python -c "import sys; sys.path.insert(0, 'runtime'); from modules.artifacts.project import build_project; import json; print(json.dumps(build_project('.', '<外部输出目录>', include_assets=True), ensure_ascii=False, indent=2))"
```

构建会生成 `<name>-v<version>-<id>/` 与同名 `.zip`，并校验 manifest、checksums、
runtime hash 和 `assets`。不会覆盖已有产物。

启动独立产品（实例配置指向构建产物；`artifact_root` 用绝对路径）：

```powershell
python <artifact>/serve.py --instance-config <instance.json>
```

`instance.json` 需要 `schema_version=1`、`instance_id`、`artifact_id`、`artifact_root`、
`name`、`scenario=robot_vacuum`、`version`、`workflow=agent`、`host`、`port`、
`storage_root`、`environment_file`、`asset_bindings={}`。实例启动时会把
`assets/knowledge` 复制到会话数据目录，把 `assets/rag_index` 复制到会话
`workspace/chroma_db`，并只读共享 `assets/models`。产品地址：
`http://127.0.0.1:<port>/`；身份：`GET /api/instance`；健康：`GET /api/health`。

## 配置

- `runtime/config/rag.yml`：`chat_provider`、`chat_model_name`、
  `embedding_provider: local`、`embedding_model_name: data/models/bge-small-zh-v1.5`、
  `retrieval_k`、`retrieval_mode: hybrid`。
- `runtime/config/chroma.yml`：`collection_name: agent`、
  `persist_directory: data/runtime/chroma_db`、`data_path: data/knowledge`。
- 这些 `data/...` 路径由实例的 `AH_DATA_ROOT` / `AH_RUNTIME_ROOT` / `AH_MODEL_ROOT`
  投影到会话目录，不依赖原仓库绝对路径。
- 换模型/换索引时保持 embedding 维度一致（当前 BGE small zh 为 512 维）；
  索引仍是 823 条，collection 仍是 `agent`。

## 编辑与重建

- 场景是 spec-owned：改 `robot_vacuum/v1.json`（或编辑器保存新版本），
  再运行上面的 `build_project`；不需要 `eject`，也不要改 `state.json` 绕过守卫。
- 工具只登记 `rag_summarize` / `rag_retrieve`，没有业务动作工具（不能控制设备、
  查询账号或生成个人报告）。
- 回答要求：真实检索、方括号来源引用、资料不足时先追问；只使用扫地机器人知识。
- 如果改了 `runtime/` 源码或 `product_ui/`，重新构建即可把改动带进新产物。

## 历史切片与支撑工具（保留）

> 以下章节保留 2026-11-13 至 2026-11-15 早段的旧产物、旧端口和切片证据，不作为当前入口。当前入口以上方 8902 为准。人工审核 8950、8955、8956 仍是支撑工具，不是客服产品入口。

## P1 回充状态与检索证据（本轮）

- 本轮新产物：`E:/CODE/Myproject/agent-workspace/robot-vacuum-support-built/robot_vacuum-v3-1e0e9eefb5`（旧 v2 产物不覆盖）。
- 服务端按 session 持久化回充故障状态（已试/未试/下一步/来源），UI 显示“当前问题 / 已试步骤 / 下一步 / 本轮来源片段”，并提供仅本地整理的“转人工摘要”。
- 检索来源只做本轮证据存在性映射，不声称引用语义一定正确；空检索/错误/超时/冲突会显式提示。
- 实现与边界见 [docs/p1-recharge-implementation.md](docs/p1-recharge-implementation.md)，报告见 [outputs/p1-recharge-report.json](docs/standalone.md#未包含)。
- 16 条起步开发集、真实 4 轮观察和重启恢复证据仅限各自范围，不是 40–60 正式评测或成功率。

## 完整产品闭环与证据型转人工摘要（2026-11-15）

- 修复 `pi-reseat-outcome-lost`（已验证语义 outcome 优先于动作 regex）、`pi-third-party-fallback-observation`（条件/第三方子句 source scope 不变成当前用户观察）、`pi-recovery-action-label-overreach`（自动恢复不再写成“检查自动回充设置”，旧持久化行用中性标签）；共享 `reseat_dock` 的 tried 显示标签统一为“手动放回充电座”，不再把“随手放上”显示成“重新对准”，raw quote 在 UI/prompt/摘要保留。
- 转人工摘要是只读本地投影：回充案例含当前问题、用户实际执行/结果（quote/turn/message id）、助手建议（单列）、观察、未知/下一步、仅真实绑定检索来源；非回充问答给 transcript-grounded 问题/回答摘要并明确未形成故障状态。`GET/POST /api/sessions/{id}/handoff-summary` 等价、不写 state；重启/重复读取稳定；不生成外部工单。
- UI 转人工栏不再依赖回充案例存在；支持复制、UTF-8 `.txt` 下载、错误重试、切换/发送清空旧摘要、安全文件名。
- 历史模型运行产物：`robot_vacuum-v4-6b90c44acf`（真实 5 sessions / 7 initial turns + 2 deterministic follow-ups 的 LLM smoke 跑在该 artifact）。
- **最终交付不可变产物**：`E:/CODE/Myproject/agent-workspace/robot-vacuum-support-built/robot_vacuum-v4-5ef6e24fbf`（外部 cwd `run.py --check` ok；source key hashes 匹配；无新 LLM）。原始定向回归 `139 passed`；本轮 label 修正只重跑 affected 新测试 `14 passed`；stored real smoke states 的 projection + headless UI 对最终 artifact 验证通过，旧截图保留为历史。
- 证据：[report](docs/standalone.md#未包含)、[freeze](docs/standalone.md#未包含)、[final label UI screenshot](docs/standalone.md#未包含)、[label projection](docs/standalone.md#未包含)、[label UI](docs/standalone.md#未包含)；实现与边界见 [docs/product-closed-loop.md](docs/product-closed-loop.md)。
- 历史升级配置：`outputs/product-closed-loop-v1/instance-8901.json`（`instance_id=robot-vacuum-support-product-v1`、port 8901、storage `outputs/manual-product-trial-v2/instance-storage`、历史 artifact `robot_vacuum-v4-5ef6e24fbf`）。该服务已于 2026-11-15 停止，存储与产物保留，当前入口是 8902。旧 manual-trial session 绑定旧 artifact，换 artifact 后目录保留但不会自动出现在新列表。
- 这是 bounded development smoke，不是 holdout/成功率；PDF 检索未命中仍 pending，未改 RAG。`not_retrieved` 只表示该轮无当前检索记录，不等于历史引用伪造，需 lineage 才能判定。

## 工作语境 / 歧义 / 渐进诊断（IP-1 开发切片）

- 正常请求先用受 schema 校验的 `turn-interpretation/v1` 描述工作语境（领域、对象、经验者、可观察症状、歧义候选、已执行/未执行、阶段、熟悉度、来源与未知），确定性 reducer 只接受能追溯到用户原文的事实。
- “找不到充电座”等歧义只做一次二选一澄清；清楚上下文不重复问；按可观察现象给一个最低成本下一步，不要求用户先会排查。
- 助手建议、用户实际执行、执行结果反馈分存；弱正向信号（谢谢/好了）不结案，topic 切换保留旧任务且最多一次轻确认。
- 实现、真实 3 场景 trace、fallback 边界和未完成项见 [docs/context-diagnosis.md](docs/context-diagnosis.md)、[outputs/context-diagnosis-report.json](docs/standalone.md#未包含)。
- 这仍是 development 切片；不是 holdout 结果，不是成功率，也不代表 P1 评测完成。

## 待确认问题生命周期 / 按需语义与成本（P1-02 下一开发切片）

- case-scoped question record：`pending / answered / deferred / superseded / cancelled`，带 slot、证据、asked/resolved turn；已回答不再问，未回答的 deferred 不伪装成已答，纠正可带证据重开，换主题只暂缓不每轮提醒。
- `decision_snapshot` 同时供 prompt context 和回答生成：只给当前唯一可问问题、已确认事实、answered scope 与旧任务摘要；动态问题文案不进 system prompt。回答文本会做 actual-asked / unexpected-ask / missed-question 审计。
- 按需语义：同 message + 同 fact-state 重放复用已验证 interpretation；纯致谢确定性回复（不虚构 resolved）；明确 slot 回答可用预定义 schema 确定性处理；歧义/新症状/更正仍走语义模型。
- 用 `core/observability/turn_usage.py` 按层（interpretation / main_agent / rag_summary）按 call_id 计一次；聚合 main 行区分 `model_requests` 与 ledger event，token 不按请求数乘；缺 usage 写 null，费用无权威单价写 null；没有把 trace/budget/tool 重复相加。
- 实现与 oracle 见 [docs/question-lifecycle.md](docs/question-lifecycle.md)，报告见 [outputs/question-lifecycle-report.json](docs/standalone.md#未包含)，固定 5 轮原始运行见 [baseline](docs/standalone.md#未包含) / [after](docs/standalone.md#未包含)，after counter 离线修正见 [usage correction](docs/standalone.md#未包含)。
- 真实 after 产物：`E:/CODE/Myproject/agent-workspace/robot-vacuum-support-built/robot_vacuum-v4-6a1eeea77f`；最新计量代码产物：`robot_vacuum-v4-af34e8f764`（外部 cwd check ok，未再跑模型）。旧 artifact 未覆盖。
- 固定 5 轮 development 对话中，重复 referent 追问从 baseline 1 次降为 after 0 次；after turn5 纯致谢为 0 model calls。baseline 缺分层 meter、RAG 无法从旧 trace 拆出，因此**不做 total comparison，不算整体节约百分比**；这不是 holdout、不是成功率。

## 动作执行/效果语义与否定更正（P1 纵向切片）

- 回充 case 的 `user_executed` 与 `outcome` 保持两维：执行未知/失败/明确成功不混用，`case.resolved` 独立；局部“手动放回能充但自动回充仍失败”不结案。
- 否定/更正统一 fact reducer：tried 与 not_tried 同一 action 不共存；后续执行撤销旧 not_tried，后续更正撤销 tried 及推导 outcome，`fact_audit` 保留原文/轮次/原因/被替代事实；泛化“都没试过”只绑定上一轮明确问过/pending 的步骤，无指代或冲突时澄清而不猜。
- `replace_dock`（换新座）与 `move_dock`（挪旧座）与 `reseat_dock`（放回机器人）分开；replace/move 只记录、不自动建议；服务重启不记 `reset_robot`。
- 契约/证据：[docs/action-state-contract.md](docs/action-state-contract.md)、[report](docs/standalone.md#未包含)；测试 `tests/test_action_state_contract.py`；真实 2 session/6 turn smoke 见 [run](docs/standalone.md#未包含)。这是 development 切片，不是 holdout/成功率，P1 整体仍 `in_progress`。

## 动作状态修复后真实回答评审（2026-11-14）

- 隔离工作区 [outputs/action-state-answer-review-v1](docs/standalone.md#未包含)：2 条 development case（contact/dock correction）、6 个真实 turn 的只读投影、`freeze.json`、`source-map.json`、`evidence_bundles/`、空 `reviews/` 和机器 `judge_results/`。不进入冻结 v1 的 32/16。
- 一次 DSH 原生 Judge `jr-action-review-20261114-01`：contact `completed/passed`（11/11 coverage）；dock 初始 `partial/uncertain`，另一任务仅补跑失败 arbitrator 后为 `completed/uncertain`（11/11 coverage、0 unresolved、无 system_errors，但程序仍保留 1 条 citation rejection，故 case verdict 仍 uncertain）。**不是成功率，也不把 dock uncertain 写成 confirmed agent failure。**
- UI 已最小动态修复：holdout=0 时隐藏 split 按钮/gate/reveal 并回退 development；holdout=16 保留原保护/曝光确认，headless fixture 0/16 验证。
- 经理分析候选（非用户人审/gold）：[action-state-manager-observations.json](docs/standalone.md#未包含)；S1 手动放回后的充电观察被写成排他因果，Judge 对应项 passed，属候选漏判/评测局限，不改冻结 rubric/Judge/reviews。
- 13/13 程序 checks 是结构检查，与语义 Judge 分开；两案共 21 条 agent finding、0 evaluation、1 insufficient_evidence。
- 入口：[评审文档](docs/action-state-answer-review.md)、[结构化报告](docs/standalone.md#未包含)、[GUI 只读验收](docs/standalone.md#未包含)、[headless](docs/standalone.md#未包含)、[Inspect](docs/standalone.md#未包含)。人审记录仍为 0，需用户在 GUI 继续填写。
- 当前隔离查看入口：<http://127.0.0.1:8955/>（只读 dataset/run/Judge；审核记录写入该 workspace 的 `reviews/`；8950 原服务未动）。

## 人工审核 GUI（本地）

人工审核入口：<http://127.0.0.1:8950>。服务只读 v1 数据集和已有 development run，
把人工判定单独写入 `eval/reviews/`，不调用产品 runtime、不重跑模型、不运行 holdout，
也不把人工状态自动当成 Agent 得分。

```powershell
$py = "python"
& $py eval/review_server.py --host 127.0.0.1 --port 8950 --store eval/reviews `
  --state-file eval/reviews/server_state.json
```

- 案例定义与运行回答是两个独立审核对象，分别保存；状态为通过 / 不通过 / 存疑（初始未审核）。
- 备注支持多行；记录带 `caseHash`、`suiteRevision`、`reviewScope`、`runId`（如适用）、
  `revision`、`createdAt/updatedAt`、审核人和 run 快照；改题后旧记录保留为历史快照。
- 未保存切换有提示和本地草稿；并发 revision 冲突返回 409，不静默覆盖本地输入。
- 留出集默认隐藏，必须显式点“查看留出集”并记录首次曝光；页面没有运行 holdout 的能力。
- 右上角可导出 JSON / CSV（UTF-8 BOM，保留备注换行/引号，包含曝光记录）。数据位置与
  接口边界见 [docs/manual-review.md](docs/manual-review.md)。

## 时延诊断（可归因观测）

- 两条时延 slice 的集成收口、2-turn 真实范围与“时延哪来”结论见 [docs/latency-followup.md](docs/latency-followup.md)；结构化报告见 [outputs/latency-followup-report.json](docs/standalone.md#未包含)，集成证据见 [run](docs/standalone.md#未包含) / [verification](docs/standalone.md#未包含)。
- 结论、阶段表和未知边界见 [docs/latency-diagnosis.md](docs/latency-diagnosis.md)，结构化报告见 [outputs/latency-diagnosis-report.json](docs/standalone.md#未包含)。
- 旧 approved run 离线归因（不调用模型）：`python scripts/latency_existing_analysis.py --run eval/results/v1-live-development-20261113-224057-3f8412 --out outputs/latency-existing-attribution.json`
- 一次小规模真实诊断（构建新 artifact、端口 8945、1 cold + 2 warm、结束自动关闭）：`python scripts/run_latency_diagnosis.py`
- 只汇总已完成样本：`python scripts/summarize_latency_diagnosis.py > outputs/latency-diagnosis/summary.json`
- 本轮只做 meter/诊断，不改 prompt、路由、timeout 默认值或产品行为；n=3 不是分布，provider 网络/远端排队/生成在代理环境下标 unavailable。

## 校验

```powershell
python <artifact>/run.py --check
```

`--check` 只校验 manifest/checksums，不代表模型凭据和 assets 已就绪；
以 `GET /api/health` 的 `ready` 与 `needs_configuration` 为准。
