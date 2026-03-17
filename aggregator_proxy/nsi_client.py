"""Factory for the shared httpx client that talks to the NSI aggregator."""

import httpx
import structlog

from aggregator_proxy.settings import settings

logger = structlog.get_logger(__name__)


def create_nsi_client() -> httpx.AsyncClient:
    """Create an async httpx client configured for mTLS against the aggregator.

    The client is configured with:
    - ``cert`` — the client certificate and private key used for mutual TLS.
    - ``verify`` — the CA bundle used to verify the aggregator's server
      certificate.
    """
    logger.debug(
        "Creating NSI client",
        provider_url=settings.provider_url,
        client_cert=settings.client_cert,
        ca_file=settings.ca_file,
    )
    cert = (
        (str(settings.client_cert), str(settings.client_key)) if settings.client_cert and settings.client_key else None
    )
    verify: str | bool = str(settings.ca_file) if settings.ca_file else True
    return httpx.AsyncClient(
        cert=cert,
        verify=verify,
    )
