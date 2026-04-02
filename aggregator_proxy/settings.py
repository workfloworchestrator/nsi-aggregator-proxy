# Copyright 2026 SURF
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Application settings loaded from environment variables."""

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """NSI Aggregator Proxy settings.

    All settings can be supplied via environment variables with the
    ``AGGREGATOR_PROXY_`` prefix, e.g. ``AGGREGATOR_PROXY_HOST=0.0.0.0``.
    """

    model_config = SettingsConfigDict(
        env_prefix="AGGREGATOR_PROXY_",
        env_file="aggregator_proxy.env",
        env_file_encoding="utf-8",
    )

    # Full URL of the NSI provider endpoint on the aggregator.
    provider_url: str

    # NSA URNs for outbound NSI SOAP headers.
    requester_nsa: str
    provider_nsa: str

    # Client certificate authentication towards the NSI aggregator.
    # When not set, no client certificate is presented.
    client_cert: Path | None = None
    client_key: Path | None = None

    # CA bundle used to verify the NSI aggregator's server certificate.
    # When not set, the system CA bundle is used.
    ca_file: Path | None = None

    # Externally reachable base URL of this proxy (e.g. https://proxy.example.com).
    # Used to construct the replyTo URL in outbound NSI SOAP headers.
    base_url: str

    @field_validator("base_url")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        """Remove trailing slash to avoid double slashes when appending paths."""
        return v.rstrip("/")

    # Timeouts (seconds) for waiting on async NSI callbacks.
    nsi_timeout: int = 180
    dataplane_timeout: int = 300

    log_level: str = "INFO"
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8080


settings = Settings()  # type: ignore[call-arg]
