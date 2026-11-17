# Docker 部署验证记录（2026-11-17）

状态（截至 2026-11-17 21:07）：**本地隔离 compose 演示通过，服务保留运行供验收；模型聊天未调用，边界见第 11 节。** 第 1–9 节保留当时“镜像构建阻塞”的历史记录，第 10 节是首次真实闭环，第 11 节是本轮最终验收。本文只记录实际跑过的命令和结果，不把预检或单元测试当作部署成功。

## 1. 结论

- 目标镜像 `rvs-backend:smoke` 本轮未构建成功，因此没有可用的真实镜像可供 compose 冒烟。
- 阻塞发生在构建期访问 PyPI / PyTorch CDN：先是 `orjson` 索引读取 TLS EOF，随后是 CPU torch wheel 哈希校验失败；不是应用代码、compose 配置或测试失败。
- 仓库侧已完成两项必要修复：容器依赖改用 CPU-only torch（本机开发 `requirements.txt` 保持原样），compose backend 复用 `rvs-backend:smoke` 镜像，避免同 tag 的重复依赖构建。
- 没有修改全局 Docker daemon、代理、registry、WSL 或网络配置，也没有调用任何模型。

## 2. 构建尝试

命令（隔离 tag，未 push）：

```powershell
docker build -t rvs-backend:smoke .
```

第 1 次：原始 `requirements.txt` 由 `sentence-transformers` 拉取 PyPI `torch==2.15.0`。该 Linux wheel 在 PyPI 元数据中声明 6 个 CUDA 13.2 依赖（`cuda-toolkit[cublas,cudart,cufft,cufile,cupti,cusolver,cusparse,nvjitlink,nvrtc,nvtx]==13.2.2`、`cuda-bindings`、`cudnn`、`cusparselt`、`nccl`、`nvshmem`），体积达数 GB。在 CUDA wheel 开始下载前停止了这次构建，改为 CPU-only 方案。

第 2 次：新增 Docker 专用 `requirements-docker.txt`，固定 `torch==2.15.0+cpu`，与常规依赖放在同一个 pip 解析中。pip 已从 `https://download.pytorch.org/whl/cpu` 选中 CPU wheel，但解析到 `orjson` 时连续出现 `ReadTimeoutError` / `SSLError(SSLEOFError)`，最终：

```text
ERROR: Could not find a version that satisfies the requirement orjson>=3.11.5 (from langgraph-sdk)
ERROR: No matching distribution found for orjson>=3.11.5
build_exit=1
```

随后确认容器网络可以读取 `orjson` 索引页（2,943,987 bytes），判断为瞬时网络故障，而不是依赖不可达。

第 3 次：把 CPU torch 安装拆成独立层，加入 `PIP_RETRIES=10`、`PIP_TIMEOUT=60` 和 BuildKit pip cache，常规依赖仍从 PyPI 安装。torch CPU wheel（209.5 MB）下载完成后的哈希校验失败：

```text
ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.
    unknown package:
        Expected sha256 cee15855a32ad89a9bb27b8a9d6e07a859dcd2e11ed1b5561f3d6206e31a103b
             Got        872f46653b01cb6d5e279a08a48cf42550e9cd8cfbfaa15fdc1da598a233c5a9
build_exit=1
```

没有生成可确认的镜像层。失败日志保留在本地 gitignored 的 `outputs/deployment-hardening-v1/`（`build-cpu.log`、`build-cpu2.log`），不随仓库发布。

## 3. 仓库侧修复

- `requirements-docker.txt`：只给容器固定 `torch==2.15.0+cpu`；不改本机 `requirements.txt`，本机开发仍用原依赖。
- `Dockerfile`：先单独安装 CPU torch 层，再从 PyPI 安装常规依赖；加入 pip 重试/超时。CPU wheel 不包含 CUDA 13 工具链。
- `docker-compose.yml`：backend 增加 `image: ${RVS_BACKEND_IMAGE:-rvs-backend:smoke}`，让 compose 复用已构建 tag；`up --build` 在 Dockerfile 未变时走本地层缓存，不重复解析依赖。
- `tests/test_cloud_deployment.py`：新增契约，锁定 CPU torch、Dockerfile 分层和 compose 镜像复用，并确认本机 `requirements.txt` 未被写入 `+cpu` 或 PyTorch 索引。

