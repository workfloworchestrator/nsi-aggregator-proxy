# Copyright 2026 SURF
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for MCP authentication wiring."""

from __future__ import annotations

import pytest

from aggregator_proxy.main import app


async def test_event_hook_registered_when_auth_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """When auth_enabled=true, build_mcp must wire the token-forwarding event hook."""
    from aggregator_proxy import mcp_server
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "mcp_auth_enabled", False)

    captured: dict = {}

    real_from_fastapi = mcp_server.FastMCP.from_fastapi

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["httpx_client_kwargs"] = kwargs.get("httpx_client_kwargs", {})
        return real_from_fastapi(*args, **kwargs)

    monkeypatch.setattr(mcp_server.FastMCP, "from_fastapi", spy)

    mcp_server.build_mcp(app)

    hooks = captured["httpx_client_kwargs"].get("event_hooks", {})
    assert "request" in hooks, "expected request event hook to be registered"
    assert len(hooks["request"]) == 1


async def test_no_event_hook_when_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """When auth_enabled=false, no event hook is needed."""
    from aggregator_proxy import mcp_server
    from aggregator_proxy.settings import settings

    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "mcp_auth_enabled", False)

    captured: dict = {}

    real_from_fastapi = mcp_server.FastMCP.from_fastapi

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["httpx_client_kwargs"] = kwargs.get("httpx_client_kwargs", {})
        return real_from_fastapi(*args, **kwargs)

    monkeypatch.setattr(mcp_server.FastMCP, "from_fastapi", spy)

    mcp_server.build_mcp(app)

    hooks = captured["httpx_client_kwargs"].get("event_hooks", {})
    assert "request" not in hooks or not hooks["request"]
