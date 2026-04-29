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


"""NSI CS v2 SOAP message building and parsing."""

from aggregator_proxy.nsi_soap.builder import (
    NsiHeader,
    build_provision,
    build_query_notification_sync,
    build_query_recursive,
    build_query_summary_sync,
    build_release,
    build_reserve,
    build_reserve_commit,
    build_terminate,
)
from aggregator_proxy.nsi_soap.parser import (
    Acknowledgment,
    ChildSegment,
    ConnectionStates,
    DataPlaneStateChange,
    ErrorEvent,
    NsiMessage,
    ProvisionConfirmed,
    QueryRecursiveResult,
    QueryReservation,
    ReleaseConfirmed,
    ReserveCommitConfirmed,
    ReserveCommitFailed,
    ReserveConfirmed,
    ReserveFailed,
    ReserveResponse,
    ReserveTimeout,
    ServiceException,
    TerminateConfirmed,
    Variable,
    XmlInput,
    parse,
    parse_correlation_id,
    parse_query_notification_sync,
    parse_query_summary_sync,
)

__all__ = [
    "NsiHeader",
    "build_provision",
    "build_query_notification_sync",
    "build_query_recursive",
    "build_query_summary_sync",
    "build_release",
    "build_reserve",
    "build_reserve_commit",
    "build_terminate",
    "Acknowledgment",
    "ChildSegment",
    "ConnectionStates",
    "DataPlaneStateChange",
    "ErrorEvent",
    "NsiMessage",
    "ProvisionConfirmed",
    "QueryRecursiveResult",
    "QueryReservation",
    "ReleaseConfirmed",
    "ReserveCommitConfirmed",
    "ReserveCommitFailed",
    "ReserveConfirmed",
    "ReserveFailed",
    "ReserveResponse",
    "ReserveTimeout",
    "ServiceException",
    "TerminateConfirmed",
    "Variable",
    "XmlInput",
    "parse",
    "parse_correlation_id",
    "parse_query_notification_sync",
    "parse_query_summary_sync",
]
