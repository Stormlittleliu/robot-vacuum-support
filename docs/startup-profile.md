# 启动 profile：`worker.scenario_resolve` 的 11.6s 拆到导入层

> 本轮只做**离线启动 profile**：不调用 provider、不调用 LLM、不执行工具/检索、不重跑 24 cases/holdout、不启动 GUI/服务、不构建全局最终 artifact。  
> 结论先说：冷启动的大头不是 embedding，也不是 spec 读取或 graph compile，而是 **RAG 工具模块首次 import**（中位数约 10.35s）。`toolbox_construct`（含 RAG service init/Chroma open）只有约 0.11s。同进程 warm 后 `scenario_resolve` 中位数约 5.3ms，说明这笔等待只在每个 worker 的首次场景构建付一次，不是每轮都付。

## 1. 范围与边界

| 项 | 本轮 |
|---|---|
| 测量对象 | `worker.scenario_resolve`（`get_scenario` + `resolve_execution`）与离线等价的 `WorkflowRuntime` 初始化 |
| 样本 | 3 个 fresh Python process；每个进程 1 cold + 2 同进程 warm（n=3 cold / n=6 warm） |
| provider / LLM | **0 次**调用；模型只构造，不发请求 |
| 工具 / 检索 | **0 次**调用；`RagSummarizeService` 构造时打开 Chroma，但不查询、不加载 embedding 权重 |
| 数据 / 产物 | 只写 `outputs/startup-profile/**`、`outputs/startup-profile-report.json`、`docs/startup-profile.md`；未改 source assets、32 reviews、v1.1 data、holdout、旧 artifact、旧 raw 证据、GUI 8950 |
| 网络 / 代理 | 未改系统代理/SSL/网络配置；仅在 child 进程内按 `worker._safe_no_proxy` 的同一规则清掉会破坏 httpx 的 bracketed IPv6 `NO_PROXY` 项 |
| 进程 | 所有子进程隐藏窗口（Windows `CREATE_NO_WINDOW`）；未使用 WMI 脱离进程树方法 |
| 日志 | 不记录 body、prompt、headers、cookie、query、凭据或 env 值；只记录模块名/阶段名/耗时 |

## 2. Cold / warm 定义

- **cold**：fresh Python process 里的第一次 scenario build；模块缓存为空、toolbox `lru_cache` 为空、worker 未复用。
- **warm**：同一进程里的第二/第三次 build；模块缓存和 toolbox 缓存已热，worker 复用；**agent history / mutable graph 不跨进程共享**。
- **OS file cache**：没有 drop page cache（需要管理员权限且会污染用户环境）。3 个 fresh process 按 run1→run3 的顺序只能作为 OS cache 的粗略代理，不是严格受控变量。
- worker 的隔离模型是**一个 session 一个 worker 进程**；`get_scenario` 不是跨用户共享的全局 state。测试里也验证了 toolbox 缓存是进程内 `lru_cache`，不是跨进程共享存储。

## 3. 方法

### 3.1 真实子 span

只在允许文件内加 span，未改公共 span 格式、prompt、budget、state、tool schema/name：

| 文件 | span |
|---|---|
| `runtime/core/scenarios/registry.py` | `worker.scenario_lookup`、`worker.scenario_module_import` |
| `runtime/core/scenarios/generic.py` | `worker.model_import_config`、`worker.spec_read_validate`、`worker.toolbox_build`、`worker.prompt_book_load`、`worker.scenario_assemble` |
| `runtime/core/scenarios/pools.py` | `worker.toolbox_factory`、`worker.toolbox_import`、`worker.toolbox_construct` |
| `runtime/modules/runtime/compiler.py` | `worker.graph_compile`、`worker.graph_compile.workflow` |
| `runtime/modules/instances/runtime/execution.py` | `worker.scenario_resolve_execution` |
| `runtime/modules/instances/runtime/worker.py` | `worker.bootstrap_import`（eager 顶层 import，独立于首次 request） |

`worker.scenario_resolve` 的直接子阶段是**顺序**的；表中相加后再加 exclusive residual 才能还原 parent。`toolbox_import` / `toolbox_construct` / `rag_service_init` / `chroma_open` 是嵌套行，**不能**再和直接子阶段相加。

### 3.2 `-X importtime`

对一个 fresh process 的首次导入路径跑一次 `-X importtime`（`outputs/startup-profile/importtime.txt`）。解析时 **self 与 cumulative 分列**，同名模块取最大行；不把嵌套 cumulative 相加。eager 顶层导入（`core.scenarios.registry`、`modules.runtime.workflow`）另用 `firstimport` wrapper 记录，并明确标 `cold=True`。

