# 本地 embedding 模型（本地提供）

公开仓库**不包含**模型权重。运行时默认读取：

```text
assets/models/bge-small-zh-v1.5/
```

## 期望内容

模型为 `BAAI/bge-small-zh-v1.5`（sentence-transformers 格式，512 维）。至少需要：

```text
bge-small-zh-v1.5/
  config.json
  model.safetensors
  tokenizer.json
  tokenizer_config.json
  special_tokens_map.json
  vocab.txt
  modules.json
  sentence_bert_config.json
  config_sentence_transformers.json
  1_Pooling/config.json
```

本工程开发机上使用的关键文件校验值：

| 文件 | 字节数 | SHA-256 |
|---|---:|---|
| `model.safetensors` | 95827648 | `354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026` |
| `config.json` | 776 | `3853a7979202c348751b753e36f579c41d8da7d36af617d3d907e1fc9b441f2a` |
| `tokenizer.json` | 439125 | `48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26` |

## 获取方式

从模型官方仓库或你已有的可信模型缓存复制到上面的目录。示例（需要本机已安装对应下载工具，且遵守模型上游许可）：

```powershell
# 任选一种你环境里可用的方式；命令只是示例，不是本仓库的运行时依赖
huggingface-cli download BAAI/bge-small-zh-v1.5 `
  --local-dir assets/models/bge-small-zh-v1.5
```

换模型时必须保持 embedding 维度与索引一致。当前索引为 **512 维**；维度不一致时实例会拒绝使用旧索引。
