# 多 Agent 协同编排设计（通用文档解析 Agent）· 含证据溯源与持久化

> 基于 **DeerFlow 2.0 多智能体协作调度机制**（Coordinator + Sub-agents，DAG 依赖图驱动并行/串行调度）。
> 本文件是**主调度 Agent（协调者）的操作手册**：定义「接收文档 → 调用技能 → 分解 → 并行分发 3 个子 Agent → 收集 → 合并去重 → **抽取 Claim → 逐条原文证据核验** → 终稿 → **结构化持久化** → 提醒」的完整编排逻辑。
> **适配通用非结构化文档**（书籍节选 / 白皮书 / 介绍类读物 / 会议记录 / 会议纪要 / 任务安排表 / 各类业务文档），不再限定技术文档。
>
> **V1 → V2 的增量**：第 8、9 步（Claim 抽取 + Evidence Agent 证据核验）与 5 张表结构。
> V1 的三子 Agent 并行、合并去重、终稿、持久化、提醒**全部保持原样**。

---

## 一、角色总览

| 角色 | 代号 | 职责 | 输入 | 输出 |
|------|------|------|------|------|
| **主调度 Agent** | `main-coordinator` | 接收文档、调用技能、任务分发、结果聚合、**Claim 抽取**、终稿、**调 db_persist_skill 写库**、生成提醒 | raw_doc_text（+ 用户补充要求） | 完整结构化报告（md）+ SQLite 结构化数据 |
| **概览抽取 Agent** | `agent-overview` | 梳理文档整体主旨、写作目的、整体背景、文档结构、关键对象 | raw_doc_text | 主旨/目的/背景/结构/关键对象 |
| **洞察提炼 Agent** | `agent-insight` | 挖掘核心观点、重要信息、矛盾点、潜在问题、约束、风险、局限 | raw_doc_text | 观点/信息/矛盾/问题/约束/风险/局限 |
| **行动项 Agent** | `agent-action` | 提取会议决议、任务、待办、责任人、时间节点、建议、后续落地事项 | raw_doc_text | 决议/任务/待办[责任人/时间]/建议 |
| **原文证据核验 Agent** | `agent-evidence` | **V2 新增**。对合并后的每条关键结论（Claim）判定其在原文中是否站得住，给出四态 + 逐字证据 + 定位 + 置信度；**不重新分析文档** | raw_doc_text + claims 列表 | `evidence_results[]`（JSON） |

> **重要**：写库动作**只属于主调度 Agent**。四个子 Agent 各自只产出文本，**绝不独立写库**，也绝不写最终报告之外的共享状态。
>
> **两个"事实校验"并存、不互相替代**：
> - `hallucination_check_flag`（V1）：主调度 Agent 的**整体自评**，`pass|fail|unknown`，语义与取值完全不变。
> - `evidence_results`（V2）：**逐条可追溯**的证据核验，四态 + 证据原文 + 定位。

---

## 二、主调度 Agent 执行流程（12 步）

### 2.1 启动条件
用户粘贴文本，或上传 PDF/Word/Excel/文本文件（先转为文本作为 `raw_doc_text`）。文档类型支持：书籍、白皮书、介绍文档、会议记录、任务计划表等。

### 2.2 执行步骤（DAG + 证据核验 + 持久化）
```
[1. 用户粘贴文本 / 上传文档 → 得到 raw_doc_text]
        |
[2. 创建输出目录 ./outputs（沙箱内 /mnt/user-data/outputs/）]
        |
[3. 提取完整原始文本 raw_doc_text 并落盘 raw.txt，整理 doc 元信息(doc_id/name/category/upload_time)]
        |
[4. 加载/应用 doc_analysis_skill，文档有效性校验]
        |
   +----+----+---------------------+
   |         |                     |
[agent-     [agent-              [agent-
 overview]   insight]             action]
   |         |                     |
   +----+----+---------------------+
        |
[5. 收集三个子Agent返回，各写入 ./outputs/intermediate_overview.md / _insight.md / _action.md]
        |
[6. 主调度Agent: 合并 / 去重 / 一致性校核]
        |
[7. (V2) 抽取关键结论 Claim（C001…），落盘 claims.json]
        |
[8. (V2) 把 「raw_doc_text + claims」 交给 agent-evidence 逐条核验 → evidence_results]
        |
[9. 生成完整结构化报告（每条核心结论附「证据核验」小节），写入 ./outputs/final_analysis_report.md]
        |
[10. 主调度Agent 调用 db_persist_skill，全部结构化数据写 SQLite（5 张表）]
        |
[11. 依据 storage_status 输出对话内提醒；失败则提示可导出本地 md]
```

