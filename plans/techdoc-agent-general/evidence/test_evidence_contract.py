#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""契约层测试 —— 覆盖用户指定的 Test1–Test4 四个场景 + 抗伪造强制规则。

运行方式（零第三方依赖，纯 stdlib）::

    python -m unittest discover -s plans/techdoc-agent-general/evidence -v
    # 或
    python plans/techdoc-agent-general/evidence/test_evidence_contract.py -v

也兼容 pytest（用例是标准 unittest.TestCase，可被 pytest 直接收集）。

**测试边界（诚实声明）**：本文件固定的是**契约语义**——即"给定 Evidence Agent 的这一输出，
系统必须判定成什么状态、渲染成什么样"。它**不**声称能离线验证「LLM 面对真实文档能否判对状态」，
那需要在线跑端到端（见 DEPLOYMENT_NOTES.md 的手工验证步骤）。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evidence_contract import (  # noqa: E402
    CLAIM_TYPES,
    REFERENCE_SCENARIOS,
    SOURCE_AGENTS,
    VERIFICATION_STATUSES,
    Claim,
    EvidenceResult,
    SourceLocation,
    format_claim_id,
    parse_claims,
    parse_evidence,
    render_evidence_section,
    render_evidence_section_block,
    render_verification_summary,
    scenario_id,
    summarize,
    validate_against_claims,
)

DOC_ID = "test-doc-0001"


def _pipeline(scenario: dict) -> tuple[list[Claim], list[EvidenceResult], list[str]]:
    """走一遍真实链路：claims JSON → 解析；evidence JSON → 解析 + 抗伪造；再交叉校验。"""
    claims_out = parse_claims(json.dumps({"claims": [scenario["claim"]]}, ensure_ascii=False), DOC_ID)
    evidence_out = parse_evidence(scenario_id(scenario), DOC_ID)
    issues = claims_out.issues + evidence_out.issues
    issues += validate_against_claims(evidence_out.items, claims_out.items)
    return claims_out.items, evidence_out.items, issues


class TestFourStatusContract(unittest.TestCase):
    """Test1–Test4：四种 verification_status 的契约语义。"""

    def test_all_four_scenarios_are_covered(self):
        """规格完整性：四个场景必须恰好覆盖四种状态各一次。"""
        self.assertEqual(
            sorted(s["expected_status"] for s in REFERENCE_SCENARIOS),
            sorted(VERIFICATION_STATUSES),
        )

    def test_scenarios_produce_expected_status(self):
        """Test1 verified / Test2 unsupported / Test3 contradicted / Test4 inferred。"""
        for scenario in REFERENCE_SCENARIOS:
            with self.subTest(scenario=scenario["name"]):
                claims, results, issues = _pipeline(scenario)
                self.assertEqual(issues, [], f"不应有任何问题：{issues}")
                self.assertEqual(len(claims), 1)
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].verification_status, scenario["expected_status"])

    def test_verified_shows_evidence_and_source(self):
        """verified：报告须给出证据原文 + 来源 + 置信度。"""
        scenario = REFERENCE_SCENARIOS[0]
        _, results, _ = _pipeline(scenario)
        rendered = render_evidence_section(results[0])
        self.assertIn("verified", rendered)
        self.assertIn("本政策支持人工智能企业发展。", rendered)  # 证据原文
        self.assertIn("第一条", rendered)  # 来源章节
        self.assertIn("0.96", rendered)  # 置信度

    def test_unsupported_warns_no_direct_basis(self):
        """unsupported：必须显式提示「未在原文中找到直接依据」。"""
        _, results, _ = _pipeline(REFERENCE_SCENARIOS[1])
        rendered = render_evidence_section(results[0])
        self.assertIn("⚠️", rendered)
        self.assertIn("未在原始文档中找到足够的直接依据", rendered)

    def test_inferred_warns_it_is_inference(self):
        """inferred：必须显式提示「属于推断、原文没有直接陈述」。"""
        _, results, _ = _pipeline(REFERENCE_SCENARIOS[3])
        rendered = render_evidence_section(results[0])
        self.assertIn("⚠️", rendered)
        self.assertIn("原文没有直接陈述", rendered)

    def test_contradicted_says_opposite_exists(self):
        """contradicted：必须显式提示「原文存在相反信息」。"""
        _, results, _ = _pipeline(REFERENCE_SCENARIOS[2])
        rendered = render_evidence_section(results[0])
        self.assertIn("原文存在与该结论相反的信息", rendered)
        self.assertIn("本政策不适用于个人开发者。", rendered)  # 反向证据

    def test_warning_statuses_are_never_rendered_as_verified(self):
        """核心防线：unsupported / inferred 的报告片段里绝不能出现 'verified' 字样。"""
        for scenario in REFERENCE_SCENARIOS:
            if scenario["expected_status"] not in ("unsupported", "inferred"):
                continue
            with self.subTest(scenario=scenario["name"]):
                _, results, _ = _pipeline(scenario)
                self.assertNotIn("verified", render_evidence_section(results[0]))


