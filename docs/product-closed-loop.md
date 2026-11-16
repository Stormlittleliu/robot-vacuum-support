# 产品闭环 v1：完整可用售后会话与证据型转人工摘要

日期：2026-11-15。状态：`completed_with_known_limits`；P1 整体仍 `in_progress`。  
范围：回充主线深挖 + 一个非回充知识问答；不是 holdout，不是成功率，不做真实外部工单。

## 1. 本切片解决什么

| 已确认问题 | 修复层 | 验收 |
|---|---|---|
| `pi-reseat-outcome-lost`：语义层 `reseat_dock/failed` 被动作 regex 重算成 `unknown` | state/execution 优先消费已验证、证据绑定的 `executed_actions.outcome`；regex 只在无语义结果时兜底 | `rm-recharge-failed-outcome` 第 1 轮真实 state 为 `reseat_dock=failed`；手工 fallback 反例仍保留 failed |
| `pi-third-party-fallback-observation`：条件/第三方子句的“手动放上去能充”被记成当前用户观察 | 共享子句 source-scope：条件/他人来源向后传递，遇到第一人称、转折或句界重置；语义观察校验与 deterministic fallback 共用 | 第三方/假设 case 无 `manual_charging=works`、无 tried、无 partial_success；当前用户自己的手动观察反例仍保留 |
| `pi-recovery-action-label-overreach`：自动恢复被写成“检查自动回充设置” | 执行类 `check_auto_return` 要求 settings/map 原文证据；旧持久化行在 UI/prompt/摘要中使用中性“自动回充（用户原话）”标签 | 明确恢复 case 结案但 tried 为空、`automatic_return=works`；旧状态行不再渲染“检查设置” |
| 共享 `reseat_dock` 显示标签过宽：用户明确“没有特意对准，只是随手放上”，tried 仍显示“重新对准充电座” | 只改显示层：`reseat_dock` 统一显示“手动放回充电座”；动作 id/详情不变；UI tried 行、prompt、handoff 共用该标签并保留用户原话 | 当前 state 与旧持久化行均显示中性标签，raw quote 可见；不把“随手放上”升级为“已对准” |

没有新增 case 专用正则来“猜”这些语义；scope 与 outcome 都复用 interpretation 的 quote/source 校验。

## 2. 转人工摘要

`handoff_summary` 是只读投影，输入是当前 support state + 实际 transcript；不调用模型、不写 state、不发外部工单。

- 回充案例：当前问题/原话/轮次、用户实际执行的动作与结果（含 quote、turn、message id）、明确没试过的步骤、能力观察、助手建议（单独标注“不是事实”）、未知项/下一步、结案与原因未确认边界。
- 非回充问答：没有回充案例时不再返回“无可整理”；按 transcript 给当前问题与最近回答原文摘录，并明确“没有形成回充故障案例、不推断设备/已试/原因”。
- 来源：对话记录保留 turn/message id；知识文档只在 `retrieval.status=ok` 时列出，`not_retrieved` 时明确写“不把未绑定文件名写成来源”。**`not_retrieved` 只表示该轮没有当前检索记录，不等于历史引用自动是伪造的；判定引用真伪需要 turn/source lineage。**
- 路由：`GET` 与 `POST /api/sessions/{id}/handoff-summary` 返回同一投影；重复读取、重启后摘要稳定（除 `generated_at` 字段）；已结束/已结案会话仍可读。
- UI：转人工栏不再依赖回充案例存在；支持复制、UTF-8 `.txt` 下载、失败可重试、切换会话/发送消息清除旧摘要；下载文件名为 `handoff-<safe-session>.txt`。

## 3. 验收证据

- 原始产品闭环定向回归：`139 passed, 1 warning, 26 subtests passed`；本轮 label 修正后只重跑受影响的新测试：`tests/test_product_closed_loop.py` **14 passed**（未重跑 139/全局）。
- 历史模型运行产物（不是最终交付物）：`E:/CODE/Myproject/agent-workspace/robot-vacuum-support-built/robot_vacuum-v4-6b90c44acf`
  - 真实 5 sessions / 7 initial turns + 2 deterministic follow-ups 的 LLM smoke 实际跑在这个 artifact 上；报告里的模型回答/summary excerpt 属于该历史运行。
