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


"""Tests for parse_query_result_sync."""

import pytest

from aggregator_proxy.nsi_soap.parser import parse_query_result_sync

_C = "http://schemas.ogf.org/nsi/2013/12/connection/types"
_CONNECTION_ID = "79a92d32-c9ab-4104-8567-88e1e8f4510f"


def _result(result_id: int, timestamp: str, operation: str) -> str:
    return (
        "<result>"
        f"<resultId>{result_id}</resultId>"
        f"<correlationId>urn:uuid:corr-{result_id}</correlationId>"
        f"<timeStamp>{timestamp}</timeStamp>"
        f"<nsi_ctypes:{operation}><connectionId>{_CONNECTION_ID}</connectionId></nsi_ctypes:{operation}>"
        "</result>"
    )


# The reported sample: reserve -> commit -> provision -> release -> provision, descending resultIds.
_SAMPLE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
    f' xmlns:nsi_ctypes="{_C}">'
    "<soapenv:Body>"
    "<nsi_ctypes:queryResultSyncConfirmed>"
    + _result(5, "2026-06-25T16:02:13.057Z", "provisionConfirmed")
    + _result(4, "2026-06-25T07:28:55.458Z", "releaseConfirmed")
    + _result(3, "2026-06-25T07:07:02.098Z", "provisionConfirmed")
    + _result(2, "2026-06-24T19:37:56.282Z", "reserveCommitConfirmed")
    + _result(1, "2026-06-24T19:37:56.021Z", "reserveConfirmed")
    + "</nsi_ctypes:queryResultSyncConfirmed>"
    "</soapenv:Body>"
    "</soapenv:Envelope>"
).encode()


def test_parses_all_results() -> None:
    results = parse_query_result_sync(_SAMPLE)
    assert [(r.result_id, r.operation) for r in results] == [
        (5, "provisionConfirmed"),
        (4, "releaseConfirmed"),
        (3, "provisionConfirmed"),
        (2, "reserveCommitConfirmed"),
        (1, "reserveConfirmed"),
    ]
    assert all(r.connection_id == _CONNECTION_ID for r in results)
    assert results[0].timestamp == "2026-06-25T16:02:13.057Z"


def test_latest_provision_is_highest_result_id() -> None:
    results = parse_query_result_sync(_SAMPLE)
    provisions = [r for r in results if r.operation == "provisionConfirmed"]
    latest = max(provisions, key=lambda r: r.result_id)
    assert latest.result_id == 5
    assert latest.timestamp == "2026-06-25T16:02:13.057Z"


def test_empty_result_list() -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
        f' xmlns:nsi_ctypes="{_C}">'
        "<soapenv:Body><nsi_ctypes:queryResultSyncConfirmed/></soapenv:Body>"
        "</soapenv:Envelope>"
    ).encode()
    assert parse_query_result_sync(xml) == []


def test_wrong_operation_raises() -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
        f' xmlns:nsi_ctypes="{_C}">'
        "<soapenv:Body>"
        "<nsi_ctypes:reserveResponse><connectionId>x</connectionId></nsi_ctypes:reserveResponse>"
        "</soapenv:Body>"
        "</soapenv:Envelope>"
    ).encode()
    with pytest.raises(ValueError, match="Expected queryResultSyncConfirmed"):
        parse_query_result_sync(xml)
