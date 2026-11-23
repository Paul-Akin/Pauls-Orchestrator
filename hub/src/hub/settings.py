"""Secrets and machine-specific settings, read ONLY from the environment / ``.env``.

Non-secret behaviour (routes, models, prices, bind address, MQTT host) lives in
YAML under ``hub/config``; see :mod:`hub.config`. The split is deliberate:
YAML is committed and reviewed, ``.env`` is never committed.

Provider keys use each vendor's conventional variable name so that existing
shell setups keep working. They are always passed to LiteLLM explicitly
(``api_key=...``); LiteLLM is never allowed to discover keys on its own, so
there is exactly one path by which a key enters a request.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from hub.paths import default_config_dir, repo_root


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=repo_root() / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- Provider credentials (all optional: any subset may be present) ---
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    gemini_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY")
    )
    # Meta's docs call this MODEL_API_KEY; LiteLLM calls it META_API_KEY. Accept both.
    meta_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("META_API_KEY", "MODEL_API_KEY")
    )
    ollama_api_base: str = Field(
        default="http://127.0.0.1:11434", validation_alias="OLLAMA_API_BASE"
    )

    # --- Hub access control ---
    # Required whenever the HTTP server binds to a non-loopback address.
    hub_api_token: SecretStr | None = Field(default=None, validation_alias="HUB_API_TOKEN")
    # JSON object mapping device_id -> token, e.g. {"cores3-kitchen": "..."}.
    device_tokens: dict[str, SecretStr] = Field(
        default_factory=dict, validation_alias="HUB_DEVICE_TOKENS"
    )

    # --- MQTT broker credentials for the hub's own client ---
    mqtt_username: str | None = Field(default=None, validation_alias="HUB_MQTT_USERNAME")
    mqtt_password: SecretStr | None = Field(default=None, validation_alias="HUB_MQTT_PASSWORD")

    # --- Overrides ---
    config_dir: Path = Field(default_factory=default_config_dir, validation_alias="HUB_CONFIG_DIR")
    host_override: str | None = Field(default=None, validation_alias="HUB_HOST")
    port_override: int | None = Field(default=None, validation_alias="HUB_PORT")
