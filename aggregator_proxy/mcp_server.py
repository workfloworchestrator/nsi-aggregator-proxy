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


"""FastMCP sub-app builder exposing GET /reservations as MCP Tools."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.providers.openapi import MCPType, RouteMap

from aggregator_proxy.auth import GROUPS_HEADER, USER_HEADER
from aggregator_proxy.settings import settings

_DETAIL_PATTERN = r"^/reservations/\{[^}]+\}$"
_LIST_PATTERN = r"^/reservations$"


def _serialize_groups(groups: object) -> str | None:
    """Coerce the JWT's groups claim into a comma-separated string.

    Returns ``None`` when there's nothing meaningful to forward (missing claim,
    empty list, empty string, or unexpected shape) so the caller does not emit
    an empty ``X-Auth-Request-Groups`` header.
    """
    match groups:
        case list() if groups:
            return ",".join(str(g) for g in groups)
        case str() if groups:
            return groups
        case _:
            return None


async def _forward_user_identity(request: httpx.Request) -> None:
    """Translate the MCP client's validated JWT into trusted identity headers.

    Reads the validated claims via fastmcp's ``get_access_token``. The hook only
    acts when ``mcp_auth_enabled`` is true; otherwise fastmcp has not validated
    anything and the claims must not be propagated. The cross-field Settings
    validator already refuses this combination at startup, but the runtime
    guard preserves the invariant as defense-in-depth.
    """
    if not settings.mcp_auth_enabled:
        return
    token = get_access_token()
    if token is None:
        return
    email = token.claims.get(settings.mcp_oidc_email_claim)
    if email:
        request.headers[USER_HEADER] = str(email)
    groups_header = _serialize_groups(token.claims.get(settings.mcp_oidc_groups_claim, []))
    if groups_header is not None:
        request.headers[GROUPS_HEADER] = groups_header
    # REST trusts only X-Auth-Request-* headers; strip Authorization so it
    # cannot be mistaken for an auth signal anywhere downstream.
    request.headers.pop("Authorization", None)


def _build_auth() -> AuthProvider | None:
    """Build an MCP-level OIDC auth provider, or None if MCP auth is disabled.

    Returns a ``JWTVerifier`` that validates incoming MCP request tokens against the
    configured MCP OIDC issuer/audience using the MCP JWKS endpoint. ``JWTVerifier``
    is itself an ``AuthProvider``, so it can be passed directly to ``FastMCP`` as
    ``auth``.
    """
    if not settings.mcp_auth_enabled:
        return None
    return JWTVerifier(
        jwks_uri=settings.mcp_oidc_jwks_uri,
        issuer=settings.mcp_oidc_issuer,
        audience=settings.mcp_oidc_audience,
    )


def build_mcp(api: FastAPI) -> FastMCP:
    """Build a FastMCP server from the given FastAPI app, exposing only GET /reservations as Tools.

    Both GET endpoints are mapped to ``MCPType.TOOL`` rather than Resources: the
    default FastMCP surface is tools, and many LLM clients (e.g. Claude Desktop)
    only support the tools half of the MCP spec, so resources would be unreachable
    from them. Everything else is excluded.
    """
    httpx_kwargs: dict[str, Any] = {}
    if settings.proxy_auth_enabled:
        httpx_kwargs["event_hooks"] = {"request": [_forward_user_identity]}

    return FastMCP.from_fastapi(
        app=api,
        name="NSI Aggregator Proxy",
        auth=_build_auth(),
        route_maps=[
            RouteMap(methods=["GET"], pattern=_DETAIL_PATTERN, mcp_type=MCPType.TOOL),
            RouteMap(methods=["GET"], pattern=_LIST_PATTERN, mcp_type=MCPType.TOOL),
            RouteMap(mcp_type=MCPType.EXCLUDE),
        ],
        httpx_client_kwargs=httpx_kwargs,
    )
