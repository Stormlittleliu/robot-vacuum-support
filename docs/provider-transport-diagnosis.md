# Provider 连接观测：保持现有代理路径

> 本轮只补 provider 连接观测，不优化产品、不重跑 24 cases、不跑 holdout、不构建全 agent 产物、不碰 GUI 8950、不改系统代理/SSL/网络设置、不改 source assets / 32 reviews / v1.1 / 旧产物。
>
> 结论先说：**可以在当前代理配置上观测 provider 连接事件。** 旧的“proxy 存在就拒绝 custom transport”结论只说明不能替换 transport；本轮用 `request.extensions["trace"]` 注入到 SDK 正常 client，保留了 `trust_env`、proxy mounts、TLS 校验和连接池。一次真实小请求成功（HTTP 200），拿到了代理 CONNECT/TLS、provider 发送/读取 headers、首 body chunk 等事件。**但 `read_headers` 仍是一个不可拆的 inclusive wait；首 body chunk 不是首 token，TTFT 仍 unavailable；n=1 不是分布，也不能外推到 20s 全链路。**

## 1. 证据范围

| 证据 | 内容 | 边界 |
|---|---|---|
| 安装版本 | `httpx 0.28.1`、`httpcore 1.0.9`、`httpx2 2.13.1`、`httpcore2 2.13.1`、`openai 3.37.0`、`langchain_openai 1.7.2` | 只对本机安装版本负责 |
| 实际 SDK 路径 | `openai 3.37` 使用 `httpx2.Client`/`httpcore2`；LangChain `ChatOpenAI` 默认 sync client 是 `openai.DefaultHttpxClient`（`httpx2.Client`） | 不是旧 `httpx` 路径；旧 `httpx` 仅作 fallback |
| 离线 preflight | `outputs/provider-transport/preflight.json`，所有 critical checks 通过 | 不发请求；只构造 client/request |
| 一次真实小请求 | `outputs/provider-transport/real-request.json`（原始）与 `real-request-corrected.json`（同一请求的离线校正） | n=1；只验证 hook 和事件，不是 agent 全链路 |
| 原始事件 | `outputs/provider-transport/records/records.json` / `records.jsonl`；校正视图 `records-corrected.json` | 原始文件保留；校正只重排同一请求的 start/complete，没有第二次 live 请求 |
| 目标测试 | `tests/test_provider_transport.py` + `tests/test_latency_spans.py`，13 passed | 本地 mock transport，不发真实网络 |

## 2. 注入方式：不替换 transport

### 2.1 旧实现的问题

旧的 `runtime/core/observability/provider_trace.py` 在检测到 `HTTP_PROXY/HTTPS_PROXY` 时直接返回 `unavailable_proxy_present`，理由是 custom `httpx` transport 会绕过 proxy 自动发现。这个理由对“替换 transport”是成立的，但它也把 provider 阶段永久留成了盲区。

### 2.2 本轮做法

新实现 `runtime/core/observability/provider_transport.py`：

1. LangChain/OpenAI 先按正常路径构造 sync client（包括 socket options、proxy mounts、`trust_env`、TLS 校验、连接池）。
2. `instrument_client()` 只在**已经构造好的真实 client 实例**上包装：
   - `build_request`：保留原 request，再注入 `request.extensions["trace"]`；
   - `send`：只记录 attempt 边界，原样调用原 `send`；
   - 一个 response hook：只读 status code、包装 response stream 以记录首 body chunk / close。
3. 不传 `http_client`，不创建第二个 transport，不改 `mounts`，不改 `trust_env`，不改 timeout/retry/model 参数。
4. 如果已有 `request.extensions["trace"]`，先调用原 extension，再调用本记录器；不覆盖原 trace。

`httpcore2` 的 trace 事件会沿 `ConnectionPool`/`HTTPProxy`/`HTTP11Connection` 传递，所以能看到 proxy CONNECT、proxy TLS、origin TLS、provider send/read headers、body read 等事件。

### 2.3 Scope

- **sync**：覆盖 `ChatOpenAI.invoke` 的产品路径。
- **async**：本轮不覆盖 `AsyncClient`/`astream`；不假装已测。
- **streaming/TTFT**：产品路径是非流式 `invoke`，首 body chunk 不是首 token；`ttft_available=false`。

### 2.4 一个必须说明的本机环境处理

本机 `NO_PROXY` 含一个带方括号的 IPv6 token（`[::1]` 形式）。`httpx 0.28.1`/`httpx2 2.13.1` 会把它拼成非法 URL pattern，构造 client 时抛 `InvalidURL: Invalid port: ':1]'`。产品的 worker/interpretation 已有同样的“临时去掉 bracketed IPv6 token”处理。本轮 preflight/probe 在**构造 client 期间**临时使用同样的 parseable 视图，构造完立即恢复进程环境：

