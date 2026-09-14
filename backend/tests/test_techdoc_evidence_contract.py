"""Thin integration shell for the TechDocAnalyzer V2 evidence contract.

The canonical contract module and its full unit-test suite ship with the product
deliverable under ``plans/techdoc-agent-general/evidence/`` — that directory is the
source of truth for the four ``verification_status`` semantics and the
anti-fabrication rules (``page`` is always ``null``; a claim may not be reported as
``verified`` without evidence).

This file deliberately holds **no** duplicated logic. It only proves that the
contract is reachable from the backend test runner and that the four reference
scenarios plus the anti-fabrication guards still hold end to end:

    parse claims -> parse evidence -> enforce integrity -> cross-validate -> render

See ``plans/techdoc-agent-general/evidence/evidence_contract.py`` for the module and
``plans/techdoc-agent-general/DEPLOYMENT_NOTES.md`` for how to run both suites.
"""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT / "plans" / "techdoc-agent-general" / "evidence" / "evidence_contract.py"
_AVAILABLE = _MODULE_PATH.is_file()

if _AVAILABLE:
    import sys

    _spec = importlib.util.spec_from_file_location("techdoc_evidence_contract", _MODULE_PATH)
    assert _spec and _spec.loader, f"cannot load evidence contract from {_MODULE_PATH}"
    ec = importlib.util.module_from_spec(_spec)
    # Register before exec: `@dataclass` resolves `cls.__module__` through sys.modules,
    # which is None until the module is registered.
    sys.modules[_spec.name] = ec
    _spec.loader.exec_module(ec)
else:
    ec = None

DOC_ID = "backend-shell-doc-0001"
_SKIP_REASON = f"product deliverable not present in this checkout: {_MODULE_PATH}"


@unittest.skipUnless(_AVAILABLE, _SKIP_REASON)
class TestEvidenceContractReachable(unittest.TestCase):
    """The deliverable contract is importable from the backend test runner."""

    def test_status_enum_is_exactly_the_four_product_states(self):
        self.assertEqual(ec.VERIFICATION_STATUSES, ("verified", "contradicted", "unsupported", "inferred"))

    def test_reference_scenarios_cover_all_four_states(self):
        self.assertEqual(
            sorted(s["expected_status"] for s in ec.REFERENCE_SCENARIOS),
            sorted(ec.VERIFICATION_STATUSES),
        )


@unittest.skipUnless(_AVAILABLE, _SKIP_REASON)
class TestEvidencePipelineEndToEnd(unittest.TestCase):
    """Full contract pipeline over the four reference scenarios."""

    def test_each_scenario_yields_its_expected_status(self):
        for scenario in ec.REFERENCE_SCENARIOS:
            with self.subTest(scenario=scenario["name"]):
                claims = ec.parse_claims(json.dumps({"claims": [scenario["claim"]]}, ensure_ascii=False), DOC_ID)
                evidence = ec.parse_evidence(ec.scenario_id(scenario), DOC_ID)
                self.assertEqual(claims.issues, [])
                self.assertEqual(evidence.issues, [])
                self.assertEqual(ec.validate_against_claims(evidence.items, claims.items), [])
                self.assertEqual(evidence.items[0].verification_status, scenario["expected_status"])

    def test_rendered_report_never_promotes_a_warning_status_to_verified(self):
        for scenario in ec.REFERENCE_SCENARIOS:
            if scenario["expected_status"] not in ("unsupported", "inferred"):
                continue
            with self.subTest(scenario=scenario["name"]):
                result = ec.parse_evidence(ec.scenario_id(scenario), DOC_ID).items[0]
                rendered = ec.render_evidence_section(result)
                self.assertIn("⚠️", rendered)
                self.assertNotIn("verified", rendered)

    def test_fabricated_page_number_is_rejected(self):
        payload = json.dumps(
            {
                "evidence_results": [
                    {
                        "claim_id": "C001",
                        "source_agent": "overview",
                        "claim": "该政策支持人工智能企业发展。",
                        "verification_status": "verified",
                        "evidence_text": "本政策支持人工智能企业发展。",
                        "source_location": {"page": 3, "source_locator": "本政策支持人工智能企业发展。"},
                        "confidence": 0.9,
                    }
                ]
            },
            ensure_ascii=False,
        )
        outcome = ec.parse_evidence(payload, DOC_ID)
        self.assertIsNone(outcome.items[0].source_location.page)
        self.assertTrue(any("page" in issue for issue in outcome.issues))
        self.assertEqual(outcome.items[0].to_db_row()[4], None)  # the persisted page column stays NULL

    def test_verified_without_evidence_is_downgraded(self):
        payload = json.dumps(
            {
                "evidence_results": [
                    {
                        "claim_id": "C001",
                        "source_agent": "insight",
                        "claim": "该政策可以降低企业研发成本。",
                        "verification_status": "verified",
                        "source_location": {"page": None},
                        "confidence": 0.99,
                    }
                ]
            },
            ensure_ascii=False,
        )
        outcome = ec.parse_evidence(payload, DOC_ID)
        self.assertEqual(outcome.items[0].verification_status, "unsupported")


if __name__ == "__main__":
    unittest.main(verbosity=2)
