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


"""Authentication dependencies.

The REST API trusts identity headers set by the edge proxy. On the portal
route, Traefik plus oauth2-proxy lands ``X-Auth-Request-Email`` and
``X-Auth-Request-Groups``. On the mTLS route, the ``nsi-auth`` validate
sidecar lands the configured ``MTLS_HEADER`` plus ``X-Client-DN``. The
``/nsi/v2/callback`` endpoint, which receives async SOAP messages from the
upstream NSI Aggregator over mTLS, uses a stricter dependency that accepts
only the mTLS header — browser/OIDC users must not be able to forge
callbacks.
"""

from typing import Any

import structlog
from fastapi import HTTPException, Request

from aggregator_proxy.settings import settings

log = structlog.get_logger(__name__)

_WWW_AUTHENTICATE = {"WWW-Authenticate": "Bearer"}

# Identity-header names set by oauth2-proxy / nsi-auth. Public so the MCP
# claim-translation hook can write to the same contract.
USER_HEADER = "X-Auth-Request-Email"
GROUPS_HEADER = "X-Auth-Request-Groups"
CLIENT_DN_HEADER = "X-Client-DN"


def _parse_groups(header_value: str) -> list[str]:
    """Parse oauth2-proxy's X-Auth-Request-Groups value into a list of group strings."""
    return [g.strip() for g in header_value.replace(",", " ").split() if g.strip()]


def check_groups(user_groups: list[str], required_groups: list[str]) -> list[str]:
    """Return the sorted intersection of ``required_groups`` and ``user_groups``."""
    return sorted(set(required_groups).intersection(user_groups))


def _verify_mtls(request: Request) -> str | None:
    """Return ``X-Client-DN`` (or ``"unknown"``) iff the configured mTLS header is present."""
    if not settings.mtls_header:
        return None
    if not request.headers.get(settings.mtls_header, "").strip():
        return None
    return request.headers.get(CLIENT_DN_HEADER, "unknown")


async def get_authenticated_user(request: Request) -> dict[str, Any] | None:
    """FastAPI dependency that authorises a request from the edge-proxy identity headers.

    When ``proxy_auth_enabled`` is ``False``, all requests pass through.
    When ``True``, the request must arrive with one of:

    * oauth2-proxy's identity headers (``X-Auth-Request-Email`` and
      ``X-Auth-Request-Groups``), set by Traefik's ForwardAuth middleware
      on the portal route, or
    * the mTLS header (``settings.mtls_header``) set by the mTLS auth
      subrequest service on the machine-client route.

    If neither is present, the request is rejected with 401.
    """
    if not settings.proxy_auth_enabled:
        return None

    path = request.url.path

    user_id = request.headers.get(USER_HEADER, "").strip()
    if user_id:
        required = settings.oidc_required_groups
        user_groups = _parse_groups(request.headers.get(GROUPS_HEADER, ""))
        matched = check_groups(user_groups, required) if required else []
        if required and not matched:
            log.warning(
                "Insufficient group membership",
                user=user_id,
                user_groups=user_groups,
                required_groups=required,
                path=path,
            )
            raise HTTPException(status_code=403, detail="Insufficient group membership")
        log.info("OIDC user authenticated", user=user_id, matched_groups=matched, path=path)
        return {"sub": user_id, "groups": user_groups}

    client_dn = _verify_mtls(request)
    if client_dn is not None:
        log.info("mTLS authentication verified", client_dn=client_dn, path=path)
        return None

    log.warning("No valid authentication credentials found", path=path)
    raise HTTPException(status_code=401, detail="Authentication required", headers=_WWW_AUTHENTICATE)


async def get_mtls_authenticated_callback(request: Request) -> None:
    """FastAPI dependency that enforces mTLS-only on the NSI callback endpoint.

    The NSI aggregator delivers async SOAP callbacks over mTLS; OIDC users
    (browser sessions) must not be able to reach this endpoint, even if the
    Traefik routing happens to land their request here.
    """
    if not settings.proxy_auth_enabled:
        return

    client_dn = _verify_mtls(request)
    if client_dn is not None:
        log.info("Callback mTLS authentication verified", client_dn=client_dn, path=request.url.path)
        return

    log.warning("Callback rejected: missing mTLS header", path=request.url.path)
    raise HTTPException(status_code=401, detail="mTLS authentication required", headers=_WWW_AUTHENTICATE)