## 4. 已通过：预检

```powershell
py -3.11 scripts/docker_deploy.py preflight --env-file .env.docker --project rvssmoke --public-port 18080
```

结果 `ok=true`，以下检查全为 true：两个 token 存在、互不相同、非占位值；knowledge/models/rag_index 资产存在；18080 端口空闲；Docker daemon 可用；compose config 可解析。命令只输出检查布尔值，没有输出 token 值。

## 5. 已通过：定向测试与静态检查

```powershell
py -3.11 -m pytest tests/test_cloud_deployment.py tests/test_docker_deploy.py -q
```

结果：`13 passed, 1 warning`（warning 是 httpx 对 per-request cookies 的弃用提示，不影响断言）。

```powershell
git diff --check
```

结果：exit 0，无 whitespace error；仅输出仓库既有的 LF/CRLF 提示。

## 6. 未验证（不得标为通过）

- `scripts/docker_deploy.py up` / `smoke`：没有确认镜像，未启动 `rvssmoke`，未验证 frontend `/healthz`、backend live/ready。
- 18080 上的 HTTP 会话创建、handoff SSE、未授权 401、用户 token 访问 admin 被拒、admin token 经 nginx 代理 200。
- 容器重启或 `down`/`up` 后的会话/handoff 状态持久化，以及命名卷保留。
- 运行日志不含 token/provider key 的运行时证据。

这些项目需要成功镜像后才能执行；本轮不把预检、单元测试或构建日志当作部署成功。

## 7. 密钥与报告边界

- `.env.docker` 未被 git 跟踪，历史上也没有被跟踪；没有把任何 token/provider key 写入本文、代码或提交。
- 对当前 `HEAD` 中所有已跟踪文件做过环境 token 值扫描：无匹配。扫描只输出布尔结果，未打印值。
- 原先被 force-add 的 `outputs/deployment-hardening-v1/report.json` 已用 `git rm --cached` 从索引移除，本地文件保留；`outputs/` 仍在 `.gitignore` 中。
- 本文与提交不含机器绝对路径或 token 值。

## 8. 后续（本记录不声称已完成）

网络稳定后，只需重跑同一 `docker build -t rvs-backend:smoke .`；若成功，再按已实现路径执行：

```powershell
py -3.11 scripts/docker_deploy.py up --env-file .env.docker --project rvssmoke --public-port 18080
py -3.11 scripts/docker_deploy.py smoke --env-file .env.docker --project rvssmoke --base-url http://127.0.0.1:18080
# 另外做 restart 后的会话/handoff 读取，以及 down 后命名卷仍在
py -3.11 scripts/docker_deploy.py down --env-file .env.docker --project rvssmoke
```

本次不重试、不下载更多 wheel、不安装软件。

## 9. 本轮收尾（headless 约束，2026-11-17）

用户新约束（最高优先）：**禁止启动或唤起 Docker Desktop，必须 headless；只连接已运行的 Docker Engine / headless Linux 服务器，不自动拉起 GUI；不停止用户已有的 Docker/WSL 进程**。

### 9.1 约束检查结果

- 全仓库检索 `Docker Desktop`、`open -a Docker`、`Start-Process ... Docker`、`com.docker`、`systemctl start docker` 等模式：**无匹配**，不存在需要移除的 GUI 启动逻辑。
- `scripts/docker_deploy.py` 只在 `preflight` 中执行 `docker info --format "{{.ServerVersion}}"` 探测 Engine；返回码非 0 时把 `docker_daemon` 记为失败并阻塞，**没有**启动 daemon、启动 GUI、重启或停止任何 Docker/WSL 进程的代码路径。
- 本轮只做只读探测与静态检查，没有执行 `up` / `smoke`，没有下载 wheel，没有构建镜像。