class TestAntiFabrication(unittest.TestCase):
    """抗伪造铁律：宁可缺定位，也绝不伪造定位 / 绝不把推断说成已验证。"""

    def test_page_is_forced_to_null(self):
        """模型编造页码 → 强制置 null 并上报问题。"""
        payload = json.dumps(
            {
                "evidence_results": [
                    {
                        "claim_id": "C001",
                        "source_agent": "overview",
                        "claim": "该政策支持人工智能企业发展。",
                        "verification_status": "verified",
                        "evidence_text": "本政策支持人工智能企业发展。",
                        "source_location": {"page": 3, "section": "第二条", "source_locator": "本政策支持人工智能企业发展。"},
                        "confidence": 0.9,
                    }
                ]
            },
            ensure_ascii=False,
        )
        out = parse_evidence(payload, DOC_ID)
        self.assertTrue(out.issues, "编造页码必须被上报")
        self.assertTrue(any("page" in i for i in out.issues))
        self.assertIsNone(out.items[0].source_location.page)
        self.assertEqual(out.items[0].source_location.section, "第二条")  # 其余定位保留
        self.assertNotIn(3, [r.to_db_row()[4] for r in out.items])  # 入库 page 恒为 NULL

    def test_verified_without_any_evidence_is_downgraded(self):
        """声称 verified 却拿不出任何证据 → 降级 unsupported（绝不允许）。"""
        payload = json.dumps(
            {
                "evidence_results": [
                    {
                        "claim_id": "C001",
                        "source_agent": "insight",
                        "claim": "该政策可以降低企业研发成本。",
                        "verification_status": "verified",
                        "evidence_text": None,
                        "source_location": {"page": None, "section": None, "source_locator": None},
                        "confidence": 0.99,
                    }
                ]
            },
            ensure_ascii=False,
        )
        out = parse_evidence(payload, DOC_ID)
        self.assertEqual(out.items[0].verification_status, "unsupported")
        self.assertTrue(any("降级" in i for i in out.issues))

    def test_evidence_with_locator_only_still_verified(self):
        """有 source_locator（拿不到独立 evidence_text）仍算有证据，不降级。"""
        payload = json.dumps(
            {
                "evidence_results": [
                    {
                        "claim_id": "C001",
                        "source_agent": "overview",
                        "claim": "该政策支持人工智能企业发展。",
                        "verification_status": "verified",
                        "evidence_text": None,
                        "source_location": {"page": None, "section": None, "locator_typo": None, "source_locator": "本政策支持人工智能企业发展。"},
                        "confidence": 0.8,
                    }
                ]
            },
            ensure_ascii=False,
        )
        out = parse_evidence(payload, DOC_ID)
        self.assertEqual(out.items[0].verification_status, "verified")

    def test_invalid_status_is_dropped_not_guessed(self):
        """未知状态一律剔除并上报，绝不猜成某个有意义的状态。"""
        payload = json.dumps(
            {
                "evidence_results": [
                    {
                        "claim_id": "C001",
                        "source_agent": "overview",
                        "claim": "x",
                        "verification_status": "probably_true",
                        "evidence_text": "y",
                        "source_location": {"page": None},
                    }
                ]
            },
            ensure_ascii=False,
        )
        out = parse_evidence(payload, DOC_ID)
        self.assertEqual(out.items, [])
        self.assertTrue(any("probably_true" in i for i in out.issues))

    def test_confidence_out_of_range_is_nulled(self):
        payload = json.dumps(
            {
                "evidence_results": [
                    {
                        "claim_id": "C001",
                        "source_agent": "overview",
                        "claim": "x",
                        "verification_status": "unsupported",
                        "evidence_text": None,
                        "source_location": {"page": None},
                        "confidence": 1.7,
                    }
                ]
            },
            ensure_ascii=False,
        )
        out = parse_evidence(payload, DOC_ID)
        self.assertIsNone(out.items[0].confidence)
        self.assertTrue(any("超出" in i for i in out.issues))


class TestClaimLayer(unittest.TestCase):
    """Claim 层：编号、枚举、去重。"""

    def test_claim_id_format(self):
        self.assertEqual(format_claim_id(1), "C001")
        self.assertEqual(format_claim_id(42), "C042")

    def test_claim_type_enum_is_strict(self):
        payload = json.dumps(
            {"claims": [{"claim_id": "C001", "source_agent": "insight", "claim_text": "x", "claim_type": "guess"}]},
            ensure_ascii=False,
        )
        out = parse_claims(payload, DOC_ID)
        self.assertEqual(out.items, [])
        self.assertTrue(any("claim_type" in i for i in out.issues))

    def test_duplicate_claim_id_reported(self):
        payload = json.dumps(
            {
                "claims": [
                    {"claim_id": "C001", "source_agent": "overview", "claim_text": "a", "claim_type": "fact"},
                    {"claim_id": "C001", "source_agent": "insight", "claim_text": "b", "claim_type": "risk"},
                ]
            },
            ensure_ascii=False,
        )
        out = parse_claims(payload, DOC_ID)
        self.assertEqual(len(out.items), 1)
        self.assertTrue(any("重复" in i for i in out.issues))

    def test_claim_type_coverage_matches_spec(self):
        """claim_type 取值须与产品定义一致（fact/risk/action/summary/inference）。"""
        self.assertEqual(CLAIM_TYPES, ("fact", "risk", "action", "summary", "inference"))

    def test_source_agent_coverage(self):
        self.assertEqual(SOURCE_AGENTS, ("overview", "insight", "action", "coordinator"))


