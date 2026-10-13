"""Tests for GET /providers and GET /providers/{nsaId}/reservations."""

from collections.abc import Sequence

import httpx
import pytest
from fastapi.testclient import TestClient
from lxml import etree

from aggregator_proxy.dds_proxy import UPA_ROLE, NetworkServiceAgent, peered_upas
from aggregator_proxy.main import app
from aggregator_proxy.nsi_soap import parse_correlation_id
from aggregator_proxy.settings import settings
from tests.conftest import build_query_summary_sync_response

DDS_PROXY_URL = "http://dds-proxy.test"
UPA_NSA = "urn:ogf:network:example.net:2025:nsa:upa"
UPA_URL = "https://upa.test/soap/connection/provider"
OTHER_UPA_NSA = "urn:ogf:network:other.net:2025:nsa:upa"
AGGREGATOR_ROLE = "vnd.ogf.nsi.cs.v2.role.aggregator"


def _agent(nsa_id: str, provider_url: str | None, roles: list[str], peers_with: Sequence[str] = ()) -> dict:
    return {
        "id": nsa_id,
        "name": nsa_id,
        "providerUrl": provider_url,
        "networkIds": [f"{nsa_id}:network"],
        "roles": roles,
        "peersWith": list(peers_with),
    }


AGENTS = [
    _agent(settings.provider_nsa, "https://aggregator.test/provider", [AGGREGATOR_ROLE], [UPA_NSA]),
    _agent(UPA_NSA, UPA_URL, [UPA_ROLE]),
    _agent(OTHER_UPA_NSA, "https://other.test/provider", [UPA_ROLE]),
]


def _dds_handler(request: httpx.Request) -> httpx.Response:
    assert request.url == f"{DDS_PROXY_URL}/network-service-agents"
    return httpx.Response(200, json=AGENTS)


def _failing_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("down")


