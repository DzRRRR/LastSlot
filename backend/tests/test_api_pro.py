"""Routes commerçant : authentification, isolation entre commerces (IDOR), publication et
annulation de créneaux, événements temps réel."""
import json
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from tests.dbutil import (EVENT_CATEGORY, db_call, delete_merchants, insert_booking, insert_merchant,
                          insert_resource, insert_service, insert_slot, requires_db)

pytestmark = requires_db

# Tokens de test, de la forme d'un secrets.token_urlsafe(32).
TOKEN_SALON = "s" * 43
TOKEN_COURSE = "c" * 43
TOKEN_OTHER = "o" * 43
AREA_BBOX = "2.29,10.29,2.33,10.33"


def auth(token: str) -> dict[str, str]:
    return {"X-Merchant-Token": token}


def at(minutes: int) -> str:
    """Instant à now + `minutes`, à la minute, ISO 8601 avec fuseau."""
    return (datetime.now(timezone.utc).replace(second=0, microsecond=0)
            + timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def shop(server: str) -> Iterator[dict]:
    """Un salon (2 prestations, 2 praticiens), un organisateur de cours collectifs (famille
    'event') et un concurrent."""
    async def setup(conn):
        salon = await insert_merchant(conn, "Pro Salon", "coiffure", 2.30, 10.30, token=TOKEN_SALON)
        course = await insert_merchant(conn, "Pro Cours", EVENT_CATEGORY, 2.31, 10.31, token=TOKEN_COURSE)
        other = await insert_merchant(conn, "Pro Concurrent", "coiffure", 2.32, 10.32, token=TOKEN_OTHER)
        return {
            "salon": salon, "course": course, "other": other,
            "cut": await insert_service(conn, salon, "Coupe", 30, 2800),
            "color": await insert_service(conn, salon, "Couleur", 90, 6500),
            "camille": await insert_resource(conn, salon, "Camille"),
            "hugo": await insert_resource(conn, salon, "Hugo"),
            "class": await insert_service(conn, course, "Cours", 120, 2500),
            "other_service": await insert_service(conn, other, "Coupe", 30, 3000),
            "other_resource": await insert_resource(conn, other, "Léa"),
        }

    data = db_call(setup)
    yield data
    db_call(lambda conn: delete_merchants(conn, [data["salon"], data["course"], data["other"]]))


@pytest.mark.parametrize("headers", [{}, {"X-Merchant-Token": "court"}, {"X-Merchant-Token": "x" * 43}])
def test_pro_routes_require_a_valid_token(client: httpx.Client, shop: dict, headers: dict) -> None:
    for method, url in [("GET", "/api/pro/me"), ("GET", "/api/pro/slots"),
                        ("POST", "/api/pro/slots"), ("DELETE", "/api/pro/slots/1")]:
        assert client.request(method, url, headers=headers).status_code == 401, (method, url)


def test_me_returns_own_merchant_services_and_resources(client: httpx.Client, shop: dict) -> None:
    body = client.get("/api/pro/me", headers=auth(TOKEN_SALON)).json()
    assert body["merchant"]["id"] == shop["salon"]
    assert (body["merchant"]["category"], body["merchant"]["family"]) == ("coiffure", "appointment")
    assert [s["id"] for s in body["services"]] == [shop["cut"], shop["color"]]
    assert [r["name"] for r in body["resources"]] == ["Camille", "Hugo"]


def test_create_appointment_slot(client: httpx.Client, shop: dict) -> None:
    response = client.post("/api/pro/slots", headers=auth(TOKEN_SALON), json={
        "service_id": shop["cut"], "starts_at": at(90), "max_discount_pct": 30, "capacity": 5,
    })
    assert response.status_code == 201, response.text
    slot = response.json()
    assert slot["capacity"] == slot["remaining"] == 1  # rendez-vous : capacité forcée à 1
    # Début dans 1 h 30 (< H-2) : le plafond s'applique déjà.
    assert (slot["base_price_cents"], slot["max_discount_pct"]) == (2800, 30)
    assert (slot["discount_pct"], slot["price_cents"], slot["next_discount_pct"]) == (30, 1960, None)
    assert slot["resource_name"] == "Camille"  # premier praticien libre
    assert slot["bookings"] == [] and slot["status"] == "open"
    start, end = datetime.fromisoformat(slot["starts_at"]), datetime.fromisoformat(slot["ends_at"])
    assert end - start == timedelta(minutes=30)

    public = client.get("/api/slots", params={"bbox": AREA_BBOX}).json()
    assert slot["id"] in [s["id"] for s in public]


def test_base_price_is_frozen_at_publication(client: httpx.Client, shop: dict) -> None:
    slot = client.post("/api/pro/slots", headers=auth(TOKEN_SALON), json={
        "service_id": shop["cut"], "starts_at": at(600), "max_discount_pct": 30}).json()
    assert (slot["discount_pct"], slot["price_cents"], slot["next_discount_pct"]) == (10, 2520, 20)
    db_call(lambda conn: conn.execute(
        "UPDATE service SET base_price_cents = 9900 WHERE id = $1", shop["cut"]))
    [again] = [s for s in client.get("/api/pro/slots", headers=auth(TOKEN_SALON)).json()
               if s["id"] == slot["id"]]
    assert (again["base_price_cents"], again["price_cents"]) == (2800, 2520)


def test_practitioner_assignment_and_overlaps(client: httpx.Client, shop: dict) -> None:
    def publish(**fields) -> httpx.Response:
        return client.post("/api/pro/slots", headers=auth(TOKEN_SALON),
                           json={"max_discount_pct": 20, **fields})

    first = publish(service_id=shop["cut"], starts_at=at(120))
    second = publish(service_id=shop["cut"], starts_at=at(120))
    assert (first.json()["resource_name"], second.json()["resource_name"]) == ("Camille", "Hugo")
    third = publish(service_id=shop["cut"], starts_at=at(120))
    assert third.status_code == 409 and "libre" in third.json()["detail"]

    # Praticien imposé : Couleur 90 min à +60 chevauche [120, 150) de Camille -> contrainte d'exclusion.
    overlap = publish(service_id=shop["color"], starts_at=at(60), resource_id=shop["camille"])
    assert overlap.status_code == 409 and "chevauche" in overlap.json()["detail"]
    # Intervalles semi-ouverts : [150, 180) touche [120, 150) sans le chevaucher.
    adjacent = publish(service_id=shop["cut"], starts_at=at(150), resource_id=shop["camille"])
    assert adjacent.status_code == 201, adjacent.text


def test_create_event_slot_requires_capacity(client: httpx.Client, shop: dict) -> None:
    body = {"service_id": shop["class"], "starts_at": at(240), "max_discount_pct": 40}
    assert client.post("/api/pro/slots", headers=auth(TOKEN_COURSE), json=body).status_code == 422
    response = client.post("/api/pro/slots", headers=auth(TOKEN_COURSE), json={**body, "capacity": 3})
    assert response.status_code == 201, response.text
    slot = response.json()
    assert (slot["capacity"], slot["remaining"], slot["resource_id"]) == (3, 3, None)
    # Début dans 4 h (palier H-6) : plafond 40 % -> -25 % maintenant, -40 % à H-2.
    assert (slot["discount_pct"], slot["price_cents"], slot["next_discount_pct"]) == (25, 1875, 40)


@pytest.mark.parametrize("make_body", [
    lambda s: {"service_id": s["other_service"], "starts_at": at(60), "max_discount_pct": 30},  # IDOR
    lambda s: {"service_id": s["cut"], "resource_id": s["other_resource"],  # IDOR
               "starts_at": at(60), "max_discount_pct": 30},
    lambda s: {"service_id": s["cut"], "starts_at": at(-5), "max_discount_pct": 30},
    lambda s: {"service_id": s["cut"], "starts_at": at(15 * 24 * 60), "max_discount_pct": 30},
    lambda s: {"service_id": s["cut"], "starts_at": "2030-01-01T10:00:00", "max_discount_pct": 30},
    lambda s: {"service_id": s["cut"], "starts_at": at(60), "max_discount_pct": 4},
    lambda s: {"service_id": s["cut"], "starts_at": at(60), "max_discount_pct": 81},
    lambda s: {"service_id": s["cut"], "starts_at": at(60)},
], ids=["service-autre-commerce", "praticien-autre-commerce", "passe", "au-dela-14j",
        "sans-fuseau", "plafond-4", "plafond-81", "sans-plafond"])
def test_create_slot_rejects_invalid_input(client: httpx.Client, shop: dict,
                                           make_body: Callable[[dict], dict]) -> None:
    response = client.post("/api/pro/slots", headers=auth(TOKEN_SALON), json=make_body(shop))
    assert response.status_code == 422, response.text


def test_list_slots_is_scoped_and_hides_booking_ids(client: httpx.Client, shop: dict) -> None:
    async def setup(conn):
        mine = await insert_slot(conn, shop["course"], shop["class"], starts_in_min=300,
                                 base_price_cents=2500, duration_min=120, capacity=10, remaining=7)
        for name, status in [("Alice", "held"), ("Bob", "confirmed"), ("Carl", "expired"),
                             ("Dan", "cancelled")]:
            await insert_booking(conn, mine, status, seats=2 if name == "Alice" else 1, name=name)
        old = await insert_slot(conn, shop["course"], shop["class"], starts_in_min=-180,
                                duration_min=120, capacity=10)
        foreign = await insert_slot(conn, shop["other"], shop["other_service"], starts_in_min=300)
        return mine, old, foreign

    mine, old, foreign = db_call(setup)
    slots = client.get("/api/pro/slots", headers=auth(TOKEN_COURSE)).json()
    assert [s["id"] for s in slots] == [mine]  # ni le créneau de plus de 2 h, ni celui du concurrent
    bookings = slots[0]["bookings"]
    assert [(b["customer_name"], b["status"], b["seats"]) for b in bookings] == [
        ("Alice", "held", 2), ("Bob", "confirmed", 1)]
    assert bookings[0]["expires_at"] is not None and bookings[1]["expires_at"] is None
    assert all("id" not in b for b in bookings)  # l'id d'une réservation est la capability du client


def test_cancel_slot(client: httpx.Client, shop: dict) -> None:
    async def setup(conn):
        held = await insert_slot(conn, shop["course"], shop["class"], starts_in_min=300,
                                 capacity=10, remaining=8)
        await insert_booking(conn, held, "held", seats=2)
        confirmed = await insert_slot(conn, shop["course"], shop["class"], starts_in_min=500,
                                      capacity=10, remaining=9)
        await insert_booking(conn, confirmed, "confirmed")
        foreign = await insert_slot(conn, shop["other"], shop["other_service"], starts_in_min=300)
        return held, confirmed, foreign

    held, confirmed, foreign = db_call(setup)
    headers = auth(TOKEN_COURSE)

    assert client.delete(f"/api/pro/slots/{held}", headers=headers).status_code == 204
    state = db_call(lambda conn: conn.fetchrow(
        "SELECT s.status, s.remaining, s.capacity, b.status AS booking FROM slot s "
        "JOIN booking b ON b.slot_id = s.id WHERE s.id = $1", held))
    assert dict(state) == {"status": "cancelled", "remaining": 10, "capacity": 10, "booking": "cancelled"}
    assert held not in [s["id"] for s in client.get("/api/slots", params={"bbox": AREA_BBOX}).json()]
    assert client.delete(f"/api/pro/slots/{held}", headers=headers).status_code == 204  # idempotent

    refused = client.delete(f"/api/pro/slots/{confirmed}", headers=headers)
    assert refused.status_code == 409 and "confirmées" in refused.json()["detail"]
    assert db_call(lambda conn: conn.fetchval("SELECT status FROM slot WHERE id = $1", confirmed)) == "open"

    assert client.delete(f"/api/pro/slots/{foreign}", headers=headers).status_code == 404  # IDOR
    assert client.delete("/api/pro/slots/999999999", headers=headers).status_code == 404


def test_publish_and_cancel_emit_sse_events(client: httpx.Client, shop: dict) -> None:
    def next_slot_event(lines: Iterator[str], slot_id: int) -> dict:
        deadline, previous = time.monotonic() + 5, ""
        for line in lines:
            assert time.monotonic() < deadline, "événement non reçu"
            if previous == "event: slot" and line.startswith("data: "):
                event = json.loads(line.removeprefix("data: "))
                if event["id"] == slot_id:
                    return event
            previous = line
        raise AssertionError("flux SSE terminé")

    with client.stream("GET", "/api/stream") as stream:
        lines = stream.iter_lines()
        assert next(lines) == "retry: 3000"  # abonné : les événements suivants nous parviennent
        created = client.post("/api/pro/slots", headers=auth(TOKEN_SALON), json={
            "service_id": shop["cut"], "starts_at": at(200), "max_discount_pct": 25}).json()
        assert next_slot_event(lines, created["id"]) == {
            "id": created["id"], "merchant_id": shop["salon"], "remaining": 1,
            "status": "open", "change": "created"}
        client.delete(f"/api/pro/slots/{created['id']}", headers=auth(TOKEN_SALON))
        assert next_slot_event(lines, created["id"]) == {
            "id": created["id"], "merchant_id": shop["salon"], "remaining": 1,
            "status": "cancelled", "change": "cancelled"}
