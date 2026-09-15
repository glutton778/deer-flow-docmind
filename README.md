# DocMind — 多智能体通用文档智能解析 Agent

> **DocMind** · 基于 **DeerFlow**（LangGraph 多智能体框架）二次开发
> 一键把「任意非结构化文档」解析成 **结构化 · 可行动 · 风险可见 · 证据可溯源** 的分析报告，并持久化到 SQLite。

---

## 项目简介

DocMind 是一个**通用文档智能解析 Agent**。你扔给它一份文档——读书摘录、白皮书、会议纪要、任务排期表、某类业务文档——它不满足于给你一段摘要，而是像一组分工明确的团队，产出三份互补的结构化产物：

- 这份文档**整体讲了什么**（概览）
- 里面**藏着哪些值得注意的观点、信息、矛盾、风险与局限**（洞察）
- 接下来**该做什么、谁来做、何时做**（行动项）

再由主调度 Agent 合并去重、交叉一致性校验，并交给**证据核验 Agent 逐条回溯原文**——每条关键结论都标注它是否在原文中站得住、依据是哪一句。最后把整条解析过程**结构化持久化**到 SQLite，会话沙箱销毁后依然可查。

---

## 核心特性

- **三子 Agent 并行分工**：`agent-overview`（概览）/ `agent-insight`（洞察）/ `agent-action`（行动项），职责隔离，各只产出自己的维度。
- **原文证据溯源（V2）**：第 4 个子 Agent `agent-evidence` 对每条关键结论判定四态——`verified`（原文明确支持）/ `contradicted`（原文明确相反）/ `unsupported`（原文找不到依据）/ `inferred`（基于原文的推断），并给出**逐字证据 + 定位 + 置信度**。它**只核验、不重做分析**。
- **不伪造定位信息**：文档转换不保留页码，因此 `page` 字段**恒为 NULL**；定位靠「章节 + 行号 + 原文逐字片段」。宁可缺定位，也绝不编造。
- **不伪造证据**：声称 `verified` 却拿不出证据的条目会被**自动降级为 `unsupported`**；`unsupported` / `inferred` 在报告里一律带 ⚠️ 提示，**绝不混同于 verified**。
- **不限定文档类型**：书籍、公开白皮书、介绍类读物、会议记录/纪要、任务安排表、各类业务文档（`doc_category` 开放取值）。
- **结构化持久化 Skill**：自定义 `db_persist_skill`，把「文档元信息 + 3 份子 Agent 原始输出 + Claim 清单 + 逐条证据核验 + 最终报告」写入 SQLite 五张表。
- **整体自评 + 逐条核验并存**：`hallucination_check_flag`（`pass/fail/unknown`，主调度 Agent 整体自评）与 `evidence_results`（逐条可追溯）口径互补、互不替代。
- **健壮性**：幂等重跑、超长自动截断（子输出 60k / 报告 100k / 摘要 500 / 单条证据 2k 字符）、写库失败或**证据核验失败都不中断主流程**、md 文件作为兜底备份。
- **产品化提醒**：输出任务 id / 状态 / 分类 / 跳转标识通知体，供前端接入消息弹窗。

---

## 系统架构

```
                    ┌───────────────────────────────────────────┐
                    │  主调度 Agent (TechDocAnalyzer)            │
                    │   ▪ 接收文档 → 生成 doc_id/doc_category    │
                    │   ▪ 并行分发 / 合并去重 / 一致性校核        │
                    │   ▪ Claim 抽取（C001…）                     │
                    │   ▪ 调 agent-evidence 逐条核验原文证据       │
                    │   ▪ 调 db_persist_skill 写库               │
                    └──┬─────────────┬─────────────┬────────────┘
                       │ task        │ task        │ task      （并行）
            ┌──────────▼────┐ ┌──────▼──────┐ ┌────▼─────────┐
            │ agent-overview│ │agent-insight│ │ agent-action │
            │ 主旨/目的/背景 │ │ 观点/信息/   │ │ 决议/任务/    │
            │ 结构/关键对象  │ │ 矛盾/风险/   │ │ 责任人/时间/  │
            │               │ │ 约束/局限    │ │ 建议/落地     │
            └───────────────┘ └─────────────┘ └──────────────┘
                       └──────────┬──────────┘
                                  │ 合并 → 去重 → 抽 Claim
            ┌─────────────────────▼─────────────────────┐
            │  agent-evidence（V2）· 逐条原文证据核验      │
            │  verified / contradicted / unsupported /   │
            │  inferred + 逐字证据 + 定位 + 置信度         │
            └─────────────────────┬─────────────────────┘
                                  │
            ┌─────────────────────▼─────────────────────┐
            │  final_analysis_report.md（含证据核验小节）  │
            │  + db_persist_skill → SQLite（5 张表）      │
            └───────────────────────────────────────────┘
```

数据落库到五张表（前三张为 V1 原有，**字段与语义未改**）：

| 表 | 内容 |
|----|------|
| `doc_source` | 文档源：`doc_id` / 名称 / `doc_category` / 上传时间 / 摘要 / 原文 hash |
| `agent_intermediate` | 3 个子 Agent 的原始输出（`agent_type` 限定 overview/insight/action） |
| `doc_analysis_result` | 最终报告 / 综合结论 / `hallucination_check_flag` |
| `document_claim` | **（V2）** 待核验结论：`(doc_id, claim_id)` / 来源 Agent / 结论 / 类型（fact/risk/action/summary/inference） |
| `evidence_verification` | **（V2）** 逐条核验：四态 / 证据原文（逐字）/ `section` · `source_offset` · `source_locator`（`page` 恒 NULL）/ `confidence` |

