"""Tuzi vision provider configuration.

The provider is configured entirely from the environment or a secret-bearing
file. Model identifiers are deployment configuration, never code constants: the
service must fail to start rather than silently pick "the first model" from a
provider list. The API key is never written to the repo, and this module's own
``repr`` and serialization redact it so it cannot leak into logs or error
responses.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, SecretStr

DEFAULT_BASE_URL = "https://api.tu-zi.com"
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024


class VisionConfig(BaseModel):
    """Resolved vision provider settings. ``api_key`` is a secret and is redacted."""

    model_config = ConfigDict(extra="forbid")

    baseUrl: str = Field(default=DEFAULT_BASE_URL)
    apiKey: SecretStr = Field(...)
    primaryModel: str = Field(min_length=1)
    reviewModel: str = Field(min_length=1)
    timeoutSeconds: int = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0)
    maxImageBytes: int = Field(default=DEFAULT_MAX_IMAGE_BYTES, gt=0)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "VisionConfig":
        """Build a config from environment variables.

        The API key is read from ``TUZI_API_KEY`` or, if that is absent, from the
        file named by ``TUZI_API_KEY_FILE`` (a secret-injection convenience).
        The two model identifiers are mandatory; a missing model id is a hard
        configuration error and must not default to the first provider model.
        """
        variables = dict(os.environ if env is None else env)

        api_key = cls._resolve_api_key(variables)
        base_url = variables.get("TUZI_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

        missing = [
            name
            for name, value in (
                ("TUZI_VISION_PRIMARY_MODEL", variables.get("TUZI_VISION_PRIMARY_MODEL")),
                ("TUZI_VISION_REVIEW_MODEL", variables.get("TUZI_VISION_REVIEW_MODEL")),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"missing vision model config: {', '.join(missing)}")

        timeout = variables.get("TUZI_TIMEOUT_SECONDS")
        max_bytes = variables.get("TUZI_MAX_IMAGE_BYTES")

        return cls(
            baseUrl=base_url,
            apiKey=SecretStr(api_key),
            primaryModel=variables["TUZI_VISION_PRIMARY_MODEL"].strip(),
            reviewModel=variables["TUZI_VISION_REVIEW_MODEL"].strip(),
            timeoutSeconds=int(timeout) if timeout else DEFAULT_TIMEOUT_SECONDS,
            maxImageBytes=int(max_bytes) if max_bytes else DEFAULT_MAX_IMAGE_BYTES,
        )

    @staticmethod
    def _resolve_api_key(variables: Mapping[str, str]) -> str:
        direct = variables.get("TUZI_API_KEY")
        if direct and direct.strip():
            return direct.strip()
        key_file = variables.get("TUZI_API_KEY_FILE")
        if key_file:
            path = Path(key_file)
            if path.is_file():
                return path.read_text(encoding="utf-8").strip()
        raise ValueError(
            "TUZI_API_KEY or TUZI_API_KEY_FILE must be set to use the vision provider"
        )

    def redacted_dict(self) -> dict:
        """A serializable view with the key masked, safe for logs and status."""

        return {
            "baseUrl": self.baseUrl,
            "apiKey": "<redacted>",
            "primaryModel": self.primaryModel,
            "reviewModel": self.reviewModel,
            "timeoutSeconds": self.timeoutSeconds,
            "maxImageBytes": self.maxImageBytes,
        }
