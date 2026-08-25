"""Azure AI Foundry wiring.

One provider (and therefore one HTTP client) is shared across every agent. The
situation and tactics agents differ only in which deployment they point at.
"""

from __future__ import annotations

from functools import cached_property

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.azure import AzureProvider

from race_bot.config import Settings


class AzureNotConfigured(RuntimeError):
    """Raised when a model is requested without Azure credentials in place."""


class AzureModels:
    """Builds models for the deployments named in settings."""

    def __init__(self, settings: Settings) -> None:
        if not settings.azure_configured:
            raise AzureNotConfigured(
                "Azure AI Foundry is not configured. Set RACE_BOT_AZURE_ENDPOINT and "
                "RACE_BOT_AZURE_API_KEY (see .env.example), then run `race-bot check`."
            )
        self.settings = settings

    @cached_property
    def provider(self) -> AzureProvider:
        assert self.settings.azure_api_key is not None  # guarded in __init__
        return AzureProvider(
            azure_endpoint=self.settings.azure_endpoint,
            api_version=self.settings.azure_api_version,
            api_key=self.settings.azure_api_key.get_secret_value(),
        )

    def model(self, deployment: str) -> OpenAIChatModel:
        return OpenAIChatModel(deployment, provider=self.provider)

    @cached_property
    def situation(self) -> OpenAIChatModel:
        """Runs every tick. Cheap deployment."""
        return self.model(self.settings.situation_model)

    @cached_property
    def tactics(self) -> OpenAIChatModel:
        """Runs only on meaningful change. Stronger deployment."""
        return self.model(self.settings.tactics_model)