---

## 支持的文档类型

书籍 · 公开白皮书 · 介绍类读物 · 会议记录 / 会议纪要 · 任务安排表 · 各类业务文档

---

## 部署架构

**混合部署（Windows + Docker Desktop WSL2）**，避免在容器内编译前端：

| 组件 | 运行位置 |
|------|----------|
| Next.js 前端 | Windows 宿主机（`pnpm dev`） |
| Gateway + 沙箱 + Nginx | Docker Compose（预拉镜像） |

`docker/docker-compose.host-frontend.yaml` 与多文件 Compose 编排，一键启停。

---

## 目录结构（要点）

```
plans/techdoc-agent-general/            # 本项目交付物（设计即文档）
├── system_prompt/SOUL_main_coordinator.md   # 主调度 Agent 系统提示
├── subagents/                                # 4 个子 Agent（可提交版）
│   ├── README.md                             # 注册 YAML + 字段说明
│   ├── agent_overview.system_prompt.md
│   ├── agent_insight.system_prompt.md
│   ├── agent_action.system_prompt.md
│   └── agent_evidence.system_prompt.md       # (V2) 原文证据核验
├── evidence/                                 # (V2) 证据核验契约层（零依赖 + 测试）
│   ├── evidence_contract.py                  # 四态可执行规格 / 抗伪造规则 / 报告渲染
│   └── test_evidence_contract.py             # 28 个用例（含 Test1–Test4）
├── db_persist_skill/SKILL.md                 # 持久化技能（含 insert.py 模板）
├── workflow.md                               # 12 步编排流程
├── schema.sql                                # SQLite DDL（5 张表）
├── DEPLOYMENT_NOTES.md
└── RISK_POINTS.md
```

---

## 快速开始

> 完整部署与配置说明见 [`plans/techdoc-agent-general/DEPLOYMENT_NOTES.md`](plans/techdoc-agent-general/DEPLOYMENT_NOTES.md)。

```bash
# 1. 准备运行配置（本地私有，不入库）
cp config.example.yaml config.yaml
cp extensions_config.example.json extensions_config.json
# 在 config.yaml 配置模型密钥

# 2. 把四个子 Agent 注册到 config.yaml 的 subagents.custom_agents
#    见 plans/techdoc-agent-general/subagents/README.md（含可粘贴 YAML）

# 3. 部署主 Coordinator 的 SOUL.md 与自定义技能到「当前用户」目录
#    目标路径（{uid} 见下方说明）：
#      backend/.deer-flow/users/{uid}/agents/intelligent-technical-document-analyzer/SOUL.md
#      backend/.deer-flow/users/{uid}/skills/custom/db_persist_skill/SKILL.md
#    源文件（均在库中）：
#      plans/techdoc-agent-general/system_prompt/SOUL_main_coordinator.md
#      plans/techdoc-agent-general/db_persist_skill/SKILL.md
#
#    ⚠️ {uid} 是**每台环境 / 每个用户都不同**的 UUID，不是固定值——不要写死，也不要照抄别人的。
#       首次启动 Gateway 后，backend/.deer-flow/users/ 下会自动生成你的用户目录，目录名即 {uid}。
#       覆盖同步后需重启 Gateway 生效。具体同步方式见 DEPLOYMENT_NOTES.md 第 7.1 节。

# 4. 跑证据核验契约测试（零依赖，不需要 Gateway / 模型密钥 / Docker）
python -m unittest discover -s plans/techdoc-agent-general/evidence -t plans/techdoc-agent-general/evidence -v

# 5. 一键启停（混合部署）
docker compose ... up          # 网关 + 沙箱 + Nginx
pnpm dev                        # 宿主机 Next.js 前端
```

启动后通过 Nginx（`127.0.0.1:2026`）访问，上传或粘贴任意文档即会触发完整的 V2 解析流程：

1. **三个抽取 Agent 并行分工** —— `agent-overview`（概览）/ `agent-insight`（洞察）/ `agent-action`（行动项），三者无依赖、并行执行；
2. **主 Coordinator 合并去重并抽取关键结论 Claim** —— 编号 `C001…`，每条标注来源 Agent 与类型（`fact / risk / action / summary / inference`）；
3. **Evidence Agent（`agent-evidence`）逐条做原文证据核验** —— 对每条 Claim 判定 `verified` / `contradicted` / `unsupported` / `inferred` 四态之一，并给出逐字证据、定位与置信度。

最后由主 Coordinator 生成含「证据核验」小节的终稿，并调用 `db_persist_skill` 写入 SQLite。

---

## 技术栈

**DeerFlow (LangGraph)** · Python · FastAPI · Next.js · Docker Compose · SQLite · YAML 配置驱动（自定义子 Agent + Skill 编排）

---

## 许可与致谢

本项目基于 **[bytedance/deer-flow](https://github.com/bytedance/deer-flow)** 二次开发，遵循其 **[MIT License](LICENSE)**（版权归 Bytedance / DeerFlow Authors）。本项目为个人学习与实践作品，在其多智能体框架之上实现了「通用文档智能解析 + 结构化持久化」的定制化能力。
