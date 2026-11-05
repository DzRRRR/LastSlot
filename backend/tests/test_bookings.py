"""Réservation : prise de place au prix figé, paiement simulé, expiration, annulation, statut,
événements temps réel, statistiques du pro, et absence de surréservation sous concurrence."""
import asyncio
import json
import time
import uuid
from collections import Counter
from collections.abc import Iterator

import httpx
import pytest

from tests.dbutil import (EVENT_CATEGORY, db_call, delete_merchants, insert_merchant, insert_resource,
                          insert_service, insert_slot, requires_db)

pytestmark = requires_db

TOKEN = "r" * 43  # token du salon de test


@pytest.fixture
def venue(server: str) -> Iterator[dict]:
    """Un salon (rendez-vous, plafond 30 %) et un organisateur de cours (5 places)."""
    async def setup(conn):
        salon = await insert_merchant(conn, "Resa Salon", "coiffure", 2.50, 10.50, token=TOKEN)
        course = await insert_merchant(conn, "Resa Cours", EVENT_CATEGORY, 2.51, 10.51)
        cut = await insert_service(conn, salon, "Coupe", 30, 3000)
        lesson = await insert_service(conn, course, "Cours", 60, 2000)
        camille = await insert_resource(conn, salon, "Camille")
        return {
            "merchants": [salon, course],
            # Dans 3 h (palier H-6) : -20 % -> 24 €, puis -30 % à H-2.
            "cut_slot": await insert_slot(conn, salon, cut, starts_in_min=180, resource_id=camille),
            "past_slot": await insert_slot(conn, salon, cut, starts_in_min=-10),
            "cancelled_slot": await insert_slot(conn, salon, cut, starts_in_min=300, status="cancelled"),
            # Dans 1 h (palier H-2) : -30 % -> 14 € la place.
            "lesson_slot": await insert_slot(conn, course, lesson, starts_in_min=60, base_price_cents=2000,
                                             duration_min=60, capacity=5),
        }

    data = db_call(setup)
    yield data
    db_call(lambda conn: delete_merchants(conn, data["merchants"]))


def hold(client: httpx.Client, slot_id: int, **fields) -> httpx.Response:
    return client.post("/api/bookings", json={
        "slot_id": slot_id, "customer_name": "Alice Martin",
        "customer_contact": "alice@example.org", **fields})


def remaining(slot_id: int) -> int:
    return db_call(lambda conn: conn.fetchval("SELECT remaining FROM slot WHERE id = $1", slot_id))


def test_hold_freezes_price_and_takes_the_seat(client: httpx.Client, venue: dict) -> None:
    response = hold(client, venue["cut_slot"])
    assert response.status_code == 201, response.text
    booking = response.json()
    assert booking["status"] == "held" and 295 <= booking["expires_in_s"] <= 300
    assert (booking["base_price_cents"], booking["discount_pct"], booking["unit_price_cents"],
            booking["amount_cents"]) == (3000, 20, 2400, 2400)
    assert booking["slot"]["merchant_name"] == "Resa Salon"
    assert remaining(venue["cut_slot"]) == 0
    public = client.get("/api/slots", params={"bbox": "2.49,10.49,2.52,10.52"}).json()
    assert venue["cut_slot"] not in [s["id"] for s in public]  # retiré de la carte
    commission = db_call(lambda conn: conn.fetchval(
        "SELECT commission_cents FROM booking WHERE id = $1", uuid.UUID(booking["id"])))
    assert commission == 360  # 15 % de 24 €

    # Le prix courant du créneau change ensuite (ici : le pro relève le plafond) : la
    # réservation garde le prix vu par le client.
    db_call(lambda conn: conn.execute(
        "UPDATE slot SET max_discount_pct = 80 WHERE id = $1", venue["cut_slot"]))
    again = client.get(f"/api/bookings/{booking['id']}").json()
    assert (again["discount_pct"], again["amount_cents"]) == (20, 2400)


def test_event_hold_for_several_seats(client: httpx.Client, venue: dict) -> None:
    booking = hold(client, venue["lesson_slot"], seats=3).json()
    assert (booking["seats"], booking["discount_pct"], booking["unit_price_cents"],
            booking["amount_cents"]) == (3, 30, 1400, 4200)
    assert remaining(venue["lesson_slot"]) == 2


