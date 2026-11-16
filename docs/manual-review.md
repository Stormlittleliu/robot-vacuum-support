# 人工审核 GUI

本页说明 `robot-vacuum-support` 的本地人工审核入口。它只读 `eval/datasets/v1` 和
`eval/results`，把人工判定单独写到 `eval/reviews/`，不调用产品 runtime、不触发模型、
不运行 holdout，也不把人工标注自动计入 Agent 得分。

## 启动与入口

```powershell
$py = "C:/Users/28980/anaconda3/envs/langchain/python.exe"
cd E:/CODE/Myproject/agent-workspace/robot-vacuum-support
& $py eval/review_server.py --host 127.0.0.1 --port 8950 --store eval/reviews --state-file eval/reviews/server_state.json
```

打开 <http://127.0.0.1:8950>。当前交付使用 WMI + `pythonw.exe` 隐藏窗口持久启动；
`GET /api/health` 是存活权威，`eval/reviews/server_state.json` 记录启动 PID/端口。

## 两个审核对象

- **案例定义**：看题目、逐轮用户输入、required / forbidden claims、required behaviors、
  expected state 和 evidence quote 是否合理。保存到 `reviewScope=case_definition`。
- **运行回答**：先选 development run，再看该 run 的 actual answer、延迟、来源与 trace 摘要。
  `expected` 只作为对照，绝不当作 actual。保存到 `reviewScope=run_answer`，带 `runId`。
  只有 selectable run 至少有一轮真实 answer 时才能保存运行回答审核；状态检查 run 没有
  回答文本时显示“暂无运行结果”，不会把 expected 填成 actual。

两套记录使用不同 `reviewId`，互不覆盖；列表也并排显示“案例”和“回答”两个徽标。

## 状态、保存和冲突

- 状态只有 `passed` / `failed` / `uncertain`（通过 / 不通过 / 存疑）；初始为未审核。
- 未保存修改会显示黄点与“未保存”，并在 `localStorage` 留草稿；切换、刷新或关页前有
  明确提示。保存成功显示时间、revision 和审核人；刷新/重启服务后可恢复。
- 每条记录绑定 `caseHash`（排除 `review` 元数据的 case 定义 hash）、`suiteRevision`、
  `reviewScope`、`runId`、`datasetCasesSha256`、`runSnapshot`、`reviewer`、
  `createdAt/updatedAt`、`revision`。
- 保存时带 `expectedRevision`。服务端发现 revision 或 dataset hash 变化会返回 `409`，
  UI 保留本地输入和草稿，只由用户显式点“加载服务端最新记录”才放弃本地内容。
- 改题或换 suite 版本后，旧记录不会覆盖新题；界面在“历史快照 / 旧 hash 记录”里展示。

## 留出集

默认只加载 32 条 development。点击“查看留出集”会先显示说明，确认后调用
`POST /api/holdout/expose`，首次写入一条曝光事件到 `eval/reviews/exposures.json`，
之后才展示 16 条 holdout。页面没有运行按钮，服务端也不提供运行 holdout 的能力；
split、冻结 hash 和原始数据都不会被修改。

## 数据位置

```text
eval/reviews/
  store.json                 # store schema/created_at，没有伪造审核
  reviews/<reviewId>.json    # 每条人工审核，独立 revision
  exposures.json             # 首次 holdout 显式曝光事件
  server_state.json          # 持久服务 PID/端口/启动时间
  server.log                 # 隐藏启动日志
```

原始数据只读：`eval/datasets/v1/{suite,cases,split,audit}.json*`；运行快照只读：
`eval/results/*/{run.json,cases.jsonl}`。服务没有任何“按路径读文件”的 API，因此不会
展示 `.env` 或目录外文件。

## 导出

页面右上角“导出 JSON / CSV”，或直接请求：

```text
GET /api/export?format=json
GET /api/export?format=csv
```

CSV 使用 UTF-8 BOM，保留备注换行、引号和逗号；包含 `caseHash`、`suiteRevision`、
`reviewScope`、`runId`、`revision`、`createdAt/updatedAt`，并额外包含
`record_type=holdout_exposure` 的曝光记录。JSON 包含 `reviews` 和 `exposures` 两个数组。
导出内容只是人工审核材料，不改变 run 的自动检查结果，也不是 Agent 成功率。

## 安全与边界

- 只绑定 `127.0.0.1`；校验 Host 必须是 loopback，POST 要求同源 Origin/Referer、
  `X-Requested-With: manual-review` 和 JSON content type，无 CORS。
- 静态文件白名单只有 `index.html`、`app.js`、`styles.css`；`/static/../...` 不可读。
- 无审核种子数据；自动测试和 headless 验收全部使用临时 store，不写生产 `eval/reviews`。
- 页面只读运行快照，不重新调用模型；holdout 不运行，生产评测结论不改。
- 历史 run 的 dataset hash 与当前不一致时，界面标“历史快照”，不冒充当前结果。

## 验证

```powershell
$py = "C:/Users/28980/anaconda3/envs/langchain/python.exe"
& $py -m pytest tests/test_manual_review.py -q
& $py eval/review_browser_acceptance.py --port 8951 --report outputs/manual-review-browser.json --output outputs/manual-review.png
```

浏览器验收使用 headless Chrome、临时 store，真实走完填写 → 保存 → 刷新 → 导出，并验证
holdout 曝光、CSV BOM/备注换行和原始 dataset hash 不变。
