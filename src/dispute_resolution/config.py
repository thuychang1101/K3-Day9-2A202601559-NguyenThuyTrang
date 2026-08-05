"""Runtime configuration and lightweight .env loading."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path


# Kept in source so the configured model is never hidden solely in .env.
DEFAULT_MODEL_NAME = "qwen/qwen3-8b"
DEFAULT_MODEL_PARAMETER_SIZE_B = 8.2
DEFAULT_MODEL_PROVIDER = "openrouter"
DEFAULT_MODEL_ENDPOINT = "https://openrouter.ai/api/v1"
DEFAULT_OPENAI_MODEL_NAME = "gpt-4o-mini"
DEFAULT_OPENAI_MODEL_ENDPOINT = "https://api.openai.com/v1"


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE pairs without adding a runtime dependency."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            # Project configuration must be reproducible even when a terminal
            # inherits stale provider variables from another project.
            os.environ[key] = value


@dataclass(frozen=True)
class RuntimeConfig:
    model_provider: str
    model_name: str
    model_parameter_size_b: float | None
    model_endpoint: str | None
    model_api_key: str | None
    model_site_url: str | None
    model_app_name: str
    model_timeout_seconds: float
    model_audit_enabled: bool
    model_proposal_enabled: bool
    model_output_mode: str
    model_audit_scope: str
    strict_model_audit: bool
    max_workers: int
    runtime: str

    @classmethod
    def from_project_root(cls, project_root: Path) -> "RuntimeConfig":
        load_dotenv(project_root / ".env")

        provider = os.getenv("DISPUTE_MODEL_PROVIDER", DEFAULT_MODEL_PROVIDER).lower()
        if provider not in {"openrouter", "openai"}:
            raise ValueError("DISPUTE_MODEL_PROVIDER must be 'openrouter' or 'openai'.")

        if provider == "openrouter":
            try:
                parameter_size: float | None = float(
                    os.getenv("DISPUTE_MODEL_PARAMETER_SIZE_B", str(DEFAULT_MODEL_PARAMETER_SIZE_B))
                )
            except ValueError as error:
                raise ValueError("DISPUTE_MODEL_PARAMETER_SIZE_B must be numeric.") from error
            if parameter_size <= 0 or parameter_size > 10:
                raise ValueError("DISPUTE_MODEL_PARAMETER_SIZE_B must be greater than 0 and at most 10.")
            model_name = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL_NAME)
            model_endpoint = os.getenv("OPENROUTER_BASE_URL", DEFAULT_MODEL_ENDPOINT)
            model_api_key = os.getenv("OPENROUTER_API_KEY") or None
            model_site_url = os.getenv("OPENROUTER_SITE_URL") or None
            model_app_name = os.getenv("OPENROUTER_APP_NAME", "olist-dispute-resolution")
            timeout_setting = "OPENROUTER_TIMEOUT_SECONDS"
        else:
            # OpenAI does not publish a parameter count for GPT-5.6 Luna.
            parameter_size = None
            model_name = os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL_NAME)
            model_endpoint = os.getenv("OPENAI_BASE_URL", DEFAULT_OPENAI_MODEL_ENDPOINT)
            model_api_key = os.getenv("OPENAI_API_KEY") or None
            model_site_url = None
            model_app_name = "olist-dispute-resolution"
            timeout_setting = "OPENAI_TIMEOUT_SECONDS"

        try:
            max_workers = int(os.getenv("DISPUTE_MAX_WORKERS", "3"))
        except ValueError as error:
            raise ValueError("DISPUTE_MAX_WORKERS must be an integer.") from error
        if not 1 <= max_workers <= 3:
            raise ValueError("DISPUTE_MAX_WORKERS must be between 1 and 3.")

        try:
            timeout_seconds = float(os.getenv(timeout_setting, "30"))
        except ValueError as error:
            raise ValueError(f"{timeout_setting} must be numeric.") from error
        if timeout_seconds <= 0:
            raise ValueError(f"{timeout_setting} must be greater than 0.")

        audit_scope = os.getenv("DISPUTE_MODEL_AUDIT_SCOPE", "final_only").lower()
        if audit_scope not in {"final_only", "per_agent"}:
            raise ValueError("DISPUTE_MODEL_AUDIT_SCOPE must be 'final_only' or 'per_agent'.")
        output_mode = os.getenv("DISPUTE_MODEL_OUTPUT_MODE", "deterministic").lower()
        if output_mode not in {"deterministic", "model_assisted"}:
            raise ValueError(
                "DISPUTE_MODEL_OUTPUT_MODE must be 'deterministic' or 'model_assisted'."
            )

        return cls(
            model_provider=provider,
            model_name=model_name,
            model_parameter_size_b=parameter_size,
            model_endpoint=model_endpoint,
            model_api_key=model_api_key,
            model_site_url=model_site_url,
            model_app_name=model_app_name,
            model_timeout_seconds=timeout_seconds,
            model_audit_enabled=os.getenv("DISPUTE_ENABLE_MODEL_AUDIT", "true").lower() == "true",
            model_proposal_enabled=os.getenv("DISPUTE_ENABLE_MODEL_PROPOSAL", "true").lower()
            == "true",
            model_output_mode=output_mode,
            model_audit_scope=audit_scope,
            strict_model_audit=os.getenv("DISPUTE_STRICT_MODEL_AUDIT", "false").lower() == "true",
            max_workers=max_workers,
            runtime=os.getenv("DISPUTE_RUNTIME", "python"),
        )

    def metadata(self) -> dict[str, object]:
        data = asdict(self)
        data.pop("model_api_key", None)
        return data