### 3.3 trace-off 对照

用同一个 profile 脚本跑 `--no-trace` 3 个 fresh process，比较 span 写入开销。这不是产品优化，只是确认观测本身没有把数字改坏。

## 4. 结果

### 4.1 cold 三次与 warm 六次（毫秒）

| 样本 | cold `scenario_resolve` | cold total init | warm `scenario_resolve` | warm total init |
|---|---:|---:|---:|---:|
| run1 | 11,642.339 | 11,645.271* | — | — |
| run2 | 11,336.982 | 11,340.331 | — | — |
| run3 | 11,670.595 | 11,674.793 | — | — |
| median | **11,642.339** | **11,645.271** | **5.259** | **7.141** |
| range | 11,336.982–11,670.595 | 11,340.331–11,674.793 | 4.473–5.995 | 6.295–7.861 |

\* 表中 total init 是该 run 的实测值；median 行取 3 次 median。`total init = scenario_resolve + workflow_build`，warm 的 workflow build 中位数 1.79ms，cold 为 3.35ms。

### 4.2 cold `scenario_resolve` 的直接子阶段（中位数，n=3）

| 子阶段 | 中位数 ms | 范围 ms | 读法 |
|---|---:|---:|---|
| `worker.toolbox_build` | **10,458.590** | 10,134.016–10,483.156 | 最大项，约 **89.8%** |
| `worker.model_import_config` | **1,165.650** | 1,160.824–1,180.482 | 第二项，约 10.0% |
| `worker.scenario_module_import` | 10.011 | 9.455–10.020 | `core.scenarios.generic` 首次 import |
| `worker.scenario_lookup` | 8.027 | 7.853–8.546 | 扫 spec 目录 |
| `worker.spec_read_validate` | 1.336 | 1.322–1.535 | 读 active spec + pydantic 校验 |
| `worker.prompt_book_load` | 0.164 | 0.138–0.170 | prompt book |
| `worker.scenario_assemble` | 0.429 | 0.377–0.540 | `build_from_spec` |
| `worker.scenario_resolve_execution` | 0.005 | 0.004–0.006 | `resolve_execution` |
| exclusive residual | 2.436 | 2.305–2.519 | 子阶段外的少量开销 |

直接子阶段相加约 11,640ms，加 residual 还原 parent 的 11,642ms，**没有把嵌套行重复相加**。

### 4.3 `toolbox_build` 内部（中位数，n=3）

| 嵌套阶段 | 中位数 ms | 范围 ms | 说明 |
|---|---:|---:|---|
| `worker.toolbox_import` | **10,351.223** | 10,017.429–10,362.361 | `core.tools.rag.tools` 首次 import |
| `worker.toolbox_construct` | 111.457 | 101.789–115.281 | 构造工具盒 |
| `worker.rag_service_init` | 105.753 | 94.659–109.256 | 构造 RAG service/retriever |
| `retrieval.chroma_open` | 85.858 | 73.566–86.512 | 打开 Chroma |

所以之前单测到的 `rag_service_init≈85ms`、`chroma_open≈66ms` 只是 **construct 的一小段**；11s 的主体在 import，且不是 embedding 权重加载（本次没有 query，`embedding.model_load` 没有被调用）。

### 4.4 模型配置内部

`worker.model_import_config` 中位数 1,165.650ms；其中真正的 `model.load.chat` 构造中位数 110.661ms。也就是说约 1.05s 花在 `core.llm.factory` 的 import，而不是构造 `ChatOpenAI`。

### 4.5 eager 顶层 import（不放进 `scenario_resolve`）

worker 在 `worker.ready` 之前会 import `execution -> workflow -> compiler/langgraph`。单独一次 fresh process 实测：

- `worker.bootstrap_import`：**1,606.472ms**（一个辅助样本；不是 n=3）。
- 其中 `modules.runtime.workflow` first import：1,421–1,442ms；`core.scenarios.registry`：24.7–27.9ms。

这笔约 1.6s 与 `scenario_resolve` 是**顺序**的，不能相加到 parent 里冒充同一层；`worker.bootstrap_import` 已加到 worker 的真实 span 中，下一步集成时可以直接从 raw spans 读。

## 5. `-X importtime` 证据

关键模块（cumulative，单位 ms；**嵌套 cumulative 不可相加**）：

