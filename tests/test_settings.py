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


"""Tests for Settings env file loading."""

from pathlib import Path

import pytest

from aggregator_proxy.settings import Settings


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    """Create a temporary env file with all required settings."""
    p = tmp_path / "test.env"
    p.write_text(
        "AGGREGATOR_PROXY_PROVIDER_URL=https://envfile.example.com/provider\n"
        "AGGREGATOR_PROXY_REQUESTER_NSA=urn:ogf:network:envfile:requester\n"
        "AGGREGATOR_PROXY_PROVIDER_NSA=urn:ogf:network:envfile:provider\n"
        "AGGREGATOR_PROXY_BASE_URL=https://envfile.example.com\n"
        "AGGREGATOR_PROXY_PORT=9999\n",
        encoding="utf-8",
    )
    return p


def test_settings_loaded_from_env_file(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Values from the env file are loaded into Settings."""
    monkeypatch.delenv("AGGREGATOR_PROXY_PROVIDER_URL", raising=False)
    monkeypatch.delenv("AGGREGATOR_PROXY_REQUESTER_NSA", raising=False)
    monkeypatch.delenv("AGGREGATOR_PROXY_PROVIDER_NSA", raising=False)
    monkeypatch.delenv("AGGREGATOR_PROXY_BASE_URL", raising=False)
    monkeypatch.delenv("AGGREGATOR_PROXY_PORT", raising=False)

    s = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert s.provider_url == "https://envfile.example.com/provider"
    assert s.requester_nsa == "urn:ogf:network:envfile:requester"
    assert s.provider_nsa == "urn:ogf:network:envfile:provider"
    assert s.base_url == "https://envfile.example.com"
    assert s.port == 9999


def test_env_var_overrides_env_file(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables take precedence over values in the env file."""
    monkeypatch.delenv("AGGREGATOR_PROXY_PROVIDER_URL", raising=False)
    monkeypatch.delenv("AGGREGATOR_PROXY_REQUESTER_NSA", raising=False)
    monkeypatch.delenv("AGGREGATOR_PROXY_PROVIDER_NSA", raising=False)
    monkeypatch.delenv("AGGREGATOR_PROXY_BASE_URL", raising=False)
    monkeypatch.delenv("AGGREGATOR_PROXY_PORT", raising=False)

    monkeypatch.setenv("AGGREGATOR_PROXY_PORT", "7777")

    s = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert s.port == 7777  # env var wins over file value of 9999


def test_env_file_read_as_utf8(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The env file is read with UTF-8 encoding, supporting non-ASCII characters."""
    monkeypatch.delenv("AGGREGATOR_PROXY_PROVIDER_URL", raising=False)
    monkeypatch.delenv("AGGREGATOR_PROXY_REQUESTER_NSA", raising=False)
    monkeypatch.delenv("AGGREGATOR_PROXY_PROVIDER_NSA", raising=False)
    monkeypatch.delenv("AGGREGATOR_PROXY_BASE_URL", raising=False)

    p = tmp_path / "utf8.env"
    p.write_text(
        "AGGREGATOR_PROXY_PROVIDER_URL=https://example.com/réseau\n"
        "AGGREGATOR_PROXY_REQUESTER_NSA=urn:ogf:network:tëst:requester\n"
        "AGGREGATOR_PROXY_PROVIDER_NSA=urn:ogf:network:tëst:provider\n"
        "AGGREGATOR_PROXY_BASE_URL=https://proxy.example.com\n",
        encoding="utf-8",
    )

    s = Settings(_env_file=p)  # type: ignore[call-arg]

    assert s.provider_url == "https://example.com/réseau"
    assert s.requester_nsa == "urn:ogf:network:tëst:requester"


def test_env_file_encoding_configured() -> None:
    """The Settings model is configured to read env files as UTF-8."""
    assert Settings.model_config.get("env_file_encoding") == "utf-8"
