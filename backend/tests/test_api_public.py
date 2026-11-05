"""API publique contre une vraie base et un vrai uvicorn."""
from collections.abc import Iterator
from datetime import datetime, timedelta

import httpx
import pytest

from app.main import CONTENT_SECURITY_POLICY
from tests.dbutil import (EVENT_CATEGORY, db_call, delete_merchants, insert_merchant, insert_service,
                          insert_slot, requires_db)

pytestmark = requires_db

# Zone loin de Paris : isolée des données du seed.
AREA_BBOX = "2.0,10.0,2.01,10.01"


@pytest.fixture(scope="module")
def area(server: str) -> Iterator[dict]:
    """Plafond de remise 30 % partout : paliers -10 % (> H-6), -20 % (H-6), -30 % (H-2)."""
    async def setup(conn):
        a = await insert_merchant(conn, "Test Coiffure", "coiffure", 2.0005, 10.0005)
        b = await insert_merchant(conn, "Test Cours", EVENT_CATEGORY, 2.0015, 10.0010)
        outside = await insert_merchant(conn, "Test Hors zone", "coiffure", 2.5, 10.005)
        sa = await insert_service(conn, a, "Coupe test", 30, 3000)
        sb = await insert_service(conn, b, "Cours test", 90, 2500)
        so = await insert_service(conn, outside, "Coupe test", 30, 3000)
        return {
            "merchants": [a, b, outside],
            "a_soon": await insert_slot(conn, a, sa, starts_in_min=60),  # H-2 : -30 % -> 21 €
            "b_event": await insert_slot(conn, b, sb, starts_in_min=180, base_price_cents=2500,
                                         duration_min=90, capacity=40, remaining=5),  # -20 % -> 20 €
            "a_later": await insert_slot(conn, a, sa, starts_in_min=600),  # -10 % -> 27 €
            "a_beyond_48h": await insert_slot(conn, a, sa, starts_in_min=50 * 60),
            "a_cancelled": await insert_slot(conn, a, sa, starts_in_min=90, status="cancelled"),
            "a_full": await insert_slot(conn, a, sa, starts_in_min=120, remaining=0),
            "a_past": await insert_slot(conn, a, sa, starts_in_min=-60),
            "outside": await insert_slot(conn, outside, so, starts_in_min=60),
        }

    data = db_call(setup)
    yield data
    db_call(lambda conn: delete_merchants(conn, data["merchants"]))


def slot_ids(response: httpx.Response) -> list[int]:
    assert response.status_code == 200, response.text
    return [slot["id"] for slot in response.json()]


def test_config(client: httpx.Client) -> None:
    body = client.get("/api/config").json()
    assert set(body) == {"app_name", "map_style_url", "hold_seconds", "default_center",
                         "default_zoom", "commission_pct"}
    lng, lat = body["default_center"]
    assert -180 <= lng <= 180 and -90 <= lat <= 90
    assert 0 <= body["commission_pct"] < 100


def test_categories_are_the_v1_sectors_in_display_order(client: httpx.Client) -> None:
    codes = [c["code"] for c in client.get("/api/categories").json() if c["code"] != EVENT_CATEGORY]
    assert codes == ["coiffure", "barbier", "esthetique", "ongles", "padel", "foot5", "tennis"]


def test_slots_only_bookable_in_bbox_sorted_by_time(client: httpx.Client, area: dict) -> None:
    # Exclus : annulé, complet, passé, au-delà de 48 h, hors bbox.
    response = client.get("/api/slots", params={"bbox": AREA_BBOX})
    assert slot_ids(response) == [area["a_soon"], area["b_event"], area["a_later"]]


def test_slots_within_hours(client: httpx.Client, area: dict) -> None:
    assert slot_ids(client.get("/api/slots", params={"bbox": AREA_BBOX, "within_hours": 2})) == [
        area["a_soon"]
    ]
    assert slot_ids(client.get("/api/slots", params={"bbox": AREA_BBOX, "within_hours": 168})) == [
        area["a_soon"], area["b_event"], area["a_later"], area["a_beyond_48h"]
    ]


def test_slots_category_is_repeatable(client: httpx.Client, area: dict) -> None:
    courses = client.get("/api/slots", params={"bbox": AREA_BBOX, "category": EVENT_CATEGORY})
    assert slot_ids(courses) == [area["b_event"]]
    both = client.get("/api/slots", params=[("bbox", AREA_BBOX), ("category", "coiffure"),
                                            ("category", EVENT_CATEGORY)])
    assert slot_ids(both) == [area["a_soon"], area["b_event"], area["a_later"]]


