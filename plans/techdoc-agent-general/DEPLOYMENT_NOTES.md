# Docker 部署注意事项 · SQLite 挂载 · 容器重建数据保护 · 故障兜底

> 面向**本地原型**（16GB Windows + Docker Desktop WSL2，DeerFlow 容器化部署）。目标：轻量、不引入额外中间件/独立服务，纯靠 DeerFlow 现有能力（沙箱 bash / write_file / read_file + SQLite stdlib）完成持久化。

---

## 1. 当前部署拓扑（已确认）

```
Nginx(:2026) ──▶ Frontend(:3000, 宿主)
             ──▶ DeerFlow Gateway(:8001, 容器内 deer-flow-gateway)
                       └─ DooD 挂载 docker.sock，按线程拉起 AIO 沙箱容器
AIO Sandbox: use=deerflow.community.aio_sandbox:AioSandboxProvider
            image=...all-in-one-sandbox:1.11.0   port=8080   replicas=1(16GB主机)
```
- 沙箱是**每线程一个 Docker 容器**，线程空闲即销毁（`--rm`），镜像约 13GB，内存吃紧。
- 当前 `config.yaml` 的 `sandbox:` 块**未配置任何 host 挂载**（无 `mounts`）。
- 上传/输出落在 `/mnt/user-data/`，它对应宿主 `backend/.deer-flow/users/{user_id}/threads/{thread_id}/user-data/`——**是 host 持久目录**（现有报告 md 跨会话可查，已证明可写可持久）。

---

## 2. SQLite 文件放哪（关键决策）

### 主方案（推荐，零配置，立即可用）
**把库写到 `/mnt/user-data/outputs/doc_analysis.db`。**
- 经沙箱 `/mnt/user-data/outputs/` 落宿主 `..../user-data/outputs/`，**沙箱销毁后仍持久**（与报告 md 同机制）。
- 优点：无需改配置、无需重启 gateway，`db_persist_skill` 用默认 `DB_PATH` 即可。
- 局限：**每线程一份库**（`doc_source` 等是该线程的文档集合）。适合「单文档/单线程查询」的原型。

### 副方案（可选，做平台级/跨文档聚合）
在 `config.yaml -> sandbox:` 增加一个 host 挂载，让沙箱把库写到共享目录：
```yaml
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider
  image: enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.11.0
  port: 8080
  replicas: 1
  mounts:
    - host_path: /g:/Mycode/deer-flow/data/doc_analysis_db   # 宿主绝对路径（Windows/WSL 注意）
      container_path: /data
      read_only: false
```
- 然后给沙箱设环境变量 `DEERFLOW_DB_PATH=/data/doc_analysis.db`（由 `db_persist_skill` 或环境注入读取）。
- **注意**：
  - `host_path` 在 Docker Desktop(WSL2) 下要用**宿主侧绝对路径**；Linux 约定 `/g:/...`（Docker Desktop 自动映射）或 Windows 盘符路径，不同环境会不同，需实测。
  - 存放库的宿主目录必须先 `mkdir -p`，并保证沙箱 UID(1000) 有写权限，否则写入报 `EPERM`。
  - 改动后需 `docker restart deer-flow-gateway` 生效。
  - 共享库会引入**并发写锁**风险（多线程同时写）——对本地原型可接受，但要知晓。

---

## 3. 容器重建数据保护

**核心事实**：`/mnt/user-data/` 走的是 `backend/.deer-flow/users/{uid}/threads/{tid}/user-data/` 的 host 目录（bind），所以：
- **`docker restart deer-flow-gateway` / `docker compose ... up` / `down` → 不丢数据**。库和 md 都在宿主编译目录里，容器重启不清。
- **线程/沙箱销毁（`--rm`）→ 也不丢**。因为写的是 host 持久目录，不是容器层。
- **唯一会丢**：手动 `rm -rf` 掉 `backend/.deer-flow/users/.../threads/.../user-data/`，或清理 `user-data/`。

**保护建议（按需）**：
1. **备份**：定期把 `backend/.deer-flow/users/{uid}/threads/*/user-data/outputs/doc_analysis.db`（和报告 md）`cp` 到独立备份目录。
2. **线程外独立库（最稳）**：用第 2 节副方案把库写到一个不随线程/线程目录走动的固定 host 目录（如 `/g:/Mycode/deer-flow/data/doc_analysis_db`），这样连线程目录被清都不受影响，且天然跨线程聚合。
3. **若用 named volume**（纯容器内）：库只存于 volume，`docker volume prune` 会删——**别用**，避免踩坑。

---

## 4. 依赖与工具可用性（需实测，方案已带回退）
- 入库脚本用 **python3 标准库 `sqlite3`**（AIO 编程沙箱大概率有 python3）。
- 每次运行前用 `bash` 探测：`python3 --version`；不可用则回退 `sqlite3` CLI。
- 若两者都没有 → `db_persist_skill` 返回 `failed`，主调度Agent仍保底 md，并向用户提示「可导出本地 md」。

---

## 5. 故障兜底方案（按优先级）
| 层 | 兜底 | 说明 |
|----|------|------|
| **最终兜底** | 3 份子输出 + 终稿的 md 文件 | **永远生成、永远保留**在 `/mnt/user-data/outputs/`；即使 DB 全挂，用户仍可导出本地 md。这是「不可丢」的底线。 |
| **存储失败** | `storage_status=failed` 提示 | DB 写入失败不抛错、不中断主流程；主调度Agent明确告知「存储失败，可导出 md」（含对话内提示行）。 |
| **临时保存** | 回退写 `/tmp/doc_analysis.db` | 若宿主目录不可写，先写临时目录并标 `warnings`；保证本轮仍有结构化数据可查。 |
| **库损坏/迁移** | 重新入库 | doc_id 幂等（doc_source upsert + 中间/结果先删后插），重跑即可重建，不需删表。 |

