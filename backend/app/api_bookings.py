"""Réservation côté client : prise de place (hold), paiement simulé, annulation, statut.

Pas de compte client dans le proto : l'id de la réservation (UUID v4, 122 bits aléatoires) est
la seule clé. Qui le connaît peut consulter, payer ou annuler CETTE réservation, rien d'autre
(capability). Il n'est jamais montré au commerçant.

Cycle de vie : held (place bloquée HOLD_SECONDS) → confirmed (payée) ; ou → cancelled (annulée
par le client ou avec le créneau) ; ou → expired (délai dépassé, job de jobs.py).
"""
import uuid
from datetime import datetime

import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from .broker import broker
from .config import settings
from .db import retry_on_deadlock

router = APIRouter(prefix="/api/bookings", tags=["réservations"])
UNPROCESSABLE = status.HTTP_422_UNPROCESSABLE_CONTENT


class BookingCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)  # "   " ne passe pas min_length=1

    slot_id: int
    customer_name: str = Field(min_length=1, max_length=80)
    customer_contact: str = Field(min_length=3, max_length=120)  # e-mail ou téléphone
    seats: int = Field(default=1, ge=1, le=10)


class BookingSlot(BaseModel):
    id: int
    starts_at: datetime
    ends_at: datetime
    service_name: str
    merchant_name: str
    merchant_address: str
    merchant_category: str


class BookingOut(BaseModel):
    id: uuid.UUID
    status: str  # held | confirmed | expired | cancelled
    seats: int
    customer_name: str
    customer_contact: str
    base_price_cents: int  # prix habituel d'une place
    discount_pct: int  # remise figée à la prise de place
    unit_price_cents: int
    amount_cents: int  # total à payer (unit_price_cents × seats)
    expires_at: datetime | None
    # Secondes restantes pour payer, calculées par le serveur : le compte à rebours du client ne
    # dépend pas de l'heure de son appareil. Null hors hold.
    expires_in_s: int | None
    created_at: datetime
    confirmed_at: datetime | None
    slot: BookingSlot


# Un hold dont le délai est dépassé est montré 'expired' même si le job ne l'a pas encore traité.
BOOKING_SQL = """
SELECT b.id,
       CASE WHEN b.status = 'held' AND b.expires_at <= now() THEN 'expired' ELSE b.status END AS status,
       b.seats, b.customer_name, b.customer_contact, s.base_price_cents, b.discount_pct,
       b.unit_price_cents, b.amount_cents, b.expires_at,
       CASE WHEN b.status = 'held' AND b.expires_at > now()
            THEN ceil(extract(epoch FROM b.expires_at - now()))::int END AS expires_in_s,
       b.created_at, b.confirmed_at,
       s.id AS slot_id, s.starts_at, s.ends_at, sv.name AS service_name,
       m.name AS merchant_name, m.address AS merchant_address, m.category_code AS merchant_category
FROM booking b
JOIN slot s ON s.id = b.slot_id
JOIN service sv ON sv.id = s.service_id
JOIN merchant m ON m.id = s.merchant_id
WHERE b.id = $1
"""

SLOT_CHECK_SQL = """
SELECT s.status, s.starts_at > now() AS upcoming, c.family
FROM slot s
JOIN merchant m ON m.id = s.merchant_id
JOIN category c ON c.code = m.category_code
WHERE s.id = $1
"""

# Décrément atomique, garde-fou anti-surréservation : l'UPDATE pose un verrou de ligne sur le
# créneau ; une transaction concurrente attend, puis (READ COMMITTED, EvalPlanQual) ré-évalue le
# WHERE sur la nouvelle version de la ligne : si les places viennent d'être prises,
# `remaining >= $2` est faux et elle ne trouve plus rien. Le prix courant (remise progressive) est
# lu dans la même instruction, sous le même verrou.
TAKE_SEATS_SQL = """
UPDATE slot s SET remaining = s.remaining - $2
WHERE s.id = $1 AND s.status = 'open' AND s.remaining >= $2 AND s.starts_at > now()
RETURNING s.merchant_id, s.remaining,
          discount_at(s.starts_at, s.max_discount_pct, now()) AS discount_pct,
          discounted_price_cents(s.base_price_cents,
                                 discount_at(s.starts_at, s.max_discount_pct, now())) AS unit_price_cents
"""

INSERT_BOOKING_SQL = """
INSERT INTO booking (slot_id, customer_name, customer_contact, seats, discount_pct, unit_price_cents,
                     amount_cents, commission_cents, status, expires_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'held', now() + make_interval(secs => $9::int))
RETURNING id
"""

# Paiement simulé. Exige status = 'held' et un délai non dépassé, comme le job d'expiration
# exige status = 'held' : celui des deux qui verrouille la ligne en premier gagne, l'autre ne
# trouve plus rien. Même chose face à l'annulation du créneau par le pro (api_pro.cancel_slot).
CONFIRM_SQL = """
UPDATE booking b SET status = 'confirmed', expires_at = NULL, confirmed_at = now()
FROM slot s
WHERE b.id = $1 AND b.status = 'held' AND b.expires_at > now() AND s.id = b.slot_id
RETURNING b.slot_id, s.merchant_id
"""

