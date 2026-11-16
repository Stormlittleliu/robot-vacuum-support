# 独立仓库说明（GitHub 发布边界）

本仓库是 `robot-vacuum-support` 的独立发布副本，不依赖父工作区 `agent-workspace` 的 `PYTHONPATH`、计划文件或构建产物。本文说明克隆后能直接看到什么、哪些内容只在本地提供，以及历史文档里的父目录链接为什么不再指向外部文件。

## 1. 发布范围

### 已包含

- `robot_vacuum/`：场景 spec（`v1.json` 为权威，`v2`–`v4` 为历史版本）
- `runtime/`：独立运行时源码与 `runtime/config/` 配置模板
- `product_ui/`：产品前端
- `scripts/`：产品管理、评测、诊断和验收脚本
- `tests/`：单元、集成、fault、评测和 UI 测试
- `eval/`：评测 runner、scorer、数据契约、小型 case 数据和 review UI 源码。测试 fixture 中含少量知识原文短引用（单条不超过约 100 字符，用于证据绑定校验），不包含 6 份资料全文
- `docs/`：设计、契约、评测口径、失败分析和发布说明
- `project.json`、`requirements.txt`、`.env.example`、`instance.example.json`
- `assets/*/README.md`：本地资产的恢复说明和校验值

### 未包含

| 路径 | 内容 | 原因 |
|---|---|---|
| `assets/knowledge/` | 6 份知识原文 | 未找到明确的再分发授权；只保留文件名、字节数和 SHA-256 |
| `assets/models/` | `bge-small-zh-v1.5` 模型 | 约 92 MB，可通过官方模型仓库重新获取 |
| `assets/rag_index/` | Chroma 向量索引 | 派生自知识原文，体积较大；从可信本地备份恢复 |
| `outputs/` | 构建产物、报告、截图、demo、真实会话与 smoke 证据 | 大生成产物；其中含运行会话、模型输出和本地路径 |
| `eval/results/` | live/state 运行结果和 session storage | 运行会话日志与模型输出 |
| `eval/reviews/` | 人工审核 JSON、reviewer 和 notes 原文 | 个人审核原文敏感数据 |
| `eval/judge_results/`、`eval/runs/` | Judge 和开发 run 结果 | 运行证据，体积与隐私边界 |
| `runtime/data/`、`runtime/outputs/` | 本地 Chroma 缓存、日志和 trace | 运行缓存与日志 |
| `__pycache__/`、`.pytest_cache/`、`.pytest-eval-tmp/` | Python/pytest 缓存 | 可再生成 |
| `.env`、凭据、venv | 本地密钥和环境 | 安全边界 |

因此，README 和其他历史文档里指向 `outputs/...`、`eval/results/...` 的链接表示**本地证据位置**，不是公开仓库中的可点击文件。需要复核时，应在本机原工作区查看；公开仓库只保留代码、文档、测试和小型数据契约。

## 2. 克隆后的最小理解路径

1. [README.md](../README.md)：产品入口、运行方式、当前边界。
2. [project.json](../project.json)：spec/runtime/frontend/assets 的工程声明。
3. [runtime/config/rag.yml](../runtime/config/rag.yml) 与 [runtime/config/chroma.yml](../runtime/config/chroma.yml)：模型、检索和索引配置。
4. [robot_vacuum/v1.json](../robot_vacuum/v1.json)：权威场景 spec；`v4.json` 为当前迭代版本。
5. [eval/README.md](../eval/README.md)：评测口径、数据契约和命令。
6. [docs/manual-review.md](manual-review.md)：人工审核边界。

## 3. 本地恢复资产

### 3.1 知识原文

把有权的 6 份文件放到 `assets/knowledge/`，文件名和 SHA-256 见 [assets/knowledge/README.md](../assets/knowledge/README.md)。如果替换了内容，检索证据、索引和部分评测结果都会变化。

### 3.2 模型

把 `BAAI/bge-small-zh-v1.5` 放到 `assets/models/bge-small-zh-v1.5/`，文件清单与校验值见 [assets/models/README.md](../assets/models/README.md)。当前索引要求 **512 维**。

