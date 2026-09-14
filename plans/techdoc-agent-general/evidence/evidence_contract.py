#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evidence_contract.py — TechDocAnalyzer V2「Evidence Agent + 原文证据溯源」契约层。

为什么需要这个文件
------------------
DeerFlow 框架**没有**给子 Agent 提供 JSON / Pydantic / structured output 机制：
`subagents/executor.py::_extract_final_result` 只是把最后一条 AIMessage 的文本原样抽出来，
`contracts/subagent_status_contract.json` 也只约束元数据（其文件内明确写着
"Task result text is display content only and is not part of this wire contract"）。
要真正约束子 Agent 输出为 JSON，必须改 `SubagentExecutor._aexecute`——那属于改写框架核心，本阶段禁止。

因此本模块承担「契约真源」角色，只做三件事（全部零第三方依赖，纯 stdlib）：

1. **解析**：把 Evidence Agent 输出的 JSON（写在 Markdown 代码块里，或裸 JSON）解析成结构化对象；
2. **校验 / 抗伪造**：把「宁可没有定位信息，也不能伪造定位信息」这类产品铁律变成**可执行、可测试**的规则；
3. **渲染**：把核验结果渲染成 Markdown 报告片段，保证四种状态在报告里区分明确、绝不混淆。

它同时是**四种状态的可执行规格**：`REFERENCE_SCENARIOS` 就是产品定义，被两个测试文件共用。

运行时说明（已知限制）
----------------------
沙箱内真正落库的是 `db_persist_skill` 的 `insert.py`（技能自带脚本，技能树只读挂载在 /mnt/skills），
它**无法 import 本模块**，因此内联了一份约 20 行的等价最小校验。本模块是权威规格 + 测试靶子，
`insert.py` 里的副本必须与之保持一致（详见 db_persist_skill/SKILL.md 的「校验规则须与此处同步」）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 一、枚举（唯一真源；config.yaml 提示词、schema.sql CHECK、insert.py 都必须与此一致）
# ---------------------------------------------------------------------------

#: 证据核验状态，四选一，绝不允许第五种取值。
VERIFICATION_STATUSES: tuple[str, ...] = ("verified", "contradicted", "unsupported", "inferred")

#: Claim 类型（对应 SOUL 里的 fact/risk/action/summary/inference）。
CLAIM_TYPES: tuple[str, ...] = ("fact", "risk", "action", "summary", "inference")

#: Claim 的来源 Agent。coordinator = 主调度 Agent 自己合并出的综合结论。
SOURCE_AGENTS: tuple[str, ...] = ("overview", "insight", "action", "coordinator")

#: 状态的中文说明（报告渲染与文档共用，避免各处口径不一）。
STATUS_NOTES: dict[str, str] = {
    "verified": "原文明确支持该结论。",
    "contradicted": "原文存在与该结论相反的信息。",
    "unsupported": "未在原始文档中找到足够的直接依据。",
    "inferred": "该结论属于基于原文信息的推断，原文没有直接陈述。",
}

#: 需要在报告里显式暴露「非直接证据」的状态（防混淆用）。
WARNING_STATUSES: tuple[str, ...] = ("unsupported", "inferred")

#: 报告里 unsupported / inferred 必须带的前缀，防止被当成 verified 阅读。
WARNING_PREFIX = "⚠️"


def normalize_token(value: Any) -> str:
    """只做**格式**归一（大小写 / 首尾空白 / 包裹的引号或反引号 / 句末标点）。

    刻意**不做语义别名**（例如不把 unknown 映射成 unsupported）：
    无法识别的取值一律作为 issue 上报并剔除，绝不"猜"成某个有意义的结论。
    """
    if value is None:
        return ""
    s = str(value).strip().strip("`'\"“”‘’").strip()
    s = s.rstrip(".,;:。，；：")
    return s.strip().lower()


