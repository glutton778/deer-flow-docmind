---
name: db_persist_skill
description: 技术文档解析结果结构化持久化。仅当主调度Agent已完成「三个子Agent(tech-core/risk/todo)并行解析 → 合并去重 → 事实一致性/幻觉校验 → 生成最终报告」这一整套流程后，调用本技能：把「文档元信息 + 3份子Agent原始输出 + 最终报告」统一写入SQLite数据库。由主调度Agent且仅由主调度Agent调用；任何子Agent禁止独立写库。输入：doc元信息 + 各中间/终稿markdown文件路径(或内容)；输出：storage_status(success/failed) + doc_id，失败返回错误信息。数据库连接/写入失败时不得中断主业务流程，沙箱md文件作为兜底备份继续保留。
---

# 文档解析结果持久化 Skill (db_persist_skill)

## 目标
把一次完整的技术文档解析结果，以结构化方式写入 SQLite，使数据在会话/沙箱销毁后仍可查询：文档源 (`doc_source`) + 3 份子Agent中间输出 (`agent_intermediate`) + 最终结果 (`doc_analysis_result`)。

## 铁律（违反即失败）
1. **只能由主调度Agent调用**。子Agent不得写库。
2. **只在三个子Agent全部成功**且主Agent已完成合并、去重、事实一致性/幻觉校验之后调用。任何子Agent失败 → 直接返回任务异常，**不要**调用本技能。
3. **写库失败不打断主流程**：md 文件照常生成作为兜底；返回 `storage_status:"failed"` 由主Agent转告用户。
4. **超长文本必须截断**后再入库，禁止把超大文本塞进化单字段（见「截断规则」）。

## 入参（主Agent在调用前必须备齐）
- `doc_id`：文档唯一标识。主Agent生成，推荐 `f"{doc_name}-{YYYYmmddHHMMSS}"` 或 uuid4 的 hex。全流程复用同一个 doc_id。
- `doc_name`：文件名或文档标题。
- `doc_type`：`pdf` / `text` / `docx` / `md` / `other`。
- `upload_time`：ISO 时间，如 `2026-09-06T12:00:00`。
- `doc_snippet`：一句话摘要（可选；缺省则取 raw 前 500 字符）。
- `summary_conclusion`：最终报告「综合结论与建议」一节。
- `hallucination_check_flag`：主Agent事实校验结果，取 `pass` / `fail` / `unknown`。
- 三个子Agent的原始输出内容(`core` / `risk` / `todo`) 与最终报告 `report`（可直接给内容，或给已写好的 md 文件路径——本技能统一落盘后再读取，确保不经过超长命令行参数）。
- `db_path`（可选）：覆盖默认数据库位置；缺省 `/mnt/user-data/outputs/tech_analysis.db`。

## 工作流（主Agent按序执行）
### 第 0 步：初始化
用 `bash` 确保临时目录与输出目录存在：
```bash
mkdir -p /tmp/db_persist /mnt/user-data/outputs
```
用 `python3 --version` 探测；若 python3 不可用，回退用 `sqlite3` CLI（见「回退」）。

### 第 1 步：把内容落盘（不要走命令行参数，超大文本会撑爆参数）
用 `write_file` 写：
- `/tmp/db_persist/raw.txt` ← 原始文档文本 `raw_doc_text`
- `/tmp/db_persist/core.md` ← tech-core-agent 原始输出
- `/tmp/db_persist/risk.md` ← tech-risk-agent 原始输出
- `/tmp/db_persist/todo.md` ← tech-todo-agent 原始输出
- `/tmp/db_persist/report.md` ← 最终报告

### 第 2 步：写元信息 JSON
用 `write_file` 写 `/tmp/db_persist/meta.json`（**必须是合法 JSON**，含 `summary_conclusion` 里的换行时用 `\n` 转义）：
```json
{
  "doc_id": "<doc_id>",
  "doc_name": "<doc_name>",
  "doc_type": "<doc_type>",
  "upload_time": "<upload_time>",
  "doc_snippet": "<一句话摘要，可空>",
  "summary_conclusion": "<综合结论与建议>",
  "hallucination_check_flag": "pass | fail | unknown"
}
```

### 第 3 步：写并执行入库脚本
用 `write_file` 写 `/tmp/db_persist/insert.py`（内容见下方「insert.py 模板」），然后：
```bash
python3 /tmp/db_persist/insert.py
```
脚本会**自动截断**、在建库建表、写入三张表，并把 `raw.txt` 的 sha256 作为 `raw_text_hash`，最后向 stdout 打印一行 JSON：
- 成功：`{"storage_status":"success","doc_id":"<id>","message":"持久化成功","warnings":[...]}`
- 失败：`{"storage_status":"failed","doc_id":"<id>","message":"<错误信息>"}`

用 `bash` 捕获这行 JSON 作为本技能的**出参**。

### 第 4 步：返回出参
- 成功：`storage_status = success`，`doc_id` 已入库。同时打印（若 md 结论截断过）`warnings` 提醒主Agent。
- 失败：`storage_status = failed` + `message`，**md 文件仍在**，主Agent据此向用户提示「存储失败，可导出本地 md 作备份」。

## 出参
| 字段 | 说明 |
|------|------|
| `storage_status` | `success` / `failed` |
| `doc_id` | 本次写入的文档ID |
| `message` | 成功/失败描述 |
| `warnings` | 可选，截断或某子Agent输出为空的提示 |

## 截断规则（第 3 步由脚本自动执行）
- 子Agent中间输出：上限 **60,000 字符**。
- 最终报告：上限 **100,000 字符**。
- 摘要/结论：上限 **500 字符**。
- 超长裁剪后追加一行 `<<TRUNCATED: N chars removed by db_persist_skill>>`，放入 `warnings`。

