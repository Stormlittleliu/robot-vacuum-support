# 三主线修复记录 v1（2026-11-18）

本文记录 three-mainlines-v1 发现问题的修复与完整产品复验。结论只来自完整 Docker 产品的正常 HTTP/SSE 入口和只读落盘 trace；组件测试仅作定位辅助。

## 已修并与整产品复验

| 问题 | 修复 | 复验证据 |
|---|---|---|
| A. provider 401 被吞成 HTTP 200 空回答 / `run_end completed` | `react_loop` 不再吞模型异常/空回答，改为 typed failure；compiler/worker/server 传递 code、retryable、requires_configuration；SSE 发 `error`，session `last_status=error`；trace/错误消息脱敏，不记录 provider 请求 header | invalid 隔离配置整产品：HTTP 200（SSE 已开始）+ `error` 事件 + `done`，无 `answer`；`model_auth_failed`、`requires_configuration=true`、`last_status=error`、`run_end=error` |
| B. embedding 路径双层嵌套 | 新增 `resolve_model_path`，在真实 worker `AH_MODEL_ROOT` 处解析；完整模型目录绑定不再追加，legacy 父模型根仍兼容 | 回归 14 个新 session worker log 双路径 0、`FileNotFoundError` 0；12/12 suite session 有检索，PDF coverage failure 0 |
| C. 解释器不可用时更正未撤回 | fallback 增加 clause-scoped correction negation：明确“没有做 X”撤回 effective tried、写 not-tried 与 fact audit；结果否定“没清理干净”不误撤回；无法明确绑定的更正写 `correction_unresolved` | 原 case 第 2 轮 `tried=[]`、`not_tried=[reseat_dock, check_auto_return]`；反例 `clean_contacts` 保持 tried、`not_tried=[]`；恢复后正常对话有 completed trace |

## 完整回归

- 镜像 `rvs-backend:three-fix-v1`，artifact `robot_vacuum-v4-e5432dc53e`。
- 隔离 `rvs-eval-three` / 18081；正式 18080 未动。
- 12/12 session、18/18 turn；10 passed structural、2 failed structural。
- 2 个 failed 仅为第 2 轮无新 `rag_retrieve`（`retrieval_attempted=false`），不是模型失败；session 级检索 12/12，turn 级 16/18。
- 39 次模型调用，0 model error、0 failed run、0 transport error；所有 suite session `last_status=completed`。
- 冻结 `cases.json`/`suite.json` 与 hash 未变；旧报告未覆盖。

## 凭据状态

- `.env.docker` 只有测试占位 key，不能用于语义验收（本文不记录其值）。
- 本轮隔离复验使用正式实例 environment_file 中既有有效 DeepSeek 凭据；未打印、未复制进仓库 tracked 文件。
- 当前无凭据阻塞。若后续 provider 再次拒绝，唯一需要用户做的是更新实例 environment_file 中的 `DEEPSEEK_API_KEY`。

## 未完成 / 明确边界

- 2 个第 2 轮无新检索的 residual finding 未修，未改 runner 或预期掩盖。
- 模型常输出“参考来源：<文件名>”而非 `[文件名]`；保留答案，把缺方括号作为软验证缺口，suite 按文件名核对。
- 401 不是 state 缺陷的已证独立根因；已证的是 fallback 撤回 gap。normal LLM interpreter 在 401 下未运行，不能宣称它当时会撤回。
- 未 push；临时 invalid compose/volume 已清理，主 named volume `rvs-eval-three_rvs-data` 保留。
- 本轮不是 holdout、不是成功率、不是权威 Judge。

## 本地证据

- `outputs/three-mainlines-v1/regression-v2/report.json`
- `outputs/three-mainlines-v1/regression-v2/transcripts.json`
- `outputs/three-mainlines-v1/regression-v2/trace-summary.json`
- `outputs/three-mainlines-v1/regression-v2/semantic-findings.json`
- `outputs/three-mainlines-v1/regression-v2/counterexample.json`
- `outputs/three-mainlines-v1/regression-v2/recovery-success.json`
- `outputs/three-mainlines-fix-v1/report.json` / `report.md`
- `outputs/three-mainlines-fix-v1/error-path-trace-summary.json`
