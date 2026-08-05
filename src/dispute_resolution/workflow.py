"""Supervisor, specialist agents, policy engine, and verification gate."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from .model_audit import ModelAuditClient
from .repository import OlistRepository


MONEY_PLACES = Decimal("0.01")
RECONCILIATION_TOLERANCE = Decimal("0.10")
POLICY_VERSION = "EC_POLICY_V1"

ISSUE_RULES = {
    "canceled_order_paid": {
        "cause": "ORDER_CANCELED_AFTER_PAYMENT",
        "action": "issue_full_refund",
        "party_type": "platform",
        "party_id": "OLIST_PLATFORM",
    },
    "unavailable_order_paid": {
        "cause": "ORDER_UNAVAILABLE_AFTER_PAYMENT",
        "action": "issue_full_refund",
        "party_type": "platform",
        "party_id": "OLIST_PLATFORM",
    },
    "late_delivery_seller": {
        "cause": "SELLER_HANDOFF_AFTER_LIMIT",
        "action": "refund_freight",
        "party_type": "seller",
    },
    "late_delivery_logistics": {
        "cause": "CARRIER_DELIVERED_AFTER_ESTIMATE",
        "action": "refund_freight",
        "party_type": "logistics_provider",
        "party_id": "LOGISTICS_PROVIDER",
    },
    "valid_split_payment": {
        "cause": "MULTIPLE_PAYMENTS_RECONCILED",
        "action": "explain_valid_split_payment",
    },
    "unsupported_late_claim": {
        "cause": "DELIVERY_WITHIN_ESTIMATE",
        "action": "reject_late_refund",
    },
}


class CaseError(ValueError):
    """A case cannot safely proceed to output generation."""


def money(value: object) -> Decimal:
    try:
        return Decimal(str(value)).quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as error:
        raise CaseError(f"Invalid monetary value: {value!r}") from error


def amount_to_float(value: Decimal) -> float:
    return float(value.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP))


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise CaseError(f"Invalid CSV timestamp: {value!r}") from error


def unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def limited(values: list[str], maximum: int = 5) -> list[str]:
    return unique(values)[:maximum]


class TraceWriter:
    """JSONL audit trace for the newest workflow run."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("w", encoding="utf-8", newline="\n")
        self._lock = Lock()

    def event(self, case_id: str, agent: str, event: str, **details: object) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "case_id": case_id,
            "agent": agent,
            "event": event,
            "details": details,
        }
        with self._lock:
            self._file.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            self._file.flush()

    def close(self) -> None:
        self._file.close()


def record_model_audit(
    trace: TraceWriter,
    audit_client: ModelAuditClient,
    case_id: str,
    agent_name: str,
    summary: dict[str, Any],
) -> None:
    """Log a model review without allowing it to replace deterministic facts."""
    outcome = audit_client.review(agent_name, summary)
    trace.event(case_id, agent_name, "model_audit_completed", **outcome)
    if audit_client.strict and outcome["status"] != "ok":
        raise CaseError(f"{agent_name} model audit failed: {outcome['reason']}")


