"""
Environment configuration loader.

Loads and validates environment-specific configuration from YAML files.
Usage: from environment import env
        value = env.get("logging.level")
"""

import os
import re
from pathlib import Path
from typing import Any, Optional
import logging

import yaml
from pydantic import BaseModel, Field, validator

logger = logging.getLogger(__name__)


class GCPConfig(BaseModel):
    project_id: str
    region: str


class FirestoreConfig(BaseModel):
    database: str
    emulator_host: Optional[str] = None


class VertexAIConfig(BaseModel):
    location: str
    model_version: str
    max_tokens: int
    temperature: float


class DynatraceConfig(BaseModel):
    tenant_url: str
    api_token: str
    mcp_server_url: str
    webhook_secret: str


class CloudRunConfig(BaseModel):
    region: str
    demo_app_url: str
    agent_url: str


class DetectionConfig(BaseModel):
    polling_interval_seconds: int
    webhook_enabled: bool
    fallback_polling_enabled: bool


class PredictionConfig(BaseModel):
    enabled: bool
    interval_seconds: int
    ttl_minutes: int


class RemediationConfig(BaseModel):
    dry_run_only: bool
    auto_approve_threshold: float
    require_explicit_confirmation: bool
    max_concurrent_remediations: int


class CircuitBreakerConfig(BaseModel):
    gemini_error_threshold: int
    gemini_success_threshold: int
    dynatrace_error_threshold: int
    dynatrace_success_threshold: int
    firestore_error_threshold: int
    firestore_success_threshold: int


class CostConfig(BaseModel):
    track_enabled: bool
    alert_threshold_usd: float
    budget_limit_usd: Optional[float] = None


class CorrelationConfig(BaseModel):
    enabled: bool
    temporal_window_minutes: int
    confidence_threshold: float


class LoggingConfig(BaseModel):
    level: str
    format: str
    structured: bool
    include_pii: bool
    include_trace_details: bool = True


class FeaturesConfig(BaseModel):
    evals_enabled: bool
    chaos_enabled: bool
    predictions_enabled: bool
    correlation_enabled: bool
    dry_run_mode: bool


class AuditConfig(BaseModel):
    enabled: bool
    ttl_days: int
    log_to_cloud_logging: bool


class DemoAppConfig(BaseModel):
    stable_revision: str
    buggy_revision: str


class EnvironmentConfig(BaseModel):
    environment: str
    description: str
    gcp: GCPConfig
    firestore: FirestoreConfig
    vertex_ai: VertexAIConfig
    dynatrace: DynatraceConfig
    cloud_run: CloudRunConfig
    detection: DetectionConfig
    prediction: PredictionConfig
    remediation: RemediationConfig
    circuit_breaker: CircuitBreakerConfig
    cost: CostConfig
    correlation: CorrelationConfig
    logging: LoggingConfig
    features: FeaturesConfig
    audit: AuditConfig
    demo_app: DemoAppConfig

    @validator("environment")
    def validate_environment(cls, v):
        if v not in ("dev", "staging", "prod"):
            raise ValueError(f"environment must be 'dev', 'staging', or 'prod', got '{v}'")
        return v

    @validator("remediation", pre=True)
    def validate_remediation(cls, v):
        if v.get("auto_approve_threshold") < 0 or v.get("auto_approve_threshold") > 1:
            raise ValueError("auto_approve_threshold must be between 0 and 1")
        return v

    class Config:
        extra = "allow"


