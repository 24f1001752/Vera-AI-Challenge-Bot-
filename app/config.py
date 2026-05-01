from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VERA_", env_file=".env", extra="ignore")

    team_name: str = "Local Test Bot"
    team_members: str = "anish"
    contact_email: str = "local@example.com"
    version: str = "0.1.0"
    model: str = "deterministic_rules_v1"
    approach: str = "Deterministic rules-based composer with grounded facts and stable tie-breakers."


settings = Settings()