class OrderSellerAgent:
    name = "order_seller"

    def __init__(
        self, repository: OlistRepository, trace: TraceWriter, audit_client: ModelAuditClient
    ) -> None:
        self.repository = repository
        self.trace = trace
        self.audit_client = audit_client

    def investigate(self, case_id: str, order_id: str) -> dict[str, Any]:
        self.trace.event(case_id, self.name, "started", order_id=order_id)
        order = self.repository.order(order_id)
        if order is None:
            raise CaseError(f"Order does not exist in Olist data: {order_id}")

        items = self.repository.items(order_id)
        item_total = sum((money(row["price"]) for row in items), Decimal("0.00"))
        freight_total = sum((money(row["freight_value"]) for row in items), Decimal("0.00"))
        seller_ids = unique([row["seller_id"] for row in items])
        missing_sellers = [seller_id for seller_id in seller_ids if not self.repository.has_seller(seller_id)]
        if missing_sellers:
            raise CaseError(f"Seller IDs absent from seller dataset: {missing_sellers}")

        result = {
            "order": order,
            "items": items,
            "seller_ids": seller_ids,
            "item_total_brl": amount_to_float(item_total),
            "freight_total_brl": amount_to_float(freight_total),
        }
        record_model_audit(
            self.trace,
            self.audit_client,
            case_id,
            self.name,
            {
                "order_id": order_id,
                "order_status": order["order_status"],
                "item_count": len(items),
                "seller_count": len(seller_ids),
                "item_total_brl": result["item_total_brl"],
                "freight_total_brl": result["freight_total_brl"],
            },
        )
        self.trace.event(
            case_id,
            self.name,
            "handoff_completed",
            item_count=len(items),
            seller_count=len(seller_ids),
            item_total_brl=result["item_total_brl"],
            freight_total_brl=result["freight_total_brl"],
        )
        return result


class PaymentAgent:
    name = "payment"

    def __init__(
        self, repository: OlistRepository, trace: TraceWriter, audit_client: ModelAuditClient
    ) -> None:
        self.repository = repository
        self.trace = trace
        self.audit_client = audit_client

    def investigate(self, case_id: str, order_id: str) -> dict[str, Any]:
        self.trace.event(case_id, self.name, "started", order_id=order_id)
        payments = self.repository.payments(order_id)
        payment_total = sum((money(row["payment_value"]) for row in payments), Decimal("0.00"))
        result = {
            "payments": payments,
            "payment_count": len(payments),
            "payment_total_brl": amount_to_float(payment_total),
        }
        record_model_audit(
            self.trace,
            self.audit_client,
            case_id,
            self.name,
            {
                "order_id": order_id,
                "payment_count": result["payment_count"],
                "payment_total_brl": result["payment_total_brl"],
            },
        )
        self.trace.event(
            case_id,
            self.name,
            "handoff_completed",
            payment_count=result["payment_count"],
            payment_total_brl=result["payment_total_brl"],
        )
        return result


class DeliveryAgent:
    name = "delivery"

    def __init__(
        self, repository: OlistRepository, trace: TraceWriter, audit_client: ModelAuditClient
    ) -> None:
        self.repository = repository
        self.trace = trace
        self.audit_client = audit_client

    def investigate(self, case_id: str, order_id: str) -> dict[str, Any]:
        self.trace.event(case_id, self.name, "started", order_id=order_id)
        order = self.repository.order(order_id)
        if order is None:
            raise CaseError(f"Order does not exist in Olist data: {order_id}")
        items = self.repository.items(order_id)

        delivered_at = parse_timestamp(order["order_delivered_customer_date"])
        estimated_at = parse_timestamp(order["order_estimated_delivery_date"])
        carrier_at = parse_timestamp(order["order_delivered_carrier_date"])
        delivered_after_estimate = bool(delivered_at and estimated_at and delivered_at > estimated_at)
        delivered_within_estimate = bool(delivered_at and estimated_at and delivered_at <= estimated_at)

        violating_seller_ids: list[str] = []
        shipping_limits: list[datetime] = []
        for item in items:
            shipping_limit = parse_timestamp(item["shipping_limit_date"])
            if shipping_limit is not None:
                shipping_limits.append(shipping_limit)
                if carrier_at and carrier_at > shipping_limit:
                    violating_seller_ids.append(item["seller_id"])

        carrier_within_all_limits = bool(
            carrier_at and shipping_limits and all(carrier_at <= limit for limit in shipping_limits)
        )
        result = {
            "delivered_after_estimate": delivered_after_estimate,
            "delivered_within_estimate": delivered_within_estimate,
            "carrier_within_all_shipping_limits": carrier_within_all_limits,
            "violating_seller_ids": unique(violating_seller_ids),
            "delivered_at": order["order_delivered_customer_date"],
            "estimated_at": order["order_estimated_delivery_date"],
            "carrier_at": order["order_delivered_carrier_date"],
        }
        record_model_audit(
            self.trace,
            self.audit_client,
            case_id,
            self.name,
            {
                "order_id": order_id,
                "delivered_after_estimate": delivered_after_estimate,
                "delivered_within_estimate": delivered_within_estimate,
                "violating_seller_count": len(result["violating_seller_ids"]),
            },
        )
        self.trace.event(
            case_id,
            self.name,
            "handoff_completed",
            delivered_after_estimate=delivered_after_estimate,
            violating_sellers=result["violating_seller_ids"],
        )
        return result


