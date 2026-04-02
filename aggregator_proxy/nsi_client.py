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


"""Factory for the shared httpx client that talks to the NSI aggregator."""

import ssl

import httpx
import structlog

from aggregator_proxy.settings import settings

logger = structlog.get_logger(__name__)


def create_nsi_client() -> httpx.AsyncClient:
    """Create an async httpx client configured for mTLS against the aggregator.

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
    if settings.ca_file:
        ssl_context = ssl.create_default_context(cafile=str(settings.ca_file))
    else:
        ssl_context = ssl.create_default_context()
    if settings.client_cert and settings.client_key:
        ssl_context.load_cert_chain(certfile=str(settings.client_cert), keyfile=str(settings.client_key))
    return httpx.AsyncClient(verify=ssl_context)
