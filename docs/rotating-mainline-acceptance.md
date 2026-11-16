# 轮换主线验收集（rotating-mainline-v1）

日期：2026-11-15。范围：只做验收数据、脚本、定向测试、报告和文档；不改 runtime / prompt / Judge 核心 / UI，不新建大评测框架，不重建 artifact。

## 目标与构成

固定 12 个独立 session，一个复杂主题占一半，其余覆盖全部其他知识文档。本批主角是回充，下一批可轮换主角；旧失败、旧 hash、旧 review/holdout 保留。

| 组 | 数量 | 内容 |
|---|---|---|
| 回充主线 | 6 | 多轮更正、局部 vs 全局成功、失败结果、假设/第三方、明确恢复、未解决对话 |
| 广度 | 6 | 故障排除、扫地机器人100问.pdf、扫地机器人100问2、扫拖一体机器人100问、维护保养、选购指南，各 1 条 |

回充 6 条复用已有 grounded development 场景的 user turns，未改原 dataset / run / session：
`fb-correction-execution-outcome-01`、`ds-live-manual-works-auto-fails-01`、
`fb-partial-place-vs-align-01`、`fb-third-party-hypothetical-01`、
`fb-explicit-auto-recovery-01`、`ds-live-clearance-manual-only-01`。

广度 6 条每题只围绕一份文档的一个焦点，源 quote 必须在该文档抽取文本中逐字（空白归一）存在；PDF 同时检查 `pdftotext -layout` 和 `pypdf` 两个视图。PDF 选题与五份 txt 无重复 quote，`source.pdf_overlap` 已记录无重复。

## 模型调用前的确定性检查

`python scripts/run_rotating_mainline.py --validate-only` 覆盖：

- 6/12 回充、6/12 广度，12 个唯一 case / 唯一 user turn；
- 六个回充 theme 恰好各一次；六份文档各一次；
- 每条 evidence 的文件存在、sha256 匹配、quote 逐字存在、locator 行号匹配；
- manual-only oracle guard：用户只说手动结果时，不允许 oracle 要求自动回充失败；自动恢复必须由用户明确说出；
- 原 frozen v1 的 cases/suite/split/audit hash 不变；
- artifact manifest runtime hash 与 suite 声明一致；
- 程序评分只做结构/证据绑定，语义单独记录，不声称权威 Judge 通过。

## 本轮 live

- artifact：`robot_vacuum-v4-6aa0ba49c6`（复用，未 build；`run.py --check` ok）；
- 隔离隐藏服务：127.0.0.1:8949，并发 2，12 个独立 session，14 turns，一次运行；
- 现有 8900 / 8950 / 8955 / 8956 未触碰；运行后 8949 已释放；
- 每案 session 均调用 DELETE（HTTP 200，status=ended）；工作区保留在 `instance-storage/` 作为 raw 证据，不当作清理失败；
- 只通过实例 `environment_file` 注入凭据，未读取/打印任何凭据值。

证据：

- [freeze.json](standalone.md#未包含)
- [validation.json](standalone.md#未包含)
- [report.json](standalone.md#未包含)
- [semantic-findings.json](standalone.md#未包含)
- [post-run-verification.json](standalone.md#未包含)
- [raw/run.json](standalone.md#未包含)

## 实际结果

程序结构层：12 sessions / 14 turns，0 error / 0 not_run；**8 passed_structural，4 failed_structural**。失败分为：2 条 state bug（失败结果、第三方事实；失败结果案第 2 轮还有一次未检索却列来源）、1 条 PDF 主文档未检索、1 条多轮更正第 2 轮未检索却列来源。

| case | 组 | 结构 | 发现 |
|---|---|---|---|
| rm-recharge-multiround-correction | 回充 | **fail** | 两轮都接受更正；第 2 轮无本轮检索（status=not_retrieved）却列了来源文件名，结构证据绑定失败；回答语义本身正确 |
| rm-recharge-local-vs-global-success | 回充 | pass | 正确区分手动局部成功与自动全局目标 |
| rm-recharge-failed-outcome | 回充 | **fail** | 第 1 轮 state 把 failed 写成 unknown（已知 `pi-reseat-outcome-lost`）；第 2 轮未检索却列来源；回答本身正确 |
| rm-recharge-hypothetical-third-party | 回充 | **fail** | 回答正确拒绝迁移；state 把朋友家/假设的手动能充记成当前用户事实（已知 `pi-third-party-fallback-observation`） |
| rm-recharge-explicit-recovery | 回充 | pass | 明确自动恢复结案，原因仍未确认 |
| rm-recharge-unresolved-conversation | 回充 | pass | 未解决；未把遮挡写成唯一原因，自动回充仍未试 |
| rm-breadth-troubleshooting-suction | 故障排除 | pass | 顺序与 item 7 一致 |
| rm-breadth-pdf-3d-structured-light | PDF | **fail** | 未检索到 PDF；回答错误声称资料没有测距原理，实际 PDF Q50 有“通过红外点阵投影测量物体距离和形状，实现精准避障。” |
| rm-breadth-100q2-frequent-pause | 100问2 | pass | 原因/首查项与 item 6 一致 |
| rm-breadth-wetdry-wood-floor-warp | 扫拖一体 | pass | 与 item 31 一致，并给合理的干燥/观察建议 |
| rm-breadth-maintenance-empty-run | 维护保养 | pass | 与 item 43 一致（每月、无负载 1 分钟、查异响/卡顿） |
| rm-breadth-selection-bedroom-noise | 选购指南 | pass | ≤55dB 主结论正确；≤45dB/≤60dB 补充也在同一文档后文 |

语义 advisory（agent inspection，不是 Judge）：9 supported / 2 supported_with_state_caveat / 1 unsupported / 0 unassessed。PDF 那条是真实检索与回答错误，不是 oracle 错误。

## 已知边界

- 12 条固定 sample 不是成功率，不是 holdout，不外推真实用户总体。
- 结构分只证明回答非空、检索发生、广度题绑定到主文档、回充题命中稳定 state subset；不证明语义正确。
- 没有新 Judge run / rubric，没有权威语义通过；人工压测本批未开展（not_run）。
- 原 frozen v1 dataset / reviews / holdout 未修改；artifact、models、knowledge、rag_index 只读。

## 下一产品步骤

仍不是“全项目完成”。下一优先是完整可用支持会话闭环：修复本轮再次确认的 state bug（`pi-reseat-outcome-lost`、`pi-third-party-fallback-observation`），再验收“成功结案 + 未解决转人工摘要”（只含当前问题、已试/结果、来源/轮次、未知项、下一步观察；不生成真实外部工单）。PDF 检索缺失作为下一轮检索/覆盖问题单独记录，不在本批改 prompt 或 Judge。

复现：

```powershell
$py = "C:/Users/28980/anaconda3/envs/langchain/python.exe"
& $py scripts/run_rotating_mainline.py --validate-only
& $py -m pytest tests/test_rotating_mainline_acceptance.py -q
& $py scripts/run_rotating_mainline.py --run   # 隔离 8949，12 sessions，一次
```