def format_claim_id(index: int) -> str:
    """把 1-based 序号格式化为 claim_id，如 1 -> "C001"。

    claim_id 只保证**在单份文档内**唯一（C001、C002…）；跨文档唯一性由 (doc_id, claim_id) 复合主键保证。
    """
    return f"C{int(index):03d}"


# ---------------------------------------------------------------------------
# 二、数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceLocation:
    """原文定位信息。

    **铁律**：`page` 恒为 None。
    DeerFlow 的文档转换（`utils/file_conversion.py`）把 PDF/Office 转成**一份扁平 Markdown**，
    **不保留页码**；系统里唯一的定位体系是 `utils/file_outline.py::extract_outline` 返回的
    `{title, line}`（1-based 行号）。所以任何"页码"都只可能是模型编造的，必须拒绝。

    其余字段拿不到就留 None——**宁可没有定位信息，也不能伪造定位信息**。
    """

    page: None = None  # 永远为 None，见类文档字符串
    section: str | None = None  # 章节 / 标题 / 条款号（如"第二条"）
    source_offset: str | None = None  # 行号区间（如 "120-134"），来自 extract_outline 的行号体系
    source_locator: str | None = None  # 原文逐字片段（最可靠的定位手段）

    def is_empty(self) -> bool:
        return not (self.section or self.source_offset or self.source_locator)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": None,
            "section": self.section,
            "source_offset": self.source_offset,
            "source_locator": self.source_locator,
        }

    def render(self) -> str:
        """渲染成一行可读来源；无任何定位信息时显式说明，而不是留空让人以为漏了。"""
        parts: list[str] = []
        if self.section:
            parts.append(f"章节：{self.section}")
        if self.source_offset:
            parts.append(f"行：{self.source_offset}")
        if self.source_locator:
            parts.append(f"原文片段：“{self.source_locator}”")
        if not parts:
            return "（原文未提供可定位信息：无页码 / 无章节 / 无可回溯片段）"
        return " · ".join(parts) + "（页码：当前转换不保留，故为 null）"


@dataclass(frozen=True)
class Claim:
    """轻量 Claim 层：从三份子 Agent 输出 + 合并结果里抽出的关键结论。"""

    doc_id: str
    claim_id: str
    source_agent: str
    claim_text: str
    claim_type: str
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "claim_id": self.claim_id,
            "source_agent": self.source_agent,
            "claim_text": self.claim_text,
            "claim_type": self.claim_type,
            "created_at": self.created_at,
        }

    def to_db_row(self) -> tuple:
        """列顺序与 schema.sql 的 document_claim 一致。"""
        return (self.doc_id, self.claim_id, self.source_agent, self.claim_text, self.claim_type)


@dataclass(frozen=True)
class EvidenceResult:
    """Evidence Agent 对单条 Claim 的核验结论。"""

    doc_id: str
    claim_id: str
    source_agent: str
    claim: str
    verification_status: str
    evidence_text: str | None = None
    source_location: SourceLocation = field(default_factory=SourceLocation)
    confidence: float | None = None
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "claim_id": self.claim_id,
            "source_agent": self.source_agent,
            "claim": self.claim,
            "verification_status": self.verification_status,
            "evidence_text": self.evidence_text,
            "source_location": self.source_location.to_dict(),
            "confidence": self.confidence,
            "notes": self.notes,
        }

    def to_db_row(self) -> tuple:
        """列顺序与 schema.sql 的 evidence_verification 一致。"""
        loc = self.source_location
        return (
            self.doc_id,
            self.claim_id,
            self.verification_status,
            self.evidence_text,
            None,  # page —— 恒为 NULL，见 SourceLocation 文档字符串
            loc.section,
            loc.source_offset,
            loc.source_locator,
            self.confidence,
        )

    def is_direct_evidence(self) -> bool:
        """是否为"直接证据"状态（verified / contradicted）。"""
        return self.verification_status in ("verified", "contradicted")


