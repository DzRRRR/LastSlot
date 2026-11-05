"""Flux SSE : générateur testé seul (sans base), puis de bout en bout avec le job d'expiration."""
import asyncio
import json
import time
from collections.abc import Iterator

import httpx
import pytest

from app import api_public
from app.broker import Broker
from tests.dbutil import (EVENT_CATEGORY, db_call, delete_merchants, insert_merchant, insert_service,
                          insert_slot, requires_db)


class FakeRequest:
    def __init__(self) -> None:
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> Broker:
    fresh = Broker(queue_size=2)
    monkeypatch.setattr(api_public, "broker", fresh)
    return fresh


@pytest.mark.anyio
async def test_stream_sends_retry_then_events(broker: Broker) -> None:
    stream = api_public.event_stream(FakeRequest(), heartbeat=5)
    assert await stream.__anext__() == "retry: 3000\n\n"
    broker.publish("slot", {"id": 1})
    assert await asyncio.wait_for(stream.__anext__(), 1) == 'event: slot\ndata: {"id": 1}\n\n'
    await stream.aclose()
    assert not broker._subscribers


@pytest.mark.anyio
async def test_stream_heartbeat_then_disconnect(broker: Broker) -> None:
    request = FakeRequest()
    stream = api_public.event_stream(request, heartbeat=0.05)
    await stream.__anext__()
    assert await asyncio.wait_for(stream.__anext__(), 1) == ": ping\n\n"
    request.disconnected = True
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(stream.__anext__(), 1)
    assert not broker._subscribers


@pytest.mark.anyio
async def test_message_after_heartbeat_timeout_is_not_lost(broker: Broker) -> None:
    # La lecture de la file survit au timeout du heartbeat (asyncio.wait ne l'annule pas).
    stream = api_public.event_stream(FakeRequest(), heartbeat=0.05)
    await stream.__anext__()
    assert await asyncio.wait_for(stream.__anext__(), 1) == ": ping\n\n"
    broker.publish("slot", {"id": 2})
    assert await asyncio.wait_for(stream.__anext__(), 1) == 'event: slot\ndata: {"id": 2}\n\n'
    await stream.aclose()


@pytest.mark.anyio
async def test_slow_client_is_dropped_and_stream_ends(broker: Broker) -> None:
    stream = api_public.event_stream(FakeRequest(), heartbeat=5)
    await stream.__anext__()
    for i in range(3):  # file de 2 : la 3e publication désabonne le client
        broker.publish("slot", {"id": i})
    assert not broker._subscribers
    # Le flux se termine : EventSource se reconnectera et rechargera l'état complet.
    assert [message async for message in stream] == []


@pytest.fixture
def released_slot(server: str) -> Iterator[dict]:
    """Créneau de 3 places dont 2 sont bloquées par un hold."""
    async def setup(conn):
        merchant = await insert_merchant(conn, "Test SSE", EVENT_CATEGORY, 2.2, 10.2)
        service = await insert_service(conn, merchant, "Cours test", 60, 2000)
        slot = await insert_slot(conn, merchant, service, starts_in_min=120, capacity=3, remaining=1)
        return {"merchant": merchant, "slot": slot}

    data = db_call(setup)
    yield data
    db_call(lambda conn: delete_merchants(conn, [data["merchant"]]))


def wait_slot_event(lines: Iterator[str], slot_id: int, timeout: float = 5) -> dict:
    deadline, previous = time.monotonic() + timeout, ""
    for line in lines:
        assert time.monotonic() < deadline, "événement slot non reçu"
        if previous == "event: slot" and line.startswith("data: "):
            event = json.loads(line.removeprefix("data: "))
            if event["id"] == slot_id:
                return event
        previous = line
    raise AssertionError("flux SSE terminé")


@requires_db
def test_discount_step_is_announced_in_real_time(client: httpx.Client) -> None:
    """Le prix baisse sans écriture en base (remise progressive) : le job publie un événement
    quand un palier est franchi, pour que les cartes ouvertes rechargent."""
    async def setup(conn):
        merchant = await insert_merchant(conn, "Test palier", "coiffure", 2.4, 10.4)
        service = await insert_service(conn, merchant, "Coupe", 30, 3000)
        # Palier H-2 franchi 1,5 s après l'insertion.
        slot = await insert_slot(conn, merchant, service, starts_in_min=120 + 1.5 / 60)
        return merchant, slot

    with client.stream("GET", "/api/stream") as response:
        lines = response.iter_lines()
        assert next(lines) == "retry: 3000"
        merchant, slot_id = db_call(setup)
        try:
            before = client.get("/api/slots", params={"bbox": "2.39,10.39,2.41,10.41"}).json()
            assert [s["discount_pct"] for s in before] == [20]
            assert wait_slot_event(lines, slot_id)["change"] == "discount"
            after = client.get("/api/slots", params={"bbox": "2.39,10.39,2.41,10.41"}).json()
            assert [(s["discount_pct"], s["price_cents"]) for s in after] == [(30, 2100)]
        finally:
            db_call(lambda conn: delete_merchants(conn, [merchant]))


@requires_db
def test_sse_end_to_end_with_expired_hold(client: httpx.Client, released_slot: dict) -> None:
    slot_id = released_slot["slot"]
    with client.stream("GET", "/api/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        assert "content-security-policy" in response.headers

        lines = response.iter_lines()
        assert next(lines) == "retry: 3000"
        deadline = time.monotonic() + 5
        while next(lines) != ": ping":  # heartbeat de 0,5 s en test
            assert time.monotonic() < deadline, "pas de heartbeat"

        # Hold déjà expiré : le job (toutes les 0,2 s) le libère et publie un événement.
        db_call(lambda conn: conn.execute(
            """INSERT INTO booking (slot_id, customer_name, customer_contact, seats, status, expires_at)
               VALUES ($1, 'Client test', 'test@example.org', 2, 'held', now() - interval '1 second')""",
            slot_id,
        ))
        event = wait_slot_event(lines, slot_id)

    assert event == {"id": slot_id, "merchant_id": released_slot["merchant"], "remaining": 3,
                     "status": "open", "change": "released"}
    status = db_call(lambda conn: conn.fetchval("SELECT status FROM booking WHERE slot_id = $1", slot_id))
    assert status == "expired"
