# 多 Agent 协同编排设计（智能技术文档解析 Agent）· 含持久化

> 基于 **DeerFlow 2.0 多智能体协作调度机制**（Coordinator + Sub-agents，DAG 依赖图驱动并行/串行调度）。
> 本文件是**主 Agent（协调者）的操作手册**：定义「接收文档 → 调用技能 → 分解 → 并行分发 3 个子 Agent → 收集 → 合并去重 → 事实校验 → 终稿 → **结构化持久化** → 提醒」的完整编排逻辑。

---

## 一、角色总览

| 角色 | 代号 | 职责 | 输入 | 输出 |
|------|------|------|------|------|
| **主 Agent（协调者）** | `main-coordinator` | 接收文档、调用技能、任务分发、结果聚合、事实校验、终稿、**调 db_persist_skill 写库**、生成提醒 | raw_doc_text（+ 用户补充要求） | 最终报告（md）+ SQLite 结构化数据 |
| **核心信息提取 Agent** | `tech-core-agent` | 文档定位、设计目标、整体方案摘要、关键技术点 | raw_doc_text | 定位/目标/方案/关键技术点 |
| **技术约束 & 风险 Agent** | `tech-risk-agent` | 技术限制、版本约束、缺陷、风险、依赖 | raw_doc_text | 约束/版本/缺陷/风险[含等级]/依赖 |
| **落地任务梳理 Agent** | `tech-todo-agent` | 落地工作项、前置条件、关键节点、里程碑 | raw_doc_text | 待办清单 + 里程碑 |

> **重要**：写库动作**只属于主 Agent**。三个子 Agent 各自只产出 Markdown 文本，**绝不独立写库**，也绝不写最终报告之外的共享状态。

---

## 二、主 Agent 执行流程（9 步）

### 2.1 启动条件
用户提供技术文档文本（字符串）或上传 PDF/文档文件（先转为文本作为 `raw_doc_text`）。

### 2.2 执行步骤（DAG + 持久化）
```
[1. 接收原始文档 raw_doc_text]
        |
[2. 创建输出目录 ./outputs（沙箱内 /mnt/user-data/outputs/）]
        |
[3. 提取/落盘原始文档原始文本 raw.txt，计算 doc 元信息]
        |
[4. 加载/应用 tech_doc_analysis_skill，文档有效性校验]
        |
   +----+----+---------------------+
   |         |                     |
[Agent-A] [Agent-B]            [Agent-C]
core       risk                 todo
   |         |                     |
   +----+----+---------------------+
        |
[5. 收集三个子Agent返回，各写入 /mnt/user-data/outputs/core.md / risk.md / todo.md]
        |
[6. 主Agent: 合并 / 去重 / 一致性 & 事实(幻觉)校验]
        |
[7. 生成最终报告，写入 /mnt/user-data/outputs/tech_analysis_report.md]
        |
[8. (新增) 主Agent 调用 db_persist_skill，全部结构化数据写 SQLite]
        |
[9. 依据 storage_status 输出对话内提醒；失败则提示可导出本地 md]
```
三个子 Agent **无依赖、并行发起**；框架不支持并行则串行执行，但**三个都必须成功**才进入汇总。只要任一子 Agent 失败 → 跳过第 8 步，直接返回任务异常。

### 2.3 各步具体动作
1. **接收**：取得 `raw_doc_text`，并整理 `doc_name` / `doc_type` / `upload_time`。
2. **建目录**：`mkdir -p /mnt/user-data/outputs`。
3. **落盘原文**：把 `raw_doc_text` 写入 `/mnt/user-data/outputs/raw.txt`（供后端 sha256 哈希与摘要抽取）。
4. **校验**：加载并遵循 `tech_doc_analysis_skill` 契约；校验输入有效性。
5. **分发**：用 `task` 把 `raw_doc_text` 同时分发给三个子 Agent；等待三者返回，**无缺失无截断**；各自写入 `core.md` / `risk.md` / `todo.md`。
6. **汇总**：交叉合并、去重（同一技术点保留最完整表述）、一致性校核（目标↔约束↔待办不得矛盾）、**事实/幻觉校验**：核对每一条结论能否追溯到 `raw_doc_text`，标记 `hallucination_check_flag = pass|fail|unknown`。
7. **终稿**：按 `output_template.md` 填入合并内容，补「综合结论与建议」，保存 `tech_analysis_report.md`。
8. **持久化**：主 Agent 调用 `db_persist_skill`，传入第 1、5、7 步的元信息 + 三份子输出 + 终稿内容（或以已写好的文件路径传）。**仅在此刻一次写库**。
9. **提醒**：按返回的 `storage_status` 输出提醒。成功→打印 doc_id；失败→明确告知「存储失败可导出本地 md 备份」。

---

## 三、子 Agent 定义

### 3.1 Agent-A —— tech-core-agent
文档定位 / 设计目标 / 整体方案摘要 / 关键技术点（3–8 个，逐条简述）。

### 3.2 Agent-B —— tech-risk-agent
技术限制、版本约束、已知缺陷、潜在风险逐条标注**风险等级（高/中/低）+ 判定依据**、依赖组件清单。

### 3.3 Agent-C —— tech-todo-agent
待办清单[表]：工作项 / 前置条件 / 关键节点 / 优先级（高/中/低）+ 里程碑说明；原文未给时间点标注「原文未提及」。

---

## 四、合并、去重与一致性校核规则

1. **合并**：以 5 个维度（核心目标/技术要点/约束与局限/依赖/待办）为骨架，映射填入。
2. **去重**：同一技术点保留最完整一份，其余作补充/删除。
3. **一致性**：目标↔约束不得矛盾；待办须能追溯目标且与约束/依赖一致；高风险 → 对应前置待办优先。
4. **事实/幻觉校验**：每条结论必须能回溯 `raw_doc_text`；找不到原文依据的标注出来，不编造。
5. **无内容**：明确标注「原文未提及」，不得编造。
6. **术语**：保留原文术语，可加括号通俗解释。

---

## 五、持久化与异常策略

| 场景 | 处理 |
|------|------|
| 全部子Agent成功 + 校验通过 | 主Agent 调 `db_persist_skill` → `success`，打印 doc_id |
| 任一子Agent失败 | **不调用** db_persist_skill，直接返回任务异常（含失败子Agent名） |
| DB 连接/写入失败 | **不中断主流程**，md 文件照常生成作兜底；返回 `storage_status=failed`，主Agent 明确告知用户「存储失败，可导出本地 md」 |
| 大文本超长 | 入库前按 `SUBAGENT_LIMIT=60k` / `REPORT_LIMIT=100k` / `SHORT_LIMIT=500` 截断，并在 `warnings` 标注 |
| doc_id 已存在（重跑） | `doc_source` 走 upsert，`agent_intermediate` / `doc_analysis_result` 先删后插，保持幂等 |

---

## 六、质量检查清单（主 Agent 交付前自检）

- [ ] 技能契约是否被正确应用（5 维度齐全）。
- [ ] 三个子 Agent 是否都成功返回（无遗漏）。
- [ ] 报告是否去重且逻辑一致，事实/幻觉校验已执行并标记 flag。
- [ ] 是否标注了「原文未提及」的下缺项，而非编造。
- [ ] 风险等级 / 优先级是否给出依据。
- [ ] 最终报告是否已持久保存至 `/mnt/user-data/outputs/tech_analysis_report.md`。
- [ ] 是否已调用 `db_persist_skill` 并依据 `storage_status` 给出提醒。
