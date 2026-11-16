# 面试演示：两个 2 分钟真实失败案例

> 日期：2026-11-17
> 用途：面试口述与追问。每段约 2 分钟，只讲已有记录能证明的部分。
> 边界：两段都是 development 证据，不是 holdout、不是成功率；案例 1 是 context/state 修复，案例 2 是 RAG ingestion 修复，**都不证明 prompt 带来了因果提升**。

## 案例 1：语义层已判 failed，状态层却写成 unknown（约 2 分钟）

### 0:00–0:30 症状

用户说：“机器人自己回不到充电座，我把它放上去也充不上电。该先确认什么？”

旧产物 `robot_vacuum-v4-6aa0ba49c6` 的链路是：语义层已经给出 `reseat_dock / outcome=failed`，证据就是“我把它放上去也充不上电”；但状态层把它写成 `unknown`。prompt 随后投影成“重新对准充电座：已做但结果未知”，回答也说“结果还不清楚”。

这里最容易误判成“模型编动作”。实际不是：动作族有用户原话依据，`confirmed_fabrication=false`。失败在状态表示没有把已验证结果传下去。

### 0:30–1:10 根因与修复层

旧状态层用动作 regex 重算结果，`放上去` 没匹配到；匹配到后文问句时，问句之后又没有结果，于是返回 `None`，最终写成 `unknown`。

所以我先定 oracle：**有语义结果且带用户 quote 时，状态优先消费它；regex 只在没有语义结果时兜底；匹配到问句不能猜成功或失败。** 改动落在 state/execution，而不是再加一句 prompt。同期把来源范围绑定到用户自身观察，避免第三方/假设句变成当前用户事实；显示标签与动作 id 分开，raw quote 保留。

### 1:10–1:45 验证与反例

- 旧 `6aa0`：`semantic_outcome=failed`、`state_outcome=unknown`，有保存的 trace 和 quote/turn/message id。
- 新 `6b90` 真实 smoke：同一 case 第 1 轮 `tried_actions[0] = reseat_dock / failed`，观察为手动充电失败。
- 最终 `24f3` final12：`rm-recharge-failed-outcome` 第 1 轮 state 为 `failed`，第 2 轮仍保留 `failed`，状态为 `passed_structural_with_lineage_finding`，第 2 轮原始 `retrieval_attempted=false` 未被改写成通过。
- 反例同时看：第三方“朋友家机器人手动能充”不写成本用户事实；明确更正会撤回旧 tried；明确自动回充成功才结案；“特意对准后仍失败”不降级成 unknown。

### 1:45–2:00 诚实边界

这不是 prompt 的因果提升，也不是成功率。它说明的是：语义层已经有的 evidence-bound 结果，不能在 context/state 层丢失；修复后用同 case 的 old→new 证据和四类反例验收。样本是固定 development 批次，没有 holdout 和权威语义 Judge。

**证据：** [optimization-loops.md](optimization-loops.md) 第 2 节、[product-closed-loop.md](product-closed-loop.md)、[fact-boundary-closeout.md](fact-boundary-closeout.md) 第 2 节；原始 trace 和 final12 报告在本机 `outputs/`，未随仓库发布。

---

## 案例 2：PDF 有原文，FAQ 却漏召回（约 2 分钟）

### 0:00–0:30 症状

冻结 case `rm-breadth-pdf-3d-structured-light` 问：“3D结构光避障是怎么测距和避障的？”旧产物回答“资料没有原理”。但 `扫地机器人100问.pdf` 的 Q50 原文明确有：“通过红外点阵投影测量物体距离和形状，实现精准避障。”

所以第一反应不能是“PDF 抽取失败”，也不能直接改 prompt 让模型“再查查”。

### 0:30–1:10 诊断

离线检查：`pypdf` 能抽取 5112 字符，Q50 的 frozen quote 逐字存在；源索引 823 条 embedding 里也有这份 PDF。真正的问题是分块：PDF 仍是 8 个页级大块，而五份 txt 已按编号条目切成 101/101/201/211/201 块。

旧逻辑的编号条目切片被文件名白名单限制在 `.txt`，PDF 没走这条路径。Q50 被稀释在一个 647 字符、覆盖 Q39–51 的页块里；混合检索 top-4 被更小的 txt 块占满，PDF 原问未进 top-4。根因在 RAG ingestion / coverage，不在 prompt。

### 1:10–1:45 修复与验证

修复只改 ingestion/coverage，不写 case 专用 prompt、不按文档名硬编码：

1. 从文本结构识别编号 FAQ，不依赖扩展名或文件名；至少 8 条、编号行占非空行 ≥20%，避免误切普通列表。
2. 实例启动时对私有索引副本做 `ensure_coverage()`：比对当前解析/切片结果的 multiset；一致则不重复 embedding，不一致才替换；只写会话私有 `workspace/chroma_db`。

结果：PDF 8 → 101 块，私有索引 823 → 916；源索引和 artifact 索引 sha256 仍为 `6cfd9514...`，第二次 coverage `reindexed=[]`。focused 5 条测试与 13 条受影响测试通过；live PDF case 结构通过，两条同族追问都检索到 PDF。

### 1:45–2:00 诚实边界

这也是 RAG 修复，不是 prompt/context 的因果提升。修复只覆盖当前 6 份资料的编号结构；扫描版 PDF/OCR、无编号结构的 PDF 未覆盖。结构通过不等于逐 claim 语义正确，也不能写成检索成功率。

**证据：** [pdf-retrieval-fix.md](pdf-retrieval-fix.md)、[release-v1.md](release-v1.md)；完整 before/after 探针和 final12 报告在本机 `outputs/`，未随仓库发布。

---

## 两段怎么连起来（30 秒）

> “两个失败分别落在两层：案例 1 是语义结果已经存在，但 state/execution 丢了；案例 2 是原始知识已经在索引里，但 ingestion 分块让检索够不到。它们都不是靠改 prompt 解决的，验收也都保留了反例和原始失败记录。”

**不要讲：** “prompt 让准确率提升”“9→3 提效”“成功率”“全部引用正确”“所有 PDF 都能答”。
