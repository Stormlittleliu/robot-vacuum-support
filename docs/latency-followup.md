# 时延收口：冷启动 import、provider 连接与 2-turn 真实集成

> 本文只收口已经完成的 measurement + integration，不重跑模型、不重建产物、不发新请求。  
> 结论先说：冷启动的大头是 **RAG 工具链首次 import（约 10s）**，不是 embedding 权重、不是 graph compile；模型配置 import 约 1s。provider trace 保留当前代理/SDK 路径，能观测连接与首 body chunk，但 `read_headers`/TTFB 是包含代理、远端排队与 compute 的 inclusive wait，**不能拆成纯网络**。本轮没有改依赖，**不能声称速度已优化**。

## 1. 一次 2-turn 真实集成看到什么

同一 session、同一 artifact、当前代理路径，1 cold + 1 warm，端口 8946；2/2 HTTP 200，结束后端口已释放。

| turn | 总耗时 | 首字可见 | 解释 | worker/场景初始化 | 主 agent run |
|---|---:|---:|---:|---:|---:|
| t1 cold | 20,033ms | 18,355ms | 3,581ms | 16,359ms | 6,091ms |
| t2 warm | 14,506ms | 11,375ms | 1,765ms | 12,687ms | 12,670ms |

冷 turn 的 16,359ms worker 段里：`scenario_resolve` 10,247ms（其中 `toolbox_import` 9,113ms、`model_import_config` 988ms），`main.run` 6,091ms（两次模型 4,210ms + 两个并行 `rag_retrieve` union 1,671ms + 210ms residual）。`bootstrap_import` 1,497ms 与解释阶段时间重叠，**不加入总耗时相加**。  
暖 turn 的 12,670ms 主 run 里：两次模型 12,167ms（1,343ms + 10,825ms），两个并行检索 union 488ms，residual 15ms。

这次 warm 明显高于旧 3-turn 的 4–5s，原因不是“产品变慢”结论：t2 最后一次 provider 请求的 `connection_setup_union` 为 6,982.875ms（6.98s），其中 origin `start_tls` 为 6,972.164ms（6.97s），随后 headers 370ms、body read 3,462ms、首 body chunk 7,356ms。它是 **n=1 请求内观察**，不能外推成网络质量或回归。  
t1 的 3 次 provider 请求 setup 分别为 156/194/204ms、`read_headers` 133/141/149ms；t2 为 149/121/6,983ms、`read_headers` 194/334/370ms。所有值都是当前代理路径的 inclusive 观测，不做纯网络拆分。

## 2. 时延来源：已证实与未证实

**已证实（startup profile，n=3 cold / n=6 warm）：**

- 冷 `scenario_resolve` 中位数 11,642ms；`toolbox_build` 10,459ms，其中 `toolbox_import` 10,351ms。
- `model_import_config` 1,166ms，其中 `model.load.chat` 构造仅约 111ms，主要是 `core.llm.factory` 的 import。
- `graph_compile` 1.4ms、spec 读取/校验约 1.3ms、Chroma open 约 86ms。
- 同一进程 warm 后 `scenario_resolve` 中位数 5.3ms，说明这笔等待每个 worker 首次付一次。
- importtime 传递链：`core.tools.rag.tools` → `langchain_classic.retrievers` → `langchain_text_splitters` → `sentence_transformers` / `torch` / `transformers`。当前路径构造 RAG 工具时**没有加载 embedding 权重**。

所以：原示例首请求 11.38s **不是全部 embedding**；它是 worker/场景首次初始化，主要是 RAG 模块 import。**不是 graph compile，也不是权重 load。**

**仍不能 claim：**

- 不能把 warm 的 14.5s 与旧 4–5s 直接比较成回归；不同 run、不同网络/TLS 状态，n 很小。
- 不能把 `read_headers` 115.9ms / 370ms 当成“纯网络 RTT”；它包含代理、远端排队、prefill、生成。
- 不能把 n=1 小请求的 setup≈120ms、headers≈238ms、body complete≈943ms、total≈948ms 外推为全用户网络已排除；它只证明当前代理路径可观测。
- 不能把 async/astream 算进覆盖；产品非流式路径没有纯 TTFT，first body chunk ≠ first token。

## 3. provider trace 与关联

- 实现保留 LangChain/OpenAI 已建的真实 sync client、proxy mounts、`trust_env`、TLS、连接池、timeout/retry/model 参数；只注入 `request.extensions["trace"]`，不替换 transport。
- 本次 6/6 provider records 有 trace 事件、6/6 观察到 proxy、6/6 HTTP 200；4/4 main-agent `model.call` 通过 `call_span_id` + `turn_id` 关联，2/2 interpretation 通过 `call_id`（`interpretation-1/2`）+ `server.interpretation` 关联。
- main-agent 的 `call_id` 当前为 null，未伪造；trace 的 4/4 `model_start.call_span_id` 匹配 `model.call` span。
- records 不写 question、请求头值、body、cookie 或凭据；6/6 redaction flags 均为 false。
- 已知测量缺口：4 条非流式 main-agent record 的 `body_complete=false`（body read 已捕获，记录器在 send 结束时先 finalize）；async 未覆盖。

## 4. 优化候选（本轮不做）

1. **把 `langchain_text_splitters` 的 import 延后到知识入库/split_text**：收益可能最大、复杂度中等；必须用同一 profile 脚本复测，未测前不报数字。
2. **用 lean RRF 替代 `EnsembleRetriever` 子类依赖，或固定/升级 `langchain_classic`**：可能移除 retriever 包级传递导入；复杂度中高，需要检索语义回归。

本轮不改依赖、不改 prompt/retriever/微服务，也不新增持久架构限制。是否需要做，等下一阶段用户决定。

## 5. 证据与边界

- 启动 profile：[report](standalone.md#未包含)、[doc](startup-profile.md)、raw `outputs/startup-profile/{summary.json,raw-spans.json,runs,spans,importtime.txt}`。
- provider 诊断：[report](standalone.md#未包含)、[doc](provider-transport-diagnosis.md)、raw `outputs/provider-transport/{real-request.json,records/records.json,records-corrected.json}`。
- 2-turn 集成：[run](standalone.md#未包含)、[raw](standalone.md#未包含)、[verification](standalone.md#未包含)；artifact `robot_vacuum-v4-ef02cc720e`，外部 cwd `run.py --check` 通过。
- 旧 3-turn 证据仍保留在 `outputs/latency-diagnosis/`；本轮独立 `--out`，未覆盖旧 raw。报告与 doc 若数字来自不同 run，以对应 run 路径和 raw 为准。
- 未改：case32/reviews/assets/hash、GUI 8950、proxy/SSL/timeout/modelparams；未跑全仓、24 cases、holdout。
- 测试：本轮不重跑；主模型工具实际执行 `python -m pytest tests/test_startup_profile.py tests/test_provider_transport.py tests/test_latency_spans.py -q`，输出 `20 passed in 2.57s`，无 subtests；无持久化 pytest log，仅有工具执行记录，不编造 log。

## 6. 下一主线

诊断到这里可交付收口，不再把更多 profiling/instrumentation 当作求职主线前置。下一最优主线回到 A 项目 approved development：24-case 回答人工审核 + 8 条 failure 归因；其中 6 条是 fixture variant，不等于 6 个产品 bug。是否补 holdout/修 fixture 历史注入，等人工审核与归因结果再决定。
