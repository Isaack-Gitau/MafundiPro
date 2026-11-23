from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    app_name: str = "MafundiPro"
    app_environment: str = "development"
    app_debug: bool = False
    api_v1_prefix: str = "/api/v1"

    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_db: str
    postgres_user: str
    postgres_password: str

    auth_secret_key: SecretStr

    session_idle_timeout_minutes: int = 30
    session_absolute_timeout_days: int = 7
    session_cookie_name: str = "mafundipro_session"
    session_cookie_secure: bool = False
    session_cookie_same_site: Literal["lax", "strict", "none"] = "lax"
    csrf_cookie_name: str = "mafundipro_csrf"
    frontend_origins: str = ""

    login_rate_limit_username_ip_attempts: int = Field(default=10, ge=1)
    login_rate_limit_ip_attempts: int = Field(default=30, ge=1)
    login_rate_limit_window_minutes: int = Field(default=15, ge=1)

    payment_provider: str = "fake"
    payment_callback_secret: SecretStr | None = None

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def allowed_frontend_origins(self) -> tuple[str, ...]:
        """Return configured frontend origins as a normalized list of entries."""
        return tuple(
            origin.strip() for origin in self.frontend_origins.split(",") if origin.strip()
        )

    @property
    def database_url(self) -> URL:
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
