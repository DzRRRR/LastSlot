"""Routes publiques (sans authentification) : configuration du client, catégories,
recherche de créneaux dans le viewport de la carte, flux temps réel SSE."""
import asyncio
import contextlib
import math
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.types import Receive, Scope, Send

from .broker import broker
from .config import settings

router = APIRouter(prefix="/api", tags=["public"])

MAX_SLOTS = 500
UNPROCESSABLE = status.HTTP_422_UNPROCESSABLE_CONTENT


class AppConfig(BaseModel):
    app_name: str
    map_style_url: str
    hold_seconds: int
    default_center: tuple[float, float]  # (lng, lat) : ordre attendu par MapLibre
    default_zoom: float
    commission_pct: int


class Category(BaseModel):
    code: str
    label: str
    family: str


class Pricing(BaseModel):
    """Prix d'un créneau à l'instant de la requête (remise progressive, voir schema.sql)."""
    base_price_cents: int
    max_discount_pct: int  # plafond fixé par le pro
    discount_pct: int  # remise appliquée maintenant
    price_cents: int  # prix payé si on réserve maintenant
    next_discount_pct: int | None  # prochain palier, null si le plafond est atteint
    next_discount_at: datetime | None


class MerchantOut(BaseModel):
    id: int
    name: str
    address: str
    category: str
    lat: float
    lng: float


class SlotOut(Pricing):
    id: int
    starts_at: datetime
    ends_at: datetime
    remaining: int
    capacity: int
    service_name: str
    merchant: MerchantOut
    distance_m: int | None  # null si lat/lng non fournis


# Paramètres : $1..$4 bbox (w, s, e, n), $5 horizon en heures, $6 catégories (text[] ou NULL),
# $7 prix max en centimes (ou NULL), $8/$9 position du client (lat, lng ou NULL), $10 limite.
# Les casts explicites ($n::type) sont nécessaires : asyncpg prépare la requête côté serveur et
# PostgreSQL ne peut pas déduire le type d'un paramètre comparé seulement à NULL.
# slot_priced (vue, schema.sql) : remise et prix courants, prochain palier.
SLOTS_SQL = """
SELECT s.id, s.starts_at, s.ends_at, s.remaining, s.capacity,
       s.base_price_cents, s.max_discount_pct, s.discount_pct, s.price_cents,
       s.next_discount_pct, s.next_discount_at,
       sv.name AS service_name,
       m.id AS merchant_id, m.name AS merchant_name, m.address, m.category_code,
       ST_Y(m.location::geometry) AS lat,
       ST_X(m.location::geometry) AS lng,
       -- ST_MakePoint est STRICT : sans position, toute l'expression vaut NULL.
       round(ST_Distance(
           m.location, ST_SetSRID(ST_MakePoint($9::float8, $8::float8), 4326)::geography
       ))::int AS distance_m
FROM slot_priced s
JOIN service sv ON sv.id = s.service_id
JOIN merchant m ON m.id = s.merchant_id
WHERE s.status = 'open'
  AND s.remaining > 0
  AND s.starts_at > now()
  AND s.starts_at < now() + make_interval(hours => $5::int)
  -- Pour un point, `&&` (intersection de boîtes englobantes) est exact ; utilise l'index
  -- merchant_location_geom_gix.
  AND m.location::geometry && ST_MakeEnvelope($1::float8, $2::float8, $3::float8, $4::float8, 4326)
  AND ($6::text[] IS NULL OR m.category_code = ANY($6::text[]))
  AND ($7::int IS NULL OR s.price_cents <= $7::int)
ORDER BY s.starts_at, s.id
LIMIT $10::int
"""


def parse_bbox(raw: str) -> tuple[float, float, float, float]:
    """`w,s,e,n` en degrés WGS84 → tuple validé, sinon 422.

    w < e exclut les vues qui traversent l'antiméridien : le client les borne à [-180, 180].
    """
    parts = raw.split(",")
    try:
        if len(parts) != 4:
            raise ValueError
        w, s, e, n = (float(p) for p in parts)
    except ValueError:
        raise HTTPException(UNPROCESSABLE, "bbox attendu : w,s,e,n (4 nombres)") from None
    # float() accepte "nan" et "inf" : on les écarte explicitement.
    if not all(map(math.isfinite, (w, s, e, n))):
        raise HTTPException(UNPROCESSABLE, "bbox : valeurs non finies")
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise HTTPException(
            UNPROCESSABLE, "bbox hors bornes ou inversé (attendu -180 ≤ w < e ≤ 180, -90 ≤ s < n ≤ 90)"
        )
    return w, s, e, n


