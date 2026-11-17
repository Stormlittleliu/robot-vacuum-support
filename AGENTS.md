# AGENTS.md（robot-vacuum-support）

本文件是本仓库的长期改进原则。本仓库是独立发布工程，父工作区文档、计划与构建产物不随仓库提供；发布边界和父目录链接的处理见 [docs/standalone.md](docs/standalone.md)。进入本仓库先读项目 [README.md](README.md) 与 [docs/standalone.md](docs/standalone.md)，再按当前用户指示推进。已有授权跨会话有效，不重复索要确认；这不是新的审批门。

## 长期改进原则

1. **从症状例上升到机制，再选层。** 看到一个错误，先写清“用户目标 / 工作语境 / 普遍失败机制”，再判断应改 `data → context → tools → state/execution → validation → prompt` 的哪一层。不要就事论事改 prompt，也不要用一个 case 的措辞代表全局。
2. **prompt 只表达稳定职责原则。** 范围、证据引用、安全边界、资料不足先追问、不编造等稳定职责可留 prompt；型号、已试步骤、旧任务、当前阶段、用户熟悉度等动态事实进 context/结构化 state。禁止把每个观察到的错误补进 system prompt。
3. **不要用一堆正则/keyword 代替语义与泛化。** 正则只能作为稳定、显式短语的临时适配器，不能当“理解用户”的主层。优先 policy/oracle + 结构化语境 + 工具证据；新增规则前先证明它覆盖同类新例和反例。
4. **先 oracle/policy，再实现。** 先定义期望行为、状态转移、歧义分类、分母和反例，再改代码。没有 oracle 的“修好了”不算完成。
5. **用同类新例和反例验收。** 每个改动至少配：同族新例子、一个反例、一个“只修了表面”的检查。不能用 holdout 调参；未运行时不据其调参，运行按当前任务与已有授权记录。
6. **不可用改预期掩盖缺陷。** 不把 passed 随便改判，不把 failed/uncertain 强行改成通过；不删改用户 notes 来让数字好看。确有 oracle 错误时，需有证据修订并保留原历史。不同意某条 note 可以举证并标“部分同意/待核实”，但保留原记录。
7. **题目审核 ≠ Agent 回答评分。** `case_definition` 的判定不是成功率；QA 与 fault 分母分开，fault 恢复不能混进用户体验成功率。
8. **测试分真实体验层。** 真实对话 QA、语言鲁棒变体、工程 fault 分套；restart 必要，但用 faultpoint + seed 程序化生成并验状态连续性/隔离/无重复副作用，不把“能继续聊”当恢复成功，也不把生成变体数量当成功率。
9. **合法例外。** API/协议确实要求特定语法时，可在局部 tool instructions 写特定规则；这不是绝对禁止 specific 规则，而是不要把局部协议要求升级成全局 prompt 补丁。

## 强制测试契约

- Agent 产品必须作为整体导入隔离测试环境，从正常产品入口验证；禁止拆出局部组件走捷径。
- 被测对象必须是完整、同版本的 artifact/container，包含真实运行配置及 prompt/context/state/tool/RAG 链路；业务场景只通过正式 HTTP/SSE 或 GUI 入口触发。
- 测试环境只隔离 data、storage、端口和凭据配置，不得改变产品组成。
- 禁止另写 mini-agent、复制 prompt、直接调用 reducer/retriever 等冒充产品验证；禁止 mock/fake 核心链路后宣布产品通过。
- 组件测试仅作定位辅助，必须明确范围；不能替代完整产品验收，也不能独立计算 Agent 效果。
- 模型、知识、API 等依赖不可用时，记录未验证/阻塞，不得自动替换 fake 绕过。
- 零模型人工路径只能证明完整产品的人工功能，不得据此宣称 Agent 全链路通过。
- 评测器可外置，被测对象必须是整产品。报告须记录 artifact/image 版本、入口、隔离配置、实际执行路径和缺项。
- 本节仅固化规则；不跑评测、不改历史记录、不新建门禁系统。

## 授权与修订

- 按用户当前任务与已有授权实施；已有授权跨会话有效，不因新会话重复确认，不设新的审批门或每任务固定文档要求。
- 用户审核记录与冻结数据按历史修订保留，不覆盖、不伪造；需要修订时留原记录、证据和原因。
- 服务/浏览器/后台验证一律隐藏窗口；临时服务用隔离端口并在验收后停止。

## 入口

- 产品与运行：[README.md](README.md)
- 独立发布边界与本地资产：[docs/standalone.md](docs/standalone.md)
- 评测：[eval/README.md](eval/README.md)
- 人工审核：[docs/manual-review.md](docs/manual-review.md)
- 本轮审核分析：[docs/review-round-1-analysis.md](docs/review-round-1-analysis.md)
- 进度与边界：本仓库不包含父工作区计划；以 README、`docs/standalone.md` 和当前任务记录为准