- 不修改系统代理、注册表、SSL 或任何网络设置；
- 不关闭代理；provider 是远端 host，`[::1]` 的 bypass 对 provider 路由无影响；
- 未带方括号的 `::1` 仍保留；
- preflight 记录 `bracketed_ipv6_entries_ignored=1`、`process_env_restored=true`。

这不是“要求无代理才能测”，也不是把代理失败当成网络已排除。

## 3. 离线 preflight（未发网络）

`outputs/provider-transport/preflight.json` 的 critical checks 全部为 true：

- baseline 与 traced client 都是 `openai._DefaultHttpxClient`；
- `trust_env`、`follow_redirects`、`max_redirects`、timeout 相同；
- mount 数量/形状相同；
- `_transport_for_url(provider)` 仍是 `HTTPTransport` + `HTTPProxy` pool；
- transport 没有被替换；
- request header 数量、method、provider host 相同；
- `trace` 已注入，recorder 已附加；
- 原有 trace extension 仍被调用；
- baseline response hooks = 0，traced response hooks = 1（只增加了本记录器）。

也就是说：**preflight 证明了 hook 不改变 proxy/mount/trust_env 的 client 形状**，但 preflight 不是网络证据。

## 4. 一次真实小请求（n=1）

- 目的：只验证 hook 在当前代理配置上能拿到事件，不是完整 agent。
- 请求：`deepseek-chat`，一条非隐私短消息“请只回复：ok”，`temperature=0`，`max_tokens=8`，`max_retries=0`。
- 凭据：只从已授权 envfile 在进程内 `load_dotenv` 注入；没有打印、复制或写入值。
- 结果：**HTTP 200，1 次 provider 请求，usage 9/1/10，client wall 1029.386ms，transport record elapsed 948.188ms**。
- 原始 record 有一个记录器匹配 bug：`httpcore` 的 `started` 回调带 `request/host`，`complete` 回调只带 `return_value`，旧 key 把 role 也放进去了，导致 start/complete 没配上。模块已修复，并新增了“completion payload 不含 start metadata”的 mock 测试。
- **没有发第二次 live 请求。** 校正视图 `records-corrected.json` 只把同一次请求的 start/complete 按事件名配对，重新计算 duration；原始 `records.json` 保留为历史证据。

### 4.1 同一请求的校正后事件

| 观测项 | 值 | 读法 |
|---|---:|---|
| proxy `connect_tcp` | 0.638ms | 目标是 proxy/other，**不是** provider 直连延迟 |
| proxy CONNECT `send_headers` | 0.235ms | 代理隧道控制请求 |
| proxy CONNECT `read_headers` | 0.410ms | 代理隧道控制响应 |
| origin `start_tls` | 119.242ms | 通过代理到 provider 的 TLS；属于连接建立 |
| **connection setup union** | **120.525ms** | connect + proxy CONNECT + origin TLS 的重叠区间 union，不是相加 |
| provider `send_headers` | 0.331ms | provider 请求头发送 |
| **provider `read_headers` wait** | **115.905ms** | 一个 inclusive wait；包含代理/公网 RTT、provider 排队、prefill、生成等，不能拆 |
| provider `time_to_headers` | 238.221ms | 从 request start 到 provider headers 完成（含前面的 proxy/TLS/send） |
| first body read start | 238.984ms | 第一次 body read 开始 |
| first body read | 709.005ms | 第一次 body read 的完整等待；非流式路径会等到整段 body |
| first body chunk | 943.286ms / 290 bytes | 首 body chunk，**不是首 token** |
| retry | 0 | 本次没有 retry/backoff |
| connection reuse | `new_connection`（known=true） | 本请求观察到 connect_tcp；目的为 proxy/other，不是 provider 直连 |
| TTFT | unavailable | 非流式 invoke；首 body chunk ≠ 首 token |

### 4.2 能从这个样本说什么

- 当前代理路径可以观测，且 hook 没有把 proxy 换成直连，
- 这次请求的 provider headers 等待约 **115.9ms**，body 读取约 **709ms**，
- 连接建立约 **120.5ms**，其中绝大部分是 origin TLS（119.2ms），proxy TCP 只有 0.638ms，
- `read_headers` 不能拆成“远端推理”与“公网纯 RTT”；`first body chunk` 不能当 TTFT。

### 4.3 不能从这个样本说什么

- 不能说“网络已排除”；
- 不能说“provider 推理耗时 = 709ms”或“网络 RTT = 115.9ms”；
- 不能把 proxy `connect_tcp` 当成 provider 直连；
- 不能把 n=1 外推到 20s turn、24 cases 或所有模型调用；
- 不能把这个请求当成完整 agent 的时延结论。