- **最终交付不可变产物**：`E:/CODE/Myproject/agent-workspace/robot-vacuum-support-built/robot_vacuum-v4-5ef6e24fbf`
  - 由工程自身 `build_project(..., include_assets=True)` 构建到外部 built 目录；外部 cwd `run.py --check` 为 `ok=true / service_ready=true / checksums_only=true`。
  - manifest sha256 `80049ec28994c2a4006f4f045c1e6335b2f3ee7f14c9a75787077a63b5ccb098`；runtime hash `b3dd047b8ae79b8747b93cb9b03c9ea1c932b575692d067125b1626835f95951`；content hash `f0cc9eeb7598c53d31d31f2026ddf69e13d9ba91f8bd958685c4e2f7474f93f4`；source key hashes 已逐文件匹配。
  - ZIP 同目录：`robot_vacuum-v4-5ef6e24fbf.zip`；build 记录 [build](standalone.md#未包含)。
  - 最终 artifact 没有新的 LLM 调用；只做了 label/UI 显示修正后的离线 projection + headless UI 验证。
- 真实 smoke（历史 artifact `6b90`）：隔离 8949，5 sessions / 7 initial turns，另加重启后 continue 与 GUI 发送各 1 个确定性 turn（合计 9 turns、6 sessions，仍在 6/10 上限内），来自 frozen rotating 集的 5 条：
  `rm-recharge-failed-outcome`、`rm-recharge-multiround-correction`、`rm-recharge-explicit-recovery`、`rm-recharge-hypothetical-third-party`、`rm-breadth-selection-bedroom-noise`。
  - 5/5 结构通过；第三方 case 无观察/无 tried/无 partial；明确恢复 resolved 且 tried 为空；更正轮撤回 reseat 并保留两项 not_tried；非回充问答为 transcript-only 摘要。
  - 所有 selected case 的 source sha256 与 quote 逐字检查通过；PDF 检索未命中的已知问题未在本切片修改。
  - 重启后 5 个 session 的 support 与 summary 哈希全部稳定；session/state 隔离通过；结束/重开 resolved session、创建新 session 通过。
  - GUI 隐藏 Chrome headless（历史 artifact）：实际复制 2365 字符、下载 UTF-8 2365 字符且与摘要一致，文件名 `handoff-s-65a808bc6e6341e0.txt`；切换/发送清空旧摘要、错误显示与重试按钮通过。
- **Label 修正离线验证（最终 artifact `5ef6`，无 LLM）**：
  - 用 stored real smoke state 重新 projection：`reseat_dock` tried 显示“手动放回充电座”，raw quote“随手放上…”保留；prompt/handoff 无 stale“重新对准充电座”。[projection evidence](standalone.md#未包含)
  - 用最终 artifact 起一次 transient hidden 8949 + headless Chrome，打开 stored real smoke session 的副本：UI tried 行显示“手动放回充电座 + 原话”，无“重新对准充电座”；handoff 同样。[UI evidence](standalone.md#未包含)、[screenshot](standalone.md#未包含)
  - 旧的 `gui/product-ui.png` 是历史运行截图，保留为历史证据，不再作为最终 label 展示。
- 证据入口：[report](standalone.md#未包含)、[freeze](standalone.md#未包含)、[final build](standalone.md#未包含)、[downloaded summary](standalone.md#未包含)、[smoke instance](standalone.md#未包含)。
- 复核说明：第一次 report 的 `source_sha_and_quote` 误把未选的 PDF 也纳入检查，故显示 false；已在 `report.json:report_corrections` 记录并按 5 条 selected case 离线修正，未重跑模型、未改任何产品结果。最终 artifact 的 linkage/addenda 见 `report.json:artifact_linkage`、`label_correction`、`addenda`。

## 4. 推荐升级配置：8901（主模型执行，不在本切片启动持久服务）

已准备 `outputs/product-closed-loop-v1/instance-8901.json`：

- `instance_id`: `robot-vacuum-support-product-v1`
- `name`: `扫地机器人售后客服`
- 最终 artifact：`robot_vacuum-v4-5ef6e24fbf`
- `port: 8901`
- `storage_root`: `outputs/manual-product-trial-v2/instance-storage`（新路径；本切片未创建）
- `environment_file` 用既有安全实例注入路径；不在文档/报告打印凭据值

本切片没有启动 8901，也没有停/启 8900。`8900` 的旧 `instance-8900-upgrade.json` 保留为历史中间配置，不再推荐；若主模型要升级 8900，用 8901 配置替换 port/storage 前需单独确认旧 manual-trial 数据迁移。

注意：session metadata 绑定 instance/artifact；换 artifact 后新会话正常，旧 session 目录保留但会被当前 runtime 的 artifact binding 过滤，不会自动出现在列表。若要让旧会话在新 artifact 下继续可见，需要另行做受控迁移；本切片未改该生命周期。

## 5. 已知限制

- 5 条 selected case 是 bounded smoke，不是完整 12 条轮换集，不是成功率，不外推真实用户总体。
- 历史 LLM smoke 跑在 `6b90`；最终 `5ef6` 只做 label/UI 修正后的离线 projection + headless UI 验证，没有新的模型调用，不声称最终 artifact 已被真实模型完整跑过。
- 没有新增权威 Judge run；结构/证据绑定与摘要投影是程序验收层，回答语义未重新评分。
- PDF 主文档检索未命中仍是已知 pending；本切片未改 RAG/prompt/知识索引。
- `not_retrieved` 只是当前轮检索状态，不是“历史引用一定伪造”的证据；引用真伪仍需 lineage。
- GUI 复制测试使用页面内 clipboard 测试替身；它验证 UI 实际调用复制路径与文本一致，不等于操作系统剪贴板审计。
- 未做真实外部工单；摘要仅本地整理。

## 6. 复现

```powershell
$py = "C:/Users/28980/anaconda3/envs/langchain/python.exe"
& $py -m pytest tests/test_product_closed_loop.py -q   # 本轮只跑这个 affected 文件

# 历史 smoke（会新建 instance-storage；已存在则拒绝覆盖旧证据；不要为变绿重跑）
& $py scripts/product_closed_loop_smoke.py --build

# 仅用已构建产物复跑（不覆盖旧 build 记录）
& $py scripts/product_closed_loop_smoke.py --artifact E:/CODE/Myproject/agent-workspace/robot-vacuum-support-built/robot_vacuum-v4-6b90c44acf
```

最终 artifact 的 label/UI 离线验证使用 stored real smoke states + transient hidden 8949，已清理；8901 未启动。现有 8900 / 8950 / 8955 / 8956 未在本切片启动、停止或修改；8949 与 headless Chrome 已释放。
