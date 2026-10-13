import json
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    secret_key: str = "local-only-change-me"
    demo_username: str = "engineer01"
    demo_password: str = "change-me-before-demo"
    cors_origins: list[str] = ["http://localhost:3000"]
    cookie_secure: bool = False
    cookie_samesite: str = "lax"
    session_ttl_minutes: int = 8 * 60
    # Server-only configuration. Never expose the Agent key to browser code.
    agent_invoke_url: str = ""
    # Optional buffered endpoint for structured generation. When empty, the
    # backend derives /invoke from AGENT_INVOKE_URL when it ends in /stream.
    agent_generation_invoke_url: str = ""
    agent_api_key: SecretStr = SecretStr("")
    # Separate server-to-server credential used when Agent calls read-only backend contracts.
    agent_callback_key: SecretStr = SecretStr("")
    agent_timeout_seconds: float = Field(default=300, gt=0, le=300)
    # FR batches may require longer than interactive Agent tasks.
    agent_fr_timeout_seconds: float = Field(default=600, gt=0, le=600)

    # Opt-in diagnostics. Never reuse AGENT_API_KEY or SECRET_KEY here.
    diagnostics_enabled: bool = False
    diagnostics_key: SecretStr = SecretStr("")
    diagnostics_project_ids: list[str] = []

    # Separate business DB credential; no default connection to any server.
    projects_database_url: SecretStr = SecretStr("")
    projects_connect_timeout_seconds: int = Field(default=3, ge=1, le=10)
    # Creation is opt-in; the SELECT-only DSN remains unchanged.
    projects_write_database_url: SecretStr = SecretStr("")
    projects_create_subjects: list[str] = []
    business_writes_enabled: bool = False
    # Global account administration is opt-in; project managers only manage project membership.
    account_admin_subjects: list[str] = []
    projects_write_origins: list[str] = [
        "http://127.0.0.1:3000", "http://localhost:3000",
        "http://127.0.0.1:8000", "http://localhost:8000",
    ]

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        enable_decoding=False,
    )

    @field_validator("cors_origins", "projects_create_subjects", "projects_write_origins", "account_admin_subjects", "diagnostics_project_ids", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, list):
            return value
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [str(parsed).strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()



