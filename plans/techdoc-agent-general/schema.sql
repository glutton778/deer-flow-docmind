-- ============================================================================
-- 通用文档解析 · 结构化持久化 Schema（SQLite）
-- 说明：
--   * 时间戳统一用 TEXT（ISO-8601），SQLite 无原生 DATETIME 类型。
--   * 为避免单字段过大，长文本（子Agent输出 / 最终报告）在写入前由
--     db_persist_skill 截断（子输出 60k 字符、最终报告 100k 字符、摘要 500 字符）。
--   * 外键 ON DELETE CASCADE：删除一份文档，其子输出与最终结果一并删除。
--   * doc_category 为开放取值（书籍/白皮书/会议记录/任务表/介绍/其他…），
--     此处用 TEXT 不加 CHECK，保留扩展空间；agent_type 则严格三选一。
--   * 目标库：/mnt/user-data/outputs/doc_analysis.db（沙箱内 host 持久目录）。
--
-- V2（Evidence Agent + 原文证据溯源）增量：
--   * 新增第 4、5 张表 document_claim / evidence_verification；
--   * 前 3 张表的字段与语义**完全未改动**（含 agent_type 的三选一 CHECK、
--     hallucination_check_flag 的 pass/fail/unknown CHECK）；
--   * document_claim 用 **(doc_id, claim_id) 复合主键**：claim_id 只保证单份文档内唯一
--     （C001、C002…，方便 LLM 顺序编号），复合主键避免跨文档撞号；
--   * evidence_verification.page **恒为 NULL**：当前文档转换不保留页码，填页码即编造。
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 1. 文档源表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doc_source (
    doc_id          TEXT PRIMARY KEY,             -- 文档唯一标识（主调度Agent生成，全流程复用）
    doc_name        TEXT    NOT NULL,             -- 文件名 / 文档标题
    doc_category    TEXT    NOT NULL,             -- 书籍 / 白皮书 / 会议记录 / 任务表 / 介绍 / 其他
    upload_time     TEXT    NOT NULL,             -- 上传时间（ISO-8601）
    doc_snippet     TEXT,                         -- 一句话摘要（默认取 raw 前 500 字符）
    raw_text_hash   TEXT    NOT NULL              -- sha256(raw_doc_text)，用于去重/溯源
);
CREATE INDEX IF NOT EXISTS idx_doc_source_raw_hash ON doc_source(raw_text_hash);

-- ---------------------------------------------------------------------------
-- 2. 子Agent 中间输出表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_intermediate (
    inter_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id               TEXT    NOT NULL,        -- 关联 doc_source.doc_id
    agent_type           TEXT    NOT NULL
        CHECK (agent_type IN ('overview', 'insight', 'action')),  -- overview / insight / action
    agent_output_content TEXT    NOT NULL,        -- 子Agent原始输出（截断后）
    generate_time        TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (doc_id) REFERENCES doc_source(doc_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agent_intermediate_doc_id ON agent_intermediate(doc_id);

-- ---------------------------------------------------------------------------
-- 3. 最终结果表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doc_analysis_result (
    result_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id                  TEXT    NOT NULL,     -- 关联 doc_source.doc_id
    final_report_content    TEXT    NOT NULL,     -- 最终报告（截断后）
    summary_conclusion      TEXT,                 -- 「综合结论与建议」一节
    hallucination_check_flag TEXT   NOT NULL DEFAULT 'unknown'
        CHECK (hallucination_check_flag IN ('pass', 'fail', 'unknown')), -- 事实一致性/幻觉校验
    create_time             TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (doc_id) REFERENCES doc_source(doc_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_doc_analysis_result_doc_id ON doc_analysis_result(doc_id);

-- ---------------------------------------------------------------------------
-- 4. Claim 表（V2 新增）——从三份子输出 + 合并结果里抽出的「待核验关键结论」
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_claim (
    doc_id       TEXT NOT NULL,                   -- 关联 doc_source.doc_id
    claim_id     TEXT NOT NULL,                   -- C001 / C002 …（单份文档内唯一）
    source_agent TEXT NOT NULL
        CHECK (source_agent IN ('overview', 'insight', 'action', 'coordinator')),
    claim_text   TEXT NOT NULL,                   -- 结论原文
    claim_type   TEXT NOT NULL
        CHECK (claim_type IN ('fact', 'risk', 'action', 'summary', 'inference')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (doc_id, claim_id),               -- 复合主键：允许不同文档都从 C001 开始
    FOREIGN KEY (doc_id) REFERENCES doc_source(doc_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_document_claim_doc_id ON document_claim(doc_id);

-- ---------------------------------------------------------------------------
-- 5. 证据核验表（V2 新增）——每条 Claim 一条核验结果
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence_verification (
    evidence_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id              TEXT NOT NULL,            -- 关联 doc_source.doc_id
    claim_id            TEXT NOT NULL,            -- 关联 document_claim.(doc_id, claim_id)
    verification_status TEXT NOT NULL
        CHECK (verification_status IN ('verified', 'contradicted', 'unsupported', 'inferred')),
    evidence_text       TEXT,                     -- 逐字摘抄的证据原文（verified/contradicted 必填）
    page                INTEGER,                  -- 恒为 NULL：当前转换不保留页码，禁止伪造
    section             TEXT,                     -- 章节 / 条款号（如"第二条"），拿不到即 NULL
    source_offset       TEXT,                     -- 行号区间（如 "12-14"），拿不到即 NULL
    source_locator      TEXT,                     -- 原文逐字片段（最可靠的定位手段），拿不到即 NULL
    confidence          REAL
        CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    verify_time         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (doc_id, claim_id)
        REFERENCES document_claim(doc_id, claim_id) ON DELETE CASCADE,
    FOREIGN KEY (doc_id) REFERENCES doc_source(doc_id) ON DELETE CASCADE,
    UNIQUE (doc_id, claim_id)                     -- 一条 Claim 恰好一条核验结果
);
CREATE INDEX IF NOT EXISTS idx_evidence_verification_doc_id ON evidence_verification(doc_id);
CREATE INDEX IF NOT EXISTS idx_evidence_verification_status ON evidence_verification(verification_status);
