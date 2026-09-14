# 通用文档解析 Agent · 三个子 Agent 注册（config.yaml）

> 本目录是**可提交到仓库**的子 Agent 注册记录。真正的生效配置写在仓库根目录
> 的 **`config.yaml`**（该文件被 `.gitignore` 覆盖，属本地私有配置，**不会**进入 git）。
> 因此这里以 `.md` 模板 + 注册 YAML 片段的方式，把三个子 Agent 的完整配置
> 固化下来，便于：① 重装/换机后重建；② 作为 PR/版本记录供他人查阅。

## 一、由谁注册 / 文件位置

DeerFlow 的自定义子 Agent **不是**放在独立的 `agents/` 目录里，而是统一注册在
**根目录 `config.yaml`** 的 `subagents.custom_agents` 节点下（`CustomSubagentConfig`）。

- 生效文件：`<repo-root>/config.yaml`
- 节点：`subagents:` → `custom_agents:`（每个子 Agent 一个 `-agent` 键）
- 纯脚本语言（Python/Bash）没有，是**配置驱动**，无需改后端代码。

## 二、要粘贴的注册 YAML

把以下整段复制到 `config.yaml` 的 `subagents.custom_agents:` 节点下，与已有的
`gov-summary-agent` / `gov-risk-agent` / `gov-todo-agent` 并列即可。配置会随每次
Gateway 启动加载（无需改动后端），修改后需**重启 Gateway** 生效。

```yaml
    agent-overview:
      description: 通用文档概览抽取子代理——提炼主旨、写作目的、整体背景、文档结构与关键对象。
      system_prompt: |
        你是"概览抽取子代理(agent-overview)"。收到一段文档原文(raw_doc_text)，请通读并只抽取文档的整体概览：不评价观点对错、不列行动项、不提建议。输出 Markdown，包含：
        1. 主旨：这篇文档总体上讲什么、想表达什么。
        2. 写作目的：为什么写、写给谁看、期望读者做什么。
        3. 整体背景：创作/使用的语境、所处场景。
        4. 文档结构：章节/模块如何组织、大致展开逻辑。
        5. 关键对象：涉及的人/组织/产品/主题/概念。
        仅依据给定原文，绝不虚构；原文没提到就标"原文未提及"。直接输出 Markdown。
      tools: []
      model: inherit
      max_turns: 4
      timeout_seconds: 900

    agent-insight:
      description: 通用文档洞察提炼子代理——挖掘核心观点、重要信息、矛盾点、潜在问题、约束、风险与局限。
      system_prompt: |
        你是"洞察提炼子代理(agent-insight)"。收到一段文档原文(raw_doc_text)，请深挖其中值得注意的洞察，聚焦信息与观点层面。输出 Markdown，包含：
        1. 核心观点：作者/文档反复强调或隐含的中心判断。
        2. 重要信息：事实性、指标性、定性的关键内容。
        3. 矛盾点：文档内前后不一、或与常识/现实冲突之处。
        4. 潜在问题：可行性、资源、依赖、流程上的隐患。
        5. 约束：限制条件、边界、前提。
        6. 风险：每条给影响与建议并标风险等级(高/中/低)，说明依据。
        7. 局限：方法论/覆盖范围/时效上的不完整。
        仅依据给定原文，每点尽量给出可回溯依据；无法确定就标"原文未明确"。用 Markdown 输出。
      tools: []
      model: inherit
      max_turns: 4
      timeout_seconds: 900

    agent-action:
      description: 通用文档行动项子代理——提取决议、任务、待办、责任人、时间节点与后续落地事项。
      system_prompt: |
        你是"行动项子代理(agent-action)"。收到一段文档原文(raw_doc_text)，请提炼其中可落地的执行事项，聚焦要做什么/谁来做/何时做。输出 Markdown，包含：
        1. 决议/结论：明确拍板或达成一致的事项。
        2. 任务与待办：可直接执行的行动，逐条列出。
        3. 责任人：每项的执行/负责主体(单位/角色/人)。
        4. 时间节点：截止、报送、实施、验收时限。
        5. 建议：作者/文档给出的可选做法或改进方向。
        6. 后续落地事项：需要继续跟进或再确认的。
        原文未明确的责任人/时间/优先级标"原文未提及"，不要编造。用 Markdown 表格(事项/责任人/时间节点/优先级)输出。
      tools: []
      model: inherit
      max_turns: 4
      timeout_seconds: 900
```

## 三、字段说明（`CustomSubagentConfig`）

| 字段 | 取值 | 说明 |
|------|------|------|
| `description` | 任意文本 | 主调度 Agent 在**决定是否委派**时读的一行简介，务必说清「何时该用这个子 Agent」。 |
| `system_prompt` | 多行块 (`\|`) | 子 Agent 的**行为契约**，即它收到 `raw_doc_text` 后必须输出的结构。 |
| `tools` | `[]` | 空数组 = **不给任何工具**（纯文本抽取子 Agent，无读/写/执行能力）。`None`/省略 = 继承父 Agent 全部；显式 `[]` = 禁用。 |
| `model` | `inherit` | `inherit` = 复用父（主调度）Agent 的模型。也可指定具体模型名。 |
| `max_turns` | 整数 | 该子 Agent 单次运行的最大对话轮数，防跑飞。默认 150（general-purpose）。 |
| `timeout_seconds` | 整数 | 超时兜底。默认 900（15 分钟）。 |

> `tools: []`（空数组）表示**这个子 Agent 没有工具**——对纯文档抽取场景是刻意为之：
> 它只读原文、输出 Markdown，不写库、不访问沙箱，杜绝越权。

## 四、主调度 Agent 怎么用它们

- 主调度 Agent（`intelligent-technical-document-analyzer`）通过 `task` 工具把
  `raw_doc_text` 同时分发给 `agent-overview` / `agent-insight` / `agent-action`。
- 三者**无依赖、并行**发起；框架不支持并行时**串行**执行，但**三个都必须成功**。
- 三份输出分别落到中间文件，由主 Agent 做「合并 → 去重 → 事实一致性/幻觉校验 →
  终稿」。
- 写库只由主 Agent 调用 `db_persist_skill` 完成，子 Agent **禁止**独立写库。

## 五、本目录文件对应关系

| 文件 | 内容 |
|------|------|
| `README.md` | 本文件——注册方式 + 可粘贴 YAML + 字段说明 |
| `agent_overview.system_prompt.md` | `agent-overview` 的 `system_prompt` 纯文本版 |
| `agent_insight.system_prompt.md` | `agent-insight` 的 `system_prompt` 纯文本版 |
| `agent_action.system_prompt.md` | `agent-action` 的 `system_prompt` 纯文本版 |

> 三个 `*.system_prompt.md` 是 config.yaml 里 `system_prompt: |` 内容的**原始抽取**，
> 去掉了 YAML 缩进。如修改 config.yaml 中的 prompt，请同步更新对应 `.md`，保持两者一致。
