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

"""Lookup of the aggregator's uPAs in the nsi-dds-proxy."""

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter
from pydantic.alias_generators import to_camel

UPA_ROLE = "vnd.ogf.nsi.cs.v2.role.uPA"


class NetworkServiceAgent(BaseModel):
    """An NSA from the dds-proxy ``GET /network-service-agents`` endpoint; only the needed fields."""

    model_config = ConfigDict(alias_generator=to_camel)

    id: str
    provider_url: str | None
    roles: list[str]
    peers_with: list[str]


_AGENTS = TypeAdapter(list[NetworkServiceAgent])


async def fetch_network_service_agents(client: httpx.AsyncClient, dds_proxy_url: str) -> list[NetworkServiceAgent]:
    """Fetch every NSA the dds-proxy knows."""
    response = await client.get(f"{dds_proxy_url}/network-service-agents")
    response.raise_for_status()
    return _AGENTS.validate_json(response.content)


def peered_upa_url(agents: list[NetworkServiceAgent], aggregator_nsa: str, nsa_id: str) -> str | None:
    """The provider URL of ``nsa_id`` if it is a uPA that the aggregator peers with, else None.

    Only such a uPA is ever sent the aggregator's client certificate.
    """
    by_id = {agent.id: agent for agent in agents}
    match by_id.get(aggregator_nsa), by_id.get(nsa_id):
        case (
            NetworkServiceAgent(peers_with=peers),
            NetworkServiceAgent(roles=roles, provider_url=str() as provider_url),
        ) if nsa_id in peers and UPA_ROLE in roles and provider_url:
            return provider_url
        case _:
            return None
