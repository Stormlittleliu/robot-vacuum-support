# 无头 Docker 运行（Windows/WSL2 独立 Engine）

本文记录本仓库在**已运行的无头 Docker Engine** 上的部署方式。它不启动 Docker Desktop，不启动/停止 daemon，不改全局 `PATH`、`DOCKER_HOST`、docker context 或 WSL 默认发行版。适用范围是单租户交付，不是多用户隔离，也不是生产 SLA。

## 1. 目标形态与路径

| 部分 | 约定 |
|---|---|
| WSL 发行版 | 已运行的 Linux 发行版（本机为 `DockerEngine`；脚本可用 `RVS_WSL_DISTRO`/`--distro` 覆盖） |
| 发行版磁盘 | `E:\Docker\wsl\DockerEngine\ext4.vhdx`（用 `wsl --manage … --move` 放到 E:，只读核实即可） |
| Engine 数据根 | 发行版内部 `/var/lib/docker`，位于上面的 VHD 内；**不需要**迁改 data-root |
| 连接方式 | 仅 Unix socket；不设置 `DOCKER_HOST`，不开 2375/2376，不新增 SSH |
| 仓库路径 | Windows 盘符在 WSL 中映射为 `/mnt/<drive>/...`；compose 从该 Linux 路径执行 |
| 资产路径 | compose 绑定必须写 Linux 绝对路径（如 `/mnt/e/.../assets`），不能写 `E:\...` |

`E:\Docker\bin\docker.exe` 是一个可选包装器；本仓库脚本不依赖它，而是用
`wsl -d <distro> -- docker ...` 直接进入同一个 Engine。这样临时进程的 `PATH`
和全局 context 都不需要改变。

## 2. 隔离命名

每次部署必须显式给出 project、镜像 tag、卷名和端口，禁止复用公共 smoke tag：

| 对象 | 本项目值 | 说明 |
|---|---|---|
| compose project | `rvs-p1` | 容器/网络/默认名称前缀 |
| backend 镜像 | `rvs-p1/backend:<short-commit>` | 不覆盖、不复用 `rvs-backend:smoke` |
| 数据卷 | `rvs-p1_rvs-data` | 在 override 中用 `name:` 固定，避免依赖默认前缀 |
| 网络 | `rvs-p1_rvs-internal` | 由 compose project 派生 |
| 公开端口 | `18080`；被占用时用 `18084` | 只暴露 nginx，backend 仅内部网络 |
| 前端/后端容器 | `rvs-p1-frontend-1` / `rvs-p1-backend-1` | 不碰其它 project 的对象 |

所有操作都带 `--project-name rvs-p1` 和显式 `--env-file`、镜像 tag；不做
`docker system prune`、`docker volume prune`、`docker compose down -v`，也不删除
其它 project 的镜像、容器、卷或网络。

## 3. 交付文件

| 文件 | 作用 |
|---|---|
| `docker-compose.yml` | 通用云部署主文件，保留原默认值，不为本机写死路径/端口 |
| `deploy/compose.headless.yml` | 最小 headless override：强制显式镜像、卷名、Linux 资产根、端口、实例 ID |
| `scripts/headless_docker.py` | 固定 WSL distro 与 project 的包装：`check / preflight / build / up / restart / down / status / smoke / logs` |
| `outputs/p1-headless-migration-v1/runtime.env` | 本机实际环境文件，**gitignored**，含 token/模型 key，不入库、不打印 |

override 中所有关键值都用 `${VAR:?}` fail-closed，缺值时 compose 直接失败：

```dotenv
# 放在仓库外或 outputs/ 下，绝不提交
PUBLIC_PORT=18080
RVS_BACKEND_IMAGE=rvs-p1/backend:<short-commit>
RVS_DATA_VOLUME=rvs-p1_rvs-data
RVS_ASSETS_HOST_ROOT=/mnt/e/<repo>/assets
RVS_INSTANCE_ID=rvs-p1
RVS_ADMIN_TOKEN=<本机生成，不提交>
RVS_ACCESS_TOKEN=<本机生成，不提交>
DEEPSEEK_API_KEY=<本地正式 instance environment_file 中的有效 key；不打印>
```