class _UpaHandler:
    """Answers querySummarySync like a uPA and records what it was sent."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            content=build_query_summary_sync_response(
                connection_id="upa-conn-1",
                correlation_id=parse_correlation_id(request.content),
                reservation_state="ReserveStart",
                provision_state="Provisioned",
                lifecycle_state="Created",
                data_plane_active=True,
                source_stp="urn:ogf:network:example.net:2025:port-a?vlan=2300",
                dest_stp="urn:ogf:network:example.net:2025:port-b?vlan=2300",
                global_reservation_id="urn:uuid:550e8400-e29b-41d4-a716-446655440000",
            ),
        )


@pytest.fixture()
def upa() -> _UpaHandler:
    return _UpaHandler()


@pytest.fixture()
def client(upa: _UpaHandler, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "dds_proxy_url", DDS_PROXY_URL)
    dds_client = httpx.AsyncClient(transport=httpx.MockTransport(_dds_handler))
    monkeypatch.setattr(app.state, "nsi_client", dds_client, raising=False)
    monkeypatch.setattr(app.state, "upa_client", httpx.AsyncClient(transport=httpx.MockTransport(upa)), raising=False)
    return TestClient(app, raise_server_exceptions=False)


def test_returns_every_reservation_with_raw_states(client: TestClient, upa: _UpaHandler) -> None:
    resp = client.get(f"/providers/{UPA_NSA}/reservations")

    assert resp.status_code == 200
    assert resp.json() == {
        "providerNSA": UPA_NSA,
        "providerURL": UPA_URL,
        "reservations": [
            {
                "connectionId": "upa-conn-1",
                "globalReservationId": "urn:uuid:550e8400-e29b-41d4-a716-446655440000",
                "description": "test reservation",
                "requesterNSA": "urn:ogf:network:example.net:2025:nsa:requester",
                "reservationState": "ReserveStart",
                "provisionState": "Provisioned",
                "lifecycleState": "Created",
                "dataPlaneActive": True,
                "capacity": 1000,
                "sourceSTP": "urn:ogf:network:example.net:2025:port-a?vlan=2300",
                "destSTP": "urn:ogf:network:example.net:2025:port-b?vlan=2300",
            }
        ],
    }
    [request] = upa.requests
    assert request.url == UPA_URL


def test_query_is_unfiltered_and_sent_as_the_aggregator(client: TestClient, upa: _UpaHandler) -> None:
    client.get(f"/providers/{UPA_NSA}/reservations")

    [request] = upa.requests
    root = etree.fromstring(request.content)
    query = root.find(".//{*}querySummarySync")
    assert query is not None and len(query) == 0
    assert root.findtext(".//{*}nsiHeader/requesterNSA") == settings.provider_nsa
    assert root.findtext(".//{*}nsiHeader/providerNSA") == UPA_NSA
    assert root.find(".//{*}nsiHeader/replyTo") is None
    assert request.headers["SOAPAction"].endswith('/querySummarySync"')


@pytest.mark.parametrize(
    "nsa_id",
    [
        pytest.param(OTHER_UPA_NSA, id="not-peered"),
        pytest.param(settings.provider_nsa, id="aggregator-itself"),
        pytest.param("urn:ogf:network:unknown.net:2025:nsa", id="unknown"),
    ],
)
def test_nsa_that_is_not_a_peered_upa_is_never_queried(client: TestClient, upa: _UpaHandler, nsa_id: str) -> None:
    resp = client.get(f"/providers/{nsa_id}/reservations")

    assert resp.status_code == 404
    assert upa.requests == []


def test_lists_only_the_peered_upas(client: TestClient) -> None:
    resp = client.get("/providers")

    assert resp.status_code == 200
    assert resp.json() == {
        "providers": [{"nsaId": UPA_NSA, "providerURL": UPA_URL, "networkIds": [f"{UPA_NSA}:network"]}]
    }


@pytest.mark.parametrize(
    "path",
    [pytest.param("/providers", id="list"), pytest.param(f"/providers/{UPA_NSA}/reservations", id="reservations")],
)
def test_unconfigured_returns_503(client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    monkeypatch.setattr(app.state, "upa_client", None)

    assert client.get(path).status_code == 503


@pytest.mark.parametrize(
    ("client_attr", "path"),
    [
        pytest.param("nsi_client", "/providers", id="list-dds-proxy-down"),
        pytest.param("nsi_client", f"/providers/{UPA_NSA}/reservations", id="reservations-dds-proxy-down"),
        pytest.param("upa_client", f"/providers/{UPA_NSA}/reservations", id="reservations-upa-down"),
    ],
)
def test_unreachable_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, client_attr: str, path: str
) -> None:
    monkeypatch.setattr(app.state, client_attr, httpx.AsyncClient(transport=httpx.MockTransport(_failing_handler)))

    assert client.get(path).status_code == 502


@pytest.mark.parametrize(
    ("agents", "expected"),
    [
        pytest.param(AGENTS, [UPA_NSA], id="peered-upa-only"),
        pytest.param(AGENTS[1:], [], id="aggregator-not-in-dds"),
        pytest.param([AGENTS[0], _agent(UPA_NSA, UPA_URL, [AGGREGATOR_ROLE])], [], id="peer-without-upa-role"),
        pytest.param([AGENTS[0], _agent(UPA_NSA, None, [UPA_ROLE])], [], id="peer-without-provider-url"),
        pytest.param([AGENTS[0], _agent(UPA_NSA, "", [UPA_ROLE])], [], id="peer-with-empty-provider-url"),
    ],
)
def test_peered_upas(agents: list[dict], expected: list[str]) -> None:
    parsed = [NetworkServiceAgent.model_validate(agent) for agent in agents]
    assert [upa.id for upa in peered_upas(parsed, settings.provider_nsa)] == expected


@pytest.mark.parametrize(
    ("path", "operation_id"),
    [
        pytest.param("/providers", "get_providers", id="list"),
        pytest.param("/providers/{nsaId}/reservations", "get_provider_reservations", id="reservations"),
    ],
)
def test_routes_have_explicit_operation_ids(path: str, operation_id: str) -> None:
    assert app.openapi()["paths"][path]["get"]["operationId"] == operation_id
