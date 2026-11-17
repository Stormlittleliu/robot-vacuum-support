# 云部署与前后端分离（单租户）

本文给出可在本地验证的 Docker 交付文件。**没有云服务器凭据，因此没有声称线上部署成功**；验证范围见文末。部署是**单租户**：一个实例、一个共享访问 token、一个管理 token，不提供多用户账号、租户隔离或按用户授权。

## 0. 运行前提：headless，只连接已运行的 Engine

本仓库的脚本和文档**不启动、不唤起 Docker Desktop，也不启动任何 Docker daemon**：

- 部署只面向**已经运行的 Docker Engine**，以及可 SSH 访问的 **headless Linux 服务器**；脚本只用 `docker info` 探测 Engine 是否可用，探测失败即报告阻塞，不会尝试拉起守护进程或 GUI。
- 不停止、不重启用户已有的 Docker / WSL 进程；不修改全局 daemon、代理、registry 或网络配置。
- `scripts/docker_deploy.py` 只做必要范围的预检、隔离 project 启停和 HTTP/SSE smoke；仓库内没有 `Docker Desktop`、`open -a Docker`、`Start-Process Docker` 之类的启动逻辑。
- 若目标环境没有可用的 headless Engine，正确做法是报告阻塞并等待环境提供，而不是在本机安装或启动新的 daemon。

## 1. 交付物

| 文件 | 作用 |
|---|---|
| `Dockerfile` | Python 3.11 单 backend 镜像，非 root（uid/gid 10001），Linux 启动 |
| `requirements.txt` / `requirements-docker.txt` | 本机开发依赖保持原样；容器只额外用 CPU-only PyTorch wheel 覆盖 `torch`，避免把多 GB 的 CUDA 13 运行时装进镜像 |
| `.dockerignore` | 排除知识/模型/索引、密钥、outputs、会话、缓存、测试与本地数据 |
| `docker/entrypoint.py` | 启动时用现有 `build_project` 在 `/data` 构建 artifact，写实例配置后 `exec` `serve.py` |
| `docker/healthcheck.py` | 独立 liveness / readiness 探针 |
| `docker-compose.yml` | `backend`（仅内部网络）+ `frontend`（nginx，唯一公开端口） |
| `deploy/nginx.conf` | 独立静态前端、`/api` 反代、SSE 禁 buffer、长超时 |
| `product_ui/config.js` | 公共 API 地址配置；默认同源 `/api` |
| `.env.docker.example` | 环境模板，只有占位值；`RVS_ADMIN_TOKEN` / `RVS_ACCESS_TOKEN` 必填 |
| `scripts/docker_deploy.py` | 必要范围的部署预检、隔离 project 启停与 HTTP/SSE smoke；不打印 token |

镜像内只有源码、spec、前端和依赖。知识原文、BGE 模型、Chroma 索引、`outputs/`、会话和密钥都在镜像外。

## 2. 准备本地资产

仓库不附带可再分发的知识原文、模型和索引；按 [standalone.md](standalone.md) 从可信备份恢复：

```text
assets/knowledge/
assets/models/bge-small-zh-v1.5/      # 512 维 BGE small zh，显式只读挂载
assets/rag_index/                     # collection=agent，含 chroma.sqlite3
```

Compose 将三者分别只读挂载到 `/assets/...`；entrypoint 通过 `asset_bindings` 显式指向它们，不把资产复制进 artifact 或镜像。启动时 `build_project(..., include_assets=False)` 只构建 runtime/spec/frontend，所以宿主不需要旧的 `outputs/` 或开发机 artifact 绝对路径。

缺少知识/索引时：服务仍可启动，`/api/health/ready` 返回 503，`/api/health` 明确显示 `missing` / `needs_configuration`，不会伪造就绪。缺少 embedding 模型时同样降级。

## 3. 配置 token 与启动

```powershell
Copy-Item .env.docker.example .env.docker
```

