# 本地资产说明

本目录的三类资产都**不随公开 Git 仓库分发**：

| 目录 | 内容 | 为什么不入库 | 恢复方式 |
|---|---|---|---|
| `knowledge/` | 6 份扫地/扫拖机器人资料（txt + pdf） | 未找到明确的再分发授权 | 由使用者提供有权的原文，文件名和校验值见 [knowledge/README.md](knowledge/README.md) |
| `models/` | `bge-small-zh-v1.5` 本地 embedding 模型 | 约 92 MB，属于可再下载的大文件 | 从模型官方仓库下载，见 [models/README.md](models/README.md) |
| `rag_index/` | 823 条向量的 Chroma 索引 | 派生自知识原文，且属于可再生成资产 | 从可信本地备份恢复，或按 [rag_index/README.md](rag_index/README.md) 的约束重建 |

`project.json` 和实例启动逻辑仍按 `assets/knowledge`、`assets/models`、`assets/rag_index` 三个路径读取。克隆后只要在本地放回这些目录，代码无需改动。

> 公开仓库只保留源码、测试、文档、配置模板和体积较小的评测数据。`outputs/`、`eval/results/`、`eval/reviews/` 等运行证据与个人审核原文不发布，详见 [../docs/standalone.md](../docs/standalone.md)。
