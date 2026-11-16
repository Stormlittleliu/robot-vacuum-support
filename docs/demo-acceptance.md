# Demo 验收（2026-11-17）

> 状态：**completed_with_known_limits**。本轮是客服 V1 最后一轮 Demo 交付验收，不是 holdout、不是成功率、不是全量回归。  
> 范围仅限本仓库：干净 clone 的构建/启动路径、8902 正式入口的 5 个新模型轮次、既有两案例整理、README 精简。  
> 结构化报告：本机 `outputs/demo-acceptance-20261117/report.json`（`outputs/` 不随仓库发布）。

## A. 干净 clone 构建与启动

从本地 git 仓库 clone 到临时目录，确认 clone 中**没有** `outputs/release-v1/instance-8902.json`，因此不能把 `manage_product.py` 当作克隆入口。随后把本地未发布的 3 类资产复制进 clone：`assets/knowledge/`（6 份资料）、`assets/models/bge-small-zh-v1.5/`、`assets/rag_index/`。

- **没有冒充全新安装**：依赖复用已有 conda `langchain` 环境（Python 3.10.21），未执行全新 `pip install`；验收只证明构建/启动路径可用。
- 构建返回码 0，产物 `robot_vacuum-v4-ff032a9cc9`；外部 cwd `run.py --check` 返回码 0。
- 用隔离端口 `8917`、隔离 `storage_root` 和现有仓库外 `environment_file` 启动；`/api/health` 为 `ready=true`，`needs_configuration=[]`，`/api/instance` 身份匹配。
- 临时服务验收后停止；原 `assets/` 与正式 artifact 未被修改。源索引 sha256 仍为 `6cfd9514f8cb840367d0da3f479681e645e2f9d3b0b6c29809d6ec2ef0c4aa05`。

边界：本次没有验证“新机器从零 pip 安装”的成功率，也没有把本地资产当作仓库内容发布。

## B. 真实模型 Demo（5 个新用户轮次）

通过 8902 正式入口的正常 API（`POST /api/sessions`、`POST /api/sessions/{id}/messages`）运行，共 **5 个新模型用户轮次 / 3 个新会话**，每轮上限 90 秒；实际全部成功，0 error，最慢 28.76 秒，未触发超时。恢复检查复用同一会话，不产生新模型轮次。

| 验证项 | 实际结果 |
|---|---|
| 资料引用（PDF） | 2 轮，检索 `status=ok`，检索结果含 `扫地机器人100问.pdf`，回答正文含 `[扫地机器人100问.pdf]` |
| 资料引用（选购） | 1 轮，检索结果含 `选购指南.txt`，回答正文含 `[选购指南.txt]` |
| 多轮回充 | 第 1 轮状态 `reseat_dock / failed`、`case=active`；第 2 轮更正后 `tried=[]`，`not_tried=[reseat_dock, check_auto_return]`，仍 `active` |
| 用户更正 | 更正轮明确区分“按了回充键”和“没有手动放回”，旧 tried 被撤回，未把更正写成又一次失败 |
| 会话恢复 | 重启 8902 后 3/3 会话仍可读；转人工摘要 sha256 与重启前一致，GET/POST 同文 |
| 交接摘要 | 回充会话为 `structured_case`（2237 字符）；PDF 会话为 `transcript_only`（1377 字符）；均标注仅本地整理、未自动发送 |

截图由隐藏 headless Chrome 生成，未打开可见窗口：[产品界面截图](images/demo-8902-headless-20261117.png)（页面显示本轮选购会话与来源）。截图只证明页面可加载和回答可展示；本轮**没有**重复做产品 UI 的复制/下载点击链路，也没有做操作系统剪贴板审计。

## C. 面试案例

[docs/interview-demo.md](interview-demo.md) 已整理为两段各约 2 分钟的故事：

1. 语义层 `failed` 被旧状态层写成 `unknown`：归因到 state/execution 的 regex 重算，不是模型编造，也不声称 prompt 因果提升。
2. PDF 有 Q50 原文却漏召回：归因到编号条目切片只对 txt 生效、页级 8 块稀释 top-4；修复在 RAG ingestion/coverage，不是 prompt。

两段都保留反例、原始失败记录和“不是 holdout/成功率”的边界。

## D. README

[README.md](../README.md) 已改为 GitHub 访客入口：移除开头冗余历史状态，补实际构建/启动步骤、Demo/案例链接、Mermaid 架构图和已知局限；保留本地资产恢复、配置、编辑重建和测试说明。明确标注 `manage_product.py` 只适用于本机固定 8902 配置，不是克隆入口。

## 运行的必要检查

- `python -m pytest tests/test_demo_readiness.py -q` → **4 passed**（0.25s）。
- 本轮未改 runtime/spec/prompt/RAG 代码，未跑全量回归；历史 139/14/13 条仍只代表当时范围。
- 已检查本次改动文档的相对链接与仓库文件存在性（见提交前检查）。

## 未完成与限制

- 没有全新 `pip install` 验收；复用已有 conda 环境。
- 5 个模型轮次只覆盖本轮演示路径，不是成功率、holdout 或用户体验通过率。
- 没有跑 GUI 复制/下载全流程；截图是隐藏 headless 首页，不是完整 GUI 验收。
- 没有跑全量测试；只跑受影响的 demo readiness 测试。
- 未推送远端；提交留在本地，等待主模型复核后推送。

## 服务状态

- 正式入口 8902：验收结束时**运行中**（本轮启动并重启过一次用于恢复检查）。
- 临时 clone 服务 8917：验收后**已停止**。
- 没有读取、打印或提交任何密钥值；没有上传知识原文、索引、模型、运行会话或密钥。