@pytest.mark.parametrize("fields, code", [
    ({"customer_name": "   "}, 422),
    ({"customer_name": "x" * 81}, 422),
    ({"customer_contact": "ab"}, 422),
    ({"seats": 0}, 422),
    ({"seats": 11}, 422),
    ({"seats": 2}, 422),  # rendez-vous : une seule personne
    ({"slot_id": 999_999_999}, 404),
], ids=["nom-vide", "nom-trop-long", "contact-court", "0-place", "11-places", "rdv-2-places",
        "creneau-inconnu"])
def test_hold_validation(client: httpx.Client, venue: dict, fields: dict, code: int) -> None:
    fields = dict(fields)
    slot_id = fields.pop("slot_id", venue["cut_slot"])
    assert hold(client, slot_id, **fields).status_code == code
    assert remaining(venue["cut_slot"]) == 1  # rien n'a été pris


def test_hold_refused_when_unavailable(client: httpx.Client, venue: dict) -> None:
    assert hold(client, venue["cut_slot"]).status_code == 201
    taken = hold(client, venue["cut_slot"])
    assert taken.status_code == 409 and "Plus de place" in taken.json()["detail"]
    cancelled = hold(client, venue["cancelled_slot"])
    assert cancelled.status_code == 409 and "annulé" in cancelled.json()["detail"]
    past = hold(client, venue["past_slot"])
    assert past.status_code == 409 and "commencé" in past.json()["detail"]
    too_many = hold(client, venue["lesson_slot"], seats=6)
    assert too_many.status_code == 409 and remaining(venue["lesson_slot"]) == 5


def test_confirm_is_idempotent_and_visible_to_the_merchant(client: httpx.Client, venue: dict) -> None:
    booking = hold(client, venue["cut_slot"]).json()
    paid = client.post(f"/api/bookings/{booking['id']}/confirm")
    assert paid.status_code == 200, paid.text
    assert paid.json()["status"] == "confirmed"
    assert paid.json()["expires_at"] is None and paid.json()["confirmed_at"] is not None
    again = client.post(f"/api/bookings/{booking['id']}/confirm")  # double clic
    assert again.status_code == 200 and again.json()["status"] == "confirmed"

    headers = {"X-Merchant-Token": TOKEN}
    [slot] = [s for s in client.get("/api/pro/slots", headers=headers).json()
              if s["id"] == venue["cut_slot"]]
    [seen] = slot["bookings"]
    assert (seen["customer_name"], seen["status"], seen["amount_cents"], seen["commission_cents"]) == (
        "Alice Martin", "confirmed", 2400, 360)
    stats = client.get("/api/pro/stats", headers=headers).json()
    assert (stats["saved_slots"], stats["recovered_cents"]) == (1, 2040)  # 24 € - 15 %


def test_confirm_after_deadline_is_refused_and_seat_released(client: httpx.Client, venue: dict) -> None:
    booking = hold(client, venue["cut_slot"]).json()
    db_call(lambda conn: conn.execute(
        "UPDATE booking SET expires_at = now() - interval '1 second' WHERE id = $1",
        uuid.UUID(booking["id"])))
    assert client.get(f"/api/bookings/{booking['id']}").json()["status"] == "expired"
    refused = client.post(f"/api/bookings/{booking['id']}/confirm")
    assert refused.status_code == 409 and "Délai" in refused.json()["detail"]
    deadline = time.monotonic() + 5
    while remaining(venue["cut_slot"]) != 1:  # le job (toutes les 0,2 s) rend la place
        assert time.monotonic() < deadline, "place non libérée"
        time.sleep(0.1)


def test_cancel_hold_releases_seats(client: httpx.Client, venue: dict) -> None:
    booking = hold(client, venue["lesson_slot"], seats=3).json()
    assert remaining(venue["lesson_slot"]) == 2
    assert client.delete(f"/api/bookings/{booking['id']}").status_code == 204
    assert remaining(venue["lesson_slot"]) == 5
    assert client.get(f"/api/bookings/{booking['id']}").json()["status"] == "cancelled"
    assert client.delete(f"/api/bookings/{booking['id']}").status_code == 204  # idempotent
    assert remaining(venue["lesson_slot"]) == 5  # rien rendu deux fois
    assert client.post(f"/api/bookings/{booking['id']}/confirm").status_code == 409

    paid = hold(client, venue["lesson_slot"]).json()
    client.post(f"/api/bookings/{paid['id']}/confirm")
    refused = client.delete(f"/api/bookings/{paid['id']}")
    assert refused.status_code == 409 and "payée" in refused.json()["detail"]


