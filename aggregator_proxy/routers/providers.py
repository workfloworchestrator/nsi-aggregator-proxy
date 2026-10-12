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

"""GET /providers/{nsaId}/reservations: every reservation a uPA holds, asked as the aggregator."""

import httpx
import structlog
from fastapi import APIRouter, HTTPException, status

from aggregator_proxy.dds_proxy import fetch_network_service_agents, peered_upa_url
from aggregator_proxy.dependencies import NsiClient, UpaClient
from aggregator_proxy.models import UpaReservation, UpaReservationsResponse
from aggregator_proxy.nsi_client import raise_for_status, soap_headers
from aggregator_proxy.nsi_soap import NsiHeader, QueryReservation, build_query_summary_sync, parse_query_summary_sync
from aggregator_proxy.settings import settings

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/providers", tags=["providers"])


def _to_upa_reservation(qr: QueryReservation) -> UpaReservation:
    states = qr.connection_states
    return UpaReservation(
        connectionId=qr.connection_id,
        globalReservationId=qr.global_reservation_id,
        description=qr.description,
        requesterNSA=qr.requester_nsa,
        reservationState=states.reservation_state,
        provisionState=states.provision_state,
        lifecycleState=states.lifecycle_state,
        dataPlaneActive=states.data_plane_active,
        capacity=qr.capacity,
        sourceSTP=qr.source_stp,
        destSTP=qr.dest_stp,
    )


async def _provider_url(nsi_client: httpx.AsyncClient, nsa_id: str) -> str:
    """The provider URL of ``nsa_id``, which must be a uPA the aggregator peers with."""
    # The settings validator only allows the uPA certificate, which UpaClient requires, with DDS_PROXY_URL.
    assert settings.dds_proxy_url is not None
    try:
        agents = await fetch_network_service_agents(nsi_client, settings.dds_proxy_url)
    except Exception as exc:
        logger.error("Failed to read network service agents from dds-proxy", error=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to reach dds-proxy") from exc
    provider_url = peered_upa_url(agents, settings.provider_nsa, nsa_id)
    if provider_url is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{nsa_id} is not a uPA with a provider URL that {settings.provider_nsa} peers with",
        )
    return provider_url


@router.get(
    "/{nsaId}/reservations",
    response_model=UpaReservationsResponse,
    operation_id="get_provider_reservations",
)
async def get_provider_reservations(
    nsaId: str, nsi_client: NsiClient, upa_client: UpaClient
) -> UpaReservationsResponse:
    """Return every reservation a uPA holds, whatever its requester, by an unfiltered querySummarySync.

    The query is sent with the aggregator's NSA id and client certificate, so the uPA answers as it
    would the aggregator. Only uPAs the aggregator peers with, per the DDS, are queried.
    """
    provider_url = await _provider_url(nsi_client, nsaId)
    soap_bytes = build_query_summary_sync(NsiHeader(requester_nsa=settings.provider_nsa, provider_nsa=nsaId))
    log = logger.bind(provider_nsa=nsaId, provider_url=provider_url)
    log.debug("Outbound SOAP querySummarySync (uPA) request", xml=soap_bytes.decode())
    try:
        response = await upa_client.post(provider_url, content=soap_bytes, headers=soap_headers("querySummarySync"))
        raise_for_status(response, "querySummarySync", provider_nsa=nsaId, provider_url=provider_url)
        log.debug("Inbound SOAP querySummarySyncConfirmed (uPA) response", xml=response.text)
        reservations = parse_query_summary_sync(response.content)
    except Exception as exc:
        log.error("Failed to query uPA", error=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to query {nsaId}") from exc
    log.info("uPA returned reservations", count=len(reservations))
    return UpaReservationsResponse(
        providerNSA=nsaId,
        providerURL=provider_url,
        reservations=[_to_upa_reservation(qr) for qr in reservations],
    )