## 回退策略
- **python3 不可用**：改用 `sqlite3` CLI 逐条执行 `INSERT`（字段量大时逐条拼接，SQL 需要 `Q` 转义）。
- **写库失败**：不 `raise`、不中断；返回 `failed` + 错误信息。主Agent继续提示用户可导 md。
- **DB 目录不可写**：尝试回退到 `/tmp/tech_analysis.db`（临时，会话内有效），并在 `warnings` 标明「未持久化到主机，仅临时保存」。

## 出参示例
```
✅ 技术文档解析任务执行完成
文档ID：ecosystem-blueprint-20260906T120000
存储状态：success
可查阅：3份子Agent中间输出 + 最终报告（SQLite doc_id=ecosystem-blueprint-20260906T120000）
```

---

### insert.py 模板
```python
#!/usr/bin/env python3
# db_persist — 技术文档解析结构化持久化（DeerFlow 沙箱内运行）
import json, sqlite3, hashlib, os, re, datetime
from pathlib import Path

BASE = Path("/tmp/db_persist")
DB_PATH = os.environ.get("DEERFLOW_DB_PATH", "/mnt/user-data/outputs/tech_analysis.db")
SUBAGENT_LIMIT = 60_000
REPORT_LIMIT = 100_000
SHORT_LIMIT = 500

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS doc_source (
  doc_id          TEXT PRIMARY KEY,
  doc_name        TEXT NOT NULL,
  doc_type        TEXT NOT NULL,
  upload_time     TEXT NOT NULL,
  doc_snippet     TEXT,
  raw_text_hash   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_intermediate (
  inter_id             INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id               TEXT NOT NULL,
  agent_type           TEXT NOT NULL CHECK (agent_type IN ('core','risk','todo')),
  agent_output_content TEXT NOT NULL,
  generate_time        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  FOREIGN KEY (doc_id) REFERENCES doc_source(doc_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS doc_analysis_result (
  result_id              INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id                 TEXT NOT NULL,
  final_report_content   TEXT NOT NULL,
  summary_conclusion     TEXT,
  hallucination_check_flag TEXT NOT NULL DEFAULT 'unknown'
    CHECK (hallucination_check_flag IN ('pass','fail','unknown')),
  create_time            TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  FOREIGN KEY (doc_id) REFERENCES doc_source(doc_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agent_intermediate_doc ON agent_intermediate(doc_id);
CREATE INDEX IF NOT EXISTS idx_doc_analysis_result_doc ON doc_analysis_result(doc_id);
"""

def truncate(s, limit):
    if not s: return ""
    s = str(s)
    return s if len(s) <= limit else s[:limit] + f"\n\n<<TRUNCATED: {len(s)-limit} chars removed by db_persist_skill>>"

def read(name):
    p = BASE / name
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""

def main():
    meta = json.loads((BASE / "meta.json").read_text(encoding="utf-8"))
    raw = read("raw.txt"); core = read("core.md"); risk = read("risk.md")
    todo = read("todo.md"); report = read("report.md")
    doc_id = meta["doc_id"]
    raw_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    snippet = (meta.get("doc_snippet") or raw[:SHORT_LIMIT]).strip()[:SHORT_LIMIT]
    warnings = []
    for atype, content in (("core", core), ("risk", risk), ("todo", todo)):
        if not content.strip():
            warnings.append(atype + " output is empty")

    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    try:
        con.executescript(SCHEMA)
        cur = con.cursor()
        # 幂等：删除该 doc_id 旧中间/结果，doc_source 用 upsert
        cur.execute("DELETE FROM agent_intermediate WHERE doc_id=?", (doc_id,))
        cur.execute("DELETE FROM doc_analysis_result WHERE doc_id=?", (doc_id,))
        cur.execute("""INSERT INTO doc_source(doc_id,doc_name,doc_type,upload_time,doc_snippet,raw_text_hash)
                       VALUES(?,?,?,?,?,?)                 
                       ON CONFLICT(doc_id) DO UPDATE SET
                         doc_name=excluded.doc_name, doc_type=excluded.doc_type,
                         upload_time=excluded.upload_time, doc_snippet=excluded.doc_snippet,
                         raw_text_hash=excluded.raw_text_hash""",
                    (doc_id, meta.get("doc_name",""), meta.get("doc_type","text"),
                     meta.get("upload_time") or datetime.datetime.now().isoformat(timespec="seconds"),
                     truncate(snippet, SHORT_LIMIT), raw_hash))
        for atype, content in (("core", core), ("risk", risk), ("todo", todo)):
            if content.strip():
                cur.execute("INSERT INTO agent_intermediate(doc_id,agent_type,agent_output_content) VALUES(?,?,?)",
                            (doc_id, atype, truncate(content, SUBAGENT_LIMIT)))
        cur.execute("""INSERT INTO doc_analysis_result(doc_id,final_report_content,summary_conclusion,hallucination_check_flag)
                       VALUES(?,?,?,?)""",
                    (doc_id, truncate(report, REPORT_LIMIT),
                     truncate(meta.get("summary_conclusion",""), SHORT_LIMIT),
                     meta.get("hallucination_check_flag","unknown")))
        con.commit()
        print(json.dumps({"storage_status":"success","doc_id":doc_id,"message":"持久化成功","warnings":warnings}, ensure_ascii=False))
    except Exception as e:
        con.rollback()
        print(json.dumps({"storage_status":"failed","doc_id":doc_id,"message":"持久化失败: %s" % e}, ensure_ascii=False))
    finally:
        con.close()

if __name__ == "__main__":
    main()
```