class PolicyAgent:
    name = "policy"

    def __init__(self, trace: TraceWriter, audit_client: ModelAuditClient) -> None:
        self.trace = trace
        self.audit_client = audit_client

    def decide(self, case_id: str, state: dict[str, Any]) -> dict[str, Any]:
        self.trace.event(case_id, self.name, "started", policy_version=POLICY_VERSION)
        order_facts = state["order_seller_facts"]
        payment_facts = state["payment_facts"]
        delivery_facts = state["delivery_facts"]
        order = order_facts["order"]

        item_total = money(order_facts["item_total_brl"])
        freight_total = money(order_facts["freight_total_brl"])
        payment_total = money(payment_facts["payment_total_brl"])
        reconciliation_delta = abs(payment_total - (item_total + freight_total))
        reconciled = reconciliation_delta <= RECONCILIATION_TOLERANCE

        issue, responsible_seller_ids = self._select_issue(
            order_status=order["order_status"],
            payment_total=payment_total,
            payment_count=payment_facts["payment_count"],
            reconciled=reconciled,
            delivery_facts=delivery_facts,
        )
        output = self._build_output(
            case_id=case_id,
            order_id=order["order_id"],
            issue=issue,
            responsible_seller_ids=responsible_seller_ids,
            items=order_facts["items"],
            seller_ids=order_facts["seller_ids"],
            payments=payment_facts["payments"],
            item_total=item_total,
            freight_total=freight_total,
            payment_total=payment_total,
        )
        record_model_audit(
            self.trace,
            self.audit_client,
            case_id,
            self.name,
            {
                "order_status": order["order_status"],
                "primary_issue": issue,
                "reconciliation_delta_brl": amount_to_float(reconciliation_delta),
                "recommended_refund_brl": output["financial_resolution"]["recommended_refund_brl"],
            },
        )
        self.trace.event(
            case_id,
            self.name,
            "handoff_completed",
            primary_issue=issue,
            reconciliation_delta_brl=amount_to_float(reconciliation_delta),
        )
        return output

    def _select_issue(
        self,
        *,
        order_status: str,
        payment_total: Decimal,
        payment_count: int,
        reconciled: bool,
        delivery_facts: dict[str, Any],
    ) -> tuple[str, list[str]]:
        if order_status == "canceled" and payment_total > 0:
            return "canceled_order_paid", []
        if order_status == "unavailable" and payment_total > 0:
            return "unavailable_order_paid", []
        if delivery_facts["delivered_after_estimate"] and delivery_facts["violating_seller_ids"]:
            return "late_delivery_seller", delivery_facts["violating_seller_ids"]
        if delivery_facts["delivered_after_estimate"] and delivery_facts["carrier_within_all_shipping_limits"]:
            return "late_delivery_logistics", []
        if payment_count >= 2 and reconciled:
            return "valid_split_payment", []
        if delivery_facts["delivered_within_estimate"] and reconciled:
            return "unsupported_late_claim", []
        raise CaseError("No EC_POLICY_V1 rule matches the available facts.")

    def _build_output(
        self,
        *,
        case_id: str,
        order_id: str,
        issue: str,
        responsible_seller_ids: list[str],
        items: list[dict[str, str]],
        seller_ids: list[str],
        payments: list[dict[str, str]],
        item_total: Decimal,
        freight_total: Decimal,
        payment_total: Decimal,
    ) -> dict[str, Any]:
        rule = ISSUE_RULES[issue]
        if issue in {"canceled_order_paid", "unavailable_order_paid"}:
            refund = payment_total
        elif issue in {"late_delivery_seller", "late_delivery_logistics"}:
            refund = freight_total
        else:
            refund = Decimal("0.00")

        item_ids = limited([f"{order_id}:{item['order_item_id']}" for item in items])
        output_seller_ids = limited(seller_ids)
        payment_ids = limited([f"{order_id}:{payment['payment_sequential']}" for payment in payments])
        responsible_parties: list[dict[str, str]] = []
        if rule.get("party_type") == "seller":
            responsible_parties = [
                {"party_type": "seller", "party_id": seller_id}
                for seller_id in limited(responsible_seller_ids, 3)
            ]
        elif "party_id" in rule:
            responsible_parties = [{"party_type": rule["party_type"], "party_id": rule["party_id"]}]

        cause = rule["cause"]
        # Preserve the order and selected policy evidence even for large orders.
        evidence_candidates = [f"order:{order_id}", f"policy:{cause}"]
        evidence_candidates.extend(f"seller:{seller_id}" for seller_id in responsible_seller_ids)
        evidence_candidates.extend(f"item:{item_id}" for item_id in item_ids)
        evidence_candidates.extend(f"payment:{payment_id}" for payment_id in payment_ids)
        evidence_candidates.extend(f"seller:{seller_id}" for seller_id in output_seller_ids)

        return {
            "case_id": case_id,
            "assessment": {
                "primary_issue": issue,
                "case_status": "action_required" if refund > 0 else "no_action",
                "confidence": 0.95,
            },
            "affected_entities": {
                "order_ids": [order_id],
                "item_ids": item_ids,
                "seller_ids": output_seller_ids,
                "payment_ids": payment_ids,
            },
            "root_cause_analysis": {
                "ranked_causes": [{"cause_code": cause, "rank": 1}],
                "responsible_parties": responsible_parties,
            },
            "evidence_ids": limited(evidence_candidates, 10),
            "financial_resolution": {
                "currency": "BRL",
                "item_total_brl": amount_to_float(item_total),
                "freight_total_brl": amount_to_float(freight_total),
                "payment_total_brl": amount_to_float(payment_total),
                "recommended_refund_brl": amount_to_float(refund),
            },
            "resolution_actions": [rule["action"]],
        }