依赖关系说明：

- 三个**抽取**子 Agent（overview / insight / action）**无依赖、并行发起**；框架不支持并行则串行执行，但**三个都必须成功**才进入汇总。任一失败 → 跳过第 10 步，直接返回任务异常。
- `agent-evidence` 是**串行**的：它必须等第 6 步合并完成、第 7 步 Claim 抽完才能发起。
- `agent-evidence` 失败**不阻断**主流程（降级为「未核验」，见 2.3 第 8 步与第五节）。

### 2.3 各步具体动作
1. **接收**：取得 `raw_doc_text`，并整理 `doc_name` / `doc_category` / `upload_time`，生成 `doc_id`。
2. **建目录**：`mkdir -p /mnt/user-data/outputs`（即 `./outputs`）。
3. **落盘原文**：把 `raw_doc_text` 写入 `/mnt/user-data/outputs/raw.txt`（供 sha256 哈希与摘要抽取）。
4. **校验**：加载并遵循 `doc_analysis_skill` 契约；校验输入有效性（有无文本、是否为空、是否可解析）。
5. **分发**：用 `task` 把 `raw_doc_text` 同时分发给三个抽取子 Agent；等待三者返回，**无缺失无截断**；各自写入 `intermediate_overview.md` / `intermediate_insight.md` / `intermediate_action.md`。
6. **汇总**：交叉合并、去重（同一信息点保留最完整表述）、一致性校核（主旨↔观点↔行动项不得矛盾）；并做 **V1 整体自评** `hallucination_check_flag = pass|fail|unknown`（语义不变）。
7. **抽取 Claim（V2）**：
   - 从「合并去重后的三份输出 + 你补的综合结论」中抽出**值得核验的关键结论**，建议 5–15 条。
   - 编号 `C001`、`C002`…（**单份文档内唯一**；跨文档唯一性由 `(doc_id, claim_id)` 复合主键保证）。
   - 每条含 `claim_id` / `source_agent`(overview|insight|action|coordinator) / `claim_text` / `claim_type`(fact|risk|action|summary|inference)。
   - 落盘 `/mnt/user-data/outputs/claims.json`。**不新增 Agent、不新增框架**——这是主调度 Agent 的一个推理步骤。
8. **证据核验（V2）**：用 `task` 调 `agent-evidence`，任务文本里给 `raw_doc_text`（截断处标明）+ claims 列表；拿回 `{"evidence_results":[...]}`。**拿到后必须核对**：
   - 每条 Claim 有且只有一条结果；漏检的标「未核验」（不要替它编状态）。
   - `verification_status` 只认四态；其他取值 → 该条按「未核验」处理。
   - 任何 `page` **一律置 null**（当前转换不保留页码，给页码即是编造）。
   - 声称 `verified`/`contradicted` 但给不出证据原文或原文片段 → 按 `unsupported` 处理。
   - 调用失败/无法解析 → 保留全部结论并统一标「未核验」，`evidence_results` 记为空，**继续**后续步骤。
   - 落盘 `/mnt/user-data/outputs/evidence_results.json`。
9. **终稿**：按输出模板填入合并内容，**每条核心结论下附「证据核验」小节**，补「综合结论与建议」，保存 `/mnt/user-data/outputs/final_analysis_report.md`。
10. **持久化**：主调度 Agent 调用 `db_persist_skill`，传入元信息 + 三份子输出 + **claims + evidence_results** + 终稿。**仅在此刻一次写库**（5 张表）。
11. **提醒**：按返回的 `storage_status` 输出提醒（含证据核验计数）。成功→打印 doc_id；失败→明确告知「存储失败可导出本地 md 备份」。

---

## 三、子 Agent 定义（通用能力，不再限定技术架构）

