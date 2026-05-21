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


"""Tests for Settings env file loading and cross-field validation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from aggregator_proxy.settings import Settings

# ---------------------------------------------------------------------------
# Env file loading
# ---------------------------------------------------------------------------


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    """Create a temporary env file with all required settings."""
    p = tmp_path / "test.env"
    p.write_text(
        "PROVIDER_URL=https://envfile.example.com/provider\n"
        "REQUESTER_NSA=urn:ogf:network:envfile:requester\n"
        "PROVIDER_NSA=urn:ogf:network:envfile:provider\n"
        "BASE_URL=https://envfile.example.com\n"
        "PORT=9999\n",
        encoding="utf-8",
    )
    return p


def _clear_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PROVIDER_URL", "REQUESTER_NSA", "PROVIDER_NSA", "BASE_URL", "PORT"):
        monkeypatch.delenv(name, raising=False)


def test_settings_loaded_from_env_file(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Values from the env file are loaded into Settings."""
    _clear_required_env(monkeypatch)
    s = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert s.provider_url == "https://envfile.example.com/provider"
    assert s.requester_nsa == "urn:ogf:network:envfile:requester"
    assert s.provider_nsa == "urn:ogf:network:envfile:provider"
    assert s.base_url == "https://envfile.example.com"
    assert s.port == 9999


def test_env_var_overrides_env_file(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables take precedence over values in the env file."""
    _clear_required_env(monkeypatch)
    monkeypatch.setenv("PORT", "7777")
    s = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert s.port == 7777  # env var wins over file value of 9999


def test_env_file_read_as_utf8(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The env file is read with UTF-8 encoding, supporting non-ASCII characters."""
    _clear_required_env(monkeypatch)
    p = tmp_path / "utf8.env"
    p.write_text(
        "PROVIDER_URL=https://example.com/réseau\n"
        "REQUESTER_NSA=urn:ogf:network:tëst:requester\n"
        "PROVIDER_NSA=urn:ogf:network:tëst:provider\n"
        "BASE_URL=https://proxy.example.com\n",
        encoding="utf-8",
    )
    s = Settings(_env_file=p)  # type: ignore[call-arg]
    assert s.provider_url == "https://example.com/réseau"
    assert s.requester_nsa == "urn:ogf:network:tëst:requester"


def test_env_file_encoding_configured() -> None:
    """The Settings model is configured to read env files as UTF-8."""
    assert Settings.model_config.get("env_file_encoding") == "utf-8"


# ---------------------------------------------------------------------------
# MCP settings defaults & validation
# ---------------------------------------------------------------------------


def test_mcp_settings_defaults() -> None:
    """MCP settings default to disabled with the standard /mcp path."""
    s = Settings()  # type: ignore[call-arg]
    assert s.mcp_enabled is False
    assert s.mcp_path == "/mcp"
    assert s.mcp_auth_enabled is False
    assert s.mcp_oidc_email_claim == "email"
    assert s.mcp_oidc_groups_claim == "groups"


def test_mcp_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP settings can be overridden via bare MCP_* env vars."""
    monkeypatch.setenv("MCP_ENABLED", "true")
    monkeypatch.setenv("MCP_PATH", "/custom-mcp")
    monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
    monkeypatch.setenv("MCP_OIDC_EMAIL_CLAIM", "preferred_username")
    monkeypatch.setenv("MCP_OIDC_GROUPS_CLAIM", "entitlements")

    s = Settings()  # type: ignore[call-arg]
    assert s.mcp_enabled is True
    assert s.mcp_path == "/custom-mcp"
    assert s.mcp_auth_enabled is True
    assert s.mcp_oidc_email_claim == "preferred_username"
    assert s.mcp_oidc_groups_claim == "entitlements"


@pytest.mark.parametrize(
    ("value", "expected_msg"),
    [
        pytest.param("mcp", "MCP_PATH must start with '/'", id="missing-leading-slash"),
        pytest.param("/mcp/", "MCP_PATH must not end with '/'", id="trailing-slash"),
    ],
)
def test_mcp_path_rejects(value: str, expected_msg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid MCP_PATH values fail validation."""
    monkeypatch.setenv("MCP_PATH", value)
    with pytest.raises(ValueError, match=expected_msg):
        Settings()  # type: ignore[call-arg]


def test_mcp_path_root_slash_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare '/' is accepted (mounting at root); only longer paths must lack a trailing slash."""
    monkeypatch.setenv("MCP_PATH", "/")
    s = Settings()  # type: ignore[call-arg]
    assert s.mcp_path == "/"


# ---------------------------------------------------------------------------
# Cross-field model validator: MCP auth must follow REST auth when both are mounted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("proxy_auth_enabled", "mcp_enabled", "mcp_auth_enabled", "should_raise"),
    [
        pytest.param(True, True, True, False, id="all-three-on-valid"),
        pytest.param(True, True, False, True, id="rest-on-mcp-on-mcp-auth-off-FORBIDDEN"),
        pytest.param(True, False, False, False, id="rest-on-mcp-off-valid"),
        pytest.param(False, True, False, False, id="rest-off-mcp-on-mcp-auth-off-valid"),
        pytest.param(False, True, True, False, id="rest-off-mcp-on-mcp-auth-on-valid"),
        pytest.param(False, False, False, False, id="all-off-valid"),
    ],
)
def test_mcp_auth_invariant(
    proxy_auth_enabled: bool,
    mcp_enabled: bool,
    mcp_auth_enabled: bool,
    should_raise: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROXY_AUTH_ENABLED", "true" if proxy_auth_enabled else "false")
    monkeypatch.setenv("MCP_ENABLED", "true" if mcp_enabled else "false")
    monkeypatch.setenv("MCP_AUTH_ENABLED", "true" if mcp_auth_enabled else "false")
    if should_raise:
        with pytest.raises(ValidationError) as info:
            Settings()  # type: ignore[call-arg]
        msg = str(info.value)
        assert "MCP_AUTH_ENABLED" in msg
        assert "PROXY_AUTH_ENABLED" in msg
        assert "MCP_ENABLED" in msg
    else:
        Settings()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# oidc_required_groups parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        pytest.param('["g1","g2"]', ["g1", "g2"], id="json-array"),
        pytest.param("g1,g2,g3", ["g1", "g2", "g3"], id="comma-separated"),
        pytest.param("single-group", ["single-group"], id="single-value"),
        pytest.param("", [], id="empty-string"),
    ],
)
def test_required_groups_parsing(env_value: str, expected: list[str]) -> None:
    assert Settings(oidc_required_groups=env_value).oidc_required_groups == expected  # type: ignore[call-arg]


def test_malformed_json_groups_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="Invalid JSON"):
        Settings(oidc_required_groups='["g1", invalid')  # type: ignore[call-arg]
