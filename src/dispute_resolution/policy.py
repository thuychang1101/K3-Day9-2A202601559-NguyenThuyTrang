"""Versioned policy definitions used by the deterministic policy engine."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
from typing import Any


REQUIRED_LIMITS = {
    "entity_ids",
    "evidence_ids",
    "ranked_causes",
    "responsible_parties",
    "resolution_actions",
}
MATCH_KEYS = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "unsupported_late_claim",
}
REFUND_BASES = {"payment_total", "freight_total", "none"}
CONFIDENCE_LEVELS = {"default", "exact", "within_tolerance"}
EVIDENCE_SOURCES = {"order", "item", "seller", "payment", "policy"}


@dataclass(frozen=True)
class PolicyRule:
    primary_issue: str
    match_key: str
    cause_code: str
    action: str
    refund_basis: str
    evidence_sources: tuple[str, ...]
    confidence_profile: str
    responsible_party: dict[str, str] | None


@dataclass(frozen=True)
class PolicyDefinition:
    policy_version: str
    currency: str
    reconciliation_tolerance_brl: Decimal
    output_limits: dict[str, int]
    confidence_profiles: dict[str, dict[str, float]]
    rules: tuple[PolicyRule, ...]

    @classmethod
    def from_file(cls, path: Path) -> "PolicyDefinition":
        if not path.exists():
            raise FileNotFoundError(f"Policy definition does not exist: {path}")
        with path.open("r", encoding="utf-8") as file:
            raw = json.load(file)
        if not isinstance(raw, dict):
            raise ValueError("Policy definition must be a JSON object.")

        policy_version = raw.get("policy_version")
        currency = raw.get("currency")
        if not isinstance(policy_version, str) or not policy_version:
            raise ValueError("policy_version is required.")
        if not isinstance(currency, str) or not currency:
            raise ValueError("currency is required.")
        try:
            tolerance = Decimal(str(raw["reconciliation_tolerance_brl"]))
        except (KeyError, ValueError, ArithmeticError) as error:
            raise ValueError("reconciliation_tolerance_brl must be numeric.") from error
        if tolerance < 0:
            raise ValueError("reconciliation_tolerance_brl cannot be negative.")

        limits = raw.get("output_limits")
        if not isinstance(limits, dict) or set(limits) != REQUIRED_LIMITS:
            raise ValueError(f"output_limits must contain exactly {sorted(REQUIRED_LIMITS)}.")
        normalized_limits: dict[str, int] = {}
        for name, value in limits.items():
            if not isinstance(value, int) or value < 1:
                raise ValueError(f"output_limits.{name} must be a positive integer.")
            normalized_limits[name] = value

        confidence_profiles = cls._parse_confidence_profiles(raw.get("confidence_profiles"))

        raw_rules = raw.get("rules")
        if not isinstance(raw_rules, list) or not raw_rules:
            raise ValueError("rules must be a non-empty list in priority order.")
        rules = tuple(cls._parse_rule(rule, confidence_profiles) for rule in raw_rules)
        issues = [rule.primary_issue for rule in rules]
        if len(issues) != len(set(issues)):
            raise ValueError("rules cannot contain duplicate primary_issue values.")
        return cls(policy_version, currency, tolerance, normalized_limits, confidence_profiles, rules)

    @staticmethod
    def _parse_confidence_profiles(raw: Any) -> dict[str, dict[str, float]]:
        if not isinstance(raw, dict) or not raw:
            raise ValueError("confidence_profiles must be a non-empty object.")
        profiles: dict[str, dict[str, float]] = {}
        for profile_name, profile in raw.items():
            if not isinstance(profile_name, str) or not profile_name:
                raise ValueError("confidence profile names must be non-empty strings.")
            if not isinstance(profile, dict) or not profile:
                raise ValueError(f"confidence_profiles.{profile_name} must be a non-empty object.")
            values: dict[str, float] = {}
            for level, confidence in profile.items():
                if not isinstance(level, str) or not isinstance(confidence, (int, float)):
                    raise ValueError(f"confidence_profiles.{profile_name} is invalid.")
                if level not in CONFIDENCE_LEVELS:
                    raise ValueError(f"Unsupported confidence level: {level!r}")
                if not 0 <= confidence <= 1:
                    raise ValueError(f"confidence_profiles.{profile_name}.{level} must be between 0 and 1.")
                values[level] = float(confidence)
            if "default" not in values and set(values) != {"exact", "within_tolerance"}:
                raise ValueError(
                    f"confidence_profiles.{profile_name} needs default or exact/within_tolerance."
                )
            profiles[profile_name] = values
        return profiles

    @staticmethod
    def _parse_rule(
        raw: Any, confidence_profiles: dict[str, dict[str, float]]
    ) -> PolicyRule:
        if not isinstance(raw, dict):
            raise ValueError("Each policy rule must be an object.")
        required = {
            "primary_issue",
            "match_key",
            "cause_code",
            "action",
            "refund_basis",
            "evidence_sources",
            "confidence_profile",
        }
        if not required.issubset(raw):
            raise ValueError(f"Policy rule is missing fields: {sorted(required - set(raw))}")
        match_key = raw["match_key"]
        refund_basis = raw["refund_basis"]
        confidence_profile = raw["confidence_profile"]
        evidence_sources = raw["evidence_sources"]
        if match_key not in MATCH_KEYS:
            raise ValueError(f"Unsupported match_key: {match_key!r}")
        if refund_basis not in REFUND_BASES:
            raise ValueError(f"Unsupported refund_basis: {refund_basis!r}")
        if not isinstance(confidence_profile, str) or confidence_profile not in confidence_profiles:
            raise ValueError("Policy rule confidence_profile must reference a configured profile.")
        if (
            not isinstance(evidence_sources, list)
            or not evidence_sources
            or not all(isinstance(source, str) for source in evidence_sources)
            or len(evidence_sources) != len(set(evidence_sources))
            or not set(evidence_sources).issubset(EVIDENCE_SOURCES)
            or {"order", "policy"} - set(evidence_sources)
        ):
            raise ValueError(
                "Policy rule evidence_sources must be unique known sources including order and policy."
            )
        for field in ("primary_issue", "cause_code", "action"):
            if not isinstance(raw[field], str) or not raw[field]:
                raise ValueError(f"Policy rule {field} must be a non-empty string.")

        party = raw.get("responsible_party")
        if party is not None:
            if not isinstance(party, dict) or not isinstance(party.get("party_type"), str):
                raise ValueError("responsible_party must contain party_type.")
            if party["party_type"] != "seller" and not isinstance(party.get("party_id"), str):
                raise ValueError("Non-seller responsible_party must contain party_id.")
            party = {key: value for key, value in party.items() if isinstance(value, str)}

        return PolicyRule(
            primary_issue=raw["primary_issue"],
            match_key=match_key,
            cause_code=raw["cause_code"],
            action=raw["action"],
            refund_basis=refund_basis,
            evidence_sources=tuple(evidence_sources),
            confidence_profile=confidence_profile,
            responsible_party=party,
        )

    def limit(self, name: str) -> int:
        return self.output_limits[name]

    def confidence_for(self, rule: PolicyRule, reconciliation_delta: Decimal) -> float:
        profile = self.confidence_profiles[rule.confidence_profile]
        if reconciliation_delta == 0 and "exact" in profile:
            return profile["exact"]
        if reconciliation_delta > 0 and "within_tolerance" in profile:
            return profile["within_tolerance"]
        if "default" in profile:
            return profile["default"]
        raise ValueError(
            f"Confidence profile {rule.confidence_profile!r} cannot score the available facts."
        )
