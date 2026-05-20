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

from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType, RouteMap

_DETAIL_PATTERN = r"^/reservations/\{[^}]+\}$"
_LIST_PATTERN = r"^/reservations$"


def build_mcp(api: FastAPI) -> FastMCP:
    """Build a FastMCP server from the given FastAPI app, exposing only GET /reservations."""
    return FastMCP.from_fastapi(
        app=api,
        name="NSI Aggregator Proxy",
        route_maps=[
            RouteMap(methods=["GET"], pattern=_DETAIL_PATTERN, mcp_type=MCPType.RESOURCE_TEMPLATE),
            RouteMap(methods=["GET"], pattern=_LIST_PATTERN, mcp_type=MCPType.RESOURCE),
            RouteMap(mcp_type=MCPType.EXCLUDE),
        ],
    )
