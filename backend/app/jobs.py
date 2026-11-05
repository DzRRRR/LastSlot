"""Tâches de fond : libération des places bloquées (holds) dont le délai de paiement a expiré,
annonce des paliers de remise franchis (la remise progressive change le prix sans écriture)."""
import asyncio
import logging
from datetime import datetime

import asyncpg

from .broker import broker

log = logging.getLogger(__name__)

# Une seule instruction, donc une seule transaction : les holds expirés passent en 'expired'
# et leurs places sont restituées atomiquement. Le confirm (paiement) exige lui aussi
# status = 'held' sur la même ligne : celui des deux qui verrouille la ligne en premier gagne,
# l'autre ré-évalue sa clause WHERE après le verrou et ne trouve plus rien à modifier.
EXPIRE_HOLDS_SQL = """
WITH exp AS (
    UPDATE booking SET status = 'expired'
    WHERE status = 'held' AND expires_at <= now()
    RETURNING slot_id, seats
), agg AS (
    SELECT slot_id, sum(seats) AS seats FROM exp GROUP BY slot_id
)
UPDATE slot SET remaining = slot.remaining + agg.seats
FROM agg
WHERE slot.id = agg.slot_id
RETURNING slot.id, slot.merchant_id, slot.remaining, slot.status
"""


async def expire_holds(pool: asyncpg.Pool) -> int:
    """Libère les holds expirés, publie un événement par créneau touché, renvoie leur nombre."""
    rows = await pool.fetch(EXPIRE_HOLDS_SQL)
    for row in rows:
        broker.publish(
            "slot",
            {
                "id": row["id"],
                "merchant_id": row["merchant_id"],
                "remaining": row["remaining"],
                "status": row["status"],
                "change": "released",
            },
        )
    return len(rows)


# Créneaux réservables dont un palier de remise (H-6 ou H-2, voir discount_at dans schema.sql)
# est tombé dans ]$1, $2]. Écrit sur starts_at pour profiter de l'index slot_open_starts_idx.
DISCOUNT_STEPS_SQL = """
SELECT id, merchant_id, remaining, status
FROM slot
WHERE status = 'open' AND remaining > 0
  AND (starts_at > $1::timestamptz + interval '6 hours' AND starts_at <= $2::timestamptz + interval '6 hours'
    OR starts_at > $1::timestamptz + interval '2 hours' AND starts_at <= $2::timestamptz + interval '2 hours')
"""


async def announce_discount_steps(pool: asyncpg.Pool, since: datetime) -> datetime:
    """Publie un événement `slot` (change: "discount") pour chaque créneau dont la remise vient
    de monter d'un palier, afin que les cartes ouvertes rechargent le nouveau prix.
    Renvoie l'instant (horloge de la base) jusqu'auquel les paliers ont été traités."""
    async with pool.acquire() as conn:
        until = await conn.fetchval("SELECT now()")
        rows = await conn.fetch(DISCOUNT_STEPS_SQL, since, until)
    for row in rows:
        broker.publish("slot", {"id": row["id"], "merchant_id": row["merchant_id"],
                                "remaining": row["remaining"], "status": row["status"],
                                "change": "discount"})
    return until


async def run_jobs_loop(pool: asyncpg.Pool, interval: float) -> None:
    # Point de départ : maintenant. Les paliers franchis avant le démarrage ne sont pas annoncés,
    # les clients qui se connectent chargent de toute façon les prix courants.
    steps_checked_until = await pool.fetchval("SELECT now()")
    while True:
        try:
            released = await expire_holds(pool)
            if released:
                log.info("holds expirés : %d créneau(x) libéré(s)", released)
            steps_checked_until = await announce_discount_steps(pool, steps_checked_until)
        except Exception:
            # Erreur ponctuelle (base redémarrée, réseau) : on journalise et on réessaie au
            # tour suivant. CancelledError hérite de BaseException et traverse ce bloc,
            # ce qui laisse le lifespan arrêter la boucle proprement.
            log.exception("échec d'une tâche de fond")
        await asyncio.sleep(interval)