### 3.1 agent-overview —— 概览抽取 Agent
文档整体主旨、写作目的、整体背景（创作/使用语境）、文档结构（章节/模块组织）、关键对象（人/组织/产品/主题）。
- **输出结构**：`主旨 / 写作目的 / 背景 / 结构 / 关键对象[列表]`。

### 3.2 agent-insight —— 洞察提炼 Agent
核心观点、重要信息/事实、矛盾点（前后不一/自相矛盾）、潜在问题、约束、风险、局限（可行性/边界/可靠度）。
- **输出结构**：`核心观点 / 重要信息 / 矛盾点 / 潜在问题 / 约束 / 风险[含等级/依据] / 局限`。

### 3.3 agent-action —— 行动项 Agent
会议决议、任务、待办事项、责任人、时间节点、建议、后续要落地事项。
- **输出结构**：`决议 / 任务[表：事项/责任人/时间节点/优先级] / 建议 / 后续落地事项`。

### 3.4 agent-evidence —— 原文证据核验 Agent（V2 新增）

**唯一职责**：判断「这个 Claim 能否从原始文档中找到依据」。

- **输入**：`raw_doc_text`（可能被截断，截断处有标记）+ claims 列表（每条形如 `{"claim_id","source_agent","claim_text","claim_type"}`）。
- **输出**：一个 JSON 代码块 `{"evidence_results":[{...}]}`，每条含 `claim_id` / `source_agent` / `claim` / `verification_status` / `evidence_text` / `source_location{page,section,source_offset,source_locator}` / `confidence`。
- **四态定义**：

  | 状态 | 含义 | 例 |
  |------|------|-----|
  | `verified` | 原文**明确**支持 | 原文「本政策支持人工智能企业发展。」↔ 结论「该政策支持人工智能企业发展。」 |
  | `contradicted` | 原文**明确**相反 | 原文「本政策不适用于个人开发者。」↔ 结论「本政策适用于个人开发者。」 |
  | `unsupported` | 原文找不到足够证据 | 结论「该政策提供500万元补贴。」，原文无补贴金额 |
  | `inferred` | 基于原文信息的**合理推断**，原文未直接陈述 | 原文「企业可以申请研发资金支持。」↔ 结论「该政策可能降低企业研发资金压力。」 |

- **判定优先级**：`contradicted` 优先于 `verified` → 再看 `verified` → 再看 `inferred` → 最后 `unsupported`。
  若原文被截断且相关内容可能落在截断处 → 判 `unsupported`（**不要猜**）。
- **它明确不做**：不总结文档、不重跑概览/洞察/行动、不产出新业务结论、不评价文档质量、不改写 Claim。
- **无工具**：`tools: []`，纯文本推理。它**无法**自己去翻原始文件，只能核验任务文本里给的原文——这在机制上排除了「偷偷读了别的东西」。

> 完整的提示词见 `subagents/agent_evidence.system_prompt.md`；
> 契约的**可执行规格**（四态语义 + 抗伪造规则 + 报告渲染）与测试见 `evidence/evidence_contract.py`。

---

## 四、合并、去重与一致性校核规则

1. **合并**：以「概览 → 洞察 → 行动」为骨架，映射填入报告结构；三者天然互补（是什么/有什么问题/该做什么）。
2. **去重**：同一信息点（如同一结论、同一待办）保留最完整一份，其余作补充/删除；避免跨章节重复粘贴。
3. **一致性**：
   - 主旨 ↔ 观点 ↔ 行动项不得矛盾（如「主旨主张可行」与「洞察指出关键局限」需给出边界说明）。
   - 行动项必须能追溯到观点/信息，且与洞察中的约束/风险一致。
   - 风险高 → 对应行动项优先级需相应提升。