@dataclass
class ParseOutcome:
    """解析结果：成功项 + 问题清单。

    刻意**不抛异常**：一份文档里某条证据格式坏了，不应该让整份报告生成失败。
    问题进入 `issues`，由主调度 Agent 决定是标注还是重试。
    """

    items: list[Any] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


# ---------------------------------------------------------------------------
# 三、JSON 抽取（兼容 "写在代码块里" 与 "裸 JSON" 两种形态）
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\r?\n(.*?)```", re.DOTALL)


def _balanced_json_objects(text: str) -> list[str]:
    """扫描全文，抽出所有括号配平的 {...} 片段（正确跳过字符串内的花括号与转义）。"""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, in_str, esc = 0, False, False
        closed = False
        for j in range(i, n):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    out.append(text[i : j + 1])
                    i = j
                    closed = True
                    break
        if not closed:
            break
        i += 1
    return out


def _payloads_from(text: str, required_key: str, strict_fence_first: bool = True) -> list[dict]:
    """抽取所有**含 required_key** 的 JSON 对象。先看代码块（精度高），再退回全文扫描（容忍裸 JSON）。"""
    candidates: list[str] = []
    if text:
        if strict_fence_first:
            candidates.extend(m.group(1).strip() for m in _FENCE_RE.finditer(text))
            candidates.extend(_balanced_json_objects(text))
        else:
            candidates.extend(_balanced_json_objects(text))

    seen: set[str] = set()
    found: list[dict] = []
    for body in candidates:
        if not body or body in seen:
            continue
        seen.add(body)
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue
        for obj in parsed if isinstance(parsed, list) else [parsed]:
            if isinstance(obj, dict) and required_key in obj:
                found.append(obj)
    return found


# ---------------------------------------------------------------------------
# 四、解析：Claims
# ---------------------------------------------------------------------------

_CLAIM_KEYS = ("claim_id", "source_agent", "claim_text", "claim_type")


def parse_claims(text: str, doc_id: str, created_at: str | None = None) -> ParseOutcome:
    """解析主调度 Agent 抽取出的 Claim 列表。

    期望形态（写在 Markdown 代码块里，或裸 JSON）::

        {"claims": [{"claim_id": "C001", "source_agent": "insight",
                     "claim_text": "...", "claim_type": "risk"}]}
    """
    payloads = _payloads_from(text or "", "claims")
    if not payloads:
        return ParseOutcome([], ["未在输入中找到含 'claims' 的 JSON 对象。"])

    issues: list[str] = []
    claims: list[Claim] = []
    seen_ids: set[str] = set()

    for payload in payloads:
        raw_list = payload.get("claims")
        if not isinstance(raw_list, list):
            issues.append("'claims' 不是数组，已跳过该 JSON 对象。")
            continue
        for idx, raw in enumerate(raw_list, start=1):
            if not isinstance(raw, dict):
                issues.append(f"claims[{idx}] 不是对象，已跳过。")
                continue
            missing = [k for k in _CLAIM_KEYS if not str(raw.get(k) or "").strip()]
            if missing:
                issues.append(f"claims[{idx}] 缺少必填字段 {'/'.join(missing)}，已跳过。")
                continue

            agent = normalize_token(raw["source_agent"])
            if agent not in SOURCE_AGENTS:
                issues.append(f"claims[{idx}] 的 source_agent='{raw['source_agent']}' 非法（允许：{'/'.join(SOURCE_AGENTS)}），已跳过。")
                continue

            ctype = normalize_token(raw["claim_type"])
            if ctype not in CLAIM_TYPES:
                issues.append(f"claims[{idx}] 的 claim_type='{raw['claim_type']}' 非法（允许：{'/'.join(CLAIM_TYPES)}），已跳过。")
                continue

            cid = str(raw["claim_id"]).strip()
            if cid in seen_ids:
                issues.append(f"claim_id='{cid}' 重复，已跳过后续重复项。")
                continue
            seen_ids.add(cid)
            claims.append(
                Claim(
                    doc_id=doc_id,
                    claim_id=cid,
                    source_agent=agent,
                    claim_text=str(raw["claim_text"]).strip(),
                    claim_type=ctype,
                    created_at=str(raw.get("created_at") or created_at or "") or None,
                )
            )

    if not claims and not issues:
        issues.append("claims 数组为空。")
    return ParseOutcome(claims, issues)


# ---------------------------------------------------------------------------
# 五、解析：Evidence + 抗伪造强制规则
# ---------------------------------------------------------------------------

_FORBIDDEN_PAGE_HINT = "当前文档转换不保留页码，已按铁律置为 null（禁止伪造定位信息）。"


def _parse_source_location(raw: Any) -> tuple[SourceLocation, list[str]]:
    """解析 source_location，并强制执行「page 必须为 null」。"""
    issues: list[str] = []
    if not isinstance(raw, dict):
        if raw not in (None, ""):
            issues.append("source_location 不是对象，已按空定位处理。")
        return SourceLocation(), issues

    page = raw.get("page")
    if page not in (None, "", "null", "None"):
        # 关键防线：系统里根本没有页码信息，模型给出一律视为编造，强制置 null 并上报。
        issues.append(f"source_location.page='{page}' 被拒绝：{_FORBIDDEN_PAGE_HINT}")

    def _clean(key: str) -> str | None:
        v = raw.get(key)
        if v in (None, "", "null", "None"):
            return None
        return str(v).strip()

    return (
        SourceLocation(
            page=None,
            section=_clean("section"),
            source_offset=_clean("source_offset"),
            source_locator=_clean("source_locator"),
        ),
        issues,
    )


def _parse_confidence(raw: Any) -> tuple[float | None, list[str]]:
    if raw in (None, ""):
        return None, []
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, [f"confidence='{raw}' 无法解析为数值，已置为 null。"]
    if not 0.0 <= value <= 1.0:
        return None, [f"confidence={value} 超出 [0,1]，已置为 null。"]
    return value, []


def enforce_evidence_integrity(results: list[EvidenceResult]) -> ParseOutcome:
    """抗伪造强制规则（把产品铁律变成可执行代码）。

    规则 1：`page` 必须为 None —— 已在解析阶段强制。
    规则 2：状态为 verified / contradicted（声称"有直接证据"）时，**必须**给出
            `evidence_text` 或 `source_locator` 之一；都给不出说明证据不存在，
            此时降级为 `unsupported` 并上报——**绝不允许把没有证据的结论标成已验证**。
    规则 3：状态为 unsupported 时不应附带证据文本；若带了，保留状态但提示复核。
    """
    fixed: list[EvidenceResult] = []
    issues: list[str] = []

    for r in results:
        has_evidence = bool((r.evidence_text or "").strip() or (r.source_location.source_locator or "").strip())

        if r.verification_status in ("verified", "contradicted") and not has_evidence:
            issues.append(
                f"{r.claim_id} 声称 {r.verification_status} 但未提供 evidence_text / source_locator，"
                "已按抗伪造规则降级为 unsupported。"
            )
            r = EvidenceResult(
                doc_id=r.doc_id,
                claim_id=r.claim_id,
                source_agent=r.source_agent,
                claim=r.claim,
                verification_status="unsupported",
                evidence_text=None,
                source_location=r.source_location,
                confidence=r.confidence,
                notes="原状态声称有直接证据但未给出任何证据，按铁律降级为 unsupported。",
            )
        elif r.verification_status == "unsupported" and (r.evidence_text or "").strip():
            issues.append(f"{r.claim_id} 状态为 unsupported 却带有 evidence_text，请人工复核该条核验结果。")

        fixed.append(r)

    return ParseOutcome(fixed, issues)


def parse_evidence(text: str, doc_id: str) -> ParseOutcome:
    """解析 Evidence Agent 输出。

    期望形态（写在 Markdown 代码块里，或裸 JSON）::

        {"evidence_results": [{
            "claim_id": "C001",
            "source_agent": "overview",
            "claim": "...",
            "verification_status": "verified",
            "evidence_text": "...",
            "source_location": {"page": null, "section": "第二条",
                                "source_offset": "12-14", "source_locator": "..."},
            "confidence": 0.96
        }]}

    返回的 items 已经过 `enforce_evidence_integrity`，即**可以直接信任其状态语义**。
    """
    payloads = _payloads_from(text or "", "evidence_results")
    if not payloads:
        return ParseOutcome([], ["未在输入中找到含 'evidence_results' 的 JSON 对象。"])

    issues: list[str] = []
    results: list[EvidenceResult] = []
    seen_ids: set[str] = set()

    for payload in payloads:
        raw_list = payload.get("evidence_results")
        if not isinstance(raw_list, list):
            issues.append("'evidence_results' 不是数组，已跳过该 JSON 对象。")
            continue
        for idx, raw in enumerate(raw_list, start=1):
            if not isinstance(raw, dict):
                issues.append(f"evidence_results[{idx}] 不是对象，已跳过。")
                continue
            for key in ("claim_id", "verification_status", "claim"):
                if not str(raw.get(key) or "").strip():
                    issues.append(f"evidence_results[{idx}] 缺少必填字段 '{key}'，已跳过。")
                    break
            else:
                status = normalize_token(raw["verification_status"])
                if status not in VERIFICATION_STATUSES:
                    # 刻意不猜测：未知状态可能是模型自造语义，猜错会把 unsupported 说成 verified。
                    issues.append(
                        f"evidence_results[{idx}] 的 verification_status='{raw['verification_status']}' "
                        f"非法（只允许 {'/'.join(VERIFICATION_STATUSES)}），已剔除该条。"
                    )
                    continue

                cid = str(raw["claim_id"]).strip()
                if cid in seen_ids:
                    issues.append(f"claim_id='{cid}' 出现多条核验结果，已跳过后续重复项。")
                    continue
                seen_ids.add(cid)

                loc, loc_issues = _parse_source_location(raw.get("source_location"))
                conf, conf_issues = _parse_confidence(raw.get("confidence"))
                issues.extend(f"{cid}: {m}" for m in loc_issues + conf_issues)

                results.append(
                    EvidenceResult(
                        doc_id=doc_id,
                        claim_id=cid,
                        source_agent=normalize_token(raw.get("source_agent")),
                        claim=str(raw["claim"]).strip(),
                        verification_status=status,
                        evidence_text=(str(raw["evidence_text"]).strip() if raw.get("evidence_text") else None),
                        source_location=loc,
                        confidence=conf,
                        notes=(str(raw["notes"]).strip() if raw.get("notes") else None),
                    )
                )

    integrity = enforce_evidence_integrity(results)
    return ParseOutcome(integrity.items, issues + integrity.issues)


# ---------------------------------------------------------------------------
# 六、交叉校验：Evidence 必须对应到真实存在的 Claim
# ---------------------------------------------------------------------------


def validate_against_claims(results: list[EvidenceResult], claims: list[Claim]) -> list[str]:
    """核对核验结果与 Claim 列表的一致性。返回问题清单（空 = 一致）。"""
    issues: list[str] = []
    by_id = {c.claim_id: c for c in claims}

    for r in results:
        claim = by_id.get(r.claim_id)
        if claim is None:
            issues.append(f"{r.claim_id} 的核验结果找不到对应 Claim（claim_id 不存在）。")
            continue
        if claim.source_agent != r.source_agent:
            issues.append(
                f"{r.claim_id} 的 source_agent 不一致：Claim='{claim.source_agent}'，Evidence='{r.source_agent}'。"
            )

    covered = {r.claim_id for r in results}
    for c in claims:
        if c.claim_id not in covered:
            issues.append(f"{c.claim_id} 未获得任何证据核验结果（Evidence Agent 漏检）。")

    return issues


# ---------------------------------------------------------------------------
# 七、渲染：Markdown 报告片段
# ---------------------------------------------------------------------------

STATUS_EMOJI: dict[str, str] = {
    "verified": "✅",
    "contradicted": "❌",
    "unsupported": WARNING_PREFIX,
    "inferred": WARNING_PREFIX,
}


def render_evidence_section(result: EvidenceResult, heading_level: int = 4) -> str:
    """渲染单条 Claim 的证据核验片段。

    四态渲染分支互斥，**unsupported / inferred 永远不可能渲染成 verified**（由构造保证）。
    """
    h = "#" * max(3, min(6, heading_level))
    status = result.verification_status
    lines: list[str] = [f"{h} 证据核验", "", f"**状态**：{STATUS_EMOJI.get(status, '')} {status}", ""]
    lines.append(f"- **结论**：{result.claim}")

    if result.is_direct_evidence():
        if result.evidence_text:
            label = "证据" if status == "verified" else "反向证据"
            lines.append(f"- **{label}**：“{result.evidence_text}”")
        lines.append(f"- **来源**：{result.source_location.render()}")
        lines.append(f"- **置信度**：{result.confidence if result.confidence is not None else '未提供'}")
        if status == "contradicted":
            lines.append(f"- {STATUS_EMOJI['contradicted']} **说明**：{STATUS_NOTES['contradicted']}")
    else:
        lines.append(f"- {WARNING_PREFIX} **说明**：{STATUS_NOTES[status]}")
        if result.source_location.source_locator:
            lines.append(f"- **相关原文片段（仅供推断参考，非直接依据）**：“{result.source_location.source_locator}”")
        if result.confidence is not None:
            lines.append(f"- **置信度**：{result.confidence}")

    if result.notes:
        lines.append(f"- **备注**：{result.notes}")
    lines.append("")
    return "\n".join(lines)


def summarize(results: list[EvidenceResult]) -> dict[str, int]:
    """核验总览计数。"""
    summary = {s: 0 for s in VERIFICATION_STATUSES}
    for r in results:
        summary[r.verification_status] = summary.get(r.verification_status, 0) + 1
    summary["total"] = len(results)
    return summary


def render_verification_summary(results: list[EvidenceResult]) -> str:
    s = summarize(results)
    return (
        f"核验总览：共 {s['total']} 条结论 —— "
        f"✅ verified {s['verified']} / ❌ contradicted {s['contradicted']} / "
        f"{WARNING_PREFIX} unsupported {s['unsupported']} / {WARNING_PREFIX} inferred {s['inferred']}"
    )


def render_evidence_section_block(results: list[EvidenceResult], title: str = "Evidence Verification（原文证据核验）") -> str:
    """渲染整节：总览 + 逐条核验。供主调度 Agent 直接贴进最终报告。"""
    if not results:
        return f"## {title}\n\n（本次未能产出证据核验结果。）\n"
    parts = [f"## {title}", "", render_verification_summary(results), ""]
    parts.extend(render_evidence_section(r) for r in results)
    return "\n".join(parts).rstrip() + "\n"


def render_claims_table(claims: list[Claim]) -> str:
    """渲染 Claim 清单表格（便于人工核对 claim_id ↔ 结论）。"""
    lines = ["| claim_id | 来源 Agent | 类型 | 结论 |", "|---|---|---|---|"]
    for c in claims:
        text = c.claim_text.replace("|", "\\|")
        lines.append(f"| {c.claim_id} | {c.source_agent} | {c.claim_type} | {text} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 八、四种状态的可执行规格（产品定义即测试数据）
# ---------------------------------------------------------------------------
# 每项是一条完整场景：原文 + Claim + Evidence Agent 应产出的核验结果 + 期望状态。
# 两个测试文件（plans/ 与 backend/tests/）共用，避免规格分叉。
# 说明：这里固定的是**契约语义**——即给定 Evidence Agent 的这一输出，系统必须判定/渲染成该状态。
# 至于 LLM 面对真实文本能否判对，属于需要在线跑 E2E 才能验证的范畴，离线测试不假装覆盖。

REFERENCE_SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "name": "Test1 原文明确支持 → verified",
        "raw_text": "本政策支持人工智能企业发展。对符合条件的人工智能企业，给予专项资金支持。",
        "claim": {
            "claim_id": "C001",
            "source_agent": "overview",
            "claim_text": "该政策支持人工智能企业发展。",
            "claim_type": "fact",
        },
        "evidence": {
            "claim_id": "C001",
            "source_agent": "overview",
            "claim": "该政策支持人工智能企业发展。",
            "verification_status": "verified",
            "evidence_text": "本政策支持人工智能企业发展。",
            "source_location": {
                "page": None,
                "section": "第一条",
                "source_offset": "1-1",
                "source_locator": "本政策支持人工智能企业发展。",
            },
            "confidence": 0.96,
        },
        "expected_status": "verified",
    },
    {
        "name": "Test2 原文完全没有 → unsupported",
        "raw_text": "本政策支持人工智能企业发展。对符合条件的人工智能企业，给予专项资金支持。",
        "claim": {
            "claim_id": "C001",
            "source_agent": "insight",
            "claim_text": "该政策提供500万元补贴。",
            "claim_type": "fact",
        },
        "evidence": {
            "claim_id": "C001",
            "source_agent": "insight",
            "claim": "该政策提供500万元补贴。",
            "verification_status": "unsupported",
            "evidence_text": None,
            "source_location": {"page": None, "section": None, "source_offset": None, "source_locator": None},
            "confidence": 0.1,
        },
        "expected_status": "unsupported",
    },
    {
        "name": "Test3 原文明确相反 → contradicted",
        "raw_text": "本政策不适用于个人开发者。本政策仅面向注册企业。",
        "claim": {
            "claim_id": "C001",
            "source_agent": "insight",
            "claim_text": "本政策适用于个人开发者。",
            "claim_type": "fact",
        },
        "evidence": {
            "claim_id": "C001",
            "source_agent": "insight",
            "claim": "本政策适用于个人开发者。",
            "verification_status": "contradicted",
            "evidence_text": "本政策不适用于个人开发者。",
            "source_location": {
                "page": None,
                "section": "适用范围",
                "source_offset": "1-1",
                "source_locator": "本政策不适用于个人开发者。",
            },
            "confidence": 0.93,
        },
        "expected_status": "contradicted",
    },
    {
        "name": "Test4 基于原文的合理推断 → inferred",
        "raw_text": "企业可以申请研发资金支持。",
        "claim": {
            "claim_id": "C001",
            "source_agent": "coordinator",
            "claim_text": "该政策可能降低企业研发资金压力。",
            "claim_type": "inference",
        },
        "evidence": {
            "claim_id": "C001",
            "source_agent": "coordinator",
            "claim": "该政策可能降低企业研发资金压力。",
            "verification_status": "inferred",
            "evidence_text": None,
            "source_location": {
                "page": None,
                "section": None,
                "source_offset": None,
                "source_locator": "企业可以申请研发资金支持。",
            },
            "confidence": 0.62,
        },
        "expected_status": "inferred",
    },
)


def scenario_id(scenario: dict[str, Any]) -> str:
    """把场景渲染成完整 JSON 输入（等价于 Evidence Agent 的实际输出文本）。"""
    return json.dumps({"evidence_results": [scenario["evidence"]]}, ensure_ascii=False)


def scenario_claims_payload(scenarios: tuple[dict[str, Any], ...] = REFERENCE_SCENARIOS) -> str:
    """把场景的 Claim 渲染成主调度 Agent 会写出的 claims JSON。"""
    return json.dumps({"claims": [s["claim"] for s in scenarios]}, ensure_ascii=False)
