# P1-03 回充失败多轮状态 + P1-04 检索证据子集实现说明

## 本轮范围

- 只做“回充失败”单流程，不扩业务动作工具、不改 DSH、不做多 Agent。
- 服务端按 session 维护 `support_state.json`（schema v1），由 server 在现有 worker busy 锁内更新；worker 不另写一份状态。
- 字段：案例、问题、可选型号、已试动作/用户反馈、未试动作、助手建议、当前 stage、下一步、来源消息/轮次、转人工摘要。
- 旧无 state 会话懒初始化；重启/重开恢复；同实例不同 session 隔离；损坏 state 不冒充成功。
- 用户事实只来自用户消息；助手建议不升级为“已试”。否定、假设、问句、用户更正、最后明确报告分别处理。
- 步骤目录区分清障、清触点、清传感器、电源、自动回充设置、放回、重启、适配器；恢复出厂是独立高风险项，不自动建议。
- P1-04 子集：RAG 工具记录真实 `page_content` 片段；产品 UI 展示来源/片段；引用只做“文件名与本轮证据存在性映射”，不声称语义正确或已验证安全。空检索/工具错误/超时/冲突/不相关给出可恢复提示，工具错误不会被伪装成正常空结果。

## 启动新产物

```powershell
$artifact = "E:/CODE/Myproject/agent-workspace/robot-vacuum-support-built/robot_vacuum-v3-1e0e9eefb5"
python "$artifact/run.py" --check
python "$artifact/serve.py" --instance-config <instance.json>
```

`instance.json` 的 `artifact_id` 必须是 `robot_vacuum-v3-1e0e9eefb5`，`environment_file` 指向实例自己的显式环境文件；密钥不进工程/产物。

外部 cwd、无主仓库 `PYTHONPATH` 下已执行 `--check`，结果 `ok=true, checksums_only=true, issues=[]`。

## 用户侧功能

- 顶部“售后状态”面板显示当前问题、已试步骤、下一步、本轮来源片段。
- “整理转人工摘要”只在本机整理并展示，不调用外部售后系统、不自动发送。
- SSE 原事件保持兼容，`answer/interrupt/error` 事件附加 `support` 与 `retrieval`；历史 transcript 接口不变。
- 失败时 UI 给出可重试入口；面板内容全部来自 server，不暴露内部 prompt。

## 边界与已知限制

- 16 条是起步开发集，不是 40–60 正式集，也不是留出集；人工语义复核仍 pending。
- 本轮真实模型为单次观察，不是成功率、延迟或引用支持度的 benchmark。
- 引用映射只验证来源文件名是否出现在**本轮**检索结果；同一文件名下的内容是否完全支持结论仍需人工核对。
- 冲突检测是窄规则（如充电时长数值），不是通用事实核验。
- UI 只做了 headless HTTP HTML 存在性检查，没有真实浏览器交互自动化。
- 资料是 6 份通用文档，不是品牌官方授权售后；不能承诺官方价格/保修/维修结论。
- 未写 `assets/knowledge`、`assets/rag_index`、`assets/models`，也未重建索引。本轮没有前置 hash 基线，因此不声称 hash 完全一致。

## 证据

- `outputs/p1-recharge-report.json`
- `outputs/p1-recharge-real-run.json`（真实模型 4 轮 + 无依据问题 + 重启恢复 + 本地转人工摘要）
- `eval/runs/p1-recharge-dev.json`（16 fixture + 独立 16 起步集的确定性检查）
- `tests/test_p1_recharge.py`（25 个相关测试）
- `eval/cases/v0/recharge-start.json` 与 `docs/recharge-case-design.md` 由独立 executor 提供：16 条、50 处 sources 原文核对；未做语义自动评分。

## 面试可讲的真实失败案例

最初状态规则用“充电座/触点”宽关键词，把“清理过充电座周围的障碍物了”误记为“清洁充电触点”；同时成功正则把“还没有恢复正常/还不能正常回充”误判为已恢复。修复不是继续堆关键词，而是：

1. 把清障、清触点、清传感器拆成独立动作，动作识别按分句和最近动作上下文定界；
2. 成功/失败按分句顺序、否定/假设/问句过滤，取用户最后明确报告；
3. 对不确定情况保留 unknown 并追问，不把用户没说过的事写成事实；
4. 用正反例回归锁住误判，真实 LLM 回答仍由文档与提示约束，不临时发明业务能力。
