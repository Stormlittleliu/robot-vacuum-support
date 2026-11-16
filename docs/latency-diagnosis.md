# 时延诊断：20 秒到底花在哪

> 本轮只做可归因观测与一次小规模真实诊断；没有优化 prompt/产品行为，没有重跑 24 cases，没有跑 holdout，没有动 GUI 8950、reviews、冻结数据或 assets。  
> 结论先说：**不是“20 秒都是模型慢”，也不是“用户网络差”一句话能解释。** 冷启动首轮的最大已测项是本地 worker/场景初始化；暖启动的主要已测项是串行模型请求。provider 网络/远端排队/生成在当前代理环境下不可拆，因此**网络没有被排除**。

## 1. 证据范围

| 证据 | 内容 | 边界 |
|---|---|---|
| 旧 approved development run | `v1-live-development-20261113-224057-3f8412`，24 QA / 35 turns，p50 20,318.92ms，p95 30,919.96ms | 旧 run 无 client/server/worker span、无 provider transport；只能从 trace 与 `turn_metrics` 反推 |
| 旧 run 归因文件 | `outputs/latency-existing-attribution.json` | cold/warm 是 `pre_interpretation_gap > 1000ms` 的代理分类，不是原始 cold 标记 |
| 本轮真实诊断 | `outputs/latency-diagnosis/run.json`，同 session 同问题 1 cold + 2 warm，端口 8945，单并发，3/3 HTTP 200、0 model/transport error | **n=3 过程样本，不报 p95，不冒充分布** |
| 原始 span | `outputs/latency-diagnosis/raw-spans.json`，client/server/worker 共 126 条，已过滤掉同目录早先失败尝试的 stale span | provider span 不可用（代理环境，安全起见拒绝 custom transport） |
| 汇总 | `outputs/latency-diagnosis/summary.json` | parent inclusive，child 用 union/exclusive；并行/嵌套不重复相加 |

本轮诊断 artifact：`robot_vacuum-v4-a6915e9e7d`（从当前 source 构建，旧 artifact 未覆盖）。  
Span 版本：`latency-span/v1`，meter `rvs-latency-meter/2026-11-13`；monotonic 是本进程时钟，跨进程排序用 wall anchor，duration 用 monotonic。

## 2. 三个真实样本的阶段表（毫秒）

同问同 session，`X-RVS-Cold` 分别为 1、0、0；turns 2–3 复用同一 worker，无重启。

| 样本 | 冷/暖 | 总 | 首字可见 delta | 语义解释 | 本地初始化 | 本地检索 | 主 agent 模型请求 | RAG summary | 本地尾部 | 远端模型请求合计（解释+主，串行） | 本地剩余 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| turn1 | cold | 20,482.975 | 18,812.557 | 3,524.659 | **11,376.939**（scenario resolve） | 1,763.237 | 3,484.890（2 次：1,122.725 + 2,362.165） | N/A（0 次） | 7.380 | 7,009.549 | ~326 |
| turn2 | warm | 4,162.203 | 2,295.476 | 1,577.309 | 2.375 | 0（无工具） | 2,491.416（1 次） | N/A（0 次） | 5.100 | 4,068.725 | ~87 |
| turn3 | warm | 5,354.871 | 3,671.435 | 1,459.050 | 2.277 | 254.859 | 3,565.540（2 次：1,094.130 + 2,471.410） | N/A（0 次） | 6.630 | 5,024.590 | ~66 |

读法：

- **总/首字**是 client 观测；首字可见 delta 是 SSE 的 `answer_delta`，**不是 provider 首 token**。本轮非流式 provider 路径没有 TTFT。冷样本的 18.8s 首字也不是“模型等了 18.8s 才回”，它前面已经发生了本地初始化、解释、模型请求 1 和检索；这正好说明不能只看首字就把责任归给某个单层。
- **语义解释**是 server 进程里一次 structured interpretation 模型请求的 inclusive 时间。
- **本地初始化**是 `worker.scenario_resolve` inclusive。它内部单独测到 `rag_service_init=84.734ms`、`chroma_open=65.890ms`、`chat model load≈98.5/107.2ms`；剩下的约 11.2s 在这轮没有更细 span，**不能全归 embedding 或全归 Chroma**。
- **本地检索**是 retrieval total union：turn1 vector 792.687 + BM25 965.848，RRF 1,762.601 与两者重叠，**不再相加**；turn3 vector 14.425 + BM25 237.884，RRF 254.445 重叠。turn1 的 embedding model load 483.554ms 是本地 CPU，两次并行加载都计在 union 里。
- **主 agent 模型请求**列是串行请求之和；它和 main run 不是同一层。turn1 的 main run 5,468.294ms 是 inclusive，里面已包含 3,484.890ms 模型请求和 1,768.962ms 工具 union，不能再把 3,484 与 5,468 相加。
- **远端模型请求合计**是解释 + 主请求串行之和（turn1 3 次、turn2 2 次、turn3 3 次）。这个数**包含**网络、远端排队、预填充、生成，**不能**拆成“纯推理”或“纯公网延迟”。
- **本地剩余**是总时间减去上面已列阶段后的残差（main/worker/server 的少量 exclusive、client/HTTP 开销等），不是新的“未知网络”。

## 3. 主因排序（按本轮可直接测到的量）