### 3.3 向量索引

把可信备份恢复到 `assets/rag_index/`，参数与 `chroma.sqlite3` 校验值见 [assets/rag_index/README.md](../assets/rag_index/README.md)。仓库没有发布独立的一键重建索引 CLI；重建需复用 `runtime/core/tools/rag/` 的解析、分块和 embedding 逻辑，并保持 collection `agent` 与 512 维一致。

## 4. 配置与运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

模型密钥只放在**仓库外**的显式环境文件里：

```powershell
Copy-Item .env.example ..\local-secrets\robot-vacuum.env
# 编辑 ..\local-secrets\robot-vacuum.env，填入 DEEPSEEK_API_KEY
```

不要提交真实的 `.env`，也不要把密钥写进 artifact、实例配置或公开报告。

构建与启动使用仓库内源码和本地资产：

```powershell
$env:PYTHONPATH = "runtime"
python -m modules.artifacts build-project . --out ..\robot-vacuum-support-built --include-assets

python ..\robot-vacuum-support-built\robot_vacuum-v4-<id>\serve.py `
  --instance-config instance.json
```

`instance.example.json` 是占位模板；把 `artifact_root`、`storage_root`、`environment_file` 换成你机器上的仓库外绝对路径。产品默认监听 `127.0.0.1:8902`，身份接口 `GET /api/instance`，健康接口 `GET /api/health`。

没有知识原文、模型或索引时，离线结构测试仍可理解源码，但真实 RAG 问答和 live 评测会明确显示 `needs_configuration`，不会 mock 成功。

## 5. 父工作区链接的处理

旧文档里的以下链接属于原开发工作区，不随本仓库发布：

| 旧链接 | 当前处理 |
|---|---|
| `../AGENTS.md` | 父级发现指引，不发布；本仓库以根目录 `AGENTS.md` 为准 |
| `../plans/LONG_TERM_PLAN.md` | 父工作区进度权威，不发布；本仓库进度与边界以 README 和本文为准 |
| `../plans/岗位A项目边界与简历描述.md` | 父工作区内部材料，不发布 |
| `../my_agent_harness/...` | 主仓库证据，不发布 |
| `../robot-vacuum-support-built/...` | 本地构建输出，不发布 |
| `outputs/...`、`eval/results/...` | 本地证据，不发布 |

必要的入口已经改到仓库内文件；其余历史文档保留原始叙述，但不再把外部路径当作可工作的入口。

## 6. 凭据与个人数据

- 公开仓库不包含 `.env`、token、私钥或 API key 值。
- `tests/test_demo_readiness.py` 中的 `dummy-value-do-not-print` 是测试用假值，不是真实凭据。
- `eval/reviews/` 不发布；`docs/review-round-1-analysis.md` 和 `docs/approved-development-failure-analysis.md` 中逐字复现的个人审核 notes 已替换为 `[个人审核原文已从公开仓库移除]`，本地原记录保留。
- 文档中的 `C:/Users/...`、`E:/CODE/...` 是开发机历史路径，仅用于解释当时的证据位置；克隆后不要按这些路径运行。

## 7. 已知限制

- `eval/real_p1_probe.py` 是一次性历史探针，仍硬编码原开发机的 artifact/仓库/env 路径；公开副本保留它用于理解当时的验收方式，不应把它当作可移植入口。可移植的评测入口是 `eval/runner.py`，env 通过 `ROBOT_VACUUM_ENV_FILE` / `--environment-file` 等显式指定。
- 其他历史文档中的 `C:/Users/...`、`E:/CODE/...` 同样是原开发机路径，不是克隆后的运行说明。
- 不发布 40–60 集正式评测、holdout 结果、真实用户量、满意率或生产 SLA。
- 回充状态只覆盖单流程；引用检查主要是来源存在性，不声称语义正确率。
- 没有本地资产时不能完整运行 live RAG；这是有意的发布边界，不是代码缺陷。
- 本仓库不覆盖已有远端，也不包含任何 GitHub token 或登录状态。