class VerifierAgent:
    name = "verifier"

    def __init__(
        self, trace: TraceWriter, policy: PolicyAgent, audit_client: ModelAuditClient
    ) -> None:
        self.trace = trace
        self.policy = policy
        self.audit_client = audit_client

    def validate(self, case_id: str, state: dict[str, Any], output: dict[str, Any]) -> list[str]:
        self.trace.event(case_id, self.name, "started")
        errors: list[str] = []
        required_keys = {
            "case_id", "assessment", "affected_entities", "root_cause_analysis",
            "evidence_ids", "financial_resolution", "resolution_actions",
        }
        if set(output) != required_keys:
            errors.append("Output top-level keys do not match the required schema.")
        if output.get("case_id") != case_id:
            errors.append("Output case_id does not match input case_id.")

        self._validate_limits(output, errors)
        self._validate_evidence(state, output, errors)
        self._validate_amounts(state, output, errors)
        self._validate_policy(case_id, state, output, errors)

        record_model_audit(
            self.trace,
            self.audit_client,
            case_id,
            self.name,
            {
                "primary_issue": output.get("assessment", {}).get("primary_issue"),
                "evidence_count": len(output.get("evidence_ids", [])),
                "validation_error_count": len(errors),
            },
        )
        self.trace.event(case_id, self.name, "validation_completed", valid=not errors, errors=errors)
        return errors

    @staticmethod
    def _validate_limits(output: dict[str, Any], errors: list[str]) -> None:
        entities = output.get("affected_entities", {})
        for field in ("order_ids", "item_ids", "seller_ids", "payment_ids"):
            values = entities.get(field)
            if not isinstance(values, list) or len(values) > 5 or len(values) != len(set(values)):
                errors.append(f"affected_entities.{field} is invalid or exceeds five IDs.")
        evidence = output.get("evidence_ids")
        if not isinstance(evidence, list) or len(evidence) > 10 or len(evidence) != len(set(evidence)):
            errors.append("evidence_ids is invalid, duplicated, or exceeds ten IDs.")
        analysis = output.get("root_cause_analysis", {})
        if len(analysis.get("ranked_causes", [])) > 3 or len(analysis.get("responsible_parties", [])) > 3:
            errors.append("Root-cause lists exceed schema limits.")
        actions = output.get("resolution_actions")
        if not isinstance(actions, list) or len(actions) > 5:
            errors.append("resolution_actions is invalid or exceeds five actions.")
        confidence = output.get("assessment", {}).get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            errors.append("assessment.confidence must be between 0 and 1.")

    @staticmethod
    def _validate_evidence(state: dict[str, Any], output: dict[str, Any], errors: list[str]) -> None:
        order_facts = state["order_seller_facts"]
        payment_facts = state["payment_facts"]
        order_id = order_facts["order"]["order_id"]
        allowed = {f"order:{order_id}"}
        allowed.update(f"item:{order_id}:{item['order_item_id']}" for item in order_facts["items"])
        allowed.update(f"payment:{order_id}:{payment['payment_sequential']}" for payment in payment_facts["payments"])
        allowed.update(f"seller:{seller_id}" for seller_id in order_facts["seller_ids"])
        allowed.update(f"policy:{rule['cause']}" for rule in ISSUE_RULES.values())
        invalid = set(output.get("evidence_ids", [])) - allowed
        if invalid:
            errors.append(f"Evidence IDs are unknown or malformed: {sorted(invalid)}")

    @staticmethod
    def _validate_amounts(state: dict[str, Any], output: dict[str, Any], errors: list[str]) -> None:
        order_facts = state["order_seller_facts"]
        payment_facts = state["payment_facts"]
        financial = output.get("financial_resolution", {})
        expected = {
            "item_total_brl": money(order_facts["item_total_brl"]),
            "freight_total_brl": money(order_facts["freight_total_brl"]),
            "payment_total_brl": money(payment_facts["payment_total_brl"]),
        }
        if financial.get("currency") != "BRL":
            errors.append("financial_resolution.currency must be BRL.")
        for field, expected_value in expected.items():
            try:
                actual = money(financial.get(field))
            except CaseError:
                errors.append(f"financial_resolution.{field} is invalid.")
                continue
            if actual != expected_value:
                errors.append(f"financial_resolution.{field} does not match CSV facts.")

    def _validate_policy(
        self,
        case_id: str,
        state: dict[str, Any],
        output: dict[str, Any],
        errors: list[str],
    ) -> None:
        expected = self.policy.decide(case_id, state)
        important_paths = (
            ("assessment",),
            ("root_cause_analysis",),
            ("financial_resolution", "recommended_refund_brl"),
            ("resolution_actions",),
        )
        for path in important_paths:
            actual_value: Any = output
            expected_value: Any = expected
            for key in path:
                actual_value = actual_value.get(key) if isinstance(actual_value, dict) else None
                expected_value = expected_value.get(key) if isinstance(expected_value, dict) else None
            if actual_value != expected_value:
                errors.append(f"Policy outcome mismatch at {'.'.join(path)}.")


