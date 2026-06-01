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


"""Tests that build_mcp exposes only the two GET /reservations operations as Tools."""

from __future__ import annotations

import pytest
from fastmcp import Client

from aggregator_proxy.main import app
from aggregator_proxy.mcp_server import build_mcp


@pytest.fixture()
def _mcp_disabled_auth(monkeypatch) -> None:
    """Ensure auth flags are off so build_mcp can be constructed without OIDC config."""
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "proxy_auth_enabled", False)
    monkeypatch.setattr(settings, "mcp_auth_enabled", False)


async def test_only_get_reservations_are_exposed(_mcp_disabled_auth) -> None:
    mcp = build_mcp(app)
    async with Client(mcp) as client:
        resources = await client.list_resources()
        templates = await client.list_resource_templates()
        tools = await client.list_tools()

    resource_names = {r.name for r in resources}
    template_names = {t.name for t in templates}
    tool_names = {t.name for t in tools}

    assert tool_names == {"list_reservations", "get_reservation"}
    assert resource_names == set(), f"expected no resources but got: {resource_names}"
    assert template_names == set(), f"expected no resource templates but got: {template_names}"
