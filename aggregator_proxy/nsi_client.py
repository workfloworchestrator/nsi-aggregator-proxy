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


"""The shared httpx clients for outbound NSI calls, and the SOAP request helpers they share.

``nsi_client`` carries this proxy's own certificate and talks to the aggregator and the dds-proxy;
``upa_client`` carries the aggregator's certificate and talks only to the aggregator's uPAs.
"""

import ssl
from pathlib import Path

import httpx
import structlog

from aggregator_proxy.settings import settings

logger = structlog.get_logger(__name__)

_SOAP_ACTION_BASE = "http://schemas.ogf.org/nsi/2013/12/connection/service"


def soap_headers(operation: str) -> dict[str, str]:
    """Return SOAP HTTP headers with the correct SOAPAction for the given NSI operation."""
    return {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{_SOAP_ACTION_BASE}/{operation}"'}


def raise_for_status(response: httpx.Response, operation: str, **extra: object) -> None:
    """Log response body on error, then raise."""
    if not response.is_success:
        logger.error(
            "NSI request failed",
            operation=operation,
            status_code=response.status_code,
            response_body=response.text,
            **extra,
        )
    response.raise_for_status()


def create_nsi_client() -> httpx.AsyncClient:
    """Create an async httpx client configured for mTLS with this proxy's own certificate.

    Builds an ``ssl.SSLContext`` directly rather than using httpx's deprecated
    ``cert=`` / ``verify=<str>`` parameters.  This ensures the full certificate
    chain (including intermediates) is sent during the TLS handshake, which is
    required by ingress controllers that verify the client certificate against a
    root CA.
    """
    logger.debug(
        "Creating NSI client",
        provider_url=settings.provider_url,
        client_cert=settings.client_cert,
        ca_file=settings.ca_file,
    )
    return httpx.AsyncClient(verify=_ssl_context(settings.client_cert, settings.client_key))


def create_upa_client() -> httpx.AsyncClient | None:
    """Create the client that queries the aggregator's uPAs as the aggregator, or None when unconfigured."""
    if not settings.upa_queries_enabled:
        return None
    logger.debug("Creating uPA client", client_cert=settings.upa_client_cert, ca_file=settings.ca_file)
    return httpx.AsyncClient(verify=_ssl_context(settings.upa_client_cert, settings.upa_client_key))


def _ssl_context(client_cert: Path | None, client_key: Path | None) -> ssl.SSLContext:
    # Server verification is always enabled and is independent of whether we present a client
    # certificate. create_default_context sets CERT_REQUIRED and check_hostname, and falls back to
    # the system trust store when CA_FILE is unset. Paths are validated in Settings, so a missing
    # file fails at startup rather than silently weakening TLS.
    ssl_context = ssl.create_default_context(cafile=settings.ca_file)
    if client_cert and client_key:
        ssl_context.load_cert_chain(certfile=client_cert, keyfile=client_key)
    return ssl_context