### 9.2 本轮实际命令与结果

```powershell
docker info --format '{{.ServerVersion}}'
```

结果：输出 `29.4.3`，exit 0。这是**已存在的 Engine**，探测本身不启动或停止任何进程。因此本轮不构成“headless daemon 不可用”的阻塞；镜像构建阻塞仍来自上一轮的网络/哈希问题，与本约束无关。

```powershell
py -3.11 -m pytest tests/test_cloud_deployment.py tests/test_docker_deploy.py -q
```

结果：`13 passed, 1 warning`（与上一轮相同的 httpx 弃用提示，不影响断言）。

```powershell
git diff --check
```

结果：exit 0，无 whitespace error；只有仓库既有的 LF/CRLF 提示。

### 9.3 本轮的未提交改动

- `Dockerfile`：上一执行被取消前留下的改动——`PIP_RETRIES` 10→2、`PIP_TIMEOUT` 60→30，CPU torch 层改为 `--no-cache-dir` 以绕开曾哈希校验失败的 pip 缓存。**本轮未重新构建验证**，仅通过静态测试断言。
- `docs/deployment.md`：新增“运行前提：headless，只连接已运行的 Engine”，明确不启动/不唤起 Docker Desktop、不启动 daemon、不停止用户已有进程。
- 本节：记录本轮实际结果与该约束。

### 9.4 当时未验证（已由第 10 节取代）

- `docker build -t rvs-backend:smoke .` 未重跑，镜像仍未经确认。
- `up` / `smoke`、18080 上的 HTTP/SSE、401、admin 代理、重启持久化均未执行，不得标为通过。

> 第 9 节保留 20:46 的中间状态；随后同一工作树完成了真实构建和容器闭环，结果见第 10 节。9.4 的阻塞结论不再代表当前状态。

## 10. 真实容器闭环（2026-11-17 20:42–20:59）

### 10.1 构建结果

命令：

```powershell
docker build --progress=plain -t rvs-backend:smoke .
```

- 构建参数：`PIP_RETRIES=2`、`PIP_TIMEOUT=30`；CPU torch 层使用一次 `--no-cache-dir` 新下载，绕开此前哈希校验失败的缓存条目；`requirements.txt` 层仍挂载 BuildKit pip cache。没有执行 `docker builder prune` 或清理全局 Docker 缓存。
- 官方 CPU 轮子：`torch==2.15.0+cpu`，来自 `https://download.pytorch.org/whl/cpu`（实际重定向到 `download-r2.pytorch.org`），209.5 MB；索引哈希校验通过，未再现上一轮的哈希不匹配。未关闭哈希/TLS 校验，未使用 `--trusted-host`，未更换镜像源或全局代理。
- 兼容性核对：`sentence-transformers==6.2.0` 的要求为 `torch>=2.2`，`2.15.0+cpu` 满足，因此本轮不需要回退到更早的 torch，也没有改动版本锁定。
- 结果：`rvs-backend:smoke` 构建成功，exit 0；镜像 ID `b017bc8ee629`，大小 625,626,835 bytes（约 597 MiB）。构建日志在本地 gitignored 的 `outputs/deployment-hardening-v1/build-final2.log`。
- 耗时：20:42:01–20:58:58，约 17 分钟。torch 层 84s，常规依赖层 851s，`libgomp1` 层 20s，导出 29s。主要耗时是 PyPI 约 0.2–0.5 MB/s 的下载；`PIP_RETRIES=2` 避免了 10 次 × 60 秒的重试长等。构建+测试超过了 900 秒预算，但没有反复网络探测或手工分块下载。

### 10.2 预检与启动

```powershell
py -3.11 scripts/docker_deploy.py preflight --env-file .env.docker --project rvssmoke --public-port 18080
py -3.11 scripts/docker_deploy.py up --env-file .env.docker --project rvssmoke --public-port 18080 --skip-preflight
```

