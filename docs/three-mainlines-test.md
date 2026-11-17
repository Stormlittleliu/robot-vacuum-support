# 三主线整产品测试（three-mainlines-v1）

日期：2026-11-17。范围：新增固定测试集与复用式 live runner，只测完整产品，不改 runtime / prompt / Judge / UI，不重建镜像。结果：**18 turn 全部执行，但模型凭据 401，语义层阻塞未验证**；程序结构层保留真实观察，不写成产品语义通过或失败率。

## 测试集与冻结

固定 12 个独立 session，占比按 session 不按 turn：

| 组 | session | turn | 内容 |
|---|---:|---:|---|
| 主线 | 6 | 12 | 回充失败 2（正常结案 / 更正撤回）；吸力下降 2（正常推进 / 第三方混淆）；拖地出水异常 2（正常推进 / 水痕更正） |
| 广度 | 6 | 6 | 选购指南、扫地机器人100问.pdf、扫地机器人100问2、扫拖一体机器人100问、维护保养、故障排除各 1 |

- 主线每 case 2 turn，广度每 case 1 turn，共 18 个唯一用户 turn。
- 只有回充 case 带专用业务 state 断言；吸力/拖地只核对问答，不要求虚构 state。
- 每份证据含文件路径、sha256、行号/PDF locator、短 quote；PDF quote 用 `pdftotext -layout` 逐字校验。
- 模型调用前 `--validate-only` 通过：12/6/6、18 turns、6 份文档各一次、证据 hash/quote/locator、回充 state 策略、重复题检查。
- 预期在运行前写入 `cases.json` / `suite.json` / `freeze.json`；运行后 hash 未变（`freeze_after.unchanged=true`）。

入口：`eval/development-next/three-mainlines-v1/`。

## 被测对象与隔离

- 镜像：`rvs-backend:smoke`，image id `sha256:cd562e9c43461640e1257e73e7b621bb0a6e5801707c063c66e13398328eadf3`。
- 图像内启动构建的 artifact：`robot_vacuum-v4-7d264b320c`；runtime hash `e23c7163…`，source content hash `78fed73a…`；source HEAD `c4f8423`。
- 配置指纹：Dockerfile `dc5bce79…`，compose `8e77974d…`，entrypoint `68f9697d…`，rag.yml `4d0c78c7…`，spec `4fc4d815…`。
- 隔离 compose project：`rvs-eval-three`；唯一公开端口 `18081`；独立命名卷 `rvs-eval-three_rvs-data`；知识/模型/索引只读 bind mount。
- 正常入口：`http://127.0.0.1:18081` 的 HTTP/SSE，经 nginx 到 backend；Bearer 访问门禁。
- 正式 `http://127.0.0.1:18080` 全程未动；收尾 `compose stop`（不是 `down -v`），数据卷保留，18081 已关闭。
- 未直接调用 reducer / retriever / interpreter，未 mock，未写 mini-agent；只读 session/support 与已落盘 trace。

## 实际执行

- 12 sessions 创建成功，18 turns 执行；并发 2，session 内 turn 串行；单轮 180 秒超时，本批无客户端超时重试。
- 18 次模型调用全部返回 provider `401 Authentication Fails`；0 次成功，0 次工具/检索调用，0 条非空回答，0 条来源引用。
- 客户端 turn 延迟：n=18，p50 8.6s，p95 28.0s，max 28.1s；trace 内模型失败本身约 0.1–0.34s，说明部分延迟来自 SSE/代理收尾而非模型计算。
- 产品行为：401 被吞成 HTTP 200 + `answer=""`，trace `run_end` 为 `status=completed,error=null`，SSE 没有 `error` 事件。这是本轮最严重的真实 badcase，而不是测试脚本异常。
- 回充 state 仍在无回答时按用户文本转移：`ml-recharge-normal-closure` 两轮 state 断言通过并结案；`ml-recharge-correction-retract` 第 2 轮更正**没有撤回** `reseat_dock`，也没有补 `not_tried_actions`，属真实 state 发现。
- 吸力/拖地/广度 case 没有专用 state，正确保持 transcript-only；本轮无回答可做语义核对。

## 主要 blockers / badcases

1. **模型凭据 401（阻塞语义验收）**：18/18 模型调用 401；本地 env 中的 key 被 provider 判为 invalid。没有换 key、fake、mock 或重跑到绿。
2. **401 被产品吞掉**：HTTP 200 + 空 answer + `run_end completed`，前端/调用方会把失败看成成功空回答；应单独修错误传播与可见失败。
3. **embedding 模型路径双层嵌套**：12/12 session worker log 出现
   `FileNotFoundError: Path /assets/models/bge-small-zh-v1.5/bge-small-zh-v1.5 not found`，
   PDF coverage 修复失败。该问题独立于 401；本批模型先失败，因此未观察到检索结果。
4. **回充更正 state 未撤回**：`ml-recharge-correction-retract` 第 2 轮仍保留 `reseat_dock=failed`，`not_tried_actions` 为空。真实 state 缺陷，不是 oracle 改动。

程序结构层：12/12 `failed_structural`，全部由空回答/未检索/无引用触发；这**不是** 12 条语义坏答案，也不能算成功率。语义层状态为 `blocked_model_credentials`，未给出 advisory 通过结论。

## 证据与复现

本地证据（不提交 git）：

- `outputs/three-mainlines-v1/report.json`
- `outputs/three-mainlines-v1/transcripts.json`
- `outputs/three-mainlines-v1/trace-summary.json`
- `outputs/three-mainlines-v1/semantic-findings.json`
- `outputs/three-mainlines-v1/freeze.json` / `validation.json`

复现入口：

```powershell
$env:PUBLIC_PORT="18081"
docker compose -p rvs-eval-three --env-file .env.docker up -d
python scripts/run_three_mainlines.py --validate-only
python scripts/run_three_mainlines.py --run
# 只读取已停止容器的数据卷 trace
python scripts/collect_three_traces.py
python scripts/finalize_three_report.py
# runner 默认已执行 compose stop，保留 named volume
```

需要本地 `.env.docker` 内有真实可用的 `DEEPSEEK_API_KEY` 才能得到非空回答；本轮没有可用 key，因此语义验收仍需重跑。不要把 key 写进日志、报告或 git。

## 边界

- 12 个固定 session 是开发验收，不是 holdout、不是真实用户成功率。
- 程序分只做结构/证据绑定；本轮空回答不是语义结论。
- 401 与 embedding 路径问题都不是本轮 runtime/prompt 修复项；按要求保留 badcase。
- 未上传 outputs、密钥或知识原文；仓库只提交测试集、runner 与本文。
