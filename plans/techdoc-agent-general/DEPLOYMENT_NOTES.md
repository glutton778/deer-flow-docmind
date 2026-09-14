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
- **大小控制**：`SUBAGENT_LIMIT=60000` / `REPORT_LIMIT=100000` / `SHORT_LIMIT=500`，防止单字段过大；超长自动截断并加标记。
- **进程内写库权限**：沙箱 `--cap-drop=ALL`，但写 host 挂载目录主要受**目录权限**约束（非能力约束），归到第 2 节挂载说明里处理。
- **不引进新服务**：全程仅用 SQLite 文件 + 沙箱内 python3；没有额外容器、消息队列或后端服务。