在本机生成 **两个不同的**强 token，并填写到 `.env.docker`。下例只展示生成方法，不在文档中放真实值，也不要把输出粘贴到 issue/报告：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"   # RVS_ADMIN_TOKEN
python -c "import secrets; print(secrets.token_urlsafe(48))"   # RVS_ACCESS_TOKEN（必须不同）
```

`.env.docker`：

```dotenv
PUBLIC_PORT=8080
RVS_ADMIN_TOKEN=<本机生成，不提交>
RVS_ACCESS_TOKEN=<本机生成，不提交>
DEEPSEEK_API_KEY=<可选；不填则清晰降级>
RVS_ALLOWED_ORIGINS=
```

`docker-compose.yml` 使用 `${RVS_ADMIN_TOKEN:?}` / `${RVS_ACCESS_TOKEN:?}`，未设置或为空时 compose 直接失败，属于 fail-closed。`RVS_ACCESS_TOKEN` 是公开会话 API 的基础访问门禁，部署示例**要求必须设置**；它不是多租户隔离。

启动：

```powershell
docker compose --env-file .env.docker up -d --build
docker compose --env-file .env.docker ps
```

推荐先跑一次不接触真实服务的预检（只读环境文件和本地资产，不打印 token 值）：

```powershell
python scripts/docker_deploy.py preflight --env-file .env.docker
```

隔离验证其他端口/项目时，显式指定 project 和端口；不要复用现有实例的 project 或卷：

```powershell
python scripts/docker_deploy.py up --env-file .env.docker --project rvssmoke --public-port 18080
python scripts/docker_deploy.py smoke --env-file .env.docker --project rvssmoke --base-url http://127.0.0.1:18080
python scripts/docker_deploy.py down --env-file .env.docker --project rvssmoke
```

`down` 不传 `-v`，命名卷 `rvssmoke_rvs-data` 会保留；需要彻底清空测试数据时才手工执行 `docker compose -p rvssmoke down -v`。`smoke` 需要本地资产和至少一个占位 provider key 才能让 readiness 通过，但它的流程只创建会话、申请人工并读取 handoff SSE，不调用模型，因此模型成本为零。若要验证真实问答，另行显式配置真实密钥并接受调用成本。

访问：

- 用户前台：`http://127.0.0.1:8080/`
- 人工后台：`http://127.0.0.1:8080/admin.html`（弹出登录页，输入 `RVS_ADMIN_TOKEN`）
- OpenAPI：`http://127.0.0.1:8080/api/docs`
- 健康：`/api/health`（安全摘要）、`/api/health/live`（liveness）、`/api/health/ready`（readiness，未就绪 HTTP 503）

`/api` 与 `/api/v1` 双路由都保留；文档 tags 为中文。

## 4. 认证、Origin 与单租户边界

| 环境变量 | 作用 | 未配置时 |
|---|---|---|
| `RVS_ADMIN_TOKEN` | 人工后台 API 的 `Authorization: Bearer`；后台页面登录后只存 `sessionStorage`，不进 URL/localStorage | 仅 loopback 本机管理；远程 bind 的管理 API/页面 403 |
| `RVS_ACCESS_TOKEN` | 普通会话/文件/转人工 API 的 Bearer（也接受同名 HttpOnly/普通 cookie `rvs_access_token`，便于同源浏览器） | 普通 API 不额外认证（仅适合 loopback） |
| `RVS_ALLOWED_ORIGINS` | 逗号分隔的显式 Origin 白名单，如 `https://ui.example.com` | 仅同源（同 Host）写入；不放开 `*` |
| `RVS_SERVE_STATIC` | `false` 时 backend 不注册 `/`、`/admin.html`、静态 catch-all | 默认为 `true`，保留本机同进程 UI 兼容 |

规则：

- 所有 token 比较使用 `hmac.compare_digest`（constant-time）。
- 管理认证**只**看进程环境中的 `RVS_ADMIN_TOKEN` 与请求 `Authorization`，不使用 `Host`/`X-Forwarded-*` 判定回环；伪造转发头不能把远程 bind 变成 loopback。
- 配置了管理 token 后，即使 loopback 管理 API 也要求 Bearer；后台页面本身是公开登录壳，数据 API 必须认证。
- CORS 只回显白名单 Origin，`allow_credentials=true`；同源反代（nginx）通过 Host 同源判断，显式独立前端必须加入白名单。
- 写请求（POST/PUT/PATCH/DELETE）校验 Origin；缺失 Origin 的非浏览器客户端允许。管理写还要求 `application/json`。
- 用户 token 在用户前台首次 401 时由浏览器提示，只存 `sessionStorage`；不拼进 URL。所有请求从 `product_ui/config.js` 的 `apiBase` 派生，默认 `/api`。
- 公开 `/api/health`、`/api/health/live`、`/api/health/ready`、`/api/instance` 不返回本地路径、凭据名/值；`/api/instance` 只给实例身份、workflow、认证模式与就绪摘要。
- **单租户警告**：持有 `RVS_ACCESS_TOKEN` 的访客可以看到该实例的会话列表/会话内容。公开云部署必须设置访问 token；如果需要“每个访客只能看自己的会话”，需要另行实现账号/会话所有权，本版本明确不做。

