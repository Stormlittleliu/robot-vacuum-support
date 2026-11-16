# PDF 检索修复与 release v1 验收

日期：2026-11-15（香港）  
状态：bounded release v1 slice `complete_with_known_limits`；P1 整体仍 `in_progress`。  
范围：修复 frozen rotating 集中 PDF 主文档检索未命中的原因，构建新不可变产物，跑一次 12 案验收 + 2 条同族 PDF 定向追问；不是 holdout，不是成功率，不是正式 40–60 集。

## 1. 症状与根因

症状：frozen rotating 的 `rm-breadth-pdf-3d-structured-light` 在旧产物上结构失败；回答声称资料没有测距原理，但 `扫地机器人100问.pdf` Q50 原文有“通过红外点阵投影测量物体距离和形状，实现精准避障。”

根因不是 PDF 抽取失败，也不是索引缺文档：

| 检查 | 证据 |
|---|---|
| 源 PDF 可抽取 | `pypdf` 抽取 5112 字符，frozen quote 逐字存在；PDF 页 4 / Q50 |
| 源索引含 PDF | 823 个 embedding；按 source 统计 PDF 8 块，五份 txt 为 101/101/201/211/201 |
| 旧产物索引 | 与源索引一致，PDF 仍是 8 个页级大块 |
| 真正问题 | PDF 没有走 txt 的编号条目切片（旧逻辑是文件名白名单），Q50 被稀释在 647 字符、覆盖 39–51 题的页块里；混合检索 top-4 被更小的 txt 块占据 |

离线复现（旧产物 `robot_vacuum-v4-5ef6e24fbf`，私有索引副本，无 LLM）：

| 查询 | 旧：PDF 排名 | 旧：Q50 quote | 新：PDF 排名 | 新：Q50 quote |
|---|---|---|---|---|
| frozen 原问“3D结构光避障是怎么测距和避障的？” | 未进 top-4 | 否 | 1 | 是 |
| 同族 1“3D结构光避障原理是什么？” | 未进 top-4 | 否 | 1 | 是 |
| 同族 2“红外点阵投影怎么测量物体距离？” | 2 | 是（页块） | 1 | 是 |

## 2. 修复层与实现

只改 runtime ingestion/coverage，不写 case 专用 prompt、不按文档名硬编码、不改 frozen cases/expected：

1. **通用编号条目切片** `runtime/core/tools/rag/vector_store.py`
   - 新增 `_numbered_faq_entries`：从抽取文本结构判断编号 FAQ，不依赖扩展名或文件名；要求至少 8 条且编号行占非空行 ≥20%，避免把普通短文列表误切。
   - `split_text` 先保留 clothing 逻辑，再对任意类型尝试编号条目切片；PDF 100 问因此从 8 个页块变成 101 个条目块。
2. **私有索引覆盖** `VectorStoreService.ensure_coverage()`
   - 实例启动时 artifact 的 `assets/rag_index` 已复制到会话 `workspace/chroma_db`；coverage 只写这个私有副本。
   - 对每个 `data/knowledge` 文件比较现有 source 分块与当前解析/切片结果的 whitespace-insensitive multiset。
   - 一致：不重新 embedding；不一致或缺失：写入新分块，成功后再删旧 id，并记录 `content_sha256` / `ingestion_schema`。
   - 磁盘上缺失的历史 source 不删除旧分块，只报告；单文件失败不阻断其他文档。
3. **启动调用** `runtime/core/tools/rag/service.py`
   - `RagSummarizeService` 在构造 retriever 前调用 coverage；失败时保留旧索引可用并记录错误，不伪造 ready。

保护边界：源 `assets/knowledge`、`assets/models`、`assets/rag_index` 与旧 artifact 均未修改；artifact 索引 sha256 与源索引相同 `6cfd9514...`。coverage 的写入路径只在实例/会话私有 workspace。

## 3. 回归与离线证据

