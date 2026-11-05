"""Pool de connexions asyncpg, application et réinitialisation du schéma."""
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
# À incrémenter à chaque changement incompatible de schema.sql.
# 1 : prix fixe par créneau ; 2 : remise progressive (plafond + paliers), catégories de la V1.
SCHEMA_VERSION = 2
ADVISORY_LOCK_ID = 424242

# Tout ce que crée schema.sql (sauf les extensions). CASCADE : vues et contraintes dépendantes.
DROP_SQL = """
DROP VIEW IF EXISTS slot_priced;
DROP TABLE IF EXISTS booking, slot, resource, service, merchant, category, schema_version CASCADE;
DROP FUNCTION IF EXISTS discount_at(timestamptz, int, timestamptz);
DROP FUNCTION IF EXISTS discounted_price_cents(int, int);
"""


class SchemaVersionError(RuntimeError):
    pass


class DatabaseUnavailableError(RuntimeError):
    pass


def masked_dsn(dsn: str) -> str:
    """DSN affichable : mot de passe remplacé par ***."""
    parts = urlsplit(dsn)
    if parts.password is None:
        return dsn
    netloc = parts.netloc.replace(f":{parts.password}@", ":***@", 1)
    return urlunsplit(parts._replace(netloc=netloc))


async def create_pool(dsn: str) -> asyncpg.Pool:
    try:
        # min_size/max_size : un proto mono-worker n'a pas besoin de plus de 10 connexions.
        return await asyncpg.create_pool(dsn, min_size=1, max_size=10)
    except (OSError, asyncpg.PostgresError) as exc:
        # OSError : refus de connexion, hôte inconnu, timeout ; PostgresError : mauvais mot de
        # passe, base inexistante. On indique quelle base était visée, sans le mot de passe.
        raise DatabaseUnavailableError(
            f"connexion impossible à {masked_dsn(dsn)} ({exc}). Vérifiez DATABASE_URL et que "
            "la base tourne (HANDOFF.md, « Lancer en local » : docker start creno-db)."
        ) from exc


async def _installed_version(conn: asyncpg.Connection) -> int | None:
    """Version du schéma en base ; None pour une base vierge."""
    if await conn.fetchval("SELECT to_regclass('schema_version')") is not None:
        return await conn.fetchval("SELECT max(version) FROM schema_version")
    if await conn.fetchval("SELECT to_regclass('slot')") is not None:
        return 1  # base créée avant l'introduction du numéro de version
    return None


async def apply_schema(pool: asyncpg.Pool) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        # Verrou consultatif : si deux instances démarrent en même temps,
        # une seule applique le DDL à la fois.
        await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_ID)
        try:
            version = await _installed_version(conn)
            if version is not None and version != SCHEMA_VERSION:
                # Échec explicite plutôt qu'une erreur SQL obscure plus loin.
                raise SchemaVersionError(
                    f"Schéma de base en version {version}, le code attend la version "
                    f"{SCHEMA_VERSION} : recréez la base de démo avec `python -m app.seed --reset`."
                )
            await conn.execute(sql)
            async with conn.transaction():
                await conn.execute("DELETE FROM schema_version")
                await conn.execute("INSERT INTO schema_version (version) VALUES ($1)", SCHEMA_VERSION)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_ID)


async def reset_schema(pool: asyncpg.Pool) -> None:
    """Supprime toutes les tables et fonctions de l'application, puis recrée le schéma courant.
    Destructif : réservé au seed (`--reset`) et aux tests."""
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_ID)
        try:
            await conn.execute(DROP_SQL)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_ID)
    await apply_schema(pool)