@router.get("/config")
async def get_config() -> AppConfig:
    return AppConfig(
        app_name=settings.app_name,
        map_style_url=settings.map_style_url,
        hold_seconds=settings.hold_seconds,
        default_center=settings.default_center,
        default_zoom=settings.default_zoom,
        commission_pct=settings.commission_pct,
    )


@router.get("/categories")
async def list_categories(request: Request) -> list[Category]:
    rows = await request.app.state.pool.fetch(
        "SELECT code, label, family FROM category ORDER BY position, label"
    )
    return [Category(**dict(row)) for row in rows]


@router.get("/slots")
async def search_slots(
    request: Request,
    bbox: Annotated[str, Query(max_length=200, description="w,s,e,n en degrés WGS84")],
    category: Annotated[list[str] | None, Query(max_length=20, description="répétable")] = None,
    max_price: Annotated[int | None, Query(ge=1, le=10_000, description="prix max en euros")] = None,
    within_hours: Annotated[int, Query(ge=1, le=168)] = 48,
    lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
    lng: Annotated[float | None, Query(ge=-180, le=180)] = None,
) -> list[SlotOut]:
    w, s, e, n = parse_bbox(bbox)
    if (lat is None) != (lng is None):
        raise HTTPException(UNPROCESSABLE, "lat et lng vont ensemble")
    max_price_cents = None if max_price is None else max_price * 100

    rows = await request.app.state.pool.fetch(
        SLOTS_SQL, w, s, e, n, within_hours, category, max_price_cents, lat, lng, MAX_SLOTS
    )
    return [
        SlotOut(
            id=row["id"],
            starts_at=row["starts_at"],
            ends_at=row["ends_at"],
            remaining=row["remaining"],
            capacity=row["capacity"],
            base_price_cents=row["base_price_cents"],
            max_discount_pct=row["max_discount_pct"],
            discount_pct=row["discount_pct"],
            price_cents=row["price_cents"],
            next_discount_pct=row["next_discount_pct"],
            next_discount_at=row["next_discount_at"],
            service_name=row["service_name"],
            merchant=MerchantOut(
                id=row["merchant_id"],
                name=row["merchant_name"],
                address=row["address"],
                category=row["category_code"],
                lat=row["lat"],
                lng=row["lng"],
            ),
            distance_m=row["distance_m"],
        )
        for row in rows
    ]


async def event_stream(request: Request, heartbeat: float) -> AsyncIterator[str]:
    """Générateur SSE d'un abonné : événements du broker, `: ping` après `heartbeat` s de silence."""
    queue = broker.subscribe()
    getter: asyncio.Task[str] | None = None
    try:
        # Délai de reconnexion conseillé à EventSource (ms). Sans champ data, rien n'est dispatché.
        yield "retry: 3000\n\n"
        # Le broker désabonne un client trop lent (file pleine) : le flux se termine alors,
        # EventSource se reconnecte et le client recharge l'état complet.
        while broker.is_subscribed(queue):
            if getter is None:
                getter = asyncio.create_task(queue.get())
            # asyncio.wait n'annule pas `getter` à l'échéance, contrairement à wait_for : un
            # message arrivé pile au timeout n'est jamais perdu, il est lu au tour suivant.
            done, _ = await asyncio.wait({getter}, timeout=heartbeat)
            if done:
                message = getter.result()
                getter = None
                yield message
            elif await request.is_disconnected():
                break
            else:
                yield ": ping\n\n"
    finally:
        if getter is not None:
            getter.cancel()
        broker.unsubscribe(queue)


class EventStreamResponse(StreamingResponse):
    """Flux SSE qui se termine proprement quand uvicorn coupe les connexions à l'arrêt.

    Un flux SSE ne finit jamais de lui-même : avec --timeout-graceful-shutdown, uvicorn annule
    la tâche de la requête. Laissée telle quelle, l'annulation remonte jusqu'à uvicorn qui
    journalise une trace « Exception in ASGI application » à chaque arrêt ou --reload avec un
    onglet ouvert. Ici on envoie le dernier chunk : le navigateur voit une fin de flux normale
    et EventSource se reconnecte quand le serveur revient.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except asyncio.CancelledError:
            # Seule source d'annulation externe de cette tâche : l'arrêt du serveur (une
            # déconnexion client est gérée à l'intérieur de StreamingResponse).
            with contextlib.suppress(Exception):
                await send({"type": "http.response.body", "body": b"", "more_body": False})


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    return EventStreamResponse(
        event_stream(request, settings.sse_heartbeat_seconds),
        media_type="text/event-stream",
        # X-Accel-Buffering : demande aux reverse proxies (nginx, Traefik) de ne pas tamponner.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
