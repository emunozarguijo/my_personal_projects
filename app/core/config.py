"""Configuración centralizada. Los secretos se leen de variables de entorno / .env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # App / logging
    app_name: str = "estado-cuenta-ai"
    app_env: Literal["dev", "prod", "test"] = "dev"
    log_level: str = "INFO"
    log_dir: Path = Path("logs")

    # Base de datos
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "postgres"
    db_password: SecretStr = SecretStr("")
    db_name: str = "estado_cuenta"
    db_schema: str | None = None          # esquema donde viven las tablas (p. ej. uno privado en Neon)
    db_sslmode: str = "prefer"            # Neon requiere "require"
    database_url: SecretStr | None = None

    # LLM
    llm_provider: Literal["anthropic", "openai"] = "anthropic"
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5-5"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o-mini"
    llm_max_tokens: int = 1024
    llm_temperature: float | None = None   # opcional: algunos modelos/SDK no aceptan este parámetro
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 3
    price_input_per_mtok: float = 0.0
    price_output_per_mtok: float = 0.0

    # Embeddings
    embedding_provider: Literal["local", "openai", "fake"] = "local"
    embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    openai_embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 384

    # RAG
    rag_top_k: int = 6
    chunk_tx_per_chunk: int = 10
    redact_pii: bool = True

    # Archivos
    max_pdf_mb: int = 20
    upload_dir: Path = Path("archivos_subidos")

    # Cuentas y sesiones
    session_ttl_hours: int = 24 * 7
    cookie_secure: bool | None = None       # None → True en prod (HTTPS), False en dev
    signup_invite_code: SecretStr | None = None   # si está definido, el registro lo exige
    login_max_attempts: int = 5
    login_window_minutes: int = 15
    local_user: str = "local"               # usuario con el que operan la GUI y la CLI
    admin_username: str = "admin"           # dueño de los datos previos a la migración multiusuario
    admin_password: SecretStr | None = None
    expose_docs: bool | None = None         # None → /docs solo fuera de prod

    # Límites de uso (0 = sin límite)
    max_users: int = 50
    max_statements_per_user: int = 24
    max_asks_per_day: int = 30
    max_uploads_per_day: int = 10

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def secure_cookies(self) -> bool:
        return self.is_prod if self.cookie_secure is None else self.cookie_secure

    @property
    def docs_enabled(self) -> bool:
        return not self.is_prod if self.expose_docs is None else self.expose_docs

    @property
    def sqlalchemy_url(self) -> str | URL:
        if self.database_url:
            return self.database_url.get_secret_value()
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password.get_secret_value() or None,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
            query={"sslmode": self.db_sslmode},
        )

    @property
    def llm_model(self) -> str:
        return self.anthropic_model if self.llm_provider == "anthropic" else self.openai_model


@lru_cache
def get_settings() -> Settings:
    return Settings()
