-- ============================================================================
-- 技术文档智能解析 · 结构化持久化 Schema（SQLite）
-- 说明：
--   * 时间戳统一用 TEXT（ISO-8601），SQLite 无原生 DATETIME 类型。
--   * 为避免单字段过大，长文本（子Agent输出 / 最终报告）在写入前由
--     db_persist_skill 截断（子输出 60k 字符、最终报告 100k 字符、摘要 500 字符）。
--   * 外键 ON DELETE CASCADE：删除一份文档，其子输出与最终结果一并删除。
--   * 目标库：/mnt/user-data/outputs/tech_analysis.db（沙箱内 host 持久目录）。
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 1. 文档源表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doc_source (
    doc_id          TEXT PRIMARY KEY,             -- 文档唯一标识（主Agent生成，全流程复用）
    doc_name        TEXT    NOT NULL,             -- 文件名 / 文档标题
    doc_type        TEXT    NOT NULL,             -- pdf / text / docx / md / other
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
        CHECK (agent_type IN ('core', 'risk', 'todo')),   -- core / risk / todo
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
