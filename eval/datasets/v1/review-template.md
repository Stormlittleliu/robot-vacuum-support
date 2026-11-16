# 人工审核模板（v1）

状态：`pending_human`。本文件不是“已审核”证明。

## 每案必填

| 字段 | 允许值 | 说明 |
| --- | --- | --- |
| required_claims | pass / partial / fail / N/A | 逐条语义判断，不用正则命中代替 |
| forbidden_claims | pass / partial / fail / N/A | 出现即失败；N/A 仅当该案没有该维度 |
| state | pass / partial / fail / N/A | 只看稳定公共语义（active/resolved、已试/未试、scope 等），不强制唯一 next_action |
| source_semantic_support | pass / partial / fail / N/A | 引文存在≠支持结论；逐条核对来源是否真的支持 |
| overall | pass / partial / fail / N/A | 综合判定 |
| notes | 文本 | 失败理由、分歧、证据缺口 |

## 规则

1. `clauses` 没有该维度时填 `N/A`，不要填 `pass`；N/A 不计入通过率分子/分母。
2. 两名复核者独立看同一 rubric；分歧由第三人裁决并留 notes。
3. 来源存在性由 `validate_v1.py` 自动检查；来源是否语义支持结论仍由人工判断。
4. 不把本轮人工构造/未审核案例写成“真实用户验证”“成功率”或“官方售后结论”。
5. 发现真实失败时，按 `docs/case-generation.md` 的“加入真实失败”流程新增开发案例，不直接改冻结 holdout。