| 模块 | cumulative | 说明 |
|---|---:|---|
| `core.tools.rag.tools` | **10,142.645** | `toolbox_import` 的主体 |
| `core.tools.rag.service` | 10,140.408 | 被 tools import 拉起 |
| `core.tools.rag.retrievers` | 10,088.184 | 被 service import 拉起 |
| `langchain_classic.retrievers` | 9,356.530 | retriever 包 `__init__` 的传递导入 |
| `langchain_text_splitters` | 9,243.224 | 其 `__init__` 导入 sentence_transformers |
| `sentence_transformers` | 7,268.207 | 当前检索路径构造时用不到 |
| `torch` | 2,182.509 | sentence_transformers 的传递依赖 |
| `transformers` | 1,884.927 | 同上 |
| `core.llm.factory` | 1,170.695 | 模型配置 import |
| `modules.runtime.workflow` | 1,459.869 | eager 顶层 import，不进 scenario_resolve |

top self 的前几项是 `transformers`（1,293ms）、`torch`（308ms）、`torch.fx...`（408ms）等；它们与 cumulative 是不同视图，**没有把 self 当总时间，也没有把嵌套 cumulative 相加**。

## 6. 解读

1. **不是 embedding**：本次没有 query，`embedding.model_load` 未触发；`chroma_open` 只有约 86ms。之前“embedding 483ms”是另一条检索路径的实测，不是这 11.6s 的主体。
2. **不是 spec 读取/校验**：`spec_read_validate` 约 1.3ms，`scenario_lookup` 约 8ms，`prompt_book_load`/`scenario_assemble` 都小于 1ms。
3. **不是 graph compile**：`worker.graph_compile` 中位数 1.38ms，`workflow_build` 中位数 3.35ms；图编译不是启动瓶颈。
4. **是首次 import**：`toolbox_build` 里 10.35s 是 `core.tools.rag.tools` 首次导入，传递拉起 `langchain_classic.retrievers` / `langchain_text_splitters` / `sentence_transformers` / `torch/transformers`。这些模块在“构造 RAG 工具”时被导入，但当前路径真正要用的只是 retriever/BM25 组合；sentence-transformers 权重直到 query 才会用。
5. **warm 后消失**：同进程 warm 的 `scenario_resolve` 中位数 5.26ms，`toolbox_build` 约 0.009ms（lru_cache 命中），`model_import_config` 约 0.007ms。这解释了旧诊断里 cold 11.38s / warm 2.3ms 的量级差。
6. **与旧 11.38s 的关系**：旧 artifact 的 `scenario_resolve=11,376.939ms`，本轮 source tree profile 中位数 11,642.339ms，差约 +265ms（+2.3%）。这是不同 run、source tree vs built artifact、OS file cache 状态不同造成的量级一致，不是“优化后变快/变慢”的结论，n=3 也不能说统计显著。

## 7. 浪费定位与为什么本轮没有改

**结论：没有找到可以在本 slice 写权限内安全消除的浪费。** spec 引用的 `rag_summarize` / `rag_retrieve` 都在 `rag` 工具盒里，两个都是可用工具；把工具构造改成“首次调用再 import”只会把 10s 从 `scenario_resolve` 挪到第一次 tool call，**总等待不变**，还会增加 schema/语义漂移风险。因此本轮没有做 lazy tool 化，也没有做“预热后声称总延迟降低”。

最大耗时触发点在无写权限文件。以下是精确路径和建议，交给主模型集成，不在本 slice 越界修改：

1. **`runtime/core/tools/rag/retrievers.py:14`**
   `from langchain_classic.retrievers import EnsembleRetriever` 会执行 `langchain_classic.retrievers` 包 `__init__`，传递拉起 `langchain_text_splitters.sentence_transformers`。  
   **建议**：用本地 lean RRF 实现替代 `EnsembleRetriever` 子类依赖，或固定/升级 `langchain_classic` 到不会 eager import sentence-transformers 的版本。  
   **已验证**：直接改从 `langchain_classic.retrievers.ensemble` 导入仍会执行包 `__init__`，单独测约 10.8s，所以“换一个子模块导入”不够。

2. **`runtime/core/tools/rag/vector_store.py:15`**
   `from langchain_text_splitters import RecursiveCharacterTextSplitter` 触发该包 `__init__`，其 `sentence_transformers` 子模块约 7.3s。  
   **建议**：把 splitter import 延后到 `_get_splitter` / `split_text`（只在知识入库时需要，检索不需要）；注意 normal submodule import 仍会跑包 `__init__`，所以“延后”比“换路径”更可靠。`vector_store` 里构造 Chroma 是 construct 的约 0.1s，不是 10s 主体。