def test_merchant_cancellation_voids_pending_hold(client: httpx.Client, venue: dict) -> None:
    booking = hold(client, venue["cut_slot"]).json()
    headers = {"X-Merchant-Token": TOKEN}
    assert client.delete(f"/api/pro/slots/{venue['cut_slot']}", headers=headers).status_code == 204
    refused = client.post(f"/api/bookings/{booking['id']}/confirm")
    assert refused.status_code == 409 and "annulée" in refused.json()["detail"]


def test_unknown_or_malformed_booking_id(client: httpx.Client) -> None:
    unknown = uuid.uuid4()
    assert client.get("/api/bookings/pas-un-uuid").status_code == 422
    assert client.get(f"/api/bookings/{unknown}").status_code == 404
    assert client.post(f"/api/bookings/{unknown}/confirm").status_code == 404
    assert client.delete(f"/api/bookings/{unknown}").status_code == 404


def test_booking_lifecycle_events(client: httpx.Client, venue: dict) -> None:
    def next_event(lines: Iterator[str], kind: str) -> dict:
        deadline, previous = time.monotonic() + 5, ""
        for line in lines:
            assert time.monotonic() < deadline, f"événement {kind} non reçu"
            if previous == f"event: {kind}" and line.startswith("data: "):
                event = json.loads(line.removeprefix("data: "))
                if event.get("id", event.get("slot_id")) == venue["lesson_slot"]:
                    return event
            previous = line
        raise AssertionError("flux SSE terminé")

    course = venue["merchants"][1]
    with client.stream("GET", "/api/stream") as stream:
        lines = stream.iter_lines()
        assert next(lines) == "retry: 3000"
        booking = hold(client, venue["lesson_slot"], seats=2).json()
        assert next_event(lines, "slot") == {"id": venue["lesson_slot"], "merchant_id": course,
                                             "remaining": 3, "status": "open", "change": "held"}
        assert next_event(lines, "booking") == {"slot_id": venue["lesson_slot"], "merchant_id": course,
                                                "status": "held"}
        client.delete(f"/api/bookings/{booking['id']}")
        assert next_event(lines, "slot")["change"] == "released"
        assert next_event(lines, "booking")["status"] == "cancelled"
        paid = hold(client, venue["lesson_slot"]).json()
        client.post(f"/api/bookings/{paid['id']}/confirm")
        assert next_event(lines, "booking")["status"] == "held"
        assert next_event(lines, "booking")["status"] == "confirmed"


def test_concurrent_holds_never_overbook(server: str, venue: dict) -> None:
    """N clients cliquent en même temps : jamais plus de places vendues que de places libres."""
    async def rush(slot_id: int, attempts: int, seats: int = 1) -> Counter:
        limits = httpx.Limits(max_connections=attempts)
        async with httpx.AsyncClient(base_url=server, trust_env=False, limits=limits, timeout=10) as http:
            responses = await asyncio.gather(*(
                http.post("/api/bookings", json={"slot_id": slot_id, "customer_name": f"Client {i}",
                                                 "customer_contact": f"c{i}@example.org", "seats": seats})
                for i in range(attempts)))
        return Counter(r.status_code for r in responses)

    # Rendez-vous (1 place), 20 clients simultanés : exactement 1 succès.
    assert asyncio.run(rush(venue["cut_slot"], 20)) == {201: 1, 409: 19}
    # Cours de 5 places, 12 clients pour 1 place puis on vérifie l'état final.
    assert asyncio.run(rush(venue["lesson_slot"], 12)) == {201: 5, 409: 7}

    async def state(conn):
        return await conn.fetch(
            """SELECT s.id, s.remaining, s.capacity, coalesce(sum(b.seats), 0)::int AS held
               FROM slot s LEFT JOIN booking b ON b.slot_id = s.id AND b.status = 'held'
               WHERE s.id = ANY($1::bigint[]) GROUP BY s.id""",
            [venue["cut_slot"], venue["lesson_slot"]])

    for row in db_call(state):
        # Invariant : places restantes + places bloquées = capacité.
        assert (row["remaining"], row["remaining"] + row["held"]) == (0, row["capacity"])
