"""Accès à la base de test depuis des tests synchrones, et fabrique de données de test."""
import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg
import pytest

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
# Catégorie de famille 'event' (séances à places multiples) : la V1 n'en a pas, mais l'API la
# gère ; les tests la créent eux-mêmes.
EVENT_CATEGORY = "cours-test"

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL non défini (base PostgreSQL + PostGIS jetable)"
)


def db_call(fn: Callable[[asyncpg.Connection], Awaitable[Any]]) -> Any:
    """Exécute `await fn(conn)` sur une connexion neuve à la base de test."""

    async def run() -> Any:
        conn = await asyncpg.connect(TEST_DATABASE_URL)
        try:
            return await fn(conn)
        finally:
            await conn.close()

    return asyncio.run(run())


async def insert_merchant(conn: asyncpg.Connection, name: str, category: str,
                          lng: float, lat: float, token: str | None = None) -> int:
    """Commerce de test ; `token` connu pour appeler /api/pro/*, sinon hash aléatoire."""
    if category == EVENT_CATEGORY:
        await conn.execute(
            "INSERT INTO category (code, label, family) VALUES ($1, 'Cours collectif (test)', 'event') "
            "ON CONFLICT (code) DO NOTHING",
            EVENT_CATEGORY,
        )
    token_hash = hashlib.sha256(token.encode()).digest() if token else os.urandom(32)
    return await conn.fetchval(
        """INSERT INTO merchant (name, category_code, address, location, token_hash)
           VALUES ($1, $2, 'Adresse de test', ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography, $5)
           RETURNING id""",
        name, category, lng, lat, token_hash,
    )


async def insert_resource(conn: asyncpg.Connection, merchant_id: int, name: str) -> int:
    return await conn.fetchval(
        "INSERT INTO resource (merchant_id, name) VALUES ($1, $2) RETURNING id", merchant_id, name
    )


async def insert_booking(conn: asyncpg.Connection, slot_id: int, status: str, *, seats: int = 1,
                         name: str = "Client test", contact: str = "client@example.org",
                         expires_in_s: int = 300) -> None:
    """Réservation brute (sans toucher à slot.remaining) ; expires_at n'est rempli que pour 'held'."""
    await conn.execute(
        """INSERT INTO booking (slot_id, customer_name, customer_contact, seats, status, expires_at)
           VALUES ($1, $2, $3, $4, $5::text,
                   CASE WHEN $5::text = 'held' THEN now() + make_interval(secs => $6::int) END)""",
        slot_id, name, contact, seats, status, expires_in_s,
    )


async def insert_service(conn: asyncpg.Connection, merchant_id: int, name: str,
                         duration_min: int, base_price_cents: int) -> int:
    return await conn.fetchval(
        """INSERT INTO service (merchant_id, name, duration_min, base_price_cents)
           VALUES ($1, $2, $3, $4) RETURNING id""",
        merchant_id, name, duration_min, base_price_cents,
    )


async def insert_slot(conn: asyncpg.Connection, merchant_id: int, service_id: int, *,
                      starts_in_min: float, base_price_cents: int = 3000, max_discount_pct: int = 30,
                      duration_min: int = 30, capacity: int = 1, remaining: int | None = None,
                      status: str = "open", resource_id: int | None = None) -> int:
    """Créneau relatif à now() (starts_in_min peut être négatif ou fractionnaire)."""
    return await conn.fetchval(
        """INSERT INTO slot (merchant_id, service_id, resource_id, starts_at, ends_at, capacity,
                             remaining, base_price_cents, max_discount_pct, status)
           VALUES ($1, $2, $10, now() + make_interval(secs => $3::float8 * 60),
                   now() + make_interval(secs => $3::float8 * 60 + $4 * 60), $5, $6, $7, $8, $9)
           RETURNING id""",
        merchant_id, service_id, float(starts_in_min), duration_min, capacity,
        capacity if remaining is None else remaining, base_price_cents, max_discount_pct, status,
        resource_id,
    )


async def delete_merchants(conn: asyncpg.Connection, ids: list[int]) -> None:
    # ON DELETE CASCADE : services, praticiens, créneaux et réservations partent avec.
    await conn.execute("DELETE FROM merchant WHERE id = ANY($1::bigint[])", ids)
