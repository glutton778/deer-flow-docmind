---
name: db_persist_skill
description: 通用文档解析结果结构化持久化。仅当主调度Agent已完成「三个子Agent(agent-overview/insight/action)并行解析 → 合并去重 → Claim抽取 → agent-evidence 逐条原文证据核验 → 生成最终报告」这一整套流程后，调用本技能：把「文档元信息 + 3份子Agent原始输出 + Claim清单 + 逐条证据核验结果 + 最终报告」统一写入SQLite数据库（5张表）。由主调度Agent且仅由主调度Agent调用；任何子Agent禁止独立写库。输入：doc元信息 + 各中间/终稿markdown文件路径(或内容) + claims/evidence的JSON；输出：storage_status(success/failed) + doc_id + 核验计数，失败返回错误描述。数据库连接/写入失败时不得中断主业务流程，沙箱md文件作为兜底备份继续保留。
---

# 文档解析结果持久化 Skill (db_persist_skill)

## 目标
把一次完整的通用文档解析结果，以结构化方式写入 SQLite，使数据在会话/沙箱销毁后仍可查询：
文档源 (`doc_source`) + 3 份子Agent中间输出 (`agent_intermediate`) + **Claim 清单 (`document_claim`)** + **逐条证据核验 (`evidence_verification`)** + 最终结果 (`doc_analysis_result`)，共 5 张表。

## 铁律（违反即失败）
1. **只能由主调度Agent调用**。子Agent（overview / insight / action / evidence）不得写库。
2. **只在三个抽取子Agent全部成功**、且主Agent已完成合并、去重、Claim抽取、证据核验之后调用。任一**抽取**子Agent失败 → 直接返回任务异常，**不要**调用本技能。
   （注意：`agent-evidence` 失败**不算**失败——此时照常持久化，只是 `evidence_results` 为空。）
3. **写库失败不打断主流程**：md 文件照常生成作为兜底；返回 `storage_status:"failed"` 由主Agent转告用户。
4. **超长文本必须截断**后再入库，禁止把超大文本塞进化单字段（见「截断规则」）。
5. **绝不伪造定位信息**：`page` 字段**恒为 NULL**（当前文档转换不保留页码）；脚本会**强制忽略**传入的任何页码。
6. **绝不把无证据的结论写成已验证**：声称 `verified`/`contradicted` 却给不出证据原文或原文片段的，
   脚本**自动降级为 `unsupported`** 并记入 `warnings`。

## 入参（主Agent在调用前必须备齐）
- `doc_id`：文档唯一标识。主Agent生成，推荐 `f"{doc_name}-{YYYYmmddHHMMSS}"` 或 uuid4 的 hex。全流程复用同一个 doc_id。
- `doc_name`：文件名或文档标题。
- `doc_category`：文档分类，取 `书籍` / `白皮书` / `会议记录` / `任务表` / `介绍` / `其他`（对应输入类型：书籍节选、公开白皮书、介绍类读物、会议记录/纪要、任务安排表、各类业务文档）。
- `upload_time`：ISO 时间，如 `2026-09-06T12:00:00`。
- `doc_snippet`：一句话摘要（可选；缺省则取 raw 前 500 字符）。
- `summary_conclusion`：最终报告「综合结论与建议」一节。
- `hallucination_check_flag`：主Agent整体自评，取 `pass` / `fail` / `unknown`（**V2 未改变其语义**）。
- 三个抽取子Agent的原始输出内容(`overview` / `insight` / `action`) 与最终报告 `report`（可直接给内容，或给已写好的 md 文件路径——本技能统一落盘后再读取，确保不经过超长命令行参数）。
- **（V2 新增）`claims` 与 `evidence_results`**：合并后的 Claim 清单与逐条核验结果。写成
  `/tmp/db_persist/claims_evidence.json`，结构见下（缺失时不算失败，只记 warning 并跳过这两张表）。
- `db_path`（可选）：覆盖默认数据库位置；缺省 `/mnt/user-data/outputs/doc_analysis.db`。

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
- `/tmp/db_persist/intermediate_overview.md` ← agent-overview 原始输出
- `/tmp/db_persist/intermediate_insight.md` ← agent-insight 原始输出
- `/tmp/db_persist/intermediate_action.md` ← agent-action 原始输出
- `/tmp/db_persist/final_analysis_report.md` ← 最终报告