## 5. 前后端分离与 nginx

`RVS_SERVE_STATIC=false` 时 backend 只提供 API；`frontend` 用 nginx 直接 serve `product_ui`，并把 `/api/` 反代到内部 `backend:8000`。

- 前端不需要构建框架；新增 `product_ui/config.js`，默认 `window.RVS_CONFIG = { apiBase: "/api" }`。
- 独立托管前端时，把该文件挂载/替换为 `apiBase: "https://api.example.com/api"`，并把前端 Origin 填入 `RVS_ALLOWED_ORIGINS`；不要在每个请求里硬编码服务器地址。
- 不要把 token 写进 `config.js` 或 URL。
- nginx 对 SSE 设置 `proxy_buffering off`、`proxy_cache off`、`proxy_read_timeout 3600s`、`X-Accel-Buffering: no`；客户端使用 `fetch` 流式读取，不依赖 EventSource。
- 同源部署时浏览器不触发跨域；独立前端时 CORS 只允许白名单 Origin。

## 6. 健康、日志与恢复

```powershell
# 容器 liveness（Docker healthcheck 用同一探针）
docker compose --env-file .env.docker exec backend python /usr/local/bin/healthcheck.py live
# readiness；缺模型/索引/密钥时返回非零，但容器仍存活
docker compose --env-file .env.docker exec backend python /usr/local/bin/healthcheck.py ready
# 日志
docker compose --env-file .env.docker logs -f backend frontend
docker compose --env-file .env.docker exec backend ls -l /data/runtime/logs
```

数据在命名卷 `rvs-data` 的 `/data`：

- `/data/build/artifacts/<artifact-id>/`：entrypoint 构建的只读 artifact；`current.json` 记录源码指纹缓存。
- `/data/instance/runtime.env`（0600）：从容器环境生成的 provider 密钥文件，不在镜像/仓库中。
- `/data/runtime/sessions/`、`workspace/`、`logs/`、`outputs/`：会话、文件、日志与 trace。

备份/恢复（先在测试实例验证，不要在 8914 上操作）：

```powershell
docker run --rm -v robot-vacuum-support_rvs-data:/data -v ${PWD}:/backup alpine `
  tar czf /backup/rvs-data-backup.tgz -C /data .
# 恢复前先停止 backend；恢复后重新 up
docker compose --env-file .env.docker down
docker run --rm -v robot-vacuum-support_rvs-data:/data -v ${PWD}:/backup alpine `
  sh -c "rm -rf /data/* && tar xzf /backup/rvs-data-backup.tgz -C /data"
docker compose --env-file .env.docker up -d
```

卷名以 `docker volume ls` 实际显示为准。`docker compose down -v` 会**删除** `/data`，只用于明确要清空的测试环境。修改源码后重建镜像即可；entrypoint 用内容指纹判断是否重建 artifact，不需要删卷。

## 7. 验证与已知限制

已执行（本地）：

- 定向 pytest：认证/Origin、独立静态、前端 API 配置、OpenAPI/双路由、compose/Dockerfile/nginx/entrypoint 契约、会话/人工回归。
- `docker compose config`（用 dummy token 仅做语法/插值校验）。
- 预检脚本、隔离 `rvssmoke` project、18080 端口、随机测试 token 的实际结果记录在 [deployment-validation.md](deployment-validation.md)；失败会如实标为阻塞，不伪称线上成功。原始本地 JSON 报告在 `outputs/`，不随仓库发布。
- 单 backend 只允许一个 uvicorn worker：会话状态、文件锁和人工工单都在进程内，不能用多 worker 扩容。

已知限制：

- 单租户，不支持多用户账号/租户隔离；访问 token 是共享门禁，不能阻止同一 token 持有者互看会话。
- Compose 不终止 TLS；公网请在云负载均衡/反向代理终止 HTTPS，并限制管理入口来源。
- 没有云服务器、域名、证书或真实线上流量验证；本地 compose 通过不等于生产 SLA。
- 首次构建/启动需要网络安装依赖；已把容器内的 `torch` 固定为 CPU-only wheel，避免 PyPI Linux 默认 wheel 拉取 CUDA 13 工具链。依赖或模型下载失败时应查看构建日志，不无限重试。
- 模型/索引/知识不随镜像分发；宿主机需按 `docs/standalone.md` 恢复。
- 旧 `serve.py --instance-config` 本机模式保持兼容；`RVS_SERVE_STATIC` 默认仍为 true，只有容器示例显式关闭。