预检 `ok=true`，以下全为 true：token 存在/互异/非占位、env 文件私有、gitignore/dockerignore、knowledge/models/rag_index、18080 空闲、Docker daemon、compose config。`up` 在 20:58:56 返回 exit 0，使用隔离 project `rvssmoke` 和 18080，未占用其它端口。

### 10.3 API / 认证 / 人工 SSE / nginx

`smoke` 在 20:59:11 返回 exit 0，15 项检查全为 true：

- nginx `frontend_healthz`、backend `/api/health/live`、`/api/health/ready`。
- `/api/sessions` 无 token 为 401，用户 token 为 200；创建会话成功并返回 session id。
- 人工工单 request 返回 200；等待工单下的消息走 handoff SSE：`text/event-stream`，包含 `handoff` 与 `done` 事件，未调用模型。
- admin token 经 nginx 访问 `/api/admin/handoff` 为 200；用户 token 访问同一接口为 401；`/admin.html` 为 200。
- `logs_no_token=true`、`logs_no_provider_value=true`：运行日志未出现 token 或 provider key 值；本文和报告也没有打印密钥。

### 10.4 重启后数据

```powershell
py -3.11 outputs/deployment-hardening-v1/persistence_smoke.py create
docker compose --project-name rvssmoke --env-file .env.docker restart backend
# 等待 /api/health/ready 返回 200
py -3.11 outputs/deployment-hardening-v1/persistence_smoke.py verify
```

- 重启前创建会话 `s-0ab36826df4d46b5`，`workflow=agent`，`handoff.status=waiting`，`created_at=1794920361.901166`。
- 仅重启 backend，等待 ready 200 后读取同一会话。
- 重启后 `session_id`、`workflow`、`handoff.status`、`created_at` 与重启前一致，`same_as_before_restart=true`。
- 会话详情的 `handoff.ticket_id` 重启前后均为 null（该响应未暴露 ticket id），因此本节只声明会话与 handoff 状态持久化，不额外声称 ticket 标识一致；同一构建的 smoke 已独立验证等待工单的 SSE 路径。

### 10.5 停止、卷与测试

```powershell
py -3.11 scripts/docker_deploy.py down --env-file .env.docker --project rvssmoke
py -3.11 -m pytest tests/test_cloud_deployment.py tests/test_docker_deploy.py -q
```

- `down` exit 0，没有传 `-v`；项目容器已停止并移除，命名卷 `rvssmoke_rvs-data` 保留。
- 定向测试：`13 passed, 1 warning`（httpx per-request cookies 弃用提示，不影响断言）。
- 全程 0 次模型调用；没有新增功能、没有改全局 proxy/镜像源、没有 push。

### 10.6 仍未验证

- 没有执行 `down` 后再 `up` 并读取旧会话的卷往返测试；只验证了容器 restart 后的持久化，以及 `down` 后卷仍存在。
- 没有推镜像、没有公网域名/TLS/云主机、没有多用户或并发负载验证。
- 没有做浏览器端交互测试；只验证了 nginx 提供的 `healthz`、`admin.html` 和 API 代理。
- 首次构建仍依赖 PyPI/PyTorch 网络；后续重建应命中本地层缓存和 pip cache（torch 层因 `--no-cache-dir` 需要重新下载，除非层缓存命中）。

## 11. 最终验收：真实 compose、人工闭环与重启持久化（2026-11-17 21:00–21:07）

状态：**通过（有明确边界）**。本轮只连接**已存在**的 Docker Engine，没有启动或唤起 Docker Desktop，没有启动任何 daemon/GUI，没有停止用户已有的 Docker/WSL 进程，也没有修改全局代理、registry、WSL 或网络配置。

### 11.1 引擎与镜像复用

```powershell
docker info --format '{{.ServerVersion}}'
```

结果：`29.4.3`，exit 0；这是已有 Engine，不是本轮启动的。

镜像先做 `docker image inspect` 和容器内检查：

