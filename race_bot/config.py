"""Runtime configuration, from environment or a .env file."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings are prefixed `RACE_BOT_` in the environment."""

    model_config = SettingsConfigDict(
        env_prefix="RACE_BOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Azure AI Foundry. Unset until phase 3; phases 1-2 run without it.
    azure_endpoint: str | None = None
    azure_api_key: SecretStr | None = None
    azure_api_version: str = "2024-10-21"

    situation_model: str = "gpt-4o-mini"
    """Runs every tick. Keep it cheap."""

    tactics_model: str = "gpt-4o"
    """Runs only on meaningful state change. Can afford to be stronger."""

    tick_seconds: float = Field(default=1.0, ge=0.0)
    replay_speed: float = Field(default=60.0, ge=0.0)
    window_max_posts: int = Field(default=25, ge=1)
    window_keep_recent: int = Field(default=8, ge=0)
    salience_threshold: float = Field(default=0.25, ge=0.0, le=1.0)
    """How salient a post must be for the pipeline to run an analysis pass."""

    tactics_salience_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    """How salient a post must be to call the tactics agent with no state change.

    Not yet calibrated against real commentary — raise it to cut cost, lower it
    to catch more moves that the state tracker alone would miss.
    """

    tactics_gap_shift_seconds: float = Field(default=30.0, ge=0.0)
    """A gap moving by this much counts as a change in the race, not noise."""

    @property
    def azure_configured(self) -> bool:
        return bool(self.azure_endpoint and self.azure_api_key)


def load_settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]