def test_slots_max_price_uses_current_price(client: httpx.Client, area: dict) -> None:
    # Prix courants : 21 € (-30 %), 20 € (-20 %), 27 € (-10 %).
    response = client.get("/api/slots", params={"bbox": AREA_BBOX, "max_price": 21})
    assert slot_ids(response) == [area["a_soon"], area["b_event"]]


def test_slots_payload_with_progressive_discount(client: httpx.Client, area: dict) -> None:
    slots = {s["id"]: s for s in client.get("/api/slots", params={"bbox": AREA_BBOX}).json()}
    soon = slots[area["a_soon"]]
    assert (soon["base_price_cents"], soon["max_discount_pct"]) == (3000, 30)
    assert (soon["discount_pct"], soon["price_cents"]) == (30, 2100)  # plafond atteint à H-2
    assert soon["next_discount_pct"] is None and soon["next_discount_at"] is None
    assert soon["service_name"] == "Coupe test"
    assert soon["capacity"] == soon["remaining"] == 1
    assert soon["distance_m"] is None
    assert soon["merchant"] == {
        "id": area["merchants"][0], "name": "Test Coiffure", "address": "Adresse de test",
        "category": "coiffure", "lat": pytest.approx(10.0005), "lng": pytest.approx(2.0005),
    }

    event = slots[area["b_event"]]
    assert (event["capacity"], event["remaining"]) == (40, 5)
    assert (event["discount_pct"], event["price_cents"], event["next_discount_pct"]) == (20, 2000, 30)
    assert (datetime.fromisoformat(event["next_discount_at"])
            == datetime.fromisoformat(event["starts_at"]) - timedelta(hours=2))

    later = slots[area["a_later"]]
    assert (later["discount_pct"], later["price_cents"], later["next_discount_pct"]) == (10, 2700, 20)
    assert (datetime.fromisoformat(later["next_discount_at"])
            == datetime.fromisoformat(later["starts_at"]) - timedelta(hours=6))


def test_slots_distance_in_meters(client: httpx.Client, area: dict) -> None:
    response = client.get(
        "/api/slots", params={"bbox": AREA_BBOX, "lat": 10.0005, "lng": 2.0005}
    )
    distances = {s["id"]: s["distance_m"] for s in response.json()}
    assert distances[area["a_soon"]] == 0
    # Δlat 0,0005° ≈ 55 m, Δlng 0,001° × cos(10°) ≈ 110 m → ≈ 123 m sur l'ellipsoïde.
    assert 115 <= distances[area["b_event"]] <= 130


@pytest.mark.parametrize("params", [
    {},
    {"bbox": "1,2,3"},
    {"bbox": "a,b,c,d"},
    {"bbox": "nan,10,2.01,10.01"},
    {"bbox": "inf,10,2.01,10.01"},
    {"bbox": "2.01,10,2.0,10.01"},  # w > e
    {"bbox": "2,10.01,2.01,10"},  # s > n
    {"bbox": "-181,10,2.01,10.01"},
    {"bbox": "2,10,2.01,91"},
    {"bbox": AREA_BBOX, "within_hours": 0},
    {"bbox": AREA_BBOX, "within_hours": 169},
    {"bbox": AREA_BBOX, "max_price": 0},
    {"bbox": AREA_BBOX, "lat": 10.0},  # lat sans lng
    {"bbox": AREA_BBOX, "lat": 95, "lng": 2.0},
    {"bbox": AREA_BBOX, "category": [f"c{i}" for i in range(21)]},
])
def test_slots_rejects_invalid_parameters(client: httpx.Client, params: dict) -> None:
    assert client.get("/api/slots", params=params).status_code == 422


def app_js_headers(client: httpx.Client) -> httpx.Headers:
    return client.get("/app.js").headers


def test_security_headers_and_static_files(client: httpx.Client) -> None:
    index = client.get("/")
    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert "Données fictives de démonstration" in index.text
    for response in (index, client.get("/api/config")):
        assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"

    # Modules ES : le navigateur exige un type MIME JavaScript.
    for module in ("/app.js", "/common.js", "/pro.js"):
        response = client.get(module)
        assert response.status_code == 200 and "javascript" in response.headers["content-type"], module
    for path in ("/style.css", "/pro.css", "/pro.html", "/vendor/maplibre-gl.js"):
        assert client.get(path).status_code == 200, path

    # Revalidation systématique : pas d'ancien app.js servi depuis le cache après une modification.
    assert app_js_headers(client)["cache-control"] == "no-cache"
    etag = app_js_headers(client)["etag"]
    assert client.get("/app.js", headers={"If-None-Match": etag}).status_code == 304

    # Swagger UI charge ses scripts depuis un CDN : pas de CSP sur /docs.
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "content-security-policy" not in docs.headers
    assert docs.headers["x-content-type-options"] == "nosniff"
