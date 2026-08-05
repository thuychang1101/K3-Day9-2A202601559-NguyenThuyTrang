"""Provider-backed audits and guarded policy proposals for configured workflow stages."""

from __future__ import annotations

import json
from typing import Any

from .config import RuntimeConfig


class ModelAuditClient:
    """Calls the selected provider model for audits and policy proposals."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self._client: Any | None = None

    @property
    def strict(self) -> bool:
        return self.config.strict_model_audit

    def should_review(self, agent_name: str) -> bool:
        """Audit only the final gate by default; retain per-agent mode for investigation."""
        return self.config.model_audit_enabled and (
            self.config.model_audit_scope == "per_agent" or agent_name == "verifier"
        )

    def should_propose(self) -> bool:
        return self.config.model_audit_enabled and self.config.model_proposal_enabled

    def review(self, agent_name: str, summary: dict[str, Any]) -> dict[str, str]:
        if not self.config.model_audit_enabled:
            return {"status": "disabled", "reason": "DISPUTE_ENABLE_MODEL_AUDIT=false"}
        if not self.should_review(agent_name):
            return {
                "status": "disabled",
                "reason": f"{agent_name} is outside DISPUTE_MODEL_AUDIT_SCOPE",
            }
        try:
            system_prompt = (
                "You audit a structured e-commerce case handoff. "
                "Do not invent facts or make a refund decision. "
                "Return compact JSON only: {\"status\": \"ok\"|\"needs_review\", "
                "\"reason\": \"short reason\"}."
            )
            user_prompt = json.dumps(
                {"agent": agent_name, "handoff_summary": summary},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            content = self._request_json(system_prompt, user_prompt, max_tokens=120)
            response = self._parse_response(content)
            response["model"] = self.config.model_name
            return response
        except Exception as error:  # API failures are traceable and optionally strict.
            return {"status": "unavailable", "reason": str(error)[:300]}

    def propose_policy(self, summary: dict[str, Any]) -> dict[str, Any]:
        """Ask the provider for a structured policy proposal."""
        if not self.config.model_audit_enabled:
            return {"status": "disabled", "reason": "DISPUTE_ENABLE_MODEL_AUDIT=false"}
        if not self.config.model_proposal_enabled:
            return {"status": "disabled", "reason": "DISPUTE_ENABLE_MODEL_PROPOSAL=false"}
        try:
            system_prompt = (
                "You audit and propose an e-commerce dispute resolution from supplied facts, "
                "policy rules, and candidate_resolution. Use only supplied facts. "
                "If candidate_resolution is consistent with the rules, return those exact decision fields. "
                "Return one compact JSON object only, with exactly these keys: "
                "primary_issue, cause_code, refund_basis, recommended_refund_brl, case_status, "
                "action, responsible_parties, confidence, reason. "
                "recommended_refund_brl and confidence must be JSON numbers; "
                "responsible_parties must be a JSON array."
            )
            user_prompt = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
            content = self._request_json(system_prompt, user_prompt, max_tokens=300)
            proposal = self._parse_policy_proposal(content)
            if proposal["status"] != "ok" and "candidate_resolution" in summary:
                retry_prompt = (
                    "Return exactly one JSON object. Use the supplied candidate_resolution as the "
                    "model's final dispute decision unless it violates the supplied facts. "
                    "The JSON object must contain only these keys: primary_issue, cause_code, "
                    "refund_basis, recommended_refund_brl, case_status, action, "
                    "responsible_parties, confidence, reason."
                )
                retry_payload = {
                    "candidate_resolution": summary["candidate_resolution"],
                    "facts": summary.get("facts", {}),
                    "policy_rules": summary.get("policy_rules", []),
                }
                content = self._request_json(
                    retry_prompt,
                    json.dumps(retry_payload, ensure_ascii=False, separators=(",", ":")),
                    max_tokens=260,
                )
                proposal = self._parse_policy_proposal(content)
            proposal["model"] = self.config.model_name
            return proposal
        except Exception as error:  # Proposal failures are traceable and optionally strict.
            return {"status": "unavailable", "reason": str(error)[:300]}

    def _request_json(self, system_prompt: str, user_prompt: str, *, max_tokens: int) -> str:
        if not self.config.model_api_key:
            key_name = "OPENROUTER_API_KEY" if self.config.model_provider == "openrouter" else "OPENAI_API_KEY"
            raise RuntimeError(f"{key_name} is not configured")
        client = self._get_client()
        if self.config.model_provider == "openai":
            request: dict[str, Any] = {
                "model": self.config.model_name,
                "instructions": system_prompt,
                "input": user_prompt,
                "max_output_tokens": max_tokens,
            }
            if self.config.model_name.startswith("gpt-5.6"):
                request["reasoning"] = {"effort": "none"}
            response = client.responses.create(**request)
            return (response.output_text or "").strip()

        completion = client.chat.completions.create(
            model=self.config.model_name,
            temperature=0,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return (completion.choices[0].message.content or "").strip()

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client

        from openai import OpenAI

        client_options: dict[str, Any] = {
            "api_key": self.config.model_api_key,
            "base_url": self.config.model_endpoint,
            "timeout": self.config.model_timeout_seconds,
        }
        if self.config.model_provider == "openrouter":
            headers = {"X-Title": self.config.model_app_name}
            if self.config.model_site_url:
                headers["HTTP-Referer"] = self.config.model_site_url
            client_options["default_headers"] = headers
        self._client = OpenAI(
            **client_options,
        )
        return self._client

    @staticmethod
    def _parse_response(content: str) -> dict[str, str]:
        parsed = ModelAuditClient._parse_json_object(content)
        if parsed is None:
            return {"status": "needs_review", "reason": content[:300] or "Empty model response"}
        status = parsed.get("status")
        reason = parsed.get("reason")
        if status not in {"ok", "needs_review"} or not isinstance(reason, str):
            return {"status": "needs_review", "reason": "Model response did not match audit schema"}
        return {"status": status, "reason": reason[:300]}

    @staticmethod
    def _parse_policy_proposal(content: str) -> dict[str, Any]:
        parsed = ModelAuditClient._parse_json_object(content)
        if parsed is None:
            return {"status": "needs_review", "reason": content[:300] or "Empty model response"}
        for wrapper in ("proposal", "resolution", "decision", "candidate_resolution"):
            nested = parsed.get(wrapper)
            if isinstance(nested, dict):
                nested = dict(nested)
                if "reason" not in nested and isinstance(parsed.get("reason"), str):
                    nested["reason"] = parsed["reason"]
                parsed = nested
                break
        aliases = {
            "recommended_refund": "recommended_refund_brl",
            "refund_amount_brl": "recommended_refund_brl",
            "resolution_action": "action",
            "resolution_actions": "action",
        }
        parsed = dict(parsed)
        for alias, canonical in aliases.items():
            if canonical not in parsed and alias in parsed:
                parsed[canonical] = parsed[alias][0] if alias == "resolution_actions" and isinstance(parsed[alias], list) and parsed[alias] else parsed[alias]
        for numeric_key in ("recommended_refund_brl", "confidence"):
            value = parsed.get(numeric_key)
            if isinstance(value, str):
                try:
                    parsed[numeric_key] = float(value)
                except ValueError:
                    pass
        if parsed.get("responsible_parties") is None:
            parsed["responsible_parties"] = []
        if "reason" not in parsed:
            parsed["reason"] = "Model returned a structured policy proposal."
        required = {
            "primary_issue": str,
            "cause_code": str,
            "refund_basis": str,
            "recommended_refund_brl": (int, float),
            "case_status": str,
            "action": str,
            "responsible_parties": list,
            "confidence": (int, float),
            "reason": str,
        }
        if any(not isinstance(parsed.get(key), expected_type) for key, expected_type in required.items()):
            return {"status": "needs_review", "reason": "Model response did not match proposal schema"}
        if not 0 <= parsed["confidence"] <= 1:
            return {"status": "needs_review", "reason": "Model proposal confidence was outside [0, 1]"}
        return {
            "status": "ok",
            **{key: parsed[key] for key in required if key != "reason"},
            "reason": parsed["reason"][:300],
        }

    @staticmethod
    def _parse_json_object(content: str) -> dict[str, Any] | None:
        cleaned = content.strip()
        if cleaned.startswith("```") and cleaned.endswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed
