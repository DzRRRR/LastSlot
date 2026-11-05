"""Routes commerçant (/api/pro/*).

Toutes dépendent de `current_merchant_id` : le commerce est TOUJOURS celui du token, jamais un
identifiant fourni par le client, et chaque requête SQL filtre sur ce merchant_id (anti-IDOR).
Les ids des réservations ne sont jamais renvoyés au commerçant : ils servent de capability
au client pour confirmer ou annuler sa réservation.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import AwareDatetime, BaseModel, Field

from .api_public import Pricing
from .broker import broker
from .security import current_merchant_id

router = APIRouter(prefix="/api/pro", tags=["pro"])

MerchantId = Annotated[int, Depends(current_merchant_id)]

MAX_ADVANCE = timedelta(days=14)
DEADLOCK_RETRIES = 3
UNPROCESSABLE = status.HTTP_422_UNPROCESSABLE_CONTENT


class ProMerchant(BaseModel):
    id: int
    name: str
    address: str
    category: str
    category_label: str
    family: str  # 'appointment' | 'event'
    lat: float
    lng: float


class ProService(BaseModel):
    id: int
    name: str
    duration_min: int
    base_price_cents: int


class ProResource(BaseModel):
    id: int
    name: str


class ProMe(BaseModel):
    merchant: ProMerchant
    services: list[ProService]
    resources: list[ProResource]


class ProBooking(BaseModel):
    customer_name: str
    customer_contact: str
    seats: int
    status: str  # 'held' | 'confirmed'
    expires_at: datetime | None
    created_at: datetime


class ProSlot(Pricing):
    id: int
    service_id: int
    service_name: str
    resource_id: int | None
    resource_name: str | None
    starts_at: datetime
    ends_at: datetime
    capacity: int
    remaining: int
    status: str  # 'open' | 'cancelled'
    bookings: list[ProBooking]


class SlotCreate(BaseModel):
    service_id: int
    resource_id: int | None = None  # rendez-vous sans praticien : premier praticien libre
    starts_at: AwareDatetime  # un horaire sans fuseau est refusé (422)
    # Plafond de remise : la remise appliquée monte par paliers jusqu'à lui (remise progressive).
    max_discount_pct: int = Field(ge=5, le=80)
    capacity: int | None = Field(default=None, ge=1, le=500)  # requis pour un événement


ME_SQL = """
SELECT m.id, m.name, m.address, m.category_code AS category, c.label AS category_label, c.family,
       ST_Y(m.location::geometry) AS lat, ST_X(m.location::geometry) AS lng
FROM merchant m
JOIN category c ON c.code = m.category_code
WHERE m.id = $1
"""

# Créneaux du commerce depuis 2 h (ou un seul si $2 est fourni), avec les réservations actives.
PRO_SLOTS_SQL = """
SELECT s.id, s.service_id, sv.name AS service_name, s.resource_id, r.name AS resource_name,
       s.starts_at, s.ends_at, s.capacity, s.remaining,
       s.base_price_cents, s.max_discount_pct, s.discount_pct, s.price_cents,
       s.next_discount_pct, s.next_discount_at,
       s.status,
       COALESCE((
           SELECT json_agg(json_build_object(
                      'customer_name', b.customer_name, 'customer_contact', b.customer_contact,
                      'seats', b.seats, 'status', b.status, 'expires_at', b.expires_at,
                      'created_at', b.created_at)
                  ORDER BY b.created_at)
           FROM booking b
           WHERE b.slot_id = s.id AND b.status IN ('held', 'confirmed')
       ), '[]') AS bookings
FROM slot_priced s
JOIN service sv ON sv.id = s.service_id
LEFT JOIN resource r ON r.id = s.resource_id
WHERE s.merchant_id = $1
  AND ($2::bigint IS NULL OR s.id = $2::bigint)
  AND s.starts_at >= now() - interval '2 hours'
ORDER BY s.starts_at, s.id
LIMIT 200
"""

SERVICE_SQL = """
SELECT sv.duration_min, sv.base_price_cents, c.family
FROM service sv
JOIN merchant m ON m.id = sv.merchant_id
JOIN category c ON c.code = m.category_code
WHERE sv.id = $1 AND sv.merchant_id = $2
"""

# Premier praticien sans créneau ouvert qui chevauche [début, fin). Une course avec une autre
# publication reste possible entre ce SELECT et l'INSERT : la contrainte d'exclusion tranche (409).
FREE_RESOURCE_SQL = """
SELECT r.id
FROM resource r
WHERE r.merchant_id = $1
  AND NOT EXISTS (
      SELECT 1 FROM slot s
      WHERE s.resource_id = r.id AND s.status = 'open'
        AND tstzrange(s.starts_at, s.ends_at) && tstzrange($2::timestamptz, $3::timestamptz)
  )
ORDER BY r.id
LIMIT 1
"""

INSERT_SLOT_SQL = """
INSERT INTO slot (merchant_id, service_id, resource_id, starts_at, ends_at, capacity, remaining,
                  base_price_cents, max_discount_pct)
VALUES ($1, $2, $3, $4, $5, $6, $6, $7, $8)
RETURNING id
"""


async def fetch_slots(conn: asyncpg.Connection | asyncpg.Pool, merchant_id: int,
                      slot_id: int | None = None) -> list[ProSlot]:
    rows = await conn.fetch(PRO_SLOTS_SQL, merchant_id, slot_id)
    # asyncpg renvoie le json de PostgreSQL sous forme de texte.
    return [ProSlot(**{**dict(row), "bookings": json.loads(row["bookings"])}) for row in rows]


async def _owned_resource(conn: asyncpg.Connection, resource_id: int, merchant_id: int) -> int:
    if not await conn.fetchval(
        "SELECT EXISTS (SELECT FROM resource WHERE id = $1 AND merchant_id = $2)",
        resource_id, merchant_id,
    ):
        raise HTTPException(UNPROCESSABLE, "Praticien ou terrain inconnu pour ce commerce.")
    return resource_id


@router.get("/me")
async def me(request: Request, merchant_id: MerchantId) -> ProMe:
    async with request.app.state.pool.acquire() as conn:
        merchant = await conn.fetchrow(ME_SQL, merchant_id)
        services = await conn.fetch(
            "SELECT id, name, duration_min, base_price_cents FROM service "
            "WHERE merchant_id = $1 ORDER BY id",
            merchant_id,
        )
        resources = await conn.fetch(
            "SELECT id, name FROM resource WHERE merchant_id = $1 ORDER BY id", merchant_id
        )
    return ProMe(
        merchant=ProMerchant(**dict(merchant)),
        services=[ProService(**dict(row)) for row in services],
        resources=[ProResource(**dict(row)) for row in resources],
    )


@router.get("/slots")
async def list_slots(request: Request, merchant_id: MerchantId) -> list[ProSlot]:
    return await fetch_slots(request.app.state.pool, merchant_id)


@router.post("/slots", status_code=status.HTTP_201_CREATED)
async def create_slot(request: Request, merchant_id: MerchantId, body: SlotCreate) -> ProSlot:
    starts_at = body.starts_at.replace(second=0, microsecond=0)
    now = datetime.now(timezone.utc)
    if not now < starts_at < now + MAX_ADVANCE:
        raise HTTPException(UNPROCESSABLE, "Le créneau doit commencer dans le futur et sous 14 jours.")

    async with request.app.state.pool.acquire() as conn:
        service = await conn.fetchrow(SERVICE_SQL, body.service_id, merchant_id)
        if service is None:
            raise HTTPException(UNPROCESSABLE, "Prestation inconnue pour ce commerce.")
        ends_at = starts_at + timedelta(minutes=service["duration_min"])

        resource_id = body.resource_id
        if resource_id is not None:
            await _owned_resource(conn, resource_id, merchant_id)
        if service["family"] == "appointment":
            capacity = 1  # un rendez-vous = un client
            if resource_id is None and await conn.fetchval(
                "SELECT EXISTS (SELECT FROM resource WHERE merchant_id = $1)", merchant_id
            ):
                resource_id = await conn.fetchval(FREE_RESOURCE_SQL, merchant_id, starts_at, ends_at)
                if resource_id is None:
                    raise HTTPException(status.HTTP_409_CONFLICT,
                                        "Aucun praticien ni terrain libre sur ce créneau.")
        else:
            if body.capacity is None:
                raise HTTPException(UNPROCESSABLE, "Nombre de places requis pour un événement.")
            capacity = body.capacity

        try:
            slot_id = await conn.fetchval(
                INSERT_SLOT_SQL, merchant_id, body.service_id, resource_id, starts_at, ends_at,
                capacity, service["base_price_cents"], body.max_discount_pct,
            )
        except asyncpg.ExclusionViolationError:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Ce praticien ou ce terrain a déjà un créneau qui chevauche celui-ci."
            ) from None
        [slot] = await fetch_slots(conn, merchant_id, slot_id)

    broker.publish("slot", {"id": slot_id, "merchant_id": merchant_id, "remaining": capacity,
                            "status": "open", "change": "created"})
    return slot


@router.delete("/slots/{slot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_slot(request: Request, merchant_id: MerchantId, slot_id: int) -> Response:
    """Annule un créneau : 409 s'il a des réservations confirmées, sinon annule les holds.

    Verrous : le créneau (FOR UPDATE) bloque les nouvelles réservations, puis ses réservations
    actives (FOR UPDATE) bloquent un paiement concurrent : un confirm en cours est attendu puis
    vu comme 'confirmed' (→ 409), un confirm ultérieur ne trouve plus de hold.
    Le job d'expiration verrouille dans l'ordre inverse (réservations puis créneau) : en cas
    d'interblocage, PostgreSQL abandonne une des deux transactions et on rejoue.
    """
    pool = request.app.state.pool
    for attempt in range(DEADLOCK_RETRIES):
        try:
            async with pool.acquire() as conn, conn.transaction():
                slot = await conn.fetchrow(
                    "SELECT status, capacity FROM slot WHERE id = $1 AND merchant_id = $2 FOR UPDATE",
                    slot_id, merchant_id,
                )
                if slot is None:  # inexistant ou appartenant à un autre commerce : même réponse
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "Créneau introuvable.")
                if slot["status"] == "cancelled":
                    return Response(status_code=status.HTTP_204_NO_CONTENT)  # idempotent
                active = await conn.fetch(
                    "SELECT status FROM booking WHERE slot_id = $1 AND status IN ('held', 'confirmed') "
                    "FOR UPDATE",
                    slot_id,
                )
                if any(row["status"] == "confirmed" for row in active):
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        "Des réservations sont confirmées : contactez les clients avant d'annuler.",
                    )
                await conn.execute(
                    "UPDATE booking SET status = 'cancelled' WHERE slot_id = $1 AND status = 'held'",
                    slot_id,
                )
                # Plus aucune réservation active : toutes les places reviennent.
                await conn.execute(
                    "UPDATE slot SET status = 'cancelled', remaining = capacity WHERE id = $1", slot_id
                )
            break
        except asyncpg.DeadlockDetectedError:
            if attempt == DEADLOCK_RETRIES - 1:
                raise

    broker.publish("slot", {"id": slot_id, "merchant_id": merchant_id, "remaining": slot["capacity"],
                            "status": "cancelled", "change": "cancelled"})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
