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


"""Tests for the SSL context built by create_nsi_client.

Server verification is always enabled and independent of whether a client certificate is
configured; the client certificate is only loaded when both cert and key are present.
"""

import ssl
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from aggregator_proxy.nsi_client import create_nsi_client

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cert_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create temporary cert, key, and CA bundle files."""
    cert = tmp_path / "client.pem"
    key = tmp_path / "client.key"
    ca = tmp_path / "ca-bundle.pem"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    ca.write_text("ca", encoding="utf-8")
    return cert, key, ca


def capture_verify_argument(**setting_overrides: object) -> object:
    """Call create_nsi_client with stub settings and return the ``verify`` argument used."""
    captured: list[object] = []
    values: dict[str, object] = {
        "provider_url": "https://aggregator.example.com/nsi-v2/ConnectionServiceProvider",
        "client_cert": None,
        "client_key": None,
        "ca_file": None,
    }
    values.update(setting_overrides)
    stub = SimpleNamespace(**values)

    def capturing_async_client(**kwargs: object) -> MagicMock:
        captured.append(kwargs.get("verify"))
        return MagicMock()

    with (
        patch("aggregator_proxy.nsi_client.settings", stub),
        patch("aggregator_proxy.nsi_client.httpx.AsyncClient", side_effect=capturing_async_client),
    ):
        create_nsi_client()
    return captured[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_verifying_context_used_without_client_certificate() -> None:
    """The aggregator's certificate is verified even when no client certificate is configured."""
    verify = capture_verify_argument()

    assert isinstance(verify, ssl.SSLContext)
    assert verify.verify_mode == ssl.CERT_REQUIRED
    assert verify.check_hostname is True


@pytest.mark.parametrize(
    "with_ca_file",
    [
        pytest.param(True, id="ca-file-set"),
        pytest.param(False, id="ca-file-unset-uses-system-trust-store"),
    ],
)
def test_ca_file_passed_to_create_default_context(cert_files: tuple[Path, Path, Path], with_ca_file: bool) -> None:
    """CA_FILE is handed to create_default_context, which falls back to the system store when unset."""
    _, _, ca = cert_files
    expected = ca if with_ca_file else None

    with patch(
        "aggregator_proxy.nsi_client.ssl.create_default_context",
        return_value=MagicMock(spec=ssl.SSLContext),
    ) as create:
        capture_verify_argument(ca_file=expected)

    create.assert_called_once_with(cafile=expected)


def test_client_cert_loaded_when_cert_and_key_are_set(cert_files: tuple[Path, Path, Path]) -> None:
    """Both halves of the client certificate are loaded into the context."""
    cert, key, ca = cert_files
    mock_ctx = MagicMock(spec=ssl.SSLContext)

    with patch("aggregator_proxy.nsi_client.ssl.create_default_context", return_value=mock_ctx):
        capture_verify_argument(client_cert=cert, client_key=key, ca_file=ca)

    mock_ctx.load_cert_chain.assert_called_once_with(certfile=cert, keyfile=key)


@pytest.mark.parametrize(
    ("with_cert", "with_key"),
    [
        pytest.param(False, False, id="neither-set"),
        pytest.param(True, False, id="cert-without-key"),
        pytest.param(False, True, id="key-without-cert"),
    ],
)
def test_client_cert_not_loaded_when_incomplete(
    cert_files: tuple[Path, Path, Path], with_cert: bool, with_key: bool
) -> None:
    """A half-configured client certificate is not loaded; the connection stays anonymous."""
    cert, key, _ = cert_files
    mock_ctx = MagicMock(spec=ssl.SSLContext)

    with patch("aggregator_proxy.nsi_client.ssl.create_default_context", return_value=mock_ctx):
        capture_verify_argument(
            client_cert=cert if with_cert else None,
            client_key=key if with_key else None,
        )

    mock_ctx.load_cert_chain.assert_not_called()