```text
rvs-backend:smoke
id=sha256:cd562e9c43461640e1257e73e7b621bb0a6e5801707c063c66e13398328eadf3
created=2026-11-17T12:57:59.751500903Z
size=625,626,835 bytes
torch=2.15.0+cpu
torch.cuda.is_available()=False
sentence-transformers=6.2.0
chromadb=1.5.9
```

镜像内 `source_fingerprint` 与当前工作树一致（`6021c0a6…e64926`），说明镜像包含当前 `runtime/`、`robot_vacuum/`、`product_ui/`、`project.json`、`requirements.txt`。运行中的 `rvssmoke-backend-1` 使用同一镜像 ID。

第 10.1 节记录的 `b017…` 是上一轮同一构建的镜像 ID；本轮 tag 重新导出后指向新的 manifest `cd562…`，created/size/源码指纹未变，不是重新下载或重装依赖。

随后只做缓存刷新，没有重新下载依赖：

```powershell
docker build --progress=plain -t rvs-backend:smoke .
```

结果：exit 0；所有应用/依赖层 `CACHED`，导出 0.3 秒，没有访问 PyPI/PyTorch CDN，没有关闭哈希或 TLS 校验，也没有使用 `--trusted-host`。

### 11.2 预检与启动

```powershell
py -3.11 scripts/docker_deploy.py preflight --env-file .env.docker --project rvssmoke --public-port 18080
py -3.11 scripts/docker_deploy.py up --env-file .env.docker --project rvssmoke --public-port 18080 --skip-preflight
```

预检 `ok=true`，`failed=[]`；token 存在/互异/非占位、`.env.docker` 私有、gitignore/dockerignore、knowledge/models/rag_index、18080 空闲、Engine、compose config 全为 true。命令没有输出 token 值。

`up` 使用隔离 project `rvssmoke` 和 18080。backend 只 expose 内部 8000，frontend 是唯一发布端口；等待后两个 service 均为 healthy：

```text
backend   Up (healthy)   8000/tcp
frontend  Up (healthy)   0.0.0.0:18080->8080/tcp
```

### 11.3 HTTP / 认证 / 人工闭环

`smoke` 在真实 nginx 端口上 15 项全为 true：`/healthz`、`/api/health/live`、`/api/health/ready`、无 token 401、access token 200、创建会话、申请人工、handoff SSE、admin 经 nginx 200、用户 token 访问 admin 401、`/admin.html` 200、日志不含 token/provider 值。

本轮新增的完整人工链路（全部经 `http://127.0.0.1:18080`，26 项检查全为 true）：

1. 创建 session `s-ce027d9552ad4431`；
2. 申请人工，工单 `waiting`；
3. 用户消息走 handoff SSE，只出现 `handoff` 和 `done`，没有 assistant 事件；
4. admin 等待列表/详情可见该 session 与排队消息；
5. admin 接手，工单 `active`；
6. 人工回复成功；同一 `request_id` 重放返回 `duplicate=true`；
7. 用户前台通过 `/api/sessions/{id}/handoff/messages?after=0` 读到人工回复；
8. admin 结案，工单 `closed`，会话详情同步显示 `closed`；
9. 最终 transcript 角色为 `user`、`human`，`assistant_messages=0`，0 次模型调用。

`/api/health/ready` 返回 200 且 `ready=true`；本轮 provider key 已配置（`model.configured=true`），因此没有触发“无 key → degraded/needs_configuration”的路径，也不声称测试过该路径。没有发出任何聊天/LLM 请求。

### 11.4 容器内 BGE / Chroma 离线验证

用一个与 backend 同镜像的一次性容器，`--network none` 完全离线，只读挂载同一份本地模型和索引：

```text
torch_cpu=true
bge_import=true
embedding_dim=512
chroma_import=true
collection=agent
collection_count=823
retrieval_hits=2
top_source=故障排除.txt
llm_calls=0
```