# Annulation d'un hold par le client, en une instruction : la réservation passe en 'cancelled' et
# ses places reviennent au créneau. Ordre des verrous : réservation puis créneau (comme le job).
RELEASE_SQL = """
WITH b AS (
    UPDATE booking SET status = 'cancelled'
    WHERE id = $1 AND status = 'held'
    RETURNING slot_id, seats
)
UPDATE slot s SET remaining = s.remaining + b.seats
FROM b
WHERE s.id = b.slot_id
RETURNING s.id, s.merchant_id, s.remaining, s.status
"""


async def fetch_booking(conn: asyncpg.Connection | asyncpg.Pool,
                        booking_id: uuid.UUID) -> BookingOut | None:
    row = await conn.fetchrow(BOOKING_SQL, booking_id)
    if row is None:
        return None
    data = dict(row)
    data["slot"] = BookingSlot(
        id=data.pop("slot_id"), starts_at=data.pop("starts_at"), ends_at=data.pop("ends_at"),
        service_name=data.pop("service_name"), merchant_name=data.pop("merchant_name"),
        merchant_address=data.pop("merchant_address"), merchant_category=data.pop("merchant_category"),
    )
    return BookingOut(**data)


def _unavailable(slot: asyncpg.Record) -> str:
    if slot["status"] == "cancelled":
        return "Ce créneau a été annulé par le commerçant."
    if not slot["upcoming"]:
        return "Ce créneau a déjà commencé."
    return "Plus de place : ce créneau vient d'être réservé."


@router.post("", status_code=status.HTTP_201_CREATED)
async def hold(request: Request, body: BookingCreate) -> BookingOut:
    """Bloque des places pendant HOLD_SECONDS au prix courant, figé dans la réservation."""
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            slot = await conn.fetchrow(SLOT_CHECK_SQL, body.slot_id)
            if slot is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Créneau introuvable.")
            if slot["family"] == "appointment" and body.seats != 1:
                raise HTTPException(UNPROCESSABLE, "Un rendez-vous se réserve pour une seule personne.")
            taken = await conn.fetchrow(TAKE_SEATS_SQL, body.slot_id, body.seats)
            if taken is None:
                raise HTTPException(status.HTTP_409_CONFLICT, _unavailable(slot))
            amount = taken["unit_price_cents"] * body.seats
            # Commission arrondie au centime, demi vers le haut (même règle que l'aperçu pro).
            commission = (amount * settings.commission_pct + 50) // 100
            booking_id = await conn.fetchval(
                INSERT_BOOKING_SQL, body.slot_id, body.customer_name, body.customer_contact,
                body.seats, taken["discount_pct"], taken["unit_price_cents"], amount, commission,
                settings.hold_seconds,
            )
        booking = await fetch_booking(conn, booking_id)

    # Après COMMIT : les cartes retirent le créneau s'il est complet, le pro voit la réservation.
    broker.publish("slot", {"id": body.slot_id, "merchant_id": taken["merchant_id"],
                            "remaining": taken["remaining"], "status": "open", "change": "held"})
    broker.publish("booking", {"slot_id": body.slot_id, "merchant_id": taken["merchant_id"],
                               "status": "held"})
    return booking


@router.get("/{booking_id}")
async def get_booking(request: Request, booking_id: uuid.UUID) -> BookingOut:
    booking = await fetch_booking(request.app.state.pool, booking_id)
    if booking is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Réservation introuvable.")
    return booking


@router.post("/{booking_id}/confirm")
async def confirm(request: Request, booking_id: uuid.UUID) -> BookingOut:
    """Paiement simulé. Idempotent : confirmer une réservation déjà payée la renvoie telle quelle
    (double clic, réseau qui rejoue la requête)."""
    async with request.app.state.pool.acquire() as conn:
        confirmed = await conn.fetchrow(CONFIRM_SQL, booking_id)
        booking = await fetch_booking(conn, booking_id)
    if booking is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Réservation introuvable.")
    if confirmed is None:
        if booking.status == "confirmed":
            return booking
        reasons = {
            "expired": "Délai de paiement dépassé : la place a été libérée.",
            "cancelled": "Réservation annulée : la place n'est plus bloquée pour vous.",
        }
        raise HTTPException(status.HTTP_409_CONFLICT, reasons.get(booking.status, "Paiement impossible."))
    broker.publish("booking", {"slot_id": confirmed["slot_id"], "merchant_id": confirmed["merchant_id"],
                               "status": "confirmed"})
    return booking


@router.delete("/{booking_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel(request: Request, booking_id: uuid.UUID) -> Response:
    """Le client renonce avant de payer : les places reviennent au créneau. Idempotent pour une
    réservation déjà expirée ou annulée ; 409 si elle est payée."""
    pool = request.app.state.pool
    released = await retry_on_deadlock(lambda: pool.fetchrow(RELEASE_SQL, booking_id))
    if released is None:
        booking = await fetch_booking(pool, booking_id)
        if booking is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Réservation introuvable.")
        if booking.status == "confirmed":
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "Réservation déjà payée : contactez le commerçant pour l'annuler.")
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    broker.publish("slot", {"id": released["id"], "merchant_id": released["merchant_id"],
                            "remaining": released["remaining"], "status": released["status"],
                            "change": "released"})
    broker.publish("booking", {"slot_id": released["id"], "merchant_id": released["merchant_id"],
                               "status": "cancelled"})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