### 第 2 步：写元信息 JSON
用 `write_file` 写 `/tmp/db_persist/meta.json`（**必须是合法 JSON**，含 `summary_conclusion` 里的换行时用 `\n` 转义）：
```json
{
  "doc_id": "<doc_id>",
  "doc_name": "<doc_name>",
  "doc_category": "<书籍|白皮书|会议记录|任务表|介绍|其他>",
  "upload_time": "<upload_time>",
  "doc_snippet": "<一句话摘要，可空>",
  "summary_conclusion": "<综合结论与建议>",
  "hallucination_check_flag": "pass | fail | unknown"
}
```

### 第 2.5 步（V2 新增）：写 Claim 与证据核验 JSON
用 `write_file` 写 `/tmp/db_persist/claims_evidence.json`（**必须是合法 JSON**）：
```json
{
  "claims": [
    {"claim_id": "C001", "source_agent": "overview", "claim_text": "<结论原文>", "claim_type": "fact"}
  ],
  "evidence_results": [
    {
      "claim_id": "C001",
      "source_agent": "overview",
      "claim": "<结论原文>",
      "verification_status": "verified",
      "evidence_text": "<逐字摘抄的证据原文，拿不到就 null>",
      "source_location": {"page": null, "section": "第二条", "source_offset": "12-14", "source_locator": "<逐字片段，拿不到就 null>"},
      "confidence": 0.96
    }
  ]
}
```
约束（脚本会再校验一遍，不合法就跳过该条并记入 `warnings`）：
- `source_agent` ∈ `overview` / `insight` / `action` / `coordinator`
- `claim_type` ∈ `fact` / `risk` / `action` / `summary` / `inference`
- `verification_status` ∈ `verified` / `contradicted` / `unsupported` / `inferred`
- `page` 必须为 `null`（填了也会被忽略并记 warning）
- `evidence_results` 里的 `claim_id` 必须能在 `claims` 里找到，否则该条被丢弃（避免孤儿数据）
- `agent-evidence` 整体失败时：`evidence_results` 写 `[]`，**不要**为了凑数据而编造

### 第 3 步：写并执行入库脚本
用 `write_file` 写 `/tmp/db_persist/insert.py`（内容见下方「insert.py 模板」），然后：
```bash
python3 /tmp/db_persist/insert.py
```
脚本会**自动截断**、建库建表（5 张表）、幂等写入，并把 `raw.txt` 的 sha256 作为 `raw_text_hash`，最后向 stdout 打印一行 JSON。

用 `bash` 捕获这行 JSON 作为本技能的**出参**。

### 第 4 步：返回出参
- 成功：`storage_status = success`，`doc_id` 已入库。同时打印（若字段被截断过 / 有条目被降级跳过）`warnings` 提醒主Agent。
- 失败：`storage_status = failed` + `message`，**md 文件仍在**，主Agent据此向用户提示「存储失败，可导出本地 md 作备份」。

## 出参
| 字段 | 说明 |
|------|------|
| `storage_status` | `success` / `failed` |
| `doc_id` | 本次写入的文档ID |
| `message` | 成功/失败描述 |
| `claim_count` | （V2）实际入库的 Claim 条数 |
| `evidence_count` | （V2）实际入库的核验结果条数 |
| `evidence_summary` | （V2）`{"verified":n,"contradicted":n,"unsupported":n,"inferred":n}`，供主Agent输出提醒 |
| `warnings` | 可选，截断 / 降级 / 非法条目跳过的提示 |

## 截断规则（第 3 步由脚本自动执行）
- 子Agent中间输出：上限 **60,000 字符**。
- 最终报告：上限 **100,000 字符**。
- 摘要/结论：上限 **500 字符**。
- Claim 文本：上限 **500 字符**。
- 单条证据原文 `evidence_text`：上限 **2,000 字符**。
- 超长裁剪后追加一行 `<<TRUNCATED: N chars removed by db_persist_skill>>`，放入 `warnings`。

## 回退策略
- **python3 不可用**：改用 `sqlite3` CLI 逐条执行 `INSERT`（字段量大时逐条拼接，SQL 需要 `Q` 转义）。
- **写库失败**：不 `raise`、不中断；返回 `failed` + 错误描述。主Agent继续提示用户可导 md。
- **DB 目录不可写**：尝试回退到 `/tmp/doc_analysis.db`（临时，会话内有效），并在 `warnings` 标明「未持久化到主机，仅临时保存」。
- **`claims_evidence.json` 缺失或非法**：不算失败；前 3 张表照常写入，第 4、5 张表跳过，`warnings` 记明。

