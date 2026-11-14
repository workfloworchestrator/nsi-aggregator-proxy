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


"""Tests for the pending-reply bookkeeping of ReservationStore."""

import asyncio

import pytest

from aggregator_proxy.nsi_soap import Acknowledgment
from aggregator_proxy.reservation_store import ReservationStore

CORRELATION_ID = "urn:uuid:11111111-1111-1111-1111-111111111111"


@pytest.mark.anyio()
async def test_wait_that_times_out_is_not_kept(store: ReservationStore) -> None:
    """``wait_for`` cancels the future it gives up on, which is how every operation's wait ends."""
    future = store.register_pending(CORRELATION_ID)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(future, timeout=0)
    await asyncio.sleep(0)

    assert not store._pending  # noqa: SLF001
    assert not store.resolve_pending(CORRELATION_ID, Acknowledgment())


@pytest.mark.anyio()
async def test_cleanup_leaves_a_future_registered_again_under_the_same_id(store: ReservationStore) -> None:
    """The first future's cleanup runs after the second is registered, and must not remove the second."""
    first = store.register_pending(CORRELATION_ID)
    first.cancel()
    second = store.register_pending(CORRELATION_ID)

    await asyncio.sleep(0)

    assert store._pending == {CORRELATION_ID: second}  # noqa: SLF001