4. **整体自评（V1，语义不变）**：主调度 Agent 自评 `hallucination_check_flag = pass|fail|unknown`，落 `doc_analysis_result`。
5. **逐条证据核验（V2）**：对抽出的每条 Claim，必须按 `agent-evidence` 返回的四态之一如实标注，**不得美化**：

   | 状态 | 报告写法（不得违反） |
   |------|----------------------|
   | `verified` | 状态 verified + 证据原文 + 来源（章节 / 行号 / 逐字片段）+ 置信度 |
   | `contradicted` | 状态 contradicted + 反向证据 + 「原文存在与该结论相反的信息」 |
   | `unsupported` | 状态 unsupported + ⚠️「未在原始文档中找到足够的直接依据」 |
   | `inferred` | 状态 inferred + ⚠️「该结论属于基于原文信息的推断，原文没有直接陈述」 |
   | 未核验 | 明写「未核验」，**不得默认按 verified 展示** |

   **铁律：`unsupported` / `inferred` 绝不允许被写成、渲染成、或读起来像 `verified`。**

   防伪三条（须在合并阶段就执行）：
   - 任何 `page` 一律置 `null`——当前文档转换不保留页码，给页码就是编造；
   - 声称 `verified` / `contradicted` 却拿不出证据原文或原文片段 → 按 `unsupported` 处理；
   - 拿不准就标 `inferred` 或「未核验」，**宁可保守，不可虚报**。

6. **无内容**：某维度原文未提及，明确标注「原文未提及」，不得编造（特别是会议纪要中未被明确记录的决定/负责人）。
7. **语言与术语**：保留原文术语/专名，可加括号通俗解释；全文语言与用户保持一致（默认中文）。

---

## 五、持久化与异常策略

| 场景 | 处理 |
|------|------|
| 三个抽取子Agent 全部成功 + 校验通过 | 主调度Agent 调 `db_persist_skill` → `success`，打印 doc_id |
| **任一抽取子Agent 失败** | **不调用** db_persist_skill，直接返回任务异常（含失败子Agent名） |
| **`agent-evidence` 调用失败 / 返回无法解析** | **不阻断主流程**：报告保留全部结论并统一标「未核验」，`evidence_results` 记为空数组；`claims` 照常入库；照常出终稿与持久化 |
| **`agent-evidence` 只核验了部分 Claim** | 已核验的照常展示入库；未核验的逐条标「未核验」，**不得用其他条结果顶替** |
| **模型给出 `page`** | 一律强制置 `null`；报告不展示页码，并可在 warnings 里记录该次编造尝试 |
| **声称 `verified` 但无任何证据** | 降级为 `unsupported`（报告写「未在原始文档中找到足够的直接依据」） |
| DB 连接/写入失败 | **不中断主流程**，md 文件照常生成作兜底；返回 `storage_status=failed`，主调度Agent 明确告知用户「存储失败，可导出本地 md」 |
| 大文本超长 | 入库前按 `SUBAGENT_LIMIT=60k` / `REPORT_LIMIT=100k` / `SHORT_LIMIT=500` / `EVIDENCE_LIMIT=2k` 截断，并在 `warnings` 标注 |
| doc_id 已存在（重跑） | `doc_source` 走 upsert，`agent_intermediate` / `doc_analysis_result` / `document_claim` / `evidence_verification` 先按 doc_id 删后插，保持幂等 |

---

## 六、质量检查清单（主调度 Agent 交付前自检）

- [ ] 技能契约是否被正确应用（概览/洞察/行动 三个维度齐全）。
- [ ] 三个抽取子 Agent 是否都成功返回（无遗漏）。
- [ ] 报告是否去重且逻辑一致，V1 整体自评已执行并标记 `hallucination_check_flag`。
- [ ] **（V2）** Claim 是否抽出并编号（`C001…` 单份文档内唯一、字段齐全、`claim_type` 合法）。
- [ ] **（V2）** 每条 Claim 是否都有恰好一条核验结果；漏检的是否已标「未核验」。
- [ ] **（V2）** 报告里的证据核验小节是否四态区分明确；`unsupported` / `inferred` 是否都带 ⚠️ 且**没有**被写成 verified。
- [ ] **（V2）** 是否所有 `page` 都是 `null`（**没有**任何编造页码）。
- [ ] **（V2）** `verified` / `contradicted` 的条目是否都给出了证据原文或原文片段。
- [ ] 是否标注了「原文未提及」的缺失项，而非编造（尤其会议纪要的决议/负责人）。
- [ ] 风险等级 / 优先级是否给出依据。
- [ ] 最终报告是否已持久保存至 `/mnt/user-data/outputs/final_analysis_report.md`。
- [ ] 是否已调用 `db_persist_skill` 并依据 `storage_status` 给出提醒（含证据核验计数）。
