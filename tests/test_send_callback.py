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


"""Tests for _send_callback delivery and its 409 retry."""

import httpx
import pytest

from aggregator_proxy.routers import reservations as reservations_module
from aggregator_proxy.routers.reservations import _send_callback
from tests.conftest import make_reservation

CALLBACK_URL = "http://callback.example.com/result"


@pytest.mark.parametrize(
    ("responses", "expected_attempts"),
    [
        pytest.param([200], 1, id="accepted-first-time"),
        pytest.param([409, 409, 200], 3, id="retried-past-409"),
        pytest.param([409, 500], 2, id="non-409-rejection-stops"),
        pytest.param([409] * 3, 3, id="gives-up-after-max-attempts"),
    ],
)
async def test_send_callback(monkeypatch: pytest.MonkeyPatch, responses: list[int], expected_attempts: int) -> None:
    monkeypatch.setattr(reservations_module, "_CALLBACK_ATTEMPTS", 3)
    monkeypatch.setattr(reservations_module, "_CALLBACK_RETRY_DELAY_SECONDS", 0)
    codes = iter(responses)
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        return httpx.Response(next(codes))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await _send_callback(client, CALLBACK_URL, make_reservation())
    assert attempts == [CALLBACK_URL] * expected_attempts