class TestCrossValidation(unittest.TestCase):
    """Evidence ↔ Claim 一致性。"""

    def test_missing_verification_is_reported(self):
        claims = [Claim(DOC_ID, "C001", "overview", "a", "fact"), Claim(DOC_ID, "C002", "insight", "b", "risk")]
        results = [EvidenceResult(DOC_ID, "C001", "overview", "a", "unsupported")]
        issues = validate_against_claims(results, claims)
        self.assertTrue(any("C002" in i and "漏检" in i for i in issues))

    def test_unknown_claim_id_is_reported(self):
        claims = [Claim(DOC_ID, "C001", "overview", "a", "fact")]
        results = [EvidenceResult(DOC_ID, "C999", "overview", "a", "unsupported")]
        issues = validate_against_claims(results, claims)
        self.assertTrue(any("C999" in i for i in issues))

    def test_source_agent_mismatch_is_reported(self):
        claims = [Claim(DOC_ID, "C001", "overview", "a", "fact")]
        results = [EvidenceResult(DOC_ID, "C001", "insight", "a", "unsupported")]
        issues = validate_against_claims(results, claims)
        self.assertTrue(any("source_agent 不一致" in i for i in issues))

    def test_consistent_pair_has_no_issues(self):
        claims = [Claim(DOC_ID, "C001", "overview", "a", "fact")]
        results = [EvidenceResult(DOC_ID, "C001", "overview", "a", "unsupported")]
        self.assertEqual(validate_against_claims(results, claims), [])


class TestRenderingAndPersistence(unittest.TestCase):
    """报告渲染与入库行结构。"""

    def test_summary_counts(self):
        results = [
            EvidenceResult(DOC_ID, "C001", "overview", "a", "verified", "e"),
            EvidenceResult(DOC_ID, "C002", "insight", "b", "unsupported"),
            EvidenceResult(DOC_ID, "C003", "action", "c", "inferred"),
            EvidenceResult(DOC_ID, "C004", "action", "d", "contradicted", "e2"),
        ]
        s = summarize(results)
        self.assertEqual((s["total"], s["verified"], s["unsupported"], s["inferred"], s["contradicted"]), (4, 1, 1, 1, 1))
        self.assertIn("共 4 条结论", render_verification_summary(results))

    def test_full_block_contains_every_section(self):
        results = [parse_evidence(scenario_id(s), DOC_ID).items[0] for s in REFERENCE_SCENARIOS]
        block = render_evidence_section_block(results)
        self.assertIn("Evidence Verification", block)
        self.assertEqual(block.count("#### 证据核验"), 4)

    def test_db_row_shapes_match_schema(self):
        claim = Claim(DOC_ID, "C001", "overview", "a", "fact")
        self.assertEqual(len(claim.to_db_row()), 5)
        ev = parse_evidence(scenario_id(REFERENCE_SCENARIOS[0]), DOC_ID).items[0]
        row = ev.to_db_row()
        self.assertEqual(len(row), 9)
        self.assertEqual(row[0], DOC_ID)
        self.assertEqual(row[1], "C001")
        self.assertEqual(row[2], "verified")
        self.assertEqual(row[4], None)  # page 恒为 NULL

    def test_source_location_render_when_empty(self):
        text = SourceLocation().render()
        self.assertIn("原文未提供可定位信息", text)

    def test_bare_json_without_code_fence_parses(self):
        """容错：模型没写代码块、直接吐裸 JSON 也能解析。"""
        bare = json.dumps({"evidence_results": [REFERENCE_SCENARIOS[0]["evidence"]]}, ensure_ascii=False)
        out = parse_evidence(bare, DOC_ID)
        self.assertEqual(out.issues, [])
        self.assertEqual(out.items[0].verification_status, "verified")

    def test_markdown_prose_around_json_is_ignored(self):
        text = "以下是核验结果：\n\n```json\n" + scenario_id(REFERENCE_SCENARIOS[3]) + "\n```\n\n完毕。"
        out = parse_evidence(text, DOC_ID)
        self.assertEqual(out.issues, [])
        self.assertEqual(out.items[0].verification_status, "inferred")

    def test_missing_payload_is_reported_not_raised(self):
        out = parse_evidence("这里没有任何 JSON。", DOC_ID)
        self.assertEqual(out.items, [])
        self.assertTrue(out.issues)


if __name__ == "__main__":
    unittest.main(verbosity=2)
