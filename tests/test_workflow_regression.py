"""Offline regression checks for the deterministic dispute workflow."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from src.dispute_resolution import (
    DisputeWorkflow,
    ModelAuditClient,
    OlistRepository,
    PolicyDefinition,
    RuntimeConfig,
)
from src.dispute_resolution.workflow import TraceWriter


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def disabled_audit_config() -> RuntimeConfig:
    return RuntimeConfig(
        model_provider="openrouter",
        model_name="qwen/qwen3-8b",
        model_parameter_size_b=8.2,
        model_endpoint="https://openrouter.ai/api/v1",
        model_api_key=None,
        model_site_url=None,
        model_app_name="workflow-regression-test",
        model_timeout_seconds=1,
        model_audit_enabled=False,
        model_proposal_enabled=False,
        model_output_mode="deterministic",
        model_audit_scope="final_only",
        strict_model_audit=False,
        max_workers=3,
        runtime="python",
    )


class WorkflowRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = PolicyDefinition.from_file(PROJECT_ROOT / "policy" / "EC_POLICY_V1.json")
        cls.repository = OlistRepository(PROJECT_ROOT / "data")
        cls.repository.load()
        cls.config = disabled_audit_config()

    def create_workflow(
        self, trace_path: Path, config: RuntimeConfig | None = None
    ) -> DisputeWorkflow:
        active_config = config or self.config
        return DisputeWorkflow(
            self.repository,
            TraceWriter(trace_path),
            ModelAuditClient(active_config),
            self.policy,
            max_workers=active_config.max_workers,
        )

    def test_policy_covers_all_readme_rules_in_priority_order(self) -> None:
        self.assertEqual(
            [rule.primary_issue for rule in self.policy.rules],
            [
                "canceled_order_paid",
                "unavailable_order_paid",
                "late_delivery_seller",
                "late_delivery_logistics",
                "valid_split_payment",
                "unsupported_late_claim",
            ],
        )
        self.assertEqual(self.policy.reconciliation_tolerance_brl, Decimal("0.10"))
        self.assertEqual(self.policy.limit("evidence_ids"), 10)

    def test_all_official_cases_are_deterministic_and_calibrated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            trace_path = Path(temporary_directory) / "trace.jsonl"
            workflow = self.create_workflow(trace_path)
            confidence_by_issue: Counter[tuple[str, float]] = Counter()
            try:
                for input_path in sorted((PROJECT_ROOT / "input").glob("EC_*.json")):
                    case = json.loads(input_path.read_text(encoding="utf-8"))
                    output = workflow.process(case)
                    confidence_by_issue[(
                        output["assessment"]["primary_issue"],
                        output["assessment"]["confidence"],
                    )] += 1
            finally:
                workflow.trace.close()

        self.assertEqual(sum(confidence_by_issue.values()), 50)
        self.assertEqual(
            confidence_by_issue,
            Counter(
                {
                    ("canceled_order_paid", 0.95): 8,
                    ("unavailable_order_paid", 0.95): 8,
                    ("late_delivery_seller", 0.97): 8,
                    ("late_delivery_logistics", 0.97): 8,
                    ("valid_split_payment", 0.98): 9,
                    ("unsupported_late_claim", 0.98): 9,
                }
            ),
        )

    def test_verifier_does_not_repeat_policy_model_audit(self) -> None:
        case = json.loads((PROJECT_ROOT / "input" / "EC_001.json").read_text(encoding="utf-8"))
        audit_config = replace(
            self.config, model_audit_enabled=True, model_audit_scope="per_agent"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            trace_path = Path(temporary_directory) / "trace.jsonl"
            workflow = self.create_workflow(trace_path, audit_config)
            try:
                workflow.process(case)
            finally:
                workflow.trace.close()
            events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]

        policy_audits = [
            event
            for event in events
            if event["agent"] == "policy" and event["event"] == "model_audit_completed"
        ]
        self.assertEqual(len(policy_audits), 1)

    def test_final_only_audits_verifier_once(self) -> None:
        case = json.loads((PROJECT_ROOT / "input" / "EC_001.json").read_text(encoding="utf-8"))
        audit_config = replace(self.config, model_audit_enabled=True)
        with tempfile.TemporaryDirectory() as temporary_directory:
            trace_path = Path(temporary_directory) / "trace.jsonl"
            workflow = self.create_workflow(trace_path, audit_config)
            try:
                workflow.process(case)
            finally:
                workflow.trace.close()
            events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]

        audit_agents = [event["agent"] for event in events if event["event"] == "model_audit_completed"]
        self.assertEqual(audit_agents, ["verifier"])

    def test_policy_proposal_is_traced_without_changing_deterministic_output(self) -> None:
        case = json.loads((PROJECT_ROOT / "input" / "EC_001.json").read_text(encoding="utf-8"))
        proposal_config = replace(
            self.config, model_audit_enabled=True, model_proposal_enabled=True
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            trace_path = Path(temporary_directory) / "trace.jsonl"
            workflow = self.create_workflow(trace_path, proposal_config)
            try:
                output = workflow.process(case)
            finally:
                workflow.trace.close()
            events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]

        proposal_events = [event for event in events if event["event"] == "model_proposal_completed"]
        self.assertEqual(len(proposal_events), 1)
        self.assertEqual(proposal_events[0]["agent"], "policy")
        self.assertEqual(output["assessment"]["primary_issue"], "late_delivery_seller")

    def test_valid_model_proposal_can_change_decision_fields(self) -> None:
        case = json.loads((PROJECT_ROOT / "input" / "EC_001.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary_directory:
            trace_path = Path(temporary_directory) / "trace.jsonl"
            workflow = self.create_workflow(trace_path)
            try:
                order_id = case["customer_request"]["claimed_order_id"]
                state = {
                    "order_seller_facts": workflow.order_seller_agent.investigate(case["case_id"], order_id),
                    "payment_facts": workflow.payment_agent.investigate(case["case_id"], order_id),
                    "delivery_facts": workflow.delivery_agent.investigate(case["case_id"], order_id),
                }
                deterministic = workflow.policy_agent.evaluate(case["case_id"], state)
                proposal = {
                    "status": "ok",
                    "primary_issue": "late_delivery_logistics",
                    "cause_code": "CARRIER_DELIVERED_AFTER_ESTIMATE",
                    "refund_basis": "freight_total",
                    "recommended_refund_brl": deterministic["financial_resolution"]["freight_total_brl"],
                    "case_status": "action_required",
                    "action": "refund_freight",
                    "responsible_parties": [
                        {"party_type": "logistics_provider", "party_id": "LOGISTICS_PROVIDER"}
                    ],
                    "confidence": 0.81,
                    "reason": "test proposal",
                }
                overridden, reason = workflow.policy_agent._apply_model_proposal(
                    deterministic, proposal, state
                )
            finally:
                workflow.trace.close()

        self.assertEqual(reason, "")
        self.assertIsNotNone(overridden)
        assert overridden is not None
        self.assertEqual(overridden["assessment"]["primary_issue"], "late_delivery_logistics")
        self.assertEqual(overridden["assessment"]["confidence"], 0.81)
        self.assertEqual(
            overridden["evidence_ids"][-1], "policy:CARRIER_DELIVERED_AFTER_ESTIMATE"
        )

    def test_entity_and_evidence_ids_use_ascending_sequence_order(self) -> None:
        case = json.loads((PROJECT_ROOT / "input" / "EC_004.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary_directory:
            trace_path = Path(temporary_directory) / "trace.jsonl"
            workflow = self.create_workflow(trace_path)
            try:
                output = workflow.process(case)
            finally:
                workflow.trace.close()

        payment_ids = output["affected_entities"]["payment_ids"]
        payment_sequences = [int(payment_id.rsplit(":", 1)[1]) for payment_id in payment_ids]
        evidence_payment_sequences = [
            int(evidence_id.rsplit(":", 1)[1])
            for evidence_id in output["evidence_ids"]
            if evidence_id.startswith("payment:")
        ]
        self.assertEqual(payment_sequences, sorted(payment_sequences))
        self.assertEqual(evidence_payment_sequences, sorted(evidence_payment_sequences))


if __name__ == "__main__":
    unittest.main()
