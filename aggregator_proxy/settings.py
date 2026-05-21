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

import json
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """NSI Aggregator Proxy settings.

    All settings can be supplied via environment variables — e.g. ``HOST=0.0.0.0``.
    """

    model_config = SettingsConfigDict(
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
    root_path: str = ""

    # Authentication for the REST API and the SOAP callback endpoint. Reads
    # identity headers set by the edge proxy (oauth2-proxy on the portal route,
    # nsi-auth on the mTLS route). When false, all requests pass through.
    proxy_auth_enabled: bool = False
    mtls_header: str = ""
    oidc_required_groups: list[str] = []

    # MCP server settings. The MCP endpoint is a separate authentication surface
    # that uses fastmcp's JWTVerifier against a (potentially different) OIDC
    # provider. mcp_oidc_* settings configure that verifier; mcp_oidc_*_claim
    # tells the claim-translation hook which JWT claims carry the user's email
    # and group memberships, which the hook then forwards to the REST layer as
    # X-Auth-Request-Email and X-Auth-Request-Groups.
    mcp_enabled: bool = False
    mcp_path: str = "/mcp"
    mcp_auth_enabled: bool = False
    mcp_oidc_jwks_uri: str = ""
    mcp_oidc_issuer: str = ""
    mcp_oidc_audience: str = ""
    mcp_oidc_email_claim: str = "email"
    mcp_oidc_groups_claim: str = "groups"

    @field_validator("oidc_required_groups", mode="before")
    @classmethod
    def parse_comma_separated_groups(cls, v: object) -> object:
        """Accept both JSON arrays and comma-separated strings."""
        if not isinstance(v, str):
            return v
        if not v:
            return []
        if v.startswith("["):
            try:
                return json.loads(v)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON in OIDC_REQUIRED_GROUPS: {e}") from e
        return [g.strip() for g in v.split(",") if g.strip()]

    @field_validator("mcp_path")
    @classmethod
    def validate_mcp_path(cls, v: str) -> str:
        """Require a leading slash and reject trailing slash so app.mount behaves predictably."""
        if not v.startswith("/"):
            raise ValueError("MCP_PATH must start with '/'")
        if len(v) > 1 and v.endswith("/"):
            raise ValueError("MCP_PATH must not end with '/'")
        return v

    @model_validator(mode="after")
    def _require_mcp_auth_when_proxy_auth_enabled(self) -> "Settings":
        """Refuse a combination that would let unverified MCP claims reach REST.

        The MCP claim-translation hook (mcp_server._forward_user_identity) extracts
        email and groups from the incoming JWT without re-verifying its signature —
        it trusts that fastmcp.JWTVerifier already validated. If MCP_AUTH_ENABLED is
        false while PROXY_AUTH_ENABLED and MCP_ENABLED are true, fastmcp validates
        nothing, the hook would translate arbitrary attacker-supplied claims into
        trusted X-Auth-Request-* headers, and REST would accept them — a
        privilege-escalation path. Fail fast at startup instead.
        """
        if self.proxy_auth_enabled and self.mcp_enabled and not self.mcp_auth_enabled:
            raise ValueError(
                "MCP_AUTH_ENABLED must be true when PROXY_AUTH_ENABLED and MCP_ENABLED are both true — "
                "otherwise the MCP claim-translation hook would translate unverified JWT claims into "
                "trusted REST headers."
            )
        return self


settings = Settings()  # type: ignore[call-arg]