---

## 6. 其余建议
- **DB 备份**：库随线程持久目录；如需集中备份用副方案挂载到统一目录后 `cp`。
- **大小控制**：`SUBAGENT_LIMIT=60000` / `REPORT_LIMIT=100000` / `SHORT_LIMIT=500` / `EVIDENCE_LIMIT=2000`，防止单字段过大；超长自动截断并加标记。
- **进程内写库权限**：沙箱 `--cap-drop=ALL`，但写 host 挂载目录主要受**目录权限**约束（非能力约束），归到第 2 节挂载说明里处理。
- **不引进新服务**：全程仅用 SQLite 文件 + 沙箱内 python3；没有额外容器、消息队列或后端服务。

---

## 7. V2 增量（Evidence Agent + 原文证据溯源）

> 本节的改动**不涉及 Docker**：不加容器、不改镜像、不改端口、不改挂载。仍是「宿主机 Next.js + 容器 gateway/sandbox」的混合部署。

### 7.1 需要动的只有两处配置（都不入库）
| 位置 | 动作 |
|------|------|
| 根 `config.yaml` → `subagents.custom_agents` | 追加 `agent-evidence`（YAML 见 `subagents/README.md`） |
| `.deer-flow/users/{uid}/skills/custom/db_persist_skill/SKILL.md` | 用 `db_persist_skill/SKILL.md` 覆盖（新增第 4、5 张表与抗伪造校验） |

同样，`.deer-flow/users/{uid}/agents/intelligent-technical-document-analyzer/SOUL.md` 需用 `system_prompt/SOUL_main_coordinator.md` 覆盖。
改完**重启 Gateway** 生效（`docker restart deer-flow-gateway`）。

### 7.2 数据库：仍是同一个 SQLite 文件（5 张表）
- 库位置不变：`/mnt/user-data/outputs/doc_analysis.db`。
- **新增表由 `insert.py` 用 `CREATE TABLE IF NOT EXISTS` 自动补建**——已存在的旧库**无需删库、无需迁移**，下次入库时自动建出新表。
- 三个旧表（`doc_source` / `agent_intermediate` / `doc_analysis_result`）的字段与语义**完全未改**，`hallucination_check_flag` 的 `pass|fail|unknown` 语义也**未改**。

### 7.3 页码：为什么 `page` 恒为 NULL
`utils/file_conversion.py::convert_file_to_markdown()` 把 PDF/Office 转成**一份扁平 Markdown**（`pymupdf4llm` 优先，稀疏则退 `MarkItDown`），**不保留页码**；系统里唯一的定位体系是
`utils/file_outline.py::extract_outline()` 返回的 `{title, line}`（**1-based 行号**，上限 50 条）。
所以任何"页码"都只可能是模型编造的 → 契约层与 `insert.py` 都会**强制把 `page` 置为 NULL**。
真实可用的定位是：`section`（章节/条款号）+ `source_offset`（行号区间）+ `source_locator`（原文逐字片段）。

> **前置条件**：`uploads.auto_convert_documents` **默认为 off**（见 `backend/docs/FILE_UPLOAD.md`），
> 要让上传的 PDF/Word 自动转成可读 Markdown（`/mnt/user-data/uploads/<name>.md`），需在 `config.yaml` 的 `uploads:` 下开启。

### 7.4 如何跑测试（零第三方依赖，离线）
```bash
# 契约层 + 四个状态用例（28 个用例）
python -m unittest discover -s plans/techdoc-agent-general/evidence -t plans/techdoc-agent-general/evidence -v

# backend 侧薄壳（证明契约可从框架测试运行器触达）
cd backend && PYTHONPATH=. uv run pytest tests/test_techdoc_evidence_contract.py -q
```
两个套件都**不需要** Gateway、模型密钥或 Docker，纯 stdlib。

### 7.5 手工验证 Evidence Agent（在线，需 Gateway 运行）
1. 起服务：`docker compose ... up` + 宿主机 `pnpm dev`，浏览器开 `127.0.0.1:2026`。
2. 上传/粘贴一份**含明确论断**的短文档（建议直接用 7.6 的例子，便于对照）。
3. 期望在最终报告里看到每条核心结论下多出「**证据核验**」小节：`verified` 带证据原文+来源+置信度，`unsupported`/`inferred` 带 ⚠️ 提示。
4. 查库确认 5 张表都有数据（在沙箱里跑，或把 `doc_analysis.db` 拷出来用本机 sqlite 工具看）：
```sql
SELECT claim_id, claim_type, claim_text FROM document_claim ORDER BY claim_id;
SELECT claim_id, verification_status, page, section, source_locator, confidence
  FROM evidence_verification ORDER BY claim_id;
```
5. **重点看**：`page` 列必须**全为 NULL**（若出现数字，说明有环节绕过了强制规则，属 bug）。

### 7.6 已知限制（诚实声明）
- Evidence Agent 是 **LLM 判定**，离线无法验证其语义准确度；离线测试固定的是**契约语义**（给定该输出 → 必判该状态、必那样渲染），不是「LLM 面对真实文档一定能判对」。
- 无页码 → 定位精度止于「章节 + 行号 + 逐字片段」；行号来自 outline（上限 50 条），长文档中后段可能定位不到。
- 证据核验的输入 `raw_doc_text` 会被截断 → 落在截断部分的内容可能被误判为 `unsupported`（提示词已要求"不确定就判 unsupported 不要猜"，但仍会有偏差）。
- `evidence_contract.py` 与 `insert.py` 的抗伪造校验是**两处副本**（技能树只读挂载，沙箱脚本无法 import 该模块），必须手工同步；权威规格与测试在 `evidence_contract.py`。
