"""
Secrets management via Google Secret Manager.

In production, secrets are accessed from Secret Manager, not environment variables.
This provides:
- Centralized secret management
- Audit logging of all secret access
- Automatic secret rotation
- Fine-grained IAM control

Usage:
    from secrets import Secrets

    api_token = Secrets.get("dynatrace-api-token")
    webhook_secret = Secrets.get("dynatrace-webhook-secret")
"""

import logging
import os
from functools import lru_cache

from google.cloud import secretmanager

logger = logging.getLogger(__name__)


class Secrets:
    """
    Secrets manager for SiteMedic.

    Reads from Google Secret Manager in production.
    Falls back to environment variables in development.
    """

    _cache = {}
    _client = None

    @classmethod
    def _get_client(cls) -> secretmanager.SecretManagerServiceClient:
        """Get or create the Secret Manager client."""
        if cls._client is None:
            cls._client = secretmanager.SecretManagerServiceClient()
        return cls._client

    @classmethod
    def get(cls, secret_name: str, default: str = None) -> str:
        """
        Get a secret by name.

        In production (env var GCP_PROJECT_ID set):
            Reads from Secret Manager at projects/{PROJECT}/secrets/{secret_name}/versions/latest

        In development (no GCP_PROJECT_ID):
            Falls back to environment variable with snake_case conversion:
            E.g., "dynatrace-api-token" → env var "DYNATRACE_API_TOKEN"

        Args:
            secret_name: Secret identifier (e.g., "dynatrace-api-token-dev")
            default: Default value if secret not found and not required

        Returns:
            Secret value as string

        Raises:
            RuntimeError: If secret not found and no default provided
        """
        # Check cache first
        if secret_name in cls._cache:
            return cls._cache[secret_name]

        project_id = os.environ.get("GCP_PROJECT_ID")

        if project_id:
            # Production: read from Secret Manager
            try:
                return cls._read_from_secret_manager(project_id, secret_name)
            except Exception as e:
                logger.error(f"Failed to read secret '{secret_name}' from Secret Manager: {e}")
                if default is not None:
                    return default
                raise RuntimeError(
                    f"Secret '{secret_name}' not found in Secret Manager and no default provided"
                )
        else:
            # Development: fall back to environment variables
            env_var_name = secret_name.upper().replace("-", "_")
            value = os.environ.get(env_var_name)
            if value is None:
                if default is not None:
                    return default
                raise RuntimeError(
                    f"Secret '{secret_name}' not found. "
                    f"Set environment variable '{env_var_name}' or configure GCP_PROJECT_ID for Secret Manager"
                )
            return value

    @classmethod
    def _read_from_secret_manager(cls, project_id: str, secret_name: str) -> str:
        """Read a secret from Google Secret Manager."""
        client = cls._get_client()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"

        logger.debug(f"Reading secret from Secret Manager: {name}")

        response = client.access_secret_version(request={"name": name})
        secret_value = response.payload.data.decode("UTF-8")

        # Cache the value
        cls._cache[secret_name] = secret_value

        # Log access (audit trail)
        logger.info(f"Accessed secret: {secret_name}")

        return secret_value

    @classmethod
    def clear_cache(cls):
        """Clear the in-memory cache (useful for testing)."""
        cls._cache.clear()


# Convenience function for quick access
def get_secret(name: str, default: str = None) -> str:
    """Get a secret from Secret Manager or environment variables."""
    return Secrets.get(name, default)