3. **`runtime/core/llm/factory.py:5`**
   `from langchain_community.chat_models.tongyi import BaseChatModel, ChatTongyi` 在 `chat_provider=deepseek` 时用不到；`core.llm.factory` cumulative 约 1.17s，其中 `langchain_openai` 包 `__init__` 约 0.9s。  
   **建议**：把 `ChatTongyi` 放到 dashscope 分支内 lazy import；评估从更窄模块导入 `ChatOpenAI`。该文件属于另一并行任务，本 slice 未改。

**不要做**：不要为了“总延迟降低”把等待预热到 session create；如果以后做预热，必须同时统计 create + first reply 的总耗时和资源占用，并且不共享 agent history / mutable graph。不要引入 service pool 或大重构。

## 8. trace-off 对照与观测开销

| 条件 | cold `scenario_resolve` 中位数 | warm `scenario_resolve` 中位数 |
|---|---:|---:|
| trace on | 11,642.339 | 5.259 |
| trace off | 11,332.174 | 2.877 |
| 差 | +310.165 | +2.382 |

warm 的 +2.4ms 是 span 写入的可见开销；cold 的 +310ms 与单次 run 的 11.25–11.81s 波动重叠，n=3 不足以把它归因于观测。**没有证据表明观测把 11s 量级改坏**。

## 9. 测试

本轮只跑 targeted tests，未跑全库、24 cases、holdout、LLM 全链：

```powershell
python -m pytest tests/test_startup_profile.py -q
# 7 passed in 0.73s
```

覆盖：

- scenario build 发出真实子 span，且 model/tool handoff 不变（source compat）；
- delayed `core.llm.factory` `ImportError` 正常向上抛出，不被吞掉；
- `get_scenario` 不 memoize，两次 build 返回不同对象；
- toolbox 缓存是进程内 `lru_cache`，可显式 clear，不是跨进程共享 state；
- 两个 `WorkflowRuntime` 得到不同 graph 对象，mutable graph 不跨 runtime 共享；
- `worker.bootstrap_import` 是独立顶层 span；
- `-X importtime` parser 的 self / cumulative 分离，不把嵌套行相加。

## 10. 未知与未归因

- 同 import 路径在 fresh process 间仍有约 0.3–0.6s 波动，可能来自 OS file cache、磁盘、杀软、CPU；n=3 分不开。
- `worker.bootstrap_import` 只有 1 个辅助样本（1.61s），不是 n=3；解释器启动、site/sitecustomize 时间也未单独测。
- 只测了 `robot_vacuum` 的 `rag` 工具盒；office/coding/research/mcp 的冷启动 import 结构未测。
- 没有跑端到端 cold turn（interpretation + main agent + provider），所以不能从本 profile 推到“用户总延迟下降”；provider 等待仍包含网络和远端，不可拆。
- 如果集成时改了非自有文件，需要重新用同一脚本、同一 cold 定义复测；本次没有做“改后”产品结论。

## 11. 复现

```powershell
# 3 个 fresh process；每个 1 cold + 2 warm；含 importtime + bootstrap 辅助样本
python scripts/profile_startup.py --runs 3 --warm 2

# 只跑 trace-off 对照（不覆盖主报告）
python scripts/profile_startup.py --runs 3 --warm 2 --skip-importtime --no-trace `
  --out outputs/startup-profile/control `
  --report outputs/startup-profile/control/report.json
```

产物：

- `outputs/startup-profile-report.json`（本报告 JSON）
- `outputs/startup-profile/summary.json`（记录/阶段/嵌套）
- `outputs/startup-profile/raw-spans.json`（本轮 span 汇总）
- `outputs/startup-profile/importtime.txt`（原始 importtime）
- `outputs/startup-profile/runs/`（每个 fresh process 的 child result/stderr）
- `outputs/startup-profile/spans/`（每个 run 的原始 span JSONL）
- `outputs/startup-profile/control/`（trace-off 对照）

## 12. 与并行任务的边界

本 slice 未写 `runtime/core/llm/factory.py`、`runtime/core/llm/port.py`、`runtime/core/observability/latency.py`；上述 factory/RAG 建议只写进报告，交由主模型集成。本报告是 slice 进度报告，不是全局最终产物；全链验证等集成统一做。