def _substitute_env_vars(data: Any) -> Any:
    """Recursively substitute ${VAR} and ${VAR:default} patterns with environment variables."""
    if isinstance(data, dict):
        return {k: _substitute_env_vars(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_substitute_env_vars(item) for item in data]
    elif isinstance(data, str):
        # Pattern: ${VAR} or ${VAR:default_value}
        def replace_var(match):
            full = match.group(1)
            if ":" in full:
                var_name, default = full.split(":", 1)
                return os.environ.get(var_name, default)
            else:
                var_name = full
                value = os.environ.get(var_name)
                if value is None:
                    raise ValueError(f"Required environment variable '{var_name}' not set")
                return value

        return re.sub(r"\$\{([^}]+)\}", replace_var, data)
    else:
        return data


class Environment:
    """
    Typed environment configuration loader.

    Loads YAML configuration for the current environment (dev, staging, prod).
    Fails fast if ENV is not set or invalid.
    Prevents cross-environment access (e.g., prod code accessing dev Firestore).
    """

    _instance: Optional["Environment"] = None
    _config: Optional[EnvironmentConfig] = None

    def __init__(self):
        """Initialize environment configuration from ENV variable."""
        env_name = os.environ.get("ENV")
        if not env_name:
            raise RuntimeError(
                "ENV environment variable must be set to 'dev', 'staging', or 'prod'. "
                "Set it before importing this module."
            )

        config_file = Path(__file__).parent.parent / "config" / "environments" / f"{env_name}.yaml"
        if not config_file.exists():
            raise RuntimeError(
                f"Configuration file not found: {config_file}\n"
                f"Valid environments are: dev, staging, prod"
            )

        try:
            with open(config_file) as f:
                raw_config = yaml.safe_load(f)

            # Substitute environment variables in the loaded config
            raw_config = _substitute_env_vars(raw_config)

            # Validate and load into Pydantic model
            self._config = EnvironmentConfig(**raw_config)
            logger.info(f"Loaded environment configuration: {self._config.environment} ({self._config.description})")
        except Exception as e:
            raise RuntimeError(f"Failed to load environment config from {config_file}: {e}")

    @classmethod
    def get_instance(cls) -> "Environment":
        """Get or create the singleton Environment instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def get(cls, path: str, default: Any = None) -> Any:
        """
        Get a configuration value by dot-notation path.

        Examples:
            env.get("logging.level")          # Returns "INFO" or "DEBUG"
            env.get("detection.webhook_enabled")  # Returns True/False
            env.get("remediation.auto_approve_threshold")  # Returns 0.85
            env.get("unknown.path", "default")  # Returns "default"
        """
        instance = cls.get_instance()
        config_dict = instance._config.dict()

        keys = path.split(".")
        value = config_dict

        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default

        return value

    @classmethod
    def name(cls) -> str:
        """Return the current environment name (dev, staging, prod)."""
        return cls.get_instance()._config.environment

    @classmethod
    def is_dev(cls) -> bool:
        """Check if running in development environment."""
        return cls.name() == "dev"

    @classmethod
    def is_staging(cls) -> bool:
        """Check if running in staging environment."""
        return cls.name() == "staging"

    @classmethod
    def is_prod(cls) -> bool:
        """Check if running in production environment."""
        return cls.name() == "prod"

    @classmethod
    def assert_not_prod(cls, feature_name: str) -> None:
        """
        Raise an error if this code is running in production.
        Use this to gate development-only features that should never run in prod.

        Example:
            Environment.assert_not_prod("evals")
        """
        if cls.is_prod():
            raise RuntimeError(
                f"Feature '{feature_name}' is not available in production environment. "
                f"This is a safety check; it should never be reached."
            )

    @classmethod
    def require_prod(cls, feature_name: str) -> None:
        """
        Raise an error if this code is NOT running in production.
        Use this for features that should only ever run in prod.

        Example:
            Environment.require_prod("cost_tracking")
        """
        if not cls.is_prod():
            raise RuntimeError(
                f"Feature '{feature_name}' is only available in production environment. "
                f"Current environment: {cls.name()}"
            )

    @classmethod
    def config(cls) -> EnvironmentConfig:
        """Get the full configuration object (typed, validated)."""
        return cls.get_instance()._config


# Initialize environment on module load (fail fast if ENV not set)
_env_name = os.environ.get("ENV")
if not _env_name:
    # Don't fail immediately in case someone imports this module just to check the name
    # But do log a warning for development
    pass
