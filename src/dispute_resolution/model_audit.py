"""Provider-backed, non-authoritative handoff audits for each agent."""

from __future__ import annotations

import json
from typing import Any

from .config import RuntimeConfig


class ModelAuditClient:
    """Calls the selected provider model without owning business decisions."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self._client: Any | None = None

    @property
    def strict(self) -> bool:
        return self.config.strict_model_audit

    def review(self, agent_name: str, summary: dict[str, Any]) -> dict[str, str]:
        if not self.config.model_audit_enabled:
            return {"status": "disabled", "reason": "DISPUTE_ENABLE_MODEL_AUDIT=false"}
        if not self.config.model_api_key:
            key_name = "OPENROUTER_API_KEY" if self.config.model_provider == "openrouter" else "OPENAI_API_KEY"
            return {"status": "skipped", "reason": f"{key_name} is not configured"}

        try:
            client = self._get_client()
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
            if self.config.model_provider == "openai":
                request: dict[str, Any] = {
                    "model": self.config.model_name,
                    "instructions": system_prompt,
                    "input": user_prompt,
                }
                if self.config.model_name.startswith("gpt-5.6"):
                    request["reasoning"] = {"effort": "none"}
                response = client.responses.create(
                    **request,
                )
                content = (response.output_text or "").strip()
            else:
                completion = client.chat.completions.create(
                    model=self.config.model_name,
                    temperature=0,
                    max_tokens=120,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                content = (completion.choices[0].message.content or "").strip()
            response = self._parse_response(content)
            response["model"] = self.config.model_name
            return response
        except Exception as error:  # API failures are traceable and optionally strict.
            return {"status": "unavailable", "reason": str(error)[:300]}

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
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {"status": "needs_review", "reason": content[:300] or "Empty model response"}
        if not isinstance(parsed, dict):
            return {"status": "needs_review", "reason": "Model response was not a JSON object"}
        status = parsed.get("status")
        reason = parsed.get("reason")
        if status not in {"ok", "needs_review"} or not isinstance(reason, str):
            return {"status": "needs_review", "reason": "Model response did not match audit schema"}
        return {"status": status, "reason": reason[:300]}