## 5. 能测 / 不能测

### 能测

- 每次 provider request 是否存在 `connect_tcp`、`start_tls`、`send_request_headers`、`receive_response_headers`、`receive_response_body`、`retry`；
- 每个事件的 duration、start/end offset；
- connection setup union（重叠算 union）；
- `connection_reuse` 的 known/unknown 及依据；
- `connect_tcp_destination`：provider / non_provider / unknown；
- `proxy_observed`；
- retry/backoff 事件数量与时长；
- provider `read_headers` 的 inclusive wait；
- 首 body chunk 的时间/字节数；
- 是否有 token stream（本轮没有）；
- trace 注入不覆盖原 trace；
- redaction：不记录 query/path/body/header 值/cookie/credential；
- 网络异常、retry 链、无 trace fallback 不阻断业务（mock 测试）。

### 不能测

- `read_headers` 内部的 provider 排队 / prefill / 生成 / 公网 RTT 分解；
- TTFT（产品非流式；缺 token stream 时明确 unavailable）；
- async/`astream` 路径（未覆盖）；
- DNS 单独耗时（`connect_tcp` 聚合 DNS+TCP；且在代理下目标是 proxy）；
- provider 远端推理时间（没有 token/timing 事件）；
- 完整 agent 的 20s 归因（本轮只有 transport hook 和一次小请求）；
- n=1 的分布、p50/p95、cold/warm 差异。

## 6. 为什么没有关代理、没有抓包、没有 ping 减法

- 关代理/改 SSL 会改变用户实际路径，不是观测。
- 自定义 transport 虽然可能能测，但会改变 mount/trust_env 语义；本轮优先用 SDK 正常 client + trace extension。
- `ping`/ICMP RTT 与 HTTP headers 等待不是同一件事；headers 等待包含远端排队、prefill、生成。
- 抓包会引入权限、隐私和“看到 TCP 不等于能拆应用层”的问题；本轮没有使用。
- 代理环境不支持某个事件时，标记 `missing`/`unavailable`/`unknown`，不推断成“网络已排除”。

## 7. 复现命令

```powershell
# 只做离线 preflight（不发 provider 请求）
C:/Users/28980/anaconda3/envs/langchain/python.exe scripts/probe_provider_transport.py --preflight-only

# 至多 1 次真实小请求；envfile 只在进程内注入，不打印值
C:/Users/28980/anaconda3/envs/langchain/python.exe scripts/probe_provider_transport.py --live
```

默认 envfile：`E:\CODE\Myproject\agent-workspace\my_agent_harness\.env`；可用 `--env-file` 指定。`--live` 使用 `max_retries=0`，不会自动重试；脚本本身不启动 subprocess。

目标测试：

```powershell
C:/Users/28980/anaconda3/envs/langchain/python.exe -m pytest tests/test_provider_transport.py tests/test_latency_spans.py -q
# 13 passed
```

## 8. 文件与边界

- 实现：`runtime/core/observability/provider_transport.py`；`provider_trace.py` 保留为兼容 shim；`runtime/core/llm/factory.py` 在 opt-in 时对 LangChain 已建好的真实 sync client 原地注入。
- 证据：`outputs/provider-transport/preflight.json`、`real-request.json`、`real-request-corrected.json`、`records/records.json`、`records-corrected.json`；汇总 `outputs/provider-transport-report.json`。
- `outputs/provider-transport/spans/` 是修复前那次 live 的原始 span，duration 受同一个 start/complete 匹配 bug 影响，**不作为指标**；该目录的 `README.txt` 已标注，指标以校正后的 record 为准。没有发第二次 live 请求。
- 未改：`latency.py`、scenario/compiler/execution/worker、source assets、32 reviews、v1.1、holdout、旧产物、GUI 8950、系统代理/SSL/网络设置。
- 未做：全 agent LLM 链路测试、全局最终产物构建、24 cases/holdout 重跑、第二次真实 provider 请求。

## 9. 建议给集成方

1. 在完整 agent 的 model-call span 下复用本 hook；用 `call_id`/`call_span_id` 把同一 model call 的 provider request 串起来。
2. 报告时把 `connection_setup_union_ms`、`provider.send_headers`、`provider.read_headers_wait`、`first body read` 分开列；不要把 `connect_tcp`（proxy）写成 provider 直连。
3. 只有在产品改成真正 token streaming 后，才把首 token 与首 body chunk 分开，再讨论 TTFT。
4. 如果要判断 20s 里网络占多少，需要更多同路径样本 + 完整 agent span；n=1 只能证明 hook 可用，不能证明比例。
5. async 路径若以后需要，另做 async client hook；本轮不把 async 结果算进覆盖。
