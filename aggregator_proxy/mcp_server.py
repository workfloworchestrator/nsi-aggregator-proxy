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


"""FastMCP sub-app builder exposing GET /reservations as MCP Resources."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.context import request_ctx
from fastmcp.server.providers.openapi import MCPType, RouteMap

from aggregator_proxy.settings import settings

_DETAIL_PATTERN = r"^/reservations/\{[^}]+\}$"
_LIST_PATTERN = r"^/reservations$"


async def _forward_user_token(request: httpx.Request) -> None:
    """Copy the MCP client's Authorization header onto the internal /reservations call."""
    ctx = request_ctx.get(None)
    if ctx is None:
        return
    incoming = getattr(ctx, "request", None)
    if incoming is None:
        return
    token = incoming.headers.get("authorization")
    if token:
        request.headers["Authorization"] = token


def _build_auth() -> AuthProvider | None:
    """Build an MCP-level OIDC auth provider, or None if MCP auth is disabled.

    Returns a ``JWTVerifier`` that validates incoming MCP request tokens against the
    configured OIDC issuer/audience using the JWKS endpoint. ``JWTVerifier`` is itself
    an ``AuthProvider``, so it can be passed directly to ``FastMCP`` as ``auth``.
    """
    if not settings.mcp_auth_enabled:
        return None
    return JWTVerifier(
        jwks_uri=settings.oidc_jwks_uri,
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
    )


def build_mcp(api: FastAPI) -> FastMCP:
    """Build a FastMCP server from the given FastAPI app, exposing only GET /reservations."""
    httpx_kwargs: dict[str, Any] = {}
    if settings.auth_enabled:
        httpx_kwargs["event_hooks"] = {"request": [_forward_user_token]}

    return FastMCP.from_fastapi(
        app=api,
        name="NSI Aggregator Proxy",
        auth=_build_auth(),
        route_maps=[
            RouteMap(methods=["GET"], pattern=_DETAIL_PATTERN, mcp_type=MCPType.RESOURCE_TEMPLATE),
            RouteMap(methods=["GET"], pattern=_LIST_PATTERN, mcp_type=MCPType.RESOURCE),
            RouteMap(mcp_type=MCPType.EXCLUDE),
        ],
        httpx_client_kwargs=httpx_kwargs,
    )