### 冷启动 turn1（20.483s）

1. **本地 worker/场景初始化 11.377s，约 55.6%。** 这是 cold/warm 最大差异：turn2/3 同阶段只有 2.375/2.277ms。
2. **语义解释模型请求 3.525s，约 17.2%。** 已测 inclusive；内部网络/远端不可拆。
3. **主 agent 串行模型请求 3.485s，约 17.0%。** 两次请求；不是一次 20s 的调用。
4. **本地检索 1.763s，约 8.6%。** vector + BM25 + RRF，RRF 重叠不重复加。
5. **本地尾部约 7ms。** 持久化/SSE 不是主因。

### 暖启动 turn2/turn3（4.162s / 5.355s）

1. **主 agent 模型请求 2.491s / 3.566s，约 59.8% / 66.6%。** turn2 只有 1 次，turn3 是 2 次串行；这是“单次调用慢 + 串行次数”混合，不能只归一个。
2. **语义解释 1.577s / 1.459s，约 37.9% / 27.2%。** 每轮固定一次。
3. **本地检索 turn3 0.255s，约 4.8%；turn2 无工具。**
4. **本地初始化降到 2.4/2.3ms，几乎消失。**
5. **尾部 5–7ms。**

因此：

- **“串行多 LLM 次数”**：turn1/turn3 各有 2 次主请求，turn2 1 次；模型请求之和是真实的，但 main run 是它们的 inclusive 父层。
- **“单个调用慢”**：本轮没有单个 20s 的主调用；最大单次约 2.49s。20s 冷样本主要是本地初始化 + 解释 + 主请求的组合。
- **“本地加载慢”**：turn1 的 11.38s 是明确证据；但更细的 import/spec/tool build 还没有拆开，不能把 11.38s 全部叫“embedding 加载”。

## 4. 已测 vs 不可拆 vs 未知

### 已测

- server receive / reserve / seed / spawn；
- interpretation inclusive；
- worker execution、scenario resolve、workflow build；
- RAG service init、Chroma open；
- vector / BM25 / RRF / embedding query / embedding model load；
- main run、每次 model request、每次 tool call、tool union、main exclusive；
- postprocess、persist、answer enqueue、SSE first byte / done；
- client headers、first SSE line、first answer delta、answer event、done。

### 已测但不可拆内部

- **每个模型请求**：包含 DNS/TCP/TLS/send、provider 排队、prefill、生成；当前 provider transport 不可用，不能拆。
- **scenario resolve 11.38s**：只拆出 84.7ms RAG init、65.9ms Chroma open、约 98.5ms chat model load；其余是未细分的 import/spec/tool 构建。
- **RAG summary**：本轮 0 次，N/A；旧 run 8 次，但 tool total 与 tool-internal model latency 之间仍不能全归网络或全归本地。

### 未知

- provider 网络/远端排队/生成在模型请求内各占多少；
- 用户网络差对 main 请求的实际影响；
- 11.38s scenario resolve 的完整构成；
- provider TTFT（本轮没有）；
- 旧 35-turn 的 cold residual 是否都能用本轮同样的 11.38s 解释（n=3 不支持外推）。

## 5. 为什么不能减 ping、不能抓包、不能甩锅网络

- 当前环境有 proxy；自定义 httpx transport 会绕过 httpx 的代理自动发现，等于改变用户代理路径。因此 provider transport hook **主动标记 unavailable**，没有关代理、没有改 SSL、没有抓包。
- 没有用“ping RTT 减一减”猜请求网络耗时；provider 首包等待包含远端排队/预填充/生成，与 ICMP RTT 不是同一件事。
- 本轮可确定的是：**冷样本里本地初始化很大**；不能确定的是：**模型请求里的网络部分有多大**。所以报告不写“网络已排除”。

## 6. 复现命令

```powershell
# 1) 旧 approved run 的离线归因（不调用模型）
python scripts/latency_existing_analysis.py --run eval/results/v1-live-development-20261113-224057-3f8412 --out outputs/latency-existing-attribution.json

# 2) 一次小规模真实诊断：构建新 artifact，端口 8945，1 cold + 2 warm，结束自动关闭
python scripts/run_latency_diagnosis.py

# 3) 只汇总已完成的 3 个样本（不调用模型）
python scripts/summarize_latency_diagnosis.py > outputs/latency-diagnosis/summary.json
```

测试（本轮已跑，未重跑全库）：

```powershell
python -m pytest tests/test_latency_spans.py -q
# 5 passed
python -m pytest tests/test_p1_recharge.py tests/development-next/test_question_lifecycle.py tests/test_context_diagnosis.py -q
# 71 passed, 24 subtests passed
```

## 7. 下一步定位（不先优化）

1. 在 `worker.scenario_resolve` 内加 import/spec/tool-build 子 span，拆开未测的约 11.2s；这是目前最大的冷启动盲区。
2. 在无代理路径或能保留代理的 SDK 级 trace 上补 DNS/TCP/TLS/headers/first chunk；否则保持 unavailable。
3. 用 2–3 个非隐私问题重复 1 cold + 2 warm，确认 11.38s 是否稳定；n=3 不够下分布结论。
4. 如果产品主路径以后改成真正 token streaming，再把 provider TTFT 与 SSE first delta 分开记录。
