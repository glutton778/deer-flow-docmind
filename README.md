# DocMind — 多智能体通用文档智能解析 Agent

> **TechDocAnalyzer** · 基于 **DeerFlow**（LangGraph 多智能体框架）二次开发
> 一键把「任意非结构化文档」解析成 **结构化 · 可行动 · 风险可见** 的分析报告，并持久化到 SQLite。

---

## 项目简介

DocMind 是一个**通用文档智能解析 Agent**。你扔给它一份文档——读书摘录、白皮书、会议纪要、任务排期表、某类业务文档——它不满足于给你一段摘要，而是像一组分工明确的团队，产出三份互补的结构化产物：

- 这份文档**整体讲了什么**（概览）
- 里面**藏着哪些值得注意的观点、信息、矛盾、风险与局限**（洞察）
- 接下来**该做什么、谁来做、何时做**（行动项）

再由主调度 Agent 合并去重、交叉一致性校验、**逐条回溯原文做事实/幻觉校验**，生成完整报告，最后把整条解析过程**结构化持久化**到 SQLite，会话沙箱销毁后依然可查。

---

## 核心特性

- **三子 Agent 并行分工**：`agent-overview`（概览）/ `agent-insight`（洞察）/ `agent-action`（行动项），职责隔离，各只产出自己的维度。
- **不限定文档类型**：书籍、公开白皮书、介绍类读物、会议记录/纪要、任务安排表、各类业务文档（`doc_category` 开放取值）。
- **结构化持久化 Skill**：自定义 `db_persist_skill`，把「文档元信息 + 3 份子 Agent 原始输出 + 最终报告」写入 SQLite 三张表。
- **事实一致性 / 幻觉校验**：每条结论必须能回溯 `raw_doc_text`，做不到就标注，绝不编造；结果以 `hallucination_check_flag`（`pass/fail/unknown`）落库。
- **健壮性**：幂等重跑、超长自动截断（子输出 60k / 报告 100k / 摘要 500 字符）、写库失败不中断主流程、md 文件作为兜底备份。
- **产品化提醒**：输出任务 id / 状态 / 分类 / 跳转标识通知体，供前端接入消息弹窗。

---

## 系统架构

```
                    ┌───────────────────────────────────────┐
                    │  主调度 Agent (TechDocAnalyzer)        │
                    │   ▪ 接收文档 → 生成 doc_id/doc_category  │
                    │   ▪ 并行分发 / 合并去重 / 一致性校核      │
                    │   ▪ 事实/幻觉校验                        │
                    │   ▪ 调 db_persist_skill 写库             │
                    └───────┬─────────────┬─────────────┬────┘
                            │ task        │ task        │ task
                 ┌──────────▼────┐ ┌──────▼──────┐ ┌────▼─────────┐
                 │ agent-overview│ │agent-insight│ │ agent-action │
                 │ 主旨/目的/背景 │ │ 观点/信息/   │ │ 决议/任务/    │
                 │ 结构/关键对象  │ │ 矛盾/风险/   │ │ 责任人/时间/  │
                 │               │ │ 约束/局限    │ │ 建议/落地     │
                 └───────────────┘ └─────────────┘ └──────────────┘
                            └──────────┬──────────┘
                                       │ 合并 → 校验 → 终稿
                    ┌──────────────────▼──────────────────┐
                    │  final_analysis_report.md            │
                    │  + db_persist_skill → SQLite         │
                    └─────────────────────────────────────┘
```

数据落库到三张表：

| 表 | 内容 |
|----|------|
| `doc_source` | 文档源：`doc_id` / 名称 / `doc_category` / 上传时间 / 摘要 / 原文 hash |
| `agent_intermediate` | 3 个子 Agent 的原始输出（`agent_type` 限定 overview/insight/action） |
| `doc_analysis_result` | 最终报告 / 综合结论 / `hallucination_check_flag` |

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
├── subagents/                                # 3 个子 Agent（可提交版）
│   ├── README.md                             # 注册 YAML + 字段说明
│   ├── agent_overview.system_prompt.md
│   ├── agent_insight.system_prompt.md
│   └── agent_action.system_prompt.md
├── db_persist_skill/SKILL.md                 # 持久化技能（含 insert.py 模板）
├── workflow.md                               # 9 步编排流程
├── schema.sql                                # SQLite DDL
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

# 2. 把三个子 Agent 注册到 config.yaml 的 subagents.custom_agents
#    见 plans/techdoc-agent-general/subagents/README.md（含可粘贴 YAML）

# 3. 注册 db_persist_skill 为自定义技能
#    脚本内容见 plans/techdoc-agent-general/db_persist_skill/SKILL.md

# 4. 一键启停（混合部署）
docker compose ... up          # 网关 + 沙箱 + Nginx
pnpm dev                        # 宿主机 Next.js 前端
```

启动后通过 Nginx（`127.0.0.1:2026`）访问，上传或粘贴任意文档即可触发三子 Agent 并行解析。

---

## 技术栈

**DeerFlow (LangGraph)** · Python · FastAPI · Next.js · Docker Compose · SQLite · YAML 配置驱动（自定义子 Agent + Skill 编排）

---

## 许可与致谢

本项目基于 **[bytedance/deer-flow](https://github.com/bytedance/deer-flow)** 二次开发，遵循其 **[MIT License](LICENSE)**（版权归 Bytedance / DeerFlow Authors）。本项目为个人学习与实践作品，在其多智能体框架之上实现了「通用文档智能解析 + 结构化持久化」的定制化能力。
