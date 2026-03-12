"""Application settings loaded from environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """NSI Aggregator Proxy settings.

    All settings can be supplied via environment variables with the
    ``AGGREGATOR_PROXY_`` prefix, e.g. ``AGGREGATOR_PROXY_HOST=0.0.0.0``.
    """

    model_config = SettingsConfigDict(env_prefix="AGGREGATOR_PROXY_")

    aggregator_url: str

    # Client certificate authentication towards the NSI aggregator.
    # When not set, no client certificate is presented.
    client_cert: Path | None = None
    client_key: Path | None = None

    # CA bundle used to verify the NSI aggregator's server certificate.
    # When not set, the system CA bundle is used.
    ca_file: Path | None = None

    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8080


settings = Settings()