- 新增 `tests/test_rag_ingestion_coverage.py`：PDF 编号切片、PDF 8→101 私有重建、二次启动幂等、txt 不重复 embedding、内容变更替换且无重复、missing persisted source 不删除、源索引不变；**5 passed**。
- 受影响 bounded run：`tests/test_rag_ingestion_coverage.py` + `tests/test_rotating_mainline_acceptance.py` **13 passed**（只跑这两个受影响文件，未重跑全量）。
- 离线私有副本：823 → 916 块；PDF 8 → 101；第二次 coverage `reindexed=[]`；源索引 hash 前后一致。
- 证据文件：[before probe](standalone.md#未包含)、[after probe](standalone.md#未包含)、[focused tests log](standalone.md#未包含)。

## 4. Live release 验收

新不可变产物：`E:/CODE/Myproject/agent-workspace/robot-vacuum-support-built/robot_vacuum-v4-24f3a3db39`（外部 cwd `run.py --check` ok）。旧 `5ef6e24fbf` 不覆盖。

一次 bounded live（隔离 8949，并发 2，12 sessions）：

- 14 个 frozen normal turns + 2 个显式 PDF 同族追问 = **16 turns**，未超预算；
- **12/12 cases 全部完成，0 runtime error / 0 not_run / 0 unassessed**；其中 1 条原始 `retrieval_attempted=false` 保留，仅因另行核实的 historical-source lineage 而不计为结构阻塞；不声称所有 raw checks 通过，也不声称逐 claim 蕴含；
- PDF 案完成，两条定向追问都检索到 PDF；不把结构/证据绑定写成语义或 claim 级通过；
- 1 条单独 lineage finding：`rm-recharge-failed-outcome` 第 2 轮没有新检索，但回答引用的 3 份文档都在第 1 轮检索结果中；原始 `retrieval_attempted` check 保留为 false，**不重写 oracle、不把原检查改成 true**，只把该 follow-up 的结构阻塞标记为不需要新检索，并单列 lineage；
- 12 个 session 的 handoff GET/POST 同文、support 只读、summary/sources sha 稳定；一次 restart 后 12/12 support、summary、sources 全部一致；
- PDF session 私有索引 source 计数：PDF 101、总 916；源/artifact 索引 hash 未变；
- headless GUI 对 PDF session 实际复制、UTF-8 下载（1275 字符，与摘要 sha 一致）、截图通过；浏览器与服务已停止，8949 已释放；
- 8900 / 8901 / 8950 / 8955 / 8956 运行前后均保持原状态；未启动 8902。

证据：[acceptance report](standalone.md#未包含)、[freeze](standalone.md#未包含)、[validation](standalone.md#未包含)、[screenshot](standalone.md#未包含)、[downloaded summary](standalone.md#未包含)。

说明：live 的 16 turns 与 restart 检查完成后，可选 GUI helper 因本地 `port_open` 参数顺序 bug 在清理阶段崩溃；随后用持久化 transcript/support_state/trace 做了一次 **0 model call** 的确定性恢复，并只重跑 GUI/重启/API 检查。原始 session、答案和 trace 未重跑、未覆盖；报告中的 `recovery` 字段记录该事实。

## 5. 交付配置

`outputs/release-v1/instance-8902.json`：

- `instance_id`: `robot-vacuum-support-release-v1`
- `name`: `扫地机器人售后客服`
- port: `8902`（已备好，由主模型启动；本切片未启动）
- 新 storage：`outputs/release-v1/manual-storage`
- artifact：`robot_vacuum-v4-24f3a3db39`
- environment_file：沿用既有安全实例注入路径；凭据值未读取/打印

不杀 8901、不迁移 8901/旧 manual-trial session；8900 旧配置保留历史。按既有 artifact binding 语义，旧 session 目录保留但不会自动出现在新 artifact 的列表里。

## 6. 已知限制与未完成

- 12 条固定 development session 不是 holdout、不是成功率、不外推真实用户总体。
- 结构分只证明回答非空、检索发生/主线证据绑定、回充 state subset；语义未由权威 Judge 重新评分。
- 没有正式 40–60 集、没有 holdout、没有新 Judge/rubric、没有人工压测。
- PDF 修复是通用“编号条目 + 私有索引覆盖”机制，但当前只在 6 份知识文档上验收；扫描版 PDF/OCR、无编号结构的 PDF 未覆盖。
- 只跑了 affected 测试文件，不是全量回归；历史 139 passed 仍是历史证据。
- 8902 未启动；本切片不把“配置已备”写成“持久服务已验收”。
- 未生成真实外部工单；GUI 复制使用页面内 clipboard 替身，不是 OS 剪贴板审计。
- `rm-recharge-failed-outcome` 第 2 轮的 `not_retrieved` 仍作为 lineage finding 保留，不声称已修复为“每轮必检索”。

## 7. 复现

```powershell
$py = "C:/Users/28980/anaconda3/envs/langchain/python.exe"

# focused regression (bounded)
& $py -m pytest tests/test_rag_ingestion_coverage.py tests/test_rotating_mainline_acceptance.py -q

# offline before/after (no LLM)
& $py outputs/release-v1/probes/probe_before_retrieval.py
& $py outputs/release-v1/probes/probe_runtime_coverage.py

# final artifact check from an external cwd
Push-Location E:/CODE/Myproject/agent-workspace/robot-vacuum-support-built
& $py robot_vacuum-v4-24f3a3db39/run.py --check
Pop-Location

# the one bounded live acceptance (do not rerun for green)
& $py scripts/release_v1_acceptance.py --artifact E:/CODE/Myproject/agent-workspace/robot-vacuum-support-built/robot_vacuum-v4-24f3a3db39

# start the prepared primary instance (user/main model action; not run in this slice)
& $py E:/CODE/Myproject/agent-workspace/robot-vacuum-support-built/robot_vacuum-v4-24f3a3db39/serve.py --instance-config outputs/release-v1/instance-8902.json
```