## 出参示例（对话内格式化提醒）
```
✅ 文档解析任务执行完成
文档ID：ecosystem-blueprint-20260906T120000
文档分类：白皮书
存储状态：success
证据核验：verified 6 / inferred 2 / unsupported 1 / contradicted 0
可查阅：子Agent三份中间输出 + 逐条证据核验 + 完整合并报告
```
若 `storage_status=failed`，追加：
```
> 存储失败提示：数据库存储异常，解析产物已保存本地沙箱文件，请及时导出备份。
```

---

### insert.py 模板
```python
#!/usr/bin/env python3
# db_persist — 通用文档解析结构化持久化（DeerFlow 沙箱内运行）
# V2：新增 document_claim / evidence_verification 两张表 + 抗伪造校验
import json, sqlite3, hashlib, os, datetime
from pathlib import Path

BASE = Path("/tmp/db_persist")
DB_PATH = os.environ.get("DEERFLOW_DB_PATH", "/mnt/user-data/outputs/doc_analysis.db")
SUBAGENT_LIMIT = 60_000
REPORT_LIMIT = 100_000
SHORT_LIMIT = 500
EVIDENCE_LIMIT = 2_000

# 枚举须与 plans/techdoc-agent-general/evidence/evidence_contract.py 保持一致
VERIFICATION_STATUSES = ("verified", "contradicted", "unsupported", "inferred")
CLAIM_TYPES = ("fact", "risk", "action", "summary", "inference")
SOURCE_AGENTS = ("overview", "insight", "action", "coordinator")

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS doc_source (
  doc_id          TEXT PRIMARY KEY,
  doc_name        TEXT NOT NULL,
  doc_category    TEXT NOT NULL,
  upload_time     TEXT NOT NULL,
  doc_snippet     TEXT,
  raw_text_hash   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_intermediate (
  inter_id             INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id               TEXT NOT NULL,
  agent_type           TEXT NOT NULL CHECK (agent_type IN ('overview','insight','action')),
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
CREATE TABLE IF NOT EXISTS document_claim (
  doc_id       TEXT NOT NULL,
  claim_id     TEXT NOT NULL,
  source_agent TEXT NOT NULL CHECK (source_agent IN ('overview','insight','action','coordinator')),
  claim_text   TEXT NOT NULL,
  claim_type   TEXT NOT NULL CHECK (claim_type IN ('fact','risk','action','summary','inference')),
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (doc_id, claim_id),
  FOREIGN KEY (doc_id) REFERENCES doc_source(doc_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS evidence_verification (
  evidence_id         INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id              TEXT NOT NULL,
  claim_id            TEXT NOT NULL,
  verification_status TEXT NOT NULL
    CHECK (verification_status IN ('verified','contradicted','unsupported','inferred')),
  evidence_text       TEXT,
  page                INTEGER,
  section             TEXT,
  source_offset       TEXT,
  source_locator      TEXT,
  confidence          REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
  verify_time         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  FOREIGN KEY (doc_id, claim_id) REFERENCES document_claim(doc_id, claim_id) ON DELETE CASCADE,
  FOREIGN KEY (doc_id) REFERENCES doc_source(doc_id) ON DELETE CASCADE,
  UNIQUE (doc_id, claim_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_intermediate_doc ON agent_intermediate(doc_id);
CREATE INDEX IF NOT EXISTS idx_doc_analysis_result_doc ON doc_analysis_result(doc_id);
CREATE INDEX IF NOT EXISTS idx_document_claim_doc ON document_claim(doc_id);
CREATE INDEX IF NOT EXISTS idx_evidence_verification_doc ON evidence_verification(doc_id);
CREATE INDEX IF NOT EXISTS idx_evidence_verification_status ON evidence_verification(verification_status);
"""

def truncate(s, limit):
    if not s: return ""
    s = str(s)
    return s if len(s) <= limit else s[:limit] + f"\n\n<<TRUNCATED: {len(s)-limit} chars removed by db_persist_skill>>"

def read(name):
    p = BASE / name
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""

def clean_text(v):
    if v is None: return None
    s = str(v).strip()
    return s or None

def clean_claims(raw_claims, doc_id, warnings):
    """校验并过滤 Claim；非法条目跳过并记 warning（不中断写库）。"""
    rows, seen = [], set()
    for i, c in enumerate(raw_claims, 1):
        if not isinstance(c, dict):
            warnings.append(f"claims[{i}] 不是对象，已跳过"); continue
        cid = clean_text(c.get("claim_id"))
        text = clean_text(c.get("claim_text"))
        agent = (clean_text(c.get("source_agent")) or "").lower()
        ctype = (clean_text(c.get("claim_type")) or "").lower()
        if not cid or not text:
            warnings.append(f"claims[{i}] 缺少 claim_id / claim_text，已跳过"); continue
        if cid in seen:
            warnings.append(f"claim_id={cid} 重复，已跳过后续重复项"); continue
        if agent not in SOURCE_AGENTS:
            warnings.append(f"claim_id={cid} 的 source_agent='{agent}' 非法，已跳过"); continue
        if ctype not in CLAIM_TYPES:
            warnings.append(f"claim_id={cid} 的 claim_type='{ctype}' 非法，已跳过"); continue
        seen.add(cid)
        rows.append((doc_id, cid, agent, truncate(text, SHORT_LIMIT), ctype))
    return rows

def clean_evidence(raw_results, doc_id, known_ids, warnings):
    """校验并过滤核验结果；含两条抗伪造规则（page 置 NULL、无证据的 verified 降级）。"""
    rows, seen = [], set()
    for i, r in enumerate(raw_results, 1):
        if not isinstance(r, dict):
            warnings.append(f"evidence_results[{i}] 不是对象，已跳过"); continue
        cid = clean_text(r.get("claim_id"))
        status = (clean_text(r.get("verification_status")) or "").lower()
        if not cid:
            warnings.append(f"evidence_results[{i}] 缺少 claim_id，已跳过"); continue
        if status not in VERIFICATION_STATUSES:
            warnings.append(f"claim_id={cid} 的 verification_status='{status}' 非法，已跳过"); continue
        if cid not in known_ids:
            warnings.append(f"claim_id={cid} 在 claims 中不存在，已丢弃该条核验结果（避免孤儿数据）"); continue
        if cid in seen:
            warnings.append(f"claim_id={cid} 有多条核验结果，已跳过后续重复项"); continue

        loc = r.get("source_location")
        loc = loc if isinstance(loc, dict) else {}
        if clean_text(loc.get("page")) not in (None, "null", "none"):
            warnings.append(f"claim_id={cid}: 检测到编造页码 '{loc.get('page')}'，已按铁律置为 NULL")
        evidence_text = clean_text(r.get("evidence_text"))
        section = clean_text(loc.get("section"))
        offset = clean_text(loc.get("source_offset"))
        locator = clean_text(loc.get("source_locator"))

        # 抗伪造：声称有直接证据却拿不出任何证据 → 降级
        if status in ("verified", "contradicted") and not (evidence_text or locator):
            warnings.append(f"claim_id={cid}: 声称 {status} 但未提供证据，已降级为 unsupported")
            status, evidence_text = "unsupported", None

        conf = r.get("confidence")
        try:
            conf = float(conf) if conf not in (None, "") else None
            if conf is not None and not (0.0 <= conf <= 1.0):
                warnings.append(f"claim_id={cid}: confidence={conf} 超出 [0,1]，已置为 NULL"); conf = None
        except (TypeError, ValueError):
            warnings.append(f"claim_id={cid}: confidence 无法解析，已置为 NULL"); conf = None

        seen.add(cid)
        # page 列硬编码 None —— 即使上面漏判也绝不会写入页码
        rows.append((doc_id, cid, status, truncate(evidence_text, EVIDENCE_LIMIT) or None,
                     None, section, offset, locator, conf))
    return rows

def main():
    meta = json.loads((BASE / "meta.json").read_text(encoding="utf-8"))
    raw = read("raw.txt")
    overview = read("intermediate_overview.md")
    insight = read("intermediate_insight.md")
    action = read("intermediate_action.md")
    report = read("final_analysis_report.md")
    doc_id = meta["doc_id"]
    raw_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    snippet = (meta.get("doc_snippet") or raw[:SHORT_LIMIT]).strip()[:SHORT_LIMIT]
    warnings = []
    for atype, content in (("overview", overview), ("insight", insight), ("action", action)):
        if not content.strip():
            warnings.append(atype + " output is empty")

    # V2：读取 Claim 与核验结果（缺失不算失败）
    claim_rows, evidence_rows, summary = [], [], {s: 0 for s in VERIFICATION_STATUSES}
    try:
        ce = json.loads((BASE / "claims_evidence.json").read_text(encoding="utf-8"))
        claim_rows = clean_claims(ce.get("claims") or [], doc_id, warnings)
        evidence_rows = clean_evidence(ce.get("evidence_results") or [], doc_id,
                                       {r[1] for r in claim_rows}, warnings)
        for row in evidence_rows:
            summary[row[2]] = summary.get(row[2], 0) + 1
    except FileNotFoundError:
        warnings.append("claims_evidence.json 缺失：已跳过 document_claim / evidence_verification 两张表")
    except Exception as e:
        warnings.append("claims_evidence.json 解析失败(%s)：已跳过两张新表" % e)

    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    try:
        con.executescript(SCHEMA)
        cur = con.cursor()
        # 幂等：按 doc_id 清理旧数据（含 V2 两张新表），doc_source 用 upsert
        cur.execute("DELETE FROM evidence_verification WHERE doc_id=?", (doc_id,))
        cur.execute("DELETE FROM document_claim WHERE doc_id=?", (doc_id,))
        cur.execute("DELETE FROM agent_intermediate WHERE doc_id=?", (doc_id,))
        cur.execute("DELETE FROM doc_analysis_result WHERE doc_id=?", (doc_id,))
        cur.execute("""INSERT INTO doc_source(doc_id,doc_name,doc_category,upload_time,doc_snippet,raw_text_hash)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(doc_id) DO UPDATE SET
                         doc_name=excluded.doc_name, doc_category=excluded.doc_category,
                         upload_time=excluded.upload_time, doc_snippet=excluded.doc_snippet,
                         raw_text_hash=excluded.raw_text_hash""",
                    (doc_id, meta.get("doc_name",""), meta.get("doc_category","其他"),
                     meta.get("upload_time") or datetime.datetime.now().isoformat(timespec="seconds"),
                     truncate(snippet, SHORT_LIMIT), raw_hash))
        for atype, content in (("overview", overview), ("insight", insight), ("action", action)):
            if content.strip():
                cur.execute("INSERT INTO agent_intermediate(doc_id,agent_type,agent_output_content) VALUES(?,?,?)",
                            (doc_id, atype, truncate(content, SUBAGENT_LIMIT)))
        cur.execute("""INSERT INTO doc_analysis_result(doc_id,final_report_content,summary_conclusion,hallucination_check_flag)
                       VALUES(?,?,?,?)""",
                    (doc_id, truncate(report, REPORT_LIMIT),
                     truncate(meta.get("summary_conclusion",""), SHORT_LIMIT),
                     meta.get("hallucination_check_flag","unknown")))
        cur.executemany("""INSERT INTO document_claim(doc_id,claim_id,source_agent,claim_text,claim_type)
                           VALUES(?,?,?,?,?)""", claim_rows)
        cur.executemany("""INSERT INTO evidence_verification
                           (doc_id,claim_id,verification_status,evidence_text,page,section,source_offset,source_locator,confidence)
                           VALUES(?,?,?,?,?,?,?,?,?)""", evidence_rows)
        con.commit()
        print(json.dumps({"storage_status":"success","doc_id":doc_id,"message":"持久化成功",
                          "claim_count":len(claim_rows),"evidence_count":len(evidence_rows),
                          "evidence_summary":summary,"warnings":warnings}, ensure_ascii=False))
    except Exception as e:
        con.rollback()
        print(json.dumps({"storage_status":"failed","doc_id":doc_id,"message":"持久化失败: %s" % e}, ensure_ascii=False))
    finally:
        con.close()

if __name__ == "__main__":
    main()
```

> **校验规则须与此处同步**：上文「铁律 5/6」与 `clean_evidence()` 的抗伪造逻辑，
> 是 `plans/techdoc-agent-general/evidence/evidence_contract.py` 中
> `enforce_evidence_integrity()` 的最小内联副本（技能树只读挂载在 `/mnt/skills`，
> 沙箱脚本无法 import 该模块，故必须内联）。**改动其一务必同步另一个**，
> 权威规格以 `evidence_contract.py` 及其测试为准。