`RVS_ASSETS_HOST_ROOT` 必须是 WSL 内可见的 Linux 绝对路径。把 Windows 路径写进
compose 的 volume 会在容器内变成错误目录；脚本只接受 `/...` 形式。

## 4. 运行步骤

先恢复 `assets/knowledge`、`assets/models/bge-small-zh-v1.5`、`assets/rag_index`
（见 [standalone.md](standalone.md)），确认目标 Engine 已由环境提供并在运行：

```powershell
python scripts/headless_docker.py check
python scripts/headless_docker.py preflight
```

`check` 打印发行版 BasePath/VHD、Engine 版本、`/var/lib/docker`、context 与
`DOCKER_HOST` 探针；`preflight` 检查环境文件、token 强度/互异、镜像/卷命名、
Linux 资产路径、端口和 `docker compose config --quiet`，不打印 secret。

构建（首次需要网络拉取 Python/CPU 轮子；一次失败不要无限重试，保留层缓存后恢复）：

```powershell
# 建议后台执行并 tail 日志；helper 已改为流式输出
python scripts/headless_docker.py build *> outputs\p1-headless-migration-v1\logs\build.log
```

启动与冒烟：

```powershell
python scripts/headless_docker.py up
python scripts/headless_docker.py smoke
```

入口：

- 用户前台：`http://127.0.0.1:18080/`
- 人工后台：`http://127.0.0.1:18080/admin.html`（输入 `RVS_ADMIN_TOKEN`）
- 健康：`/api/health`、`/api/health/live`、`/api/health/ready`
- OpenAPI：`/api/docs`

停止时保留数据卷：

```powershell
python scripts/headless_docker.py down
```

`down` 不传 `-v`；`rvs-p1_rvs-data` 保留。重启后端做持久化验收可用
`python scripts/headless_docker.py restart`，它只重启本项目 backend 容器，不重启
daemon。

## 5. 已迁入 / 未迁入

**已迁入（本机本次交付）**

- 仓库当前完整产品源码（commit `33a6b67`）构建出的 `rvs-p1/backend:<short>` 镜像；
  Dockerfile 的 CPU-only PyTorch 路径、非 root 用户、entrypoint 构建 artifact 均不变。
- 最新完整 Docker 产品：backend + nginx 前端 + `/api`/SSE 反代 + 人工后台。
- 6 份知识、BGE 模型和 Chroma 索引以只读 Linux 绑定挂载；不复制进镜像。
- 新的独立命名卷 `rvs-p1_rvs-data`，用于 artifact、会话、人工工单和日志。
- 模型 key 仅从本地正式 instance environment_file 注入 gitignored 运行环境文件；
  不写 tracked 文件，不打印。

**未迁入**

- 旧 Docker Desktop 的镜像、容器、卷和历史会话。Desktop 未运行，其数据盘仍保留在
  `E:\Docker\wsl\DockerDesktop\disk\docker_data.vhdx`（另有备份），本轮**没有**做
  离线 data-root 移植或导入，因此旧会话数据不会出现在新 Engine。
- 如果本地存在可安全导入的 artifact storage，应作为单独步骤显式导入；否则新实例
  从独立空数据卷开始，并在验收报告里明确写“旧会话未迁入”。
- 其它 project（例如 P2）的任何对象、端口、卷、镜像和容器均未读取或修改。

## 6. 验收边界

完整验收必须走正常 HTTP/SSE 入口：health/frontend/API/admin 认证、人工工单闭环、
容器重启后的会话/工单持久化，以及至少一次真实模型检索引用回答。零模型 smoke
只能证明人工入口和路由，不能替代 Agent 全链路；单次运行也不是成功率或生产 SLA。
真实模型依赖网络与凭据，失败时记录为未验证/阻塞，不用 fake 冒充通过。

共享 Engine 可能被其它任务重启。`restart: unless-stopped` 会把容器拉起，已落盘的
会话/工单仍在命名卷中；但进行中的 SSE 连接会中断。验收报告应区分“外部 Engine
重启导致的流中断”和产品缺陷，流中断后应重连或重新发起，不要把半截流判为通过。
