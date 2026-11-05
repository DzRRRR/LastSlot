"""Remise progressive (executive summary) : paliers calculés par PostgreSQL (discount_at,
discounted_price_cents dans schema.sql), et refus d'un schéma d'une ancienne version."""
import asyncio

import asyncpg
import pytest

from app.db import SCHEMA_VERSION, SchemaVersionError, apply_schema
from tests.dbutil import TEST_DATABASE_URL, db_call, requires_db

pytestmark = requires_db

# Paliers attendus (plafond -> remise la veille / dès H-6 / dès H-2), arrondis à 5 %.
TIERS = {
    5: (5, 5, 5), 10: (5, 5, 10), 20: (5, 15, 20), 25: (10, 15, 25), 30: (10, 20, 30),
    35: (10, 25, 35), 40: (15, 25, 40), 50: (15, 35, 50), 80: (25, 55, 80),
}


@pytest.mark.parametrize("max_pct", list(TIERS))
def test_discount_tiers(seeded: dict, max_pct: int) -> None:
    async def tiers(conn):
        # Bornes incluses : à H-6 pile on est au 2e palier, à H-2 pile au plafond.
        return tuple(await conn.fetchrow(
            """SELECT discount_at(now() + interval '30 hours', $1, now()),
                      discount_at(now() + interval '6 hours 1 second', $1, now()),
                      discount_at(now() + interval '6 hours', $1, now()),
                      discount_at(now() + interval '2 hours 1 second', $1, now()),
                      discount_at(now() + interval '2 hours', $1, now()),
                      discount_at(now() + interval '10 minutes', $1, now())""", max_pct))

    early, mid = TIERS[max_pct][0], TIERS[max_pct][1]
    assert db_call(tiers) == (early, early, mid, mid, max_pct, max_pct)


def test_executive_summary_example(seeded: dict) -> None:
    """Plafond 30 % : -10 % la veille, -20 % à H-6, -30 % à H-2."""
    assert TIERS[30] == (10, 20, 30)


def test_discounted_price_rounds_half_up_and_never_zero(seeded: dict) -> None:
    async def prices(conn):
        return tuple(await conn.fetchrow(
            "SELECT discounted_price_cents(2800, 30), discounted_price_cents(1505, 10), "
            "discounted_price_cents(1, 80)"))

    assert db_call(prices) == (1960, 1355, 1)  # 1354,5 -> 1355 ; jamais 0


def test_outdated_schema_is_refused(seeded: dict) -> None:
    async def run():
        pool = await asyncpg.create_pool(TEST_DATABASE_URL, min_size=1, max_size=1)
        try:
            await pool.execute("UPDATE schema_version SET version = 1")
            with pytest.raises(SchemaVersionError, match="--reset"):
                await apply_schema(pool)
        finally:
            await pool.execute("UPDATE schema_version SET version = $1", SCHEMA_VERSION)
            await pool.close()

    asyncio.run(run())