class DisputeWorkflow:
    """Coordinator that owns state transitions and the final quality gate."""

    def __init__(
        self,
        repository: OlistRepository,
        trace: TraceWriter,
        audit_client: ModelAuditClient,
        max_workers: int = 3,
    ) -> None:
        self.repository = repository
        self.trace = trace
        self.audit_client = audit_client
        self.max_workers = max_workers
        self.order_seller_agent = OrderSellerAgent(repository, trace, audit_client)
        self.payment_agent = PaymentAgent(repository, trace, audit_client)
        self.delivery_agent = DeliveryAgent(repository, trace, audit_client)
        self.policy_agent = PolicyAgent(trace, audit_client)
        self.verifier_agent = VerifierAgent(trace, self.policy_agent, audit_client)

    def process(self, input_case: dict[str, Any]) -> dict[str, Any]:
        case_id, order_id = self._validate_input(input_case)
        self.trace.event(case_id, "coordinator", "case_started", claimed_order_id=order_id)
        record_model_audit(
            self.trace,
            self.audit_client,
            case_id,
            "coordinator",
            {"claimed_order_id": order_id, "policy_version": input_case["policy_version"]},
        )
        state: dict[str, Any] = {"input_case": input_case, "validation_errors": []}

        specialist_jobs: dict[str, Callable[[], dict[str, Any]]] = {
            "order_seller_facts": lambda: self.order_seller_agent.investigate(case_id, order_id),
            "payment_facts": lambda: self.payment_agent.investigate(case_id, order_id),
            "delivery_facts": lambda: self.delivery_agent.investigate(case_id, order_id),
        }
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {name: executor.submit(job) for name, job in specialist_jobs.items()}
            for name, future in futures.items():
                state[name] = future.result()
                self.trace.event(case_id, "coordinator", "handoff_received", handoff=name)

        output = self.policy_agent.decide(case_id, state)
        errors = self.verifier_agent.validate(case_id, state, output)
        if errors:
            state["validation_errors"] = errors
            self.trace.event(case_id, "coordinator", "retry_requested", errors=errors)
            self._retry_specialists_once(case_id, order_id, state, errors)
            output = self.policy_agent.decide(case_id, state)
            errors = self.verifier_agent.validate(case_id, state, output)
        if errors:
            raise CaseError(f"Verifier rejected case {case_id}: {'; '.join(errors)}")

        state["final_output"] = output
        self.trace.event(
            case_id,
            "coordinator",
            "case_completed",
            primary_issue=output["assessment"]["primary_issue"],
        )
        return output

    def _retry_specialists_once(
        self,
        case_id: str,
        order_id: str,
        state: dict[str, Any],
        errors: list[str],
    ) -> None:
        # Facts are immutable in the repository; a retry only refreshes the relevant handoff.
        error_text = " ".join(errors).lower()
        if "payment" in error_text or "financial" in error_text:
            state["payment_facts"] = self.payment_agent.investigate(case_id, order_id)
        if "evidence" in error_text or "seller" in error_text or "item" in error_text:
            state["order_seller_facts"] = self.order_seller_agent.investigate(case_id, order_id)
        if "policy" in error_text or "delivery" in error_text:
            state["delivery_facts"] = self.delivery_agent.investigate(case_id, order_id)

    @staticmethod
    def _validate_input(input_case: dict[str, Any]) -> tuple[str, str]:
        case_id = input_case.get("case_id")
        customer_request = input_case.get("customer_request")
        policy_version = input_case.get("policy_version")
        if not isinstance(case_id, str) or not case_id.startswith("EC_"):
            raise CaseError("input.case_id must be an EC_* string.")
        if not isinstance(customer_request, dict):
            raise CaseError("input.customer_request must be an object.")
        order_id = customer_request.get("claimed_order_id")
        if not isinstance(order_id, str) or not order_id:
            raise CaseError("input.customer_request.claimed_order_id is required.")
        if policy_version != POLICY_VERSION:
            raise CaseError(f"Only {POLICY_VERSION} is supported, got {policy_version!r}.")
        return case_id, order_id
