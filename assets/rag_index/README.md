# 本地 Chroma 索引（本地提供）

公开仓库**不包含**向量索引。运行时默认读取：

```text
assets/rag_index/
  chroma.sqlite3
  <collection-segment>/
    data_level0.bin
    header.bin
    length.bin
    link_lists.bin
```

## 当前索引参数

| 项 | 值 |
|---|---|
| collection | `agent` |
| embedding 维度 | 512 |
| 向量数 | 823 |
| 知识文件 | 6 份（见 `assets/knowledge/README.md`） |
| `chroma.sqlite3` SHA-256 | `6cfd9514f8cb840367d0da3f479681e645e2f9d3b0b6c29809d6ec2ef0c4aa05` |

索引由 6 份知识原文和本地 `bge-small-zh-v1.5` 生成。实例启动时会把该目录复制到会话私有 `workspace/chroma_db`，PDF 的编号条目覆盖只写私有副本，不回写这里。

## 恢复与重建

- **推荐**：从你自己的可信本地备份恢复整个 `assets/rag_index/`，并核对 `chroma.sqlite3` 的 SHA-256。
- 本仓库没有发布独立的“一键重建索引”命令行入口；重建需要复用 `runtime/core/tools/rag/` 的解析、分块和 embedding 逻辑，并保持 collection 名与 512 维一致。
- 索引与知识原文不匹配会导致检索证据和评测结果漂移。若替换了知识文件，应同时重建索引，不要只替换其中一边。

详见 [../../docs/standalone.md](../../docs/standalone.md)。