查询“充电座一直闪红灯怎么办”命中“故障排除.txt”，证明本地 BGE 可加载、Chroma 集合可检索，且没有模型/网络调用。

Chroma 即使只查询也会以读写方式打开 SQLite；直接打开 `:ro` 挂载会报 `attempt to write a readonly database`。探针按生产 worker 的 `copy_rag_index` 方式，先把只读索引复制到容器内可写临时目录再查询；这是预期实现细节，不是服务故障。

### 11.5 重启后的 session / ticket 持久化

```powershell
# 验收脚本与报告在本地 gitignored 的 outputs/deployment-final-v1/，不随仓库发布
py -3.11 outputs/deployment-final-v1/persistence_flow.py create
docker compose --project-name rvssmoke --env-file .env.docker restart backend
# 等待 /api/health/ready 返回 200
py -3.11 outputs/deployment-final-v1/persistence_flow.py verify
```

重启前创建 session `s-2419bde365a743bd`、工单 `h-7aed732a06d6`（`active`），并写入一条人工回复。仅重启 backend 后，10 项检查全为 true：

- session id、`created_at`、工单 id、工单状态、transcript 长度、人工消息 id、回复文本均与重启前一致；
- 用户前台重新轮询仍能读到该人工回复；
- 命名卷 `rvssmoke_rvs-data` 保留，frontend 在 backend 重启期间保持 healthy。

这补上了第 10.6 节“未核对 ticket id 一致”的缺口；本轮仍未做 `down` 后再 `up` 的完整卷往返，不声称已验证。

### 11.6 密钥、日志与报告

- 全程没有打印、写入报告或提交任何 token/provider key 值；只读取 `.env.docker` 做认证和存在性检查。
- 重启后再次运行 `smoke`，`logs_no_token=true`、`logs_no_provider_value=true`。
- 本地报告 `outputs/deployment-final-v1/report.json` 及证据文件不含密钥；仓库公开文档只写 token 的**文件与键名**，不写值。

### 11.7 定向测试

```powershell
py -3.11 -m pytest tests/test_cloud_deployment.py tests/test_docker_deploy.py tests/test_handoff_admin.py -q
py -3.11 -m pytest tests/test_rag_ingestion_coverage.py -q
git diff --check
```

结果：

- 部署/人工链路：`18 passed, 1 warning`（httpx per-request cookies 弃用提示，不影响断言）。
- RAG 主机测试：收集阶段失败，原因是本机 Python 缺少 `langchain_chroma`；**不记为通过**。镜像内该依赖存在，且上面的离线 BGE/Chroma 检索探针通过；这是主机环境缺少可选依赖，不是本轮部署回归。
- `git diff --check`：exit 0。

### 11.8 保留的演示与访问方式

compose 演示**保留运行**，供用户验收：

- 用户前台：`http://127.0.0.1:18080/`
- 人工后台：`http://127.0.0.1:18080/admin.html`
- OpenAPI：`http://127.0.0.1:18080/api/docs`
- 就绪：`http://127.0.0.1:18080/api/health/ready`
- token 位置：`.env.docker` 中的 `RVS_ACCESS_TOKEN` 与 `RVS_ADMIN_TOKEN`；**值不在本文、报告或提交中**。

停止但不删除会话数据：

```powershell
py -3.11 scripts/docker_deploy.py down --env-file .env.docker --project rvssmoke
```

### 11.9 本轮边界

- 没有云主机、域名、TLS 或真实公网流量；这是本地 compose 演示，不是生产 SLA。
- 没有调用 LLM；聊天质量、provider 延迟和计费未验证；人工链路本身设计为 0 模型调用。
- 没有强制测试“无 provider key → degraded readiness”路径。
- 只做了一次离线检索查询，不代表检索质量评测。
- 没有浏览器自动化；只验证 HTTP/SSE、nginx 代理和静态页面返回。
- 未重复 `down` 后再 `up` 的卷往返；只验证容器 restart 后的持久化。
- 仍是单租户、单 uvicorn worker；没有多用户隔离或并发压测。
